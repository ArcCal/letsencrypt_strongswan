#!/usr/bin/env bash
# Run inside a disposable container booted with systemd as PID 1.
set -euo pipefail
[[ -f /.dockerenv ]] || { echo 'Run only in a disposable Docker container' >&2; exit 1; }
diagnostics() {
    systemctl --no-pager --full status strongswan.service ikev2-vpn-renew.service || true
    journalctl -b -u strongswan.service -u ikev2-vpn-renew.service -n 100 --no-pager || true
}
trap diagnostics ERR
for ((n=0; n<20; n++)); do
    if systemctl is-active --quiet strongswan.service ikev2-vpn-firewall.service ikev2-vpn-renew.timer; then break; fi
    sleep 1
done
systemctl is-active --quiet strongswan.service ikev2-vpn-firewall.service ikev2-vpn-renew.timer
swanctl --list-conns > /tmp/systemd-conns
grep ikev2-eap /tmp/systemd-conns
test "$(systemctl show strongswan.service -p NoNewPrivileges --value)" = yes
test "$(systemctl show strongswan.service -p ProtectSystem --value)" = strict
systemctl start ikev2-vpn-renew.service
test "$(systemctl show ikev2-vpn-renew.service -p Result --value)" = success
cmp /var/lib/letsencrypt/audit/cert2.pem /etc/swanctl/x509/vpn-server.pem
swanctl --list-certs > /tmp/systemd-certs
grep 'serial:.*02' /tmp/systemd-certs
systemctl restart strongswan.service
systemctl is-active --quiet strongswan.service
swanctl --list-conns > /tmp/systemd-restarted
grep ikev2-eap /tmp/systemd-restarted
printf 'Actual systemd startup, restart and sandboxed renewal passed.\n'
