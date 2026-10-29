#!/usr/bin/env python3
"""回忆：在全部会话里抓正文气泡。只读，不改任何会话和数据。

    sessions.py grep [词] [-u] [-a] [-s] [--long-summary] [--from 时刻] [--to 时刻] [--days 天数] [--head N] [--tail N] [--per N]
                     [--who 名字] [--sess 编号开头,…] [--cwd 字] [--born-from/--born-to 时刻] [--last-from/--last-to 时刻]
                     [--min-bubbles/--max-bubbles N] [--min-user N] [--min-ctx/--max-ctx K] [--min-cost/--max-cost 元] [-n 80] [--cut 字数]

搜的是本实例的数据目录（环境变量 CC_VIEWER_DATA，不设就是代码目录）。

只有抓气泡这一个用法。正文气泡就是 user / assistant 说的话（用户在助手干活中途插的话也算）；系统注入的上下文、元消息、
压缩摘要、工具调用和工具返回都不算。几秒出结果，每次都扫全部会话。
  词：大小写敏感，中文一个字、英文两个字母起。词可以不给，这时给出符合下面条件的全部气泡，但至少要给一个条件。
  气泡分几类：-u 用户（人）说的话，-a 助手说的话，-s 会话的 100 字短摘要，--long-summary 会话的 1000 字长摘要。
  写了几类就只要这几类（几类之间是「或」，比如 -u -s 是用户的话加短摘要）；一类都不写是前三类，不含长摘要。
  气泡条件：--from、--to 按气泡说话的时刻筛（北京时间，写 10-25、10-25 20:00 或 2026-10-25 20:00，--to 含那一分钟或那一天）；
  --days 只要最近几天说的话。--head N、--tail N 是每个会话里符合上面条件的最前面、最后面 N 条（-u --tail 2 就是用户最后说的两句，
  两个都给是开头加末尾），--per N 是每个会话最多给 N 条、在这个会话里均匀抽（不让大会话占满名额）；这三个都不算摘要的名额。
  会话条件（整个会话符合才要它的气泡）：--who 会话主人（网页账号名）；--sess 编号开头，好几个用逗号隔开（--sess 66fa44ba,85823d3f）；
  --cwd 工作目录里含这几个字；--born-from、--born-to 新建（第一条气泡）的时刻；--last-from、--last-to 最后一条气泡的时刻；
  --min-bubbles、--max-bubbles 正文气泡的条数；--min-user 用户说的话至少几条（--min-user 3 能筛掉一问一答的测试会话）；
  --min-ctx、--max-ctx 现在的上下文（K token）；--min-cost、--max-cost 一共花了多少元（含子代理，按收钱单价算，没扣预定付掉的部分）。
  所有条件同时满足的才留下。判断不了的会话（原文件已删读不到工作目录、上下文和花费没在时限内算出来）不算符合，总数前面会报有几个。
  抓到气泡以后要看那个会话的上下文：grep --sess <编号开头>，只看某一段就再加 --from、--to 或者词。
  要看一批会话各在做什么、停在哪：grep -u -s --who … --last-from … --min-user 3 --head 1 --tail 2（第一句、最后两句加短摘要）。
  要找某个时候说过话的会话：grep --from … --to …，看最前面「气泡最多的会话」。
输出先是总数和气泡最多的几个会话，再按会话分组：每组开头一行写会话编号开头、会话主人、条数，和会话概况（新建、最后一条的时刻，
气泡条数，上下文，花费，工作目录）；组里一行一条：时间、谁说的、文字，按时间从旧到新；会话之间的先后每次随机，不代表什么。
  文字：给了词是命中处前后约 100 字，没给词是开头约 200 字，截掉的地方写明前面、后面还有多少字；--cut 改这个字数，--cut 0 给全文。
  超过 -n 条（默认 80）时在全部符合条件的气泡里均匀地随机抽，再搜一次换一批。
会话没有标题，也没有概述；自动起的标题和随手抓的一句话都不能拿来判断会话内容，要知道一个会话做了什么，就抓它的气泡来看。
要搜好几轮时可以派子代理去搜（默认同一个模型，用户明确要求才用 Haiku），但它只带回会话编号开头、时刻和用过的词，不带结论、不转述内容；
原文由你自己按这些编号和时刻用 --sess、--from、--to 再抓一遍来读，这一步不能省。
回忆是想起你自己做过的事：同一个用户的会话里同一个人设（没选人设卡就是默认人设）的 AI 都是你，讲它们说过、做过的事一律用第一人称，不说「那边的 AI」「他」。
原文件已被 Claude Code 按保留天数删掉的会话，索引里还留着正文的照样搜得到。
摘要是查看器在后台给每个会话写的（会话聊多了以后离开约 45 分钟才写，很多会话还没有），只算最新的一份，谁说的写「摘要」「长摘要」。
摘要的时刻是写摘要的时候，不一定是会话的最后一条：写完以后会话可能又往下聊了，要看停在哪还得看最后几条气泡（抬头里有最后一条的时刻）。"""
# 做法和查看器后端一样（isolation.roots 列出全部根目录，calls.snapshot 一趟清点，users.owner_of 认会话主人），
# 以当前用户、当前的 CC_VIEWER_DATA 在子进程里跑一遍再收回结果。
# 子进程只输出 ASCII 的 JSON 行：经伪终端按块转发时中文会被切坏，转义成 ASCII 就不会。
# grep 读数据目录里的正文索引 recall_index/（目录 0700、文件 0600，里面有密码之类的原话），见下面「正文索引」。
# 只有抓气泡：列会话看不出会话做了什么，按时间找会话、看一个会话的上下文都是按条件抓气泡，list、show、index 都去掉了。
# 普通用户也能回忆：会话里敲 ccrecall，经人设卡接口的套接字（personas.handle 的 op=recall）到查看器，
# 查看器以自己的身份跑本脚本（serve_request，隐藏参数 --single、--only）：只搜这一个实例、只搜会话主人是他的会话。
import argparse
import bisect
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import itertools
import json
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
INSTANCES = {"local": None}   # 实例 → 数据目录；None 是本实例（当前环境的 CC_VIEWER_DATA，不设就是代码目录）
START = time.time()   # 搜索按总时间收口：整个命令要在十秒内给结果
CHILD_LIMIT = 15      # 一份子进程最多等这么久：补索引 6.5 秒就收手，换身份再花两三秒；卡住了也不能让人干等
GONE = ("list", "show", "index")
SNAP = None           # _all_mains 那次清点


def _queued_text(d):
    """用户在助手干活中途插的话：只存成一条 attachment（queued_command、commandMode 为 prompt），
    不会再有对应的 user 记录，不认它就搜不到。
    不是这种记录返回 None；commandMode 为 task-notification 的是后台任务的系统通知，不算用户的话"""
    a = d.get("attachment") if d.get("type") == "attachment" else None
    if not isinstance(a, dict) or a.get("type") != "queued_command" or a.get("commandMode") != "prompt":
        return None
    t = a.get("prompt")
    if isinstance(t, list):
        t = "\n".join(b.get("text", "") for b in t if isinstance(b, dict) and b.get("type") == "text")
    return (t or "").strip() if isinstance(t, str) else ""


def _all_mains():
    """这个实例看得到的全部主会话：(根目录, 编号, 完整路径, 大小, 修改时间, 环境身份)"""
    sys.path.insert(0, HERE)
    import calls
    import isolation
    import users
    global SNAP
    roots = list(isolation.roots(internal=True))
    snap = SNAP = calls.snapshot(roots)   # 算花费时找子代理文件也用这次清点
    for root in roots:
        base = str(root).rstrip("/") + "/"
        mains = snap.mains.get(str(root))
        if mains is None:   # 这个根目录没清点成，照旧一个个列
            mains = {}
            for d in root.iterdir():
                if d.is_dir():
                    for f in d.glob("*.jsonl"):
                        mains[f.stem] = f"{d.name}/{f.name}"
        for sid, rel in mains.items():
            if (users.owners.get(sid) or {}).get("workflow"):
                continue   # 脚本派发不算用户原话，记录仍在账本和工作流查询里
            st = snap.stats.get(base + rel)
            if st is None:
                s = (root / rel).stat()
                st = [s.st_size, s.st_mtime]
            yield root, sid, root / rel, st[0], st[1], getattr(root, "run_as", None) or "共用"


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=True), flush=True)


# ---- 正文索引（grep 用）----
# 一个会话平均几 MB，正文气泡只占百分之一二，其余是工具返回和图片；每次从头解析全部记录要近一分钟（几千个会话时量的）。
# 所以每个会话只把正文气泡抽出来存一份，记下读到的字节位置，以后只读追加的部分。
INDEX_VER = 2   # 2：收进用户中途插的话


def _index_dir():
    import instance
    d = instance.DATA / "recall_index"
    d.mkdir(exist_ok=True)
    os.chmod(d, 0o700)   # 正文里有密码之类的原话，数据目录别人可能读得到，索引只给本用户
    return d


def _body_file(d, p):
    return d / (hashlib.sha1(str(p).encode()).hexdigest()[:20] + ".jsonl")


def _load_state(d):
    try:
        s = json.loads((d / "state.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return s.get("files", {}) if s.get("v") == INDEX_VER else {}   # 版本变了就全部重建


def _save_state(d, st):
    tmp = d / "state.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"v": INDEX_VER, "files": st}, f)
    os.replace(tmp, d / "state.json")


def _bubbles(raw):
    """一行会话记录里的正文气泡 [(时间, u/a, 文字)]：只要 user / assistant 的 text 块，
    元消息、压缩摘要、侧链、只给界面看的、工具调用和工具返回、以 < 开头的系统插话都不算"""
    # 先按字节筛掉工具返回和别的类型，不解析：时间几乎都花在解析这些大行上。
    # 正文里写到这几个词时引号会被转义成 \"，不会误筛
    if b'"type":"tool_result"' in raw or (b'"type":"user"' not in raw and b'"type":"assistant"' not in raw
                                          and b'"queued_command"' not in raw):
        return []
    try:
        d = json.loads(raw)
    except ValueError:
        return []
    said = _queued_text(d)
    if said is not None:
        return [(d.get("timestamp") or "", "u", said)] if said else []
    kind = d.get("type")
    if kind not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta") \
            or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
        return []
    c = d.get("message", {}).get("content")
    if isinstance(c, str):
        c = [{"type": "text", "text": c}]
    if not isinstance(c, list):
        return []
    out = []
    for b in c:
        if isinstance(b, dict) and b.get("type") == "text":
            t = (b.get("text") or "").strip()
            if t and not (kind == "user" and t.startswith("<")):
                out.append((d.get("timestamp") or "", "a" if kind == "assistant" else "u", t))
    return out


def _index_one(d, p, sid, rec, st, deadline):
    """把一个会话新追加的部分补进索引。半截的最后一行留到下次；文件换过（inode 变了、变短了）就重建这个会话"""
    with p.open("rb") as f:
        s = os.fstat(f.fileno())
        if rec and rec["ino"] == s.st_ino and s.st_size >= rec["off"]:
            off, blen = rec["off"], rec["blen"]
        else:
            off, blen = 0, 0
        f.seek(off)
        out, n = [], 0
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            off += len(raw)
            for ts, w, t in _bubbles(raw):
                out.append(json.dumps({"t": ts, "w": w, "x": t}, ensure_ascii=False).encode() + b"\n")
            n += 1
            if deadline and n % 2000 == 0 and time.time() > deadline:   # 大会话第一次建索引可能很久，读到哪记到哪
                break
    data = b"".join(out)
    fd = os.open(_body_file(d, p), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as g:
        g.truncate(blen)   # 上次写了正文但没来得及记进状态的那截丢掉，免得重复
        g.seek(blen)
        g.write(data)
    st[str(p)] = {"sid": sid, "ino": s.st_ino, "off": off, "blen": blen + len(data)}


def _update_index(inst, files, deadline):
    """补索引，新改过的会话先补。返回 (状态, 还没补完的会话数)；别的搜索正在补时不等，返回 (状态, None) 只搜现有的"""
    import fcntl
    d = _index_dir()
    lock = open(d / ".lock", "w")
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if deadline and time.time() > deadline:
                return _load_state(d), None
            time.sleep(0.1)
    try:
        st = _load_state(d)
        todo = sorted((f for f in files if (st.get(str(f[2])) or {}).get("off") != f[3]), key=lambda f: -f[4])
        pending, saved = 0, time.time()
        for i, (root, sid, p, size, mtime, env) in enumerate(todo):
            if deadline and time.time() > deadline:
                pending = len(todo) - i
                break
            try:
                _index_one(d, p, sid, st.get(str(p)), st, deadline)
            except OSError as e:
                _emit({"err": f"读 {p} 没成：{e}"})
            if time.time() - saved > 3:   # 中途被掐掉也不白干
                _save_state(d, st)
                saved = time.time()
        if todo:
            _save_state(d, st)
        return st, pending
    finally:
        lock.close()


# 索引里每行都是 json.dumps({"t": 时间, "w": u/a, "x": 文字})，时间和谁说的在开头，筛的时候不用解析整行
HEAD = re.compile(rb'^\{"t": "([^"]*)", "w": "([ua])"')


def _when(s, end):
    """北京时间（本机时区）的 10-25、10-25 20:00、2026-10-25、2026-10-25 20:00 换成 UTC 时刻；
    end 为真时换成那一分钟（只给日期就是那一天）结束的时刻，当不含的上界用。看不懂返回 None"""
    s = " ".join(s.split())
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M", "%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    if not fmt.startswith("%Y"):
        now = datetime.now()
        dt = dt.replace(year=now.year)
        if dt > now + timedelta(days=1):   # 年初查去年年底的，算去年
            dt = dt.replace(year=now.year - 1)
    if end:
        dt += timedelta(minutes=1) if "%H" in fmt else timedelta(days=1)
    return dt.astimezone(timezone.utc)


def _span(a):
    """--from、--to、--days 合成 (下界, 上界) 两个 UTC 时刻，下界含、上界不含，没给的是 None；写错了直接退出"""
    lo = hi = None
    for flag, s, end in (("--from", a.since, False), ("--to", a.until, True)):
        if not s:
            continue
        t = _when(s, end)
        if t is None:
            sys.exit(f"{flag} {s} 看不懂：写成 10-25、10-25 20:00 或 2026-10-25 20:00（北京时间）")
        lo, hi = (lo, t) if end else (t, hi)
    if a.days:
        t = datetime.now(timezone.utc) - timedelta(days=a.days)
        lo = max(lo, t) if lo else t
    return lo, hi


def _iso(t):
    # 索引里的时间是 2026-10-25T12:00:17.046Z 这样的 UTC 串，截到秒以后按字符串比大小就是按时间比
    return t.strftime("%Y-%m-%dT%H:%M:%S") if t else ""


def _summary_rows(kinds):
    """这个实例数据目录里每个会话最新的摘要（summaries.py 写的 session_summaries.json），排成和索引一样的行：(会话编号, 行)。
    kinds 里有 s 给短摘要、有 l 给长摘要。时刻是写摘要的时候；读不出来要说出来，不当成没有。"""
    import instance
    try:
        d = json.loads((instance.DATA / "session_summaries.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        _emit({"err": f"会话摘要的记录读不出来：{e}"})
        return []
    out = []
    for sid, r in (d.get("sessions") or {}).items():
        s = r.get("summary") or {}
        if not s.get("at"):
            continue
        t = datetime.fromtimestamp(s["at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for w, key in (("s", "short"), ("l", "long")):
            if w in kinds and s.get(key):
                out.append((sid, json.dumps({"t": t, "w": w, "x": s[key]}, ensure_ascii=False).encode()))
    return out


# ---- 会话的概况（会话条件和每组抬头用，2026-10-30 主人要的）----
# 新建、最后一条、气泡数从正文索引现数（索引就在手里，数一遍很快）；工作目录、上下文、花费要读原文件或调用表，
# 记在 recall_index/meta.json 里：工作目录按会话编号记一次就够（不会变），上下文和花费按会话和子代理文件的大小记，大小变了才重算。
TS_W = re.compile(rb'^\{"t": "([^"]*)", "w": "([ua])"', re.M)
CWD = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')


def _load_meta(d):
    try:
        return json.loads((d / "meta.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_meta(d, m):
    tmp = d / f"meta.json.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(m, f)
    os.replace(tmp, d / "meta.json")   # 两个搜索同时写，后写的为准；丢的只是缓存，下次重算


def _counts(data):
    """正文索引里一个会话的 (第一条气泡的时刻, 最后一条的时刻, 气泡总数, 其中用户说的条数)；摘要不算"""
    first = last = ""
    n = nu = 0
    for m in TS_W.finditer(data):
        t = m.group(1).decode()
        first = first or t
        last = t
        n += 1
        nu += m.group(2) == b"u"
    return first, last, n, nu


def _cwd(p, sid, meta):
    """会话的工作目录：会话记录前面几行里的 cwd。原文件已删、读不到是 None（显示成不知道，不当成空）"""
    m = meta.setdefault(sid, {})
    if "cwd" in m:
        return m["cwd"]
    try:
        with p.open("rb") as f:
            hit = CWD.search(f.read(256 << 10))
    except (OSError, AttributeError):
        return None
    m["cwd"] = json.loads(b'"' + hit.group(1) + b'"') if hit else ""
    return m["cwd"]


_calls_ready = False


def _money(p, sid, meta, snap):
    """(上下文 token 数, 花费元)：上下文是主会话最后一次调用的输入、缓存写入、缓存命中之和；
    花费是这个会话名下的全部调用（含子代理，users.own_calls，和标签页上的总消耗同一份）按收钱单价算的，没扣预定付掉的部分。
    调用表先读查看器存下的（数据目录 calls_cache，只读不写），对不上就从会话记录读。算不出来是 (None, None)"""
    global _calls_ready
    import calls
    if p is None or isinstance(p, str):
        return None, None
    m = meta.setdefault(sid, {})
    try:
        sizes = [s for _, s in calls.file_sizes(p, snap)]
        if m.get("sizes") == sizes and "yuan" in m:
            return m["ctx"], m["yuan"]
        if not _calls_ready:   # 只借用查看器存下的调用表，不开存盘线程；版本算法和 calls.enable_disk 一样，对不上就当没存
            import instance
            import peak
            with open(calls.__file__, "rb") as a, open(peak.__file__, "rb") as b:
                calls.VERSION = calls._digest(a.read() + b.read())
            calls._disk_dir = str(instance.DATA / "calls_cache")
            _calls_ready = True
        import rates
        import reserve
        import users
        table = rates.load(price=True)
        yuan = sum(rates.cost(table, c) for c in users.own_calls(p).values()) * reserve.FEN / 100
        mine = calls.file_calls(p)
        lastc = max(mine.values(), key=lambda c: c["ts"] or "", default=None)
        ctx = sum(lastc["use"].get(k, 0) for k in ("in", "cw", "cr")) if lastc else 0
    except Exception as e:   # 单价表、调用表哪里出错都要说出来，不当成零
        _emit({"err": f"会话 {sid[:8]} 的上下文和花费没算成：{type(e).__name__}: {e}"})
        return None, None
    m.update(sizes=sizes, ctx=ctx, yuan=round(yuan, 4))
    return ctx, m["yuan"]


def _snippet(t, word, width=100):
    i = t.find(word)
    s = max(0, i - 30)
    e = min(len(t), max(s + width, i + len(word)))
    return _cut_mark(t, s, e)


def _cut_mark(t, s, e):
    """t[s:e]，前后被截掉的地方写明截了多少字：和抽样报总数一样，看的人知道自己只看了一部分，要不要 --cut 0 看全文"""
    return (f"（前面还有 {s} 字）…" if s else "") + t[s:e] + (f"…（后面还有 {len(t) - e} 字）" if e < len(t) else "")


def _text(x, word, cut):
    """一条气泡要显示的文字：cut 为 0 给全文；给了词是命中处前后约 cut 字，没给词是开头 cut 字。
    先把空白压成一个空格再截，报的剩余字数和看到的字数是同一种数法"""
    t = " ".join(x.split())
    if cut == 0:
        return t
    if word:
        return _snippet(t, word, cut)
    return _cut_mark(t, 0, min(cut, len(t)))


def here_grep(inst, a):
    """在这个实例的正文索引里抓气泡，先把新增的部分补进索引。发回符合条件的总数、每个会话的条数，
    和从中随机抽的至多 -n 条；抽出来的保持随机顺序发回，父进程才能按各份的条数均匀合并"""
    import users
    word = a.word
    files = list(_all_mains())
    if a.only:   # ccrecall 来的普通用户：只看主人是他的会话，补索引也只补他的，别人的会话连条数、路径都不出现
        files = [f for f in files if users.owner_of(f[1]) == a.only]
    st, pending = _update_index(inst, files, START + 6.5)
    d = _index_dir()
    # 原文件已经不在的会话（Claude Code 按 cleanupPeriodDays 把超过 30 天的会话记录删了，2026-10-29 发现）：
    # 索引里的正文还在，照样搜。按会话编号认，同一个会话换了位置的不重复算；修改时间当成无穷大，--from 不会把它筛掉
    have = {f[1] for f in files}
    for path, rec in st.items():
        sid = rec.get("sid")
        if sid and sid not in have and not (users.owners.get(sid) or {}).get("workflow") \
                and not (a.only and users.owner_of(sid) != a.only):
            files.append((None, sid, path, 0, float("inf"), None))
            have.add(sid)
    key = json.dumps(word, ensure_ascii=False)[1:-1].encode() if word else b""   # 索引里文字按 JSON 存，词也照样转义了再按字节找
    lo, hi = _span(a)
    lo_s, hi_s = _iso(lo), _iso(hi)
    want = _kinds(a)
    sess = _prefixes(a.sess)
    cond = _conds(a)
    meta = _load_meta(d)
    n_meta = len(json.dumps(meta))
    info = {}   # 会话编号 → 概况（新建、最后、气泡数、工作目录、上下文、花费），只给最后抽到的会话发回去
    where = {}   # 会话编号 → 原文件（原文件已删的是索引里记的路径字符串）
    found, per = [], {}

    def session_ok(sid, p, data):
        """会话条件：新建、最后一条、气泡数、用户说的条数、工作目录。上下文和花费贵，等有了命中再看（money_ok）"""
        first, last, n, nu = _counts(data)
        info[sid] = {"born": first, "last": last, "n": n, "nu": nu}
        if (cond["born_lo"] and first < cond["born_lo"]) or (cond["born_hi"] and first >= cond["born_hi"]) \
                or (cond["last_lo"] and last < cond["last_lo"]) or (cond["last_hi"] and last >= cond["last_hi"]) \
                or (a.min_bubbles and n < a.min_bubbles) or (a.max_bubbles is not None and n > a.max_bubbles) \
                or (a.min_user and nu < a.min_user):
            return False
        if a.cwd:
            c = _cwd(p, sid, meta)
            if c is None or a.cwd not in c:   # 读不到工作目录的会话判断不了，不算符合（总数前面会报有几个）
                skipped["cwd"] += c is None
                return False
        return True

    skipped = {"cwd": 0, "money": 0}
    for root, sid, p, size, mtime, env in files:
        rec = st.get(str(p))
        if not rec or not rec["blen"] or (sess and not sid.startswith(sess)):
            continue
        if (lo or cond["last_lo"]) and mtime < max(x.timestamp() for x in (lo, cond["last_lo_t"]) if x):
            continue   # 起点之前就没再动过的会话，里面不会有更晚的气泡
        if a.who and (users.owner_of(sid) or "") != a.who:
            continue
        with open(_body_file(d, p), "rb") as f:
            data = f.read(rec["blen"])   # 只认状态里记着的长度，别的搜索正在写的那截不算
        where[sid] = p
        ok = session_ok(sid, p, data)
        info[sid]["bad"] = not ok   # 摘要那一段要用，不再读一遍正文
        if not ok:
            continue
        if key and key not in data:
            continue
        sel = []
        for raw in data.splitlines():
            if key and key not in raw:
                continue
            m = HEAD.match(raw)
            if not m:
                continue
            t, w = m.group(1).decode(), m.group(2).decode()
            if w not in want or (lo_s and t < lo_s) or (hi_s and t >= hi_s):
                continue
            if word and word not in json.loads(raw)["x"]:   # 按字节找到的也可能落在时间或转义里，对着原文再核一遍
                continue
            sel.append(raw)
        # --head、--tail 在筛完类别、时间和词以后取（-u --tail 2 就是用户最后说的两句）；两个都给是开头几条加末尾几条
        if a.head or a.tail:
            keep = set(range(min(a.head or 0, len(sel)))) | set(range(max(0, len(sel) - (a.tail or 0)), len(sel)))
            sel = [sel[i] for i in sorted(keep)]
        if a.per and len(sel) > a.per:   # 每个会话最多给几条：在这个会话的气泡里均匀抽，不让大会话占满名额
            sel = [sel[i] for i in sorted(random.sample(range(len(sel)), a.per))]
        for raw in sel:
            found.append((sid, raw))
            per[sid] = per.get(sid, 0) + 1
    # 每个会话最新的摘要也算气泡（2026-10-29 主人说的）；要哪类由 -s、--long-summary 定，和 -u、-a 是「或」（2026-10-30 主人说的）
    if want & {"s", "l"}:
        for sid, raw in _summary_rows(want):
            e = json.loads(raw)
            if sid not in where or (lo_s and e["t"] < lo_s) or (hi_s and e["t"] >= hi_s) or (word and word not in e["x"]):
                continue   # 不在 where 里的是没过编号、主人、时间这些前置条件的
            if info[sid]["bad"]:
                continue
            found.append((sid, raw))
            per[sid] = per.get(sid, 0) + 1
    # 上下文、花费：只给有命中的会话算，算到时限为止；没算出来的会话判断不了，不算符合，总数前面报有几个
    if cond["money"]:
        keep = set()
        for sid in per:
            if time.time() > START + 8.5:
                skipped["money"] += 1
                continue
            ctx, yuan = _money(where.get(sid), sid, meta, SNAP)
            if ctx is None:
                skipped["money"] += 1
                continue
            if (a.min_ctx is not None and ctx < a.min_ctx * 1000) or (a.max_ctx is not None and ctx > a.max_ctx * 1000) \
                    or (a.min_cost is not None and yuan < a.min_cost) or (a.max_cost is not None and yuan > a.max_cost):
                continue
            keep.add(sid)
        found = [x for x in found if x[0] in keep]
        per = {s: c for s, c in per.items() if s in keep}
    _emit({"inst": inst, "count": len(found), "per": per, "pending": pending, "skipped": skipped})
    cut = a.cut if a.cut is not None else (100 if word else 200)
    picked = random.sample(found, min(len(found), a.n or 80))
    for sid, raw in picked:
        e = json.loads(raw)
        _emit({"inst": inst, "ts": e["t"], "sid": sid, "owner": users.owner_of(sid),
               "who": {"a": "助手", "u": "用户", "s": "摘要", "l": "长摘要"}[e["w"]], "text": _text(e["x"], word, cut)})
    # 抽到的会话各发一行概况，父进程写进每组的抬头。上下文、花费算到时限为止，没算出来的抬头上写明
    for sid in {s for s, _ in picked}:
        m = dict(info.get(sid) or {})
        m.pop("bad", None)
        m["cwd"] = _cwd(where.get(sid), sid, meta)
        if time.time() < START + 9:
            m["ctx"], m["yuan"] = _money(where.get(sid), sid, meta, SNAP)
        m["gone"] = isinstance(where.get(sid), str)
        _emit({"inst": inst, "meta": sid, **m})
    if len(json.dumps(meta)) != n_meta:
        try:
            _save_meta(d, meta)
        except OSError as e:
            _emit({"err": f"会话概况的缓存没写成（不影响这次结果）：{e}"})


def _conds(a):
    """会话条件里的时刻换成和索引一样的 UTC 串；money 为真时要算上下文或花费"""
    out = {"money": any(x is not None for x in (a.min_ctx, a.max_ctx, a.min_cost, a.max_cost))}
    for name, s, end in (("born_lo", a.born_from, False), ("born_hi", a.born_to, True),
                         ("last_lo", a.last_from, False), ("last_hi", a.last_to, True)):
        t = _when(s, end) if s else None
        out[name] = _iso(t)
        out[name + "_t"] = t
    return out


def _kinds(a):
    """要哪几类气泡：u 用户、a 助手、s 短摘要、l 长摘要。写了几类就是这几类的「或」，一类都没写是 u、a、s（长摘要太长，要明说才给）"""
    got = {w for w, on in (("u", a.user), ("a", a.assistant), ("s", a.summary), ("l", a.long_summary)) if on}
    return got or {"u", "a", "s"}


def _prefixes(s):
    """--sess 的值拆成编号开头的元组（逗号、中文逗号、空白都算分隔），没给是空元组；str.startswith 直接认元组"""
    return tuple(x for x in re.split(r"[,，\s]+", s or "") if x)


def _local(ts):
    try:
        return datetime.fromisoformat(ts[:19] + "+00:00").astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return ts[:16]


def run_child(inst, argv, local=False):
    """在那个实例的身份和数据目录下跑本脚本，收回 JSON 行；超时就当这份没搜到。
    local 为真（--single，查看器自己调的）是就以当前身份、当前数据目录搜，环境原样传下去"""
    me = [PY, os.path.abspath(__file__), *argv, "--here", inst]
    try:
        if not local and INSTANCES[inst] is not None:   # 只搜本实例：照当前环境的 CC_VIEWER_DATA
            sys.exit(f"不支持搜别的实例：{inst}")
        r = subprocess.run(me, capture_output=True, text=True, timeout=CHILD_LIMIT, env=dict(os.environ))
    except subprocess.TimeoutExpired:
        print(f"！有一部分会话 {CHILD_LIMIT} 秒还没搜完，这次没算进去", file=sys.stderr)
        return []
    out, err = r.stdout, r.stderr
    got = []
    for line in out.replace("\r", "").splitlines():
        if line.startswith("{"):
            try:
                got.append(json.loads(line))
            except ValueError:
                pass
    if r.returncode != 0:
        tail = (err or out).replace("\r", "").strip()[-1500:]
        print(f"！读一部分会话时出错了（退出码 {r.returncode}）：\n{tail}", file=sys.stderr)
    return got


def _q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


# ---- 给查看器调的（ccrecall）----
HIDDEN = ("--here", "--single", "--only")


class Refused(Exception):
    """ccrecall 那边要看到的错（参数不对、超时、脚本出错）"""


def serve_request(argv, only):
    """会话里 ccrecall 的一次搜索（personas.handle 的 op=recall 在线程里调）：以查看器自己的身份、数据目录只搜这个实例，
    only 是网页账号名时只搜主人是他的会话（普通用户），None 是不限（查看器账号）。返回要打到标准输出上的字，不成立就抛 Refused。
    用户给的参数原样交给本脚本，隐藏参数放在最后（argparse 同一个参数以后写的为准）；看着像隐藏参数（含缩写）的先拒掉"""
    argv = [str(x) for x in argv]
    for x in argv:
        k = x.split("=", 1)[0]
        if len(k) >= 3 and k.startswith("--") and any(h.startswith(k) for h in HIDDEN):
            raise Refused(f"不认识的参数 {x}（ccrecall --help 看用法）")
    cmd = [PY, os.path.abspath(__file__), *argv, "--single", "here"] + (["--only", only] if only else [])
    limit = CHILD_LIMIT + 8
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=limit, env={**os.environ, "RECALL_PROG": "ccrecall"})
    except subprocess.TimeoutExpired:
        raise Refused(f"搜了 {limit} 秒还没出结果，过一会儿再试") from None
    if r.returncode != 0:
        raise Refused((r.stderr or r.stdout).strip() or f"出错了（退出码 {r.returncode}）")
    return r.stderr + r.stdout   # 标准错误里是「有一部分没搜到」这类提醒，放在最前面


def main():
    if len(sys.argv) > 1 and sys.argv[1] in GONE:
        sys.exit(f"{sys.argv[1]} 已经去掉了，回忆只剩抓气泡：看一个会话用 grep --sess <编号开头>，"
                 "按时间找用 grep --from … --to …，用法见 --help")
    # 用法说明就是文件开头那段：--help 原先只有一串没解释的参数名，换了模型的 AI 看了也不知道怎么用
    # ccrecall 经查看器调时 RECALL_PROG 是 ccrecall，参数写错时的提示照它的名字说
    ap = argparse.ArgumentParser(prog=os.environ.get("RECALL_PROG") or None, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["grep"], help="只有 grep：抓正文气泡")
    ap.add_argument("word", nargs="?", default="", help="要找的词，可以不给")
    ap.add_argument("-n", type=int, default=None, help="最多给几条（默认 80），多了在全部符合条件的里均匀随机抽")
    ap.add_argument("--who", default="", help="只要这个网页账号名的会话")
    ap.add_argument("--sess", default="", help="只要编号以它开头的会话，好几个用逗号隔开")
    ap.add_argument("--from", dest="since", default="", help="只要这个时刻以后说的（北京时间，如 10-25 20:00）")
    ap.add_argument("--to", dest="until", default="", help="只要这个时刻以前说的（含那一分钟或那一天）")
    ap.add_argument("--days", type=float, default=None, help="只要最近几天说的")
    ap.add_argument("--head", type=int, default=None, help="每个会话只要符合条件的最前面 N 条（摘要不占名额）")
    ap.add_argument("--tail", type=int, default=None, help="每个会话只要符合条件的最后 N 条（摘要不占名额）")
    ap.add_argument("--per", type=int, default=None, help="每个会话最多给几条，多了在这个会话里均匀抽（摘要不占名额）")
    ap.add_argument("--cut", type=int, default=None, help="每条给多少字：给了词默认 100，没给词默认 200，0 是全文")
    # 会话条件：整个会话符合才要它的气泡（2026-10-30 主人要的）
    ap.add_argument("--cwd", default="", help="工作目录里含这几个字的会话")
    ap.add_argument("--born-from", default="", help="新建（第一条气泡）在这个时刻以后的会话")
    ap.add_argument("--born-to", default="", help="新建在这个时刻以前的会话（含那一分钟或那一天）")
    ap.add_argument("--last-from", default="", help="最后一条气泡在这个时刻以后的会话")
    ap.add_argument("--last-to", default="", help="最后一条气泡在这个时刻以前的会话（含那一分钟或那一天）")
    ap.add_argument("--min-bubbles", type=int, default=None, help="正文气泡（用户和助手说的话）至少这么多条的会话")
    ap.add_argument("--max-bubbles", type=int, default=None, help="正文气泡至多这么多条的会话")
    ap.add_argument("--min-user", type=int, default=None, help="用户说的话至少这么多条的会话（筛掉测试会话）")
    ap.add_argument("--min-ctx", type=float, default=None, help="现在的上下文至少这么多 K token 的会话")
    ap.add_argument("--max-ctx", type=float, default=None, help="现在的上下文至多这么多 K token 的会话")
    ap.add_argument("--min-cost", type=float, default=None, help="一共花了至少这么多元的会话")
    ap.add_argument("--max-cost", type=float, default=None, help="一共花了至多这么多元的会话")
    # 气泡的类别：写了几类就要这几类的「或」，一类都不写是用户、助手和短摘要（2026-10-30 主人说的，原先 -u、-a 互斥、要了就没有摘要）
    ap.add_argument("--user", "-u", action="store_true", help="要用户（人）说的话")
    ap.add_argument("--assistant", "-a", action="store_true", help="要助手说的话")
    ap.add_argument("--summary", "-s", action="store_true", help="要会话的 100 字短摘要")
    ap.add_argument("--long-summary", action="store_true", help="要会话的 1000 字长摘要")
    ap.add_argument("--here", default="", help=argparse.SUPPRESS)
    ap.add_argument("--single", default="", help=argparse.SUPPRESS)   # 只搜当前身份的这个实例（serve_request）
    ap.add_argument("--only", default="", help=argparse.SUPPRESS)     # 只搜主人是这个网页账号的会话（serve_request）
    a = ap.parse_args()
    if a.here:
        here_grep(a.here, a)
        return
    if a.word and a.word.isascii() and len(a.word) < 2:
        sys.exit("英文至少两个字母，中文至少一个字")
    session_conds = (a.cwd, a.born_from, a.born_to, a.last_from, a.last_to, a.min_bubbles, a.max_bubbles, a.min_user,
                     a.min_ctx, a.max_ctx, a.min_cost, a.max_cost)
    if not (a.word or a.who or _prefixes(a.sess) or a.since or a.until or a.days or a.user or a.assistant
            or a.summary or a.long_summary or any(x not in (None, "") for x in session_conds)):
        sys.exit("要给一个词，或者至少一个条件（气泡的类别、时刻，或者会话条件），用法见 --help")
    for flag, v in (("--head", a.head), ("--tail", a.tail), ("--per", a.per)):
        if v is not None and v < 1:
            sys.exit(f"{flag} 要给正整数")
    _span(a)   # 时刻写错了在这里就报，不去开子进程
    for flag, s in (("--born-from", a.born_from), ("--born-to", a.born_to), ("--last-from", a.last_from), ("--last-to", a.last_to)):
        if s and _when(s, False) is None:
            sys.exit(f"{flag} {s} 看不懂：写成 10-25、10-25 20:00 或 2026-10-25 20:00（北京时间）")
    insts = [a.single] if a.single else list(INSTANCES)
    with ThreadPoolExecutor(max_workers=len(insts)) as ex:
        parts = list(ex.map(lambda inst: run_child(inst, sys.argv[1:], local=bool(a.single)), insts))
    limit = a.n or 80
    heads, shown, per, failed, busy, pending = [], [], {}, False, False, 0
    metas, skipped = {}, {"cwd": 0, "money": 0}
    for got in parts:
        rows, h = [], None
        for e in got:
            if "err" in e:
                print("！" + e["err"], file=sys.stderr)
            elif "count" in e:
                h = e
                for k, v in (e.get("skipped") or {}).items():
                    skipped[k] = skipped.get(k, 0) + v
            elif "meta" in e:
                metas[e["meta"]] = e
            else:
                rows.append(e)
        if h is None:
            failed = True
            continue
        heads.append(h)
        shown.append(rows)
        per.update(h["per"])
        busy |= h["pending"] is None
        pending += h["pending"] or 0
    total = sum(h["count"] for h in heads)
    # 在全部符合条件的气泡里均匀地抽：先在总数里不放回地抽 limit 个位置，数出每份落了几个，
    # 再从那份自己随机抽好的样本里按顺序取这么多（样本本身是均匀随机排的，取前几个仍然均匀）
    take = [0] * len(heads)
    ends = list(itertools.accumulate(h["count"] for h in heads))
    for i in random.sample(range(total), min(limit, total)):
        take[bisect.bisect_right(ends, i)] += 1
    picked = [e for k, rows in zip(take, shown) for e in rows[:k]]
    # 按会话分组、组里照旧按时间从旧到新；会话之间没有该排的先后，每次随机排（2026-10-29 主人说的）
    groups = {}
    for e in sorted(picked, key=lambda e: e["ts"]):
        groups.setdefault(e["sid"], []).append(e)
    order = list(groups)
    random.shuffle(order)
    # 总数、气泡最多的会话和没搜全的提示都放在最前面：调用的人常在后面接 head，放末尾会被截掉
    print((f"共命中 {total} 条" if a.word else f"符合条件的气泡共 {total} 条") + f"，在 {len(per)} 个会话里" +
          (f"；随机抽 {len(picked)} 条，再搜一次换一批" if total > len(picked) else "") + "。")
    if len(per) > 1:
        top = sorted(per.items(), key=lambda kv: -kv[1])[:10]
        print(("命中最多的会话：" if a.word else "气泡最多的会话：") + "  ".join(f"{s[:8]}（{c}）" for s, c in top))
    if failed:
        print("！有一部分会话这次没搜到：读取出错或超时了，原因见上面的报错")
    if busy:
        print("（另有一个搜索正在补索引，这次只搜了已经进索引的部分）")
    if pending:
        print(f"（还有 {pending} 个会话的新内容没进索引，这次没搜到它们；再搜一次会接着补）")
    if skipped.get("cwd"):
        print(f"（有 {skipped['cwd']} 个会话读不到工作目录（原文件已删），判断不了，这次没算进去）")
    if skipped.get("money"):
        print(f"（有 {skipped['money']} 个会话的上下文和花费这次没算出来，判断不了，没算进去；再搜一次会接着算）")
    if picked:
        print("按会话分组，会话的先后随机，组里按时间从旧到新：")
    for sid in order:
        items = groups[sid]
        n = per.get(sid, len(items))
        print(f"\n== 会话 {sid[:8]}（{items[0]['owner'] or '无主'}）："
              + (f"{n} 条里抽到 {len(items)} 条" if n > len(items) else f"{len(items)} 条") + _brief(metas.get(sid)) + " ==")
        for e in items:
            print(f"{_local(e['ts'])} {e['who']}：{e['text']}")


def _brief(m):
    """每组抬头后面的会话概况：新建、最后一条、气泡数、上下文、花费、工作目录。算不出来的写明，不省掉"""
    if not m:
        return ""
    out = []
    if m.get("born"):
        out.append(f"新建 {_local(m['born'])}，最后 {_local(m['last'])}")
    if "n" in m:
        out.append(f"气泡 {m['n']} 条（用户 {m['nu']}）")
    if m.get("ctx") is not None:
        out.append(f"上下文 {round(m['ctx'] / 1000)}K，花费 {m['yuan']:.2f} 元")
    elif m.get("gone"):
        out.append("原文件已删，上下文和花费算不了")
    else:
        out.append("上下文和花费这次没算出来")
    cwd = m.get("cwd")
    out.append(f"工作目录 {cwd}" if cwd else "工作目录不知道" if cwd is None else "记录里没有工作目录")
    return " · " + " · ".join(out)


if __name__ == "__main__":
    main()
