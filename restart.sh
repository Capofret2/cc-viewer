#!/bin/bash
# 按端口重启一个自己起的查看器实例（比如 8777），以实例所属的系统用户运行。
# 归 systemd 管的实例要用 systemctl 重启：这个脚本另起的进程 systemd 不知道，它又会自己拉起一份，两份抢端口，
# 所以发现目标归 systemd 管就拒绝。
# 照旧进程原样重启：同一个 Python、同一个工作目录、同样的命令行参数、同一个数据目录（CC_VIEWER_DATA），
# 所以实例第一次要手动起，以后都靠它。网页对话里的 Claude 自己就是实例的子进程，要由它来重启时这样用：
#   setsid env -i HOME=$HOME USER=$USER LOGNAME=$USER PATH=/usr/bin:/bin \
#       bash -i -c '<代码目录>/restart.sh 8777 0' </dev/null >/dev/null 2>&1 &
# setsid 让它脱离发起者（实例一关，发起它的 CLI 也会被收掉）；干净的环境加 bash -i 读 .bashrc，
# 和在终端里起的一样，不把 CLI 的环境变量带进新服务。要立刻重启就把等待秒数给 0。
# 先等给定的秒数，再预写存盘（见下面），再按进程号发 Ctrl+C 正常退出，等端口空出来，原样重新起。
# 日志记在实例数据目录的 restart.log，服务自己的输出接在同一目录的 nohup.out 后面。
# 要在停和起之间改数据目录里的文件，就设环境变量 BEFORE_START（一条 bash 命令），见下面。
PORT=${1:?用法：restart.sh <端口> [等待秒数，默认 60]}
DELAY=${2:-60}

pid=$(ss -ltnp | grep -o "127.0.0.1:$PORT .*pid=[0-9]*" | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1)
if [ -z "$pid" ]; then echo "$PORT 上没有自己的实例在跑，第一次要手动起" >&2; exit 1; fi
unit=$(grep -o '[^/]*\.service$' "/proc/$pid/cgroup")
if [ -n "$unit" ]; then
    echo "$PORT 的进程 $pid 归 systemd 的 $unit 管，不能用这个脚本重启，要用 systemctl --user restart ${unit%.service}" >&2
    exit 1
fi
py=$(readlink "/proc/$pid/exe")
dir=$(readlink "/proc/$pid/cwd")
mapfile -d '' -t argv < "/proc/$pid/cmdline"
data=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^CC_VIEWER_DATA=//p')
DATA=${data:-$(cd "$(dirname "$0")" && pwd)}
LOG=$DATA/restart.log
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

say "计划 $DELAY 秒后重启 $PORT（进程 $pid：$py ${argv[*]:1}，工作目录 $dir，数据目录 $DATA）"
sleep "$DELAY"
# 关旧服务之前先预写调用表和花费存盘（prewarm_cache.py），这期间旧服务照常服务；
# 存盘对得上时几秒就完，代码改过才从头读。预写出错就不重启（服务照旧是旧的），要跳过就设 NO_PREWARM=1
if [ -z "${NO_PREWARM:-}" ]; then
    say "先预写存盘"
    if ! (cd "$(dirname "$0")" && CC_VIEWER_DATA=$DATA timeout 1500 nice -n 10 "$py" prewarm_cache.py) >> "$LOG" 2>&1; then
        say "预写出错，没有重启 $PORT（上面是它的输出；确定要跳过预写就设 NO_PREWARM=1 再跑）"
        exit 1
    fi
fi
if kill -0 "$pid" 2>/dev/null; then
    say "给旧服务 $pid 发 Ctrl+C"
    kill -INT "$pid"
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then say "30 秒还没退，发 SIGTERM"; kill -TERM "$pid"; sleep 3; fi
fi
for _ in $(seq 1 20); do ss -ltn | grep -q "127.0.0.1:$PORT " || break; sleep 1; done
# BEFORE_START：旧服务已退、新服务还没起的空档里要做的事（比如改数据目录里的 session_owners.json，
# 运行中的实例把这些表放在内存里，会整份写回、冲掉外面的改动）。失败只记日志，照样起服务
if [ -n "${BEFORE_START:-}" ]; then
    say "起之前先执行：$BEFORE_START"
    bash -c "$BEFORE_START" >> "$LOG" 2>&1 || say "上面这条执行失败（退出码 $?），照样起服务"
fi
cd "$dir" || { say "进不去原来的工作目录 $dir，没有重新起"; exit 1; }
[ -n "$data" ] && export CC_VIEWER_DATA=$data
nohup "$py" "${argv[@]:1}" >> "$DATA/nohup.out" 2>&1 &
say "新服务进程 $!"
sleep 5
if ss -ltn | grep -q "127.0.0.1:$PORT "; then say "$PORT 已经起来了"; else say "$PORT 没起来，看 $DATA/nohup.out"; fi
