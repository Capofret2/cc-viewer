#!/usr/bin/env python3
"""报到钥匙的强制命令（以运行 Claude 的那个用户运行，见 make-pc-setup.sh 开头）。

电脑装到第 3 步「登记这台电脑」时，带着自己生成的隧道公钥和「ssh cc」用的登录公钥连上来报到
（用安装脚本里那把报到钥匙，authorized_keys 里它只能执行本脚本；有中转服务器时经它中转）。这里给它分端口，
把隧道钥匙登记成只能在这个端口开反向隧道（连上时执行 pc-hello.sh），登录钥匙登记成 cc-login-<标识>，回答 ok <端口>。
- 同一台电脑重装时带的是同一把隧道钥匙：认出来就沿用原来的端口，门卫里的别名、记下的主机指纹都不用变。
- 不同的电脑（哪怕用的是同一个链接）各分一个端口，不然接上一台就会把另一台踢掉。
- 链接过期（dl/<名字>.ticket 里写的时刻，生成后 60 分钟）或者这个链接已经报到过 5 台电脑就拒绝。
请求（SSH_ORIGINAL_COMMAND）：enroll <系统> <隧道公钥类型> <隧道公钥> <登录公钥类型> <登录公钥> <电脑用户名> <主机名>
用户名、主机名是 UTF-8 的 base64url（Windows 的用户名可以有空格、中文）。回答是给电脑上的安装脚本看的，用英文。
每次报到在这个链接的进度记录 dl/<名字>.log 里记一行（来源记成 host），wait-pc.py 和设备页看到了就去登记门卫、测通。
改 authorized_keys 和端口表时拿 tunnels/.lock 这把锁，和 make-pc-setup.sh、pc-hello.sh 共用。
"""
import base64
import fcntl
import os
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
T = HOME / "tunnels"
AK = HOME / ".ssh" / "authorized_keys"


def _conf() -> dict:
    """~/tunnels/pc-tunnel.conf（pc-tunnel/install.sh 写的）：一行一个「名字=值」。"""
    try:
        return {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in (T / "pc-tunnel.conf").read_text().splitlines()
                                                   if "=" in l and not l.lstrip().startswith("#"))}
    except FileNotFoundError:
        return {}


NAME = _conf().get("NAME") or "cc-viewer"   # 回给电脑的话里怎么称呼这台服务器
KEYTYPE = re.compile(r"ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)")
BLOB = re.compile(r"[A-Za-z0-9+/]{16,1000}={0,3}")
B64URL = re.compile(r"[A-Za-z0-9_-]{1,200}")
LOGIN = re.compile(r"[^\s\"'\\@:%][^\"'\\@:%\x00-\x1f\x7f]{0,63}")   # 和门卫（cc_viewer/sshgw.py）认的电脑用户名一致
HOSTNAME = re.compile(r"[^\x00-\x1f\x7f]{1,64}")
OSES = ("windows", "mac", "linux")
FIRST_PORT = 23001   # 和别处用到的端口错开（设备页 devices.py 也认这个起点）
MAX_PCS = 5
LISTEN = re.compile(r'permitlisten="127\.0\.0\.1:(\d+)"')
LABEL = re.compile(r'pc-hello\.sh (\S+) \d+"')


def answer(text: str, code: int = 0):
    print(text)
    sys.exit(code)


def unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")


def main():
    tok = sys.argv[1] if len(sys.argv) > 1 else ""
    if not re.fullmatch(r"[0-9a-f]{32}", tok):
        answer("error bad link", 1)
    try:
        info = dict(l.split("=", 1) for l in (T / "dl" / f"{tok}.ticket").read_text().splitlines() if "=" in l)
    except FileNotFoundError:
        answer(f"error this link does not exist; ask Claude on {NAME} for a new command", 1)
    if time.time() > float(info.get("expires") or 0):
        answer(f"error this link has expired (links work for 60 minutes); ask Claude on {NAME} for a new command", 1)

    req = os.environ.get("SSH_ORIGINAL_COMMAND", "").split()
    if len(req) != 8 or req[0] != "enroll":
        answer("error bad request", 1)
    _, osname, tt, tk, lt, lk, u64, h64 = req
    if osname not in OSES or not KEYTYPE.fullmatch(tt) or not BLOB.fullmatch(tk) \
            or not KEYTYPE.fullmatch(lt) or not BLOB.fullmatch(lk) or not B64URL.fullmatch(u64) or not B64URL.fullmatch(h64):
        answer("error bad request", 1)
    try:
        user, host = unb64(u64), unb64(h64)
    except ValueError:
        answer("error bad request", 1)
    if not LOGIN.fullmatch(user):
        answer(f"error this user name cannot be used for ssh logins: {user!r}", 1)
    if not HOSTNAME.fullmatch(host):
        answer("error bad computer name", 1)

    blob = f"{tt} {tk}"
    log = T / "dl" / f"{tok}.log"
    with open(T / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = AK.read_text().splitlines() if AK.exists() else []
        mine = next((l for l in lines if f" {blob} " in f" {l} " and LISTEN.search(l)), None)
        if mine is None:
            try:
                done = {m[1] for m in re.finditer(r"\tenrolled\tport=(\d+)", log.read_text())}
            except FileNotFoundError:
                done = set()
            if len(done) >= MAX_PCS:
                answer(f"error this link has already been used for {MAX_PCS} computers; ask Claude on {NAME} for a new command", 1)
            used = {int(m[1]) for l in lines if (m := LISTEN.search(l))}
            ports = T / "ports.txt"
            for l in ports.read_text().splitlines() if ports.exists() else []:
                f = l.split()
                if len(f) == 2 and f[1].isdigit():
                    used.add(int(f[1]))
            port = max(used | {FIRST_PORT - 1}) + 1
            label = str(port)
            lines.append(f'restrict,port-forwarding,permitlisten="127.0.0.1:{port}",permitopen="127.0.0.1:9",'
                         f'command="bash {T}/pc-hello.sh {port} {port}" {blob} cc-tunnel-{port}')
            safe = re.sub(r"[^A-Za-z0-9.-]", "", host)[:24] or "pc"
            with ports.open("a") as f:
                f.write(f"{info.get('who', '?')}/{osname}-{safe} {port}\n")
        else:
            port = int(LISTEN.search(mine)[1])
            label = m[1] if (m := LABEL.search(mine)) else str(port)
        # 「ssh cc」用的登录钥匙：每台电脑一把，重装换新的
        lines = [l for l in lines if not l.endswith(f" cc-login-{label}")] + [f"{lt} {lk} cc-login-{label}"]
        tmp = AK.with_name(f".authorized_keys.{os.getpid()}")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.chmod(0o600)
        tmp.replace(AK)
        (T / f"{label}.user").write_text(user + "\n")   # 隧道每次连上时 pc-hello.sh 也会写它
        with log.open("a") as f:
            f.write(f"{time.time():.0f}\thost\tenrolled\tport={port} label={label} os={osname} user64={u64} host64={h64} "
                    f"new={int(mine is None)}\n")
    answer(f"ok {port}")


if __name__ == "__main__":
    main()
