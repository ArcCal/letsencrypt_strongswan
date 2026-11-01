#!/usr/bin/env bash
# DESTRUCTIVE TO CONTAINER FILESYSTEM: run only in a disposable vpn-dev container.
set -euo pipefail
cd /repo
export PYTHONDONTWRITEBYTECODE=1
shellcheck install.sh scripts/renew.sh config/certbot-deploy-hook.sh tests/integration.sh
python3 -m unittest discover -s tests -v
mkdir -p /var/lib/ikev2-vpn /etc/swanctl/{x509,x509ca,private} /etc/letsencrypt/live/vpn.example.com /test-bin
# Validate the actual installer's unit heredocs and merged vendor/drop-in syntax.
python3 - <<'PY'
from pathlib import Path
import re
import subprocess
text = Path('install.sh').read_text()
for path, contents in re.findall(r"cat > (/etc/systemd/system/[^ ]+) <<'UNIT'\n(.*?)\nUNIT", text, re.S):
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(contents + '\n')
for service, source in [('strongswan', 'strongswan-hardening.conf'), ('ikev2-vpn-renew', 'renew-hardening.conf')]:
    directory = Path('/etc/systemd/system/' + service + '.service.d')
    directory.mkdir(exist_ok=True)
    (directory / 'hardening.conf').write_text(Path('config', source).read_text())
Path('/usr/local/lib/ikev2-vpn').mkdir(parents=True, exist_ok=True)
Path('/usr/local/lib/ikev2-vpn/renew.sh').write_text(Path('scripts/renew.sh').read_text())
Path('/usr/local/lib/ikev2-vpn/renew.sh').chmod(0o700)
subprocess.run(['systemd-analyze', 'verify', '--man=no', 'strongswan.service',
                'ikev2-vpn-renew.service', 'ikev2-vpn-renew.timer', 'ikev2-vpn-firewall.service'], check=True)
PY
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/repo/scripts')
import vpn
vpn.render(vpn.load_state(Path('/var/lib/ikev2-vpn'), 'vpn.example.com'))
vpn.write_private('/var/lib/ikev2-vpn/domain', 'vpn.example.com\n')
PY
LINEAGE=/etc/letsencrypt/live/vpn.example.com
export LINEAGE
# Test CA and two distinct leaf certificates; no real ACME requests.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
    -subj /CN=TestRoot -keyout /tmp/ca.key -out /tmp/ca.pem >/dev/null 2>&1
printf 'subjectAltName=DNS:vpn.example.com\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n' > /tmp/leaf.ext
for version in 1 2; do
    openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj /CN=vpn.example.com \
        -keyout "/tmp/key$version.pem" -out /tmp/leaf.csr >/dev/null 2>&1
    openssl x509 -req -in /tmp/leaf.csr -CA /tmp/ca.pem -CAkey /tmp/ca.key -set_serial "$version" \
        -days 1 -extfile /tmp/leaf.ext -out "/tmp/cert$version.pem" >/dev/null 2>&1
done
cp /tmp/cert1.pem "$LINEAGE/cert.pem"
cp /tmp/key1.pem "$LINEAGE/privkey.pem"
cat /tmp/ca.pem /tmp/ca.pem > "$LINEAGE/chain.pem"
export PATH=/test-bin:$PATH
export RENEWED_LINEAGE=$LINEAGE
sh config/certbot-deploy-hook.sh
cmp "$LINEAGE/cert.pem" /etc/swanctl/x509/vpn-server.pem
test "$(stat -c %a /etc/swanctl/private/vpn-server.key)" = 600
test "$(find /etc/swanctl/x509ca -name 'letsencrypt-chain-*.pem' | wc -l)" = 2
# Refuse mismatched keys, retaining the previously installed credential.
cp /tmp/key2.pem "$LINEAGE/privkey.pem"
if sh config/certbot-deploy-hook.sh; then echo 'Mismatched key was accepted' >&2; exit 1; fi
cmp /tmp/cert1.pem /etc/swanctl/x509/vpn-server.pem
cp /tmp/key1.pem "$LINEAGE/privkey.pem"
/usr/sbin/charon-systemd >/tmp/charon.log 2>&1 &
daemon_pid=$!
trap 'kill "$daemon_pid" 2>/dev/null || true' EXIT
for ((n=0; n<5; n++)); do test ! -S /var/run/charon.vici || break; sleep 1; done
if ! swanctl --load-all --noprompt > /tmp/load.log 2>&1; then cat /tmp/load.log /tmp/charon.log; exit 1; fi
grep 'successfully loaded 1 connections' /tmp/load.log
grep 'successfully loaded 2 pools' /tmp/load.log
test "$(grep -c 'loaded eap secret' /tmp/load.log)" = 2
swanctl --stats > /tmp/stats.log
grep 'eap-mschapv2' /tmp/stats.log
# Hot-load a replacement certificate while the actual daemon is running.
cp /tmp/cert2.pem "$LINEAGE/cert.pem"
cp /tmp/key2.pem "$LINEAGE/privkey.pem"
sh config/certbot-deploy-hook.sh > /tmp/reload.log 2>&1
cmp /tmp/cert2.pem /etc/swanctl/x509/vpn-server.pem
swanctl --list-certs > /tmp/certs.log
grep '02' /tmp/certs.log >/dev/null
# A failed live reload must propagate to certbot/systemd.
cat > /test-bin/swanctl <<'SH'
#!/bin/sh
exit 42
SH
chmod +x /test-bin/swanctl
if sh config/certbot-deploy-hook.sh; then echo 'Reload failure was ignored' >&2; exit 1; fi
rm /test-bin/swanctl
# Test renewal orchestration with a mocked CA command; daemon/reload remain real.
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
install -m 700 config/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn
cat > /test-bin/certbot <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> /tmp/certbot-calls
cp /tmp/cert2.pem "$LINEAGE/cert.pem"
cp /tmp/key2.pem "$LINEAGE/privkey.pem"
SH
chmod +x /test-bin/certbot
bash scripts/renew.sh > /tmp/renew.log 2>&1
grep 'renew --cert-name vpn.example.com' /tmp/certbot-calls
python3 - <<'PY'
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from datetime import datetime, timezone
from pathlib import Path
ca = x509.load_pem_x509_certificate(Path('/tmp/ca.pem').read_bytes())
leaf = x509.load_pem_x509_certificate(Path('/tmp/cert2.pem').read_bytes())
key = serialization.load_pem_private_key(Path('/tmp/ca.key').read_bytes(), password=None)
expired = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(ca.subject)
           .public_key(leaf.public_key()).serial_number(3)
           .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
           .not_valid_after(datetime(2020, 1, 2, tzinfo=timezone.utc))
           .sign(key, hashes.SHA256()))
Path('/etc/letsencrypt/live/vpn.example.com/cert.pem').write_bytes(expired.public_bytes(serialization.Encoding.PEM))
PY
if sh config/certbot-deploy-hook.sh; then echo 'Expired certificate was accepted' >&2; exit 1; fi
bash scripts/renew.sh > /tmp/renew.log 2>&1
grep -- --force-renewal /tmp/certbot-calls
openssl x509 -in /etc/swanctl/x509/vpn-server.pem -noout -checkend 0
# Check and apply twice in this container's isolated network namespace only.
sed 's/SSH_PORTS/22,2222/' config/nftables-vpn.conf > /tmp/firewall.nft
nft -c -f /tmp/firewall.nft
nft -f /tmp/firewall.nft
nft -f /tmp/firewall.nft
nft list chain inet ikev2_vpn forward > /tmp/forward.log
grep 'ipsec exists' /tmp/forward.log
printf 'Integration checks passed (no public ACME or iPhone test performed).\n'
