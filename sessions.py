#!/usr/bin/env python3
"""回忆：在全部会话里抓正文气泡。只读，不改任何会话和数据。

    sessions.py grep [词] [-u | -a] [--who 名字] [--sess 编号开头] [--from 时刻] [--to 时刻] [--days 天数] [-n 80] [--cut 字数]

搜的是本实例的数据目录（环境变量 CC_VIEWER_DATA，不设就是代码目录）。

只有抓气泡这一个用法。正文气泡就是 user / assistant 说的话（用户在助手干活中途插的话也算）；系统注入的上下文、元消息、
压缩摘要、工具调用和工具返回都不算。几秒出结果，每次都扫全部会话。
  词：大小写敏感，中文一个字、英文两个字母起。词可以不给，这时给出符合下面条件的全部气泡，但至少要给一个条件。
  -u 只要用户（人）说的话，-a 只要助手说的；--who 按会话主人（网页账号名）筛；--sess 只要编号以它开头的会话；
  --from、--to 按气泡说话的时刻筛（北京时间，写 10-25、10-25 20:00 或 2026-10-25 20:00，--to 含那一分钟或那一天）；
  --days 只要最近几天说的话。
  抓到气泡以后要看那个会话的上下文：grep --sess <编号开头>，只看某一段就再加 --from、--to 或者词。
  要找某个时候说过话的会话：grep --from … --to …，看最前面「气泡最多的会话」。
输出先是总数和气泡最多的几个会话，再一行一条：时间、会话编号开头、会话主人、谁说的、文字，按时间从旧到新。
  文字：给了词是命中处前后约 100 字，没给词是开头约 200 字；--cut 改这个字数，--cut 0 给全文。
  超过 -n 条（默认 80）时在全部符合条件的气泡里均匀地随机抽，再搜一次换一批。
会话没有标题，也没有概述；自动起的标题和随手抓的一句话都不能拿来判断会话内容，要知道一个会话做了什么，就抓它的气泡来看。"""
# 做法和查看器后端一样（isolation.roots 列出全部根目录，calls.snapshot 一趟清点，users.owner_of 认会话主人），
# 以当前用户、当前的 CC_VIEWER_DATA 在子进程里跑一遍再收回结果。
# 子进程只输出 ASCII 的 JSON 行：经伪终端按块转发时中文会被切坏，转义成 ASCII 就不会。
# grep 读数据目录里的正文索引 recall_index/（目录 0700、文件 0600，里面有密码之类的原话），见下面「正文索引」。
# 只有抓气泡：列会话看不出会话做了什么，按时间找会话、看一个会话的上下文都是按条件抓气泡，list、show、index 都去掉了。
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
    roots = list(isolation.roots(internal=True))
    snap = calls.snapshot(roots)
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


def _snippet(t, word, width=100):
    i = t.find(word)
    s = max(0, i - 30)
    e = min(len(t), max(s + width, i + len(word)))
    return ("…" if s else "") + " ".join(t[s:e].split()) + ("…" if e < len(t) else "")


def _text(x, word, cut):
    """一条气泡要显示的文字：cut 为 0 给全文；给了词是命中处前后约 cut 字，没给词是开头 cut 字"""
    if cut == 0:
        return " ".join(x.split())
    if word:
        return _snippet(x, word, cut)
    t = " ".join(x.split())
    return t[:cut] + ("…" if len(t) > cut else "")


def here_grep(inst, a):
    """在这个实例的正文索引里抓气泡，先把新增的部分补进索引。发回符合条件的总数、每个会话的条数，
    和从中随机抽的至多 -n 条；抽出来的保持随机顺序发回，父进程才能按各份的条数均匀合并"""
    import users
    word = a.word
    files = list(_all_mains())
    st, pending = _update_index(inst, files, START + 6.5)
    d = _index_dir()
    key = json.dumps(word, ensure_ascii=False)[1:-1].encode() if word else b""   # 索引里文字按 JSON 存，词也照样转义了再按字节找
    lo, hi = _span(a)
    lo_s, hi_s = _iso(lo), _iso(hi)
    want = "u" if a.user else "a" if a.assistant else None
    found, per = [], {}
    for root, sid, p, size, mtime, env in files:
        rec = st.get(str(p))
        if not rec or not rec["blen"] or (a.sess and not sid.startswith(a.sess)):
            continue
        if lo and mtime < lo.timestamp():   # 起点之前就没再动过的会话，里面不会有更晚的气泡
            continue
        if a.who and (users.owner_of(sid) or "") != a.who:
            continue
        with open(_body_file(d, p), "rb") as f:
            data = f.read(rec["blen"])   # 只认状态里记着的长度，别的搜索正在写的那截不算
        if key and key not in data:
            continue
        for raw in data.splitlines():
            if key and key not in raw:
                continue
            m = HEAD.match(raw)
            if not m:
                continue
            t, w = m.group(1).decode(), m.group(2).decode()
            if (want and w != want) or (lo_s and t < lo_s) or (hi_s and t >= hi_s):
                continue
            if word and word not in json.loads(raw)["x"]:   # 按字节找到的也可能落在时间或转义里，对着原文再核一遍
                continue
            found.append((sid, raw))
            per[sid] = per.get(sid, 0) + 1
    _emit({"inst": inst, "count": len(found), "per": per, "pending": pending})
    cut = a.cut if a.cut is not None else (100 if word else 200)
    for sid, raw in random.sample(found, min(len(found), a.n or 80)):
        e = json.loads(raw)
        _emit({"inst": inst, "ts": e["t"], "sid": sid, "owner": users.owner_of(sid),
               "who": "助手" if e["w"] == "a" else "用户", "text": _text(e["x"], word, cut)})


def _local(ts):
    try:
        return datetime.fromisoformat(ts[:19] + "+00:00").astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return ts[:16]


def run_child(inst, argv):
    """在那个实例的身份和数据目录下跑本脚本，收回 JSON 行；超时就当这份没搜到"""
    me = [PY, os.path.abspath(__file__), *argv, "--here", inst]
    try:
        if INSTANCES[inst] is not None:   # 只搜本实例：照当前环境的 CC_VIEWER_DATA
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


def main():
    if len(sys.argv) > 1 and sys.argv[1] in GONE:
        sys.exit(f"{sys.argv[1]} 已经去掉了，回忆只剩抓气泡：看一个会话用 grep --sess <编号开头>，"
                 "按时间找用 grep --from … --to …，用法见 --help")
    # 用法说明就是文件开头那段：--help 原先只有一串没解释的参数名，换了模型的 AI 看了也不知道怎么用
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["grep"], help="只有 grep：抓正文气泡")
    ap.add_argument("word", nargs="?", default="", help="要找的词，可以不给")
    ap.add_argument("-n", type=int, default=None, help="最多给几条（默认 80），多了在全部符合条件的里均匀随机抽")
    ap.add_argument("--who", default="", help="只要这个网页账号名的会话")
    ap.add_argument("--sess", default="", help="只要编号以它开头的会话")
    ap.add_argument("--from", dest="since", default="", help="只要这个时刻以后说的（北京时间，如 10-25 20:00）")
    ap.add_argument("--to", dest="until", default="", help="只要这个时刻以前说的（含那一分钟或那一天）")
    ap.add_argument("--days", type=float, default=None, help="只要最近几天说的")
    ap.add_argument("--cut", type=int, default=None, help="每条给多少字：给了词默认 100，没给词默认 200，0 是全文")
    by = ap.add_mutually_exclusive_group()
    by.add_argument("--user", "-u", action="store_true", help="只要用户（人）说的话")
    by.add_argument("--assistant", "-a", action="store_true", help="只要助手说的话")
    ap.add_argument("--here", default="", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.here:
        here_grep(a.here, a)
        return
    if a.word and a.word.isascii() and len(a.word) < 2:
        sys.exit("英文至少两个字母，中文至少一个字")
    if not (a.word or a.who or a.sess or a.since or a.until or a.days or a.user or a.assistant):
        sys.exit("要给一个词，或者至少一个条件（--sess、--from、--to、--days、--who、-u、-a），用法见 --help")
    _span(a)   # 时刻写错了在这里就报，不去开子进程
    insts = list(INSTANCES)
    with ThreadPoolExecutor(max_workers=len(insts)) as ex:
        parts = list(ex.map(lambda inst: run_child(inst, sys.argv[1:]), insts))
    limit = a.n or 80
    heads, shown, per, failed, busy, pending = [], [], {}, False, False, 0
    for got in parts:
        rows, h = [], None
        for e in got:
            if "err" in e:
                print("！" + e["err"], file=sys.stderr)
            elif "count" in e:
                h = e
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
    picked.sort(key=lambda e: e["ts"])
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
    if picked:
        print("按时间从旧到新：")
    for e in picked:
        print(f"{_local(e['ts'])} {e['sid'][:8]} {e['owner'] or '无主'} {e['who']}：{e['text']}")


if __name__ == "__main__":
    main()
