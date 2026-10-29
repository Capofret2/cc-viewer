# 添加电脑（可选）

让 cc-viewer 里的 Claude 连到用户自己的电脑（Windows、Mac、Linux）上干活。用户在电脑上粘贴运行一行命令，电脑就会装好 SSH 服务，
主动挂一条反向通路回到运行 cc-viewer 的这台服务器，开机自动重连。装好以后，网页的「设备」页里就能看到这台电脑，
会话里的 Claude 经 ssh 门卫连过去。

## 原理

1. 网页的「设备」页（或者会话里的 Claude）运行 `make-pc-setup.sh`，生成一个 60 分钟内有效的随机链接和两条命令（Windows 一条、Mac/Linux 一条）。
2. 用户在电脑上运行命令：从 `BASE/pc-setup/<链接>` 下载安装脚本，装好 SSH 服务，把门卫的公钥加进电脑的 `authorized_keys`，
   再用一把一次性的报到钥匙连回服务器报到（`pc-enroll.py`），分到一个端口（从 23001 起）。
3. 电脑用自己生成的隧道钥匙，挂一条只能开在这个端口上的反向通路（服务器上的 `authorized_keys` 限死了，连上时只能执行 `pc-hello.sh`）。
4. 查看器看到端口通了，就把这台电脑登记进门卫；之后 Claude 用 `ssh <电脑名>` 经门卫连过去，钥匙只在门卫手里。

服务器的 SSH 电脑能直接连到时不用中转；服务器在内网时，电脑经一台有公网地址的中转服务器连回来，中转账号只许转发到服务器的 SSH。

## 要准备的东西

1. 查看器开着 ssh 门卫：`instance.json` 写了 `gw_socket`（见 `instance.py`），设备页才有「添加设备」。
2. 一个电脑访问得到的 HTTPS 查看器地址（下面叫 `BASE`）。电脑要从这里下载安装脚本、报告安装进度。`/pc-setup/` 这一路不能要网页登录
   （链接名是 128 位随机数，60 分钟过期），其余路径照常要登录。只有隔离的本机测试才使用 HTTP。
3. 电脑连得到服务器的 SSH：
   - 服务器有公网 SSH：`TARGET_HOST`、`TARGET_PORT` 写它的公网地址和端口，不用中转。
   - 服务器在内网：准备一台有公网地址的中转服务器（见下一节）。
4. 服务器的 sshd 允许端口转发（`AllowTcpForwarding` 默认就是 yes），OpenSSH 7.8 以上（要用 `permitlisten`）。

## 中转服务器怎么准备

1. 在中转服务器上建一个专门的账号（比如 `pcrelay`），不设密码，不给 sudo。
2. 让服务器把自己的 SSH 端口挂到中转服务器上，例如在服务器上用 systemd 或 autossh 一直保持：
   ```bash
   ssh -N -R 127.0.0.1:2222:localhost:22 <你在中转服务器上的账号>@<中转服务器>
   ```
   这时 `TARGET_HOST=127.0.0.1`、`TARGET_PORT=2222`（从中转服务器看过来的地址）。
3. 为 `pcrelay` 限制 SSH 权限，在中转服务器的 `sshd_config` 末尾加上（地址、端口按上一步实际填写）：
   ```text
   Match User pcrelay
       AllowTcpForwarding local
       PermitOpen 127.0.0.1:2222
       PermitTTY no
       X11Forwarding no
       AllowAgentForwarding no
       PermitTunnel no
       ForceCommand /bin/false
   ```
   由服务器管理者运行 `sshd -t` 检查配置，再按系统的方式重载 sshd。这里允许的是电脑到目标 SSH 的转发，禁止中转账号开反向端口或执行命令。
   上一步从查看器服务器发起的反向通路要用另一个账号，不能用这个受限的 `pcrelay`。
4. 运行下面的 `install.sh` 以后，把它打印的那一行加进 `pcrelay` 的 `~/.ssh/authorized_keys`。
5. `BASE` 也可以放在中转服务器上：比如用 nginx 把 `https://<中转服务器>/pc-setup/` 反向代理到查看器。

安装链接包含报到凭据，拿到链接的人能在有效期内登记电脑。安装还会添加一把普通登录公钥，让用户在电脑上用 `ssh cc`
登录运行 Claude 的系统账号。只向可信用户提供链接；这个功能不会为每个人建立独立的系统账号。不要把安装链接放进公开页面或仓库。

## 安装

在查看器的代码目录里，以运行 Claude 的那个用户运行：

```bash
# 有中转
BASE=https://relay.example.com RELAY_HOST=relay.example.com RELAY_USER=pcrelay TARGET_HOST=127.0.0.1 TARGET_PORT=2222 \
  bash pc-tunnel/install.sh
# 没有中转
BASE=https://viewer.example.com TARGET_HOST=viewer.example.com TARGET_PORT=22 bash pc-tunnel/install.sh
```

它把脚本装进 `~/tunnels/`，配置写在 `~/tunnels/pc-tunnel.conf`，生成主机公钥和中转用的钥匙，下载 Windows 用的 OpenSSH 安装包。
重复运行没关系，没给的项沿用上一次的。每一项的意思写在 `install.sh` 开头。

## 用户的电脑上会多出什么

- Mac/Linux：`~/.ssh/ccviewer/`（钥匙和隧道脚本）、一个开机自启的用户服务 `ccviewer-tunnel`（Mac 是 LaunchAgent），
  `~/.ssh/config` 开头一段「`ssh cc`」直达服务器的配置（改之前会检查，原文件留一份 `config.before-ccviewer`）。
- Windows：`C:\ProgramData\ccviewer`、计划任务 `ccviewer-tunnel`、微软的 OpenSSH 服务，以及 `~\.ssh\config` 里同样的一段。

## 卸载

在设备页删除电脑时，电脑在线就会顺手卸掉；不在线时，设备页会给出在那台电脑上运行的卸载命令（`BASE/pc-setup/uninstall.sh`、`uninstall.ps1`）。
SSH 服务本身不会卸。

## 说明

当前开源版通过了直连和中转配置、安装脚本生成、模拟电脑报到、重复登记、链接过期和吊销的沙盒检查。
尚未在真实 Windows、Mac、Linux 电脑上完成安装验证，Windows 脚本也尚未运行 PowerShell 语法检查；中转账号的 SSH 权限限制尚未做双端集成测试。
部署前请按自己的系统和网络试装，遇到问题欢迎提 issue。
