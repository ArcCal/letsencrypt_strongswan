ARG BASE_IMAGE=debian:13-slim
FROM ${BASE_IMAGE}
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update && \
    standard_plugins="" && \
    if apt-cache show libstrongswan-standard-plugins >/dev/null 2>&1; then standard_plugins=libstrongswan-standard-plugins; fi && \
    DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends \
      $standard_plugins \
      strongswan-swanctl charon-systemd libcharon-extra-plugins libcharon-extauth-plugins \
      libstrongswan-extra-plugins nftables certbot python3-certbot-dns-cloudflare \
      openssl shellcheck util-linux systemd && rm -rf /var/lib/apt/lists/*
WORKDIR /repo
COPY install.sh ./
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY tests/ ./tests/
