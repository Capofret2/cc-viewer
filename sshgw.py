"""ssh 门卫：网页对话里的 Claude 经它 ssh 到会话主人自己登记的电脑和服务器，别人的连不上。

为什么要它：多用户实例里大家的 Claude 都是同一个系统用户（instance.RUN_AS），钥匙放在这个用户手里，谁的 Claude 都拿得到、连得上别人的电脑。
门卫在查看器里（查看器可以以另一个系统用户运行，见 hostfs.py），钥匙只在它的数据目录里，RUN_AS 碰不到：
- 认人：Claude 在会话里执行 ccssh（同目录的客户端），连到本机的 Unix 套接字（instance.GW_SOCKET，不配就不开）。
  内核报来连接进程的进程号（SO_PEERCRED，伪造不了），门卫看它在不在哪个网页对话的 CLI 底下（chat.cli_pids，父子关系也由内核保证），
  就知道是哪个会话、会话主人是谁（users.owner_of）。不在任何网页对话底下的（终端里、别处起的）一律不认；无主会话也不认。
- 认目标：每人自己登记目标（ccssh add 名字 用户@主机[:端口] [--via 跳板]），记在数据目录的 gw/targets.json，只有登记的人能用。
  每人一把钥匙（gw/keys/，第一次用时生成），公钥由本人装到自己的目标上；所以登记了别人的地址也没用，那边没装他的公钥。
  主机指纹第一次连上时记下（gw/known/，每人一份），以后变了就连不上。
- 代连：ssh 的参数全由门卫自己写（每次按登记生成一份配置，用 -F 只读它，系统和用户的 ssh 配置都不读），Claude 只交过来
  「哪个目标」和「执行什么命令」两样，带不进任何 ssh 选项（ProxyCommand 这类会在本机执行命令的选项尤其不行）。
  ccssh 的标准输入、输出、错误三个文件描述符随请求交过来（SCM_RIGHTS），ssh 直接用它们，输出和退出码和直接 ssh 一样；
  ccssh 被打断（连接断了）就收掉 ssh。不开转发、不转代理（-L/-R/-D/-A 都没有）。
- 传文件、挂载：rsync -e ccssh、scp -S ccssh、ccssh mount（sshfs 用 ccssh 当 ssh 程序）。它们自己加的那几个固定选项 ccssh 认得、不往下传；
  scp -s 和 sshfs 要的是 sftp 子系统，交过来的是 op=sftp。挂载在会话结束后还留着，sshfs 断线重连时已经不在网页会话底下，
  所以会话里第一次挂载时把那个 sshfs 进程（进程号 + 启动时间）记在会话主人名下（gw/mounts.json），以后它起的 sftp 请求认作同一个人、只许连同一个目标。
- 记账：每次连接记一行到 gw/log.jsonl（谁、哪个会话、哪个目标、什么命令、退出码、多久）。本人用 ccssh log 看自己的，查看器账号加 --all 看全部。
防不住的：大家的 Claude 仍是同一个 RUN_AS，改得了大家共用的文件（比如 ~/.bashrc），可以让别人的 Claude 在不知情时替它执行 ccssh；
会话记录里的输出别人也看得到。彻底隔开要每人一个系统用户。
"""
import array
import contextlib
import hashlib
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import threading
import time
import traceback
from pathlib import Path

import users
import workdev   # 会话勾选的工作设备：没勾上的不放行（见 workdev.py）
from instance import ADMINS, DATA, GW_SOCKET, MULTI

DIR = DATA / "gw"
TARGETS = DIR / "targets.json"   # 用户名 → {目标名: {host, port, user, via, ts}}
LOG = DIR / "log.jsonl"
MOUNTS = DIR / "mounts.json"     # sshfs 进程号 → 挂载它的人和目标（见 remember_mount）
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
RESERVED = {"add", "rm", "ls", "key", "log", "mount", "umount", "help"}   # ccssh 的子命令，不能拿来当目标名
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
LOGIN = re.compile(r"[^\s\"'\\@:%][^\"'\\@:%\n\r\t]{0,63}")   # Windows 的用户名可以有空格、中文；配置里加引号写
MAX_CMD = 100_000
# 电脑连接可能和查看器共用中转带宽，单个大文件传输不能占满链路；每条连接两个方向各限 20 Mbps（gw_throttle.py）。
PC_RATE = 20_000_000 // 8
THROTTLE = Path(__file__).resolve().parent / "gw_throttle.py"
lock = threading.Lock()
drivers = lambda: []   # noqa: E731  server 设成「现在所有的网页对话驱动」
_sock: socket.socket | None = None


def say(fd: int, text: str):
    with contextlib.suppress(OSError):
        os.write(fd, text.encode())


def _load() -> dict:
    try:
        return json.loads(TARGETS.read_text())
    except FileNotFoundError:
        return {}


def _save(data: dict):
    tmp = TARGETS.with_name(f".targets.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(TARGETS)


def _tag(user: str) -> str:
    """用户名可能有中文，文件名用它的散列。"""
    return hashlib.sha1(user.encode()).hexdigest()[:12]


def key_of(user: str) -> Path:
    """这个人的钥匙，没有就生成一把（ed25519，无口令：门卫要替他自动连）。"""
    path = DIR / "keys" / _tag(user)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"cc_viewer-gw-{_tag(user)}", "-f", str(path)],
                       check=True, capture_output=True, timeout=20)
    return path


def who_sent(pid: int) -> tuple[str, str | None]:
    """连接进程在哪个网页对话的 CLI 底下：(会话编号, 会话主人)。不在任何一个底下就报错。"""
    import chat   # 只有对话模式才有驱动，也只有对话模式才开门卫
    me = _proc(pid)
    for d in list(drivers()):
        if d.client and {pid, me and me[1]} & set(chat.cli_pids(d.client)):
            # 自己在 CLI 底下，或者所在会话的组长在 CLI 底下：sshfs 起 ssh 程序时多分叉一层让它脱离自己（父进程变成 1），
            # 但会话编号是继承的，组长就是 ccssh mount 起的那个 sshfs
            return d.session_id, users.owner_of(d.session_id)
    raise PermissionError("ccssh 只能在网页上开的会话里使用（认不出这条命令是哪个会话发出来的）")


def config_for(user: str, name: str, mine: dict) -> Path:
    """按登记生成这次连接用的 ssh 配置：目标和它一路的跳板，外加一律要有的那些限制。只读这一份（-F）。"""
    hosts, todo = [], [name]
    while todo:
        n = todo.pop()
        if n in (h for h, _ in hosts):
            raise ValueError(f"跳板绕成了圈：{n}")
        t = mine[n]
        hosts.append((n, t))
        if t.get("via"):
            if t["via"] not in mine:
                raise ValueError(f"{n} 的跳板 {t['via']} 没有登记")
            todo.append(t["via"])
        if len(hosts) > 4:
            raise ValueError("跳板最多三层")
    known = DIR / "known" / _tag(user)
    known.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    import devices   # 它在模块顶上引用本模块，放这里免得互相引用
    lines = []
    for n, t in hosts:
        # key：从别处收编来的目标用它原来那把钥匙（对方只装了那一把）
        # 添加的电脑连接本机反向端口；主机和端口在登记时校验过，这里由门卫自己写进命令。
        slow = not t.get("via") and devices.is_pc(t)
        lines += [f"Host {n}", f"  HostName {t['host']}", f"  Port {int(t['port'])}", f"  User \"{t['user']}\"",
                  f"  HostKeyAlias gw-{n}"] + ([f"  ProxyJump {t['via']}"] if t.get("via") else []) \
            + ([f"  ProxyCommand /usr/bin/python3 -I {THROTTLE} {t['host']} {int(t['port'])} {PC_RATE}"] if slow else []) \
            + ([f"  IdentityFile {DIR / 'keys' / t['key']}"] if t.get("key") else [])
    lines += ["Host *", f"  IdentityFile {key_of(user)}", "  IdentitiesOnly yes", f"  UserKnownHostsFile {known}",
              "  GlobalKnownHostsFile /dev/null", "  StrictHostKeyChecking accept-new", "  UpdateHostKeys no",
              "  BatchMode yes", "  ForwardAgent no", "  ForwardX11 no", "  ClearAllForwardings yes", "  PermitLocalCommand no",
              "  ControlMaster no", "  ControlPath none", "  ConnectTimeout 15", "  ServerAliveInterval 30",
              "  ServerAliveCountMax 4", "  LogLevel ERROR"]
    path = DIR / "cfg" / f"{_tag(user)}-{threading.get_ident()}"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text("\n".join(lines) + "\n")
    return path


def run_ssh(conn, fds, user: str, sid: str, name: str, cmd: str, sftp: bool = False) -> int:
    """以门卫的身份 ssh 过去，用 ccssh 交过来的三个描述符；ccssh 断了就收掉 ssh。返回退出码（和 ssh 的一样）。
    sftp 为真时开的是 sftp 子系统（给 scp -s、sshfs 用），cmd 只是记账用的说明。"""
    mine = _load().get(user, {})
    if name not in mine:
        say(fds[2], f"ccssh：你没有登记叫 {name} 的目标（ccssh ls 看已登记的，ccssh add 登记）\n")
        return 255
    if why := paused_why(mine, name) or workdev.why_not(sid, name):
        say(fds[2], f"ccssh：{why}\n")
        return 255
    cfg = config_for(user, name, mine)
    t0 = time.time()
    argv = ["ssh", "-F", str(cfg)] + (["-s", "--", name, "sftp"] if sftp else ["--", name, cmd])
    p = subprocess.Popen(argv, stdin=fds[0], stdout=fds[1], stderr=fds[2],
                         close_fds=True, start_new_session=True, env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
    gone = False
    try:
        while p.poll() is None:
            r, _, _ = select.select([conn], [], [], 0.5)
            if r and not conn.recv(1):   # ccssh 被打断、退出了：别让 ssh 在那边接着跑
                gone = True
                with contextlib.suppress(OSError):
                    os.killpg(p.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(5)
                with contextlib.suppress(OSError):
                    os.killpg(p.pid, signal.SIGKILL)
                break
        rc = p.wait()
    finally:
        cfg.unlink(missing_ok=True)
    note({"ts": t0, "user": user, "session": sid, "target": name, "cmd": cmd[:2000], "exit": rc,
          "secs": round(time.time() - t0, 1), **({"cut": True} if gone else {})})
    return rc


def note(rec: dict):
    with lock, open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _proc(pid: int) -> tuple[str, int, int] | None:
    """(程序名, 所在会话的编号, 启动时间)，都从 /proc/<pid>/stat 来（谁都能读，内核写的，伪造不了）；进程不在了是 None。
    启动时间和进程号合起来才认得准一个进程：进程号会被复用。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    name, rest = raw[raw.index("(") + 1:raw.rindex(")")], raw[raw.rindex(")") + 2:].split()
    return name, int(rest[3]), int(rest[19])


def _mounts() -> dict:
    """挂载登记：sshfs 进程号 → {start, user, session, target}。进程已经不在的条目顺手清掉。"""
    try:
        data = json.loads(MOUNTS.read_text())
    except FileNotFoundError:
        return {}
    live = {k: v for k, v in data.items() if (p := _proc(int(k))) and p[2] == v["start"] and p[1] == int(k)}
    if live != data:
        _save_mounts(live)
    return live


def _save_mounts(data: dict):
    tmp = MOUNTS.with_name(f".mounts.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(MOUNTS)


def remember_mount(pid: int, user: str, sid: str, target: str):
    """会话里第一次挂载时：发来 sftp 请求的 ccssh 所在会话的组长是 sshfs（ccssh mount 让 sshfs 自立一个会话），就把这个 sshfs
    记在会话主人名下，它以后断线重连（那时已经不在任何网页会话底下）还认作同一个人，而且只能连同一个目标。"""
    me = _proc(pid)
    lead = me and _proc(me[1])
    if not lead or lead[0] != "sshfs" or lead[1] != me[1]:
        return
    with lock:
        data = _mounts()
        data[str(me[1])] = {"start": lead[2], "user": user, "session": sid, "target": target, "ts": time.time()}
        _save_mounts(data)


def mount_owner(pid: int, target: str) -> tuple[str, str]:
    """不在网页会话底下的 sftp 请求：只有登记过的 sshfs 那个会话里的、连的还是登记时那个目标，才认作那个 sshfs 的主人。
    会话编号只能从父进程继承（自己另立会话只会得到自己的进程号），别的进程混不进去。"""
    me = _proc(pid)
    with lock:
        m = me and _mounts().get(str(me[1]))
    if not m:   # _mounts() 已经核对过启动时间
        raise PermissionError("ccssh 只能在网页上开的会话里使用（认不出这条命令是哪个会话发出来的）")
    if m["target"] != target:
        raise PermissionError(f"这个挂载是连 {m['target']} 的，不能拿来连 {target}")
    return m["session"], m["user"]


# ---------- 登记、删除、改名、暂停：ccssh 的 add、rm 和网页的「设备」页（devices.py）共用 ----------
# 目标除了连接要的 host、port、user、via、key，还可以带几样给「设备」页看的说明（META）：kind 是 pc（经反向隧道接进来的电脑）
# 还是 server，os、hostname 是电脑报到时报的，added 是从哪儿加的（web 设备页、chat 对话里），note 是用户写的备注，
# paused 为真时门卫拒绝连它（「设备」页的暂停访问，不删设备，临时不让 Claude 连）。
# 重新登记同一个名字时这些说明照旧留着。
META = ("kind", "os", "hostname", "added", "note", "paused")


def paused_why(mine: dict, name: str) -> str:
    """这个目标（或它一路的跳板）被暂停了访问就说为什么，没有就是空字符串。"""
    seen = set()
    while name in mine and name not in seen:
        seen.add(name)
        if mine[name].get("paused"):
            return f"{name} 被你在网页的「设备」页暂停了访问，要用就先在那里点「恢复访问」"
        name = mine[name].get("via")
    return ""


def try_connect(user: str, name: str, mine: dict | None = None, timeout: int = 60, who: str = "") -> tuple[int, str]:
    """以门卫的身份连一下（执行 whoami），主机指纹第一次就记下。返回 (退出码, ssh 说的话)。
    who 不空就和别的连接一样记一笔账，会话那一栏写 who：「设备页」靠它显示刚登记、一次没连上的设备「没连上」。
    不用 true：Windows 上 ssh 进去是 PowerShell，没有 true，连上了也是退出码 1，Windows 电脑登记时全被当成没连上
    （实际遇到过一台 Win11）。whoami 哪种系统都有，设备页的「测试连接」和 wait-pc.py 用的也是它。"""
    mine = mine if mine is not None else _load().get(user, {})
    cfg = config_for(user, name, mine)
    t0 = time.time()
    try:
        p = subprocess.run(["ssh", "-F", str(cfg), "--", name, "whoami"], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout, env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
        rc, said = p.returncode, p.stderr.decode(errors="replace").strip()
    except subprocess.TimeoutExpired:
        rc, said = 124, f"{timeout} 秒没连上"
    finally:
        cfg.unlink(missing_ok=True)
    if who:
        note({"ts": t0, "user": user, "session": who, "target": name, "cmd": "whoami", "exit": rc, "secs": round(time.time() - t0, 1)})
    return rc, said


def add_target(user: str, name: str, dest: str, via=None, meta: dict | None = None, who: str = "") -> tuple[int, str, bool]:
    """登记（同名的覆盖）并试连一次：(退出码，要说的话，连上没有)。退出码 2 是写法不对、没登记；0 是登记了（连没连上看第三项）。
    who 是试连那次记账时会话那一栏写的（见 try_connect）。"""
    m = re.fullmatch(r"([^@]+)@([^:]+)(?::(\d{1,5}))?", dest)
    if not NAME.fullmatch(name):
        return 2, "名字只能用字母、数字、点、横杠、下划线，最长 32 个字\n", False
    if name in RESERVED:
        return 2, f"{name} 是 ccssh 的子命令，换个名字\n", False
    if not m or not LOGIN.fullmatch(m[1]) or not HOST.fullmatch(m[2]) or not 0 < int(m[3] or 22) < 65536:
        return 2, "地址要写成 用户@主机 或 用户@主机:端口（主机是域名或 IP）\n", False
    with lock:
        data = _load()
        mine = data.setdefault(user, {})
        if via is not None and (not isinstance(via, str) or via not in mine or via == name):
            return 2, f"跳板 {via} 不是你登记过的目标\n", False
        old = mine.get(name)
        mine[name] = {"host": m[2], "port": int(m[3] or 22), "user": m[1], "via": via, "ts": time.time()}
        if old and old.get("key") and (old["host"], old["port"], old["user"]) == (m[2], int(m[3] or 22), m[1]):
            mine[name]["key"] = old["key"]   # 只改了跳板：收编来的钥匙接着用
        for k in META:
            if meta and k in meta:
                if meta[k] is not None:
                    mine[name][k] = meta[k]
            elif old and k in old:
                mine[name][k] = old[k]
        _save(data)
    if old and (old["host"], old["port"]) != (m[2], int(m[3] or 22)):   # 换了地址：旧的主机指纹不算数了
        subprocess.run(["ssh-keygen", "-R", f"gw-{name}", "-f", str(DIR / "known" / _tag(user))], capture_output=True)
    text = f"登记了 {name} → {dest}" + (f"（经 {via}）" if via else "") + "，试着连一下……\n"
    rc, said = try_connect(user, name, mine, who=who)
    if rc == 0:
        return 0, text + f"连上了，主机指纹已记下。以后用：ccssh {name} <命令>\n", True
    return 0, text + (f"还连不上（退出码 {rc}{'：' + said.splitlines()[-1] if said else ''}）。如果是还没装公钥：把下面这行加进 {name} 上 "
                      f"{m[1]} 的 ~/.ssh/authorized_keys，再用 ccssh {name} whoami 试\n{Path(f'{key_of(user)}.pub').read_text()}"), False


def remove_target(user: str, name: str) -> tuple[int, str]:
    """删掉一个登记：(退出码，要说的话)，不是 0 就是没删。"""
    with lock:
        data = _load()
        if name not in data.get(user, {}):
            return 2, f"你没有登记叫 {name} 的目标\n"
        if any(t.get("via") == name for t in data[user].values()):
            return 2, f"还有目标经 {name} 跳过去，先删它们\n"
        gone = data[user].pop(name)
        _save(data)
    workdev.removed(user, name)   # 各会话勾选的工作设备里也去掉
    if gone.get("key") and not any(t.get("key") == gone["key"] for u in data.values() for t in u.values()):
        (DIR / "keys" / gone["key"]).unlink(missing_ok=True)   # 收编来的钥匙没人用了就删掉
        (DIR / "keys" / f"{gone['key']}.pub").unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-R", f"gw-{name}", "-f", str(DIR / "known" / _tag(user))], capture_output=True)
    return 0, f"删掉了 {name}\n"


def rename_target(user: str, old: str, new: str) -> str:
    """改名：经它跳的目标跟着改，记下的主机指纹换到新名字上（指纹按 gw-<名字> 记的）。不行就抛 ValueError。"""
    if not NAME.fullmatch(new):
        raise ValueError("名字只能用字母、数字、点、横杠、下划线，最长 32 个字")
    if new in RESERVED:
        raise ValueError(f"{new} 是 ccssh 的子命令，换个名字")
    with lock:
        data = _load()
        mine = data.get(user, {})
        if old not in mine:
            raise ValueError(f"你没有登记叫 {old} 的目标")
        if new in mine:
            raise ValueError(f"已经有一个叫 {new} 的了")
        mine[new] = mine.pop(old)
        for t in mine.values():
            if t.get("via") == old:
                t["via"] = new
        _save(data)
        known = DIR / "known" / _tag(user)
        with contextlib.suppress(FileNotFoundError):
            lines = known.read_text().splitlines()
            known.write_text("".join((f"gw-{new} " + l[len(f'gw-{old} '):] if l.startswith(f"gw-{old} ") else l) + "\n"
                                     for l in lines))
    workdev.renamed(user, old, new)   # 各会话勾选的工作设备跟着改名
    return f"{old} 改名成 {new} 了"


def set_meta(user: str, name: str, **fields) -> dict:
    """改「设备」页的说明（META 里那几样），值为 None 就去掉。返回改好的这一条。"""
    with lock:
        data = _load()
        t = data.get(user, {}).get(name)
        if t is None:
            raise ValueError(f"你没有登记叫 {name} 的目标")
        for k, v in fields.items():
            if k not in META:
                raise ValueError(f"不认识的说明：{k}")
            if v is None:
                t.pop(k, None)
            else:
                t[k] = v
        _save(data)
        return dict(t)


def targets(user: str) -> dict:
    return {n: dict(t) for n, t in _load().get(user, {}).items()}


def everyone() -> dict:
    """所有人的登记（「设备」页查同一个端口是不是还有别人登记着、后台记电脑最后在线时用）。"""
    return _load()


def pubkey(user: str) -> str:
    return Path(f"{key_of(user)}.pub").read_text().strip()


def tag(user: str) -> str:
    return _tag(user)


def run_capture(user: str, name: str, cmd: str, who: str, timeout: int = 60) -> tuple[int, str, str]:
    """「设备」页替用户在目标上执行一条命令（测连接、删除时卸载电脑上的东西）：(退出码，输出，错误)。和 ccssh 一样记账，会话写 who。"""
    mine = _load().get(user, {})
    if name not in mine:
        return 255, "", f"你没有登记叫 {name} 的目标"
    cfg = config_for(user, name, mine)
    t0 = time.time()
    try:
        p = subprocess.run(["ssh", "-F", str(cfg), "--", name, cmd], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout, env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
        rc, out, err = p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", f"{timeout} 秒没回来"
    finally:
        cfg.unlink(missing_ok=True)
    note({"ts": t0, "user": user, "session": who, "target": name, "cmd": cmd[:2000], "exit": rc, "secs": round(time.time() - t0, 1)})
    return rc, out, err


def history(user: str, name: str | None = None, n: int = 50) -> list[dict]:
    """这个人（某个目标）最近的连接记录，新的在后。只读日志末尾一段：日志只增不减，全读会越来越慢。"""
    rows = []
    with contextlib.suppress(FileNotFoundError), open(LOG, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - (2 << 20)))
        for raw in f.read().splitlines():
            with contextlib.suppress(ValueError):
                r = json.loads(raw)
                if r.get("user") == user and (name is None or r.get("target") == name):
                    rows.append(r)
    return rows[-n:]


def manage(req: dict, fds, user: str, sid: str) -> int:
    """登记、删除、列出目标，给出公钥，看记录。输出直接写到 ccssh 的标准输出、错误上。"""
    op, out, err = req.get("op"), fds[1], fds[2]
    if op == "key":
        say(out, Path(f"{key_of(user)}.pub").read_text())
        return 0
    if op == "ls":
        mine = _load().get(user, {})
        if not mine:
            say(out, "还没有登记目标（ccssh add 名字 用户@主机[:端口] [--via 跳板]）\n")
        for n, t in sorted(mine.items()):
            say(out, f"{n}\t{t['user']}@{t['host']}:{t['port']}" + (f"\t经 {t['via']}" if t.get("via") else "")
                + ("\t用原来那把钥匙" if t.get("key") else "") + ("\t已暂停访问" if t.get("paused") else "") + "\n")
        return 0
    if op == "add":
        rc, text, _ = add_target(user, str(req.get("name") or ""), str(req.get("dest") or ""), req.get("via"), who=sid)
        if rc == 0:   # 在这个会话里登记的，就是要在这个会话里用：勾进它的工作设备
            workdev.added(sid, user, str(req.get("name")))
        say(err if rc == 2 else out, ("ccssh：" if rc == 2 else "") + text)
        return rc
    if op == "rm":
        rc, text = remove_target(user, str(req.get("name") or ""))
        say(err if rc else out, ("ccssh：" if rc else "") + text)
        return rc
    if op == "log":
        everyone = bool(req.get("all"))
        if everyone and MULTI and user not in ADMINS:
            say(err, "ccssh：log 只能看自己的记录，不认 --all\n")   # 会话里的 AI 看得到，不提管理员
            return 2
        n = max(1, min(int(req.get("n") or 20), 1000))
        rows = []
        with contextlib.suppress(FileNotFoundError), open(LOG, encoding="utf-8") as f:
            rows = [r for r in map(json.loads, f) if everyone or r.get("user") == user]
        for r in rows[-n:]:
            say(out, f"{time.strftime('%m-%d %H:%M:%S', time.localtime(r['ts']))}  " + (f"{r['user']}  " if everyone else "")
                + f"{r['target']}  退出码 {r['exit']}  {r['secs']} 秒" + ("（中途断开）" if r.get("cut") else "")
                + f"  {r['cmd'][:200]}\n")
        return 0
    say(err, f"ccssh：不认识的操作 {op}\n")
    return 2


def model_driver(pid):
    """内部认证只认实际子孙进程，不沿用 SSH 挂载的会话组/重连例外；不接受客户端自报账号。"""
    import chat
    for d in list(drivers()):
        client = d.client or getattr(d, "starting_client", None)
        if client and pid in chat.cli_pids(client):
            return d
    raise PermissionError("认不出认证请求所属的会话")


def serve(conn: socket.socket):
    fds: list[int] = []
    rc = 255
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, _, _ = struct.unpack("3i", creds)
        msg, anc, _, _ = conn.recvmsg(1 << 20, socket.CMSG_SPACE(3 * 4), socket.MSG_CMSG_CLOEXEC)
        for level, kind, raw in anc:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                got = array.array("i")
                got.frombytes(raw[:len(raw) - len(raw) % got.itemsize])
                fds.extend(got)
        (n,) = struct.unpack("!I", msg[:4])
        body = msg[4:]
        while len(body) < n:   # 请求很长（大段命令）时分几次到
            chunk = conn.recv(n - len(body))
            if not chunk:
                return
            body += chunk
        req = json.loads(body)
        if len(fds) != 3:
            raise ValueError("ccssh 要把标准输入、输出、错误一起交过来")
        op, target = req.get("op"), str(req.get("target") or "")
        if op == "model-auth":
            import cc_credentials
            try:
                token = cc_credentials.issue(model_driver(pid))
                # 令牌只写入 helper 的输出管道，不进入响应JSON、连接日志或会话正文。
                os.write(fds[1], (token + "\n").encode())
                rc = 0
            except Exception as e:
                say(fds[2], "认证信息暂时取不到：" + str(e) + "\n")
                print("会话认证请求失败：" + type(e).__name__ + ": " + str(e), flush=True)
            return
        try:
            try:
                sid, user = who_sent(pid)
            except PermissionError:
                if op not in ("sftp", "has"):
                    raise
                sid, user = mount_owner(pid, target)   # sshfs 断线重连：不在网页会话底下，按挂载登记认（经包装时先问 has）
            else:
                if op == "sftp" and user:
                    remember_mount(pid, user, sid, target)
        except PermissionError as e:
            say(fds[2], f"ccssh：{e}\n")
            return
        if not user:
            say(fds[2], "ccssh：这个会话没有主人（无主会话），不知道该用谁登记的目标，登录后新建的会话才能用\n")
            return
        if op == "has":   # 同名包装问：这个目标是不是本人登记过的（是就走门卫，不是就交给系统的 ssh）
            rc = 0 if target in _load().get(user, {}) else 1
        elif op == "sftp":
            rc = run_ssh(conn, fds, user, sid, target, "[sftp]", sftp=True)
        elif op == "run":
            cmd = str(req.get("cmd") or "")
            if not cmd.strip() or len(cmd) > MAX_CMD:
                say(fds[2], "ccssh：要给出在那边执行的命令（不支持交互登录）\n" if not cmd.strip() else "ccssh：命令太长了\n")
                rc = 2
            else:
                rc = run_ssh(conn, fds, user, sid, str(req.get("target") or ""), cmd)
        else:
            rc = manage(req, fds, user, sid)
    except Exception as e:   # 出错要让 ccssh 那边看到，不能只是断开
        if fds:
            say(fds[2], f"ccssh：门卫出错了：{type(e).__name__}: {e}\n")
        print(f"ssh 门卫出错：{traceback.format_exc()}", flush=True)
    finally:
        with contextlib.suppress(OSError):
            conn.sendall(json.dumps({"exit": rc}).encode() + b"\n")
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        conn.close()


def _accept(sock: socket.socket):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=serve, args=(conn,), daemon=True, name="sshgw").start()


def start():
    """开门卫（对话模式、配了 gw_socket 才开）。套接字所在目录要让运行 Claude 的用户进得去（目录 711），套接字本身谁都能连：
    认人靠内核报来的进程号，不靠文件权限。"""
    global _sock
    if not GW_SOCKET or _sock:
        return
    path = Path(GW_SOCKET)
    path.parent.mkdir(parents=True, exist_ok=True)
    DIR.mkdir(exist_ok=True, mode=0o700)
    path.unlink(missing_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    os.chmod(path, 0o666)
    s.listen(16)
    _sock = s
    threading.Thread(target=_accept, args=(s,), daemon=True, name="sshgw-accept").start()
    print(f"ssh 门卫在 {path}", flush=True)


def stop():
    global _sock
    if _sock:
        with contextlib.suppress(OSError):
            _sock.close()
        Path(GW_SOCKET).unlink(missing_ok=True)
        _sock = None
