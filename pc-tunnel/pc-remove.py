#!/usr/bin/env python3
"""吊销一台电脑（以运行 Claude 的那个用户运行；网页的「设备」页删除电脑时经 hostfs 调它）。

用法：python3 ~/tunnels/pc-remove.py <端口>
删掉 authorized_keys 里这台电脑的隧道钥匙（cc-tunnel-<标识>）和「ssh cc」用的登录钥匙（cc-login-<标识>），
结束它正挂着的隧道会话，删掉 <标识>.user / .pid 和端口表里的那一行。之后这台电脑再连上来也进不来了；
电脑上装的东西由设备页经门卫卸（电脑在线时），或者用户自己跑卸载命令。标识是端口号。
只认 23001 起的端口（电脑用的），改授权文件时拿 tunnels/.lock 这把锁（和 pc-enroll.py、make-pc-setup.sh 共用）。
"""
import fcntl
import os
import re
import signal
import sys
from pathlib import Path

HOME = Path.home()
T = HOME / "tunnels"
AK = HOME / ".ssh" / "authorized_keys"


def holder(pidf: Path) -> int | None:
    """pc-hello.sh hold 记下的隧道会话：进程号和启动时间都对得上、还是本用户的 sshd，才算它。"""
    try:
        pid, start = pidf.read_text().split()[:2]
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    if not cmd.startswith(f"sshd: {os.environ.get('USER') or Path.home().name}") or stat[stat.rindex(")") + 2:].split()[19] != start:
        return None
    return int(pid)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if not arg.isdigit() or not 23001 <= int(arg) <= 65535:
        print("error 只认 23001 起的端口", file=sys.stderr)
        sys.exit(2)
    port = int(arg)
    with open(T / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = AK.read_text().splitlines() if AK.exists() else []
        tunnel = [l for l in lines if f'permitlisten="127.0.0.1:{port}"' in l]
        labels = {m[1] for l in tunnel if (m := re.search(r'pc-hello\.sh (\S+) \d+"', l))} or {str(port)}
        keep = [l for l in lines if l not in tunnel and not any(l.endswith(f" cc-login-{x}") for x in labels)]
        if len(keep) != len(lines):
            tmp = AK.with_name(f".authorized_keys.{os.getpid()}")
            tmp.write_text("\n".join(keep) + "\n")
            tmp.chmod(0o600)
            tmp.replace(AK)
        for x in labels:
            if pid := holder(T / f"{x}.pid"):
                os.kill(pid, signal.SIGTERM)
            for f in (T / f"{x}.pid", T / f"{x}.user"):
                f.unlink(missing_ok=True)
        ports = T / "ports.txt"
        if ports.exists():
            rest = [l for l in ports.read_text().splitlines() if l.split()[1:2] != [str(port)]]
            ports.write_text("".join(f"{l}\n" for l in rest))
    print(f"ok 端口 {port}（{'、'.join(sorted(labels))}）删掉了 {len(lines) - len(keep)} 行钥匙")


if __name__ == "__main__":
    main()
