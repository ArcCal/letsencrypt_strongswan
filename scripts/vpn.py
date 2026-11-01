#!/usr/bin/env python3
"""Root-only deployment state, Cloudflare DNS and Apple profile generation."""
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import string
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

STATE = Path('/var/lib/ikev2-vpn')
SYMBOLS = '!@#%+-_=.:?'


def write_private(path, data, mode=0o600):
    """Atomic replacement; never follow an existing output symlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data.encode() if isinstance(data, str) else data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def domain_name(value):
    value = value.strip().lower().rstrip('.')
    if (len(value) > 253 or '.' not in value or
            not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                    for part in value.split('.'))):
        raise ValueError('Enter an ASCII FQDN (use punycode for international domains), without a scheme, port, or wildcard')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError('A domain name is required, not an IP address')


def account():
    while True:
        name = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
        if all(any(c in group for c in name) for group in
               (string.ascii_lowercase, string.ascii_uppercase, string.digits)):
            break
    while True:
        password = ''.join(secrets.choice(string.ascii_letters + string.digits + SYMBOLS)
                           for _ in range(22))
        if all(any(c in group for c in password) for group in
               (string.ascii_lowercase, string.ascii_uppercase, string.digits, SYMBOLS)):
            return {'username': name, 'password': password}


def load_state(root, domain):
    path = root / 'state.json'
    if path.exists():
        data = json.loads(path.read_text())
        if data['domain'] != domain:
            raise ValueError('An existing deployment uses another domain. Migrate it before changing the domain')
        users = data['users']
        if (len(users) != 2 or len({u['username'] for u in users}) != 2 or
                any(not re.fullmatch(r'[A-Za-z0-9]{8}', u['username']) or
                    not re.fullmatch('[' + re.escape(string.ascii_letters + string.digits + SYMBOLS) + ']{22}', u['password'])
                    for u in users)):
            raise ValueError('Saved account state is invalid; accounts will not be overwritten')
        return data
    users = [account(), account()]
    while users[0]['username'] == users[1]['username']:
        users[1] = account()
    data = {'domain': domain, 'users': users}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(path, json.dumps(data))
    return data


def profile(domain, user, address):
    name = user['username']
    identifier = 'org.ikev2-vpn.' + domain + '.' + name
    sa = {'EncryptionAlgorithm': 'AES-256', 'IntegrityAlgorithm': 'SHA2-256',
          'DiffieHellmanGroup': 14, 'LifeTimeInMinutes': 60}
    vpn = {
        'PayloadType': 'com.apple.vpn.managed', 'PayloadVersion': 1,
        'PayloadIdentifier': identifier + '.vpn', 'PayloadUUID': str(uuid.uuid5(uuid.NAMESPACE_DNS, identifier + '.vpn')),
        'PayloadDisplayName': domain + ' (' + name + ')', 'UserDefinedName': domain + ' (' + name + ')',
        'VPNType': 'IKEv2', 'IPv4': {'OverridePrimary': 1},
        'IKEv2': {
            'RemoteAddress': address, 'RemoteIdentifier': domain, 'LocalIdentifier': name,
            'AuthenticationMethod': 'None', 'ExtendedAuthEnabled': 1,
            'AuthName': name, 'AuthPassword': user['password'],
            'ServerCertificateCommonName': domain, 'DeadPeerDetectionRate': 'Medium',
            'DisableMOBIKE': 0, 'EnablePFS': 1,
            'IKESecurityAssociationParameters': dict(sa, LifeTimeInMinutes=240),
            'ChildSecurityAssociationParameters': sa,
            'IncludeAllNetworks': 1, 'ExcludeLocalNetworks': 0,
        },
    }
    return plistlib.dumps({
        'PayloadType': 'Configuration', 'PayloadVersion': 1,
        'PayloadIdentifier': identifier, 'PayloadUUID': str(uuid.uuid5(uuid.NAMESPACE_DNS, identifier)),
        'PayloadDisplayName': domain + ' VPN (' + name + ')',
        'PayloadDescription': 'IKEv2 full-tunnel VPN with username/password login and public CA server certificate validation.',
        'PayloadContent': [vpn],
    })


def render(data, root=Path('/')):
    domain = data['domain']
    address = str(ipaddress.ip_address(data['server_address']))
    (root / 'var/lib/ikev2-vpn/profiles').mkdir(parents=True, exist_ok=True, mode=0o700)
    text = f'''# Managed by ikev2-vpn. Do not put other VPN connections in this file.
connections {{
    ikev2-eap {{
        version = 2
        local_addrs = %any
        pools = vpn4, vpn6
        proposals = aes256-sha256-modp2048
        mobike = yes
        fragmentation = yes
        send_certreq = no
        dpd_delay = 30s
        local {{
            auth = pubkey
            certs = vpn-server.pem
            id = {domain}
        }}
        remote {{
            auth = eap-mschapv2
            eap_id = %any
        }}
        children {{
            net {{
                local_ts = 0.0.0.0/0, ::/0
                esp_proposals = aes256-sha256-modp2048
                reqid = 1
                dpd_action = clear
            }}
        }}
    }}
}}
pools {{
    vpn4 {{
        addrs = 10.10.10.0/24
        dns = 1.1.1.1, 1.0.0.1
    }}
    vpn6 {{
        addrs = fd10:10:10::/64
        dns = 2606:4700:4700::1111, 2606:4700:4700::1001
    }}
}}
secrets {{
'''
    output = ['Server IP: ' + address, 'Certificate domain / Remote ID: ' + domain]
    for user in data['users']:
        name, password = user['username'], user['password']
        text += f'    eap-{name} {{\n        id = {name}\n        secret = "{password}"\n    }}\n'
        write_private(root / f'var/lib/ikev2-vpn/profiles/{name}.mobileconfig', profile(domain, user, address))
        output.extend(['', 'Username: ' + name, 'Password: ' + password,
                       'Profile: /var/lib/ikev2-vpn/profiles/' + name + '.mobileconfig'])
    write_private(root / 'etc/swanctl/conf.d/ikev2-vpn.conf', text + '}\n')
    write_private(root / 'tmp/usr.txt', '\n'.join(output) + '\n')


class Cloudflare:
    def __init__(self, token):
        self.token = token

    def request(self, path, method='GET', data=None):
        req = urllib.request.Request('https://api.cloudflare.com/client/v4/' + path,
                                     data=None if data is None else json.dumps(data).encode(),
                                     headers={'Authorization': 'Bearer ' + self.token,
                                              'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            # Never log response/request bodies which might contain credentials.
            raise RuntimeError(f'Cloudflare API returned HTTP {error.code}; check token permissions and zone scope') from None
        if not body.get('success'):
            raise RuntimeError('Cloudflare API request failed; check DNS permissions and the domain zone')
        return body['result']

    def zone(self, domain):
        parts = domain.split('.')
        for i in range(len(parts) - 1):
            rows = self.request('zones?' + urllib.parse.urlencode({'name': '.'.join(parts[i:]), 'status': 'active'}))
            if rows:
                return rows[0]['id']
        raise ValueError('No active Cloudflare zone found for this domain; Zone:Read and DNS:Edit are required')

def public_ipv4():
    with urllib.request.urlopen('https://api.ipify.org', timeout=10) as response:
        address = ipaddress.ip_address(response.read(128).decode().strip())
    if address.version != 4 or not address.is_global:
        raise ValueError('Could not detect a public IP address')
    # GCE and other clouds map external IPs to private NIC addresses. The public
    # client endpoint must not be required to appear on a local interface.
    # Observing the egress address does not prove inbound UDP reachability.
    return str(address)


def prepare():
    domain = domain_name(sys.stdin.readline().strip())
    token = sys.stdin.readline().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', token):
        raise ValueError('Invalid Cloudflare API token format')
    if (STATE / 'state.json').exists():
        load_state(STATE, domain)  # Validate before any DNS changes.
    if any(path.stem != domain for path in Path('/etc/letsencrypt/renewal').glob('*.conf')):
        raise ValueError('Other certbot certificates exist. This installer requires a dedicated VPN VPS and will not take over their renewal')
    api = Cloudflare(token)
    api.zone(domain)  # Permission check only; never read/write address records.
    ipv4 = public_ipv4()
    write_private('/etc/letsencrypt/cloudflare.ini', 'dns_cloudflare_api_token = ' + token + '\n')
    data = load_state(STATE, domain)
    data['server_address'] = ipv4
    write_private(STATE / 'state.json', json.dumps(data))
    render(data)
    write_private(STATE / 'domain', domain + '\n')
    print('Accounts and profiles generated using the server public IP. No A/AAAA/CNAME records were modified.')


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--export']:
            saved = json.loads((STATE / 'state.json').read_text())
            if 'server_address' not in saved:
                raise ValueError('Legacy state has no server IP. Rerun the latest install.sh; existing accounts will be preserved')
            render(load_state(STATE, domain_name(saved['domain'])))
        elif len(sys.argv) == 1:
            prepare()
        else:
            raise ValueError('Run without arguments to deploy, or use --export to regenerate account files and profiles')
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        # Avoid dumping stack locals or arbitrary HTTP bodies containing tokens.
        print('Configuration failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
