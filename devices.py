"""网页的「设备」页：每个用户自己的设备列表，看得见、管得了，不用非得跟 AI 说。

起因：添加设备是人人都会碰到的事，却只能在对话里让 Claude 跑命令，
中间哪一步出了问题用户和 AI 都看不见；很多用户不熟悉电脑和网络，命令行让人害怕，而 VS Code 远程、Codex 桌面版这类
「添加设备」的界面他们早就会用。所以给每人一个设备列表：连了哪些设备、在不在线、Claude 最近在上面做了什么，能添加、改名、
暂停访问、删除。跟 Claude 说一句话也照样能连，两条路用的是同一份数据：
- 设备列表就是 ssh 门卫（sshgw.py）里这个人的登记，kind、os、hostname、added、note、paused 这几样说明也记在那里；
- 添加电脑的进度就是装电脑链接的进度记录（Claude 那边 ~/tunnels/dl/<链接名>.log：查看器记下载和电脑报的进度、
  pc-enroll.py 记报到，这里记登记结果），所以 Claude 在对话里看到的（wait-pc.py）和页面上显示的是同一回事；
- 电脑报到、隧道接上以后由这里统一登记进门卫（tick，后台循环每两秒踢一下），名字按系统起：win、mac、linux，重名加数字，
  重装的电脑沿用原名。对话那条路的 wait-pc.py 等它登记好再测通、叫醒 Claude（等不到才自己登记，兼容旧版查看器）。
页面上出错时可以一键开个新对话把出错的链接交给 Claude；Claude 在对话里给的链接（#dev=<链接名>）点开就是同一个添加窗口。
只在开了门卫（instance.json 的 gw_socket）的实例里有，没开的页面上不给入口。
电脑在本机上的端口从 23001 起（见添加电脑的安装脚本），是不是在线就看这个端口有没有在监听（谁都能用 ss 看）。
"""
import base64
import gzip
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import hostfs
import sshgw
import users
from instance import DATA, GW_SOCKET

ENABLED = bool(GW_SOCKET)
T = hostfs.HOME / "tunnels"
SEEN = DATA / "gw" / "seen.json"   # 端口 → 电脑最后一次被看到在线的时刻（后台每两秒看一次，变化超过一分钟才写）
FIRST_PORT = 23001
TOK = re.compile(r"[0-9a-f]{32}")
ACTIVE_SECS = 3 * 3600   # 生成多久以内的链接还要盯着：报到、登记（下载 60 分钟内有效，装还要一阵）
ALIAS = {"windows": "win", "mac": "mac", "linux": "linux"}
OS_NAME = {"windows": "Windows", "mac": "Mac", "linux": "Linux"}
STEPS = ["运行命令", "安装 SSH 服务", "允许 Claude 登录", "登记这台电脑", "建立连接"]
_seen: dict[str, float] = {}
_seen_saved: dict[str, float] = {}
_tick_lock = threading.Lock()
_retry: dict[tuple[str, int], tuple[int, float]] = {}   # (链接, 端口) → (登记试了几次, 上次的时刻)


class DeviceError(Exception):
    """说给用户听的错（接口回 400）。"""


def is_pc(t: dict) -> bool:
    return t.get("kind") == "pc" or (t.get("host") == "127.0.0.1" and int(t.get("port") or 0) >= FIRST_PORT)


def listening() -> set[int]:
    """本机正在监听的端口（只看 127.0.0.1 上的：电脑的隧道都挂在这里）。"""
    out = subprocess.run(["ss", "-Hltn"], capture_output=True, text=True, timeout=10).stdout
    ports = set()
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4 and f[3].startswith("127.0.0.1:") and f[3].rsplit(":", 1)[1].isdigit():
            ports.add(int(f[3].rsplit(":", 1)[1]))
    return ports


def _load_seen():
    global _seen, _seen_saved
    if not _seen_saved:
        try:
            _seen = json.loads(SEEN.read_text())
        except (FileNotFoundError, ValueError):
            _seen = {}
        _seen_saved = dict(_seen)


def _unb64(s: str) -> str:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
    except ValueError:
        return "?"


# ---------- 装电脑的链接 ----------

def ticket(tok: str) -> dict:
    """链接的票（make-pc-setup.sh 写的 dl/<名字>.ticket）：谁的、几点过期、给用户的两条命令。没有就是 {}。"""
    try:
        text = (T / "dl" / f"{tok}.ticket").read_text()
    except FileNotFoundError:
        return {}
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)


def events(tok: str) -> list[dict]:
    """进度记录的每一行：时刻、谁记的（web 查看器、pc 电脑、host 报到、gw 登记）、内容。"""
    try:
        text = (T / "dl" / f"{tok}.log").read_text(errors="replace")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        f = line.split("\t", 2)
        if len(f) == 3 and f[0].isdigit():
            out.append({"ts": int(f[0]), "src": f[1], "text": f[2]})
    return out


def _kv(text: str) -> dict:
    return dict(x.split("=", 1) for x in text.split() if "=" in x)


def note_event(tok: str, kind: str, text: str):
    with (T / "dl" / f"{tok}.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.time():.0f}\tgw\t{kind}\t{text}\n")


# 电脑报上来的错是英文的（电脑的控制台不一定显示得了中文）：常见的几种换成大白话，告诉用户怎么办
EXPLAIN = [
    (r"administrator permission was not given", "未获得管理员权限：Windows 询问「是否允许此应用对你的设备进行更改」时请点击「是」；"
                                                 "如果要求输入密码，说明当前账号无权管理这台电脑，请使用管理员账号登录 Windows 后重试。"),
    (r"could not reach the server", "这台电脑连不上查看器所在的服务器（或中转服务器）的 SSH 端口：请确认电脑可以上网；"
                                           "部分校园网、公司网会拦截该端口，可更换网络（例如手机热点）后重试。"),
    (r"Remote Login is still off", "这台 Mac 未开启「远程登录」：请在「系统设置 → 通用 → 共享」中开启「远程登录」，然后再次运行命令。"),
    (r"Windows is busy installing", "Windows 正在安装其他程序（通常是系统更新），请等待几分钟后再次运行命令。"),
    (r"has expired|does not exist", "此链接已过期（有效期 60 分钟），请重新点击「添加设备」获取新命令。"),
    (r"already been used for \d+ computers", "此命令已用于安装多台电脑，请重新点击「添加设备」获取新命令。"),
    (r"did not come up within a minute", "安装已完成，但一分钟内未能连上查看器所在的服务器：通常是网络问题，请确认电脑可以上网后再次运行命令。"),
    (r"msiexec exit code", "SSH 服务安装失败：请重启电脑后再次运行命令；如仍失败，请点击「请 Claude 排查」。"),
    (r"no known package manager|ssh client is not installed", "这台电脑缺少必要的系统组件，请点击「请 Claude 排查」。"),
    (r"is not running on port", "SSH 服务未能启动，请点击「请 Claude 排查」。"),
]


def explain(text: str) -> str:
    return next((tip for pat, tip in EXPLAIN if re.search(pat, text)), "")


def describe(e: dict) -> str:
    """进度记录的一行，写成给人看的中文。"""
    t, src = e["text"], e["src"]
    if src == "web":
        kind, _, rest = t.partition("\t")
        os_, _, ip = rest.partition(" ")
        return {"download": f"下载了 {os_} 安装脚本（从 {ip}）", "expired": f"想下载 {os_} 安装脚本，但链接已经过期了"}.get(kind, t)
    if src == "host" and t.startswith("enrolled\t"):
        kv = _kv(t.split("\t", 1)[1])
        return (f"{OS_NAME.get(kv.get('os'), kv.get('os'))} 电脑 {_unb64(kv.get('host64', ''))}（用户 {_unb64(kv.get('user64', ''))}）登记好了"
                + ("，以前装过，沿用原来的连接" if kv.get("new") == "0" else ""))
    if src == "gw" and t.startswith("registered\t"):
        kv = _kv(t.split("\t", 1)[1])
        return (f"连通了，在设备列表里叫「{kv.get('name')}」" if kv.get("ok") == "1"
                else f"隧道接上了，但从查看器所在的服务器登录这台电脑没成功：{_unb64(kv.get('why64', ''))}")
    if src == "pc":
        if t.startswith("start "):
            return f"{OS_NAME.get((t.split() + [''])[1], '')} 电脑上开始安装".lstrip()
        if m := re.match(r"step ([1-4])/4", t):   # 电脑报的第 1～4 步就是页面上五步里的后四步，不写序号免得对不上
            return f"开始「{STEPS[int(m[1])]}」"
        if t.startswith("elevate"):
            return "等待用户在弹出的窗口中点击「是」（授予管理员权限）"
        if t.startswith("done ok"):
            return "电脑端安装已结束"
        if t.startswith("error"):
            return f"出错：{t}"
        if t.startswith("note"):
            return f"提示：{t[5:]}"
    return t


def link_state(tok: str, user: str) -> dict:
    """一个链接装到哪了：给添加窗口用。不是这个人的链接不给看。"""
    if not TOK.fullmatch(tok):
        raise DeviceError("链接名字不对")
    tk = ticket(tok)
    if not tk:
        raise DeviceError("没有这个安装链接（一天以前的会被清掉）")
    if tk.get("who") != sshgw.tag(user):
        raise DeviceError("这个安装链接不是你的")
    evs = events(tok)
    now = time.time()
    # os：最近在装的是什么系统（windows、mac、linux；只知道下载了 Mac/Linux 那份时是 unix），重新打开窗口时命令先显示这一种
    st = {"tok": tok, "expires": float(tk.get("expires") or 0), "win": tk.get("win", ""), "unix": tk.get("unix", ""),
          "from": tk.get("from", "chat"), "events": [{"ts": e["ts"], "text": describe(e), "src": e["src"]} for e in evs],
          "step": -1, "error": "", "tip": "", "stuck": 0, "elevate": False, "downloaded": False, "os": "", "machines": []}
    machines: dict[int, dict] = {}
    last_pc = None
    for e in evs:
        t = e["text"]
        if e["src"] == "web" and t.startswith("download"):
            st["downloaded"] = True
            st["step"] = max(st["step"], 0)
            st["os"] = "windows" if "\tWindows" in t else st["os"] if st["os"] in ("mac", "linux") else "unix"
        elif e["src"] == "pc":
            last_pc = e
            if t.startswith("start "):
                st.update(error="", tip="", elevate=False, step=max(st["step"], 0))
                if (os_ := (t.split() + [""])[1]) in OS_NAME:   # 电脑经公网报上来的，缺字段、乱写也不能出错
                    st["os"] = os_
            elif m := re.match(r"step ([1-4])/4", t):
                st["step"] = max(st["step"], int(m[1]))
            elif t.startswith("elevate"):
                st["elevate"] = True
            elif t.startswith("error") or t.startswith("done fail"):
                st["error"], st["tip"] = t, explain(t)
        elif e["src"] == "host" and t.startswith("enrolled\t"):
            kv = _kv(t.split("\t", 1)[1])
            port = int(kv["port"])
            machines[port] = {"port": port, "os": kv.get("os"), "host": _unb64(kv.get("host64", "")),
                              "user": _unb64(kv.get("user64", "")), "ts": e["ts"], "name": "", "ok": None, "why": ""}
            st["step"] = max(st["step"], 3)
        elif e["src"] == "gw" and t.startswith("registered\t"):
            kv = _kv(t.split("\t", 1)[1])
            if (mach := machines.get(int(kv.get("port", 0)))) is not None:
                mach.update(name=kv.get("name", ""), ok=kv.get("ok") == "1", why=_unb64(kv.get("why64", "")))
    on = listening()
    for mach in machines.values():
        mach["online"] = mach["port"] in on
    st["machines"] = sorted(machines.values(), key=lambda x: x["ts"])
    # 页面上那五步（STEPS）现在走到第几步：0 等用户运行命令，1～3 电脑报的第几步，4 报到好了、在等连通，5 全好了
    st["cur"] = (5 if any(m["ok"] for m in machines.values()) else 4 if machines
                 else max(st["step"], 1 if st["downloaded"] else 0))
    # 电脑上的安装停在半路、几分钟没有新动静：页面上提醒（等管理员确认时尤其常见）
    if last_pc and not st["error"] and not last_pc["text"].startswith("done") and not any(m["ok"] for m in machines.values()):
        st["stuck"] = int(now - last_pc["ts"]) if now - last_pc["ts"] > 180 else 0
    return st


def create_link(user: str) -> dict:
    """在 Claude 那边生成一个装电脑的链接（make-pc-setup.sh），交回链接名字和给用户的两条命令。"""
    rc, out, err = hostfs.run(["bash", str(T / "make-pc-setup.sh"), "--pub", sshgw.pubkey(user), "--from", "web", "--machine"],
                              timeout=60)
    if rc != 0:
        raise DeviceError(f"生成安装链接失败（退出码 {rc}）：{(err or out).decode(errors='replace').strip()[-500:]}")
    kv = dict(l.split("=", 1) for l in out.decode().splitlines() if "=" in l)
    if not TOK.fullmatch(kv.get("tok", "")):
        raise DeviceError(f"生成安装链接的脚本回的东西看不懂：{out.decode(errors='replace')[:300]}")
    return link_state(kv["tok"], user)


def active_links() -> list[tuple[str, dict]]:
    """还要盯着的链接：生成不到三小时的。"""
    out = []
    now = time.time()
    try:
        files = list((T / "dl").glob("*.ticket"))
    except FileNotFoundError:
        return out
    for p in files:
        tok = p.name[:-len(".ticket")]
        if not TOK.fullmatch(tok):
            continue
        tk = ticket(tok)
        if tk and now - float(tk.get("created") or 0) < ACTIVE_SECS:
            out.append((tok, tk))
    return out


def owner_of_tag(tag: str) -> str | None:
    for n in users.names():
        if sshgw.tag(n) == tag:
            return n
    return None


def register(tok: str, tk: dict, ev: dict, on: set[int]):
    """报到过、隧道也接上了的电脑：登记进它主人的门卫，试连一次，结果记进进度记录。"""
    kv = _kv(ev["text"].split("\t", 1)[1])
    port = int(kv["port"])
    owner = owner_of_tag(tk.get("who", ""))
    if not owner:
        return
    tries, last = _retry.get((tok, port), (0, 0.0))
    if tries and time.time() - last < 20:
        return
    mine = sshgw.targets(owner)
    name = next((n for n, x in mine.items() if x.get("host") == "127.0.0.1" and int(x.get("port") or 0) == port), None)
    if not name:
        base = ALIAS.get(kv.get("os", ""), "pc")
        name = next(n for n in [base] + [f"{base}{k}" for k in range(2, 100)] if n not in mine)
    try:
        login = (T / f"{kv.get('label', port)}.user").read_text().strip()
    except FileNotFoundError:
        login = _unb64(kv.get("user64", ""))
    rc, text, ok = sshgw.add_target(owner, name, f"{login}@127.0.0.1:{port}", None,
                                    {"kind": "pc", "os": kv.get("os"), "hostname": _unb64(kv.get("host64", "")),
                                     "added": tk.get("from", "chat")}, who="自动登记·登记时试连")
    _retry[(tok, port)] = (tries + 1, time.time())
    if ok or rc == 2 or tries + 1 >= 4:   # 连不上的多试几次（电脑上的 SSH 服务可能刚起来）
        m = re.search(r"还连不上（(.*?)）。", text)
        why = "" if ok else m[1] if m else text.strip().splitlines()[0]
        note_event(tok, "registered", f"port={port} name={name} ok={int(ok)}"
                   + (f" why64={base64.urlsafe_b64encode(why.encode()).decode().rstrip('=')}" if why else ""))
        print(f"设备：{owner} 的电脑（端口 {port}）登记为 {name}，{'连上了' if ok else '没连上：' + why}", flush=True)


def tick():
    """后台循环每两秒踢一下（在自己的线程里跑，上一次没跑完就跳过）：记电脑最后在线的时刻，登记报到好了的电脑。"""
    if not ENABLED or not _tick_lock.acquire(blocking=False):
        return
    try:
        on = listening()
        now = time.time()
        _load_seen()
        for owner, mine in sshgw.everyone().items():
            for t in mine.values():
                if is_pc(t) and int(t["port"]) in on:
                    _seen[str(t["port"])] = now
        if any(now - _seen_saved.get(k, 0) > 60 for k in _seen):
            SEEN.parent.mkdir(parents=True, exist_ok=True)
            SEEN.write_text(json.dumps(_seen))
            _seen_saved.clear()
            _seen_saved.update(_seen)
        for tok, tk in active_links():
            # 每个端口最后一次报到之后还没登记过的才登记（同一台电脑删掉以后用同一个链接重装，会再报到一次）
            last: dict[int, dict] = {}
            for e in events(tok):
                if e["src"] == "host" and e["text"].startswith("enrolled\t"):
                    last[int(_kv(e["text"].split("\t", 1)[1]).get("port", 0))] = e
                elif e["src"] == "gw" and e["text"].startswith("registered\t"):
                    last.pop(int(_kv(e["text"].split("\t", 1)[1]).get("port", 0)), None)
            for port, e in last.items():
                if port in on:
                    register(tok, tk, e, on)
    except Exception as e:   # 后台的事出错只记日志，别把后台循环带停
        print(f"设备页后台出错：{type(e).__name__}: {e}", flush=True)
    finally:
        _tick_lock.release()


def kick():
    threading.Thread(target=tick, daemon=True, name="devices-tick").start()


# ---------- 设备列表和对设备的操作 ----------

def overview(user: str) -> dict:
    mine = sshgw.targets(user)
    on = listening()
    _load_seen()
    last: dict[str, dict] = {}
    for r in sshgw.history(user, None, 2000):
        last[r["target"]] = r
    devices = []
    for name, t in sorted(mine.items()):
        pc = is_pc(t)
        d = {"name": name, "kind": "pc" if pc else "server", "os": t.get("os"), "hostname": t.get("hostname"),
             "login": t["user"], "host": t["host"], "port": t["port"], "via": t.get("via"), "paused": bool(t.get("paused")),
             "note": t.get("note", ""), "added": t.get("added"), "ts": t.get("ts"), "oldKey": bool(t.get("key"))}
        if pc:
            d["online"] = int(t["port"]) in on
            d["seen"] = time.time() if d["online"] else _seen.get(str(t["port"]))
        if r := last.get(name):
            d["last"] = {"ts": r["ts"], "exit": r["exit"], "cmd": r["cmd"][:200], "who": r.get("session", "")}
        devices.append(d)
    pending = []
    for tok, tk in active_links():
        if tk.get("who") != sshgw.tag(user) or time.time() > float(tk.get("expires") or 0) + 1800:
            continue
        st = link_state(tok, user)
        if st["downloaded"] and not any(m["ok"] for m in st["machines"]):   # 有人运行了、还没装好的
            pending.append({k: st[k] for k in ("tok", "cur", "error", "tip", "stuck", "elevate", "from")})
    return {"enabled": True, "devices": devices, "pending": pending, "pubkey": sshgw.pubkey(user)}


def target(user: str, name: str) -> dict:
    t = sshgw.targets(user).get(name)
    if t is None:
        raise DeviceError(f"你没有叫 {name} 的设备")
    return t


# 查看器自己经门卫连的（测试连接、登记时试连、删除时卸载……）记账时会话那一栏写「来源·动作」，
# Claude 连的写会话编号：页面靠这个把两种分开显示（devices.js 的 histWho）
def test(user: str, name: str) -> dict:
    t = target(user, name)
    if why := sshgw.paused_why(sshgw.targets(user), name):
        raise DeviceError(why)
    rc, out, err = sshgw.run_capture(user, name, "whoami", "设备页·测试连接", timeout=40)
    ok = rc == 0
    return {"ok": ok, "text": f"连上了，对面是 {out.strip()}" if ok else
            f"没连上（退出码 {rc}）：{(err.strip().splitlines() or ['没有说原因'])[-1]}"
            + ("。电脑不在线：确认它开着、能上网" if is_pc(t) and int(t["port"]) not in listening() else "")}


def rename(user: str, name: str, new: str) -> str:
    target(user, name)
    if new.strip() in ("links", "servers"):
        raise DeviceError(f"{new.strip()} 这个名字设备页要用，换一个")
    try:
        return sshgw.rename_target(user, name, new.strip())
    except ValueError as e:
        raise DeviceError(str(e))


def set_paused(user: str, name: str, paused: bool) -> dict:
    target(user, name)
    return sshgw.set_meta(user, name, paused=True if paused else None)


def set_note(user: str, name: str, note: str) -> dict:
    target(user, name)
    if len(note) > 200:
        raise DeviceError("备注最多 200 个字")
    return sshgw.set_meta(user, name, note=note.strip() or None)


def history(user: str, name: str) -> list[dict]:
    target(user, name)
    return [{"ts": r["ts"], "cmd": r["cmd"][:500], "exit": r["exit"], "secs": r.get("secs"), "who": r.get("session", "")}
            for r in reversed(sshgw.history(user, name, 100))]


SERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")


def add_server(user: str, name: str, login: str, host: str, port: int, via: str | None) -> dict:
    """登记一台服务器（用户已经把公钥加到那边了），试连一次。连不上也登记着，告诉用户为什么。"""
    name, login, host = name.strip(), login.strip(), host.strip()
    if name in ("links", "servers"):   # 和接口的路径撞名
        raise DeviceError(f"{name} 这个名字设备页要用，换一个")
    if name in sshgw.targets(user):
        raise DeviceError(f"已经有一个叫 {name} 的设备了，换个名字，或者先删掉它")
    rc, text, ok = sshgw.add_target(user, name, f"{login}@{host}:{int(port)}", via or None, {"kind": "server", "added": "web"},
                                    who="设备页·登记时试连")
    if rc == 2:
        raise DeviceError(text.strip())
    return {"ok": ok, "text": web_text(text)}


def web_text(text: str) -> str:
    """门卫登记时说的话是写给 ccssh 用户的（「再用 ccssh … 试」、公钥），页面上只留连没连上和原因。"""
    lines = text.strip().splitlines()
    return "\n".join(l.split("。如果是还没装公钥")[0] + ("。" if "。如果是还没装公钥" in l else "") for l in lines[:2])


def edit_server(user: str, name: str, login: str, host: str, port: int, via: str | None) -> dict:
    t = target(user, name)
    if is_pc(t):
        raise DeviceError("电脑的地址是安装时定的，不能改；要换就删掉重新添加")
    rc, text, ok = sshgw.add_target(user, name, f"{login.strip()}@{host.strip()}:{int(port)}", via or None,
                                    who="设备页·改地址后试连")
    if rc == 2:
        raise DeviceError(text.strip())
    return {"ok": ok, "text": web_text(text)}


def _files(name: str) -> str:
    return (T / "files" / name).read_text()


def _uninstall_cmd(osname: str) -> str:
    """在电脑上卸掉装电脑脚本装的东西：卸载会掐断它自己来的这条隧道，所以放到后台、等三秒再动手，命令先回来。
    Unix 把卸载脚本经 base64 带过去；Windows 的远程 shell 可能是 cmd（一行最多 8191 个字），所以压缩了再带，
    经 WMI 起一个不在 ssh 会话里的进程（ssh 会话结束时会收掉会话里起的进程），以管理员身份卸。"""
    if osname == "windows":
        packed = base64.b64encode(gzip.compress(_files("uninstall.ps1").encode())).decode()
        boot = ("$b=[Convert]::FromBase64String('" + packed + "');"
                "$z=New-Object IO.Compression.GzipStream((New-Object IO.MemoryStream(,$b)),[IO.Compression.CompressionMode]::Decompress);"
                "$s=(New-Object IO.StreamReader($z)).ReadToEnd();"
                "$t=Join-Path $env:TEMP ('ccviewer-uninstall-'+[guid]::NewGuid().ToString('N')+'.ps1');"
                "[IO.File]::WriteAllText($t,('$UserProfileDir='''+$env:USERPROFILE.Replace('''','''''')+''''+[char]10+'$Delay=3'+[char]10+$s));"
                "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='powershell.exe -NoProfile "
                "-NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File \"'+$t+'\"'};"
                "if($r.ReturnValue -eq 0){'started'}else{'could not start: '+$r.ReturnValue}")
        return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(boot.encode("utf-16-le")).decode()
    packed = base64.b64encode(_files("uninstall.sh").encode()).decode()
    # Mac 没有 setsid，只用 nohup（ssh 会话结束时发的挂断信号它不理）
    return (f"f=$(mktemp) && echo {packed} | base64 --decode > \"$f\" && "
            "{ if command -v setsid >/dev/null 2>&1; then CCV_UNINSTALL_DELAY=3 setsid nohup bash \"$f\"; "
            "else CCV_UNINSTALL_DELAY=3 nohup bash \"$f\"; fi; } >/dev/null 2>&1 < /dev/null & echo started")


def remove(user: str, name: str, cleanup: bool) -> dict:
    """删除设备。电脑：在线就先经门卫卸掉电脑上装的东西，再吊销本机上它的钥匙（pc-remove.py），它再连回来也进不来；
    别人（或自己的另一个名字）也登记着同一台电脑时只删这个名字。服务器：cleanup 时顺手删掉服务器上门卫的公钥（Linux、Mac 才行）。
    最后删门卫里的登记。返回每一步的结果，前端照着列出来。"""
    t = target(user, name)
    steps = []
    others = [(u, n) for u, mine in sshgw.everyone().items() for n, x in mine.items()
              if (u, n) != (user, name) and x.get("host") == t["host"] and int(x.get("port") or 0) == int(t["port"])]
    if is_pc(t):
        port = int(t["port"])
        if others:
            steps.append({"ok": True, "text": "这台电脑还以其他名称登记，仅删除此名称，电脑上的组件和密钥均保留"})
        else:
            if cleanup and port in listening():
                osname = t.get("os")
                if not osname:   # 改版以前登记的电脑不知道是什么系统：Linux、Mac 上 uname 能跑，Windows 上不能
                    rc, out, _ = sshgw.run_capture(user, name, "uname -s", "设备页·删除前看系统", timeout=30)
                    osname = "mac" if rc == 0 and "Darwin" in out else "linux" if rc == 0 else "windows"
                try:
                    rc, out, err = sshgw.run_capture(user, name, _uninstall_cmd(osname), "设备页·删除时卸载", timeout=60)
                    ok = rc == 0 and "started" in out
                    steps.append({"ok": ok, "text": "电脑上的连接程序、钥匙和配置已经开始卸载（几秒后断开）" if ok else
                                  f"没能卸掉电脑上的东西（退出码 {rc}）：{(err or out).strip()[-300:]}；可以以后在电脑上运行卸载命令"})
                except OSError as e:
                    steps.append({"ok": False, "text": f"无法读取卸载脚本：{e}"})
            elif cleanup:
                steps.append({"ok": False, "text": "电脑当前离线，无法卸载其上的组件；之后可在该电脑上运行下方的卸载命令"})
            rc, out, err = hostfs.run(["python3", str(T / "pc-remove.py"), str(port)], timeout=30)
            steps.append({"ok": rc == 0, "text": "本机上这台电脑的钥匙吊销了，它再也连不上来" if rc == 0 else
                          f"吊销本机上这台电脑的钥匙失败（退出码 {rc}）：{(err or out).decode(errors='replace').strip()[-300:]}"})
    elif cleanup:
        same = [n for u, n in others if u == user]
        if t.get("key"):
            steps.append({"ok": True, "text": "这台服务器使用的是迁移过来的旧密钥，服务器上没有需要删除的门卫公钥"})
        elif same:
            steps.append({"ok": True, "text": f"您的 {'、'.join(same)} 也连接这台服务器，已保留门卫公钥"})
        elif sshgw.paused_why(sshgw.targets(user), name):
            steps.append({"ok": False, "text": "该设备处于暂停访问状态，未删除服务器上的公钥"})
        else:
            blob = " ".join(sshgw.pubkey(user).split()[:2])
            # grep 读写出错（退出码 2）时临时文件可能是半截的，那时绝不能拿它盖掉 authorized_keys；
            # 不是 Linux、Mac（uname 跑不了）就什么都不动
            cmd = ("f=~/.ssh/authorized_keys; uname -s >/dev/null 2>&1 && [ -f \"$f\" ] && "
                   f"{{ grep -v -F '{blob}' \"$f\" > \"$f.ccviewertmp\"; r=$?; "
                   "if [ $r -le 1 ]; then cat \"$f.ccviewertmp\" > \"$f\" && echo removed; fi; rm -f \"$f.ccviewertmp\"; }")
            rc, out, err = sshgw.run_capture(user, name, cmd, "设备页·删除时去掉公钥", timeout=40)
            ok = rc == 0 and "removed" in out
            steps.append({"ok": ok, "text": "服务器上门卫的公钥删掉了" if ok else
                          f"没删掉服务器上门卫的公钥（{(err or out).strip()[-200:] or f'退出码 {rc}'}），要的话自己删掉 ~/.ssh/authorized_keys 里结尾是 "
                          f"{sshgw.pubkey(user).split()[-1]} 的那一行"})
    rc, text = sshgw.remove_target(user, name)
    if rc:
        raise DeviceError(text.strip())
    steps.append({"ok": True, "text": f"已从设备列表中删除 {name}"})
    return {"steps": steps}
