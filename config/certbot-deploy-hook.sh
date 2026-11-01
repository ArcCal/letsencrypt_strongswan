#!/bin/sh
# Installed at /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn.
set -eu
umask 077
DOMAIN=$(cat /var/lib/ikev2-vpn/domain)
LINEAGE=${RENEWED_LINEAGE:?must be run by certbot or the installer}
# certbot may also renew unrelated certificates on this host.
[ "$LINEAGE" = "/etc/letsencrypt/live/$DOMAIN" ] || exit 0
# Separate from cert.lock: callers hold that lock while certbot runs this hook.
exec 9>/var/lib/ikev2-vpn/deploy.lock
flock 9
SWANCTL=/etc/swanctl
STAGE=$(mktemp -d "$SWANCTL/.vpn-cert.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM
# Validate the exact snapshot that will be installed, not mutable source paths.
install -m 0644 "$LINEAGE/cert.pem" "$STAGE/vpn-server.pem"
install -m 0600 "$LINEAGE/privkey.pem" "$STAGE/vpn-server.key"
install -m 0644 "$LINEAGE/chain.pem" "$STAGE/chain.pem"
openssl x509 -in "$STAGE/vpn-server.pem" -noout -checkend 0 >/dev/null
openssl verify -CApath /etc/ssl/certs -untrusted "$STAGE/chain.pem" \
    -purpose sslserver -verify_hostname "$DOMAIN" "$STAGE/vpn-server.pem" >/dev/null
openssl x509 -in "$STAGE/vpn-server.pem" -pubkey -noout > "$STAGE/cert.pub"
openssl pkey -in "$STAGE/vpn-server.key" -pubout > "$STAGE/key.pub"
cmp -s "$STAGE/cert.pub" "$STAGE/key.pub"
awk -v dir="$STAGE" '
    /-----BEGIN CERTIFICATE-----/ { n++; f = sprintf("%s/letsencrypt-chain-%d.pem", dir, n) }
    n > 0 { print > f }
' "$STAGE/chain.pem"
for cert in "$STAGE"/letsencrypt-chain-*.pem; do
    openssl x509 -in "$cert" -noout >/dev/null
    chmod 0644 "$cert"
done
# Files only replace live credentials after all input has been validated.
mv -f "$STAGE/vpn-server.pem" "$SWANCTL/x509/vpn-server.pem"
mv -f "$STAGE/vpn-server.key" "$SWANCTL/private/vpn-server.key"
rm -f "$SWANCTL"/x509ca/letsencrypt-chain-*.pem
mv "$STAGE"/letsencrypt-chain-*.pem "$SWANCTL/x509ca/"
if [ "${IKEV2_VPN_SKIP_RELOAD:-0}" = 1 ]; then
    # Installation repairs the service next and requires a full credential load.
    printf 'Certificate deployed; credential loading is deferred to service startup.\n'
else
    # A socket file may be stale. Let the real VICI request determine success.
    # Scheduled renewal must report a stopped daemon or failed live reload.
    swanctl --load-creds --noprompt
fi
