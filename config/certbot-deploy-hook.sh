#!/bin/sh
# Installed at /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn.
set -eu
umask 077
DOMAIN=$(cat /var/lib/ikev2-vpn/domain)
LINEAGE=${RENEWED_LINEAGE:?must be run by certbot or the installer}
# certbot may also renew unrelated certificates on this host.
[ "$LINEAGE" = "/etc/letsencrypt/live/$DOMAIN" ] || exit 0
SWANCTL=/etc/swanctl
openssl x509 -in "$LINEAGE/cert.pem" -noout -checkend 0 >/dev/null
openssl x509 -in "$LINEAGE/cert.pem" -noout -checkhost "$DOMAIN" >/dev/null
STAGE=$(mktemp -d "$SWANCTL/.vpn-cert.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM
openssl x509 -in "$LINEAGE/cert.pem" -pubkey -noout > "$STAGE/cert.pub"
openssl pkey -in "$LINEAGE/privkey.pem" -pubout > "$STAGE/key.pub"
cmp -s "$STAGE/cert.pub" "$STAGE/key.pub"
install -m 0644 "$LINEAGE/cert.pem" "$STAGE/vpn-server.pem"
install -m 0600 "$LINEAGE/privkey.pem" "$STAGE/vpn-server.key"
awk -v dir="$STAGE" '
    /-----BEGIN CERTIFICATE-----/ { n++; f = sprintf("%s/letsencrypt-chain-%d.pem", dir, n) }
    n > 0 { print > f }
' "$LINEAGE/chain.pem"
for cert in "$STAGE"/letsencrypt-chain-*.pem; do
    openssl x509 -in "$cert" -noout >/dev/null
    chmod 0644 "$cert"
done
# Files only replace live credentials after all input has been validated.
mv -f "$STAGE/vpn-server.pem" "$SWANCTL/x509/vpn-server.pem"
mv -f "$STAGE/vpn-server.key" "$SWANCTL/private/vpn-server.key"
rm -f "$SWANCTL"/x509ca/letsencrypt-chain-*.pem
mv "$STAGE"/letsencrypt-chain-*.pem "$SWANCTL/x509ca/"
if [ -S /run/charon.vici ]; then
    # Report reload errors; initial installation may precede daemon startup.
    swanctl --load-creds --noprompt
fi
