FROM debian:13-slim
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update && \
    DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends \
      strongswan-swanctl charon-systemd libcharon-extra-plugins libcharon-extauth-plugins \
      libstrongswan-extra-plugins nftables certbot python3-certbot-dns-cloudflare \
      openssl shellcheck util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /repo
COPY install.sh ./
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY tests/ ./tests/
