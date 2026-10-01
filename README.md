# Let's Encrypt + strongSwan: Manual IKEv2 VPN Deployment Guide

[![letsencrypt](res/images/letsencrypt-logo-horizontal.png)](https://letsencrypt.org/) ![plus](res/images/add-symbolic.symbolic.png) [![strongswan](res/images/strongswan.png)](https://strongswan.org/)

Manually set up an IKEv2 VPN on **Debian 13 (trixie) x64** or **Ubuntu 26.04 LTS x64**, **using only the distribution's own packages** (no compiling from source):

| Component | Choice |
|---|---|
| VPN software | strongSwan 6.x (`charon-systemd` + `swanctl`, i.e. the modern `swanctl.conf` configuration) |
| Server authentication | Let's Encrypt certificate (certbot, Cloudflare **DNS-01** challenge) |
| Client authentication | EAP-MSCHAPv2 (username + password) |
| Clients | Built-in or strongSwan clients on iOS / macOS / Windows / Android / Linux; no extra certificates needed |
| Firewall | nftables |

> **Differences from the old guide**: the old version compiled strongSwan 5.x from source and used `ipsec.conf` / `ipsec start` (`starter`), `acme.sh` + `--standalone` (which occupies port 80), and iptables.
> The new version uses apt packages, `swanctl.conf` + systemd, certbot + Cloudflare DNS (no need to open ports 80/443, and wildcard domains are supported), and nftables.

## Table of Contents

0. [Architecture](#1-prerequisites)
1. [Preparation](#1-preparation)
2. [Create a Cloudflare API token](#2-create-a-cloudflare-api-token)
3. [Install packages](#3-install-packages)
4. [Obtain a certificate with certbot](#4-obtain-a-certificate-with-certbot)
5. [Configure strongSwan](#5-configure-strongswan)
6. [Kernel parameters and firewall](#62-firewall-nftables)
7. [Start and verify](#7-start-and-verify)
8. [Client configuration](#8-client-configuration)
9. [Routine maintenance](#9-routine-maintenance)
10. [Security hardening](#11-what-this-guide-has-been-verified-against)
11. [Optional advanced configuration](#11-optional-advanced-configuration)
12. [Troubleshooting](#12-troubleshooting)
13. [Uninstallation and rollback](#13-uninstallation-and-rollback)
14. [Validation scope](#14-validation-scope)

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

**Conventions**: domain `vpn.example.com`, documentation server IPs `203.0.113.10` / `2001:db8::10`, IPv4 client pool `10.10.10.0/24`, IPv6 pool `fd10:10:10::/64`. **Replace these with your values** and never commit real domains, IPs, tokens or passwords publicly. Commands prefixed with `#` run as root (use `sudo -i` first). Example versions and timestamps will differ.

---

## 1. Prerequisites

### 0.1 Traffic flow

```
 Phone / computer                       Your VPS                              Internet
+--------------+   IKEv2 + ESP        +-----------------------+   After NAT   +--------+
| VPN client   | === UDP 500/4500 ===>| strongSwan (charon)   | ------------> | Target |
| Virtual IP   | encrypted tunnel    | Decrypt -> Forward -> NAT | <---------- |        |
| 10.10.10.x   |                      | nftables              |               +--------+
+--------------+                      +-----------------------+
                                         ^
                       certbot ----------+ certificate (Let's Encrypt)
                       Cloudflare DNS <-- DNS-01 validation (temporary TXT records)
```

1. The client initiates **IKEv2** to UDP 500 at `vpn.example.com` (automatically switching to UDP 4500 for NAT).
2. The server presents a **Let's Encrypt certificate**. The client validates the CA chain, domain and validity period using system roots, ensuring it reaches the intended server.
3. The client authenticates with **EAP-MSCHAPv2** username/password inside the encrypted channel established with the authenticated server.
4. After authentication, the server assigns virtual IPs and DNS, and both peers establish an **ESP tunnel** (CHILD_SA).
5. Client traffic enters the tunnel, is decrypted and forwarded by the server kernel, and reaches the internet through nftables source NAT. Return traffic follows the reverse path.

## 2. Create a Cloudflare API Token

| Component | Responsibility |
|---|---|
| `charon-systemd` | IKE daemon for handshakes, authentication, key exchange and installing kernel XFRM policies |
| `swanctl` | CLI using VICI to load configuration, certificates and secrets into charon and inspect status |
| Linux kernel | ESP encryption/decryption through XFRM and IP forwarding |
| nftables | Allow UDP 500/4500 and forwarding, apply outbound NAT and MSS clamping |
| certbot | Obtain and renew Let's Encrypt certificates through ACME |
| Deploy hook | Install renewed certificates into strongSwan and hot reload |

### 0.3 Why DNS-01 validation

> The official certbot-dns-cloudflare documentation recommends this kind of **scoped API Token**. The "Global API Key" (account-wide full permissions) is still supported but **not recommended**: if it leaks, your entire account is compromised. If you only have a Global API Key, see the next section for the format.

- No web service or inbound ports 80/443 are required;
- Obtain a certificate **before** the VPN server is online;
- Obtain wildcard certificates.

certbot needs DNS modification permissions, so scope the Cloudflare token carefully (section 2).

---

## 1. Preparation

### 1.1 Server

- **Debian 13** or **Ubuntu 26.04 LTS**, x86-64, preferably a fresh installation.
- Public IPv4, optionally IPv6 if clients need IPv6 connectivity.
- 512 MB RAM and one CPU core can support a few dozen clients.
- Choose an IPsec-capable instance and image. Some OpenVZ/LXC kernels lack XFRM/ESP support. Prefer full virtualization such as KVM; the following commands give a basic kernel check:

```sh
# Expect XFRM-related files
ls /proc/net/xfrm_stat
# Check kernel ESP support (built in or provided by a module)
grep -E 'esp4|esp6' /proc/modules /proc/net/protocols 2>/dev/null; modinfo esp4 2>/dev/null | head -2
```

### 1.2 Login and basic security

Use SSH keys and disable password login before exposing services. These nftables rules allow SSH port 22 by default; **update the firewall first if your SSH port differs**.

Update and reboot to use current kernels and components:

```sh
# apt update && apt full-upgrade -y
# reboot
```

### 1.3 Domain and DNS

You need a domain **with DNS hosted by Cloudflare**, using Cloudflare nameservers.

1. Add the following for `vpn.example.com` in Cloudflare DNS:
   - **A record** -> server public IPv4;
   - If IPv6 is available, add an **AAAA record** -> server public IPv6.
2. **Use DNS only (gray cloud)**. The orange-cloud proxy forwards HTTP(S), not IKE UDP, and prevents VPN connectivity.
3. Verify DNS resolution on the server or your computer:

```sh
getent ahostsv4 vpn.example.com | head -1      # Expect the server IPv4
getent ahostsv6 vpn.example.com | head -1      # Expect the server IPv6 if AAAA exists
```

### 1.4 Ports

Allow inbound traffic in the **cloud security group / network firewall**:

| Protocol | Port | Purpose |
|---|---|---|
| UDP | 500 | IKE negotiation |
| UDP | 4500 | IKE and ESP encapsulation for NAT traversal |
| TCP | 22 (or your SSH port) | Administration |

The old guide's 50/UDP and 51/UDP are ESP/AH protocol numbers, not UDP ports. Most clients use NAT with ESP over UDP 4500. The cloud rules need the three listed entries; host nftables also permits plain ESP for clients without NAT.

### 1.5 Check the current server state

Check first to avoid conflicts with existing configuration:

```sh
# System version
cat /etc/os-release | head -3

# Interfaces and addresses; note the main interface and public IP for troubleshooting
ip -br addr
ip route get 1.1.1.1

# Existing firewall? This determines scenario A or B in section 6.2.
nft list ruleset | head -20
ufw status 2>/dev/null
iptables -S 2>/dev/null | head

# Check whether UDP 500/4500 are already occupied (expect no output)
ss -lunp | grep -E ':(500|4500)\b'

# Check for Docker or other firewall management software
systemctl is-active docker 2>/dev/null
```

### 1.6 Time synchronization

Certificate validity and IKE replay protection require accurate time:

```sh
timedatectl status          # Look for "System clock synchronized: yes"
```

If it is not yes:

```sh
# timedatectl set-ntp true
```

---

## 2. Create a Cloudflare API token

certbot uses the Cloudflare API to create a temporary TXT record at `_acme-challenge.vpn.example.com`, then deletes it after validation.

### 2.1 Create a token

1. Open the Cloudflare dashboard > avatar > **My Profile** > **API Tokens** > **Create Token**.
2. Select **Edit zone DNS** or a custom token with:
   - Permission: **Zone -> DNS -> Edit**
   - Zone Resources: **Include -> Specific zone -> your domain** (such as `example.com`).
   - Optionally restrict Client IP Address Filtering to the server IP and set the token TTL.
3. **Copy the token immediately** after creation; it is shown only once.

> certbot-dns-cloudflare recommends a **scoped API token**. Global API Keys remain supported but are **not recommended**, as exposure compromises the entire account.

### 2.2 Check token validity (optional)

Use `read -s` to avoid putting the token in shell history:

```sh
read -rs CF_TOKEN; echo
curl -s -H "Authorization: Bearer $CF_TOKEN" https://api.cloudflare.com/client/v4/user/tokens/verify
unset CF_TOKEN
```

JSON `"status":"active"` indicates validity. Account-owned tokens use a different verification endpoint and may fail here; skip this step and use the section 4.3 dry run instead.

---

## 3. Install packages

Commands are identical on both distributions:

```sh
# apt update
# apt install -y \
    strongswan-swanctl charon-systemd \
    libcharon-extra-plugins libcharon-extauth-plugins libstrongswan-extra-plugins \
    nftables \
    certbot python3-certbot-dns-cloudflare
```

What each package does:

Purpose of each package:

| Package | Purpose |
|---|---|
| `charon-systemd` | strongSwan IKE daemon with `strongswan.service` |
| `strongswan-swanctl` | `swanctl` command and `/etc/swanctl/` directory layout |
| `libcharon-extauth-plugins` | **EAP-MSCHAPv2**, used for client authentication |
| `libcharon-extra-plugins` | EAP-Identity and other plugins |
| `libstrongswan-extra-plugins` | Algorithms including ChaCha20-Poly1305 |
| `nftables` | Firewall and NAT |
| `certbot`, `python3-certbot-dns-cloudflare` | ACME client and Cloudflare DNS plugin |

> **Do not install the `strongswan` metapackage.** It depends on legacy `strongswan-starter` (`ipsec.conf` / `ipsec`) and `strongswan-charon`. Mixing this stack with `charon-systemd` causes daemon port conflicts.

### 3.1 Verify installation

```sh
dpkg -l | grep -E 'strongswan|charon|certbot'    # No strongswan-starter / strongswan-charon expected
swanctl --version                                # Expect strongSwan 6.x swanctl
certbot plugins 2>/dev/null | grep -A3 dns-cloudflare   # Expect dns-cloudflare
systemctl cat strongswan.service | head -12
```

> Ubuntu's official certbot documentation recommends installing via snap; this guide uses the apt version instead. The apt version ships with `certbot.timer` and renews automatically.

```
ExecStart=/usr/sbin/charon-systemd
ExecStartPost=/usr/sbin/swanctl --load-all --noprompt
```

Contents (see [`config/cloudflare.ini.example`](config/cloudflare.ini.example)):

> Package scripts may start the service before any connections are configured. This is expected; use `systemctl stop strongswan` if needed.
>
> Ubuntu: `charon-systemd` and `swanctl` AppArmor policies permit `/etc/swanctl/`; no adjustment is needed for these paths.

If you only have a Global API Key, use this instead (the token and the two lines below **must not appear together**, or certbot will report an error):

Distributions retain strongSwan versions and backport security fixes, so versions may remain 6.0.x. The used `swanctl.conf` options (`connections`, `pools`, `secrets`, `local`/`remote`, `children`) are shared by 5.9 and 6.x.

---

## 4. Obtain a certificate with certbot

### 4.1 Store Cloudflare credentials

Create an empty file with secure permissions before writing content, avoiding a momentary exposure:

```sh
# install -m 600 /dev/null /etc/letsencrypt/cloudflare.ini
# nano /etc/letsencrypt/cloudflare.ini
```

### 4.2 Install the deploy hook

```ini
# Recommended: scoped API token
dns_cloudflare_api_token = YOUR_TOKEN
```

(If you haven't cloned this repository, paste the contents of [`config/certbot-deploy-hook.sh`](config/certbot-deploy-hook.sh) into that file manually.)

```ini
dns_cloudflare_email = you@example.com
dns_cloudflare_api_key = YOUR_GLOBAL_API_KEY
```

Check mode 600 to avoid certbot's "Unsafe permissions on credentials configuration file" warning:

```sh
# ls -l /etc/letsencrypt/cloudflare.ini
-rw------- 1 root root 62 ... /etc/letsencrypt/cloudflare.ini
```

### 4.2 Install the deploy hook

strongSwan requires certificates in the corresponding `/etc/swanctl/` directories. certbot runs executable **deploy hooks** under `/etc/letsencrypt/renewal-hooks/deploy/` after successful issuance or renewal.

```sh
# install -m 755 config/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
```

Without a checkout, copy [`config/certbot-deploy-hook.sh`](config/certbot-deploy-hook.sh) into the file manually and `chmod 755`.

The hook performs:

| Source (certbot) | Destination (strongSwan) | Description |
|---|---|---|
| `cert.pem` | `/etc/swanctl/x509/vpn-server.pem` | Server leaf certificate, mode 644 |
| `privkey.pem` | `/etc/swanctl/private/vpn-server.key` | Private key, mode 600 |
| `chain.pem` | `/etc/swanctl/x509ca/letsencrypt-chain-N.pem` | One intermediate certificate per file |

strongSwan reads only the first certificate in each file. Using `fullchain.pem` directly omits intermediates, which must be sent during handshakes to avoid client trust errors.

The hook runs `swanctl --load-creds` to reload certificates **without disconnecting clients**. Before daemon startup this fails, and this version ignores it with `|| true`.

### 4.3 Run a dry run first

```sh
# certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d vpn.example.com \
    --key-type ecdsa \
    --dry-run
```

`--dry-run` uses Let's Encrypt's staging environment, which does not count against the production rate limits and does not trigger the deploy hook. The first time you run certbot, it will ask for an email address and for you to agree to the terms of service.

| Parameter | Meaning |
|---|---|
| `certonly` | Obtain certificates without changing web server configuration |
| `--dns-cloudflare` | Cloudflare DNS plugin for DNS-01 |
| `--dns-cloudflare-credentials` | Credential file path, retained by certbot for renewal |
| `--dns-cloudflare-propagation-seconds 30` | Wait 30 seconds after writing TXT before CA validation; the default 10 seconds may be too short |
| `-d` | Certificate domain |
| `--key-type ecdsa` | Compact ECDSA certificates with fast handshakes, supported by major IKEv2 clients; use `rsa` if needed |
| `--dry-run` | Let's Encrypt staging rehearsal, **without production rate limits or deploy hook execution** |

First certbot use asks for an email and terms acceptance. Success output resembles:

```
Simulating a certificate request for vpn.example.com
Waiting 30 seconds for DNS changes to propagate
The dry run was successful.
```

### 4.4 Issue the certificate

Repeat without `--dry-run`:

```sh
# certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d vpn.example.com \
    --key-type ecdsa
```

Success shows certificate paths and expiry. The deploy hook has run; check:

```sh
# ls -l /etc/swanctl/x509 /etc/swanctl/x509ca /etc/swanctl/private
/etc/swanctl/private:
-rw------- 1 root root ... vpn-server.key
/etc/swanctl/x509:
-rw-r--r-- 1 root root ... vpn-server.pem
/etc/swanctl/x509ca:
-rw-r--r-- 1 root root ... letsencrypt-chain-1.pem
```

Inspect the certificate domain and validity:

```sh
# openssl x509 -in /etc/swanctl/x509/vpn-server.pem -noout -subject -issuer -dates -ext subjectAltName
```

Certificates are valid for 90 days, and certbot only actually renews once fewer than 30 days remain. After a successful renewal, the hook installs the new certificate into strongSwan again.

## 5. Configure strongSwan

`/etc/swanctl/swanctl.conf` already ends with `include conf.d/*.conf`, so we put our configuration in `conf.d/` and leave the main file untouched.

```
/etc/letsencrypt/
|-- cloudflare.ini                 Cloudflare credentials
|-- live/vpn.example.com/          Symlinks to current cert.pem / chain.pem / fullchain.pem / privkey.pem
|-- archive/vpn.example.com/       Actual renewal files (cert1.pem, cert2.pem ...)
|-- renewal/vpn.example.com.conf   Renewal parameters, credential path and validation method
`-- renewal-hooks/deploy/          Deploy hooks
```

Key points (see the comments in the file for details):

Apt certbot includes a systemd timer checking twice daily and renewing when fewer than 30 days remain:

```sh
# systemctl list-timers certbot.timer
# certbot renew --dry-run
```

`certbot renew --dry-run` simulates renewal including Cloudflare credentials; expect "Congratulations, all simulated renewals succeeded".

Run the hook manually to confirm it works with strongSwan running; repeat after starting the service in section 7:

```sh
# RENEWED_LINEAGE=/etc/letsencrypt/live/vpn.example.com /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh && echo OK
```

---

## 5. Configure strongSwan

### 5.1 Directory structure

After installing `strongswan-swanctl`, `/etc/swanctl/` contains:

```
/etc/swanctl/
|-- swanctl.conf        Main configuration, includes "include conf.d/*.conf"
|-- conf.d/             Configuration files; leave the main file unchanged
|-- x509/               Server / endpoint certificates
|-- x509ca/             CA and intermediate certificates
|-- private/            Private keys
`-- ...                 rsa/ ecdsa/ pkcs8/ pkcs12/ and other unused directories
```

### 5.2 Connection and address pools

```sh
# install -m 644 config/ikev2-eap.conf /etc/swanctl/conf.d/ikev2-eap.conf
# sed -i 's/vpn\.example\.com/your.domain/' /etc/swanctl/conf.d/ikev2-eap.conf
```

See [`config/ikev2-eap.conf`](config/ikev2-eap.conf) for the complete file. Settings:

| Setting | Meaning |
|---|---|
| `version = 2` | IKEv2 only; do not use legacy IKEv1 |
| `mobike = yes` | Maintain connectivity when switching Wi-Fi and cellular networks |
| `fragmentation = yes` | Fragment IKE messages to avoid drops with large certificate chains |
| `local_addrs = %any` | Listen on all local addresses |
| `pools = vpn4, vpn6` | Assign client virtual IPs from these pools |
| `send_certreq = no` | Do not request certificates from password-authenticated clients |
| `local.auth = pubkey` | Server public-key certificate authentication |
| `local.certs = vpn-server.pem` | Certificate relative to `/etc/swanctl/x509/`; matching key is located in `private/` |
| `local.id` | Server identity; **must match the certificate SAN domain** checked by clients |
| `remote.auth = eap-mschapv2` | EAP-MSCHAPv2 username/password client authentication |
| `remote.eap_id = %any` | Accept EAP identities for matching `id` in `secrets` |
| `children.net.local_ts` | Server traffic selector; `0.0.0.0/0, ::/0` sends all client traffic through the tunnel |
| `dpd_action = clear` | Dead peer detection clears SAs when the peer disappears |
| Omitted `esp_proposals` / `proposals` | Use strongSwan's maintained defaults rather than long manual `ike=` / `esp=` lists |
| `pools.vpn4.addrs` | IPv4 virtual pool, up to 254 concurrent clients |
| `pools.*.dns` | DNS assigned to clients; examples use Cloudflare public DNS, replace with trusted resolvers |

> **No IPv6?** Remove `vpn6`, `, vpn6` from `pools`, `, ::/0` from `local_ts`, and IPv6 firewall/sysctl lines. See section 11.

### 5.3 User accounts

```sh
# install -m 600 config/secrets.conf.example /etc/swanctl/conf.d/secrets.conf
# nano /etc/swanctl/conf.d/secrets.conf
```

```
secrets {
    eap-alice {
        id = alice
        secret = "a long random password"
    }
    eap-bob {
        id = bob
        secret = "another long random password"
    }
}
```

- The section name (`eap-alice`) is only a label; **`id` is the client username**.
- One section per user, with unique section names.
- Escape double quotes in passwords with a backslash. Prefer long random strings without characters requiring escaping:

```sh
# openssl rand -base64 18        # Example: Zx3K... (24 characters)
```

- This file contains plaintext passwords; keep it root-owned with mode `600`. **Never commit real passwords to Git.**

### 5.4 Check configuration before starting (optional)

Before daemon startup `swanctl` cannot connect, so inspect configuration visually:

```sh
# ls -l /etc/swanctl/conf.d/
# grep -n 'vpn.example.com' /etc/swanctl/conf.d/*.conf      # No placeholder domain should remain
```

---

### 6.2 Firewall (nftables)

### 6.1 Enable forwarding

```sh
# install -m 644 config/99-vpn-sysctl.conf /etc/sysctl.d/99-vpn.conf
# sysctl --system
# sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
```

> Have your cloud provider's VNC / web console ready before doing this, in case a rule mistake cuts off your connection. `nftables.service` loads `/etc/nftables.conf` at boot.

| Parameter | Purpose |
|---|---|
| `net.ipv4.ip_forward = 1` | Enable kernel IPv4 forwarding, required for a VPN gateway |
| `net.ipv6.conf.all.forwarding = 1` | Enable IPv6 forwarding |
| `accept_redirects = 0`, `send_redirects = 0` | Disable ICMP redirects to prevent unwanted route changes |
| `net.ipv6.conf.{all,default}.accept_ra = 2` | Accept router advertisements with IPv6 forwarding enabled; see below |

**IPv6 warning**: IPv6 forwarding makes Linux ignore RA by default, potentially removing the VPS IPv6 default route and breaking host connectivity. `accept_ra` is per interface; check your actual interface:

```sh
# ip -6 route show default                  # A default route should remain
# ip -br link                               # Find the interface name, such as eth0 / ens3
# sysctl net.ipv6.conf.eth0.accept_ra       # Expect 2; otherwise set it and add it to /etc/sysctl.d/99-vpn.conf
```

After `strongswan.service` starts, it automatically runs `swanctl --load-all`, which loads all the connections, address pools, certificates and passwords from `conf.d/`.

### 6.2 Firewall (nftables)

Three requirements: **allow UDP 500/4500**, **forward VPN pool traffic**, and **apply outbound source NAT**.

Choose the appropriate scenario based on the section 1.5 checks:

#### Scenario A: fresh VPS without firewall rules or Docker

Use the complete [`config/nftables-vpn.conf`](config/nftables-vpn.conf) rules:

```sh
# 1) Edit: confirm the SSH port (default 22) to avoid locking yourself out.
# nano config/nftables-vpn.conf

# 2) Check syntax without applying rules
# nft -c -f config/nftables-vpn.conf && echo SYNTAX_OK

# 3) Install and enable loading /etc/nftables.conf at boot
# install -m 644 config/nftables-vpn.conf /etc/nftables.conf
# systemctl enable --now nftables

# 4) Check
# nft list ruleset
```

> **Prepare the cloud web console / VNC first** so incorrect firewall rules can be rolled back if SSH disconnects.
>
> The first effective statement, `flush ruleset`, **removes all existing nftables rules**, including Docker, libvirt and Kubernetes rules. Use scenario B on hosts running these services.

Rule overview:

- `input`: drop by default; allow established connections, loopback, ICMP/ICMPv6 (needed for IPv6 neighbor discovery), SSH, UDP 500/4500 and ESP.
- `forward`: drop by default; allow VPN pool traffic and established connections, with TCP MSS clamping.
- `vpn_nat`: masquerade VPN pool traffic as the server public source address.
- Rules match source pools and **do not depend on interface names**.

#### Scenario B: existing firewall (ufw / custom nftables / iptables / Docker)

Keep existing rules and merge only the following:

1. Allow inbound UDP 500/4500 and ESP;
2. Allow forwarding from `10.10.10.0/24` and `fd10:10:10::/64`, plus established/related return traffic;
3. Add the final two independent `table ... vpn_nat` tables from `config/nftables-vpn.conf`, or equivalent iptables `-t nat ... -j MASQUERADE` rules. They can be appended without modifying existing tables;
4. With ufw, set `DEFAULT_FORWARD_POLICY=ACCEPT` in `/etc/default/ufw`, allow `ufw allow 500,4500/udp`, and add NAT in the `*nat` section of `/etc/ufw/before.rules`. Consult ufw documentation for syntax.

### 6.3 Reboot verification (strongly recommended)

Forwarding and firewall configuration must **survive reboot**. After section 7, reboot and recheck `sysctl net.ipv4.ip_forward`, `nft list ruleset` and `systemctl status strongswan`.

---

## 7. Start and verify

### 7.1 Start the service

```sh
# systemctl enable --now strongswan
# systemctl status strongswan --no-pager
```

Expect `active (running)`. `strongswan.service` automatically runs `swanctl --load-all` at startup.

> The service is `strongswan.service` (alias `strongswan-swanctl.service`). `strongswan-starter.service` belongs to the legacy stack.

### 7.2 Check listeners and loaded configuration

```sh
# ss -lunp | grep -E ':(500|4500)\b'       # charon should listen on UDP 500/4500
# swanctl --list-certs                     # Expect vpn-server.pem with your domain and intermediate certificates
# swanctl --list-conns                     # Expect ikev2-eap with local/remote/children
# swanctl --list-pools                     # Expect vpn4 / vpn6 and available address counts
# journalctl -u strongswan -n 50 --no-pager
```

## 8. Client Configuration

| Log | Meaning |
|---|---|
| Server / Server address | `vpn.example.com` (use the domain name, not the IP, or certificate validation will fail) |
| Remote ID | `vpn.example.com` (same as the server's `id`) |
| Authentication method | Username + password (EAP / IKEv2 EAP) |
| Username / Password | The `id` / `secret` from step 5.2 |
| Local ID | Leave empty |

After modifying `conf.d`, reload without disconnecting clients:

```sh
# swanctl --load-all
```

  If a Linux client stalls on large packets (MTU), set `mtu = 1280` in `/etc/strongswan.d/charon/kernel-netlink.conf`.

In another server terminal, watch logs while connecting a client (section 8):

```sh
# journalctl -fu strongswan
```

Expect logs such as `IKE_SA ikev2-eap[1] established between ...` and `CHILD_SA net{1} established with SPIs ...`. Then:

```sh
# swanctl --list-sas
```

(Note that repeatedly forcing renewals in production within a short period will hit Let's Encrypt's rate limits; for routine checks, `certbot renew --dry-run` is enough.)

**Upgrades**: `apt update && apt upgrade`; strongSwan security updates are delivered through the distribution. After upgrading, run `systemctl status strongswan` to confirm the service is healthy.

---

## 8. Client configuration

### 8.1 Common parameters

| Item | Value |
|---|---|
| Server / Server address | `vpn.example.com` (**domain**, not IP, for certificate validation) |
| Remote ID | `vpn.example.com` (matches server `local.id`) |
| Authentication | Username + password (IKEv2 EAP / username authentication) |
| Username / Password | `id` / `secret` from section 5.3 |
| Local ID | Leave empty |
| Client / CA certificate | No import required; Let's Encrypt roots are in the system trust store |

### 8.2 iOS / iPadOS / macOS (built in)

1. Settings > VPN (macOS: System Settings > VPN) > Add VPN Configuration.
2. Select **IKEv2**.
3. Use any description; set server and Remote ID to `vpn.example.com`, and leave Local ID empty.
4. Select username authentication and enter username/password.
5. Save and enable the connection.

See [strongSwan Apple client instructions](https://wiki.strongswan.org/projects/strongswan/wiki/AppleClients).

### 8.3 Windows 10 / 11 (built in)

1. Settings > Network & Internet > VPN > Add VPN.
2. Select Windows (built in), any connection name, server `vpn.example.com`, **IKEv2**, and username/password authentication. Enter credentials.
3. Save and select Connect.

See [strongSwan Windows instructions](https://wiki.strongswan.org/projects/strongswan/wiki/Windows7#C).

> Policy mismatch or unacceptable IKE credentials often indicate incompatible client defaults, especially older Windows DH groups. Use administrator PowerShell `Set-VpnConnectionIPsecConfiguration` with modern parameters such as AES256, SHA256 and Group14/ECP256. Consult Microsoft documentation for exact options; avoid weak server algorithms.

### 8.4 Android

1. Install **strongSwan VPN Client** from the app store.
2. Add a VPN: server `vpn.example.com`, **IKEv2 EAP (Username/Password)**, and your username/password.
3. Leave CA certificate on automatic selection.
4. Save and connect. The first connection asks for system VPN authorization.

See [strongSwan Android client instructions](https://wiki.strongswan.org/projects/strongswan/wiki/AndroidVPNClient).

### 8.5 Linux

Use the same packages and [`config/client-linux.conf`](config/client-linux.conf):

```sh
# apt install -y strongswan-swanctl charon-systemd libcharon-extra-plugins libcharon-extauth-plugins

# Trust the Let's Encrypt root certificate from the system certificate store
# cp /etc/ssl/certs/ISRG_Root_X1.pem /etc/ssl/certs/ISRG_Root_X2.pem /etc/swanctl/x509ca/

# install -m 600 config/client-linux.conf /etc/swanctl/conf.d/vpn-client.conf
# nano /etc/swanctl/conf.d/vpn-client.conf      # Change domain, username and password

# systemctl restart strongswan
# swanctl --initiate --child net                # Connect
# swanctl --list-sas                            # Check status
# swanctl --terminate --ike vpn                 # Disconnect
```

- `vips = 0.0.0.0, ::` requests IPv4 and IPv6 virtual addresses. `start_action = none` requires manual initiation; `start` connects after `swanctl --load-all`.
- Client routes: `remote_ts = 0.0.0.0/0, ::/0` routes **all traffic** through the VPN, including SSH. strongSwan bypasses the VPN server endpoint, but check management connectivity before configuring a remote machine as a client.
- If large pages stall due to MTU/fragmentation, set `mtu = 1280` in `/etc/strongswan.d/charon/kernel-netlink.conf` and restart.

### 8.6 Check connectivity

On the client:

1. Open an IP check site such as <https://ifconfig.me>; it should show **the server public IP**.
2. If IPv6 is available, check the IPv6 address too, for example <https://ipv6.icanhazip.com>.
3. Run a DNS leak test; local ISP DNS should not appear.
4. Check `swanctl --list-sas` on the server for the client and increasing byte counters.

---

## 9. Routine maintenance

### 9.1 Add, change or delete users

Edit `/etc/swanctl/conf.d/secrets.conf`, then:

```sh
# swanctl --load-creds
```

Online users remain connected.

> Existing connections remain valid after deleting users or changing passwords until reconnection. For immediate termination, find the connection with `swanctl --list-sas` and use `swanctl --terminate --ike ikev2-eap --child-id <number>` or `--ike-id <number>` (`#N` in the output).

By default an account can connect on multiple devices under the default `unique` policy. To allow one connection per account, add `unique = replace` to `ikev2-eap`, then run `swanctl --load-all`.

### 9.2 Certificate renewal

Renewal is automatic. For troubleshooting, check:

```sh
# systemctl status certbot.timer certbot.service
# journalctl -u certbot --no-pager | tail -50
# less /var/log/letsencrypt/letsencrypt.log
```

Common renewal failures: deleted or changed Cloudflare tokens, or slow DNS propagation. Increase `--dns-cloudflare-propagation-seconds` and the corresponding `/etc/letsencrypt/renewal/vpn.example.com.conf` value.

Use `certbot renew --dry-run` routinely. **Avoid frequent forced production renewals** (`--force-renewal`) because of Let's Encrypt rate limits.

### 9.3 Upgrades

```sh
# apt update && apt upgrade
# systemctl status strongswan --no-pager
```

Distribution updates supply strongSwan security fixes. Package upgrades restart the service; connected users briefly disconnect and reconnect. Back up section 9.4 data before major OS upgrades such as Debian 13 -> 14.

### 9.4 Backup and recovery

Back up:

```sh
# tar czf vpn-backup-$(date +%F).tar.gz \
    /etc/swanctl/conf.d \
    /etc/letsencrypt/cloudflare.ini /etc/letsencrypt/renewal /etc/letsencrypt/renewal-hooks \
    /etc/sysctl.d/99-vpn.conf /etc/nftables.conf
```

Backups contain **plaintext passwords and API tokens**; encrypt them and keep them private.

Restore on a new host: repeat section 3, unpack the backup, point DNS to the new IP, run `certbot renew` or reissue (the hook installs the certificate), then start services. Certificates and keys can be reissued instead of backed up.

### 9.5 Logs

IKE logs go to the systemd journal and include client source IPs. For debugging, edit the `journal { }` log levels in `/etc/strongswan.d/charon-systemd.conf`, then `systemctl restart strongswan`. Restore defaults afterwards:

```
charon-systemd {
    journal {
        default = 1
        ike = 2
    }
}
```

## 10. Troubleshooting

---

## 11. What This Guide Has Been Verified Against

1. **Use a unique, long random password per user** (at least 16 random characters). EAP-MSCHAPv2 depends on password strength and strict server certificate validation to prevent interception. Connect using the domain and never ignore certificate warnings.
2. **SSH**: use keys, disable root password login, and consider restricting source addresses.
3. **Automatic security updates**: install and enable `unattended-upgrades` for timely strongSwan, OpenSSL and kernel patches.
4. **Protect the Cloudflare token**: scope `Zone:DNS:Edit` to one zone, keep credentials mode 600, rotate regularly, and never commit them.
5. **Expose only required ports**: SSH and UDP 500/4500.
6. **Monitor certificate expiry**: certbot sends reminders to the registered email; also check `certbot certificates` regularly.
7. **Limit VPN access to private networks**: default forwarding permits access to server-side private networks. Add private-destination drop rules before outbound accept rules where isolation is needed.

---

## 11. Optional advanced configuration

**Not verified** (due to limitations of the test environment; keep an eye on these after deploying):

1. Remove the `vpn6` pool in `ikev2-eap.conf`, set `pools = vpn4` and `local_ts = 0.0.0.0/0`.
2. Remove `ip6 saddr ...` forwarding rules and the `vpn_nat6` table from `nftables-vpn.conf`.
3. Remove the IPv6 sysctl settings.
4. Reload with `sysctl --system && nft -f /etc/nftables.conf && swanctl --load-all`.

> IPv4-only tunnels can leak native IPv6 on IPv6-enabled client networks. Disable client IPv6 or keep this guide's dual-stack configuration.

### 11.2 Split tunneling

Replace `children.net.local_ts = 0.0.0.0/0, ::/0` with the networks requiring VPN access, for example private networks only:

```
local_ts = 192.168.100.0/24
```

Only these networks use the tunnel; other traffic uses the local network. Run `swanctl --load-all` and reconnect clients.

### 11.3 RSA certificates

Replace `--key-type ecdsa` with `--key-type rsa`, optionally `--rsa-key-size 3072`. The hook and `swanctl.conf` are unchanged; strongSwan detects the key in `private/`. To switch existing certificates, reissue with `--force-renewal` or delete the lineage and reissue.

### 11.4 Multiple domains and wildcards

- Add alternate domains with `-d vpn.example.com -d vpn2.example.com`. Both appear in the SAN. Server `local.id` must still match a selected domain; add another connection using `local.id = vpn2.example.com` and the same certificate if both identities must be accepted.
- For wildcards, see section 4.4.

### 11.5 Change assigned DNS servers

Change `pools.vpn4.dns` / `pools.vpn6.dns`, run `swanctl --load-all`, and reconnect clients. A local resolver such as unbound requires server installation and firewall access to port 53 restricted to VPN pools.

---

## 12. Troubleshooting

### 12.1 Identify the failing layer

Answer the following questions:

1. **Do client packets reach the server?** -> Capture packets (12.2).
2. **Did the IKE handshake succeed?** -> Check server logs.
3. **Tunnel established without internet?** -> Check forwarding, NAT and MTU.

### 12.2 Packet capture

```sh
# apt install -y tcpdump
# tcpdump -ni any 'udp port 500 or udp port 4500'
```

When a client connects:

- No packets: check cloud security groups, firewalls, DNS or client network IPsec restrictions.
- Incoming packets without responses: check strongSwan startup and the host firewall.
- Bidirectional traffic but handshake failure: check `journalctl -u strongswan` for authentication or proposal errors.

### 12.3 Troubleshooting table

| Symptom | Checks |
|---|---|
| certbot `Unsafe permissions` | `chmod 600 /etc/letsencrypt/cloudflare.ini` |
| certbot Cloudflare authentication / zone error | Scope **DNS:Edit** to the domain zone; do not combine token and email/api_key; verify the domain belongs to the account |
| certbot validation timeout | Increase `--dns-cloudflare-propagation-seconds`, for example to 60 |
| `swanctl: connecting to 'unix:///var/run/charon.vici' failed` | Daemon is stopped; check `systemctl status strongswan` |
| eap-mschapv2 missing / `loading EAP_MSCHAPV2 method failed` | Install `libcharon-extauth-plugins` |
| `no EAP key found` / login failure | Match `id` in `secrets.conf` to the client username and run `swanctl --load-creds` |
| Invalid certificate / server identity cannot be verified | Use the domain rather than an IP; verify intermediates with `swanctl --list-certs` and check server/client clocks |
| Cannot connect and no requests in server logs | Check inbound UDP 500/4500, correct DNS and Cloudflare DNS-only status |
| Handshake succeeds but `CHILD_SA` fails with `no matching proposal` | Update old client algorithms or add appropriate `esp_proposals` under `children.net` |
| Connected without internet | Check IPv4 forwarding and NAT/forward rules; inspect traffic with `tcpdump -ni any host 10.10.10.1` |
| Internet works but DNS fails | Check DNS assignment in `swanctl --list-sas` or client settings, and server access to `pools.*.dns` |
| IPv6 fails / host IPv6 disappears | See 6.1: check `accept_ra` and the default route, or use IPv4 only (11.1) |
| Large pages fail or traffic is very slow | Check MTU/MSS clamping in nftables; set `mtu = 1280` on Linux clients |
| Fails after reboot | Check `systemctl is-enabled strongswan nftables` and persistent sysctl files under `/etc/sysctl.d/` |
| Certificate loading denied on Ubuntu | Check `dmesg \| grep -i apparmor`; packaged policies permit `/etc/swanctl/`. Adjust for custom paths |

### 12.4 Increase log detail

See 9.5. Set `ike = 2`, optionally `cfg = 2`, restart, reproduce the problem and restore default logging.

---

## 13. Uninstallation and rollback

```sh
# Stop and disable services
# systemctl disable --now strongswan

# Remove firewall rules; back up or restore previous rules if using this tutorial's /etc/nftables.conf.
# nft flush ruleset

# Delete VPN configuration
# rm -f /etc/swanctl/conf.d/ikev2-eap.conf /etc/swanctl/conf.d/secrets.conf
# rm -f /etc/swanctl/x509/vpn-server.pem /etc/swanctl/private/vpn-server.key /etc/swanctl/x509ca/letsencrypt-chain-*.pem
# rm -f /etc/sysctl.d/99-vpn.conf
# rm -f /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh

# Revoke and delete the certificate (optional)
# certbot revoke --cert-name vpn.example.com --delete-after-revoke
# shred -u /etc/letsencrypt/cloudflare.ini    # Also delete the token in the Cloudflare dashboard

# Uninstall packages
# apt purge strongswan-swanctl charon-systemd libcharon-extra-plugins libcharon-extauth-plugins libstrongswan-extra-plugins
# apt autoremove --purge
```

---

## 14. Validation scope

Validation scope and remaining checks for your own deployment:

**Validated** (Ubuntu 24.04, strongSwan 5.9.13, certbot 5.8 and certbot-dns-cloudflare):

- Package split: `eap-mschapv2` in `libcharon-extauth-plugins`, `eap-identity` in `libcharon-extra-plugins`; `charon-systemd` supplies `strongswan.service` with `ExecStartPost` calling `swanctl --load-all`; `swanctl.conf` includes `conf.d/*.conf`; apt certbot supplies `certbot.timer`.
- Actually loaded `config/ikev2-eap.conf`, `secrets.conf.example`, `client-linux.conf` and deploy hook: connection, both pools, certificate, key and EAP secrets loaded successfully.
- Complete IKE handshake with a test CA (root -> intermediate -> ECDSA leaf): the client validated the server chain, **EAP-MSCHAPv2 succeeded**, and the server assigned `10.10.10.x` / `fd10:10:10::x` virtual IPs and DNS.
- The deploy hook correctly split and installed the leaf certificate, key and intermediates using certbot's `RENEWED_LINEAGE` convention.
- `nftables-vpn.conf` passed `nft -c`; Cloudflare credential format and certbot options came from the installed plugin documentation.

**Unverified** (test environment limitations; check after deployment):

- Physical Debian 13 / Ubuntu 26.04 installations: package indexes indicate Ubuntu 26.04 provides strongSwan 6.0.x with matching package splits, but neither system was actually installed. The used `swanctl.conf` syntax is shared by 5.9 and 6; verify real `swanctl --load-all` output.
- Real Let's Encrypt issuance (requires a Cloudflare account) and the Cloudflare token validation API were untested.
- ESP data plane, forwarding, NAT and applied nftables: the test kernel lacks ESP support, so CHILD_SA could not be established.
- Client UI steps were untested. Descriptions use common workflows; labels vary by OS version.
- Some operations in sections 9-11, such as `unique = replace`, `--ike-id`, journald retention and Windows PowerShell settings, describe common practices but were not individually tested. Check `swanctl` / `man` help first.
- Official strongSwan (docs.strongswan.org) and certbot documentation were inaccessible in the writing environment; configuration uses distribution templates, installed plugin documentation and observed behavior.
