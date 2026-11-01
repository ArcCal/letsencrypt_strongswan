#!/usr/bin/env bash
# Two prompts only. Run on a dedicated Debian/Ubuntu VPS as root.
set -Eeuo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
export UCF_FORCE_CONFFOLD=1
trap 'printf "Deployment failed at line %s. Fix the error above and rerun; existing accounts will be preserved.\n" "$LINENO" >&2' ERR
die() { printf '%s\n' "$*" >&2; exit 1; }
platform_supported() {
    local minimum
    case "$1" in
        debian) minimum=12 ;;
        ubuntu) minimum=22.04 ;;
        *) return 1 ;;
    esac
    [[ $2 =~ ^[0-9]+([.][0-9]+)*$ ]] || return 1
    dpkg --compare-versions "$2" ge "$minimum"
}
[[ $EUID == 0 ]] || die 'Run as root or use sudo bash install.sh.'
[[ -d /run/systemd/system ]] || die 'A VPS booted with systemd is required; ordinary containers are not supported.'
# shellcheck disable=SC1091
source /etc/os-release
platform_supported "${ID:-}" "${VERSION_ID:-}" || die 'Supported systems: Debian 12 or later, and Ubuntu 22.04 or later.'
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for file in scripts/vpn.py scripts/renew.sh config/certbot-deploy-hook.sh config/nftables-vpn.conf config/99-vpn-sysctl.conf config/strongswan-hardening.conf config/renew-hardening.conf; do
    [[ -f "$REPO/$file" ]] || die 'The embedded payload is incomplete. Download install.sh again.'
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
SWAN_PACKAGES=(strongswan-swanctl charon-systemd libcharon-extra-plugins
    libcharon-extauth-plugins libstrongswan-extra-plugins)
# Debian 12 splits OpenSSL/certificate plugins into this package; other
# releases include them in libstrongswan or use a different package split.
if apt-cache show libstrongswan-standard-plugins >/dev/null 2>&1; then
    SWAN_PACKAGES+=(libstrongswan-standard-plugins)
fi
apt-get install -y -o Dpkg::Options::=--force-confold --no-install-recommends \
    "${SWAN_PACKAGES[@]}" nftables \
    certbot python3-certbot-dns-cloudflare python3 openssl iproute2 procps ca-certificates

# Reject existing firewall tables on first installation; preserve them on reruns.
python3 - <<'PY'
import json, pathlib, subprocess, sys
managed = pathlib.Path('/var/lib/ikev2-vpn/managed').exists()
tables = json.loads(subprocess.check_output(['nft', '-j', 'list', 'tables']))['nftables']
owned = {('inet', 'ikev2_vpn'), ('ip', 'ikev2_vpn_nat'), ('ip6', 'ikev2_vpn_nat6')}
external = []
for entry in tables:
    table = entry.get('table')
    if table and not managed:
        sys.exit('Unmanaged nftables/iptables-nft rules exist. Deploy on a dedicated VPS.')
    if table and (table['family'], table['name']) not in owned:
        external.append(table['family'] + ' ' + table['name'])
if external:
    print('Preserving external firewall tables: ' + ', '.join(external) +
          '. Their policies may affect VPN traffic.', file=sys.stderr)
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

# Claim only the reserved configuration after checking for unmanaged files.
# Keep this marker even if a later step fails so a partial setup can be retried.
touch /var/lib/ikev2-vpn/managed
install -d -m 700 /usr/local/lib/ikev2-vpn
install -m 700 "$REPO/scripts/vpn.py" /usr/local/lib/ikev2-vpn/vpn.py
install -m 700 "$REPO/scripts/renew.sh" /usr/local/lib/ikev2-vpn/renew.sh
# Prevent concurrent renewal while replacing credentials and certificate hooks.
exec 8>/var/lib/ikev2-vpn/cert.lock
flock 8
printf '%s\n%s\n' "$DOMAIN" "$CF_TOKEN" | python3 /usr/local/lib/ikev2-vpn/vpn.py
unset CF_TOKEN
DOMAIN=$(cat /var/lib/ikev2-vpn/domain)
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy /etc/swanctl/x509 /etc/swanctl/x509ca
install -d -m 700 /etc/swanctl/private
: >> /var/lib/ikev2-vpn/deploy.lock
install -m 700 "$REPO/config/certbot-deploy-hook.sh" /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn

# Noninteractive registration deliberately has no email: documented in README.
# Both certbot's directory hook and the explicit deployment below must work even
# if a previous crash left a stale VICI socket. The mandatory service restart and
# --load-all later in installation validate the running daemon with new settings.
export IKEV2_VPN_SKIP_RELOAD=1
certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 60 --preferred-challenges dns \
    --cert-name "$DOMAIN" -d "$DOMAIN" --key-type ecdsa --elliptic-curve secp256r1 \
    --keep-until-expiring
openssl x509 -in "/etc/letsencrypt/live/$DOMAIN/cert.pem" -noout -checkend 0
# Also deploy when certbot reuses an existing unexpired certificate.
RENEWED_LINEAGE="/etc/letsencrypt/live/$DOMAIN" /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn
unset IKEV2_VPN_SKIP_RELOAD

install -m 644 "$REPO/config/99-vpn-sysctl.conf" /etc/sysctl.d/99-ikev2-vpn.conf
sysctl -p /etc/sysctl.d/99-ikev2-vpn.conf
install -d -m 755 /etc/ikev2-vpn
install -m 644 "$REPO/config/nftables-vpn.conf" /etc/ikev2-vpn/firewall.nft
# Detect configured SSH ports, including nonstandard ports, without another prompt.
SSH_PORTS=$(python3 /usr/local/lib/ikev2-vpn/vpn.py --ssh-ports)
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
systemctl reset-failed strongswan.service
if ! systemctl restart strongswan.service; then
    systemctl --no-pager --full status strongswan.service >&2 || true
    journalctl -u strongswan.service -b -n 100 --no-pager >&2 || true
    die 'strongSwan startup failed. Diagnostics are shown above; existing accounts and certificates were preserved.'
fi
swanctl --load-all --noprompt
swanctl --list-conns
swanctl --list-pools
systemctl start ikev2-vpn-renew.timer
# Dedicated host: use the hardened renewal service, never a second unsandboxed timer.
systemctl disable --now certbot.timer
systemctl is-active --quiet strongswan.service ikev2-vpn-firewall.service ikev2-vpn-renew.timer
flock -u 8
printf '\nDeployment complete. Both accounts: /tmp/usr.txt\nProfiles: /var/lib/ikev2-vpn/profiles/*.mobileconfig\nTransfer profiles securely to the iPhone and install them. Allow inbound UDP 500/4500 in the cloud firewall.\n'
