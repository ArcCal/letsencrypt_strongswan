# Let's Encrypt + strongSwan: Manual IKEv2 VPN Deployment Guide

[![letsencrypt](res/images/letsencrypt-logo-horizontal.png)](https://letsencrypt.org/) ![plus](res/images/add-symbolic.symbolic.png) [![strongswan](res/images/strongswan.png)](https://strongswan.org/)

Manually deploy IKEv2 VPN on **Debian 13 (trixie) x64** or **Ubuntu 26.04 LTS x64** using **distribution packages without source compilation**:

| Component | Choice |
|---|---|
| VPN software | strongSwan 6.x (`charon-systemd` + `swanctl`, using `swanctl.conf`) |
| Server authentication | Let's Encrypt certificate (certbot, Cloudflare **DNS-01** challenge) |
| Client authentication | EAP-MSCHAPv2 (username + password) |
| Clients | Built-in or strongSwan clients on iOS / macOS / Windows / Android / Linux; no extra certificate import |
| Firewall | nftables |

> **Earlier approach**: source-built strongSwan 5.x, `ipsec.conf` / `ipsec start` (`starter`), `acme.sh --standalone` (port 80), and iptables.
> The updated approach uses apt, `swanctl.conf` and systemd, certbot with Cloudflare DNS (no ports 80/443, with wildcard support), and nftables.

## Table of Contents

1. [Preparation](#1-preparation)
2. [Create a Cloudflare API token](#2-create-a-cloudflare-api-token)
3. [Install packages](#3-install-packages)
4. [Obtain a certificate with certbot](#4-obtain-a-certificate-with-certbot)
5. [Configure strongSwan](#5-configure-strongswan)
6. [Kernel parameters and firewall](#62-firewall-nftables)
7. [Start and verify](#7-start-and-verify)
8. [Client configuration](#8-client-configuration)
9. [Routine maintenance](#9-routine-maintenance)
10. [Troubleshooting](#10-troubleshooting)
11. [Validation scope](#11-validation-scope)

The `config/` directory in this repository contains every configuration file used in this guide, ready to copy:

| File | Deploy to |
|---|---|
| [`config/cloudflare.ini.example`](config/cloudflare.ini.example) | `/etc/letsencrypt/cloudflare.ini` |
| [`config/certbot-deploy-hook.sh`](config/certbot-deploy-hook.sh) | `/etc/letsencrypt/renewal-hooks/deploy/strongswan.sh` |
| [`config/ikev2-eap.conf`](config/ikev2-eap.conf) | `/etc/swanctl/conf.d/ikev2-eap.conf` |
| [`config/secrets.conf.example`](config/secrets.conf.example) | `/etc/swanctl/conf.d/secrets.conf` |
| [`config/99-vpn-sysctl.conf`](config/99-vpn-sysctl.conf) | `/etc/sysctl.d/99-vpn.conf` |
| [`config/nftables-vpn.conf`](config/nftables-vpn.conf) | `/etc/nftables.conf` |
| [`config/client-linux.conf`](config/client-linux.conf) | Linux client `/etc/swanctl/conf.d/vpn-client.conf` |

---

## 1. Preparation

1. **A VPS**: Debian 13 or Ubuntu 26.04 LTS, x86-64, public IPv4 with optional IPv6. Run commands as root (`sudo -i` for regular users).
2. **A domain with DNS hosted in Cloudflare**, such as `vpn.example.com`.
   - Add an **A record**, and an **AAAA record** if IPv6 is available, pointing to the VPS public IP.
   - **Use DNS only (gray cloud)**. Cloudflare's orange-cloud proxy does not forward IKE UDP traffic.
3. **Allow ports**: inbound **UDP 500** and **UDP 4500**, plus SSH, in the cloud security group/firewall.
   The old tutorial's UDP 50/51 values are ESP/AH protocol numbers, not UDP ports. Most modern clients use NAT, with ESP encapsulated in UDP 4500, so the cloud security group needs UDP 500/4500.
4. Keep system time accurate for certificates and IKE:

```sh
timedatectl status          # Look for "System clock synchronized: yes"
```

Conventions: domain `vpn.example.com`, IPv4 pool `10.10.10.0/24`, IPv6 pool `fd10:10:10::/64`. Replace these throughout with your own values.

## 2. Create a Cloudflare API token

certbot proves domain ownership using a temporary TXT record at `_acme-challenge.vpn.example.com` through the Cloudflare API.

1. Sign into Cloudflare > avatar > **My Profile** > **API Tokens** > **Create Token**.
2. Select **Edit zone DNS** or configure a custom token:
   - Permission: **Zone -> DNS -> Edit**
   - Zone Resources: **Include -> Specific zone -> your domain** (such as `example.com`)
3. Copy the token after creation; it is shown only once.

> certbot-dns-cloudflare recommends a **scoped API token**. Global API Keys are supported but **not recommended** due to account-wide compromise risk. See the next section if a Global API Key is your only option.

## 3. Install packages

Both distributions use the same commands:

```sh
apt update
apt install -y \
    strongswan-swanctl charon-systemd \
    libcharon-extra-plugins libcharon-extauth-plugins libstrongswan-extra-plugins \
    nftables \
    certbot python3-certbot-dns-cloudflare
```

Package purposes:

| Package | Purpose |
|---|---|
| `charon-systemd` | strongSwan IKE daemon with `strongswan.service` |
| `strongswan-swanctl` | `swanctl` command and `/etc/swanctl/` directory |
| `libcharon-extauth-plugins` | **EAP-MSCHAPv2**, used for client authentication |
| `libcharon-extra-plugins` | EAP-Identity and other plugins |
| `libstrongswan-extra-plugins` | Algorithms including ChaCha20-Poly1305 |
| `certbot` + `python3-certbot-dns-cloudflare` | ACME client and Cloudflare DNS plugin |

> **Do not install the `strongswan` metapackage.** It pulls in legacy `strongswan-starter` (`ipsec.conf` / `ipsec`), which conflicts with `charon-systemd`. Choose one stack.

Confirm only the `charon-systemd` stack is present:

```sh
dpkg -l | grep -E 'strongswan|charon'
systemctl cat strongswan.service | head -3
```

> Ubuntu certbot documentation recommends snap. This guide uses apt as requested; its `certbot.timer` provides automatic renewal.

## 4. Obtain a certificate with certbot

### 4.1 Store Cloudflare credentials

```sh
install -m 600 /dev/null /etc/letsencrypt/cloudflare.ini
nano /etc/letsencrypt/cloudflare.ini
```

Contents (see [`config/cloudflare.ini.example`](config/cloudflare.ini.example)):

```ini
# API Token (recommended)
dns_cloudflare_api_token = YOUR_TOKEN
```

If only a Global API Key is available, use these two lines instead; **do not combine them with a token**, or certbot fails:

```ini
dns_cloudflare_email = you@example.com
dns_cloudflare_api_key = YOUR_GLOBAL_API_KEY
```

Use mode 600 to avoid certbot's "Unsafe permissions on credentials configuration file" warning:

```sh
chmod 600 /etc/letsencrypt/cloudflare.ini
ls -l /etc/letsencrypt/cloudflare.ini
```

### 4.2 Install the deploy hook

strongSwan does not read `/etc/letsencrypt/live/` directly. Install certificates into `/etc/swanctl/` with a certbot **deploy hook**, invoked after each successful issuance or renewal.

```sh
install -m 755 config/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
```

(Without a checkout, copy [`config/certbot-deploy-hook.sh`](config/certbot-deploy-hook.sh) into the file manually.)

The script performs:

- `cert.pem` (leaf certificate) -> `/etc/swanctl/x509/vpn-server.pem`
- `privkey.pem` -> `/etc/swanctl/private/vpn-server.key` (mode 600)
- Split `chain.pem` into individual `/etc/swanctl/x509ca/letsencrypt-chain-N.pem` files. strongSwan reads only the first certificate per file, so do not use `fullchain.pem` directly. Clients need the intermediate certificates during the handshake to validate trust.
- Hot reload certificates with `swanctl --load-creds`, **without disconnecting existing clients**.

### 4.3 Run a dry run first

```sh
certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d vpn.example.com \
    --key-type ecdsa \
    --dry-run
```

`--dry-run` uses Let's Encrypt staging without production rate limits or invoking the deploy hook. First use asks for email and terms acceptance.

### 4.4 Issue the certificate

Remove `--dry-run`:

```sh
certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d vpn.example.com \
    --key-type ecdsa
```

- `--key-type ecdsa`: compact ECDSA certificates with fast handshakes, supported by major IKEv2 clients. Use `--key-type rsa` for RSA; other steps remain the same.
- For a wildcard (`*.example.com`), add `-d '*.example.com'`; wildcard issuance requires DNS-01.

The hook runs after issuance. Because strongSwan is not started, `swanctl --load-creds` fails and this version ignores the failure. Check:

```sh
ls -l /etc/swanctl/x509 /etc/swanctl/x509ca /etc/swanctl/private
```

Expect `vpn-server.pem`, `letsencrypt-chain-1.pem` and `vpn-server.key`.

### 4.5 Verify automatic renewal

```sh
systemctl list-timers certbot.timer     # Enabled by default by Debian/Ubuntu apt certbot
certbot renew --dry-run                 # Simulate renewal to check credentials and workflow
```

Certificates last 90 days; certbot renews with fewer than 30 days remaining. The successful renewal hook installs the new certificate into strongSwan.

## 5. Configure strongSwan

Use the existing `include conf.d/*.conf` at the end of `/etc/swanctl/swanctl.conf`. Put configuration in `conf.d/` without changing the main file.

### 5.1 Connection and address pools

```sh
install -m 644 config/ikev2-eap.conf /etc/swanctl/conf.d/ikev2-eap.conf
sed -i 's/vpn\.example\.com/your.domain/' /etc/swanctl/conf.d/ikev2-eap.conf
```

Key points (see file comments):

- `local { auth = pubkey; certs = vpn-server.pem; id = your.domain }`: server certificate authentication. `id` must match the certificate SAN domain checked by clients.
- `remote { auth = eap-mschapv2; eap_id = %any }`: client username/password authentication.
- `pools = vpn4, vpn6`: assign virtual IPs and DNS from the pools.
- `local_ts = 0.0.0.0/0, ::/0`: route all traffic through the VPN.
- Omitted `esp_proposals` / `proposals` use **strongSwan's maintained defaults**, avoiding long manual `ike=` / `esp=` lists.

> **No IPv6?** Remove `vpn6`, `, vpn6` from `pools`, `, ::/0` from `local_ts`, and IPv6 lines from [`nftables-vpn.conf`](config/nftables-vpn.conf).

### 5.2 User accounts

```sh
install -m 600 config/secrets.conf.example /etc/swanctl/conf.d/secrets.conf
nano /etc/swanctl/conf.d/secrets.conf
```

```
secrets {
    eap-alice {
        id = alice
        secret = "a long random password"
    }
}
```

- The section name (`eap-alice`) is only an identifier; **`id` is the client username**.
- Generate passwords with `openssl rand -base64 18`; the file contains plaintext secrets and must be root-owned with mode 600.

### 6.2 Firewall (nftables)

### 6.1 Enable forwarding

```sh
install -m 644 config/99-vpn-sysctl.conf /etc/sysctl.d/99-vpn.conf
sysctl --system
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
```

**IPv6 warning**: IPv6 forwarding makes Linux ignore RA by default, potentially removing the VPS IPv6 default route. The configuration sets `accept_ra = 2`, but the setting is per interface; check your actual interface:

```sh
ip -6 route show default                 # A default route should remain
ip -br link                              # Find the interface name, such as eth0 / ens3
sysctl net.ipv6.conf.eth0.accept_ra      # Expect 2; otherwise set it and persist in /etc/sysctl.d/99-vpn.conf
```

For IPv4 only, remove the IPv6 lines.

### 6.2 Firewall (nftables)

Allow UDP 500/4500, forward VPN pool traffic, and apply outbound source NAT.

**Scenario A: fresh VPS without firewall rules** -- use the complete repository rules:

```sh
# Edit first: confirm the actual SSH port (default 22) to avoid locking yourself out.
nano config/nftables-vpn.conf
nft -c -f config/nftables-vpn.conf       # Check syntax without applying rules
install -m 644 config/nftables-vpn.conf /etc/nftables.conf
systemctl enable --now nftables
nft list ruleset
```

> Prepare cloud VNC / console access before changing rules. `nftables.service` loads `/etc/nftables.conf` at boot.

**Scenario B: existing firewall (ufw / custom nftables / iptables)** -- merge the following without overwriting existing rules:

- Allow inbound UDP 500 and UDP 4500;
- Allow forwarding from `10.10.10.0/24` and `fd10:10:10::/64`;
- Merge the final two `table ... vpn_nat` NAT sections in `config/nftables-vpn.conf`, or equivalent iptables `-t nat ... MASQUERADE` rules;
- With ufw, set `DEFAULT_FORWARD_POLICY` to `ACCEPT` in `/etc/default/ufw` and put NAT rules in `/etc/ufw/before.rules`.

NAT matches source pools without depending on interface names.

## 7. Start and verify

```sh
systemctl enable --now strongswan
systemctl status strongswan --no-pager
```

`strongswan.service` automatically runs `swanctl --load-all` to load connections, pools, certificates and secrets from `conf.d/`.

Check loaded configuration:

```sh
swanctl --list-certs        # Expect vpn-server.pem with your domain and intermediates
swanctl --list-conns        # Expect ikev2-eap
swanctl --list-pools        # Expect vpn4 / vpn6
journalctl -u strongswan -n 50 --no-pager
```

Manually reload after changing `conf.d`:

```sh
swanctl --load-all
```

After connection:

```sh
swanctl --list-sas          # Established connections, usernames and assigned virtual IPs
```

## 8. Client configuration

Common client parameters:

| Item | Value |
|---|---|
| Server / Server address | `vpn.example.com` (domain, not IP, for certificate validation) |
| Remote ID | `vpn.example.com` (matches server `id`) |
| Authentication | Username + password (EAP / IKEv2 EAP) |
| Username / Password | `id` / `secret` from section 5.2 |
| Local ID | Leave empty |

Let's Encrypt roots are already trusted by the OS; **clients do not need to import certificates**.

- **iOS / iPadOS / macOS**: Settings > VPN > Add VPN Configuration > **IKEv2**. Enter server, Remote ID and username/password authentication. See [Apple client instructions](https://wiki.strongswan.org/projects/strongswan/wiki/AppleClients).
- **Windows 10 / 11**: Settings > Network & Internet > VPN > Add VPN. Select Windows (built in), **IKEv2**, and username/password authentication. See [Windows instructions](https://wiki.strongswan.org/projects/strongswan/wiki/Windows7#C).
  - If the client reports unacceptable IKE credentials or a policy mismatch, its default cryptographic parameters may differ from the server defaults, especially older Windows DH groups. Use PowerShell `Set-VpnConnectionIPsecConfiguration` to select modern parameters such as `AES256`, `SHA256`, and `Group14`/`ECP256`; avoid enabling weak server algorithms.
- **Android**: install **strongSwan VPN Client** from Google Play. Add **IKEv2 EAP (Username/Password)**, enter the server domain and credentials, and leave CA certificate on automatic selection. See [Android client instructions](https://wiki.strongswan.org/projects/strongswan/wiki/AndroidVPNClient).
- **Linux**: use the same packages and [`config/client-linux.conf`](config/client-linux.conf):

```sh
apt install -y strongswan-swanctl charon-systemd libcharon-extra-plugins libcharon-extauth-plugins
# Trust the Let's Encrypt root certificate from the system certificate store
cp /etc/ssl/certs/ISRG_Root_X1.pem /etc/ssl/certs/ISRG_Root_X2.pem /etc/swanctl/x509ca/
install -m 600 config/client-linux.conf /etc/swanctl/conf.d/vpn-client.conf
nano /etc/swanctl/conf.d/vpn-client.conf          # Change domain, username and password
systemctl restart strongswan
swanctl --initiate --child net                    # Connect
swanctl --list-sas
swanctl --terminate --ike vpn                     # Disconnect
```

  For Linux client stalls with large packets, set `mtu = 1280` in `/etc/strongswan.d/charon/kernel-netlink.conf`.

After connecting, open <https://ifconfig.me> on the client; it should show the VPS IP.

## 9. Routine maintenance

**Add / delete users**: edit `/etc/swanctl/conf.d/secrets.conf`, then

```sh
swanctl --load-creds
```

Online users remain connected. Terminate a session with `swanctl --terminate --ike ikev2-eap --ike-id <number>` (see `--list-sas`). Accounts allow multiple devices by default.

**Certificate renewal**: automatic. Check `journalctl -u certbot` or `/var/log/letsencrypt/letsencrypt.log`; force an end-to-end test with:

```sh
certbot renew --force-renewal --deploy-hook /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
```

(Repeated forced production renewals can trigger Let's Encrypt rate limits. Normally use `certbot renew --dry-run`.)

**Upgrade**: run `apt update && apt upgrade` for distribution strongSwan security updates. Check `systemctl status strongswan` afterwards.

**Log level**: for detailed IKE logs, edit the `journal { }` section in `/etc/strongswan.d/charon-systemd.conf`, then `systemctl restart strongswan`. Restore the log level after debugging.

```
charon-systemd {
    journal {
        default = 1
        ike = 2
    }
}
```

## 10. Troubleshooting

| Symptom | Checks |
|---|---|
| certbot `Unsafe permissions` | `chmod 600 /etc/letsencrypt/cloudflare.ini` |
| certbot Cloudflare authentication / zone error | Scope **DNS:Edit** to the domain zone; do not combine token and email/api_key |
| certbot validation timeout | Increase `--dns-cloudflare-propagation-seconds`, for example to 60 |
| `swanctl: connecting to 'unix:///var/run/charon.vici' failed` | Daemon is stopped; check `systemctl status strongswan` |
| `loading EAP_MSCHAPV2 method failed` / eap-mschapv2 absent in logs | Install `libcharon-extauth-plugins` |
| `no EAP key found` / login failure | Match `id` in `secrets.conf` to the client username and run `swanctl --load-creds` |
| Invalid certificate / server identity cannot be verified | Use the domain rather than an IP; check intermediates in `swanctl --list-certs` and server time |
| Cannot connect with no server log activity | Check cloud firewall UDP 500/4500, correct DNS and Cloudflare DNS-only status |
| Connected without internet | Check `sysctl net.ipv4.ip_forward` is 1 and NAT/forward rules in `nft list ruleset` |
| IPv6 fails / host IPv6 disappears | See 6.1: check `accept_ra` and the default route, or remove the IPv6 pool |
| Large pages fail or traffic is very slow | Check MTU/MSS clamping in nftables; set `mtu = 1280` on Linux clients |
| Certificate loading denied on Ubuntu | Check `dmesg \| grep -i apparmor`; packaged `charon-systemd` / `swanctl` policies permit `/etc/swanctl/`. Adjust policies for custom paths |

## 11. Validation scope

Validation scope and remaining checks for your own deployment:

**Validated** (Ubuntu 24.04, strongSwan 5.9.13, certbot 5.8 and certbot-dns-cloudflare):

- Package split: `eap-mschapv2` in `libcharon-extauth-plugins`, `eap-identity` in `libcharon-extra-plugins`; `charon-systemd` supplies `strongswan.service` and automatically runs `swanctl --load-all`; `swanctl.conf` includes `conf.d/*.conf`.
- Actually loaded `config/ikev2-eap.conf`, `secrets.conf.example` and deploy hook: connection, both pools, certificate, key and EAP secrets loaded successfully.
- Complete IKE handshake with a test CA (root -> intermediate -> ECDSA leaf): the client validated the server chain, **EAP-MSCHAPv2 succeeded**, and the server assigned `10.10.10.x` / `fd10:10:10::x` virtual IPs and DNS.
- The deploy hook correctly split and installed the leaf certificate, key and intermediates using certbot's `RENEWED_LINEAGE` convention.
- `nftables-vpn.conf` passed `nft -c`; Cloudflare credential format and certbot options came from the installed plugin documentation.

**Unverified** (test environment limitations; check after deployment):

- Physical Debian 13 / Ubuntu 26.04 installations: public package information indicates strongSwan 6.0.x and compatible package splits, but neither system was installed. Verify the shared 5.9/6 syntax with actual `swanctl --load-all` output.
- Real Let's Encrypt issuance (requires your Cloudflare account) was untested.
- ESP data plane, forwarding, NAT and applied nftables: the test kernel lacks ESP support, so CHILD_SA could not be established.
- Actual client connection interfaces were untested. Instructions describe common workflows; labels vary by OS version.
- Official strongSwan (docs.strongswan.org) and certbot documentation were inaccessible in the test environment; configuration uses distribution templates, installed plugin documentation and observed behavior.
