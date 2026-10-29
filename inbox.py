"""Claude 那边的状态栏脚本记下的额度和调用，收进查看器自己的数据目录。

查看器和 Claude 分属两个系统用户时（instance.RUN_AS，见 hostfs.py），状态栏脚本（quota_statusline.py）由 CLI 拉起，
跟着 Claude 是 RUN_AS，写不进查看器的数据目录（本来就不能让 RUN_AS 写得进），所以照旧写在 Claude 那边的数据目录
（instance.GUEST_DATA，~/.cc_viewer），这里定时把新写的部分收过来（server.watch 每隔几秒调一次 collect）：
- quota_history.jsonl 的新行：追加进自己的 quota_history.jsonl，并照状态栏脚本的做法合并进 quota/<号>.json；
- api_calls.jsonl 的新行：还没记过的回复编号追加进自己的 api_calls.jsonl（同一个编号只认第一行，网页对话自己当场记的在先）。
那边的文件 RUN_AS 都改得了，所以只当成和以前终端里报上来的一样可信：号不在账号表里、格式不对、时间离谱的行一律不收。
各文件读到哪儿记在数据目录的 .inbox_state.json；第一次见到的文件从当时的末尾开始收（以前的已经随数据目录搬过来了）。
查看器和 Claude 用同一个数据目录时什么都不做。
"""
import json
import os
import re
import threading
import time

import api_calls
import hostfs
from instance import ACCOUNTS, DATA, GUEST_DATA

ACTIVE = GUEST_DATA != DATA
STATE = DATA / ".inbox_state.json"
QUOTA_DIR = DATA / "quota"
WINDOWS = ("five_hour", "seven_day")
SID = re.compile(r"[0-9A-Za-z-]{8,64}")
_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {}


def _save(st: dict):
    tmp = STATE.with_name(f".inbox_state.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def _new_lines(name: str, st: dict) -> list[bytes]:
    """那边这个文件新写的完整的行；读到哪儿记进 st。文件变短了（被换掉）就从头再来。"""
    p = hostfs.Path(str(GUEST_DATA / name))
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        st.setdefault(name, 0)   # 还没有：以后出现了从头收
        return []
    if name not in st:   # 第一次见到：以前的已经在自己这边了，从现在的末尾开始收
        st[name] = size
        return []
    off = st[name] if st[name] <= size else 0
    if size == off:
        return []
    with p.open("rb") as f:
        f.seek(off)
        data = f.read(size - off)
    end = data.rfind(b"\n") + 1
    st[name] = off + end
    return data[:end].splitlines()


def _parse(raw: bytes, now: float) -> dict | None:
    """一行能收就返回它，不能收就是 None：号要在账号表里，时间要在一周以内、不能跑到将来去。"""
    try:
        r = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(r, dict) or r.get("account") not in ACCOUNTS:
        return None
    ts = r.get("ts")
    try:
        t = api_calls.stamp(ts)
    except (TypeError, ValueError):
        return None
    return r if now - 7 * 86400 < t < now + 300 else None


def _quota(rows: list[dict]):
    """额度历史：追加进自己的那份，同时合并进 quota/<号>.json（比那边这次读数还新的不动）。"""
    good = []
    for r in rows:
        windows = {k: r[k] for k in WINDOWS if isinstance(r.get(k), dict)}
        if not windows or not isinstance(r["ts"], (int, float)):
            continue
        rec = {"ts": r["ts"], "account": r["account"], "source": str(r.get("source") or "statusline"),
               **({"session": r["session"]} if isinstance(r.get("session"), str) else {}), **windows}
        good.append(rec)
        QUOTA_DIR.mkdir(exist_ok=True)
        path = QUOTA_DIR / f"{r['account']}.json"
        try:
            snap = json.loads(path.read_text())
        except (OSError, ValueError):
            snap = {}
        if (snap.get("ts") or 0) > rec["ts"]:
            continue
        snap.update(rec)
        tmp = QUOTA_DIR / f".{r['account']}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(snap, ensure_ascii=False))
        tmp.replace(path)
    if good:
        with open(DATA / "quota_history.jsonl", "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in good))


def _calls(rows: list[dict]):
    good = [{"ts": r["ts"], "account": r["account"], "source": "terminal", "session": r["session"],
             **({"agent": r["agent"]} if isinstance(r.get("agent"), str) else {}), "mid": r["mid"]}
            for r in rows if isinstance(r.get("mid"), str) and isinstance(r.get("session"), str)
            and SID.fullmatch(r["session"])]
    if good:
        api_calls.merge(good)


def collect():
    """把那边新写的收过来。出错往外抛，由调用的地方记日志；读到哪儿只在收完以后才存，下次接着收。"""
    if not ACTIVE:
        return
    with _lock:
        st, now = _load(), time.time()
        q = [r for raw in _new_lines("quota_history.jsonl", st) if (r := _parse(raw, now))]
        c = [r for raw in _new_lines("api_calls.jsonl", st) if (r := _parse(raw, now))]
        _quota(q)
        _calls(c)
        _save(st)
