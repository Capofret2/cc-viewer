#!/bin/bash
# 为会话主人生成一个装电脑用的链接，打印给用户的两条命令（Windows、Mac/Linux）和等待命令。以运行 Claude 的那个用户运行，
# 由网页会话里的 Claude 调用，或者由网页的「设备」页（devices.py）调用。配置在 ~/tunnels/pc-tunnel.conf（pc-tunnel/install.sh 写的）。
# 登录电脑的钥匙在查看器的 ssh 门卫里（每人一把）：装到电脑上的是 ccssh key 给出的会话主人的公钥，电脑接上以后登记进门卫。
# 端口不在这里分：电脑装到第 3 步时自己生成隧道钥匙，用报到钥匙连上来报到（pc-enroll.py），那时按电脑分端口；
# 同一个链接装几台电脑各得一个端口，同一台电脑重装认得出来，沿用原端口。链接 60 分钟内能反复用。
# 安装脚本边装边把进度和报错报回来（查看器记进 dl/<名字>.log），wait-pc.py 盯着它，装好、出错、卡住都会结束。
# Windows 的 SSH 服务装微软官方的 OpenSSH 安装包（install.sh 放进 files/，由查看器发给电脑），拿不到就退回 Windows Update。
# 生成的东西：dl/<名字>.ps1 / .sh（安装脚本）、dl/<名字>.ticket（谁的、几点过期）、keys/enroll-<名字>（报到钥匙），
# 以及 authorized_keys 里一行只能执行 pc-enroll.py 的报到钥匙（过期的下次运行时清掉）。一天以前的 dl/ 文件顺手删掉。
# 用法：make-pc-setup.sh [--pub <门卫公钥>] [--from chat|web] [--machine]（电脑在门卫里叫什么按系统起：win、mac、linux）
#   设备页不在会话里、没法 ccssh key，门卫公钥由它经 --pub 交过来；--from web 记下是从设备页加的，--machine 只打印给程序读的几行。
# 环境变量 CCV_TEST_PUB 可以代替门卫公钥、CCV_TEST_BASE 可以换下载入口、CCV_TEST_TPL 可以换安装模板所在的目录，只给测试用。
set -euo pipefail
T=$HOME/tunnels
CONF=$T/pc-tunnel.conf
[ -f "$CONF" ] || { echo "还没配好添加电脑：先在查看器的代码目录里运行 pc-tunnel/install.sh" >&2; exit 1; }
. "$CONF"
pubarg='' from=chat machine=''
while [ $# -gt 0 ]; do
    case "$1" in
        --pub) pubarg=${2:-}; shift 2 ;;
        --from) from=${2:-}; shift 2 ;;
        --machine) machine=1; shift ;;
        *) shift ;;   # 改版以前的用法带着电脑名（make-pc-setup.sh pc），现在电脑名由系统起，不用了
    esac
done
[[ "$from" =~ ^(chat|web)$ ]] || { echo "--from 只能是 chat 或 web" >&2; exit 1; }

BASE=${CCV_TEST_BASE:-${BASE:-}}
TPL=${CCV_TEST_TPL:-$T}
for v in "${NAME:-cc-viewer}" "${RELAY_HOST:-}" "${RELAY_USER:-}" "${TARGET_HOST:-127.0.0.1}" "${RELAY_PORT:-22}" "${TARGET_PORT:-22}"; do
    [[ "$v" =~ ^[A-Za-z0-9._:-]*$ ]] || { echo "pc-tunnel.conf 里有不认得的写法：$v" >&2; exit 1; }
done
[[ "$BASE" =~ ^https?://[A-Za-z0-9.:-]+(/[A-Za-z0-9._/-]*[A-Za-z0-9._-])?$ ]] || { echo "pc-tunnel.conf 的 BASE 写得不对：$BASE" >&2; exit 1; }
VALID=3600
mkdir -p "$T/keys" "$T/dl" "$HOME/.ssh"
chmod 700 "$T/keys" "$T/dl" "$HOME/.ssh"
touch "$T/ports.txt" "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
[ -f "$T/known_hosts" ] && { [ -z "${RELAY_HOST:-}" ] || [ -f "$T/keys/relay" ]; } || { echo "缺主机公钥或中转用的钥匙（~/tunnels/known_hosts、~/tunnels/keys/relay）：先运行 pc-tunnel/install.sh" >&2; exit 1; }

# 查看器这台机器登录用户电脑用的钥匙：会话主人在门卫里的那把，这里只拿公钥
pubgw=${pubarg:-${CCV_TEST_PUB:-$("${VIEWER_DIR:-.}/ccssh" key)}} || { echo "拿不到门卫给的公钥：这个脚本要在网页查看器里、登录后的会话中运行" >&2; exit 1; }
[[ "$pubgw" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+\ cc_viewer-gw-([0-9a-z]+)$ ]] || { echo "门卫给的公钥不对：$pubgw" >&2; exit 1; }
who=${BASH_REMATCH[1]}

tok=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
now=$(date +%s)
find "$T/dl" -type f -mmin +1440 -delete
ek=$T/keys/enroll-$tok
ssh-keygen -q -t ed25519 -N '' -C "cc-enroll-$tok" -f "$ek"
printf 'who=%s\ncreated=%s\nexpires=%s\nfrom=%s\n' "$who" "$now" "$((now + VALID))" "$from" > "$T/dl/$tok.ticket"

# authorized_keys：清掉过期的报到钥匙，加上这把（它只能执行 pc-enroll.py，不能转发、不能开终端）
(
    flock 9
    python3 - "$T" "$tok" "$HOME/.ssh/authorized_keys" <<'PY'
import os, re, sys, time
from pathlib import Path
T, tok, ak = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
keep = []
for l in ak.read_text().splitlines():
    m = re.search(r" cc-enroll-([0-9a-f]{32})$", l)
    if m and m[1] != tok:
        try:
            alive = time.time() < float(dict(x.split("=", 1) for x in (T / "dl" / f"{m[1]}.ticket").read_text().splitlines() if "=" in x)["expires"])
        except (OSError, KeyError, ValueError):
            alive = False
        if not alive:
            for f in (T / "keys" / f"enroll-{m[1]}", T / "keys" / f"enroll-{m[1]}.pub"):
                f.unlink(missing_ok=True)
            continue
    keep.append(l)
pub = (T / "keys" / f"enroll-{tok}.pub").read_text().split()
keep.append(f'restrict,command="/usr/bin/python3 -I {T}/pc-enroll.py {tok}" {pub[0]} {pub[1]} cc-enroll-{tok}')
tmp = ak.with_name(f".authorized_keys.{os.getpid()}")
tmp.write_text("\n".join(keep) + "\n")
tmp.chmod(0o600)
tmp.replace(ak)
PY
) 9>>"$T/.lock"

# 两份安装脚本，同一个名字、不同后缀
for os in unix win; do
    ext=sh; [ $os = win ] && ext=ps1
    python3 - "$TPL/tpl-$os.$ext" "$T/dl/$tok.$ext" "$tok" "$BASE" "$pubgw" "$T/keys/relay" "$ek" "$T/known_hosts" \
        "${NAME:-cc-viewer}" "${RELAY_HOST:-}" "${RELAY_PORT:-22}" "${RELAY_USER:-}" "${TARGET_HOST:-127.0.0.1}" "${TARGET_PORT:-22}" "$(id -un)" <<'PY'
import sys
tpl, out, tok, base, pub, relay, enroll, known, name, rhost, rport, ruser, thost, tport, login = sys.argv[1:]
s = open(tpl).read()
for k, v in {
    '__TOK__': tok, '__BASE__': base, '__PUBGW__': pub.strip(), '__KNOWNHOSTS__': open(known).read().strip(),
    '__RELAYKEY__': open(relay).read().strip() if rhost else '', '__ENROLLKEY__': open(enroll).read().strip(),
    '__NAME__': name, '__RELAYHOST__': rhost, '__RELAYPORT__': rport, '__RELAYUSER__': ruser,
    '__TARGETHOST__': thost, '__TARGETPORT__': tport, '__LOGINUSER__': login,
}.items():
    s = s.replace(k, v)
open(out, 'w').write(s)
PY
    chmod 600 "$T/dl/$tok.$ext"
done

win="[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm $BASE/pc-setup/$tok.ps1 | iex"
unix="bash -c \"\$(curl -fsSL $BASE/pc-setup/$tok.sh || wget -qO- $BASE/pc-setup/$tok.sh)\""
printf 'win=%s\nunix=%s\n' "$win" "$unix" >> "$T/dl/$tok.ticket"   # 设备页重新打开这个链接（对话里给的 #dev=）时要再显示命令
if [ -n "$machine" ]; then
    printf 'tok=%s\nexpires=%s\nwin=%s\nunix=%s\n' "$tok" "$((now + VALID))" "$win" "$unix"
    exit 0
fi
echo "链接 $tok：60 分钟内有效，可以反复运行，几台电脑都能用它装（每台各分一个端口）"
echo "接着在后台运行（装好、出错、卡住都会结束并叫醒你）：python3 ~/tunnels/wait-pc.py $tok"
echo "用户也可以在网页上看安装进度，消息里给他这个链接：[在网页上看安装进度](#dev=$tok)"
echo "Windows（打开 PowerShell，不用管理员身份，粘贴回车；弹出「是否允许此应用对你的设备进行更改」点「是」）："
echo "$win"
echo "Mac 或 Linux（打开终端，粘贴回车；要密码时输入这台电脑的开机密码）："
echo "$unix"
