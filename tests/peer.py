#!/usr/bin/env python3
"""Actual IKEv2/EAP and ESP checks inside a disposable privileged test container.

Requires tests/runtime_setup.py fixtures and a running source systemd service.
All endpoint and metadata addresses below belong to isolated network namespaces.
"""
import contextlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE = Path('/var/lib/letsencrypt/audit/peer')
CLIENT = 'ikev2-audit-client'
ORIGIN = 'ikev2-audit-origin'
processes = []


def run(args, *, check=True, timeout=20, **kwargs):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          check=check, timeout=timeout, **kwargs)


def namespace(name, *args, **kwargs):
    return run(['ip', 'netns', 'exec', name, *args], **kwargs)


def client_command(pid, *args, **kwargs):
    return run(['nsenter', '-t', str(pid), '-m', '-n', '--', 'env',
                'STRONGSWAN_CONF=' + str(BASE / 'strongswan.conf'), *args], **kwargs)


def http_request(pid, address, port, *, should_work=True):
    # Direct requests only to isolated local fixtures, never real metadata APIs.
    code = ('import http.client; c=http.client.HTTPConnection(' + repr(address) + ',' + str(port) +
            ',timeout=2); c.request("GET","/"); r=c.getresponse(); print(r.read().decode()); assert r.status==200')
    result = client_command(pid, 'python3', '-c', code, check=False, timeout=5)
    if should_work:
        assert result.returncode == 0, result.stdout
    else:
        assert result.returncode != 0, 'Forbidden request unexpectedly succeeded: ' + result.stdout
    return result.stdout.strip()


def main():
    if not Path('/run/systemd/system').exists() or not Path('/.dockerenv').exists():
        raise RuntimeError('Run only in a disposable systemd test container')
    state = json.loads(Path('/var/lib/ikev2-vpn/state.json').read_text())
    if BASE.exists():
        shutil.rmtree(BASE)
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    client_etc, client_run = BASE / 'swanctl', BASE / 'run'
    for directory in ('x509', 'x509ca', 'private', 'conf.d'):
        (client_etc / directory).mkdir(parents=True, exist_ok=True)
    client_run.mkdir(exist_ok=True)
    shutil.copyfile('/var/lib/letsencrypt/audit/root.pem', client_etc / 'x509ca/root.pem')
    (BASE / 'strongswan.conf').write_text('''charon {
    load_modular = yes
    plugins { include /etc/strongswan.d/charon/*.conf }
}
include /etc/strongswan.d/*.conf
''')
    for name in (CLIENT, ORIGIN):
        run(['ip', 'netns', 'add', name])
        namespace(name, 'ip', 'link', 'set', 'lo', 'up')
    for host, guest, ns, host_ip, guest_ip in [
        ('audit-srv', 'audit-cli', CLIENT, '192.0.2.1/30', '192.0.2.2/30'),
        ('audit-out', 'audit-web', ORIGIN, '203.0.113.1/30', '203.0.113.2/30'),
    ]:
        run(['ip', 'link', 'add', host, 'type', 'veth', 'peer', 'name', guest])
        run(['ip', 'link', 'set', guest, 'netns', ns])
        run(['ip', 'address', 'add', host_ip, 'dev', host])
        run(['ip', 'link', 'set', host, 'up'])
        namespace(ns, 'ip', 'address', 'add', guest_ip, 'dev', guest)
        namespace(ns, 'ip', 'link', 'set', guest, 'up')
    namespace(ORIGIN, 'ip', 'address', 'add', '169.254.169.254/32', 'dev', 'lo')
    run(['ip', 'route', 'add', '169.254.169.254/32', 'via', '203.0.113.2', 'dev', 'audit-out'])
    Path('/proc/sys/net/ipv4/ip_forward').write_text('1\n')
    # This fixture returns the source address observed after the server firewall/NAT.
    server = BASE / 'responder.py'
    server.write_text('''from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = self.client_address[0].encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *args): pass
HTTPServer(('0.0.0.0', int(sys.argv[1])), Handler).serve_forever()
''')
    processes.append(subprocess.Popen(['ip', 'netns', 'exec', ORIGIN, 'python3', str(server), '8080']))
    processes.append(subprocess.Popen(['python3', str(server), '2222']))
    for _ in range(30):
        try:
            conn = http.client.HTTPConnection('169.254.169.254', 8080, timeout=1)
            conn.request('GET', '/')
            assert conn.getresponse().read().decode() == '203.0.113.1'
            conn.close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError('Isolated metadata fixture did not become reachable')
    files = run(['dpkg-query', '-L', 'charon-systemd']).stdout.splitlines()
    daemon = next(path for path in files if path.endswith('/charon-systemd') and Path(path).is_file() and os.access(path, os.X_OK))
    client_script = BASE / 'client.sh'
    client_script.write_text('''#!/bin/sh
set -eu
mount --bind "$1" /etc/swanctl
mount --bind "$2" /run
export STRONGSWAN_CONF="$3"
exec "$4"
''')
    client_log = (BASE / 'client.log').open('w')
    process = subprocess.Popen(['ip', 'netns', 'exec', CLIENT, 'unshare', '--mount', '--propagation', 'private',
                                'sh', str(client_script), str(client_etc), str(client_run),
                                str(BASE / 'strongswan.conf'), daemon], stdout=client_log, stderr=subprocess.STDOUT)
    processes.append(process)
    pid = process.pid
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError('Client daemon failed: ' + (BASE / 'client.log').read_text())
        if (client_run / 'charon.vici').is_socket() and client_command(pid, 'swanctl', '--stats', check=False).returncode == 0:
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('Client VICI never became ready')

    def configure(user, *, password=None, identity='vpn.example.com'):
        text = f'''connections {{
    audit {{
        version = 2
        local_addrs = 192.0.2.2
        remote_addrs = 192.0.2.1
        vips = 0.0.0.0, ::
        proposals = aes256-sha256-modp2048
        local {{ auth = eap-mschapv2; eap_id = {user['username']} }}
        remote {{ auth = pubkey; id = {identity} }}
        children {{ net {{ remote_ts = 0.0.0.0/0; esp_proposals = aes256-sha256-modp2048 }} }}
    }}
}}
secrets {{ eap-audit {{ id = {user['username']}; secret = "{password or user['password']}" }} }}
'''.replace('; ', '\n')
        (client_etc / 'swanctl.conf').write_text(text)
        os.chmod(client_etc / 'swanctl.conf', 0o600)
        client_command(pid, 'swanctl', '--load-creds', '--clear', '--noprompt')
        client_command(pid, 'swanctl', '--load-all', '--noprompt')

    def terminate():
        client_command(pid, 'swanctl', '--terminate', '--ike', 'audit', check=False)

    # The physical endpoint remains an IP while certificate identity is the domain.
    for user in state['users']:
        configure(user)
        result = client_command(pid, 'swanctl', '--initiate', '--ike', 'audit', check=False)
        assert result.returncode == 0, result.stdout
        status = client_command(pid, 'swanctl', '--list-sas').stdout
        assert 'ESTABLISHED' in status, status
        assert user['username'] in run(['swanctl', '--list-sas']).stdout
        terminate()
    print('Both generated users authenticated with EAP-MSCHAPv2 and server certificate validation.')
    configure(state['users'][0], password='IncorrectPassword12345!')
    result = client_command(pid, 'swanctl', '--initiate', '--ike', 'audit', check=False)
    assert result.returncode != 0, 'Incorrect password was accepted'
    terminate()
    configure(state['users'][0], identity='wrong.example.com')
    result = client_command(pid, 'swanctl', '--initiate', '--ike', 'audit', check=False)
    assert result.returncode != 0, 'Incorrect server identity was accepted'
    terminate()
    root_ca = client_etc / 'x509ca/root.pem'
    saved_root = root_ca.read_bytes()
    root_ca.unlink()
    configure(state['users'][0])
    result = client_command(pid, 'swanctl', '--initiate', '--ike', 'audit', check=False)
    assert result.returncode != 0, 'An untrusted server certificate was accepted'
    terminate()
    root_ca.write_bytes(saved_root)
    print('Incorrect password, incorrect server identity and untrusted server certificate were rejected.')
    configure(state['users'][0])
    cursor_output = run(['journalctl', '-u', 'strongswan.service', '-n', '1', '--show-cursor', '--no-pager']).stdout
    cursor = next(line.split(': ', 1)[1] for line in cursor_output.splitlines() if line.startswith('-- cursor: '))
    result = client_command(pid, 'swanctl', '--initiate', '--child', 'net', check=False)
    (BASE / 'child-result.log').write_text(result.stdout)
    if result.returncode != 0:
        print(result.stdout)
        journal = run(['journalctl', '-u', 'strongswan.service', '--after-cursor', cursor, '--no-pager']).stdout
        if ('(93)' in journal and
                ('Requested type not found' in journal or 'Protocol not supported' in journal) and
                'unable to install inbound and outbound IPsec SA' in journal):
            print('IKE/EAP passed; ESP is unavailable in the test kernel (netlink error 93). Data-plane checks are unrun.')
            return 77
        raise RuntimeError('CHILD_SA failed for an unexplained reason:\n' + journal)
    status = client_command(pid, 'swanctl', '--list-sas').stdout
    assert 'INSTALLED' in status, status
    assert http_request(pid, '203.0.113.2', 8080) == '203.0.113.1', 'NAT44 did not use the server exit address'
    http_request(pid, '192.0.2.1', 2222, should_work=False)
    http_request(pid, '169.254.169.254', 8080, should_work=False)
    run(['systemctl', 'start', 'ikev2-vpn-renew.service'])
    assert http_request(pid, '203.0.113.2', 8080) == '203.0.113.1'
    assert 'INSTALLED' in client_command(pid, 'swanctl', '--list-sas').stdout
    print('ESP traffic, NAT44, host/metadata isolation and live renewal with an active tunnel passed.')
    return 0


if __name__ == '__main__':
    result = 1
    try:
        result = main()
    finally:
        for process in reversed(processes):
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
            if process.poll() is None:
                process.kill()
        for namespace_name in (CLIENT, ORIGIN):
            subprocess.run(['ip', 'netns', 'delete', namespace_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for link in ('audit-srv', 'audit-out'):
            subprocess.run(['ip', 'link', 'delete', link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.exit(result)
