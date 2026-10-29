"""待办（清单入口：查看器不自己管待办，只把每个人工作区里的清单接进来）。

「待办」只是顺口的叫法：它其实是用户的管家工作区，记着手上有哪些事、做到哪、材料在哪。用户开一个全新的会话说
「看待办」是起手式，要 AI 凭它很快恢复上下文（用户的，也是 AI 自己的）；回忆旧会话是兜底，回忆到的东西写回清单。
- 一份清单就是一个工作区目录，查看器只要求里面有开头文件 待办.md 和目录 待办条目/（每件事一个 .md），别的东西随便放。
  查看器只存每个人登记了哪几份清单（名字、设备、工作区路径、哪份默认），内容和组织都在文件里，由 AI 按用户的要求改。
- 给 AI 的读法只有一种（cctodo、cctodo read <清单名>）：开头文件全文，再按随机顺序给出每个条目文件的全文，一条不少；
  先后每次打乱，免得 AI 把文件顺序当成轻重。最后附上网页上还没写进文件的操作。
- 网页的待办页样子照旧：数据从文件解析出来，每份清单当成原来的一个分组，没有层级和上下级。
  一份清单里有一个文件不符合格式（GUIDE 的「条目文件」），页面上这份就不列条目，只说哪里不对、给一颗「让 AI 修好」。
- 网页上的改动（改状态、重要性、时限、标题，新建一条）不写文件：记进这个人的 pending，页面上马上按改过的样子显示，
  AI 下次读清单时一并列给它，它写进文件以后用 cctodo synced 标掉（用户的操作前端立刻生效，AI 下次看清单时看到累积的操作）。
- 设备：登记写「设备:路径」。实例开着 ssh 门卫、用户在门卫里登记了这个名字，就经门卫以用户自己的连接去读（文件权限不用为查看器开，
  新建清单也不用再开）；否则按本机路径直接读（查看器和清单在同一台机器上）。写「本机:路径」一律直接读。
- 存法照旧：数据目录的 todos.json，todos_shared 列出的账号存进共用文件（两个实例读写同一份、拿文件锁，见 instance.py）。每人一本
  {"lists": [{name, device, path}], "default": 名字, "pending": [操作], "born": {条目编号: [{t, sid, op, at}]}, "old": 改版前的整本}。
  改版前存的条目（分组、层级那一套）第一次碰到时整本收进 old，不删、不再用。
- 每个会话第一次运行 cctodo（不管什么操作）只给出指引、不执行，记在 GUIDED（AI 多半不看指引就直接用）。
- 会话链接只剩「从这里开出」（待办页勾选几条发到新会话，server.Birth 记），别的（认领、查过、记录过）随旧做法一起去掉了。
"""
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import random
import re
import secrets
import shlex
import tarfile
import threading
import time
from datetime import date, datetime, timedelta, timezone

import fcntl
from pathlib import Path

import instance
from instance import DATA, TODOS_SHARED

FILE = DATA / "todos.json"
# 看过指引的会话：AI 多半不看指引就直接用，于是每个会话第一次运行 cctodo（不管什么操作）
# 都先只给指引、不执行；记进文件，重启以后看过的会话不再拦。{会话编号: 时刻}，存 90 天
GUIDED = DATA / "todo_guided.json"
GUIDED_DAYS = 90
SHARED = Path(TODOS_SHARED["file"]) if TODOS_SHARED and TODOS_SHARED.get("file") else None
SHARED_AS: dict[str, str] = dict(TODOS_SHARED.get("users") or {}) if SHARED else {}
HERE = ""        # server 按端口填：从待办开出的会话是哪个实例上的
HEAD, ITEMS = "待办.md", "待办条目"
# 状态写三个字；条目文件里「- 状态：」以其中一个开头，不写算未开始
STATUS = {"todo": "未开始", "doing": "工作中", "waiting": "等待中", "done": "已完成", "dropped": "暂搁置"}
OPEN = ("todo", "doing", "waiting")
IMPORTANCE = {"critical": "关键", "high": "重要", "mid": "一般", "low": "次要", "minor": "可选"}
EFFORT = {"hours": "几小时", "days": "几天", "weeks": "几周"}
LOCAL = "本机"
MAX_TITLE, MAX_GROUP, MAX_SUMMARY, MAX_FILE, MAX_ITEMS, MAX_LISTS, MAX_PENDING = 80, 24, 400, 200_000, 500, 20, 500
MAX_TAR = 8_000_000   # 经门卫取回来的整个工作区清单部分（base64 之前）最多这么大
PAGE_TTL = 20         # 待办页每 15 秒刷一次，读文件（经门卫要连一次 ssh、记一笔账）最多 20 秒一次；cctodo 每次都现读
BJ = timezone(timedelta(hours=8))
_lock = threading.RLock()
_cache: dict[Path, tuple] = {}
_broken: dict[Path, str] = {}
_moved: set[str] = set()
_files: dict[tuple, tuple] = {}   # (用户, 设备, 路径) → (读的时刻, 读到的样子)
on_take = None   # 旧接口（分组当会话标签），server 照旧会填；新做法没有分组，用不到


class Bad(Exception):
    pass


# ---------- 存登记表（和改版前同一套：本实例的 todos.json，或共用文件）----------

def _stamp(path: Path):
    st = path.stat()
    return st.st_mtime_ns, st.st_size, st.st_ino


def _load(path: Path) -> dict:
    """读一份存档，没变就用缓存。读不出来（不是没有）就报错，不当成空的：那样下一次保存会把原来的盖掉。"""
    try:
        try:
            stamp = _stamp(path)
        except FileNotFoundError:
            stamp = None
        hit = _cache.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
        data = json.loads(path.read_text()) if stamp else {}
    except (OSError, ValueError) as e:
        _cache.pop(path, None)
        _broken[path] = f"待办的登记文件 {path.name} 读不出来（{e}），为了不把原来的盖掉，待办暂时不能用，请联系管理员"
        raise Bad(_broken[path]) from None
    _broken.pop(path, None)
    _cache[path] = (stamp, data)
    return data


def _save(path: Path, data: dict):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    mode = 0o660 if path == SHARED else 0o600
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(tmp, mode)
        tmp.replace(path)
        _cache[path] = (_stamp(path), data)
    except OSError as e:
        raise Bad(f"待办存不下来：{e}") from None


@contextlib.contextmanager
def _xlock(path: Path):
    if path != SHARED:
        yield
        return
    try:
        fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o660)
    except OSError as e:
        raise Bad(f"共用待办的锁文件打不开：{e}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _where(user: str) -> tuple[Path, str]:
    """这个人的登记存在哪份文件、哪个名字底下（共用账号第一次碰到时，把本实例里原来那份搬进共用文件，两边都有就停下）。"""
    if user not in SHARED_AS:
        return FILE, user
    key = SHARED_AS[user]
    if user not in _moved:
        local = _load(FILE)
        if user in local:
            with _xlock(SHARED):
                shared = _load(SHARED)
                if key in shared:
                    raise Bad(f"{user} 的待办在本实例和共用文件里各有一份，没有合并，请联系管理员")
                shared[key] = local[user]
                _save(SHARED, shared)
            mine = local.pop(user)
            try:
                _save(FILE, local)
            except Bad:
                local[user] = mine
                raise
        _moved.add(user)
    return SHARED, key


def _fresh(old) -> dict:
    """改版前的一本（groups、items）整本收进 old，不删、不再用。"""
    if old and "lists" in old:
        return old
    book = {"lists": [], "default": "", "pending": [], "born": {}}
    if old:
        book["old"] = old
    return book


def _change(user: str, fn):
    if not user:
        raise Bad("待办只给登录的用户用")
    with _lock:
        path, key = _where(user)
        with _xlock(path):
            data = _load(path)
            old = data.get(key)
            book = copy.deepcopy(_fresh(old))
            out = fn(book)
            data[key] = book
            try:
                _save(path, data)
            except Bad:
                if old is None:
                    data.pop(key, None)
                else:
                    data[key] = old
                raise
            return copy.deepcopy(out)


def _book(user: str) -> dict:
    if not user:
        raise Bad("待办只给登录的用户用")
    with _lock:
        path, key = _where(user)
        return copy.deepcopy(_fresh(_load(path).get(key)))


# ---------- 读清单文件 ----------

NAME = re.compile(r"[^\s/\\:][^/\\:\n]{0,23}")


def _spec(raw: str) -> tuple[str, str]:
    """「设备:路径」→ (设备, 路径)。路径要是绝对路径；设备是门卫里登记的名字，或者「本机」。"""
    dev, sep, path = str(raw or "").partition(":")
    dev, path = dev.strip(), path.strip()
    if not sep or not dev or not path.startswith("/"):
        raise Bad("清单的位置要写成「设备:工作区的绝对路径」，比如 server:/home/me/工作区；就在查看器这台机器上写「本机:/路径」")
    return dev, path.rstrip("/") or "/"


def where_text(ent: dict) -> str:
    return f"{'查看器这台机器' if ent['device'] == LOCAL else ent['device']} 上的 {ent['path']}"


def _gateway(user: str, dev: str):
    """这个设备要不要经门卫读：实例开着门卫、用户登记了这个名字就要；暂停了访问就说清楚。"""
    if dev == LOCAL or not instance.GW_SOCKET:
        return None
    import sshgw
    mine = sshgw.targets(user)
    if dev not in mine:
        return None
    why = sshgw.paused_why(mine, dev)
    if why:
        raise Bad(why)
    return sshgw


def _fetch(user: str, ent: dict, who: str) -> dict:
    """把工作区里清单的部分读回来：{head: 开头文件的字或 None, files: {文件名: 字}, odd: [不该在 待办条目/ 里的东西]}。
    连不上、读不了抛 Bad（页面上照实写）；格式问题不抛，交给 _parse 说清楚。"""
    dev, path = ent["device"], ent["path"]
    gw = _gateway(user, dev)
    head, files, odd = None, {}, []
    head_mt, mt = None, {}   # 开头文件、各条目文件最后改动的时刻：AI 看清单旧不旧要用（_full），tar 里本来就带着，不用多连一次
    if gw:
        # 一条命令取回：开头文件和 待办条目/ 整个打成 tar 再转 base64（文本通道，文件名里有中文、内容里有什么都不怕）
        cmd = (f"cd -- {shlex.quote(path)} || exit 3; "
               f"[ -f {HEAD} ] || {{ echo '没有开头文件 {HEAD}' >&2; exit 4; }}; "
               f"[ -d {ITEMS} ] || {{ echo '没有目录 {ITEMS}/' >&2; exit 4; }}; "
               f"tar -cf - -- {HEAD} {ITEMS} | base64")
        rc, out, err = gw.run_capture(user, dev, cmd, who, timeout=25)
        if rc == 4:
            return {"head": None, "files": {}, "odd": [], "missing": err.strip(), "mtime": {}, "head_mtime": None}
        if rc != 0:
            raise Bad(f"经门卫读 {where_text(ent)} 没成功（退出码 {rc}）：{(err.strip() or out.strip())[-300:]}")
        try:
            raw = base64.b64decode(out)
            if len(raw) > MAX_TAR:
                raise Bad(f"清单太大了（{len(raw) // 1_000_000} MB），待办条目/ 里只放条目文件")
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                for m in tar.getmembers():
                    name = m.name.lstrip("./")
                    if name == HEAD and m.isfile():
                        head = tar.extractfile(m).read().decode("utf-8", errors="replace")
                        head_mt = m.mtime
                    elif name in (HEAD, ITEMS):
                        continue
                    elif name.startswith(ITEMS + "/"):
                        rel = name[len(ITEMS) + 1:]
                        if "/" in rel or not m.isfile() or not rel.endswith(".md"):
                            if "/" not in rel and not rel.startswith("."):
                                odd.append(rel)
                            elif "/" in rel and rel.split("/")[0] not in odd and not rel.startswith("."):
                                odd.append(rel.split("/")[0])
                            continue
                        if rel.startswith("."):
                            continue
                        files[rel] = tar.extractfile(m).read().decode("utf-8", errors="replace")
                        mt[rel] = m.mtime
        except (ValueError, tarfile.TarError, OSError) as e:
            raise Bad(f"从 {where_text(ent)} 取回来的内容解不开：{e}") from None
    else:
        root = Path(path)
        try:
            if not root.is_dir():
                raise Bad(f"{where_text(ent)} 不是一个能读的目录" + ("" if dev == LOCAL else "（这个设备没在门卫里登记，按查看器这台机器上的路径找的）"))
            h, d = root / HEAD, root / ITEMS
            if not h.is_file():
                return {"head": None, "files": {}, "odd": [], "missing": f"没有开头文件 {HEAD}", "mtime": {}, "head_mtime": None}
            if not d.is_dir():
                return {"head": None, "files": {}, "odd": [], "missing": f"没有目录 {ITEMS}/", "mtime": {}, "head_mtime": None}
            head = h.read_text(errors="replace")
            head_mt = h.stat().st_mtime
            total = 0
            for p in sorted(d.iterdir()):
                if p.name.startswith("."):
                    continue
                if not p.is_file() or not p.name.endswith(".md"):
                    odd.append(p.name)
                    continue
                st = p.stat()
                total += st.st_size
                if total > MAX_TAR:
                    raise Bad("清单太大了，待办条目/ 里只放条目文件")
                files[p.name] = p.read_text(errors="replace")
                mt[p.name] = st.st_mtime
        except OSError as e:
            raise Bad(f"读不了 {where_text(ent)}：{e.strerror or e}") from None
    return {"head": head, "files": files, "odd": odd, "missing": "", "mtime": mt, "head_mtime": head_mt}


FIELD = re.compile(r"- ([^\s：（(]{1,12})((?:（[^）\n]*）)?)：(.*)", re.S)


def parse_item(text: str) -> tuple[dict | None, str]:
    """一个条目文件 → ({title, fields: [{k, v}], status}, "") 或 (None, 哪里不对)。
    格式（GUIDE「条目文件」）：第一行「# 显示名」；下面「- 键：值」一行一项，键可以带括号说明（「- 进展（某天巡查）：」），
    没有键的「- 」也算一项；不是「- 」开头的行接在上一项后面。状态以五个状态之一开头，不写算未开始。"""
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not lines[i].startswith("# ") or not lines[i][2:].strip():
        return None, "第一行要是「# 显示名」"
    title = lines[i][2:].strip()
    if len(title) > MAX_TITLE:
        return None, f"显示名最多 {MAX_TITLE} 字，现在 {len(title)} 字"
    fields: list[dict] = []
    for ln in lines[i + 1:]:
        if ln.startswith("- "):
            m = FIELD.fullmatch(ln)
            fields.append({"k": m.group(1), "note": m.group(2), "v": m.group(3)} if m else {"k": "", "note": "", "v": ln[2:]})
        elif not ln.strip():
            if fields:
                fields[-1]["v"] += "\n"
        else:
            if not fields:
                return None, "显示名下面要是「- 键：值」这样一行一项"
            fields[-1]["v"] += "\n" + ln
    status = "todo"
    for f in fields:
        f["v"] = f["v"].strip("\n")
        if f["k"] == "状态":
            hit = [k for k, v in STATUS.items() if f["v"].startswith(v)]
            if not hit:
                return None, "「- 状态：」要以 " + "、".join(STATUS.values()) + " 之一开头"
            status = hit[0]
    return {"title": title, "fields": fields, "status": status}, ""


def _parse(ent: dict, got: dict) -> dict:
    """读回来的样子 → {head, items: [{file, text, title, fields, status}], errors: [哪里不对]}"""
    errors = [got["missing"]] if got["missing"] else []
    errors += [f"{ITEMS}/{n} 不是条目文件（{ITEMS}/ 里只放 .md 文件）" for n in got["odd"]]
    if len(got["files"]) > MAX_ITEMS:
        errors.append(f"条目最多 {MAX_ITEMS} 个，现在 {len(got['files'])} 个")
    items = []
    for name, text in sorted(got["files"].items()):
        if len(text) > MAX_FILE:
            errors.append(f"{ITEMS}/{name} 太长了（最多 {MAX_FILE} 字）")
            continue
        it, why = parse_item(text)
        if why:
            errors.append(f"{ITEMS}/{name}：{why}")
            continue
        items.append({"file": name, "text": text, "mtime": got["mtime"].get(name), **it})
    return {"head": got["head"], "items": items, "errors": errors, "head_mtime": got["head_mtime"]}


def _read(user: str, ent: dict, who: str, fresh: bool) -> dict:
    key = (user, ent["device"], ent["path"])
    hit = _files.get(key)
    if not fresh and hit and time.time() - hit[0] < PAGE_TTL:
        return hit[1]
    res = _parse(ent, _fetch(user, ent, who))
    _files[key] = (time.time(), res)
    return res


# ---------- 给网页的样子（和改版前的接口同一个形状，待办页的前端照旧能用）----------

def item_id(lst: str, fname: str) -> str:
    return hashlib.sha1(f"{lst}\0{fname}".encode()).hexdigest()[:12]


def _field(it: dict, key: str) -> str:
    return next((f["v"] for f in it["fields"] if f["k"] == key), "")


def _op_text(op: dict) -> str:
    v = op.get("v")
    if op["op"] == "new":
        return f"新建一条「{v['title']}」" + ("".join(f"，{w}：{x}" for w, x in _new_bits(v)) if _new_bits(v) else "")
    what = {"status": "状态", "importance": "重要性", "deadline": "时限", "title": "显示名", "effort": "耗时"}[op["op"]]
    if v is None:
        return f"{what}清掉"
    return f"{what}改成「{_val_text(op['op'], v)}」"


def _val_text(op: str, v) -> str:
    if op == "status":
        return STATUS[v]
    if op == "importance":
        return IMPORTANCE[v["v"]]
    if op == "effort":
        return EFFORT[v["v"]]
    if op == "deadline":
        return v["v"]
    return str(v)


def _new_bits(v: dict) -> list[tuple[str, str]]:
    out = []
    if v.get("status", "todo") != "todo":
        out.append(("状态", STATUS[v["status"]]))
    for k, w in (("importance", "重要性"), ("deadline", "时限"), ("effort", "耗时")):
        if v.get(k):
            out.append((w, _val_text(k, v[k])))
    return out


def _web_item(lst: dict, it: dict, idx: int, book: dict) -> dict:
    """一个条目文件 → 待办页认得的一条（字段和改版前一样，没有的给空）。页面上的改动（pending）叠在上面。"""
    tid = item_id(lst["name"], it["file"])
    body = "\n".join((f"- {f['k']}{f['note']}：{f['v']}" if f["k"] else f"- {f['v']}") for f in it["fields"])
    out = {"id": tid, "title": it["title"], "group": lst["name"], "parent": None, "level": 1, "status": it["status"],
           "importance": None, "deadline": None, "effort": None, "summary": _field(it, "现状")[:MAX_SUMMARY], "body": body,
           "progress": [], "unknowns": [], "related": [], "dir": _field(it, "目录").strip("`").strip(),
           "file": f"{lst['path']}/{ITEMS}/{it['file']}", "created": idx, "updated": 0,
           "done": None if it["status"] in OPEN else idx, "pending": [], "log": book["born"].get(tid, []), "working": []}
    for op in book["pending"]:
        if op["list"] == lst["name"] and op.get("file") == it["file"]:
            _overlay(out, op)
    return out


def _overlay(out: dict, op: dict):
    if op["op"] == "title":
        out["title"] = op["v"]
    elif op["op"] == "status":
        out["status"] = op["v"]
        out["done"] = None if op["v"] in OPEN else op["t"]
    elif op["op"] in ("importance", "deadline", "effort"):
        out[op["op"]] = op["v"]
    out["pending"].append(_op_text(op))


def _new_item(lst: dict, op: dict, book: dict) -> dict:
    v = op["v"]
    tid = "n" + op["id"]
    return {"id": tid, "title": v["title"], "group": lst["name"], "parent": None, "level": 1, "status": v.get("status", "todo"),
            "importance": v.get("importance"), "deadline": v.get("deadline"), "effort": v.get("effort"), "summary": "", "body": "",
            "progress": [], "unknowns": [], "related": [], "dir": "", "file": "", "created": 10 ** 6 + op["t"], "updated": op["t"],
            "done": None if v.get("status", "todo") in OPEN else op["t"], "pending": ["网页上新建的，还没写进文件"],
            "log": book["born"].get(tid, []), "working": []}


def overview(user: str, fresh: bool = False) -> dict:
    """待办页要的：{groups: [清单名，默认的在前], items: {编号: 一条}, lists: [{name, where, default, error, count}], pending: 几条没同步}。
    一份清单读不出来、格式不对，它的条目一条都不列，lists 里写清楚原因（不符合就空着说不符合，给 AI 去修）。"""
    book = _book(user)
    lists = sorted(book["lists"], key=lambda l: l["name"] != book["default"])
    items, info = {}, []
    for lst in lists:
        row = {"name": lst["name"], "where": where_text(lst), "default": lst["name"] == book["default"], "error": "", "count": 0}
        try:
            res = _read(user, lst, "待办页·读清单", fresh)
            if res["errors"]:
                row["error"] = "；".join(res["errors"][:5]) + (f"；另有 {len(res['errors']) - 5} 处" if len(res["errors"]) > 5 else "")
            else:
                for i, it in enumerate(res["items"]):
                    w = _web_item(lst, it, i, book)
                    items[w["id"]] = w
                row["count"] = len(res["items"])
        except Bad as e:
            row["error"] = str(e)
        if not row["error"]:
            for op in book["pending"]:
                if op["list"] == lst["name"] and op["op"] == "new":
                    w = _new_item(lst, op, book)
                    items[w["id"]] = w
        info.append(row)
    return {"groups": [l["name"] for l in lists], "items": items, "lists": info, "pending": len(book["pending"])}


def links(it: dict) -> dict:
    """一条挂着的会话：只剩从这里开出的（每个会话留最近一次，新的在前）；其余几样给空，前端照旧认。"""
    seen = {}
    for e in it.get("log", []):
        if e.get("sid"):
            seen[e["sid"]] = (e["t"], e.get("at", ""))
    born = [{"sid": s, "t": t, "at": at} for s, (t, at) in sorted(seen.items(), key=lambda kv: -kv[1][0])]
    return {"working": [], "born": born, "wrote": [], "read": []}


def _find(user: str, tid: str) -> tuple[dict, dict, dict | None]:
    """网页上点的那一条：(这一条的样子, 它在哪份清单, 新建还没写进文件的那个操作或 None)。"""
    view = overview(user)
    it = view["items"].get(str(tid))
    if not it:
        raise Bad("找不到这一条（清单文件可能刚被改过，刷新一下）")
    book = _book(user)
    lst = next(l for l in book["lists"] if l["name"] == it["group"])
    new = next((op for op in book["pending"] if "n" + op["id"] == it["id"]), None) if it["id"].startswith("n") else None
    return it, lst, new


def _rated(raw, table: dict | None, what: str):
    if raw is None or raw == "" or raw == "none":
        return None
    v = raw.get("v") if isinstance(raw, dict) else str(raw)
    if table is None:
        try:
            v = date.fromisoformat(str(v)).isoformat()
        except ValueError:
            raise Bad(f"时限要写成 YYYY-MM-DD，不认识 {v}") from None
    elif v not in table:
        raise Bad(f"{what}只能是 " + "、".join(table) + f"，不认识 {v}")
    return {"v": v, "src": "user"}


WEB_FIELDS = {"title", "status", "importance", "deadline", "effort"}


def _clean(fields: dict) -> dict:
    bad = [k for k in fields if k not in WEB_FIELDS]
    if bad:
        raise Bad("待办页上只能改状态、重要性、时限和显示名，别的（" + "、".join(bad) + "）请让 AI 改清单文件")
    out = {}
    for k, raw in fields.items():
        if k == "title":
            t = str(raw or "").strip()
            if not t or len(t) > MAX_TITLE:
                raise Bad(f"显示名不能是空的，最多 {MAX_TITLE} 字")
            out[k] = t
        elif k == "status":
            if raw not in STATUS:
                raise Bad("状态只能是 " + "、".join(f"{k}（{v}）" for k, v in STATUS.items()))
            out[k] = raw
        else:
            out[k] = _rated(raw, {"importance": IMPORTANCE, "effort": EFFORT, "deadline": None}[k],
                            {"importance": "重要性", "effort": "耗时", "deadline": "时限"}[k])
    return out


def add(user: str, fields: dict, sid: str | None = None) -> dict:
    """待办页顶上那颗输入框：记一个「新建」操作，加进默认清单（给了 group 就加进那份），页面上马上列出来。"""
    book = _book(user)
    if not book["lists"]:
        raise Bad("还没有登记待办清单，先让 AI 新建一份")
    name = fields.pop("group", None) or book["default"] or book["lists"][0]["name"]
    if not any(l["name"] == name for l in book["lists"]):
        raise Bad(f"没有叫「{name}」的清单")
    v = _clean(fields)
    if "title" not in v:
        raise Bad("显示名不能是空的")
    op = {"id": secrets.token_hex(3), "t": time.time(), "list": name, "file": None, "op": "new", "v": {"status": "todo", **v}}

    def fn(b):
        if len(b["pending"]) >= MAX_PENDING:
            raise Bad(f"网页上攒着没写进文件的操作已经有 {MAX_PENDING} 条了，先让 AI 读一次清单把它们写进去")
        b["pending"].append(op)
    _change(user, fn)
    lst = next(l for l in book["lists"] if l["name"] == name)
    return _new_item(lst, op, _book(user))


def edit(user: str, tid: str, fields: dict, sid: str | None = None) -> tuple[dict, list[str]]:
    """待办页上点的改动：记成操作（同一条的同一样只留最后一次；改回文件里原来的样子就撤掉那个操作），不写文件。"""
    it, lst, new = _find(user, tid)
    v = _clean(fields)
    changed = [k for k in v if it.get(k) != v[k]]
    if not changed:
        return it, []
    now = time.time()

    def fn(b):
        if new:
            for op in b["pending"]:
                if op["id"] == new["id"]:
                    op["v"].update({k: v[k] for k in changed})
            return
        res = _files.get((user, lst["device"], lst["path"]), (0, {"items": []}))[1]
        orig = next((x for x in res["items"] if item_id(lst["name"], x["file"]) == tid), None)
        if orig is None:
            raise Bad("找不到这一条（清单文件可能刚被改过，刷新一下）")
        base = {"title": orig["title"], "status": orig["status"], "importance": None, "deadline": None, "effort": None}
        for k in changed:
            b["pending"] = [op for op in b["pending"] if not (op["list"] == lst["name"] and op.get("file") == orig["file"] and op["op"] == k)]
            if v[k] != base[k]:
                if len(b["pending"]) >= MAX_PENDING:
                    raise Bad(f"网页上攒着没写进文件的操作已经有 {MAX_PENDING} 条了，先让 AI 读一次清单把它们写进去")
                b["pending"].append({"id": secrets.token_hex(3), "t": now, "list": lst["name"], "file": orig["file"],
                                     "title": orig["title"], "op": k, "v": v[k]})
    _change(user, fn)
    return overview(user)["items"].get(tid, it), changed


def check(user: str, tids: list[str]) -> list[str]:
    """从待办页发到新会话之前核对：这几条都在、都没做完。返回去重后的编号。"""
    items, out = overview(user)["items"], []
    for tid in tids:
        it = items.get(str(tid))
        if not it:
            raise Bad("勾选的待办找不到了（清单文件可能刚被改过，刷新一下）")
        if it["status"] not in OPEN:
            raise Bad(f"「{it['title']}」的状态是{STATUS[it['status']]}")
        if it["id"] not in out:
            out.append(it["id"])
    if not out:
        raise Bad("没有选中待办")
    return out


def born(user: str, tids: list[str], sid: str) -> list[str]:
    """从待办页发出的新会话（server.Birth，拉起 CLI 之前）：记上从这几条开出。返回要打的标签（没有分组了，给空）。"""
    def fn(b):
        now = time.time()
        for tid in tids:
            b["born"].setdefault(str(tid), []).append({"t": now, "sid": sid, "op": "born", **({"at": HERE} if HERE else {})})
    _change(user, fn)
    return []


def unborn(user: str, sid: str):
    def fn(b):
        for k in list(b["born"]):
            b["born"][k] = [e for e in b["born"][k] if e.get("sid") != sid]
            if not b["born"][k]:
                del b["born"][k]
    _change(user, fn)


def drop_session(user: str, sid: str, why: str):
    """旧接口：会话被删了要放手；新做法没有认领，什么都不用做。"""


# ---------- cctodo（会话里的 Claude 用；经人设卡接口的套接字 op=todo，personas.handle 转过来）----------

GUIDE = """# 待办清单怎么组织（通用建议，所有人都一样。只是建议，用户怎么说就怎么做；只有「条目文件」的格式是网页要解析的，必须照着写）

「待办」只是顺口的叫法。它其实是用户的管家工作区：记着用户手上有哪些事（在做的、没开始的、做完的），每件做到哪一步、材料在哪。
用户开一个全新的会话说「看待办」，是起手式：要你凭它很快恢复上下文，告诉用户最近在做什么、各件事停在哪、建议接着做哪几件。
它也是你自己恢复上下文的地方，比翻旧会话结构化、性价比高。回忆（翻旧会话）是兜底的办法：回忆到的、有用的东西写回这里，下次就不用再大量回忆。

## 工作区的样子
- 一份清单就是一个工作区目录。查看器只要求里面有两样：开头文件 `待办.md`，和目录 `待办条目/`（每件事一个 `.md` 文件，别的东西不要放进这个目录）。
- 工作区里别的东西完全自由：建什么目录、放多少东西都行，不要求每件事都有自己的目录，靠索引（文件路径）把它们串起来。
- 放在实验室的服务器上（换设备、换会话都连得到），不建议放在自己的电脑上。登记：`cctodo add <清单名> <设备>:<工作区路径>`，设备就是门卫里登记的名字。

## 一件事分几层写
一件事可以用一句话讲，也可以用一百字、一千字讲。下面几层只是篇幅不同，说的都是这件事的现状，没有别的分工：
- 条目（`待办条目/<名字>.md`）：必读，看待办时全部读进去，所以要短，推荐 100 字左右（不强求）。写描述、状态、一两句现状，再加几条索引。
- 详情：单独一个文件，不强制读，接手这件事时再读。推荐 1000 字左右，有必要可以长一点，也可以更短。放在工作区里哪儿都行，由条目里的索引指过去。
- 更多资料：调查结果、原话、草稿、想法、讨论，放多少、怎么组织都随意，由详情或条目里的索引指过去（索引不多就全放在条目里也行），
  可以有好几个文件。工作区外面的东西（别的目录、网址、会话）也可以指。
- 小事一个条目就够了，没有详情和资料也行。

## 开头文件 `待办.md`
写给 AI 看，读清单时第一个读到。建议写这几样：
- 怎么讲给用户听：比如用户没说要几条，就挑最该做的五条左右，一条一行只说显示名，前面写当场流水的编号；用户说「展开」「再往后列」就接着列，
  说「细说某条」再把那一条的描述用人话讲。不把整份清单倒给用户，也不一条条念给用户确认。
- 现在的重点：按用户最近的原话排的先后，几句话，情况变了就改。挑哪几条讲给用户，按它和各条的描述来判断，不按文件的先后。
- 可信度：哪些是用户亲口说的，哪些是推断，哪些是以前 AI 说的、没核实。
- 工作区里别的说明在哪（新会话先读哪些文件、做完怎么写回）。
- 清单过时了怎么办：`cctodo` 会写出清单最近一次改动是什么时候。好几天没人改或者条目很少时，AI 默认只提议用回忆更新、等用户答应；
  用户想让它看到过时就直接补，就在这里写一句，比如「过时了就直接回忆最近的会话补上，不用问我」。

## 条目文件 `待办条目/<名字>.md`（网页要解析，这部分必须照着写）
- 文件名就是这件事的名字，别的条目提到它时写「名字」。
- 第一行 `# 显示名`：给用户看的那一行，短、好看，最多 80 字。
- 下面每行一项 `- 键：值`，值可以接着往下写好几行；键可以带括号说明，比如 `- 现状（10-28）：…`。常用的键：描述、现状、状态、
  索引（指向详情和别的资料，可以好几条）、不清楚（还不知道的事，写清能查还是要问用户）、重要性、时限。不知道的就不写，不要编。
- `- 状态：` 只能以 未开始、工作中、等待中、已完成、暂搁置 之一开头，后面可以接说明；不写算未开始。做完的不删，写「已完成」。
- 不分组、不分层级。一件事是另一件的一部分、要等另一件、和另一件相关，都写在描述里。
- 推断的标「推断」，用户说的标「用户说」和日期。

## 维护
- 维护是几方同时在做的：干活的 AI 做到哪就写回哪；用户自己随时可能改；有时用户会专门开一个管理的会话，回忆最近的进展、统一再整理一遍。
  所以改哪个文件之前都先用读文件的工具读它的最新版（别人可能刚改过），只改自己负责的那部分，不整份重写别人的条目。
- 写回：做到哪一步、用户说了什么新的，当场写回。这件事的条目、详情和相关资料一视同仁，按各自的篇幅一起改成最新的样子。
  写回是改写，不是往后追加：信息确实变多了可以略长一点，但不能每更新一次就长一截；过时的说法改掉，还有用的来历挪进资料。
- 新冒出来的事建一个新的条目文件；用户纠正了什么就改什么。
- 管理的会话（或者用户让你巡查、更新待办的时候）：用回忆把最近的会话过一遍（从清单最近一次改动的时候往后看就行），
  把没写进来的进展、新的事补进来，合并重复的、改掉过时的。
- 用户在网页待办页上的改动（改状态、重要性、时限、显示名，新建一条）会列在 `cctodo` 输出的最后，照着写进文件，再 `cctodo synced <编号>…`。
"""

FIRST_NOTE = ("这个会话第一次用 cctodo，先读下面的指引（读完照着做）。刚才那条命令没有执行，读完再运行一次。")


def _first_use(sid: str) -> bool:
    """记下这个会话用过 cctodo；是第一次就返回 True（GUIDED）。没有会话编号的不拦。
    记录读不出来就报错（_load 抛 Bad），不当成没看过，也不把原来的盖掉。"""
    if not sid:
        return False
    with _lock:
        seen = _load(GUIDED)
        if sid in seen:
            return False
        now = time.time()
        keep = {k: v for k, v in seen.items() if now - v < GUIDED_DAYS * 86400}
        keep[sid] = now
        _save(GUIDED, keep)
    return True


USAGE = "参数不对，cctodo --help 看用法"
# 清单旧不旧：看待办时清单好几天没人改、或者没几条，多半漏了用户最近在做的事，
# 也说明用户多半还不会用这套东西。这时让 AI 讲完以后提一句、问要不要用回忆来更新；默认只提议，
# 因为用户说的是「看」，更新要改用户的文件、花用户的额度。想让 AI 直接补的人在自己的开头文件里写明（GUIDE）
STALE_DAYS = 2    # 最近一次改动早于这么多天就算旧了
THIN_ITEMS = 3    # 条目少于这么多就算单薄
NO_LIST = ("还没有登记待办清单。\n"
           "告诉用户：待办是一份清单，记着用户手上有哪些事、各做到哪一步；你可以用回忆（系统提示里「翻以前的会话」那段）"
           "把用户最近的会话过一遍，整理出一份。问用户要不要，答应了再看 cctodo guide 新建，建好后用 "
           "cctodo add <清单名> <设备>:<工作区路径> 登记；没答应就不要自己动手。\n")


def _when(t: float) -> str:
    return datetime.fromtimestamp(t, BJ).strftime("%m-%d %H:%M")


def _ago(t: float) -> str:
    s = time.time() - t
    if s < 3600:
        return "不到一小时前"
    if s < 86400:
        return f"{int(s // 3600)} 小时前"
    return f"{int(s // 86400)} 天前"


def _age_text(res: dict, items: list) -> str:
    """清单旧不旧：一共几条、最近一次改动在什么时候；旧了或者单薄时，再交代 AI 讲完以后问用户要不要用回忆更新。"""
    stamps = [t for t in [res.get("head_mtime"), *(it.get("mtime") for it in items)] if t]
    newest = max(stamps) if stamps else None
    open_n = sum(it["status"] in OPEN for it in items)
    out = [f"这份清单一共 {len(items)} 条（没做完的 {open_n} 条）"
           + (f"，最近一次改动在 {_when(newest)}（{_ago(newest)}）" if newest else "")
           + "；每个文件最后改动的时刻写在它的标题行里。"]
    old = newest is not None and time.time() - newest > STALE_DAYS * 86400
    thin = len(items) < THIN_ITEMS
    if old or thin:
        why = "、".join(([f"已经 {int((time.time() - newest) // 86400)} 天没人改过了"] if old else [])
                       + ([f"只有 {len(items)} 条"] if thin else []))
        out.append(f"这份清单{why}，多半没记上用户最近在做的事。照开头文件讲完以后，用一句话把这一点告诉用户，"
                   "问要不要你用回忆（系统提示里「翻以前的会话」那段）把最近的会话过一遍、更新这份清单；"
                   "用户答应了再动手，怎么更新见 cctodo guide 的「维护」。开头文件里写明了过时就直接补的，照开头文件做。")
    return "\n".join(out)


def _lists_text(book: dict) -> str:
    if not book["lists"]:
        return "还没有登记待办清单。新建一份先看 cctodo guide，建好后用 cctodo add <清单名> <设备>:<工作区路径> 登记。\n"
    rows = [f"- {l['name']}{'（默认）' if l['name'] == book['default'] else ''}：{where_text(l)}" for l in book["lists"]]
    return "登记的待办清单：\n" + "\n".join(rows) + "\n"


def _full(user: str, book: dict, lst: dict, sid: str) -> str:
    res = _read(user, lst, f"cctodo·{(sid or '')[:8]}", fresh=True)
    items = list(res["items"])
    random.shuffle(items)
    root = lst["path"]
    out = [f"清单「{lst['name']}」在{where_text(lst)}。下面先是开头文件，再是全部 {len(items)} 个条目文件的全文，"
           "条目的先后每次随机打乱，不代表轻重。全部读完再回答用户，一条都不能跳过；读完照开头文件里的说明去做。",
           "要改哪个文件，先用读文件的工具读它再改（这里的输出不算读过）；文件在别的设备上就 ssh 到那台设备去读写。改完不用告诉查看器。"
           "写回照 cctodo guide 的「一件事分几层写」和「维护」：条目、详情、资料一起改成最新的样子，不往后追加。"]
    out.append(_age_text(res, items))
    if res["errors"]:
        out.append("这份清单有不符合格式的地方，网页上暂时显示不了，先照 cctodo guide 里「条目文件」的要求改好：\n"
                   + "\n".join(f"- {e}" for e in res["errors"]))

    def changed(t):
        return f"（最后改动 {_when(t)}）" if t else ""
    if res["head"] is not None:
        out += ["", f"===== 开头文件：{root}/{HEAD}{changed(res.get('head_mtime'))} =====", res["head"].rstrip()]
    for k, it in enumerate(items, 1):
        out += ["", f"===== 条目 {k}/{len(items)}：{root}/{ITEMS}/{it['file']}{changed(it.get('mtime'))} =====", it["text"].rstrip()]
    ops = [op for op in book["pending"] if op["list"] == lst["name"]]
    if ops:
        out += ["", f"===== 网页上还没写进文件的操作（{len(ops)} 条）=====",
                "用户在网页的待办页上做了这些改动，页面上已经按改过的样子显示了。照实写进对应的条目文件（新建的照现有条目的样子建一个文件），"
                "这些是用户亲口定的；写好以后运行 cctodo synced <编号>…（全部写好了就 cctodo synced all）。"]
        for op in ops:
            target = "" if op["op"] == "new" else f"「{op.get('title', '')}」（{ITEMS}/{op['file']}）："
            out.append(f"- {op['id']}（{_when(op['t'])}）{target}{_op_text(op)}")
    others = [l["name"] for l in book["lists"] if l["name"] != lst["name"]]
    if others:
        out += ["", "另外还登记了：" + "、".join(f"{n}（cctodo read {n}）" for n in others)]
    return "\n".join(out) + "\n"


def handle(req: dict, user: str, sid: str) -> str:
    """cctodo 的一条命令：req 是 {argv}，返回要打到标准输出上的字；不成立就抛 Bad。"""
    argv = [str(x) for x in req.get("argv") or []]
    op, rest = (argv[0], argv[1:]) if argv else ("read", [])
    first = _first_use(sid)
    if op == "guide":
        return GUIDE
    if first:
        return FIRST_NOTE + "\n\n" + GUIDE
    book = _book(user)
    if op == "read":
        if len(rest) > 1:
            raise Bad(USAGE)
        if not book["lists"]:
            return NO_LIST
        name = rest[0] if rest else book["default"] or book["lists"][0]["name"]
        lst = next((l for l in book["lists"] if l["name"] == name), None)
        if not lst:
            raise Bad(f"没有叫「{name}」的清单。" + _lists_text(book).strip())
        return _full(user, book, lst, sid)
    if op == "lists":
        return _lists_text(book)
    if op == "add":
        flags = [x for x in rest if x.startswith("--")]
        plain = [x for x in rest if not x.startswith("--")]
        if len(plain) != 2 or any(f != "--default" for f in flags):
            raise Bad("add <清单名> <设备>:<工作区路径> [--default]")
        name = plain[0].strip()
        if not NAME.fullmatch(name):
            raise Bad("清单名最多 24 字，不能有空格开头、斜杠或冒号")
        dev, path = _spec(plain[1])
        ent = {"name": name, "device": dev, "path": path}
        res = _read(user, ent, f"cctodo·登记·{(sid or '')[:8]}", fresh=True)   # 连不上、读不了就不登记（抛 Bad）

        def fn(b):
            if any(l["name"] == name for l in b["lists"]):
                raise Bad(f"已经有叫「{name}」的清单了")
            if len(b["lists"]) >= MAX_LISTS:
                raise Bad(f"清单最多登记 {MAX_LISTS} 份")
            b["lists"].append(ent)
            if "--default" in flags or not b["default"]:
                b["default"] = name
        _change(user, fn)
        warn = ("\n这份清单有不符合格式的地方（网页上暂时显示不了）：\n" + "\n".join(f"- {e}" for e in res["errors"])) if res["errors"] else ""
        return f"登记好了：「{name}」，{where_text(ent)}，{len(res['items'])} 个条目。{warn}\n"
    if op in ("rm", "default"):
        if len(rest) != 1:
            raise Bad(f"{op} <清单名>")
        name = rest[0]

        def fn(b):
            if not any(l["name"] == name for l in b["lists"]):
                raise Bad(f"没有叫「{name}」的清单")
            if op == "default":
                b["default"] = name
                return
            b["lists"] = [l for l in b["lists"] if l["name"] != name]
            b["pending"] = [p for p in b["pending"] if p["list"] != name]
            if b["default"] == name:
                b["default"] = b["lists"][0]["name"] if b["lists"] else ""
        _change(user, fn)
        return f"「{name}」设成默认了\n" if op == "default" else f"去掉了「{name}」的登记（文件没动）\n"
    if op == "synced":
        if not rest:
            raise Bad("synced <操作编号>…，或者 synced all")
        holder = {}

        def fn(b):
            keep = [] if rest == ["all"] else [p for p in b["pending"] if p["id"] not in rest]
            if rest != ["all"]:
                unknown = [x for x in rest if not any(p["id"] == x for p in b["pending"])]
                if unknown:
                    raise Bad("没有这几个编号的操作：" + "、".join(unknown))
            holder["n"] = len(b["pending"]) - len(keep)
            b["pending"] = keep
        _change(user, fn)
        _files.clear()   # 刚改过文件：页面下次现读
        return f"标掉了 {holder['n']} 条网页上的操作\n"
    if op in ("ls", "show", "mine", "edit", "progress", "unknown", "claim", "release", "done", "drop", "log"):
        raise Bad("待办改版了：查看器只做清单的入口，清单的内容是工作区里的文件，直接改文件。读清单只用 cctodo，用法见 cctodo --help")
    raise Bad(f"不认识的操作 {shlex.quote(op)}（cctodo --help 看用法）")


def reset_cache():
    """测试用：下次读的时候重新读文件。"""
    with _lock:
        _cache.clear()
        _broken.clear()
        _moved.clear()
        _files.clear()
