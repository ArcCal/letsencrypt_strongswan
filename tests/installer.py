#!/usr/bin/env python3
"""Exercise the complete installer through a real terminal in a test container.

Only package operations, Cloudflare/public-IP discovery, and CA issuance are
mocked. Files, permissions, nftables, sysctl, systemd and VICI are real.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import termios
import time

STATE = Path('/var/lib/ikev2-vpn')
DOMAIN_PROMPT = b'VPN certificate domain (FQDN): '
TOKEN_PROMPT = b'Cloudflare API token (hidden input): '
TOKEN = b'a' * 40


def run(args, **kwargs):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=30, check=True, **kwargs)


def terminal_install(fail=False):
    master, slave = pty.openpty()
    def terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    process = subprocess.Popen(['bash', '/repo/install.sh'], stdin=slave, stdout=slave, stderr=slave,
                               preexec_fn=terminal, env={**os.environ, 'PATH': '/audit-bin:' + os.environ['PATH'],
                                                        'IKEV2_AUDIT_FAIL_CERTBOT': '1' if fail else '0'})
    os.close(slave)
    output = bytearray()
    domain_sent = token_sent = False
    deadline = time.monotonic() + 40
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    block = os.read(master, 65536)
                except OSError:
                    break
                if not block:
                    break
                output.extend(block)
                if not domain_sent and DOMAIN_PROMPT in output:
                    os.write(master, b'vpn.example.com\n')
                    domain_sent = True
                if not token_sent and TOKEN_PROMPT in output:
                    os.write(master, TOKEN + b'\n')
                    token_sent = True
            if process.poll() is not None and not ready:
                break
        else:
            raise RuntimeError('Installer exceeded the terminal test deadline')
        status = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        os.close(master)
    assert output.count(DOMAIN_PROMPT) == 1, 'Domain prompt count changed'
    assert output.count(TOKEN_PROMPT) == 1, 'Token prompt count changed'
    assert TOKEN not in output, 'Hidden token was echoed'
    return status, output.decode(errors='replace')


def main():
    if not Path('/.dockerenv').exists() or not Path('/run/systemd/system').exists():
        raise RuntimeError('Run only in a disposable systemd test container')
    os.umask(0o077)
    run(['systemctl', 'stop', 'strongswan.service', 'ikev2-vpn-renew.timer'])
    for family, table in [('inet', 'ikev2_vpn'), ('ip', 'ikev2_vpn_nat')]:
        subprocess.run(['nft', 'delete', 'table', family, table], check=False)
    for path in [STATE / 'state.json', STATE / 'managed', STATE / 'domain',
                 Path('/etc/swanctl/conf.d/ikev2-vpn.conf')]:
        path.unlink(missing_ok=True)
    # An actual socket-activated SSH listener has no sshd process to detect.
    for name, text in {
        'ssh.socket': '[Unit]\nDescription=Audit socket\n[Socket]\nListenStream=2222\n',
        'ssh.service': '[Service]\nExecStart=/bin/sleep infinity\n',
    }.items():
        path = Path('/etc/systemd/system') / name
        assert not path.exists(), 'Refusing to overwrite an existing test-host SSH unit'
        path.write_text(text)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'start', 'ssh.socket'])
    test_bin = Path('/audit-bin')
    test_bin.mkdir(mode=0o700, exist_ok=True)
    wrappers = {
        'apt-get': '#!/bin/sh\nexit 0\n',
        'certbot': '''#!/bin/sh
if [ "${IKEV2_AUDIT_FAIL_CERTBOT:-0}" = 1 ]; then exit 31; fi
exec /usr/local/bin/certbot "$@"
''',
        'python3': '''#!/usr/bin/python3
import os, runpy, sys
if len(sys.argv)==2 and sys.argv[1]=='/usr/local/lib/ikev2-vpn/vpn.py':
    module=runpy.run_path(sys.argv[1],run_name='audit_module')
    def request(self,path,method='GET',data=None):
        assert path.startswith('zones?') and method=='GET' and data is None
        return [{'id':'audit-zone'}]
    module['Cloudflare'].request=request
    module['prepare'].__globals__['public_ipv4']=lambda:'192.0.2.1'
    module['prepare']()
else:
    os.execv('/usr/bin/python3',['/usr/bin/python3',*sys.argv[1:]])
''',
    }
    for name, text in wrappers.items():
        path = test_bin / name
        path.write_text(text)
        path.chmod(0o700)
    try:
        run(['nft', 'add', 'table', 'ip', 'filter'])
        run(['nft', 'add', 'chain', 'ip', 'filter', 'ts-input'])
        run(['nft', 'add', 'rule', 'ip', 'filter', 'ts-input', 'udp', 'dport', '41641', 'accept'])
        external = run(['nft', '-s', 'list', 'table', 'ip', 'filter']).stdout
        status, output = terminal_install()
        assert status != 0 and 'Unmanaged nftables/iptables-nft rules exist' in output
        assert not (STATE / 'managed').exists() and not (STATE / 'state.json').exists()
        assert run(['nft', '-s', 'list', 'table', 'ip', 'filter']).stdout == external
        run(['nft', 'delete', 'table', 'ip', 'filter'])
        status, output = terminal_install(fail=True)
        assert status != 0, 'Injected issuance failure was ignored'
        first = json.loads((STATE / 'state.json').read_text())
        assert (STATE / 'managed').exists(), 'Partial installation lost its ownership marker'
        Path('/var/log/letsencrypt/audit-install-failed.log').write_text(output)
        run(['nft', '-f', '-'], input=external)
        for index in range(2):
            status, output = terminal_install()
            Path(f'/var/log/letsencrypt/audit-install-{index}.log').write_text(output)
            if status:
                print(output)
                raise RuntimeError(f'Full installer failed with status {status}')
            assert json.loads((STATE / 'state.json').read_text())['users'] == first['users']
            assert 'Deployment complete.' in output
            assert 'Preserving external firewall tables: ip filter' in output
            assert run(['nft', '-s', 'list', 'table', 'ip', 'filter']).stdout == external
            for path in [STATE / 'state.json', Path('/tmp/usr.txt'), Path('/etc/letsencrypt/cloudflare.ini'),
                         *list((STATE / 'profiles').glob('*.mobileconfig'))]:
                assert path.stat().st_mode & 0o777 == 0o600, path
        rules = run(['nft', 'list', 'chain', 'inet', 'ikev2_vpn', 'input']).stdout
        assert '2222' in rules, 'Socket-activated SSH port was not allowed'
        run(['systemctl', 'start', 'ikev2-vpn-renew.service'])
        assert run(['systemctl', 'show', 'ikev2-vpn-renew.service', '-p', 'Result', '--value']).stdout.strip() == 'success'
        print('Full terminal installer: first-install firewall rejection, external table preservation on reruns, failure recovery, two prompts, hidden token, repeatability, SSH socket and sandboxed renewal passed.')
    finally:
        subprocess.run(['nft', 'delete', 'table', 'ip', 'filter'], check=False)
        run(['systemctl', 'stop', 'ssh.socket'])
        subprocess.run(['systemctl', 'stop', 'ssh.service'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name in ('ssh.socket', 'ssh.service'):
            Path('/etc/systemd/system', name).unlink(missing_ok=True)
        run(['systemctl', 'daemon-reload'])
        shutil.rmtree(test_bin)


if __name__ == '__main__':
    main()
