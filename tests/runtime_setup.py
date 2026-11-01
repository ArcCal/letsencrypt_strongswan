#!/usr/bin/env python3
"""Prepare a disposable systemd test image; never run on a production host."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import re
import subprocess
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, '/repo/scripts')
import vpn

FIXTURES = Path('/var/lib/letsencrypt/audit')
DOMAIN = 'vpn.example.com'


def private(path, data, mode=0o600):
    vpn.write_private(path, data, mode)


def key_bytes(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def ca_certificate(name, key, issuer=None, issuer_key=None, path_length=1):
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    return (x509.CertificateBuilder().subject_name(subject)
            .issuer_name(issuer.subject if issuer else subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(hours=1))
            .not_valid_after(now + timedelta(days=3))
            .add_extension(x509.BasicConstraints(ca=True, path_length=path_length), True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
            .sign(issuer_key or key, hashes.SHA256()))


def main():
    if os.environ.get('IKEV2_AUDIT_FIXTURES') != '1':
        raise RuntimeError('This helper is only for explicitly requested disposable test images')
    FIXTURES.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_key, intermediate_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    root = ca_certificate('Audit Root', root_key)
    intermediate = ca_certificate('Audit Intermediate', intermediate_key, root, root_key, 0)
    root_pem = root.public_bytes(serialization.Encoding.PEM)
    chain = intermediate.public_bytes(serialization.Encoding.PEM) + root_pem
    private(FIXTURES / 'root.pem', root_pem, 0o644)
    for serial in (1, 2, 3):
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        leaf = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DOMAIN)]))
                .issuer_name(intermediate.subject).public_key(key.public_key()).serial_number(serial)
                .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(DOMAIN)]), False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
                .sign(intermediate_key, hashes.SHA256()))
        private(FIXTURES / f'cert{serial}.pem', leaf.public_bytes(serialization.Encoding.PEM), 0o644)
        private(FIXTURES / f'key{serial}.pem', key_bytes(key))
    private('/usr/local/share/ca-certificates/ikev2-audit-root.crt', root_pem, 0o644)
    subprocess.run(['update-ca-certificates'], check=True)
    state = vpn.load_state(vpn.STATE, DOMAIN)
    state['server_address'] = '192.0.2.1'
    private(vpn.STATE / 'state.json', json.dumps(state))
    private(vpn.STATE / 'domain', DOMAIN + '\n')
    for name in ('managed', 'cert.lock', 'deploy.lock', 'install.lock'):
        private(vpn.STATE / name, '')
    vpn.render(state)
    lineage = Path('/etc/letsencrypt/live') / DOMAIN
    private(lineage / 'cert.pem', (FIXTURES / 'cert1.pem').read_bytes(), 0o644)
    private(lineage / 'privkey.pem', (FIXTURES / 'key1.pem').read_bytes())
    private(lineage / 'chain.pem', chain, 0o644)
    for name in ('x509', 'x509ca', 'private'):
        Path('/etc/swanctl', name).mkdir(parents=True, exist_ok=True)
    for source, target in [
        ('scripts/vpn.py', '/usr/local/lib/ikev2-vpn/vpn.py'),
        ('scripts/renew.sh', '/usr/local/lib/ikev2-vpn/renew.sh'),
        ('config/certbot-deploy-hook.sh', '/etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn'),
    ]:
        private(target, Path('/repo', source).read_bytes(), 0o700)
    subprocess.run(['/etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn'], check=True,
                   env={**os.environ, 'RENEWED_LINEAGE': str(lineage), 'IKEV2_VPN_SKIP_RELOAD': '1'})
    source = Path('/repo/scripts/install-body.sh').read_text()
    for path, body in re.findall(r"cat > (/etc/systemd/system/[^ ]+) <<'UNIT'\n(.*?)\nUNIT", source, re.S):
        private(path, body + '\n', 0o644)
    for service, filename in [('strongswan', 'strongswan-hardening.conf'), ('ikev2-vpn-renew', 'renew-hardening.conf')]:
        private(f'/etc/systemd/system/{service}.service.d/hardening.conf',
                Path('/repo/config', filename).read_bytes(), 0o644)
    private('/etc/ikev2-vpn/firewall.nft', Path('/repo/config/nftables-vpn.conf').read_text().replace('SSH_PORTS', '22,2222'), 0o644)
    private('/etc/sysctl.d/99-ikev2-vpn.conf', Path('/repo/config/99-vpn-sysctl.conf').read_bytes(), 0o644)
    for directory in ('/var/log/letsencrypt', '/var/lib/letsencrypt'):
        Path(directory).mkdir(parents=True, exist_ok=True)
    private('/usr/local/bin/certbot', '''#!/bin/sh
set -eu
lineage=/etc/letsencrypt/live/vpn.example.com
printf '%s\\n' "$*" >> /var/log/letsencrypt/audit-renew.log
serial=2
if [ -f /var/lib/letsencrypt/audit/renewed ]; then serial=3; fi
cp "/var/lib/letsencrypt/audit/cert$serial.pem" "$lineage/cert.pem"
cp "/var/lib/letsencrypt/audit/key$serial.pem" "$lineage/privkey.pem"
: > /var/lib/letsencrypt/audit/renewed
RENEWED_LINEAGE="$lineage" /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn
''', 0o755)


if __name__ == '__main__':
    main()
