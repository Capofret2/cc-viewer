#!/bin/bash
# 隧道专用钥匙的强制命令（以运行 Claude 的那个用户运行）。用户电脑会带着下面几种请求连上来：
#   user=<电脑用户名>      挂隧道之前：结束上一次的隧道会话（断线后它可能还占着端口），登记用户名
#   user64=<base64url>     同上，用户名是 UTF-8 的 base64url（Windows 的用户名可以有空格、中文）
#   hold                   挂隧道：记下持有这条隧道的 sshd 会话进程，然后一直挂着
#   status                 这台电脑的隧道接上了没有（端口在监听回 up，否则 down）：安装脚本最后用它确认真的通了才说装好
#   addkey=<公钥>          把电脑自己的公钥登记进来，之后在电脑上 ssh cc 免密（安装脚本在报到时就登记了，这一条留给手动用）
# 不用 fuser/ss 找端口的持有者：sshd 会话进程不可查看，同一用户也读不到它的连接。
# 第一个参数是这台电脑的标识（端口号），第二个参数是端口
abbr="$1"; port="$2"
T=$HOME/tunnels
pidf=$T/$abbr.pid

is_my_sshd() { { tr '\0' ' ' < "/proc/$1/cmdline"; } 2>/dev/null | grep -q "^sshd: $(id -un)"; }
starttime() { awk '{print $22}' "/proc/$1/stat" 2>/dev/null; }

hello() {
    # 进程号和启动时间都对得上，才是上次记下的那个会话
    { read -r pid st < "$pidf"; } 2>/dev/null
    if [ -n "${pid:-}" ] && is_my_sshd "$pid" && [ "$(starttime "$pid")" = "${st:-}" ]; then
        kill "$pid"
    fi
    case "$1" in ''|*\"*|*\'*|*$'\n'*|*$'\r'*|*$'\t'*) exit 0 ;; esac
    printf '%s\n' "$1" > "$T/$abbr.user"
}

req="${SSH_ORIGINAL_COMMAND:-}"
case "$req" in
    hold)
        p=$PPID
        for _ in 1 2 3 4 5; do
            is_my_sshd "$p" && break
            p=$(awk '{print $4}' "/proc/$p/stat" 2>/dev/null) || break
        done
        is_my_sshd "$p" && echo "$p $(starttime "$p")" > "$pidf"
        exec sleep infinity
        ;;
    user=?*)
        hello "${req#user=}"
        ;;
    user64=?*)
        b="${req#user64=}"
        [[ "$b" =~ ^[A-Za-z0-9_-]{1,200}$ ]] || exit 0
        b="${b//-/+}"; b="${b//_//}"
        while (( ${#b} % 4 )); do b+="="; done
        hello "$(printf '%s' "$b" | base64 -d 2>/dev/null)"
        ;;
    status)
        if ss -Hltn "sport = :$port" | grep -q .; then echo up; else echo down; fi
        ;;
    addkey=?*)
        key="${req#addkey=}"
        if [[ "$key" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))\ [A-Za-z0-9+/=]+ ]]; then
            ak=$HOME/.ssh/authorized_keys
            exec 9>>"$T/.lock"; flock 9   # 和 pc-enroll.py、make-pc-setup.sh 共用一把锁
            { grep -v " cc-login-$abbr\$" "$ak"; echo "${BASH_REMATCH[0]} cc-login-$abbr"; } > "$ak.tmp"
            mv "$ak.tmp" "$ak"
            chmod 600 "$ak"
            echo ok
        fi
        ;;
esac
exit 0
