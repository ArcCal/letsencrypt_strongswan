import importlib.util
import io
import ipaddress
import json
from pathlib import Path
import plistlib
import re
import string
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('vpn', Path(__file__).resolve().parents[1] / 'scripts/vpn.py')
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)


class DeploymentTests(unittest.TestCase):
    def test_export_does_not_rewrite_server_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'var/lib/ikev2-vpn'
            data = vpn.load_state(state, 'vpn.example.com')
            data['server_address'] = '203.0.113.10'
            vpn.write_private(state / 'state.json', json.dumps(data))
            server = root / 'etc/swanctl/conf.d/ikev2-vpn.conf'
            vpn.write_private(server, '# existing operator settings\n')
            render = vpn.render
            with mock.patch.object(vpn, 'STATE', state), mock.patch.object(
                    vpn, 'render', side_effect=lambda data, **kwargs: render(data, root, **kwargs)):
                vpn.export_profiles()
            self.assertEqual(server.read_text(), '# existing operator settings\n')
            self.assertEqual(json.loads((state / 'state.json').read_text()), data)
            self.assertEqual(len(list((state / 'profiles').glob('*.mobileconfig'))), 2)
            self.assertTrue((root / 'tmp/usr.txt').exists())

    def test_invalid_saved_state_is_rejected_without_overwrite(self):
        samples = [[], {}, {'domain': None}, {'domain': 'vpn.example.com', 'users': None},
                   {'domain': 'vpn.example.com', 'users': ['first', 'second']},
                   {'domain': 'vpn.example.com', 'users': [{'username': 123}, {}]}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for data in samples:
                with self.subTest(data=data):
                    encoded = json.dumps(data)
                    vpn.write_private(root / 'state.json', encoded)
                    with self.assertRaises(ValueError):
                        vpn.load_state(root, 'vpn.example.com')
                    self.assertEqual((root / 'state.json').read_text(), encoded)

    def test_ssh_ports_cover_socket_activation_and_current_session(self):
        ports = vpn.parse_ssh_ports('port 22\nport 2200\n',
            'LISTEN 0 128 [::]:2222 [::]:* users:(("sshd",pid=7,fd=3))\n'
            'LISTEN 0 128 0.0.0.0:8080 *:* users:(("python3",pid=8,fd=4))',
            '[::]:2022 (Stream)\n0.0.0.0:3022 (Stream)\n/run/ssh22 (Stream)',
            '192.0.2.2 54321 192.0.2.1 4022')
        self.assertEqual(ports, [22, 2022, 2200, 2222, 3022, 4022])
        self.assertEqual(vpn.parse_ssh_ports('', '', '2222 (Stream)', ''), [2222])
        self.assertEqual(vpn.parse_ssh_ports('port 0\nport 65536\n', '', '', 'invalid input'), [22])

    def test_ipv4_pool_avoids_lan_and_matches_firewall(self):
        lan = ipaddress.ip_network('10.10.0.0/16')
        self.assertFalse(vpn.IPV4_POOL.overlaps(lan))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'var/lib/ikev2-vpn'
            data = vpn.load_state(state, 'vpn.example.com')
            data['server_address'] = '203.0.113.10'
            vpn.write_private(state / 'state.json', json.dumps(data))
            original_accounts = list(data['users'])
            original_profiles = [vpn.profile(data['domain'], user, data['server_address'])
                                 for user in data['users']]
            # Reuse an existing account state while regenerating server settings.
            vpn.render(vpn.load_state(state, data['domain']), root)
            server = (root / 'etc/swanctl/conf.d/ikev2-vpn.conf').read_text()
            pool = re.search(r'vpn4\s*\{\s*addrs\s*=\s*(\S+)', server).group(1)
            self.assertEqual(ipaddress.ip_network(pool), vpn.IPV4_POOL)
            self.assertFalse(ipaddress.ip_network(pool).overlaps(lan))
            firewall = (Path(__file__).resolve().parents[1] / 'config/nftables-vpn.conf').read_text()
            self.assertIn(f'ip saddr {pool} drop', firewall)
            self.assertIn(f'ip saddr {pool} meta ipsec exists accept', firewall)
            self.assertIn(f'ip saddr {pool} ip daddr != {pool} masquerade', firewall)
            self.assertEqual(vpn.load_state(state, data['domain'])['users'], original_accounts)
            for user, expected in zip(data['users'], original_profiles):
                self.assertEqual((state / 'profiles' / (user['username'] + '.mobileconfig')).read_bytes(), expected)

    def test_public_ipv4_accepts_cloud_nat_external_address(self):
        # A GCE NIC may only have 10.x addresses; the external IP is cloud-side.
        with mock.patch.object(vpn.urllib.request, 'urlopen', return_value=io.BytesIO(b'8.8.4.4\n')) as request:
            self.assertEqual(vpn.public_ipv4(), '8.8.4.4')
            request.assert_called_once_with('https://api.ipify.org', timeout=10)

    def test_public_ipv4_rejects_nonpublic_and_malformed_responses(self):
        for address in (b'10.128.0.2', b'127.0.0.1', b'169.254.169.254', b'::1', b'2606:4700:4700::1111', b'not-an-ip'):
            with self.subTest(address=address), mock.patch.object(
                    vpn.urllib.request, 'urlopen', return_value=io.BytesIO(address)):
                with self.assertRaises(ValueError):
                    vpn.public_ipv4()

    def test_random_accounts_character_classes(self):
        users = [vpn.account() for _ in range(200)]
        self.assertEqual(len({u['username'] for u in users}), len(users))
        self.assertEqual(len({u['password'] for u in users}), len(users))
        for u in users:
            self.assertRegex(u['username'], r'^[A-Za-z0-9]{8}$')
            self.assertEqual(len(u['password']), 22)
            for field in ('username', 'password'):
                for group in (string.ascii_lowercase, string.ascii_uppercase, string.digits):
                    self.assertTrue(set(u[field]) & set(group))
            self.assertTrue(set(u['password']) & set(vpn.SYMBOLS))

    def test_repeated_install_preserves_accounts_and_profile_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'var/lib/ikev2-vpn'
            initial = vpn.load_state(state, 'vpn.example.com')
            initial['server_address'] = '203.0.113.10'
            vpn.write_private(state / 'state.json', json.dumps(initial))
            vpn.render(initial, root)
            profiles = {p.name: p.read_bytes() for p in (state / 'profiles').glob('*')}
            again = vpn.load_state(state, 'vpn.example.com')
            vpn.render(again, root)
            self.assertEqual(initial, again)
            self.assertEqual(profiles, {p.name: p.read_bytes() for p in (state / 'profiles').glob('*')})
            with self.assertRaises(ValueError):
                vpn.load_state(state, 'other.example.com')
            for path in [state / 'state.json', root / 'tmp/usr.txt',
                         root / 'etc/swanctl/conf.d/ikev2-vpn.conf', *(state / 'profiles').glob('*')]:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_profiles_match_server_credentials_and_full_tunnel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = vpn.load_state(root / 'var/lib/ikev2-vpn', 'vpn.example.com')
            # Colons in credentials must not be mistaken for IPv6 configuration.
            data['users'][0]['password'] = 'Aa1::' + 'x' * 17
            data['server_address'] = '203.0.113.10'
            vpn.render(data, root)
            server = (root / 'etc/swanctl/conf.d/ikev2-vpn.conf').read_text()
            summary = (root / 'tmp/usr.txt').read_text()
            for u in data['users']:
                profile = plistlib.loads((root / f"var/lib/ikev2-vpn/profiles/{u['username']}.mobileconfig").read_bytes())
                self.assertTrue((root / f"var/lib/ikev2-vpn/profiles/{u['username']}.mobileconfig").read_text().isascii())
                self.assertEqual(len(profile['PayloadContent']), 1)  # No client or CA certificate payload.
                payload = profile['PayloadContent'][0]
                self.assertEqual(profile['PayloadDisplayName'], 'IKEv2 IPSec VPN')
                self.assertEqual(payload['PayloadDisplayName'], 'IKEv2 IPSec VPN')
                self.assertEqual(payload['UserDefinedName'], 'IKEv2 IPSec VPN')
                self.assertEqual(payload['VPNType'], 'IKEv2')
                ike = payload['IKEv2']
                self.assertEqual(ike['AuthName'], u['username'])
                self.assertEqual(ike['AuthPassword'], u['password'])
                self.assertEqual(ike['AuthenticationMethod'], 'None')
                self.assertEqual(ike['ExtendedAuthEnabled'], 1)
                self.assertEqual(ike['IncludeAllNetworks'], 1)
                self.assertEqual(ike['ExcludeLocalNetworks'], 0)
                self.assertEqual(ike['OnDemandEnabled'], 1)
                self.assertEqual(ike['OnDemandUserOverrideDisabled'], 1)
                self.assertEqual(ike['OnDemandRules'], [{'Action': 'Connect'}])
                self.assertNotIn('AlwaysOn', payload)
                self.assertNotIn('PayloadRemovalDisallowed', profile)
                self.assertEqual(ike['RemoteIdentifier'], data['domain'])
                self.assertEqual(ike['RemoteAddress'], data['server_address'])
                self.assertNotEqual(ike['RemoteAddress'], ike['RemoteIdentifier'])
                self.assertNotIn('PayloadCertificateUUID', ike)
                self.assertIn(u['username'], server)
                self.assertIn(u['password'], server)
                self.assertIn(u['username'], summary)
                self.assertIn(u['password'], summary)
                self.assertIn('VPN name: IKEv2 IPSec VPN', summary)
            self.assertTrue(summary.isascii())
            self.assertIn('local_ts = 0.0.0.0/0\n', server)
            self.assertNotIn('vpn6', server)
            self.assertNotIn('::', server.split('secrets {', 1)[0])
            pools = re.findall(r'addrs\s*=\s*([^\s]+/\d+)', server)
            self.assertEqual([ipaddress.ip_network(pool).version for pool in pools], [4])

    def test_export_replaces_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'important'
            target.write_text('keep')
            link = root / 'usr.txt'
            link.symlink_to(target)
            vpn.write_private(link, 'accounts')
            self.assertEqual(target.read_text(), 'keep')
            self.assertFalse(link.is_symlink())
            self.assertEqual(link.read_text(), 'accounts')

    def test_invalid_domain_cannot_inject_configuration(self):
        for name in ('https://vpn.example.com', '*.example.com', 'example.com\n}', '127.0.0.1', '-a.example.com', 'a..com'):
            with self.assertRaises(ValueError):
                vpn.domain_name(name)
        self.assertEqual(vpn.domain_name('VPN.Example.com.'), 'vpn.example.com')

    def test_prepare_checks_zone_without_modifying_dns_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            writes = []
            def capture(path, data, *args):
                writes.append(str(path))
                if Path(path).is_relative_to(state):
                    vpn_write(path, data, *args)
            vpn_write = vpn.write_private
            with (mock.patch.object(vpn, 'STATE', state),
                  mock.patch.object(vpn.sys, 'stdin', __import__('io').StringIO('vpn.example.com\n' + 'a' * 40 + '\n')),
                  mock.patch.object(vpn.Cloudflare, 'request', return_value=[{'id': 'zone'}]) as request,
                  mock.patch.object(vpn, 'public_ipv4', return_value='203.0.113.10'),
                  mock.patch.object(vpn, 'write_private', side_effect=capture),
                  mock.patch.object(vpn, 'render') as render):
                vpn.prepare()
                self.assertTrue(request.called)
                for call in request.call_args_list:
                    self.assertTrue(call.args[0].startswith('zones?'))
                    self.assertEqual(len(call.args), 1)  # Default GET; no record mutation.
                    self.assertFalse(call.kwargs)
                self.assertEqual(render.call_args.args[0]['server_address'], '203.0.113.10')
                self.assertEqual(len(render.call_args.args[0]['users']), 2)


if __name__ == '__main__':
    unittest.main()
