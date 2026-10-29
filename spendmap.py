"""会话花了多少、花在哪：上下文药丸卡片里「花费」视角的环形图（画法和上下文视角一样：里圈粗外圈细，外圈落在所属扇形里）。

第一圈是计费的几项：缓存写入、缓存命中、输出。输入并进缓存写入、不单列：GPT 没有缓存写入，
没命中的前缀全记在输入里，可它会被自动缓存、下次命中，就是缓存写入；Claude 的输入只有零星几个 token，一起并了。
并起来的这块按输入、缓存写入各自的单价算钱（write_of），总数不变。
每次请求的四个数是 API 准数，按 points_rates.json 的单价（rates.py）换成点数，前端再换成分；
调用只算记在这个会话名下的（users.own_calls，和标签页上的总消耗同一份）。
往外一圈是哪一轮（用户的哪句话引起的），再往外是这一轮里的每次请求（子代理的请求先归到「子代理：任务说明」底下，
子代理按发起它的那次工具调用归到那一轮），最外圈是这次请求的这一项具体花在什么上：
- 输出：说的话、思考、各个工具调用。看得见的字按字数算，剩下的算思考（和上下文视角一样，见 ctxmap.split_reply）。
- 缓存写入：写进去的是上一次请求以来新进上下文的东西（上一条回复、工具结果、用户的话、附加信息……），
  量是两次请求的上下文之差（准数），各条按字数分；写入比新内容多出来的，是把已有的历史重新写了一遍
  （缓存没命中：文件里的第一次请求、续接以后、换了模型或思考强度、压缩之后）。
- 缓存命中：就是这次请求本身，不再往下分。
"""
import hashlib
import json
import os
import pickle
import threading
import time
from collections import OrderedDict

import api_calls
import calls
import memtrim
from hostfs import Path   # 会话记录在运行 Claude 的那个用户名下，经它读（见 hostfs.py）
import rates
import platform_costs
from ctxmap import CALL, finish, grow, split_reply, units_of

CHUNK = calls.CHUNK

KINDS = (("cw", "缓存写入"), ("cr", "缓存命中"), ("out", "输出"))
RATIO = 1.3   # 字数估的 token 和准数的大致比例（代码、英文按字数会估少），只用来在一组里和回复的准数一起分摊
TALK = f"{CALL}的话"


def write_of(use: dict, rate: dict) -> tuple[float, float]:
    """「缓存写入」这一块（含输入）有多少 token、每百万 token 几点：输入和缓存写入的单价可能不一样，按量加权，总钱数不变。"""
    tok = use["cw"] + use["in"]
    return tok, (use["cw"] * rate["cw"] + use["in"] * rate["in"]
                 + use.get("cw1h", 0) * rate.get("cw1h", 0)) / tok if tok else 0.0


HEAD = 4096   # 记下文件开头这么多字节：再读时对不上，就是文件被整个换掉或改写了，从头重拆


class FileReqs:
    """一个记录文件（主会话或一个子代理）里的请求，按顺序：mid 回复编号、u 第一行编号、属于哪一轮、out 回复拆成的几块、
    new 上次请求以来新进上下文的几块、ctx 这次请求的上下文多大。
    主会话里用户每说一句话开一轮、记进 turns；子代理整个文件归在一轮里，归哪一轮要等出图时按发起它的调用定（compose），
    先放一个占位的 turn，到时候把轮次填进去。顺手记下每个工具调用在哪一轮（tool_turn），子代理靠它归轮。

    增量拆：记着读到哪儿和拆到一半的状态（tools、pending、by……），下次只读新追加的完整行。
    每次都把主会话和全部子代理从头解析一遍的话，一个 291MB 的会话加 190 个子代理（462MB）要 7.6 秒，线上十到十二秒，
    会话一长缓存就失效，每次悬停都重来。文件变短、换了 inode、开头的字节对不上就从头重拆。"""

    def __init__(self, path: Path, cwd: str, main: bool):
        self.path, self.cwd, self.main = path, cwd, main
        self.saved = None   # 存盘时的 seen（见「存盘」）：和现在的一样就不用再存
        self.reset()

    # 存盘不带路径和工作目录：路径对象带着以谁的身份读（run_as），pickle 会丢掉它；读回来时换成当时的（见 restore）
    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k not in ("path", "cwd")}

    def __setstate__(self, state):
        self.__dict__.update(state)

    def reset(self):
        self.off, self.ino, self.head, self.seen = 0, None, b"", None
        self.tools: dict = {}
        self.reqs: list[dict] = []
        self.pending: list[dict] = []
        self.by: dict[str, dict] = {}
        self.tool_turn: dict = {}
        self.turns: list | None = [] if self.main else None
        self.turn = {"n": "（第一句话之前）" if self.main else "", "at": ""}
        self.first = self.turn
        self.first_ts: str | None = None   # 第一条有 type 的行的时间：子代理按它排先后
        self.meta: dict = {}

    def update(self, st, upto: int | None = None) -> int:
        """按这一刻的 stat 把新追加的完整行拆进来，返回读了多少字节。upto 只给测试用：只读到这个位置为止。"""
        if upto is None and (st.st_ino, st.st_size, st.st_mtime_ns) == self.seen:
            return 0
        if st.st_ino != self.ino or st.st_size < self.off:
            self.reset()
        self.ino = st.st_ino
        size = st.st_size if upto is None else min(upto, st.st_size)
        begin = self.off
        with self.path.open("rb") as f:
            if self.head and f.read(len(self.head)) != self.head:
                self.reset()
                self.ino, begin = st.st_ino, 0
            f.seek(self.off)
            pos, rest = self.off, b""
            while pos < size and (chunk := f.read(min(CHUNK, size - pos))):
                pos += len(chunk)
                data = rest + chunk
                end = data.rfind(b"\n") + 1
                if self.off == 0 and end:
                    self.head = data[:min(HEAD, end)]
                for x in data[:end].split(b"\n"):
                    self.feed(x)
                self.off += end
                rest = data[end:]
        if upto is None:
            self.seen = (st.st_ino, st.st_size, st.st_mtime_ns)
        return self.off - begin

    def feed(self, x: bytes):
        if not x.strip():
            return
        try:
            d = json.loads(x)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        if self.first_ts is None and d.get("type"):
            self.first_ts = d.get("timestamp") or ""
        units = units_of(d, self.tools, self.cwd)
        if self.main:
            words = next((u["p"][-1] for u in units if u["p"][0] == TALK), None)
            if words is not None:
                self.turn = {"n": f"{units[0]['ts']} {words}", "at": d.get("timestamp") or ""}
                self.turns.append(self.turn)
        for u in units:
            if "reply" in u:
                r = self.by.get(u["reply"])
                if r is None:   # prev_ctx：同一个文件里上一次请求的上下文，两者之差就是这次新写进缓存的
                    r = self.by[u["reply"]] = {"mid": u["reply"], "u": u["u"], "ts": u["ts"], "turn": self.turn, "out": [],
                                               "new": self.pending, "ctx": u["ctx"],
                                               "prev_ctx": self.reqs[-1]["ctx"] if self.reqs else None}
                    self.reqs.append(r)
                    self.pending = []
                r["ctx"] = max(r["ctx"], u["ctx"])
                r["out"].append(u)
            self.pending.append(u)   # 回复本身也是下一次请求要写进缓存的新内容
        if d.get("type") == "assistant":
            for b in (d.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"):
                    self.tool_turn[b.get("id")] = self.turn

    def load_meta(self):
        """子代理的说明（任务描述、类型、由哪次工具调用发起）；还没写出来的下次出图时再读。"""
        if not self.meta:
            try:
                self.meta = json.loads(self.path.with_suffix("").with_suffix(".meta.json").read_text())
            except (OSError, ValueError):
                self.meta = {}


def req_label(r: dict) -> str:
    """一次请求写成什么：说了话就写话的开头，没说话写调用了什么，都没有就是只想了想。"""
    said = next((u["p"][-1] for u in r["out"] if u["p"][0] == "Claude 的回复"), None)
    if said:
        return f"{r['ts']} {said}"
    tool = next((u["p"] for u in r["out"] if u["p"][0] == "工具调用"), None)
    return f"{r['ts']} 调用 {tool[1]} · {tool[2]}" if tool else f"{r['ts']} 思考"


def part_name(u: dict) -> str:
    """回复或新内容里的一块，在最外圈叫什么。"""
    p = u["p"]
    if p[0] == "Claude 的回复":
        return f"用户消息：{p[-1]}"
    if p[0] in ("工具调用", "工具结果"):
        return f"{p[0]} {p[1]} · {p[2]}"
    return p[0] if p[0] in ("思考", "压缩摘要") else f"{p[0]}：{p[-1]}"


def build(path: Path, files: dict[str, FileReqs], own: dict[str, dict]) -> dict:
    """拆好的各个文件 → 花费树。分摊、建树每次都重算：单价、限时特惠、哪些调用记在这个会话名下都可能变。"""
    table = rates.load(price=True)   # 花费视角写的是花了多少钱，用收钱的单价（乘过 rates.PRICE）
    main = files.get(str(path)) or FileReqs(path, "", True)   # 主会话读不了就当它是空的（以前也是这样）
    turns, first = main.turns, main.first
    tool_turn = dict(main.tool_turn)
    jobs = [(None, r) for r in main.reqs]
    # 子代理：按开始时间排，好让套娃的子代理找得到发起它的那次调用在哪一轮
    subs = [fr for k, fr in files.items() if k != str(path)]
    for fr in sorted(subs, key=lambda fr: fr.first_ts or ""):
        fr.load_meta()
        meta, at = fr.meta, fr.first_ts or ""
        turn = tool_turn.get(meta.get("toolUseId")) or next((t for t in reversed(turns) if t["at"] <= at), first)
        fr.turn.update(n=turn["n"], at=turn["at"])   # 填进占位：这个子代理的请求和它发起的调用都指着它
        tool_turn.update(fr.tool_turn)
        name = f"子代理：{meta.get('description') or meta.get('agentType') or fr.path.stem}"
        jobs += [(name, r) for r in fr.reqs]
    # 回复的几块上留着上一次出图分到的 token 数（split_reply 直接写在块上），先清掉：
    # 缓存写入按「回复刚分到的输出」当分量，这次没分到的不能拿上次的数顶上
    for _, r in jobs:
        for u in r["out"]:
            u.pop("t", None)
            u.pop("e", None)

    root = {"n": "花费"}
    seen, unrated, total = set(), 0, [0.0]

    def rate_of(mid, c):
        """这次调用的单价。限时特惠不改单价（只决定花费里几成进账本，见 users._split），所以照单价表。"""
        r = rates.rate(table, c["model"])
        if r and "charge_yuan" in c["use"]:
            # 平台只给总实扣，分项按原有用量权重分摊，不伪造平台的分项单价。
            raw = {**c, "use": {k: v for k, v in c["use"].items() if k != "charge_yuan"}}
            nominal = rates.cost(table, raw)
            if nominal:
                factor = rates.cost(table, c) / nominal
                return {k: value * factor for k, value in r.items()}
        return r

    def put(kind, base, leaf):
        grow(root, [kind, *base], leaf)
        total[0] += leaf["t"]

    for sub, r in jobs:
        c = own.get(r["mid"])
        if not c or r["mid"] in seen:
            continue
        seen.add(r["mid"])
        rate = rate_of(r["mid"], c)
        if not rate:
            unrated += 1
            continue
        use, label = c["use"], req_label(r)
        base = [r["turn"]["n"], *([sub] if sub else [])]
        if 'charge_yuan' in use and not any(use.get(k, 0) for k in rates.KEYS):
            put('平台扣费', base, {'t': rates.cost(table, c), 'tok': 0, 'e': 0, 'u': r['u'], 'ts': r['ts'], 'label': '平台只返回费用，没有可分摊的用量'})
            continue
        cw, wrate = write_of(use, rate)
        rate = {**rate, "cw": wrate}   # 下面缓存写入一律按含输入的单价；另拷一份，rate_of 给的可能就是单价表本身
        pts = lambda tok, k: tok * rate[k] / 1e6   # noqa: E731
        if use["cr"]:
            put("缓存命中", base, {"t": pts(use["cr"], "cr"), "tok": use["cr"], "e": 0, "u": r["u"], "ts": r["ts"], "label": label})
        # 输出：看得见的字按字数，剩下的给思考；拆出来的 t 先是 token 数
        split_reply(r["out"], use["out"], True, RATIO)
        for u in r["out"]:
            if u["t"] > 0:
                put("输出", [*base, label], {"t": pts(u["t"], "out"), "tok": u["t"], "e": u["e"], "u": u["u"], "ts": u["ts"],
                                            "label": part_name(u)})
        if (lost := use["out"] - sum(u["t"] for u in r["out"])) > 0.5:   # 回复的内容块没写进记录（中途断了之类），钱照样算
            put("输出", [*base, label], {"t": pts(lost, "out"), "tok": lost, "e": 0, "u": r["u"], "ts": r["ts"],
                                        "label": "记录里没写出内容的输出"})
        # 缓存写入：新内容按两次请求的上下文之差（准数）分，回复的块用它刚分到的输出 token 当分量；多写的是重写历史
        if cw:
            prev = r["prev_ctx"]
            # 上一次请求不算在这个会话名下（或没单价）时它的回复没分过，按字数
            weights = [(u, u["t"] if "reply" in u and "t" in u else u["w"] * RATIO) for u in r["new"]]
            wsum = sum(w for _, w in weights)
            delta = r["ctx"] - prev if prev else 0
            fresh = min(cw, delta if delta > 0 else wsum)
            for u, w in weights:
                if wsum and w:
                    put("缓存写入", [*base, label], {"t": pts(fresh * w / wsum, "cw"), "tok": fresh * w / wsum,
                                                 "e": int(len(weights) > 1), "u": u["u"], "ts": u["ts"], "label": part_name(u)})
            again = cw - (fresh if wsum else 0)
            if again > 0:
                put("缓存写入", [*base, label], {"t": pts(again, "cw"), "tok": again, "e": 0, "u": r["u"], "ts": r["ts"],
                                             "label": "重新写入已有的历史（缓存没命中）" if prev else "第一次写入：系统提示词、工具和已有的历史"})
    # 记录里找不到的调用（文件被改写过之类）照样算钱，单独一块，不然总数和标签页上的对不上
    for mid, c in own.items():
        if mid in seen:
            continue
        rate = rate_of(mid, c)
        if not rate:
            unrated += 1
            continue
        if 'charge_yuan' in c['use'] and not any(c['use'].get(k, 0) for k in rates.KEYS):
            put('平台扣费', ['记录里找不到的调用'], {'t': rates.cost(table, c), 'tok': 0, 'e': 0, 'u': None, 'ts': '', 'label': '平台只返回费用，没有可分摊的用量'})
            continue
        cw, wrate = write_of(c["use"], rate)
        rate = {**rate, "cw": wrate}
        for k, name in KINDS:
            if tok := cw if k == "cw" else c["use"][k]:
                put(name, ["记录里找不到的调用"], {"t": tok * rate[k] / 1e6, "tok": tok, "e": 0, "u": None,
                                           "ts": "", "label": mid[-8:]})
    # 第一圈的计费项各自收尾、按固定顺序排，再小也不合进「其余」
    kids = [finish(root["kids"][name], total[0], 4) for _, name in (*KINDS, ('charge_yuan', '平台扣费')) if name in (root.get("kids") or {})]
    return {"n": "花费", "t": round(sum(c["t"] for c in kids), 4), "k": sum(c["k"] for c in kids),
            "e": int(any(c["e"] for c in kids)), "tok": sum(c.get("tok", 0) for c in kids), "c": kids,
            "unrated": unrated, "calls": len(own)}


class SessionSpend:
    """一个会话的各个文件拆到哪儿了，加上最近一次出的图。"""

    def __init__(self, cwd: str, use_disk: bool = True):
        self.cwd = cwd
        self.use_disk = use_disk   # compose（从头拆、对照用）不读不写存盘
        self.lock = threading.Lock()   # 同一个会话同时只许一个线程拆、出图：拆的状态和回复块上的数都是共用的
        self.files: dict[str, FileReqs] = {}
        self.key: tuple | None = None
        self.tree: dict | None = None

    def refresh(self, path: Path, own: dict, upto: int | None = None) -> dict:
        """主会话和子代理文件都没变、调用数没变就直接给上次的图（单价换了要重启才生效，和账本一样）；
        变了只读新追加的部分再出图。upto 只给测试用：主会话只读到这个位置。"""
        stats = []
        for f in calls.files_of(path):
            try:
                stats.append((f, f.stat()))
            except OSError:   # 列出来以后没了的，和以前一样当它不在
                pass
        key = (tuple((str(f), st.st_ino, st.st_size, st.st_mtime_ns) for f, st in stats), len(own), platform_costs.version())
        if key == self.key and upto is None:
            return self.tree
        files, read, ok = {}, 0, upto is None
        disk = bool(_disk_dir) and self.use_disk and upto is None
        for f, st in stats:
            main = str(f) == str(path)
            fr = self.files.get(str(f)) or (disk and restore(f, self.cwd, main, st)) or FileReqs(f, self.cwd, main)
            try:
                read += fr.update(st, upto if fr.main else None)
            except OSError as e:   # 读不了的文件先用已经拆好的那些，下次再试
                print(f"拆花费时读 {f} 失败：{e}", flush=True)
                ok = False
            files[str(f)] = fr
        self.files = files
        self.tree = build(path, files, own)
        self.key = key if ok else None
        memtrim.read(read)
        if disk:   # 不再变化的文件拆好的存盘，在后台写（见「存盘」）
            now = time.time()
            todo = [files[str(f)] for f, st in stats
                    if now - st.st_mtime > STABLE_SECS and files[str(f)].seen == (st.st_ino, st.st_size, st.st_mtime_ns)
                    and files[str(f)].saved != files[str(f)].seen]
            if todo:
                threading.Thread(target=_save, args=(self, todo), name="spend-save", daemon=True).start()
        return self.tree


# 最多留几个会话拆好的东西，多了扔掉最久没看的。实测：291MB 加 196 个子代理的会话留着 36MB，
# 36MB 加 959 个子代理（子代理共 6GB）的留着 120MB，四个都是这种也就几百 MB
KEEP_SESSIONS = 4
_states: "OrderedDict[tuple, SessionSpend]" = OrderedDict()
_lock = threading.Lock()


# ---------- 存盘 ----------
# 重启以后第一次打开大会话的花费视图要从头拆：291MB 的会话 7.7 秒，带 959 个子代理（共 6GB）的四十秒左右，大头是跑完了的子代理，
# 它们再也不会变。所以不再变化的记录文件（STABLE_SECS 没动过的，主要是跑完的子代理，跑完的主会话也算）拆好的状态存进数据目录，
# 一个文件一份；重启后、或者会话被挤出 KEEP_SESSIONS 以后再打开，先读存下的。读回来以后和内存里留着的一样处理：
# 文件没变直接用，变长了从存下的位置接着拆，换了 inode、变短、开头的字节对不上就从头重拆（FileReqs.update）；
# 文件没变时 update 不读文件，所以读盘时另外对一次开头的字节。拆的代码改了（版本见 DISK_CODE）就整份作废。
# 总量超过 DISK_CAP 扔最久没用过的（读到一份就碰一下它的修改时间）。用 pickle：数据目录只有查看器自己读写，目录 0700、文件 0600
STABLE_SECS = 1800
DISK_CAP = 1 << 30
_disk_dir = None
VERSION = None   # enable_disk 时按 DISK_CODE 算（codever.py）


def enable_disk(folder):
    """查看器启动时调：不再变化的文件拆好的状态存在 folder（数据目录下）。"""
    global _disk_dir, VERSION
    import codever
    VERSION = codever.version(*DISK_CODE)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    _disk_dir = str(folder)
    _drop_tmp(_disk_dir)


def _drop_tmp(folder: str):
    """上次写到一半就退出（被杀、崩溃）留下的临时文件：不算存盘，也不占总量，清掉。"""
    with os.scandir(folder) as it:
        for e in it:
            if e.name.endswith(".tmp"):
                try:
                    os.remove(e.path)
                except OSError:
                    pass


def _disk_name(path: Path, cwd: str) -> str:
    who = getattr(path, "run_as", None) or ""
    return os.path.join(_disk_dir, hashlib.sha1(f"{who}\0{path}\0{cwd}".encode()).hexdigest() + ".pkl")


def restore(path: Path, cwd: str, main: bool, st) -> FileReqs | None:
    """读这个文件存下的拆分状态；对不上或没存过是 None（照旧从头拆）。"""
    name = _disk_name(path, cwd)
    try:
        with open(name, "rb") as f:
            rec = pickle.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"花费存盘读不了，从头拆 {path}：{type(e).__name__}: {e}", flush=True)
        return None
    fr, why = rec.get("fr"), ""
    if rec.get("v") != VERSION:
        why = "代码改过了"
    elif (rec.get("path"), rec.get("run_as"), rec.get("cwd"), rec.get("main")) != (str(path), getattr(path, "run_as", None), cwd, main):
        why = "存的不是这个文件"
    elif fr.seen == (st.st_ino, st.st_size, st.st_mtime_ns):
        try:
            with path.open("rb") as f:
                if f.read(len(fr.head)) != fr.head:
                    why = "文件开头被改写过"
        except OSError:
            return None
    if why:
        print(f"花费存盘对不上（{why}），从头拆 {path}", flush=True)
        return None
    fr.path, fr.cwd, fr.saved = path, cwd, fr.seen
    try:
        os.utime(name)   # 最近用过：总量超了先扔别的
    except OSError:
        pass
    return fr


def _save(s: SessionSpend, todo: list[FileReqs]):
    """在后台把这几个文件拆好的状态写下去。拿着这个会话的锁：pickle 时别的线程不能同时在拆、在出图（会改这些状态）。"""
    try:
        with s.lock:
            for fr in todo:
                if fr.saved == fr.seen:
                    continue
                name = _disk_name(fr.path, fr.cwd)
                tmp = f"{name}.{os.getpid()}.{threading.get_ident()}.tmp"
                rec = {"v": VERSION, "path": str(fr.path), "run_as": getattr(fr.path, "run_as", None), "cwd": fr.cwd,
                       "main": fr.main, "fr": fr}
                with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                    pickle.dump(rec, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, name)
                fr.saved = fr.seen
        _prune()
    except Exception as e:
        print(f"花费存盘没写成：{type(e).__name__}: {e}", flush=True)


def _prune():
    """总量超过 DISK_CAP 就从最久没用过的扔起，扔到九成以下。"""
    with os.scandir(_disk_dir) as it:
        ents = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in it if e.name.endswith(".pkl")]
    total = sum(s for _, s, _ in ents)
    if total <= DISK_CAP:
        return
    for _, size, p in sorted(ents):
        if total <= DISK_CAP * 0.9:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass
    print(f"花费存盘超过 {DISK_CAP >> 20}MB，扔掉了最久没用过的，剩 {total >> 20}MB", flush=True)


# 存盘的版本按哪些代码算（codever.py）：存下的是 FileReqs 拆到一半的状态，顺着它（逐行拆、ctxmap 里把一行拆成几块的那些）
# 和存盘读盘找下去的函数和大写常量（连称呼 CALL）。出图（build）、悬停这些只用存下的状态，改了不作废
DISK_CODE = ([FileReqs, restore, _save], ["spendmap", "ctxmap", "calls", "peak"])


def compose(path: Path, own: dict[str, dict], cwd: str) -> dict:
    """不用缓存（内存里的和存盘的都不用），从头拆一遍出图（单独跑、对照测试用）。"""
    return SessionSpend(cwd, use_disk=False).refresh(Path(path), own)


def cached(path: Path, own: dict, cwd: str) -> dict:
    """按会话、运行身份和工作目录（拆的时候路径按它缩写）留着拆好的东西，几个人各看各的会话也不会互相挤掉。"""
    path = Path(path)
    ident = (str(path), getattr(path, "run_as", None), cwd)
    with _lock:
        s = _states.get(ident)
        if s is None:
            s = _states[ident] = SessionSpend(cwd)
        _states.move_to_end(ident)
        while len(_states) > KEEP_SESSIONS:
            _states.popitem(last=False)
    with s.lock:
        try:
            return s.refresh(path, own)
        except Exception:   # 拆到一半出了别的错，状态可能只拆了半块，整个扔掉，下次从头来（错照样报给调用的地方）
            with _lock:
                if _states.get(ident) is s:
                    del _states[ident]
            s.files, s.key = {}, None
            raise
