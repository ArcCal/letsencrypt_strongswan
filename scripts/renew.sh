#!/usr/bin/env bash
set -euo pipefail
umask 077
exec 9>/var/lib/ikev2-vpn/cert.lock
flock 9
DOMAIN=$(cat /var/lib/ikev2-vpn/domain)
CERT="/etc/letsencrypt/live/$DOMAIN/cert.pem"
if ! openssl x509 -in "$CERT" -noout -checkend 0 >/dev/null 2>&1; then
    # Expired/missing certificate: issue immediately, never just reload old files.
    certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
        --dns-cloudflare --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
        --dns-cloudflare-propagation-seconds 60 --preferred-challenges dns \
        --cert-name "$DOMAIN" -d "$DOMAIN" --key-type ecdsa --elliptic-curve secp256r1 \
        --force-renewal
else
    # Let certbot decide when a still-valid certificate is due, before expiration.
    certbot renew --cert-name "$DOMAIN" --non-interactive --quiet
fi
openssl x509 -in "$CERT" -noout -checkend 0
RENEWED_LINEAGE="/etc/letsencrypt/live/$DOMAIN" /etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn
