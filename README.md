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

1. [Prerequisites](#1-prerequisites)
2. [Create a Cloudflare API Token](#2-create-a-cloudflare-api-token)
3. [Install Packages](#3-install-packages)
4. [Obtain a Certificate with certbot](#4-obtain-a-certificate-with-certbot)
5. [Configure strongSwan](#5-configure-strongswan)
6. [Kernel Parameters and Firewall](#6-kernel-parameters-and-firewall)
7. [Start and Verify](#7-start-and-verify)
8. [Client Configuration](#8-client-configuration)
9. [Routine Maintenance](#9-routine-maintenance)
10. [Troubleshooting](#10-troubleshooting)
11. [What This Guide Has Been Verified Against](#11-what-this-guide-has-been-verified-against)

The `config/` directory in this repository contains every configuration file used in this guide, ready to copy:

| File | Deploy to |
|---|---|
| [`config/cloudflare.ini.example`](config/cloudflare.ini.example) | `/etc/letsencrypt/cloudflare.ini` |
| [`config/certbot-deploy-hook.sh`](config/certbot-deploy-hook.sh) | `/etc/letsencrypt/renewal-hooks/deploy/strongswan.sh` |
| [`config/ikev2-eap.conf`](config/ikev2-eap.conf) | `/etc/swanctl/conf.d/ikev2-eap.conf` |
| [`config/secrets.conf.example`](config/secrets.conf.example) | `/etc/swanctl/conf.d/secrets.conf` |
| [`config/99-vpn-sysctl.conf`](config/99-vpn-sysctl.conf) | `/etc/sysctl.d/99-vpn.conf` |
| [`config/nftables-vpn.conf`](config/nftables-vpn.conf) | `/etc/nftables.conf` |
| [`config/client-linux.conf`](config/client-linux.conf) | `/etc/swanctl/conf.d/vpn-client.conf` on the Linux client |

---

## 1. Prerequisites

1. **A VPS**: Debian 13 or Ubuntu 26.04 LTS, x86-64, with a public IPv4 address (IPv6 optional). All commands below are run as root (regular users should run `sudo -i` first).
2. **A domain whose DNS is hosted on Cloudflare**, e.g. `vpn.example.com`.
   - Add an **A record** (plus an **AAAA** record if you have IPv6) pointing to the VPS's public IP.
   - **It must be set to "DNS only" (grey cloud)**. Do not enable the Cloudflare proxy (orange cloud); the proxy does not forward IKE's UDP traffic.
3. **Open the ports**: in your cloud provider's security group / firewall, allow inbound **UDP 500** and **UDP 4500** (plus your SSH port).
   The "UDP 50/51" mentioned in the old guide are not UDP ports but the ESP/AH protocol numbers. Modern clients are almost always behind NAT, where ESP is encapsulated in UDP 4500, so the cloud security group only needs UDP 500 / 4500.
4. The system clock must be accurate (certificate validity and IKE both depend on it):

```sh
timedatectl status          # look for "System clock synchronized: yes"
```

Conventions used below: domain `vpn.example.com`, IPv4 client address pool `10.10.10.0/24`, IPv6 address pool `fd10:10:10::/64`. Replace them with your own values throughout.

## 2. Create a Cloudflare API Token

certbot proves domain ownership by temporarily writing a TXT record at `_acme-challenge.vpn.example.com` through the Cloudflare API.

1. Log in to Cloudflare → avatar in the top-right corner → **My Profile** → **API Tokens** → **Create Token**.
2. Choose the **Edit zone DNS** template, or create a custom token with:
   - Permissions: **Zone → DNS → Edit**
   - Zone Resources: **Include → Specific zone → your domain** (e.g. `example.com`)
3. Copy the token after creating it (it is only shown once).

> The official certbot-dns-cloudflare documentation recommends this kind of **scoped API Token**. The "Global API Key" (account-wide full permissions) is still supported but **not recommended**: if it leaks, your entire account is compromised. If you only have a Global API Key, see the next section for the format.

## 3. Install Packages

The commands are the same on both distributions:

```sh
apt update
apt install -y \
    strongswan-swanctl charon-systemd \
    libcharon-extra-plugins libcharon-extauth-plugins libstrongswan-extra-plugins \
    nftables \
    certbot python3-certbot-dns-cloudflare
```

What each package does:

| Package | Purpose |
|---|---|
| `charon-systemd` | strongSwan's IKE daemon; provides the systemd service `strongswan.service` |
| `strongswan-swanctl` | The `swanctl` command and the `/etc/swanctl/` directory |
| `libcharon-extauth-plugins` | **EAP-MSCHAPv2** plugin (the client authentication method used in this guide) |
| `libcharon-extra-plugins` | EAP-Identity and other plugins |
| `libstrongswan-extra-plugins` | ChaCha20-Poly1305 and other algorithms |
| `certbot` + `python3-certbot-dns-cloudflare` | ACME client and its Cloudflare DNS plugin |

> **Do not install the `strongswan` metapackage.** It pulls in the legacy `strongswan-starter` (`ipsec.conf` / the `ipsec` command), which is an alternative to `charon-systemd`; having both installed causes conflicts.

Confirm that only the `charon-systemd` stack is installed:

```sh
dpkg -l | grep -E 'strongswan|charon'
systemctl cat strongswan.service | head -3
```

> Ubuntu's official certbot documentation recommends installing via snap; this guide uses the apt version instead. The apt version ships with `certbot.timer` and renews automatically.

## 4. Obtain a Certificate with certbot

### 4.1 Save the Cloudflare credentials

```sh
install -m 600 /dev/null /etc/letsencrypt/cloudflare.ini
nano /etc/letsencrypt/cloudflare.ini
```

Contents (see [`config/cloudflare.ini.example`](config/cloudflare.ini.example)):

```ini
# API Token (recommended)
dns_cloudflare_api_token = your_api_token
```

If you only have a Global API Key, use this instead (the token and the two lines below **must not appear together**, or certbot will report an error):

```ini
dns_cloudflare_email = your_cloudflare_login_email
dns_cloudflare_api_key = your_global_api_key
```

The permissions must be 600, otherwise certbot warns "Unsafe permissions on credentials configuration file":

```sh
chmod 600 /etc/letsencrypt/cloudflare.ini
ls -l /etc/letsencrypt/cloudflare.ini
```

### 4.2 Install the deploy hook

strongSwan does not read the files under `/etc/letsencrypt/live/`; the certificate has to be installed into `/etc/swanctl/`. We use certbot's **deploy hook**, which runs automatically after every **successful issuance or renewal**.

```sh
install -m 755 config/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
```

(If you haven't cloned this repository, paste the contents of [`config/certbot-deploy-hook.sh`](config/certbot-deploy-hook.sh) into that file manually.)

What the script does:

- `cert.pem` (leaf certificate) → `/etc/swanctl/x509/vpn-server.pem`
- `privkey.pem` → `/etc/swanctl/private/vpn-server.key` (mode 600)
- `chain.pem` (intermediate certificates) is split into one file per certificate → `/etc/swanctl/x509ca/letsencrypt-chain-N.pem`. strongSwan reads only the first certificate in each file, so `fullchain.pem` cannot be used directly. The server must send the intermediate certificates to clients during the handshake, otherwise some clients will report "certificate not trusted".
- Runs `swanctl --load-creds` to hot-reload the certificate **without disconnecting connected clients**.

### 4.3 Do a dry run first

```sh
certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d vpn.example.com \
    --key-type ecdsa \
    --dry-run
```

`--dry-run` uses Let's Encrypt's staging environment, which does not count against the production rate limits and does not trigger the deploy hook. The first time you run certbot, it will ask for an email address and for you to agree to the terms of service.

### 4.4 Request the real certificate

Remove `--dry-run`:

```sh
certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d vpn.example.com \
    --key-type ecdsa
```

- `--key-type ecdsa`: an ECDSA certificate (smaller and faster to handshake), supported by all mainstream IKEv2 clients. If you need RSA, change it to `--key-type rsa`; the remaining steps are the same.
- For a wildcard domain (`*.example.com`), also add `-d '*.example.com'`; DNS-01 is the only way to get a wildcard certificate.

On success the deploy hook runs automatically (strongSwan isn't running yet at this point, so the hook's `swanctl --load-creds` will fail; the script already ignores that failure). Check:

```sh
ls -l /etc/swanctl/x509 /etc/swanctl/x509ca /etc/swanctl/private
```

You should see `vpn-server.pem`, `letsencrypt-chain-1.pem` and `vpn-server.key`.

### 4.5 Confirm automatic renewal

```sh
systemctl list-timers certbot.timer     # enabled by default for the apt certbot on Debian/Ubuntu
certbot renew --dry-run                 # simulate a renewal to verify the credentials and the process work
```

Certificates are valid for 90 days, and certbot only actually renews once fewer than 30 days remain. After a successful renewal, the hook installs the new certificate into strongSwan again.

## 5. Configure strongSwan

`/etc/swanctl/swanctl.conf` already ends with `include conf.d/*.conf`, so we put our configuration in `conf.d/` and leave the main file untouched.

### 5.1 Connection and address pools

```sh
install -m 644 config/ikev2-eap.conf /etc/swanctl/conf.d/ikev2-eap.conf
sed -i 's/vpn\.example\.com/vpn.yourdomain.com/' /etc/swanctl/conf.d/ikev2-eap.conf
```

Key points (see the comments in the file for details):

- `local { auth = pubkey; certs = vpn-server.pem; id = <your domain> }`: the server authenticates itself with the certificate. `id` must match the domain in the certificate (SAN); clients verify the server against it.
- `remote { auth = eap-mschapv2; eap_id = %any }`: clients log in with a username and password.
- `pools = vpn4, vpn6`: assigns clients a virtual IP and DNS servers from the address pools.
- `local_ts = 0.0.0.0/0, ::/0`: all traffic goes through the VPN.
- `esp_proposals` / `proposals` are omitted, so strongSwan's **default cipher suites** are used. They are maintained by strongSwan from release to release, which is safer than a long hand-written `ike=` / `esp=` list.

> **No IPv6?** Remove the `vpn6` pool from `ikev2-eap.conf`, the `, vpn6` from the `pools` line, the `, ::/0` from `local_ts`, and the IPv6-related lines in [`nftables-vpn.conf`](config/nftables-vpn.conf).

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

- The section name (`eap-alice`) is just a label; **`id` is the username the client enters**.
- Generate passwords with `openssl rand -base64 18`. The file contains plaintext passwords, so make sure it stays mode 600 and owned by root.

## 6. Kernel Parameters and Firewall

### 6.1 Enable forwarding

```sh
install -m 644 config/99-vpn-sysctl.conf /etc/sysctl.d/99-vpn.conf
sysctl --system
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
```

**IPv6 note**: once IPv6 forwarding is enabled, Linux ignores Router Advertisements (RA) by default, and the VPS may lose its IPv6 default route as a result. The config already sets `accept_ra = 2`, but `accept_ra` applies per interface, so check it against your actual interface:

```sh
ip -6 route show default                 # a default route should still be present
ip -br link                              # find the interface name, e.g. eth0 / ens3
sysctl net.ipv6.conf.eth0.accept_ra      # should be 2; if not, set it manually and add it to /etc/sysctl.d/99-vpn.conf
```

If you only use IPv4, you can delete the IPv6 lines.

### 6.2 Firewall (nftables)

Three things are needed: allow UDP 500/4500, allow forwarding of traffic from the VPN pools, and NAT (masquerade) outbound traffic.

**Scenario A: a fresh VPS with no firewall rules at all** — use the complete ruleset from this repository:

```sh
# Edit it first and make sure the SSH port (default 22) is correct, or you will lock yourself out!
nano config/nftables-vpn.conf
nft -c -f config/nftables-vpn.conf       # syntax check only; nothing is applied
install -m 644 config/nftables-vpn.conf /etc/nftables.conf
systemctl enable --now nftables
nft list ruleset
```

> Have your cloud provider's VNC / web console ready before doing this, in case a rule mistake cuts off your connection. `nftables.service` loads `/etc/nftables.conf` at boot.

**Scenario B: you already have a firewall (ufw / your own nftables / iptables)** — don't overwrite it; merge only:

- Allow inbound UDP 500 and UDP 4500;
- Allow forwarding from `10.10.10.0/24` and `fd10:10:10::/64`;
- Add the NAT rules from the last two `table ... vpn_nat` blocks of `config/nftables-vpn.conf` (or the equivalent iptables `-t nat ... MASQUERADE`);
- If you use ufw, also change `DEFAULT_FORWARD_POLICY` to `ACCEPT` in `/etc/default/ufw`, and put the NAT rules in `/etc/ufw/before.rules`.

The NAT rules are not tied to an interface name (they only match the source address pools), so nothing needs changing whatever your interface is called.

## 7. Start and Verify

```sh
systemctl enable --now strongswan
systemctl status strongswan --no-pager
```

After `strongswan.service` starts, it automatically runs `swanctl --load-all`, which loads all the connections, address pools, certificates and passwords from `conf.d/`.

Check what was loaded:

```sh
swanctl --list-certs        # should show vpn-server.pem (subject is your domain) and the intermediate certificate
swanctl --list-conns        # should show ikev2-eap
swanctl --list-pools        # should show vpn4 / vpn6
journalctl -u strongswan -n 50 --no-pager
```

To reload the configuration manually (after editing `conf.d`):

```sh
swanctl --load-all
```

Once a client has connected:

```sh
swanctl --list-sas          # established connections, including usernames and assigned virtual IPs
```

## 8. Client Configuration

Settings common to all clients:

| Setting | Value |
|---|---|
| Server / Server address | `vpn.example.com` (use the domain name, not the IP, or certificate validation will fail) |
| Remote ID | `vpn.example.com` (same as the server's `id`) |
| Authentication method | Username + password (EAP / IKEv2 EAP) |
| Username / Password | The `id` / `secret` from step 5.2 |
| Local ID | Leave empty |

Because the certificate is issued by Let's Encrypt, whose root certificate the operating system already trusts, **clients do not need to import any certificates**.

- **iOS / iPadOS / macOS**: Settings → VPN → Add VPN Configuration → Type **IKEv2**; fill in Server and Remote ID, set User Authentication to "Username", and enter the username and password. See [strongSwan's notes on Apple clients](https://wiki.strongswan.org/projects/strongswan/wiki/AppleClients).
- **Windows 10 / 11**: Settings → Network & Internet → VPN → Add VPN; set VPN provider to "Windows (built-in)", VPN type to **IKEv2**, and Type of sign-in info to "User name and password". See [strongSwan's notes on Windows](https://wiki.strongswan.org/projects/strongswan/wiki/Windows7#C).
  - If connecting fails with "IKE authentication credentials are unacceptable" / "policy match error", it's usually because the built-in client's default crypto parameters (older Windows versions default to weak DH groups) don't match the server's default suites. Use `Set-VpnConnectionIPsecConfiguration` in PowerShell to specify modern parameters such as `AES256`, `SHA256`, `Group14`/`ECP256`. Enabling weak algorithms on the server to work around this is not recommended.
- **Android**: install **strongSwan VPN Client** from Google Play, create a new profile with type **IKEv2 EAP (Username/Password)**, and fill in the server domain, username and password; leave the CA certificate on "Select automatically". See [the Android client notes](https://wiki.strongswan.org/projects/strongswan/wiki/AndroidVPNClient).
- **Linux**: the same packages + [`config/client-linux.conf`](config/client-linux.conf):

```sh
apt install -y strongswan-swanctl charon-systemd libcharon-extra-plugins libcharon-extauth-plugins
# Make strongSwan trust the Let's Encrypt root certificates (already in the system certificate store)
cp /etc/ssl/certs/ISRG_Root_X1.pem /etc/ssl/certs/ISRG_Root_X2.pem /etc/swanctl/x509ca/
install -m 600 config/client-linux.conf /etc/swanctl/conf.d/vpn-client.conf
nano /etc/swanctl/conf.d/vpn-client.conf          # change the domain, username and password
systemctl restart strongswan
swanctl --initiate --child net                    # connect
swanctl --list-sas
swanctl --terminate --ike vpn                     # disconnect
```

  If a Linux client stalls on large packets (MTU), set `mtu = 1280` in `/etc/strongswan.d/charon/kernel-netlink.conf`.

Once connected, visit a site such as <https://ifconfig.me> from the client; it should show the VPS's IP.

## 9. Routine Maintenance

**Adding / removing users**: edit `/etc/swanctl/conf.d/secrets.conf`, then

```sh
swanctl --load-creds
```

Users who are already online are not disconnected; to kick someone, use `swanctl --terminate --ike ikev2-eap --ike-id <number>` (the number is shown by `--list-sas`). By default, the same account can be online from multiple devices at once.

**Certificate renewal**: fully automatic. When troubleshooting, check `journalctl -u certbot` or `/var/log/letsencrypt/letsencrypt.log`. To force-test the whole chain:

```sh
certbot renew --force-renewal --deploy-hook /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
```

(Note that repeatedly forcing renewals in production within a short period will hit Let's Encrypt's rate limits; for routine checks, `certbot renew --dry-run` is enough.)

**Upgrades**: `apt update && apt upgrade`; strongSwan security updates are delivered through the distribution. After upgrading, run `systemctl status strongswan` to confirm the service is healthy.

**Log level**: when you need more detailed IKE logs, edit `/etc/strongswan.d/charon-systemd.conf`, set the levels in the `journal { }` section, then run `systemctl restart strongswan`. Remember to change them back after debugging.

```
charon-systemd {
    journal {
        default = 1
        ike = 2
    }
}
```

## 10. Troubleshooting

| Symptom | What to check |
|---|---|
| certbot reports `Unsafe permissions` | `chmod 600 /etc/letsencrypt/cloudflare.ini` |
| certbot reports a Cloudflare authentication error / can't find the zone | The token's permissions must include **DNS:Edit** on the domain's zone; don't mix the token with email/api_key |
| certbot validation times out | Increase `--dns-cloudflare-propagation-seconds` (e.g. to 60) |
| `swanctl: connecting to 'unix:///var/run/charon.vici' failed` | The daemon isn't running: `systemctl status strongswan` |
| `loading EAP_MSCHAPV2 method failed` / no eap-mschapv2 in the logs | `libcharon-extauth-plugins` is not installed |
| Logs show `no EAP key found` / login fails | The `id` in `secrets.conf` doesn't match the client's username, or `swanctl --load-creds` wasn't run |
| Client says the certificate is invalid / server identity can't be verified | The client must use the domain name, not the IP; confirm the intermediate certificate appears in `swanctl --list-certs`; check the server's clock |
| Client can't connect and the server logs show no requests at all | The cloud security group / firewall doesn't allow UDP 500 and 4500; the domain doesn't resolve to this machine; the Cloudflare record is set to "Proxied" |
| Connected but no internet access | Check that `sysctl net.ipv4.ip_forward` is 1 and that the NAT and forward rules exist in `nft list ruleset` |
| IPv6 doesn't work / the server lost its own IPv6 | See 6.1: check `accept_ra` and the default route, or simply remove the IPv6 pool |
| Large pages won't load, or speeds are abnormally slow | MTU/MSS issue: confirm the MSS clamping rule exists in nftables; set `mtu = 1280` on Linux clients |
| Loading certificates is denied on Ubuntu | `dmesg \| grep -i apparmor`; `charon-systemd` / `swanctl` ship AppArmor profiles that allow reading `/etc/swanctl/`; if you changed the paths, adjust the profiles to match |

## 11. What This Guide Has Been Verified Against

An honest account, so you can judge what needs to be double-checked on your own machine:

**Actually verified** (in an Ubuntu 24.04 environment with strongSwan 5.9.13, certbot 5.8 + certbot-dns-cloudflare):

- Package names and splits: `eap-mschapv2` is in `libcharon-extauth-plugins`, `eap-identity` is in `libcharon-extra-plugins`; `strongswan.service` comes from `charon-systemd`, and its `ExecStartPost` automatically runs `swanctl --load-all`; `swanctl.conf` ships with `include conf.d/*.conf`.
- `ikev2-eap.conf`, `secrets.conf.example` and the deploy hook from `config/` were actually loaded: the connection, both address pools, the certificate, the private key and the EAP passwords all loaded successfully.
- A complete IKE handshake succeeded using a test CA (root → intermediate → ECDSA leaf): the client validated the server's certificate chain, **EAP-MSCHAPv2 authentication succeeded**, and the server assigned `10.10.10.x` / `fd10:10:10::x` virtual IPs and DNS servers.
- Under certbot's `RENEWED_LINEAGE` convention, the deploy hook correctly split and installed the leaf certificate, private key and intermediate certificates.
- `nftables-vpn.conf` passes the `nft -c` syntax check; the certbot-dns-cloudflare credential format and command-line option names are taken from the plugin's bundled documentation.

**Not verified** (due to limitations of the test environment; keep an eye on these after deploying):

- Real installs on Debian 13 and Ubuntu 26.04: it was only confirmed from package indexes and public information that Ubuntu 26.04 ships strongSwan 6.0.x and that both systems use the same package names and splits; nothing was actually installed and run. The parts of the `swanctl.conf` syntax used in this guide are the same in strongSwan 6 and 5.9, but treat the output of `swanctl --load-all` on your actual machine as authoritative.
- Real Let's Encrypt issuance (requires your Cloudflare account).
- The ESP data plane, forwarding, NAT, and nftables actually taking effect: the test environment's kernel has no ESP support, so a CHILD_SA could not be established.
- The actual connection UI of each client platform; the descriptions are based on common practice, and UI labels may change between OS versions.
- The official strongSwan documentation site (docs.strongswan.org) and certbot's online documentation were not reachable from this environment; the configuration above is based on the configuration templates shipped with the distribution packages, the plugins' bundled documentation, and observed behavior.
