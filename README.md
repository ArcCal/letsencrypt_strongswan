# Let's Encrypt + strongSwan: IKEv2 VPN with two inputs

Run `install.sh` on a dedicated VPS and enter only the **certificate domain** and **Cloudflare API token**. The installer sets up strongSwan, obtains a public CA certificate using certbot and Cloudflare DNS-01, generates two random accounts and two iPhone `.mobileconfig` profiles, and configures full-tunnel routing, a firewall, and automatic renewal.

The server authenticates with a Let's Encrypt certificate. Clients authenticate with **EAP-MSCHAPv2 usernames and passwords**. Clients do not need client certificates or a private CA installation. Server certificate and domain validation remain enabled.

## 1. Requirements

- A fresh, dedicated Debian 13 or Ubuntu 24.04 / 26.04 VPS booted with **systemd**, with a root terminal. The installer uses distribution packages, without compiling strongSwan.
- An inbound-reachable public IPv4 address, either assigned directly to a NIC or mapped to its private address by the cloud provider, such as a Google Compute Engine External IP. Public IPv6 is optional. Ordinary containers are not supported for production deployment.
- Kernel support for IPsec/XFRM, ESP, nftables, and IPv6. IPv6 kernel support is required for the dual-stack tunnel even without a public IPv6 address. A running IKE daemon alone does not prove ESP data-plane support.
- An active Cloudflare DNS zone for the certificate domain, for example `vpn.example.com`. **The domain does not need to point to the VPS or have A/AAAA records.**
- Allow inbound **UDP 500 and UDP 4500** in the cloud security group and upstream firewall. For plain ESP without NAT-T, allow **IP protocol 50**, not UDP port 50. Preserve SSH access.
- Access to distribution package repositories, Cloudflare API, Let's Encrypt ACME API, and `api.ipify.org`. The system clock must be accurate.

The installer **does not create, modify, or delete A/AAAA/CNAME address records**. Cloudflare is used only for the `_acme-challenge` TXT records needed for certificate issuance and renewal. Clients connect to the automatically detected **VPS public IPv4 address**, while the certificate identity remains the **domain** you enter.

**Google Cloud and cloud-side NAT:** a NIC with only a private `10.x` address is normal. The public IP is not required to appear in `ip address`. strongSwan listens on local addresses and uses IKE NAT detection to enable UDP 4500 encapsulation. Configure an External IP for the GCE instance and allow inbound UDP 500/4500 to it; a static external IP is recommended. HTTPS egress detection does not establish inbound reachability. An instance with only Cloud NAT outbound access and no external IP or inbound port mapping cannot directly serve as a public VPN server. Shared NAT requires UDP 500/4500 forwarding to the instance. An HTTP proxy exit address may also differ from the inbound endpoint; check the server IP in the generated profiles.

The dedicated-server firewall defaults to dropping inbound traffic. It allows established traffic, loopback, DHCP maintenance, ICMP/ICMPv6, detected SSH TCP ports, IKE/NAT-T, and ESP. Web ports are not opened. The installer stops on unmanaged nftables/iptables-nft tables, active ufw/firewalld, legacy strongSwan, existing swanctl configurations, or certbot certificates for other domains. **Do not run it directly on a production machine with existing complex networking, firewalls, or container services.**

Installation automatically accepts the [Let's Encrypt terms of service](https://letsencrypt.org/repository/) and registers an ACME account without an email address to avoid another prompt. Read the terms before use.

## 2. Create a Cloudflare token

Create an API token scoped to the target domain zone:

| Scope | Permission |
| --- | --- |
| Zone | Zone: Read |
| Zone | DNS: Edit |
| Zone Resources | Include: Specific zone: the target zone |

Zone Read locates the domain zone. DNS Edit is used only to create and clean up `_acme-challenge` TXT records during issuance and renewal. A Global API Key is not required. The token must remain valid for future renewals. If it restricts source IPs, include the server egress address.

## 3. Deploy with exactly two prompts

Log in to the VPS as root. Download and run this single file:

```sh
curl -fL --proto '=https' --tlsv1.2 \
  https://raw.githubusercontent.com/ArcCal/letsencrypt_strongswan/main/install.sh \
  -o install.sh
bash install.sh
```

This is an **all-in-one installer**. Configuration files, certificate hooks, and helper code are embedded in it. Running it does not require Git, clone a repository, or fetch companion files from GitHub. It verifies and extracts its embedded payload into a private root temporary directory and removes that directory when finished, without overwriting neighbouring files. Package installation, Cloudflare calls, and certificate issuance still require network access.

You can also run the same single-file installer from a full checkout:

```sh
git clone https://github.com/ArcCal/letsencrypt_strongswan.git
cd letsencrypt_strongswan
bash install.sh
```

Install `git` with the distribution package manager first if using the checkout method. A regular user can enter a root shell with `sudo -i` before running these commands. System sudo authentication is separate from the installer prompts.

The installer asks only:

```text
VPN certificate domain (FQDN): vpn.example.com
Cloudflare API token (hidden input):
```

Use an FQDN without a scheme, port, path, or wildcard. International domains must use punycode. The token is hidden, never placed in command arguments, and never written into the account summary. Do not run with `bash -x` or record terminal input to shared logs. Invalid input or insufficient permissions cause an error and exit instead of another prompt.

The installer automatically:

1. Checks the system, concurrent installation, and conflicting services, then installs packages noninteractively.
2. Checks Cloudflare zone access and detects the public IPv4 client endpoint without changing address records.
3. Generates and persists two cryptographically random accounts and renders server configuration and Apple profiles.
4. Obtains an ECDSA P-256 certificate through certbot DNS-01, with a 60-second DNS propagation delay. Ports 80/443 are not required.
5. Deploys the leaf certificate, private key, and individually split intermediate certificates after checking expiration, hostname, and key matching.
6. Configures forwarding, IPv6 RA handling, dedicated nftables tables, boot services, and the certificate timer.
7. Starts strongSwan, loads accounts, certificates, connections, and pools, and checks service status.

Package installation and DNS propagation can take several minutes. Errors from networking, Cloudflare, the CA, or the firewall stop deployment with diagnostic output. A Cloudflare token cannot modify cloud-provider security groups or repair blocked upstream UDP traffic.

## 4. Random accounts and output files

Each username has **8 uppercase/lowercase letters and digits**, including at least one uppercase letter, one lowercase letter, and one digit. Each password has **22 characters**, including uppercase/lowercase letters, digits, and at least one symbol from `!@#%+-_=.:?`. This alphabet avoids configuration escaping problems. Generation uses Python `secrets`, backed by the operating system random source. The two usernames are distinct.

```sh
cat /tmp/usr.txt
ls -l /var/lib/ikev2-vpn/profiles/
```

| File | Contents |
| --- | --- |
| `/tmp/usr.txt` | Both random accounts, server endpoint, and profile paths |
| `/var/lib/ikev2-vpn/profiles/<username>.mobileconfig` | One iPhone profile per user, including that user's credentials |
| `/var/lib/ikev2-vpn/state.json` | Persistent account state and server IP; accounts are reused on rerun |
| `/etc/letsencrypt/cloudflare.ini` | Cloudflare token for certificate renewal |
| `/etc/swanctl/conf.d/ikev2-vpn.conf` | Generated connection, dual-stack pools, and both EAP accounts |

Credential files are root-owned with mode `0600`. Do not publish them in Git, on a web server, or to other users. Give each user only their own profile. Passwords are not printed to the terminal, and the installer does not start a public profile-download service.

`/tmp/usr.txt` may disappear after reboot or system cleanup. Accounts remain in `/var/lib/ikev2-vpn/state.json`. Regenerate the summary and profiles without entering the token:

```sh
python3 /usr/local/lib/ikev2-vpn/vpn.py --export
```

Rerunning the installer for the same domain still asks only for the domain and token. It preserves accounts, profile identifiers, and passwords, and can update the token. If the VPS public IP changes, rerun installation to detect the new IP and redistribute the profiles. Profiles do not follow domain DNS changes. Changing the domain is rejected to protect an existing deployment; plan a migration separately. Do not delete saved state as a repair step.

## 5. Install and connect on iPhone 18 Pro Max

Profiles use Apple's standard `com.apple.vpn.managed` **IKEv2** payload for modern iOS versions supporting these fields. The full-tunnel settings require iOS 14.2 or later. The iPhone 18 Pro Max uses the standard built-in VPN client; there is no model-specific profile format or third-party VPN app requirement. Physical-device validation remains necessary as described below.

1. Download the user's `.mobileconfig` through SCP/SFTP to a trusted computer. Transfer it to the corresponding iPhone using AirDrop, trusted device management, or another secure channel.
2. Open the file on iPhone. Install it in Settings > Profile Downloaded or Settings > General > VPN & Device Management. iOS requires device authentication and installation confirmation.
3. Enable the configuration in Settings > VPN. Server IP, remote domain identity, username, and password are already filled in. Domain address resolution is not required.
4. Check the public IP in a browser after connecting; it should show the VPS exit address. Check DNS and IPv6 routing as well.

The server cannot replace the user's profile-installation and VPN-enable actions. Profiles are **unsigned XML plists**, so iOS may label them unsigned. Profile signing and server certificate trust are separate. No CA, client certificate, MDM enrollment, or device supervision is installed.

`AuthenticationMethod=None` with `ExtendedAuthEnabled=1` selects EAP username/password authentication. `RemoteAddress` is the server public IP. `RemoteIdentifier` and `ServerCertificateCommonName` are the certificate domain. Public CA chain and server identity checks remain enabled; an IP SAN is not required. **Password-based client authentication does not disable server certificate verification.**

IKE and ESP use AES-256/SHA-256 with DH group 14, explicitly matched between the server and Apple profile. Profiles enable MOBIKE, PFS, and `IncludeAllNetworks`, disable local-network bypass, and request `0.0.0.0/0, ::/0` traffic selectors. Apple still excludes certain system traffic, such as DHCP, captive-network login, some cellular services, and companion-device communication. This is not a supervised Always On VPN. Traffic is not protected after the user disables the VPN.

Client pools are `10.10.10.0/24` and `fd10:10:10::/64`, using NAT44 and NAT66. Without a public IPv6 uplink, client IPv6 stays within the tunnel but cannot reach the IPv6 internet; it is not deliberately sent through the local network. Choose an IPv6-capable VPS if IPv6 internet access is required.

## 6. Certificate checks, renewal, and hot reload

The installer uses the production Let's Encrypt ACME service with certbot and Cloudflare **DNS-01**. CA, TLS, and package-signature verification remain enabled.

- Initial deployment issues a certificate. Existing certificates are reused or renewed according to certbot's expiration policy, without forcing issuance on every installation.
- `ikev2-vpn-renew.timer` runs twice daily, with up to one hour of randomized delay and persistent catch-up after missed runs.
- Each run checks expiration with OpenSSL. Missing or expired certificates trigger immediate issuance. Valid certificates go through `certbot renew`, allowing certbot to decide when renewal is due instead of hardcoding certificate lifetimes.
- Successful issuance invokes the deploy hook. The scheduled check also redeploys the current valid certificate to recover from a previous deployment failure.
- Before deployment, the hook checks expiration, domain, key matching, and intermediate certificate syntax. Files go into `/etc/swanctl/x509/`, `private/`, and `x509ca/`.
- A running daemon reloads credentials with `swanctl --load-creds --noprompt`, without restarting VPN sessions. Reload failures are returned as failures.
- During installation only, the hook deploys files without contacting VICI. The installer then repairs service settings, restarts strongSwan, and requires `swanctl --load-all` to succeed. This allows recovery from a crashed daemon with a stale socket. Scheduled renewal still requires a successful live credential reload and reports daemon/reload failures.
- Unrelated certificate lineages are ignored by the hook. The dedicated host must not already manage certbot certificates for other domains. After installation, the default `certbot.timer` is disabled in favour of the hardened renewal service, avoiding an additional unsandboxed timer entry point.

```sh
systemctl list-timers ikev2-vpn-renew.timer
systemctl start ikev2-vpn-renew.service
journalctl -u ikev2-vpn-renew.service -n 100 --no-pager
openssl x509 -in /etc/swanctl/x509/vpn-server.pem -noout -dates -subject
```

Keep the token valid. Unreachable APIs, expired token permissions, and DNS propagation failures can prevent renewal. Email-free ACME registration provides no CA email notifications, so monitor service failures. Avoid deleting certbot state or repeatedly forcing renewal; CA rate limits still apply.

## 7. Services, files, and troubleshooting

| Location | Purpose |
| --- | --- |
| `/usr/local/lib/ikev2-vpn/` | Installed state/configuration and certificate tools, independent of the checkout |
| `/etc/letsencrypt/renewal-hooks/deploy/ikev2-vpn` | Certificate deployment and credential hot reload |
| `/etc/ikev2-vpn/firewall.nft` | Firewall with detected SSH ports substituted |
| `/etc/sysctl.d/99-ikev2-vpn.conf` | IP forwarding and IPv6 RA settings |
| `strongswan.service` | IKEv2 daemon, loading connections, accounts, and certificates at startup |
| `ikev2-vpn-firewall.service` | The three managed nftables tables, started before strongSwan |
| `ikev2-vpn-renew.service/.timer` | Certificate checks, renewal, deployment, and reload |

```sh
systemctl status strongswan ikev2-vpn-firewall ikev2-vpn-renew.timer --no-pager
swanctl --list-conns
swanctl --list-pools
swanctl --list-certs
swanctl --list-sas
journalctl -u strongswan -n 100 --no-pager
nft list ruleset
```

| Symptom | Check |
| --- | --- |
| Cloudflare 401/403 or zone not found | Zone Read and DNS Edit permissions for the correct active zone |
| DNS-01 validation fails | Authoritative Cloudflare nameservers, DNSSEC/CAA, token validity; increase the 60-second propagation wait if needed |
| Certificate succeeds but iPhone cannot connect | Correct server IP in the profile and inbound UDP 500/4500; A/AAAA changes are not required |
| Authentication fails | The correct user's current profile, persistent account state, and strongSwan logs |
| Connected without internet | Forwarding, default routes, firewall, CHILD_SA, and upstream network; IKE_SA alone does not establish a working data plane |
| Large pages or network switching stall | MTU/MSS and blocked UDP; MSS clamping precedes accepting firewall rules |
| SSH is blocked | Recover through the cloud console; check SSH ports in the managed firewall, validate with `nft -c -f`, then restart its service |
| IPv6 default route disappears after reboot | Actual interface `accept_ra=2`, provider configuration, and the IPv6 default route |

There is no automatic uninstall. Reverse firewall and forwarding changes according to the server's networking requirements. Back up account state, the token, ACME data, and profiles securely with encryption.

## 8. File permissions and systemd hardening

Credentials are not passed in command arguments. Tokens, account state, summaries, private keys, and profiles are root-owned with mode `0600`; persistent account and profile directories use `0700`. The server installs distribution packages rather than executing third-party remote installation scripts.

`config/strongswan-hardening.conf` and `config/renew-hardening.conf` are installed as systemd drop-ins:

| Restriction | strongSwan | Certificate checks and renewal |
| --- | --- | --- |
| Privilege escalation | NoNewPrivileges; only NET_ADMIN, NET_BIND_SERVICE, NET_RAW capabilities | NoNewPrivileges; empty capability set |
| Writes | Read-only system, writable `/run` | Certificate/credential directories, certbot work/log directories, and the certificate lock file only |
| Sensitive files | Cloudflare/ACME data, persistent account backups, and profiles hidden | Additional account backups and profiles hidden |
| System control | systemd private control socket and system D-Bus hidden | Same; the hook reloads directly through VICI |
| Devices and kernel | Private devices, protected tunables/modules/control groups/kernel logs | Same |
| Other | Private temporary directory; protected home directories; restricted address families, namespaces, setuid, and realtime scheduling; no writable executable memory | Similar restrictions, with memory behaviour needed by certbot/Python allowed |

Before allowing client forwarding, the firewall checks the IPsec decryption marker rather than trusting only source IPs. VPN clients cannot access host services such as SSH, other VPN users, common private/link-local networks, or cloud metadata endpoints. This version is not intended for corporate private-network access.

These measures limit some consequences of a service compromise; they do not contain a host-root or kernel compromise. strongSwan needs `CAP_NET_ADMIN` to install IPsec policies, which carries network-management risk. Renewal needs access to the token and private key. Host-root or kernel control can bypass restrictions and expose credentials. Scope the Cloudflare token to the required zone to limit DNS impact if it leaks.

Check accepted and effective settings on the actual VPS:

```sh
systemctl cat strongswan ikev2-vpn-renew
systemctl show strongswan -p NoNewPrivileges -p CapabilityBoundingSet -p ProtectSystem
systemd-analyze security strongswan.service ikev2-vpn-renew.service
```

## 9. Development and validation limits

`install.sh` is the distributable single-file artifact. Reviewable sources remain in the repository: `scripts/install-body.sh` contains installation logic, while `scripts/vpn.py`, `scripts/renew.sh`, and `config/` supply embedded dependencies. After editing them, regenerate the installer:

```sh
python3 scripts/build-installer.py
```

Generation is offline. Tests verify the embedded checksum, exact source correspondence, and reproducible output. The embedded SHA-256 detects corruption, not authenticity; download through trusted HTTPS.

`build-installer.py` only packages this repository's scripts and configuration. It does not compile strongSwan or install a compiler toolchain. Production installation always obtains strongSwan through `apt-get install` from the configured distribution repositories. If strongSwan startup fails, the installer prints its service status and recent journal entries automatically.

Unit tests use the Python standard library without contacting Cloudflare or ACME:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Run `tests/integration.sh` **only as root in a disposable container**. It writes to `/etc`, generates test CA/certificate fixtures, starts real charon, and applies firewall rules within the container network namespace. It requires the documented Debian packages plus `shellcheck` and the NET_ADMIN capability. It checks accounts and profiles, standalone extraction, repeatability, key mismatch rejection, hot reload, expired-certificate handling, and repeated firewall application. Renewal uses a mocked certbot command, not public CA issuance.

Build and run the supplied development image from the repository root. CA trust is mounted during the build rather than retained as a proxy-specific image layer; TLS and apt signature verification remain enabled.

```sh
docker build -f tests/Dockerfile \
  --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -t ikev2-vpn-test:local .
docker run --rm --network none --cap-drop ALL \
  --cap-add NET_ADMIN --cap-add NET_BIND_SERVICE --cap-add NET_RAW \
  --security-opt no-new-privileges \
  ikev2-vpn-test:local bash /repo/tests/integration.sh
```

Tests include static validation of systemd units/drop-ins and real daemon startup with reduced capabilities. The container does not run full systemd, so its sandbox and boot behaviour are not fully verified. Validation does not cover a real VPS installation/reboot, live Cloudflare writes and Let's Encrypt issuance, physical iPhone 18 Pro Max installation/connection, or end-to-end internet ESP/NAT traffic. Profiles follow the [official Apple device-management schema](https://github.com/apple/device-management/blob/release/mdm/profiles/com.apple.vpn.managed.yaml); parsing a plist does not establish physical-device compatibility.

The daemon test also enforces the configured socket address families using libseccomp. Distribution plugins need `AF_PACKET` for ARP/packet sockets and `AF_ALG` for kernel cryptography; both are allowed. This does not emulate all systemd sandbox settings.

## 10. Project history

The project began as an IKEv2 VPN tutorial in 2016, using source-built strongSwan, `ipsec.conf`, [acme.sh](https://github.com/acmesh-official/acme.sh) standalone issuance, and iptables. In 2026 it moved to distribution packages, swanctl, certbot Cloudflare DNS-01, and nftables.

This version adds two-input automated deployment, two random EAP accounts, Apple profiles, repeatable installation, and certificate lifecycle management while retaining IKEv2, public CA server certificates, password-based client authentication, and a full-tunnel VPN. Legacy account/client files in `config/` are manual references only. Automated deployment uses `scripts/vpn.py` and never loads example passwords.
