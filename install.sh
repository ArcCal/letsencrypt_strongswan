#!/usr/bin/env bash
# Two prompts only. Run on a dedicated Debian/Ubuntu VPS as root.
set -Eeuo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
export UCF_FORCE_CONFFOLD=1
trap 'printf "Deployment failed at line %s. Fix the error above and rerun; existing accounts will be preserved.\n" "$LINENO" >&2' ERR
die() { printf '%s\n' "$*" >&2; exit 1; }
[[ $EUID == 0 ]] || die 'Run as root or use sudo bash install.sh.'
[[ -d /run/systemd/system ]] || die 'A VPS booted with systemd is required; ordinary containers are not supported.'
# shellcheck disable=SC1091
source /etc/os-release
case "$ID:$VERSION_ID" in
    debian:13|ubuntu:24.04|ubuntu:26.04) ;;
    *) die 'Supported systems: Debian 13 and Ubuntu 24.04/26.04.' ;;
esac
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for file in scripts/vpn.py scripts/renew.sh config/certbot-deploy-hook.sh config/nftables-vpn.conf config/99-vpn-sysctl.conf config/strongswan-hardening.conf config/renew-hardening.conf; do
    [[ -f "$REPO/$file" ]] || die 'Clone the complete repository before running; install.sh alone is not sufficient.'
done
install -d -m 700 /var/lib/ikev2-vpn
exec 9>/var/lib/ikev2-vpn/install.lock
flock -n 9 || die 'Another deployment is already running.'
[[ -r /dev/tty ]] || die 'A terminal is required to read the domain and hidden token.'
IFS= read -r -p 'VPN certificate domain (FQDN): ' DOMAIN </dev/tty
IFS= read -r -s -p 'Cloudflare API token (hidden input): ' CF_TOKEN </dev/tty
printf '\n'

# Check conflicting managers before installing a competing IKE stack.
for service in ufw firewalld strongswan-starter; do
    if systemctl is-active --quiet "$service"; then
        die "Found active service $service. This installer requires a dedicated VPS; migrate existing services first."
    fi
done
if dpkg-query -W -f='${Status}' strongswan-starter 2>/dev/null | grep -q 'install ok installed'; then
    die 'Legacy strongswan-starter is installed. Migrate it first; it will not be removed automatically.'
fi
apt-get update
apt-get install -y -o Dpkg::Options::=--force-confold --no-install-recommends \
    strongswan-swanctl charon-systemd libcharon-extra-plugins \
    libcharon-extauth-plugins libstrongswan-extra-plugins nftables \
    certbot python3-certbot-dns-cloudflare python3 openssl iproute2 ca-certificates

# Refuse unmanaged rules/configuration instead of flushing someone else's firewall.
python3 - <<'PY'
import json, pathlib, subprocess, sys
managed = pathlib.Path('/var/lib/ikev2-vpn/managed').exists()
tables = json.loads(subprocess.check_output(['nft', '-j', 'list', 'tables']))['nftables']
owned = {('inet', 'ikev2_vpn'), ('ip', 'ikev2_vpn_nat'), ('ip6', 'ikev2_vpn_nat6')}
for entry in tables:
    table = entry.get('table')
    if table and (not managed or (table['family'], table['name']) not in owned):
        sys.exit('Unmanaged nftables/iptables-nft rules exist. Deploy on a dedicated VPS.')
directory = pathlib.Path('/etc/swanctl/conf.d')
for path in directory.glob('*.conf'):
    if path.name == 'ikev2-vpn.conf' and managed:
        continue
    if any(line.strip() and not line.lstrip().startswith('#') for line in path.read_text().splitlines()):
        sys.exit('Other swanctl configurations exist and will not be overwritten.')
main = pathlib.Path('/etc/swanctl/swanctl.conf').read_text()
if any(line.strip() and not line.lstrip().startswith('#') and
       line.strip() != 'include conf.d/*.conf' for line in main.splitlines()):
    sys.exit('swanctl.conf contains custom settings and cannot be managed automatically.')
PY

install -d -m 700 /usr/local/lib/ikev2-vpn
install -m 700 "$REPO/scripts/vpn.py" /usr/local/lib/ikev2-vpn/vpn.py
install -m 700 "$REPO/scripts/renew.sh" /usr/local/lib/ikev2-vpn/renew.sh
# Prevent concurrent renewal while replacing credentials and certificate hooks.
exec 8>/var/lib/ikev2-vpn/cert.lock
flock 8
printf '%s\n%s\n' "$DOMAIN" "$CF_TOKEN" | python3 /usr/local/lib/ikev2-vpn/vpn.py
unset CF_TOKEN
DOMAIN=$(cat /var/lib/ikev2-vpn/domain)
touch /var/lib/ikev2-vpn/managed
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy /etc/swanctl/x509 /etc/swanctl/x509ca
install -d -m 700 /etc/swanctl/private
install -m 700 "$REPO/config/certbot-deploy-hook.sh" /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn

# Noninteractive registration deliberately has no email: documented in README.
certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 60 --preferred-challenges dns \
    --cert-name "$DOMAIN" -d "$DOMAIN" --key-type ecdsa --elliptic-curve secp256r1 \
    --keep-until-expiring
openssl x509 -in "/etc/letsencrypt/live/$DOMAIN/cert.pem" -noout -checkend 0
# Also deploy when certbot reuses an existing unexpired certificate.
RENEWED_LINEAGE="/etc/letsencrypt/live/$DOMAIN" /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn
flock -u 8

install -m 644 "$REPO/config/99-vpn-sysctl.conf" /etc/sysctl.d/99-ikev2-vpn.conf
# accept_ra must be set for existing interfaces too, before enabling IPv6 forwarding.
for interface in /proc/sys/net/ipv6/conf/*/accept_ra; do
    if [[ -f $interface ]]; then
        printf '2\n' > "$interface"
        interface_name=${interface%/accept_ra}
        interface_name=${interface_name##*/}
        printf 'net/ipv6/conf/%s/accept_ra = 2\n' "$interface_name" >> /etc/sysctl.d/99-ikev2-vpn.conf
    fi
done
sysctl -p /etc/sysctl.d/99-ikev2-vpn.conf
install -d -m 755 /etc/ikev2-vpn
install -m 644 "$REPO/config/nftables-vpn.conf" /etc/ikev2-vpn/firewall.nft
# Detect configured SSH ports, including nonstandard ports, without another prompt.
SSH_PORTS=$( { /usr/sbin/sshd -T 2>/dev/null || true; } | awk '$1 == "port" && $2 ~ /^[0-9]+$/ {print $2}')
SSH_PORTS+=$'\n'$(ss -H -ltnp | awk '/"sshd"/ {addr=$4; sub(/.*:/,"",addr); if(addr ~ /^[0-9]+$/) print addr}')
SSH_PORTS=$(printf '%s\n' "$SSH_PORTS" | awk 'NF')
SSH_PORTS="${SSH_PORTS:-22}"
if [[ ${SSH_CONNECTION:-} ]]; then SSH_PORTS+=$'\n'"${SSH_CONNECTION##* }"; fi
SSH_PORTS=$(printf '%s\n' "$SSH_PORTS" | sort -nu | paste -sd, -)
sed -i "s/SSH_PORTS/$SSH_PORTS/" /etc/ikev2-vpn/firewall.nft
cat > /etc/systemd/system/ikev2-vpn-firewall.service <<'UNIT'
[Unit]
Description=IKEv2 VPN dedicated firewall
After=nftables.service
Before=strongswan.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/ikev2-vpn/firewall.nft
ExecReload=/usr/sbin/nft -f /etc/ikev2-vpn/firewall.nft
[Install]
WantedBy=multi-user.target
UNIT
install -d -m 755 /etc/systemd/system/strongswan.service.d
install -m 644 "$REPO/config/strongswan-hardening.conf" /etc/systemd/system/strongswan.service.d/hardening.conf
cat > /etc/systemd/system/strongswan.service.d/ikev2-vpn.conf <<'UNIT'
[Unit]
Requires=ikev2-vpn-firewall.service
After=ikev2-vpn-firewall.service
UNIT
cat > /etc/systemd/system/ikev2-vpn-renew.service <<'UNIT'
[Unit]
Description=Check and renew IKEv2 VPN certificate
Wants=network-online.target
After=network-online.target strongswan.service
[Service]
Type=oneshot
ExecStart=/usr/local/lib/ikev2-vpn/renew.sh
UNIT
cat > /etc/systemd/system/ikev2-vpn-renew.timer <<'UNIT'
[Unit]
Description=Twice daily IKEv2 VPN certificate check
[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
install -d -m 755 /etc/systemd/system/ikev2-vpn-renew.service.d
install -m 644 "$REPO/config/renew-hardening.conf" /etc/systemd/system/ikev2-vpn-renew.service.d/hardening.conf
install -d -m 700 /var/lib/letsencrypt /var/log/letsencrypt
nft -c -f /etc/ikev2-vpn/firewall.nft
systemctl daemon-reload
systemctl enable ikev2-vpn-firewall.service strongswan.service ikev2-vpn-renew.timer
systemctl restart ikev2-vpn-firewall.service
systemctl restart strongswan.service
swanctl --load-all --noprompt
swanctl --list-conns
swanctl --list-pools
systemctl start ikev2-vpn-renew.timer
# Dedicated host: use the hardened renewal service, never a second unsandboxed timer.
systemctl disable --now certbot.timer
systemctl is-active --quiet strongswan.service ikev2-vpn-firewall.service ikev2-vpn-renew.timer
printf '\nDeployment complete. Both accounts: /tmp/usr.txt\nProfiles: /var/lib/ikev2-vpn/profiles/*.mobileconfig\nTransfer profiles securely to the iPhone and install them. Allow inbound UDP 500/4500 in the cloud firewall.\n'
