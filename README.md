# Let's Encrypt + strongSwan:IKEv2 VPN 手动部署教程

[![letsencrypt](res/images/letsencrypt-logo-horizontal.png)](https://letsencrypt.org/) ![plus](res/images/add-symbolic.symbolic.png) [![strongswan](res/images/strongswan.png)](https://strongswan.org/)

在 **Debian 13 (trixie) x64** 或 **Ubuntu 26.04 LTS x64** 上,**全部使用发行版自带软件包**(不编译源码),手动搭建一个 IKEv2 VPN:

| 项目 | 选择 |
|---|---|
| VPN 软件 | strongSwan 6.x(`charon-systemd` + `swanctl`,即新式 `swanctl.conf` 配置) |
| 服务器认证 | Let's Encrypt 证书(certbot,Cloudflare **DNS-01** 验证) |
| 客户端认证 | EAP-MSCHAPv2(用户名 + 密码) |
| 客户端 | iOS / macOS / Windows / Android / Linux 的系统内置或 strongSwan 客户端,无需额外证书 |
| 防火墙 | nftables |

> **与旧版教程的区别**:旧版用源码编译 strongSwan 5.x、`ipsec.conf` / `ipsec start`(`starter`)、`acme.sh` + `--standalone`(要占用 80 端口)、iptables。
> 新版用 apt 安装、`swanctl.conf` + systemd、certbot + Cloudflare DNS(不需要开放 80/443 端口,也支持泛域名)、nftables。

## 目录

1. [准备工作](#1-准备工作)
2. [创建 Cloudflare API Token](#2-创建-cloudflare-api-token)
3. [安装软件包](#3-安装软件包)
4. [用 certbot 申请证书](#4-用-certbot-申请证书)
5. [配置 strongSwan](#5-配置-strongswan)
6. [内核参数与防火墙](#6-内核参数与防火墙)
7. [启动与验证](#7-启动与验证)
8. [客户端配置](#8-客户端配置)
9. [日常维护](#9-日常维护)
10. [故障排查](#10-故障排查)
11. [本教程的验证范围](#11-本教程的验证范围)

仓库中的 `config/` 目录是教程里用到的全部配置文件,可直接复制:

| 文件 | 部署位置 |
|---|---|
| [`config/cloudflare.ini.example`](config/cloudflare.ini.example) | `/etc/letsencrypt/cloudflare.ini` |
| [`config/certbot-deploy-hook.sh`](config/certbot-deploy-hook.sh) | `/etc/letsencrypt/renewal-hooks/deploy/strongswan.sh` |
| [`config/ikev2-eap.conf`](config/ikev2-eap.conf) | `/etc/swanctl/conf.d/ikev2-eap.conf` |
| [`config/secrets.conf.example`](config/secrets.conf.example) | `/etc/swanctl/conf.d/secrets.conf` |
| [`config/99-vpn-sysctl.conf`](config/99-vpn-sysctl.conf) | `/etc/sysctl.d/99-vpn.conf` |
| [`config/nftables-vpn.conf`](config/nftables-vpn.conf) | `/etc/nftables.conf` |
| [`config/client-linux.conf`](config/client-linux.conf) | Linux 客户端的 `/etc/swanctl/conf.d/vpn-client.conf` |

---

## 1. 准备工作

1. **一台 VPS**:Debian 13 或 Ubuntu 26.04 LTS,x86-64,有公网 IPv4(可选 IPv6)。下文命令都以 root 执行(普通用户请加 `sudo -i`)。
2. **一个域名,且 DNS 托管在 Cloudflare**,例如 `vpn.example.com`。
   - 添加一条 **A 记录**(有 IPv6 再加 **AAAA**)指向 VPS 公网 IP。
   - **必须是"仅 DNS / DNS only"(灰色云朵)**,不能开 Cloudflare 代理(橙色云朵),代理不转发 IKE 的 UDP 流量。
3. **放行端口**:云厂商的安全组 / 防火墙里放行入站 **UDP 500** 和 **UDP 4500**(以及你的 SSH 端口)。
   旧版教程里的 UDP 50/51 不是 UDP 端口,而是 ESP/AH 协议号;现代客户端基本都在 NAT 后,ESP 会封装进 UDP 4500,所以云安全组只需要 UDP 500 / 4500。
4. 系统时间要准确(证书有效期、IKE 都依赖它):

```sh
timedatectl status          # 看 "System clock synchronized: yes"
```

下文约定:域名 `vpn.example.com`,IPv4 客户端地址池 `10.10.10.0/24`,IPv6 地址池 `fd10:10:10::/64`。请全程替换成你自己的值。

## 2. 创建 Cloudflare API Token

certbot 通过 Cloudflare API 在 `_acme-challenge.vpn.example.com` 下临时写入 TXT 记录来证明域名所有权。

1. 登录 Cloudflare → 右上角头像 → **My Profile** → **API Tokens** → **Create Token**。
2. 选 **Edit zone DNS** 模板,或自定义:
   - 权限:**Zone → DNS → Edit**
   - Zone Resources:**Include → Specific zone → 你的域名**(如 `example.com`)
3. 创建后复制 token(只显示一次)。

> certbot-dns-cloudflare 的官方文档推荐使用这种**限定范围的 API Token**;"Global API Key"(账号级别全权限)仍被支持,但**不推荐**,泄露的后果是整个账号被控制。如果你只有 Global API Key,见下一节的写法。

## 3. 安装软件包

两个发行版的命令相同:

```sh
apt update
apt install -y \
    strongswan-swanctl charon-systemd \
    libcharon-extra-plugins libcharon-extauth-plugins libstrongswan-extra-plugins \
    nftables \
    certbot python3-certbot-dns-cloudflare
```

各包作用:

| 包 | 作用 |
|---|---|
| `charon-systemd` | strongSwan 的 IKE 守护进程,提供 systemd 服务 `strongswan.service` |
| `strongswan-swanctl` | `swanctl` 命令与 `/etc/swanctl/` 目录 |
| `libcharon-extauth-plugins` | **EAP-MSCHAPv2** 插件(本教程的客户端认证方式) |
| `libcharon-extra-plugins` | EAP-Identity 等插件 |
| `libstrongswan-extra-plugins` | ChaCha20-Poly1305 等算法 |
| `certbot` + `python3-certbot-dns-cloudflare` | ACME 客户端及 Cloudflare DNS 插件 |

> **不要安装 `strongswan` 元包。** 它会拉进旧式的 `strongswan-starter`(`ipsec.conf` / `ipsec` 命令),与 `charon-systemd` 二选一,两者同时存在会冲突。

确认只有 `charon-systemd` 这一套:

```sh
dpkg -l | grep -E 'strongswan|charon'
systemctl cat strongswan.service | head -3
```

> Ubuntu 的 certbot 官方文档推荐 snap 安装;本教程按你的要求使用 apt 版本。apt 版自带 `certbot.timer`,会自动续期。

## 4. 用 certbot 申请证书

### 4.1 保存 Cloudflare 凭据

```sh
install -m 600 /dev/null /etc/letsencrypt/cloudflare.ini
nano /etc/letsencrypt/cloudflare.ini
```

内容(参考 [`config/cloudflare.ini.example`](config/cloudflare.ini.example)):

```ini
# API Token(推荐)
dns_cloudflare_api_token = 你的token
```

只有 Global API Key 时改成(token 与下面两行**不能同时出现**,certbot 会报错):

```ini
dns_cloudflare_email = 你的Cloudflare登录邮箱
dns_cloudflare_api_key = 你的GlobalAPIKey
```

权限必须是 600,否则 certbot 会警告 "Unsafe permissions on credentials configuration file":

```sh
chmod 600 /etc/letsencrypt/cloudflare.ini
ls -l /etc/letsencrypt/cloudflare.ini
```

### 4.2 安装部署钩子(deploy hook)

strongSwan 不读 `/etc/letsencrypt/live/` 下的文件,要把证书装到 `/etc/swanctl/` 里。我们用 certbot 的 **deploy hook**:每次**签发或续期成功**后自动执行。

```sh
install -m 755 config/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
```

(没有克隆本仓库的话,把 [`config/certbot-deploy-hook.sh`](config/certbot-deploy-hook.sh) 内容手动贴进去即可。)

脚本做的事:

- `cert.pem`(叶子证书)→ `/etc/swanctl/x509/vpn-server.pem`
- `privkey.pem` → `/etc/swanctl/private/vpn-server.key`(权限 600)
- `chain.pem`(中间证书)按张拆开 → `/etc/swanctl/x509ca/letsencrypt-chain-N.pem`。strongSwan 每个文件只读第一张证书,所以不能直接用 `fullchain.pem`;服务器握手时需要把中间证书一并发给客户端,否则部分客户端会报"证书不受信任"。
- 执行 `swanctl --load-creds` 热加载证书,**不会断开已连接的客户端**。

### 4.3 先干跑一次

```sh
certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d vpn.example.com \
    --key-type ecdsa \
    --dry-run
```

`--dry-run` 走 Let's Encrypt 的 staging 环境,不计入正式的频率限制,也不会触发部署钩子。首次运行 certbot 会要求填写邮箱并同意服务条款。

### 4.4 正式申请

去掉 `--dry-run`:

```sh
certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
    --dns-cloudflare-propagation-seconds 30 \
    -d vpn.example.com \
    --key-type ecdsa
```

- `--key-type ecdsa`:ECDSA 证书(体积小、握手快),所有主流 IKEv2 客户端均支持。需要 RSA 就改成 `--key-type rsa`,其余步骤不变。
- 要泛域名(`*.example.com`),再加 `-d '*.example.com'`;DNS-01 是签发泛域名的唯一方式。

成功后部署钩子会自动运行(此时 strongSwan 还没启动,钩子里的 `swanctl --load-creds` 会失败,脚本已经忽略这个失败)。检查:

```sh
ls -l /etc/swanctl/x509 /etc/swanctl/x509ca /etc/swanctl/private
```

应该能看到 `vpn-server.pem`、`letsencrypt-chain-1.pem` 和 `vpn-server.key`。

### 4.5 确认自动续期

```sh
systemctl list-timers certbot.timer     # Debian/Ubuntu 的 apt 版 certbot 默认启用
certbot renew --dry-run                 # 模拟续期,验证凭据与流程可用
```

证书有效期 90 天,certbot 在剩余 30 天内才会真正续期;续期成功后钩子会再次把新证书装入 strongSwan。

## 5. 配置 strongSwan

`swanctl.conf` 采用 `/etc/swanctl/swanctl.conf` 末尾自带的 `include conf.d/*.conf`,我们把配置放进 `conf.d/`,不改主文件。

### 5.1 连接与地址池

```sh
install -m 644 config/ikev2-eap.conf /etc/swanctl/conf.d/ikev2-eap.conf
sed -i 's/vpn\.example\.com/你的域名/' /etc/swanctl/conf.d/ikev2-eap.conf
```

要点(详见文件内注释):

- `local { auth = pubkey; certs = vpn-server.pem; id = 你的域名 }`:服务器用证书证明自己,`id` 必须与证书里的域名(SAN)一致,客户端据此校验。
- `remote { auth = eap-mschapv2; eap_id = %any }`:客户端用用户名密码登录。
- `pools = vpn4, vpn6`:从地址池给客户端分配虚拟 IP 与 DNS。
- `local_ts = 0.0.0.0/0, ::/0`:所有流量都走 VPN。
- `esp_proposals` / `proposals` 不写,使用 strongSwan 的**默认安全套件**,由 strongSwan 随版本维护,比手写长长的 `ike=` / `esp=` 清单更稳妥。

> **没有 IPv6?** 删掉 `ikev2-eap.conf` 里的 `vpn6` 池、`pools` 行里的 `, vpn6`、`local_ts` 里的 `, ::/0`,以及 [`nftables-vpn.conf`](config/nftables-vpn.conf) 中 IPv6 相关的几行。

### 5.2 用户账号

```sh
install -m 600 config/secrets.conf.example /etc/swanctl/conf.d/secrets.conf
nano /etc/swanctl/conf.d/secrets.conf
```

```
secrets {
    eap-alice {
        id = alice
        secret = "一个长而随机的密码"
    }
}
```

- 段名(`eap-alice`)只是标识,**`id` 才是客户端填写的用户名**。
- 密码用 `openssl rand -base64 18` 生成;文件含明文密码,务必保持 600 且 root 所有。

## 6. 内核参数与防火墙

### 6.1 开启转发

```sh
install -m 644 config/99-vpn-sysctl.conf /etc/sysctl.d/99-vpn.conf
sysctl --system
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
```

**IPv6 注意**:开启 IPv6 转发后,Linux 默认会忽略路由通告(RA),VPS 可能因此丢掉 IPv6 默认路由。配置里已设置 `accept_ra = 2`;但 `accept_ra` 是按网卡生效的,请对你的实际网卡核对:

```sh
ip -6 route show default                 # 应该仍有默认路由
ip -br link                              # 找出网卡名,如 eth0 / ens3
sysctl net.ipv6.conf.eth0.accept_ra      # 应为 2;不是的话手动设置并写进 /etc/sysctl.d/99-vpn.conf
```

只用 IPv4 的话可以删掉 IPv6 那几行。

### 6.2 防火墙(nftables)

需要三件事:放行 UDP 500/4500、允许转发 VPN 池的流量、对出网流量做 NAT(伪装)。

**场景 A:全新 VPS,没有任何防火墙规则** —— 直接用仓库里的完整规则:

```sh
# 先编辑,确认 SSH 端口(默认 22)正确,否则会把自己锁在外面!
nano config/nftables-vpn.conf
nft -c -f config/nftables-vpn.conf       # 只检查语法,不生效
install -m 644 config/nftables-vpn.conf /etc/nftables.conf
systemctl enable --now nftables
nft list ruleset
```

> 先在云厂商控制台准备好 VNC / 控制台入口再操作,以防规则写错断连。`nftables.service` 开机会加载 `/etc/nftables.conf`。

**场景 B:已经有防火墙(ufw / 自有 nftables / iptables)** —— 不要覆盖,只合并:

- 放行入站 UDP 500、UDP 4500;
- 放行来自 `10.10.10.0/24`、`fd10:10:10::/64` 的 forward;
- 加入 `config/nftables-vpn.conf` 最后两个 `table ... vpn_nat` 的 NAT 规则(或等价的 iptables `-t nat ... MASQUERADE`);
- 使用 ufw 的话,还需要在 `/etc/default/ufw` 把 `DEFAULT_FORWARD_POLICY` 改为 `ACCEPT`,并把 NAT 规则写进 `/etc/ufw/before.rules`。

NAT 规则不绑定网卡名(只匹配来源地址池),所以网卡叫什么都不用改。

## 7. 启动与验证

```sh
systemctl enable --now strongswan
systemctl status strongswan --no-pager
```

`strongswan.service` 启动后会自动执行 `swanctl --load-all`,把 `conf.d/` 里的连接、地址池、证书、密码全部加载。

检查加载结果:

```sh
swanctl --list-certs        # 应看到 vpn-server.pem(subject 为你的域名)和中间证书
swanctl --list-conns        # 应看到 ikev2-eap
swanctl --list-pools        # 应看到 vpn4 / vpn6
journalctl -u strongswan -n 50 --no-pager
```

手动重新加载配置(改完 `conf.d` 后):

```sh
swanctl --load-all
```

客户端连上以后:

```sh
swanctl --list-sas          # 已建立的连接,含用户名和分到的虚拟 IP
```

## 8. 客户端配置

所有客户端通用的参数:

| 项 | 值 |
|---|---|
| 服务器 / 服务器地址 | `vpn.example.com`(填域名,不要填 IP,否则证书校验失败) |
| 远程 ID / Remote ID | `vpn.example.com`(与服务器 `id` 一致) |
| 认证方式 | 用户名 + 密码(EAP / IKEv2 EAP) |
| 用户名 / 密码 | 第 5.2 步里的 `id` / `secret` |
| 本地 ID | 留空 |

因为证书由 Let's Encrypt 签发,系统已信任其根证书,**客户端无需导入任何证书**。

- **iOS / iPadOS / macOS**:设置 → VPN → 添加 VPN 配置 → 类型 **IKEv2**;填服务器、远程 ID,用户鉴定选"用户名",填用户名密码。详见 [strongSwan 的 Apple 客户端说明](https://wiki.strongswan.org/projects/strongswan/wiki/AppleClients)。
- **Windows 10 / 11**:设置 → 网络和 Internet → VPN → 添加 VPN;VPN 提供商选"Windows(内置)",VPN 类型选 **IKEv2**,登录信息类型选"用户名和密码"。详见 [strongSwan 的 Windows 说明](https://wiki.strongswan.org/projects/strongswan/wiki/Windows7#C)。
  - 若连接时报"IKE 身份验证凭证不可接受 / 策略不匹配",多为内置客户端的默认加密参数(旧版 Windows 默认 DH 组较弱)与服务器的默认套件不匹配,可在 PowerShell 里用 `Set-VpnConnectionIPsecConfiguration` 指定 `AES256`、`SHA256`、`Group14`/`ECP256` 等现代参数,不建议为此在服务器端启用弱算法。
- **Android**:安装 Google Play 上的 **strongSwan VPN Client**,新建配置,类型选 **IKEv2 EAP (用户名/密码)**,填服务器域名、用户名、密码;CA 证书保持"自动选择"。详见 [Android 客户端说明](https://wiki.strongswan.org/projects/strongswan/wiki/AndroidVPNClient)。
- **Linux**:同样的包 + [`config/client-linux.conf`](config/client-linux.conf):

```sh
apt install -y strongswan-swanctl charon-systemd libcharon-extra-plugins libcharon-extauth-plugins
# 让 strongSwan 信任 Let's Encrypt 根证书(系统证书库里已有)
cp /etc/ssl/certs/ISRG_Root_X1.pem /etc/ssl/certs/ISRG_Root_X2.pem /etc/swanctl/x509ca/
install -m 600 config/client-linux.conf /etc/swanctl/conf.d/vpn-client.conf
nano /etc/swanctl/conf.d/vpn-client.conf          # 改域名、用户名、密码
systemctl restart strongswan
swanctl --initiate --child net                    # 拨号
swanctl --list-sas
swanctl --terminate --ike vpn                     # 断开
```

  Linux 客户端若遇到大包卡顿(MTU),可在 `/etc/strongswan.d/charon/kernel-netlink.conf` 中设置 `mtu = 1280`。

连通后在客户端访问 <https://ifconfig.me> 之类的网站,看到的应是 VPS 的 IP。

## 9. 日常维护

**添加 / 删除用户**:编辑 `/etc/swanctl/conf.d/secrets.conf`,然后

```sh
swanctl --load-creds
```

已在线的用户不会被断开;要踢人用 `swanctl --terminate --ike ikev2-eap --ike-id <编号>`(编号见 `--list-sas`)。同一账号默认可以多设备同时在线。

**证书续期**:全自动。排查时看 `journalctl -u certbot` 或 `/var/log/letsencrypt/letsencrypt.log`,强制测试整条链路:

```sh
certbot renew --force-renewal --deploy-hook /etc/letsencrypt/renewal-hooks/deploy/strongswan.sh
```

(注意正式环境短期内反复强制续期会触碰 Let's Encrypt 的频率限制,日常用 `certbot renew --dry-run` 即可。)

**升级**:`apt update && apt upgrade`,strongSwan 的安全更新会随发行版发布。升级后 `systemctl status strongswan` 确认服务正常。

**日志级别**:需要更详细的 IKE 日志时,编辑 `/etc/strongswan.d/charon-systemd.conf`,在 `journal { }` 段里设置级别,然后 `systemctl restart strongswan`;调试完记得改回去。

```
charon-systemd {
    journal {
        default = 1
        ike = 2
    }
}
```

## 10. 故障排查

| 现象 | 排查 |
|---|---|
| certbot 报 `Unsafe permissions` | `chmod 600 /etc/letsencrypt/cloudflare.ini` |
| certbot 报 Cloudflare 认证 / 找不到 zone | Token 权限需包含该域名 zone 的 **DNS:Edit**;token 与 email/api_key 不能混写 |
| certbot 验证超时 | 提高 `--dns-cloudflare-propagation-seconds`(如 60) |
| `swanctl: connecting to 'unix:///var/run/charon.vici' failed` | 守护进程没运行:`systemctl status strongswan` |
| `loading EAP_MSCHAPV2 method failed` / 日志里找不到 eap-mschapv2 | 没装 `libcharon-extauth-plugins` |
| 日志有 `no EAP key found` / 登录失败 | `secrets.conf` 里的 `id` 与客户端用户名不一致,或没执行 `swanctl --load-creds` |
| 客户端提示证书无效 / 服务器身份无法验证 | 客户端填的必须是域名,不是 IP;确认 `swanctl --list-certs` 里有中间证书;核对服务器时间 |
| 客户端连不上、服务端日志里完全没有请求 | 云安全组 / 防火墙未放行 UDP 500、4500;域名解析到的不是这台机器;Cloudflare 记录被设成了"代理" |
| 连上了但上不了网 | `sysctl net.ipv4.ip_forward` 是否为 1;`nft list ruleset` 里 NAT 与 forward 规则是否存在 |
| IPv6 不通 / 服务器自己的 IPv6 丢了 | 见 6.1:检查 `accept_ra` 与默认路由;或直接去掉 IPv6 池 |
| 大页面打不开、网速异常慢 | MTU/MSS 问题:确认 nftables 里的 MSS 钳制规则存在;Linux 客户端设置 `mtu = 1280` |
| Ubuntu 下加载证书被拒 | `dmesg \| grep -i apparmor`;`charon-systemd` / `swanctl` 自带 AppArmor 策略,允许读 `/etc/swanctl/`,自行改了路径就要同步调整 |

## 11. 本教程的验证范围

诚实说明,方便你判断哪些地方需要在自己的机器上再确认:

**已实际验证**(在一台 Ubuntu 24.04 环境中,strongSwan 5.9.13、certbot 5.8 + certbot-dns-cloudflare):

- 包名与拆分:`eap-mschapv2` 在 `libcharon-extauth-plugins`,`eap-identity` 在 `libcharon-extra-plugins`;`strongswan.service` 来自 `charon-systemd`,`ExecStartPost` 自动 `swanctl --load-all`;`swanctl.conf` 自带 `include conf.d/*.conf`。
- `config/` 中的 `ikev2-eap.conf`、`secrets.conf.example`、部署钩子被实际加载:连接、双地址池、证书、私钥、EAP 密码全部加载成功。
- 用测试 CA(根 → 中间 → ECDSA 叶子)走通完整 IKE 握手:客户端校验了服务器证书链,**EAP-MSCHAPv2 认证成功**,服务器分配了 `10.10.10.x` / `fd10:10:10::x` 虚拟 IP 与 DNS。
- 部署钩子在 certbot 的 `RENEWED_LINEAGE` 约定下,正确拆分并安装了叶子证书、私钥与中间证书。
- `nftables-vpn.conf` 通过 `nft -c` 语法检查;certbot-dns-cloudflare 的凭据格式、命令行参数名取自插件自带文档。

**未能验证**(测试环境限制,请部署后留意):

- Debian 13 与 Ubuntu 26.04 的实机安装:只能从包索引与公开信息确认 Ubuntu 26.04 提供 strongSwan 6.0.x、两系统包名与拆分一致,未实际安装运行。strongSwan 6 与 5.9 的 `swanctl.conf` 语法本教程用到的部分相同,但请以实机的 `swanctl --load-all` 输出为准。
- 真实的 Let's Encrypt 签发(需要你的 Cloudflare 账号)。
- ESP 数据平面、转发、NAT、nftables 实际生效:测试环境的内核没有 ESP 支持,CHILD_SA 无法建立。
- 各平台客户端的实际连接界面,文字描述基于通用做法,界面名称可能随系统版本变化。
- strongSwan 官方文档站(docs.strongswan.org)与 certbot 在线文档在本次环境中无法访问,上述配置依据发行版随包带的配置模板、插件自带文档以及实测行为。
