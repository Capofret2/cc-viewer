"""会话记录里的 API 调用：每次调用（一个回复编号）是什么模型、什么时候发的、四个 token 数各多少。

查看器里所有的 token 数都从这里来，只此一处：气泡上的四个数（session.py）、会话的总消耗、账本（users.py）、
拟合额度单价（analysis/）。哪个号发的不在这里，见 api_calls.py。

CLI 把同一条回复按内容块拆成好几行写，每行都抄一份用量，靠前的行可能是流式写到一半的中间数，
所以同一个回复编号的四个数各取见过的最大值（merge）。出错时 CLI 自己补的回复（模型是 <synthetic>）不是真的调用，不算。
子代理的记录在会话旁边的 <会话编号>/subagents/ 里，和主会话合在一起算。

会话文件只会往后追加，每个文件记着读到哪儿，下次只读新增的部分；文件变短了（被改写）就从头再来。
只解析 Claude 回复的行（行里有 "type":"assistant" 和 "usage"），工具结果那种可能有整份文件内容的行不解析。
只用标准库：状态栏脚本也要导入它。可以单独跑：python3 calls.py <会话文件>…
"""
import hashlib
import json
import os
import pickle
import sys
import threading
import time

import hostfs
import memtrim
import peak
from hostfs import Path   # 会话记录在运行 Claude 的那个用户名下，一律经它读（见 hostfs.py）

PROJECTS = hostfs.HOME / ".claude" / "projects"   # 各号的 projects 都软链接到这里
FIELDS = (("in", "input_tokens"), ("cw", "cache_creation_input_tokens"),
          ("cr", "cache_read_input_tokens"), ("out", "output_tokens"))


def usage_of(msg: dict) -> dict | None:
    """一行回复里的四个数；这一行没带用量就是 None。"""
    u = msg.get("usage")
    if not u:
        return None
    out = {k: u.get(f) or 0 for k, f in FIELDS}
    # 一小时写入是 cw 的子集，只在有独立 API 价的号上补差价；不改变订阅号的四项计费。
    out["cw1h"] = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
    return out


def merge(old: dict | None, new: dict) -> dict:
    return {k: max(old.get(k, 0), new.get(k, 0)) for k in old.keys() | new.keys()} if old else dict(new)


def parse(raw: bytes) -> tuple[str, dict] | None:
    """一行会话记录 → (回复编号, {model, ts, use})；不是一次真的调用就是 None。"""
    if b'"type":"assistant"' not in raw or b'"usage"' not in raw:
        return None
    try:
        e = json.loads(raw)
    except ValueError:
        return None
    msg = e.get("message") if e.get("type") == "assistant" else None
    if not isinstance(msg, dict) or not msg.get("id") or msg.get("model") == "<synthetic>":
        return None
    use = usage_of(msg)
    # 按时段计价的模型（DeepSeek）在空闲时段的调用换成单价表里那一条的名字（peak.py）
    model = peak.tag(msg.get("model") or "", e.get("timestamp"))
    return (msg["id"], {"model": model, "ts": e.get("timestamp"), "use": use}) if use else None


CHUNK = 8 << 20   # 大文件分块读，每块这么多字节（session.py、spendmap.py 也用它）


def scan(path: Path, start: int = 0) -> tuple[dict[str, dict], int]:
    """从 start 往后读完整的行：返回这一段里的调用（回复编号 → {model, ts, use}，按出现顺序）和读到了哪儿。
    最后一行可能还没写完，留到下次。分块读：第一次碰到大会话时要从头读几百 MB，一口气读进来内存会冲得很高（见 memtrim.py）。
    读到一半出错，已经读完的那些行照样算数，下次从停下的地方接着读。"""
    calls, off, _ = _scan(path, start)
    return calls, off


HEAD = 4096   # 存盘时记下文件开头这么多字节的指纹，读盘时对一下，认出文件被整个换掉或改写过


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _scan(path: Path, start: int = 0, stop: int | None = None) -> tuple[dict[str, dict], int, dict | None]:
    """scan，外加这个文件的 inode 和（从头读时）开头一段的指纹：{ino, head_len, head}，存盘时用（见 _restore）。
    stop 不为 None 就只读到这个位置为止（预写抽查存盘时，从头读到存下的位置对一遍，见 prewarm_cache.py）。"""
    calls: dict[str, dict] = {}
    off, rest, meta = start, b"", None   # off 是 rest 开头在文件里的位置
    try:
        with Path(path).open("rb") as f:
            meta = {"ino": os.fstat(f.fileno()).st_ino}
            f.seek(start)
            pos = start   # 读到了文件的哪儿（stop 用；off 只到最后一个完整行）
            while (stop is None or pos < stop) and (chunk := f.read(CHUNK if stop is None else min(CHUNK, stop - pos))):
                pos += len(chunk)
                data = rest + chunk
                end = data.rfind(b"\n") + 1
                if start == 0 and off == 0 and end:   # 开头那几行已经写完、不会再变（文件只往后追加）
                    n = min(HEAD, end)
                    meta.update(head_len=n, head=_digest(data[:n]))
                for raw in data[:end].split(b"\n"):
                    if hit := parse(raw):
                        mid, c = hit
                        old = calls.get(mid)
                        calls[mid] = {**c, "ts": old["ts"], "use": merge(old["use"], c["use"])} if old else c
                off, rest = off + end, data[end:]
    except OSError:
        pass
    memtrim.read(off - start)
    return calls, off, meta


_files: dict[Path, dict] = {}   # 文件 → {off 读到哪儿, calls 回复编号 → {model, ts, use}, ino, head_len, head（存盘用）}
_lock = threading.Lock()


def file_calls(path: Path, size: int | None = -1) -> dict[str, dict]:
    """这个文件里的全部调用（读新增的部分并进缓存）。size 是调用的地方已经查好的大小（None 是查不了），不给就自己查。"""
    st = _files.get(path)
    given = size != -1
    if not given:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = None
    if size is None:
        return st["calls"] if st else {}
    if st is None and _disk_dir is not None and (st := _restore(path)) is not None:
        _files[path] = st   # 重启以前存下的（见「存盘」），接着往后读
    if st and size < st["off"] and given:   # 给的大小是清点那一刻的，这期间别处已经读得更远了：当场再查一次，真变短了才从头来
        try:
            size = Path(path).stat().st_size
        except OSError:
            return st["calls"]
    if not st or size < st["off"]:
        st = _files[path] = {"off": 0, "calls": {}}
    if size > st["off"]:
        new, off, meta = _scan(path, st["off"])
        if meta and st.get("ino", meta["ino"]) != meta["ino"]:   # 同一个路径换成了别的文件：接着旧位置读是错的，从头来
            st = _files[path] = {"off": 0, "calls": {}}
            new, off, meta = _scan(path, 0)
        st["off"] = off
        if meta:
            st.update(meta)
        for mid, c in new.items():
            old = st["calls"].get(mid)
            st["calls"][mid] = {**c, "ts": old["ts"], "use": merge(old["use"], c["use"])} if old else c
        _dirty.add(path)
    return st["calls"]


# ---------- 存盘 ----------
# 重启以后查看器账号第一次打开「全部会话」，要把全组几千个会话和子代理文件（十几 GB）从头读一遍才拿得到调用，单独实测 46 秒，
# 这期间它还占着全局锁拖慢所有请求。所以每个文件读到哪儿、里面的调用存在数据目录里（一个文件一份，只重写变了的），
# 重启后第一次用到这个文件时先读存下的，再从存下的位置接着往后读。只有查看器打开（enable_disk），状态栏脚本导入时不存不读。
# 存下的对不上就当没存（从头读、记一行日志）：代码改了（按 calls.py、peak.py 整个文件算的版本）、文件换了 inode、
# 文件比存的时候短、开头一段的指纹变了（被改写过；mark_as_cli 原地改第一行也会这样，从头读一遍就是了）。
# 版本故意按整个文件算、不像花费存盘那样只看相关的代码（codever.py）：这里读出的调用直接进账本，宁可多作废、多读一遍，
# 不能有漏算的代码让旧结果进账（账本要谨慎）。重启前会先预写（prewarm_cache.py），多读的那一遍旧实例还在照常服务。
# 用 pickle：数据目录只有查看器自己读写，缓存目录再收紧成 0700、文件 0600
SAVE_SECS = 120
VERSION = None   # enable_disk 时算；状态栏脚本不存盘，不用算
_disk_dir = None
_dirty: set = set()
_disk_lock = threading.Lock()   # 存盘只许一个线程在做


def enable_disk(folder):
    """查看器启动时调：调用表存在 folder 里（数据目录下），并开一个线程每 SAVE_SECS 秒把变了的写下去。"""
    global _disk_dir, VERSION
    # 版本连 peak.py 一起算：节假日表改了，存下的调用里打的时段标记也要重打。自己的源码以自己的身份读（代码目录不给 Claude 那边的用户读）
    with open(__file__, "rb") as src, open(peak.__file__, "rb") as pk:
        VERSION = _digest(src.read() + pk.read())
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    _disk_dir = str(folder)
    with os.scandir(_disk_dir) as it:   # 上次写到一半就退出（被杀、崩溃）留下的临时文件，清掉
        for e in it:
            if e.name.endswith(".tmp"):
                try:
                    os.remove(e.path)
                except OSError:
                    pass
    threading.Thread(target=_saver, name="calls-save", daemon=True).start()


def _disk_name(path: Path) -> str:
    who = getattr(path, "run_as", None) or ""
    return os.path.join(_disk_dir, _digest(f"{who}\0{path}".encode()) + ".pkl")


def _restore(path: Path) -> dict | None:
    """读这个文件存下的调用表，对得上就交回去（同 _files 里的一项），对不上或没存过是 None。"""
    name = _disk_name(path)
    try:
        with open(name, "rb") as f:
            rec = pickle.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"调用表存盘读不了，从头读 {path}：{type(e).__name__}: {e}", flush=True)
        return None
    why = ""
    if rec.get("v") != VERSION:
        why = "calls.py 或 peak.py 改过了"
    elif rec.get("path") != str(path) or rec.get("run_as") != getattr(path, "run_as", None):
        why = "存的不是这个文件"
    else:
        try:
            with Path(path).open("rb") as f:
                st = os.fstat(f.fileno())
                head = f.read(rec["head_len"])
        except OSError as e:
            return None   # 文件打不开：照旧走下面 file_calls 自己查不了的路
        if st.st_ino != rec["ino"]:
            why = "文件换了（inode 不一样）"
        elif st.st_size < rec["off"]:
            why = "文件比存的时候短"
        elif len(head) != rec["head_len"] or _digest(head) != rec["head"]:
            why = "文件开头被改写过"
    if why:
        print(f"调用表存盘对不上（{why}），从头读 {path}", flush=True)
        return None
    return {k: rec[k] for k in ("off", "calls", "ino", "head_len", "head")}


def save_dirty() -> int:
    """把读过新内容的那些文件的调用表写下去，返回写了几个。先在锁里抄一份，写盘在锁外（几十 MB 的 pickle 要零点几秒）。"""
    if _disk_dir is None:
        return 0
    with _disk_lock:
        with _lock:
            todo = [(p, {**_files[p], "calls": dict(_files[p]["calls"])}) for p in _dirty
                    if p in _files and _files[p]["off"] and "ino" in _files[p]]
            _dirty.clear()
        t, n = time.monotonic(), 0
        for p, st in todo:
            if "head" not in st:   # 第一次读到它时开头一行还没写完，没记下指纹：这里补读一次
                try:
                    with Path(p).open("rb") as f:
                        if os.fstat(f.fileno()).st_ino != st["ino"]:
                            continue   # 已经换成别的文件了，下次读到时会从头来
                        head = f.read(min(HEAD, st["off"]))
                except OSError:
                    continue
                st.update(head_len=len(head), head=_digest(head))
            rec = {"v": VERSION, "path": str(p), "run_as": getattr(p, "run_as", None),
                   **{k: st[k] for k in ("off", "calls", "ino", "head_len", "head")}}
            name = _disk_name(p)
            tmp = f"{name}.{os.getpid()}.tmp"
            try:
                with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                    pickle.dump(rec, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, name)
                n += 1
            except OSError as e:
                print(f"调用表存盘没写成 {p}：{e}", flush=True)
                with _lock:
                    _dirty.add(p)   # 下一圈再试
        if time.monotonic() - t > 1:
            print(f"调用表存盘：写了 {n} 个文件，用了 {time.monotonic() - t:.1f} 秒", flush=True)
        return n


def _saver():
    while True:
        time.sleep(SAVE_SECS)
        try:
            save_dirty()
        except Exception as e:
            print(f"调用表存盘出错：{type(e).__name__}: {e}", flush=True)


TREE = ("*/*.jsonl", "*/*/subagents/*.jsonl")   # projects 底下的会话文件和子代理文件


class Snapshot:
    """一次清点到的 projects 底下全部会话和子代理文件的大小、修改时间（hostfs.tree，一个根目录一趟）。
    总览要给几百张卡片各查一遍主会话和子代理文件变没变（几千个文件），经小助手一个个问要好几秒；
    同一次请求里改从这里拿，和当场一个个去问是同一回事。清点里没有的主会话（清点以后才出现的、那个根目录没清点成的）照旧一个个问。"""

    def __init__(self):
        self.stats: dict[str, list] = {}   # 路径 → [大小, 修改时间]
        self.subs: dict[str, list[str]] = {}   # 会话目录（主会话文件去掉 .jsonl）→ 里面 subagents/ 下的文件名
        # 清点成了的根目录 → 会话编号 → 主会话文件相对根目录的路径（find_in 按编号找会话用，不用再一个个去问）
        self.mains: dict[str, dict[str, str]] = {}

    def add(self, stats: dict[str, list]):
        self.stats.update(stats)
        for s in stats:
            folder, name = s.rsplit("/", 1)
            if folder.endswith("/subagents"):
                self.subs.setdefault(folder[:-len("/subagents")], []).append(name)

    def has(self, path) -> bool:
        return str(path) in self.stats


def snapshot(roots) -> Snapshot:
    """清点这几个 projects 根目录。哪个根目录清点不成（比如独立环境的小助手还是旧的拷贝），那里的会话就照旧一个个问，结果一样，只是慢些。"""
    snap = Snapshot()
    for root in roots:
        try:
            stats = hostfs.tree(root, TREE)
        except OSError as e:
            print(f"清点 {root} 没成，那里的会话改成一个个查：{e}", flush=True)
            continue
        snap.add(stats)
        base, mains = str(root).rstrip("/") + "/", snap.mains.setdefault(str(root), {})
        for s in stats:
            rel = s[len(base):] if s.startswith(base) else ""
            if rel.count("/") == 1 and rel.endswith(".jsonl"):   # 工作区/编号.jsonl，子代理的在更深一层
                mains.setdefault(rel.rsplit("/", 1)[1][:-len(".jsonl")], rel)
    return snap


def files_of(main: Path, snap: Snapshot | None = None) -> list[Path]:
    """主会话的文件，加上它的子代理文件。给了清点结果、里面又有这个会话时，子代理文件从清点里拿，不再去找。"""
    main = Path(main)
    if snap is not None and snap.has(main):
        folder = main.with_suffix("")
        return [main, *sorted(folder / "subagents" / name for name in snap.subs.get(str(folder), ()))]
    return [main, *sorted(main.with_suffix("").glob("subagents/*.jsonl"))]


def file_sizes(main: Path, snap: Snapshot | None = None) -> list[tuple[Path, int | None]]:
    """会话（主会话加子代理）的每个文件和它现在的大小，查不了的是 None。"""
    out = []
    for p in files_of(main, snap):
        hit = snap.stats.get(str(p)) if snap is not None and snap.has(main) else None
        if hit is not None:
            out.append((p, hit[0]))
            continue
        try:
            out.append((p, p.stat().st_size))
        except OSError:
            out.append((p, None))
    return out


def calls_of(sizes: list[tuple[Path, int | None]]) -> dict[str, dict]:
    """file_sizes 列出的这些文件里的全部调用：回复编号 → {model, ts, use}。"""
    out: dict[str, dict] = {}
    with _lock:
        for p, size in sizes:
            out.update(file_calls(p, size))
    # 归属可能比回复晚到：取出时才选账号价，不把尚未认主的结果固化进调用缓存。
    import api_calls
    import rates
    import platform_costs
    platform_costs.ensure_prices()
    result = {}
    for mid, c in out.items():
        account = api_calls.INDEX.account(mid)
        result[mid] = {**c, "model": rates.billing_model(c["model"], account),
                       "use": platform_costs.quote(mid, account, c["model"], c["use"])}
    return result


def session_calls(main: Path | str, snap: Snapshot | None = None) -> dict[str, dict]:
    """一个会话（主会话加子代理）的全部调用：回复编号 → {model, ts, use}。"""
    return calls_of(file_sizes(Path(main), snap))


_where: dict[Path, dict[str, Path]] = {}   # 运行身份及会话根目录 → 编号 → 文件，不能跨用户共用索引
_where_lock = threading.Lock()


def find(sid: str) -> Path | None:
    """按会话所属身份查索引；未命中才重列该环境的工作区，不扫描别人的目录。"""
    import isolation
    env = isolation.for_sid(sid)
    root = env.projects if env else PROJECTS
    p = _where.get(root, {}).get(sid)
    if p is not None and p.exists():
        return p
    with _where_lock:
        found: dict[str, Path] = {}
        for q in root.glob("*/*.jsonl"):
            found.setdefault(q.stem, q)
        _where[root] = found
    return found.get(sid)


def find_in(snap: Snapshot | None, sid: str) -> Path | None:
    """和 find 一样按会话所属身份找文件，但从一次清点（snapshot）里拿：余额看板、用户管理要给几百个会话各找一遍，
    经小助手一个个确认、找不到就重列整个环境，几千个会话的实例要十几秒。那个根目录清点成了、里面没有，就是没有；
    没清点成（或者没给清点结果）照旧 find。路径从根目录对象拼出来，独立环境的照样带着那个环境的身份"""
    import isolation
    env = isolation.for_sid(sid)
    root = env.projects if env else PROJECTS
    mains = snap.mains.get(str(root)) if snap is not None else None
    if mains is None:
        return find(sid)
    rel = mains.get(sid)
    return root / rel if rel else None


def by_model(calls) -> dict[str, dict]:
    """一串调用按模型加起来：{模型: {in, cw, cr, out, n 几次}}，前端换算成点数。"""
    out: dict[str, dict] = {}
    for c in calls:
        tot = out.setdefault(c["model"], {**dict.fromkeys(c["use"], 0), "n": 0})
        for k, v in c["use"].items():
            tot[k] = tot.get(k, 0) + v
        tot["n"] += 1
    return out


if __name__ == "__main__":
    for a in sys.argv[1:]:
        print(a, by_model(session_calls(a).values()))
