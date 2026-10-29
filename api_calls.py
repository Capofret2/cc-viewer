"""每次 API 调用是哪个号发的：一次调用一行，记在 api_calls.jsonl。
    {"ts", "account", "source": "web" | "terminal" | "remote", "session", "agent", "mid"}
remote 表示这次调用已由另一个查看器结算，只展示费用，本地不重复扣款；以实际 CLI 路线标记，不按切换时间猜历史。
mid 是回复编号，session 是会话编号（会话文件名），agent 是子代理的文件名（主对话、或记的时候不知道是哪个子代理就不写）。

会话记录里有 token 数但不写是哪个号发的，所以要趁知道的时候当场记。token 数不记在这里，一律从会话记录里取（calls.py），
这样气泡上看到的、账本扣的、拟合用的是同一份数。老版本记的行还带着模型和四个 token 数，那是当时记的副本，
有些是流式写到一半的中间数，读的时候一律不用。

什么时候记：
- 网页对话：驱动的 CLI 一发出请求（message_start 里就有回复编号）就记（note），不等一轮结束，查看器重启也丢不了；
  一轮结束时再按会话文件补一遍（record），流事件漏了的、子代理的都不漏。
- 终端里：状态栏脚本每次运行时，按会话文件记下新出现的调用（record），号是启动这个 CLI 的别名。
同一个回复编号只记一次，不分会话：分叉续接的会话会把整段历史抄进新文件（行的 uuid、时间都不变，只有会话编号换成新的），
抄过去的调用已经记在原来的会话名下，不再记一遍；读的时候也只认每个回复编号的第一行（老版本记重过，照这条处理）。

每个会话文件读到哪儿记在 .api_calls_state.json。第一次碰到的会话从当时的末尾开始记，以前的调用不知道是哪个号发的，不补。
"""
import contextlib
import fcntl
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import calls
import hostfs   # 会话文件在运行 Claude 的那个用户名下，经它读；调用记录自己写在数据目录里
from instance import DATA   # 记在这个实例的数据目录里（见 instance.py）

LOG = DATA / "api_calls.jsonl"
STATE = DATA / ".api_calls_state.json"
LOCK = DATA / ".api_calls.lock"


def stamp(ts) -> float:
    """行里的时间：以前记的是会话记录里的时间字符串，网页请求一发出就记的是 unix 秒。"""
    return ts if isinstance(ts, (int, float)) else datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()


@contextlib.contextmanager
def locked(wait: float):
    """网页 server 和好几个终端的状态栏脚本可能同时来记，用文件锁排队；等不到就放弃这一次（下次接着记）。"""
    with open(LOCK, "a") as f:
        t0 = time.monotonic()
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - t0 > wait:
                    raise TimeoutError("api_calls 锁被占着")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Index:
    """调用记录读进内存：回复编号 → (号, 会话编号)，只认每个编号的第一行。增量读，文件变短（被换掉）就从头来。"""

    def __init__(self, path: Path = LOG):
        self.path = Path(path)
        self.lock = threading.Lock()
        self._reset()

    def _reset(self):
        # gen 加 off 一起认得出索引变没变（users._own 按它缓存）：从头再读一遍时 off 可能又回到原来的数
        self.gen = getattr(self, "gen", 0) + 1
        self.off = 0
        self.first: dict[str, tuple[str, str | None]] = {}
        self.by_sid: dict[str | None, dict[str, str]] = {}
        self.remote: set[str] = set()   # 真正走远端结算的调用，不能用改配置的时刻倒推历史
        self.start: float | None = None   # 最早一行的时间：开始记调用的时刻
        # 按文件里的先后：(时间, 回复编号, 号, 会话编号)。预定额度要按号、按时间找调用（reserve.py）
        self.order: list[tuple[float, str, str, str | None]] = []

    def refresh(self):
        with self.lock:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                return
            if size < self.off:
                self._reset()
            if size == self.off:
                return
            with open(self.path, "rb") as f:
                f.seek(self.off)
                data = f.read(size - self.off)
            end = data.rfind(b"\n") + 1   # 最后一行可能还没写完，留到下次
            for raw in data[:end].splitlines():
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                mid, acc = r.get("mid"), r.get("account")
                if not mid or not acc or mid in self.first:
                    continue
                t = stamp(r["ts"])
                self.start = t if self.start is None else min(self.start, t)
                self.first[mid] = (acc, r.get("session"))
                if r.get('source') == 'remote':
                    self.remote.add(mid)
                self.by_sid.setdefault(r.get("session"), {})[mid] = acc
                self.order.append((t, mid, acc, r.get("session")))
            self.off += end

    def account(self, mid: str) -> str | None:
        hit = self.first.get(mid)
        return hit[0] if hit else None

    def owner(self, mid: str) -> str | None:
        """这次调用记在哪个会话名下（第一次记下它的那个）。"""
        hit = self.first.get(mid)
        return hit[1] if hit else None

    def accounts_of(self, sid: str) -> dict[str, str]:
        return dict(self.by_sid.get(sid, {}))

    def count(self, sid: str) -> int:
        return len(self.by_sid.get(sid, ()))

    def since(self, account: str, t: float) -> list[tuple[float, str, str | None]]:
        """这个号在 t 以后发的调用：(时间, 回复编号, 会话编号)。"""
        return [(ts, mid, sid) for ts, mid, acc, sid in list(self.order) if acc == account and ts > t]

    def recorded(self, ts) -> bool:
        """这个时刻已经在记调用了吗（之前的调用本来就不知道是哪个号发的）。"""
        return self.start is not None and ts is not None and stamp(ts) >= self.start


INDEX = Index()


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict):
    tmp = STATE.with_name(f".api_calls_state.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _append(rows: list[dict]):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    INDEX.refresh()


def note(mid: str, account: str, source: str, session: str):
    """网页对话的 CLI 发出一次请求时调用：这个回复编号还没记过就记下是哪个号发的。"""
    with locked(5):
        INDEX.refresh()
        if INDEX.account(mid) is None:
            _append([{"ts": time.time(), "account": account, "source": source, "session": session, "mid": mid}])


def merge(rows: list[dict]) -> int:
    """别处记下的调用行（Claude 那边状态栏脚本写的，见 inbox.py）并进来：还没记过的回复编号才记，返回记了几行。"""
    with locked(5):
        INDEX.refresh()
        new, seen = [], set()
        for r in rows:
            if INDEX.account(r["mid"]) is None and r["mid"] not in seen:
                seen.add(r["mid"])
                new.append(r)
        if new:
            _append(new)
        return len(new)


def web_source(account):
    import instance
    return 'remote' if instance.remote_billed(account) else 'web'


def record(main: Path | str, account: str, source: str, wait: float = 5) -> int:
    """按会话文件（连同子代理）记下新出现、还没记过的调用，都算在 account 头上。
    第一次碰到这个会话，只从现在的末尾开始记。返回记了几次。"""
    main = hostfs.Path(main).resolve()   # 各号的 projects 目录是软链接，同一个文件只认一个名字
    if not main.is_file():
        return 0
    with locked(wait):
        state = load_state()
        files = calls.files_of(main)
        if str(main) not in state:   # 第一次碰到：以前的调用不知道是哪个号的，从现在的末尾开始
            for p in files:
                state[str(p)] = {"offset": size(p)}
            state[str(main)].update(account=account, source=source)
            save_state(state)
            return 0
        INDEX.refresh()
        rows = []
        for p in files:
            ent = state.setdefault(str(p), {"offset": 0})   # 后来才出现的子代理文件，从头记
            if size(p) < ent["offset"]:   # 文件被截短或换掉了，从头来（记过的编号不会再记）
                ent["offset"] = 0
            new, ent["offset"] = calls.scan(p, ent["offset"])
            for mid, c in new.items():
                if INDEX.account(mid) is None and all(r["mid"] != mid for r in rows):
                    rows.append({"ts": c["ts"], "account": account, "source": source, "session": main.stem,
                                 "agent": p.stem if p != main else None, "mid": mid})
        state[str(main)].update(account=account, source=source)
        if rows:
            _append(rows)
        save_state(state)
        return len(rows)


def start(main: Path | str, account: str, fresh: bool = False):
    """网页上要往这个会话发话、Claude 又没在干活时调用。fresh=True 是网页刚新建的会话，从文件开头记；
    以前没记过的会话从现在的末尾开始记；记过的，先把上次没记上的（比如查看器重启时正在进行的那一轮）按上次驱动它的号收尾，
    再记下这次是 account 在驱动，下次收尾就按它算。"""
    main = hostfs.Path(main).resolve()
    with locked(5):
        state = load_state()
        ent = state.get(str(main))
        if fresh or not ent or not ent.get("account"):
            for p in calls.files_of(main):
                state[str(p)] = {"offset": 0 if fresh else size(p)}
            state[str(main)].update(account=account, source=web_source(account))
            save_state(state)
            return
    record(main, ent["account"], ent.get("source") or "web")
    with locked(5):
        state = load_state()
        state[str(main)].update(account=account, source=web_source(account))
        save_state(state)
