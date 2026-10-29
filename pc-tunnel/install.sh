#!/bin/bash
# 装「添加电脑」：把这套脚本装进运行 Claude 的那个用户的 ~/tunnels，写好配置、主机公钥和中转用的钥匙，
# 再下载 Windows 用的 OpenSSH 安装包。以运行 Claude 的那个用户、在查看器的代码目录里运行（单用户时就是你自己），重复运行没关系：
#   BASE=https://viewer.example.com bash pc-tunnel/install.sh
# 配置用环境变量给，第一次装时 BASE 必填；以后重装时没给的沿用 ~/tunnels/pc-tunnel.conf 里已有的（要清掉某一项就直接改那个文件）：
#   BASE         电脑访问得到的查看器地址，比如 https://viewer.example.com（/pc-setup/ 这一路不能要网页登录）
#   NAME         给用户看的服务器名字，默认 cc-viewer
#   TARGET_HOST  连这台机器 SSH 用的地址：有中转时写从中转服务器看过来的地址，没有中转时写电脑能直接连到的地址；默认 127.0.0.1
#   TARGET_PORT  上面那个地址上这台机器 SSH 的端口，默认 22
#   RELAY_HOST   中转服务器的地址；电脑能直接连到这台机器时不写
#   RELAY_PORT   中转服务器 SSH 的端口，默认 22
#   RELAY_USER   中转服务器上专门给电脑用的受限账号
# 中转服务器要怎么准备见 pc-tunnel/README.md。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
VIEWER_DIR=$(dirname "$HERE")
T=$HOME/tunnels
fail() { echo "装不上：$*" >&2; exit 1; }

# 已有的配置：这次没给的沿用
if [ -f "$T/pc-tunnel.conf" ]; then
    while IFS='=' read -r k v; do
        case "$k" in
            BASE|NAME|TARGET_HOST|TARGET_PORT|RELAY_HOST|RELAY_PORT|RELAY_USER) [ -n "${!k:-}" ] || printf -v "$k" '%s' "$v" ;;
        esac
    done < "$T/pc-tunnel.conf"
fi
NAME=${NAME:-cc-viewer} TARGET_HOST=${TARGET_HOST:-127.0.0.1} TARGET_PORT=${TARGET_PORT:-22}
RELAY_HOST=${RELAY_HOST:-} RELAY_PORT=${RELAY_PORT:-22} RELAY_USER=${RELAY_USER:-}

# 这些值会被填进安装脚本、写进配置文件（配置文件由 bash 读），所以只认简单的写法
[ -n "${BASE:-}" ] || fail "要给 BASE（电脑访问得到的查看器地址），比如：BASE=https://viewer.example.com bash pc-tunnel/install.sh"
[[ "$BASE" =~ ^https?://[A-Za-z0-9.:-]+(/[A-Za-z0-9._/-]*[A-Za-z0-9._-])?$ ]] || fail "BASE 写得不对：$BASE（写 http(s)://域名[:端口][/路径]，结尾不要斜杠）"
for k in NAME TARGET_HOST RELAY_HOST RELAY_USER; do
    [[ "${!k}" =~ ^[A-Za-z0-9._:-]*$ ]] || fail "$k 只能用字母、数字和 . _ : -：${!k}"
done
for k in TARGET_PORT RELAY_PORT; do
    [[ "${!k}" =~ ^[0-9]{1,5}$ ]] || fail "$k 要写端口号：${!k}"
done
[ -z "$RELAY_HOST" ] || [ -n "$RELAY_USER" ] || fail "写了 RELAY_HOST 就要写 RELAY_USER（中转服务器上给电脑用的受限账号）"
[[ "$VIEWER_DIR" =~ ^[^[:space:]\'\"\$\`\\]+$ ]] || fail "查看器代码目录的路径里不能有空格、引号这类字符：$VIEWER_DIR"
[ -x "$VIEWER_DIR/ccssh" ] || fail "在 $VIEWER_DIR 里找不到 ccssh：请在查看器的代码目录里运行 pc-tunnel/install.sh"
command -v ssh-keygen >/dev/null || fail "没有 ssh-keygen（先装 openssh-client）"

mkdir -p "$T/keys" "$T/dl" "$T/files" "$HOME/.ssh"
chmod 700 "$T/keys" "$T/dl" "$HOME/.ssh"

# 脚本：报到、隧道钥匙的强制命令写的是 ~/tunnels 下的路径，所以装到这里，不直接从代码目录跑
install -m 755 "$HERE/make-pc-setup.sh" "$HERE/pc-enroll.py" "$HERE/pc-hello.sh" "$HERE/pc-remove.py" "$HERE/wait-pc.py" "$T/"
install -m 644 "$HERE/tpl-unix.sh" "$HERE/tpl-win.ps1" "$T/"
install -m 644 "$HERE/uninstall.sh" "$HERE/uninstall.ps1" "$T/files/"

cat > "$T/pc-tunnel.conf" <<EOF
# 添加电脑的配置（pc-tunnel/install.sh 写的；改了不用重启查看器，下一个链接就用新的）
NAME=$NAME
BASE=$BASE
TARGET_HOST=$TARGET_HOST
TARGET_PORT=$TARGET_PORT
RELAY_HOST=$RELAY_HOST
RELAY_PORT=$RELAY_PORT
RELAY_USER=$RELAY_USER
VIEWER_DIR=$VIEWER_DIR
EOF

# 主机公钥：电脑连这台机器时按别名 ccviewer 核对（安装脚本里写的 HostKeyAlias），有中转时再加上中转服务器的
own=
for t in ed25519 ecdsa rsa; do
    f=/etc/ssh/ssh_host_${t}_key.pub
    if [ -r "$f" ]; then own=$(cut -d' ' -f1,2 "$f"); break; fi
done
[ -n "$own" ] || fail "读不到这台机器的 SSH 主机公钥（/etc/ssh/ssh_host_*_key.pub）"
{
    echo "ccviewer $own"
    if [ -n "$RELAY_HOST" ]; then
        hk=$(ssh-keyscan -T 5 -p "$RELAY_PORT" -t ed25519,ecdsa,rsa "$RELAY_HOST" 2>/dev/null)
        [ -n "$hk" ] || fail "取不到中转服务器 $RELAY_HOST:$RELAY_PORT 的主机公钥"
        echo "$hk"
    fi
} > "$T/known_hosts.new"
mv "$T/known_hosts.new" "$T/known_hosts"

# 中转用的钥匙：所有电脑共用这一把，在中转服务器上它只许转发到这台机器的 SSH
if [ -n "$RELAY_HOST" ] && [ ! -f "$T/keys/relay" ]; then
    ssh-keygen -q -t ed25519 -N '' -C ccviewer-relay -f "$T/keys/relay"
fi

# Windows 用的 OpenSSH 安装包（微软 Win32-OpenSSH 的发行版，校验值和 tpl-win.ps1 里写的一样）。
# 下不来也能装，电脑会改走 Windows Update，只是慢一些
REL=https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview
for pair in OpenSSH-Win64-v10.0.0.0.msi:ddec9c53864280759cf9f74791cefd387100e3946aa849a1c138a4ed1b96b7d9 \
            OpenSSH-ARM64-v10.0.0.0.msi:7a17d0e22d004fb47ca4bfd8fef926fa305de4ebf70a6f3c7a29c39aabef0023; do
    f=${pair%%:*} sum=${pair#*:}
    if [ -f "$T/files/$f" ] && echo "$sum  $T/files/$f" | sha256sum -c --status; then continue; fi
    if curl -fsSL --max-time 300 -o "$T/files/$f.part" "$REL/$f" && echo "$sum  $T/files/$f.part" | sha256sum -c --status; then
        mv "$T/files/$f.part" "$T/files/$f"
    else
        rm -f "$T/files/$f.part"
        echo "注意：$f 没下载下来（或者校验值不对），Windows 电脑会改走 Windows Update 装 SSH 服务"
    fi
done

echo "装好了：$T（配置在 $T/pc-tunnel.conf）"
echo "主机公钥的指纹如下。有中转时请核对中转服务器那一行，要和在中转服务器上运行 ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub 的结果一样："
ssh-keygen -lf "$T/known_hosts"
if [ -n "$RELAY_HOST" ]; then
    echo
    echo "还要在中转服务器上做一件事：把下面这一行加进账号 $RELAY_USER 的 ~/.ssh/authorized_keys。"
    echo "这把钥匙只能转发到 $TARGET_HOST:$TARGET_PORT，不能执行命令；还必须按 README 配好中转账号的 AllowTcpForwarding local，禁止反向转发。"
    echo "restrict,port-forwarding,permitopen=\"$TARGET_HOST:$TARGET_PORT\",command=\"/bin/false\" $(cut -d' ' -f1,2 "$T/keys/relay.pub") ccviewer-relay"
fi
echo
echo "另外请确认：查看器的 instance.json 里开了 gw_socket（ssh 门卫），网页的「设备」页才有「添加设备」；"
echo "电脑要访问得到 $BASE/pc-setup/，而且这一路不能要网页登录。详见 pc-tunnel/README.md。"
