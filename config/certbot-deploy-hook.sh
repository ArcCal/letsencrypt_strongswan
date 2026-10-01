#!/bin/sh
# /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
# certbot 每次签发/续期成功后执行,把证书装进 swanctl 目录并热加载。
# certbot 会设置 RENEWED_LINEAGE=/etc/letsencrypt/live/<域名>
set -eu

LINEAGE="${RENEWED_LINEAGE:?must be run by certbot}"
SWANCTL=/etc/swanctl

# 服务器证书(只含叶子证书)和私钥
install -m 0644 "$LINEAGE/cert.pem"    "$SWANCTL/x509/vpn-server.pem"
install -m 0600 "$LINEAGE/privkey.pem" "$SWANCTL/private/vpn-server.key"

# 中间证书:strongSwan 每个文件只读第一张证书,所以逐张拆开。
rm -f "$SWANCTL"/x509ca/letsencrypt-chain-*.pem
awk -v dir="$SWANCTL/x509ca" '
    /-----BEGIN CERTIFICATE-----/ { n++; f = sprintf("%s/letsencrypt-chain-%d.pem", dir, n) }
    n > 0 { print > f }
' "$LINEAGE/chain.pem"
chmod 0644 "$SWANCTL"/x509ca/letsencrypt-chain-*.pem

# 只重新加载证书/密钥,不会断开现有连接。守护进程没在运行时忽略失败。
swanctl --load-creds --noprompt || true
