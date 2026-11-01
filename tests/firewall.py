#!/usr/bin/env python3
"""Reproduce conntrack bypasses using only disposable local TCP fixtures."""
import contextlib
from pathlib import Path
import re
import select
import subprocess
import tempfile
import time

CLIENT = 'ikev2-filter-client'
ORIGIN = 'ikev2-filter-origin'
PROCESSES = []


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=10, **kwargs)


def ns(name, *args):
    return run(['ip', 'netns', 'exec', name, *args])


def exchange(chain, address, port, blocked):
    run(['nft', 'insert', 'rule', 'inet', 'ikev2_vpn', chain, 'ip', 'saddr',
         '172.29.240.50', 'tcp', 'dport', str(port), 'accept', 'comment', '"audit-seed"'])
    rules = run(['nft', '-a', 'list', 'chain', 'inet', 'ikev2_vpn', chain]).stdout
    handle = re.search(r'comment "audit-seed" # handle (\d+)', rules).group(1)
    code = '''import socket,sys
s=socket.socket(); s.settimeout(2); s.bind(('172.29.240.50',0))
s.connect((sys.argv[1],int(sys.argv[2]))); s.sendall(b'first')
assert s.recv(5)==b'first'; print('READY',flush=True)
sys.stdin.readline(); s.sendall(b'second')
try:
    assert s.recv(6)==b'second'; print('ALLOWED',flush=True)
except socket.timeout:
    print('BLOCKED',flush=True)
'''
    client = subprocess.Popen(['ip', 'netns', 'exec', CLIENT, 'python3', '-c', code, address, str(port)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    PROCESSES.append(client)
    assert select.select([client.stdout], [], [], 5)[0], 'Seed connection timed out'
    assert client.stdout.readline().strip() == 'READY', client.stderr.read()
    run(['nft', 'delete', 'rule', 'inet', 'ikev2_vpn', chain, 'handle', handle])
    output, errors = client.communicate('\n', timeout=5)
    assert client.returncode == 0, errors
    assert output.strip() == ('BLOCKED' if blocked else 'ALLOWED'), output


def main():
    if not Path('/.dockerenv').exists() or not Path('/run/systemd/system').exists():
        raise RuntimeError('Run only in a disposable systemd test container')
    current = Path('/etc/ikev2-vpn/firewall.nft').read_text()
    # Prove the regression fixture detects the previous ordering and missing check.
    previous = current.replace('        ip saddr 172.29.240.0/24 drop\n', '')
    previous = previous.replace('        ct state established,related accept\n',
                                '        ct state established,related accept\n        ip saddr 172.29.240.0/24 drop\n', 1)
    previous = previous.replace('        ip saddr 172.29.240.0/24 meta ipsec missing drop\n', '')
    try:
        for name in (CLIENT, ORIGIN):
            run(['ip', 'netns', 'add', name])
            ns(name, 'ip', 'link', 'set', 'lo', 'up')
        for host, guest, name, left, right in [
            ('filter-in', 'filter-client', CLIENT, '192.0.2.1/30', '192.0.2.2/30'),
            ('filter-out', 'filter-origin', ORIGIN, '203.0.113.1/30', '203.0.113.2/30'),
        ]:
            run(['ip', 'link', 'add', host, 'type', 'veth', 'peer', 'name', guest])
            run(['ip', 'link', 'set', guest, 'netns', name])
            run(['ip', 'address', 'add', left, 'dev', host])
            run(['ip', 'link', 'set', host, 'up'])
            ns(name, 'ip', 'address', 'add', right, 'dev', guest)
            ns(name, 'ip', 'link', 'set', guest, 'up')
        ns(CLIENT, 'ip', 'address', 'add', '172.29.240.50/32', 'dev', 'lo')
        ns(CLIENT, 'ip', 'route', 'add', 'default', 'via', '192.0.2.1')
        run(['ip', 'route', 'add', '172.29.240.50/32', 'via', '192.0.2.2'])
        Path('/proc/sys/net/ipv4/ip_forward').write_text('1\n')
        server = '''import socketserver,sys
class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            data=self.request.recv(1024)
            if not data: return
            self.request.sendall(data)
class Server(socketserver.ThreadingTCPServer): allow_reuse_address=True
Server(('0.0.0.0',int(sys.argv[1])),Echo).serve_forever()
'''
        PROCESSES.append(subprocess.Popen(['python3', '-c', server, '2222']))
        PROCESSES.append(subprocess.Popen(['ip', 'netns', 'exec', ORIGIN, 'python3', '-c', server, '8080']))
        time.sleep(0.3)
        with tempfile.TemporaryDirectory() as directory:
            for text, blocked in [(previous, False), (current, True)]:
                path = Path(directory, 'filter.nft')
                path.write_text(text)
                run(['nft', '-f', str(path)])
                exchange('input', '192.0.2.1', 2222, blocked)
                exchange('forward', '203.0.113.2', 8080, blocked)
        print('Conntrack regression reproduced; current input and forwarding reject unencrypted pool traffic even on established connections.')
    finally:
        run(['nft', '-f', '/etc/ikev2-vpn/firewall.nft'])
        for process in PROCESSES:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
            if process.poll() is None:
                process.kill()
        for name in (CLIENT, ORIGIN):
            subprocess.run(['ip', 'netns', 'delete', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for link in ('filter-in', 'filter-out'):
            subprocess.run(['ip', 'link', 'delete', link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
