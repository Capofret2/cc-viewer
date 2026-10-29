#!/usr/bin/env python3
"""等用户的电脑装好（以运行 Claude 的那个用户运行，见 make-pc-setup.sh 开头）。

用法：python3 ~/tunnels/wait-pc.py <链接名字> [最多等几分钟，默认 60]   由网页会话里的 Claude 放到后台跑
      python3 ~/tunnels/wait-pc.py <链接名字> --show                    把这个链接的进度翻成中文打印出来（用户问装到哪了时用）
盯着这个链接的进度记录 dl/<名字>.log（查看器记下载和电脑报上来的进度、报错，pc-enroll.py 记报到），有结果就结束，
CLI 会叫醒 Claude 看输出、告诉用户。退出码：
  0 有电脑接好了：查看器把它登记进门卫（devices.py；名字按系统起：win、mac、linux，重名加数字；重装的电脑沿用原来的名字，
    和网页的「设备」页是同一份），这里再从门卫登录测通。隧道接上 25 秒还没等到查看器登记，就自己 ccssh add
  3 电脑那边报了错（输出里有原话和整个过程）   4 装到一半 5 分钟没动静   5 报到了但 3 分钟还没接上   6 登记或测试没成功
  1 等满了时间也没有结果   2 用法不对
用同一个链接装几台电脑时，接好一台就先结束；再跑一次接着等（已经报告过的结果记在 dl/<名字>.seen，不会重报）。
ccssh 认会话靠进程父子关系，所以必须由会话里的 Claude 自己起。
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

T = Path.home() / "tunnels"


def _conf() -> dict:
    """~/tunnels/pc-tunnel.conf（pc-tunnel/install.sh 写的）：一行一个「名字=值」。"""
    try:
        return {k.strip(): v.strip() for k, v in (l.split("=", 1) for l in (T / "pc-tunnel.conf").read_text().splitlines()
                                                   if "=" in l and not l.lstrip().startswith("#"))}
    except FileNotFoundError:
        return {}


CONF = _conf()
NAME = CONF.get("NAME") or "cc-viewer"
# 门卫的客户端在查看器的代码目录里；测试时换成假的（网页会话外用不了门卫）
CCSSH = os.environ.get("CCV_TEST_CCSSH") or str(Path(CONF.get("VIEWER_DIR") or ".") / "ccssh")
STUCK = 300
NO_TUNNEL = 180
OSNAME = {"windows": "Windows", "mac": "Mac", "linux": "Linux"}
ALIAS = {"windows": "win", "mac": "mac", "linux": "linux"}


def unb64(s: str) -> str:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
    except ValueError:
        return "?"


def hm(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def events(log: Path) -> list[dict]:
    """进度记录的每一行：时刻、谁记的（web 查看器、pc 电脑、host 报到）、内容。"""
    out = []
    try:
        text = log.read_text(errors="replace")
    except FileNotFoundError:
        return out
    for i, line in enumerate(text.splitlines()):
        f = line.split("\t", 2)
        if len(f) == 3 and f[0].isdigit():
            out.append({"i": i, "ts": int(f[0]), "src": f[1], "text": f[2]})
    return out


def describe(e: dict) -> str:
    t, src = e["text"], e["src"]
    if src == "web":
        kind, _, rest = t.partition("\t")
        what = {"download": "下载了安装脚本", "expired": "下载时链接已过期，没给"}.get(kind, kind)
        return f"{what}（{rest}）"
    if src == "host" and t.startswith("enrolled\t"):
        kv = dict(x.split("=", 1) for x in t.split("\t", 1)[1].split() if "=" in x)
        return (f"报到成功：{OSNAME.get(kv.get('os'), kv.get('os'))} 电脑 {unb64(kv.get('host64', ''))}（用户 {unb64(kv.get('user64', ''))}），"
                f"端口 {kv.get('port')}" + ("，是以前装过的电脑，沿用原端口" if kv.get("new") == "0" else ""))
    if src == "pc":
        return "电脑报告：" + t
    if src == "gw" and t.startswith("registered\t"):
        kv = dict(x.split("=", 1) for x in t.split("\t", 1)[1].split() if "=" in x)
        return (f"查看器把端口 {kv.get('port')} 的电脑登记进门卫，叫 {kv.get('name')}" if kv.get("ok") == "1"
                else f"查看器登记端口 {kv.get('port')} 的电脑时从门卫登录没成功：{unb64(kv.get('why64', ''))}")
    return f"{src}：{t}"


def show(evs: list[dict]) -> str:
    return "\n".join(f"  {hm(e['ts'])} {describe(e)}" for e in evs) or "  （还没有任何动静：用户还没运行命令）"


def ccssh(*args, timeout=90) -> tuple[int, str]:
    try:
        p = subprocess.run([CCSSH, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"ccssh {' '.join(args)} 超过 {timeout} 秒没回来"
    except OSError as e:
        return 127, f"运行不了 ccssh（{CCSSH}）：{e}"


def registered() -> dict[str, tuple[str, int]]:
    """门卫里这位用户登记的目标：名字 → (主机, 端口)。"""
    rc, out = ccssh("ls", timeout=30)
    found = {}
    for line in out.splitlines():
        m = re.match(r"(\S+)\t[^\t]*@([^:\t]+):(\d+)", line)
        if m:
            found[m[1]] = (m[2], int(m[3]))
    return found


def listening(port: int) -> bool:
    return bool(subprocess.run(["ss", "-Hltn", f"sport = :{port}"], capture_output=True, text=True).stdout.strip())


def finish(code: int, text: str, seen_path: Path, seen: dict):
    seen_path.write_text(json.dumps(seen))
    print(text)
    sys.exit(code)


def main():
    args = sys.argv[1:]
    if not args or not re.fullmatch(r"[0-9a-f]{32}", args[0]):
        print("用法：wait-pc.py <链接名字> [分钟] 或 wait-pc.py <链接名字> --show", file=sys.stderr)
        sys.exit(2)
    tok = args[0]
    log, seen_path = T / "dl" / f"{tok}.log", T / "dl" / f"{tok}.seen"
    if args[1:] == ["--show"]:
        print(f"链接 {tok} 的进度：\n" + show(events(log)))
        return
    mins = int(args[1]) if len(args) > 1 and args[1].isdigit() else 60
    try:
        seen = json.loads(seen_path.read_text())
    except (FileNotFoundError, ValueError):
        seen = {}
    seen.setdefault("upto", -1)     # 报告过的报错、卡住，记到哪一行
    seen.setdefault("ports", [])    # 报告过接好了的端口
    start, end = time.time(), time.time() + mins * 60
    enrolled_at: dict[int, float] = {}
    listening_at: dict[int, float] = {}

    while True:
        evs = events(log)
        now = time.time()
        new = [e for e in evs if e["i"] > seen["upto"]]
        for e in new:
            if e["src"] == "pc" and (e["text"].startswith("error") or e["text"].startswith("done fail")):
                seen["upto"] = e["i"]
                finish(3, f"电脑那边报错了（{hm(e['ts'])}）：{e['text']}\n整个过程：\n{show(evs)}\n"
                          "照报错跟用户说明原因和怎么办；用户改好以后可以重新运行同一条命令（60 分钟内有效），再起一次本命令接着等。",
                       seen_path, seen)
        # 报到过的电脑：隧道接上了就登记门卫、测通。同一个端口只看最后一次报到
        lasts = {}
        for e in evs:
            if e["src"] == "host" and e["text"].startswith("enrolled\t"):
                lasts[re.search(r"port=(\d+)", e["text"])[1]] = e
        for e in lasts.values():
            kv = dict(x.split("=", 1) for x in e["text"].split("\t", 1)[1].split() if "=" in x)
            port = int(kv["port"])
            if port in seen["ports"]:
                continue
            enrolled_at.setdefault(port, e["ts"])
            if not listening(port):
                if now - enrolled_at[port] > NO_TUNNEL and now - start > 30:
                    seen["upto"] = max(seen["upto"], evs[-1]["i"])
                    finish(5, f"端口 {port} 的电脑 {hm(enrolled_at[port])} 报到了，但过了 {NO_TUNNEL // 60} 分钟隧道还没接上。\n整个过程：\n{show(evs)}\n"
                              f"看电脑报告的最后一步；装好了却没接上，多半是电脑那边连不上中转服务器（没有中转时是 {NAME} 这台机器）的 SSH 端口，请用户确认能上网、再运行一次命令。",
                           seen_path, seen)
                continue
            osname, host = kv.get("os", ""), unb64(kv.get("host64", ""))
            try:
                user = (T / f"{kv.get('label', port)}.user").read_text().strip()
            except FileNotFoundError:
                user = unb64(kv.get("user64", ""))
            # 登记进门卫由查看器做（devices.py，它记一行 gw registered，名字和网页的设备页一致）；
            # 隧道接上 25 秒还没等到（查看器还是旧版），才自己登记
            listening_at.setdefault(port, now)
            # 只认这次报到之后的登记（同一台电脑删掉后用同一个链接重装，会再报到一次）
            reg = next((dict(x.split("=", 1) for x in r["text"].split("\t", 1)[1].split() if "=" in x) for r in reversed(evs)
                        if r["i"] > e["i"] and r["src"] == "gw" and r["text"].startswith("registered\t")
                        and f"port={port} " in r["text"] + " "), None)
            if reg is None and now - listening_at[port] < 25:
                continue
            if reg is not None:
                name = reg.get("name", "")
                if reg.get("ok") != "1":
                    seen["ports"].append(port)
                    seen["upto"] = max(seen["upto"], evs[-1]["i"])
                    finish(6, f"{OSNAME.get(osname, osname)} 电脑 {host} 接上了，登记为 {name}，但从门卫登录没成功：{unb64(reg.get('why64', ''))}\n"
                              f"整个过程：\n{show(evs)}", seen_path, seen)
            else:
                have = registered()
                name = next((n for n, (h, p) in have.items() if h == "127.0.0.1" and p == port), None)
                if not name:
                    base = ALIAS.get(osname, "pc")
                    name = next(n for n in [base] + [f"{base}{k}" for k in range(2, 100)] if n not in have)
                rc, out = ccssh("add", name, f"{user}@127.0.0.1:{port}")
                if rc != 0:
                    seen["ports"].append(port)
                    finish(6, f"电脑 {host} 接上了，但登记进门卫失败：\n{out}", seen_path, seen)
            r = ""
            for _ in range(3):
                rc, r = ccssh(name, "whoami", timeout=40)
                if rc == 0:
                    break
                time.sleep(8)
            seen["ports"].append(port)
            seen["upto"] = max(seen["upto"], evs[-1]["i"])
            done = next((x["text"] for x in reversed(evs) if x["src"] == "pc" and x["text"].startswith("done ok")), "")
            shortcut = m[1] if (m := re.search(r"shortcut=(\S+)", done)) else ""
            if rc != 0:
                finish(6, f"{OSNAME.get(osname, osname)} 电脑 {host} 接上了，登记为 {name}，但从门卫登录没成功：{r}\n整个过程：\n{show(evs)}",
                       seen_path, seen)
            finish(0, f"接好了：{OSNAME.get(osname, osname)} 电脑 {host}（用户 {user}）登记为 {name}，以后用 ssh {name} '<命令>' 操作它"
                      f"（测试 whoami 得到 {r.strip()}）。\n"
                      + (f"用户在这台电脑上用 ssh {shortcut} 就能直达 {NAME}。\n" if shortcut and shortcut != "none" else "")
                      + f"整个过程：\n{show(evs)}", seen_path, seen)
        # 装到一半没了动静
        pcs = [e for e in evs if e["src"] == "pc"]
        if pcs and pcs[-1]["i"] > seen["upto"] and not pcs[-1]["text"].startswith("done") and now - pcs[-1]["ts"] > STUCK:
            seen["upto"] = evs[-1]["i"]
            last = pcs[-1]["text"]
            hint = "多半是在等用户在弹出的窗口里点「是」（用户账号能管理这台电脑才行）" if last.startswith("elevate") else "请用户看看电脑上的窗口停在哪一行、有没有提示"
            finish(4, f"电脑装到一半 {STUCK // 60} 分钟没动静了，停在：{last}（{hm(pcs[-1]['ts'])}）。{hint}。\n整个过程：\n{show(evs)}",
                   seen_path, seen)
        if now > end:
            seen["upto"] = max(seen["upto"], evs[-1]["i"] if evs else -1)
            finish(1, f"等了 {mins} 分钟还没有结果。到现在的过程：\n{show(evs)}", seen_path, seen)
        time.sleep(3)


if __name__ == "__main__":
    main()
