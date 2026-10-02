#!/bin/sh
# /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
# Run by certbot after every successful issuance/renewal; installs the certificate into the
# swanctl directories and hot-reloads it.
# certbot sets RENEWED_LINEAGE=/etc/letsencrypt/live/<domain>
set -eu

LINEAGE="${RENEWED_LINEAGE:?must be run by certbot}"
SWANCTL=/etc/swanctl

# Server certificate (leaf certificate only) and private key
install -m 0644 "$LINEAGE/cert.pem"    "$SWANCTL/x509/vpn-server.pem"
install -m 0600 "$LINEAGE/privkey.pem" "$SWANCTL/private/vpn-server.key"

# Intermediate certificates: strongSwan reads only the first certificate in each file, so split them one per file.
rm -f "$SWANCTL"/x509ca/letsencrypt-chain-*.pem
awk -v dir="$SWANCTL/x509ca" '
    /-----BEGIN CERTIFICATE-----/ { n++; f = sprintf("%s/letsencrypt-chain-%d.pem", dir, n) }
    n > 0 { print > f }
' "$LINEAGE/chain.pem"
chmod 0644 "$SWANCTL"/x509ca/letsencrypt-chain-*.pem

# Reload only certificates/keys; existing connections are not dropped. Ignore failure if the daemon isn't running.
swanctl --load-creds --noprompt || true
