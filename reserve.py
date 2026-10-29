"""预定额度：在选号的地方右键（手机上长按）某个号，付钱订下它的一部分额度。

两种预定，都是一下单就一次付清、用不完也不退，也没有「兑现不了退钱」这回事（订的时候就按能保证的上限卡住）：
- 5 小时预定：订某一个 5 小时窗口（当前的，或者本周重置之前以后的某一个）的百分之几，窗口结束时没用完的作废。
  它同时占着折成周额度的那部分（百分点 × FIVE_IN_WEEK），下单当场就从别人能用的本周额度里扣掉，
  不然别人可能先把周额度烧完，到时候这个窗口兑现不了。窗口过去了没用完的，那部分周额度还回去。
- 周预定：订本周额度的百分之几，不绑窗口，本周重置时没用完的作废。用它时 5 小时窗口的限制照样有，和别人共用。
  还能提前订以后的几周（连本周最多 WEEKS 周）：周额度固定 7 天一轮（额度历史里 Claude 号相邻两次重置都正好隔 7 天，
  analysis/first_use.py 核对的），以后各周按本周重置时刻往后推。以后的周记在这一批的 weeks 里（[{start, until, pct}]），
  到了那一周才开始占额度、付调用，和本周的周预定（week）一样；每一周最多订 100% 减去那一周已经被订的。
  号到期以后的周和窗口不列出来、也不能订（instance.EXPIRES，没写到期时间的号不限）。

别人看来，预定的额度就像已经被用掉了，和官方限额扣血一样：当前窗口的 5 小时预定从 5 小时血条上扣掉（画成更早用掉的空槽，
不画红色——红色是最近有人在用，表示速度），周预定和 5 小时预定占的周额度从本周剩余里扣掉（本周快用完时血条跟着砍短，
最后几管别人看来已经空了）。血条上别人看不出有人在预定；打开预定面板才列出这个号上所有人的每一批（mine_list）。
预定的人自己看到蓝条：周预定是深蓝，5 小时预定是浅一点、会呼吸的蓝。每一批预定（付一次款）自己选先用还是后用：
front=True 先用预定的（蓝条在绿条右边，先被吃掉，不容易浪费），False 当保底最后才用（蓝条在左边）。
用蓝条不扣钱：每次调用发出后，后台按会话主人、号、时刻决定由哪几批的蓝来付，一次一行记进 reserve_use.jsonl，记下就不再改；
账本（users.ledger）里扣掉付预定的钱，蓝付掉的那部分调用不再扣钱。
特惠（手写的限时特惠 instance.PROMO、号主自己定的折扣 set_owner_promo 和自动特惠 AUTO_PROMO，见 promo_factor）：时段里的调用免掉的那几成要有绿的来出、
不碰预定，其余进账本的那几成照常由预定付；绿的不够就少免，用完了（剩下的全是自己的预定）就不免，整次由预定付。预定本身按原价收，不受特惠影响。

以后窗口的时刻要靠「每次重置立刻开下一个窗口」才确定（重置后没人用，下一个窗口就会往后推），这件事由独立的 keeper.py 做。
预测拦截（server.limit_block）也用这里的算法：一个人此刻还能用多少（split），下一次请求大约要花多少（next_cost）。

单位：5 小时预定、蓝条、绿条都按真实的 5 小时百分点，周预定按本周百分点；和点数的换算靠实例配置（instance.py）的
PTS_PER_FIVE（5 小时 1% 合几点）和 FIVE_IN_WEEK（5 小时 1% 合本周百分之几）；GPT 号各有各的（instance.pts_per_five / five_in_week 按号取）。
没有 5 小时限额的号（instance.WEEK_ONLY）：「5 小时百分点」只是计价单位，5 小时这层当成无穷大，只能周预定。
"""
import contextlib
import json
import math
import statistics
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import api_calls
import api_usage
import calls
import peak
import rates
# 换算按号取：GPT 号（ChatGPT 的订阅）的 5 小时窗口折合本周、合几点都和实例里的 Claude 号不一样（见 instance.py）
from instance import ADMINS, AUTO_ACCOUNTS, AUTO_PROMO, DATA, EXPIRES, FIVE_IN_WEEK, NO_RESERVE, OWNERS, WEEK_ONLY, five_in_week, pts_per_five, week_end
from instance import model_family
from instance import promo_factor as fixed_promo_factor   # 手写的限时特惠；加上自动特惠的是下面的 promo_factor

FILE = DATA / "reservations.jsonl"          # 预定：一批一行
CANCEL_FILE = DATA / "reserve_cancel.jsonl"  # 撤销的预定（查看器账号撤的，或号主撤自己在自己号上的）：一批一行，预定文件本身一行不动
USE_FILE = DATA / "reserve_use.jsonl"       # 每次调用由哪几批的蓝付了多少：一次调用一行
OWNER_PROMO_FILE = DATA / "owner_promo.jsonl"   # 号主自己定的折扣：改一次一行（见 set_owner_promo）
HISTORY = DATA / "quota_history.jsonl"      # 额度历史：从里面认出每个号实际开过哪些窗口
KEEPER_STATE = DATA / "keeper_state.json"   # keeper.py 的心跳和最近一次开窗
FIVE = 5 * 3600
SAME = FIVE / 2      # 预定时预测的开窗时刻和实际的差不到半个窗口，就是同一个窗口（链条偶尔会偏一点）
WEEK = 7 * 86400
WEEKS = 5            # 周预定连本周在内最多能订几周
GIVE_UP = 900       # 一次调用发出这么久还读不到 token 数（被打断、没写进记录），不再等它：没有用量，也就没什么要付
SYSTEM_GUESS = 20000   # 会话的第一次请求里系统提示词、工具说明这些大约多少 token（只用来预测，没有更准的来源）
BYTES_PER_TOKEN = 3    # 预测时按 utf-8 字节数估 token：中文一个字三个字节、约一个 token，英文四个字母约一个
IMAGE_TOKENS = 1600    # 一张图大约多少 token
# 一次工具结果最多算多少 token：太长的输出 CLI 会存成文件、只把开头一小段交给模型（实测 165KB 的输出只交了 2KB 预览），
# Read 自己也限在 25000 token 以内，所以按这个封顶，免得把存成文件的大输出整个算进去
TOOL_TOKENS_MAX = 25000
CACHE_TTL = 3600       # 缓存一小时没用就失效，再发要整段重新写入；写入只有一个价，和存多久无关（见 FITTING.md）
FEN = 5                # 1 点 = 5 分，和前端 points.js 的 FEN 一致，只用在给人看的说明里

_lock = threading.RLock()
batches: list[dict] = []
# 回复编号 → 这次调用由哪几批付了多少。只有 _apply 往里放、放的是整条建好的记录，放进去以后不再改，
# 所以只查一条的（frac、recorded_free、free_of）不拿锁：推送循环在事件循环里对会话的每条回复各查一遍，
# 拿锁就要等 process 在线程里分完一整圈（它拿着锁读会话记录、写文件），多用户实例卡顿的大头曾经就是这里
uses: dict[str, dict] = {}
used5: defaultdict = defaultdict(float)             # (批编号, 窗口开始时刻) → 这批这个窗口已经用掉的 5 小时百分点
usedW: defaultdict = defaultdict(float)             # 批编号 → 这批的周预定已经用掉的周百分点
settled_n: defaultdict = defaultdict(int)           # 会话编号 → 这个会话有几次调用分给了预定：变了就把标签页再推一次，气泡上的花费跟着变蓝
_file_off = 0   # 预定文件读到哪儿：keeper.py 是另一个进程，靠它增量读到 server 新写的预定
_cancel_off = 0
cancelled: dict[str, dict] = {}                     # 批编号 → 撤销记录
dropped: list[dict] = []                            # 撤销了的批（带 cancel 字段）：不再占额度、不再付调用，只留着记账


def _read_lines(path: Path, off: int = 0) -> tuple[list[dict], int]:
    """从 off 往后读完整的行。文件不在就是还没有；在却解析不了，就报错停下，不能当成没有预定。"""
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except FileNotFoundError:
        return [], off
    end = data.rfind(b"\n") + 1
    return [json.loads(x) for x in data[:end].splitlines() if x.strip()], off + end


def _append(path: Path, rec: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def reload():
    """把预定文件新写的行读进来（本进程写的在 book 里已经加过，按编号去重），撤销了的批挪进 dropped。"""
    global _file_off, _cancel_off
    with _lock:
        rows, _file_off = _read_lines(FILE, _file_off)
        have = {b["id"] for b in batches} | {b["id"] for b in dropped}
        batches.extend(b for b in rows if b["id"] not in have)
        rows, _cancel_off = _read_lines(CANCEL_FILE, _cancel_off)
        cancelled.update((r["id"], r) for r in rows)
        for b in [b for b in batches if b["id"] in cancelled]:
            batches.remove(b)
            dropped.append({**b, "cancel": cancelled[b["id"]]})


def charged(b: dict) -> float:
    """这一批实际收了多少点（按单价算，收钱比例由调用的地方乘）：撤销了的扣掉退回的那部分。"""
    return b["points"] - ((b.get("cancel") or {}).get("refund") or 0.0)


def week_slots(b: dict) -> list[tuple]:
    """这一批订的每一周：(记用量的键, 开始时刻, 重置时刻, 订了几个周百分点)。本周的那份（week）下单就开始了，开始时刻是 None，
    用量按批编号记（和以前一样）；以后的周（weeks）按 (批编号, 重置时刻) 记。"""
    out = [(b["id"], None, w["until"], w["pct"])] if (w := b.get("week")) else []
    return out + [((b["id"], w["until"]), w["start"], w["until"], w["pct"]) for w in b.get("weeks") or ()]


def _active(start: float | None, until: float, t: float) -> bool:
    return (start is None or start <= t) and t < until


def _used_share(b: dict) -> float:
    """这一批订下的额度用掉了几成（0～1）：5 小时预定按 5 小时百分点，周预定（本周和以后各周）按下单时的换算折成 5 小时百分点，合在一起算。"""
    k = b.get("k") or FIVE_IN_WEEK
    slots = week_slots(b)
    bought = sum(s["pct"] for s in b["five"]) + sum(pct for *_, pct in slots) / k
    used = sum(used5[(b["id"], s["start"])] for s in b["five"]) + sum(usedW[key] for key, *_ in slots) / k
    return min(1.0, used / bought) if bought > 0 else 1.0


def cancel(batch_id: str, by: str, now: float) -> dict:
    """撤销一批预定：查看器账号能撤任何人的；用户自己只能撤自己在自己号上的（钱本来就流回自己），
    别的单下单时写明了付了不能撤销。谁能撤由 server 的接口核好再调这里。
    没用掉的那几成按下单的价钱退回（refund，点数），这一批就此作废：占着的额度还给别人，以后的调用不再由它付。
    已经由它付掉的调用照旧算预定付的、不再扣钱，那几成的钱也不退。还没分好的调用（记下后几秒才分）照常扣钱。"""
    with _lock:
        b = next((b for b in batches if b["id"] == batch_id), None)
        if not b:
            raise ValueError("没有这一批预定，可能已经撤销过了")
        refund = round(b["points"] * (1 - _used_share(b)), 4)
        rec = {"id": batch_id, "ts": now, "by": by, "user": b["user"], "account": b["account"], "refund": refund}
        _append(CANCEL_FILE, rec)
        cancelled[batch_id] = rec
        batches.remove(b)
        d = {**b, "cancel": rec}
        dropped.append(d)
        return d


def _apply(u: dict):
    uses[u["mid"]] = u
    if (u["parts"] or "free" in u) and u.get("sid"):   # 特惠免了几成也会改气泡和花费，同样要再推一次
        settled_n[u["sid"]] += 1
    for p in u["parts"]:
        if p["kind"] == "five":
            used5[(p["batch"], p["start"])] += p["five"]
        else:   # 以后各周的那几份带着 until（见 week_slots）
            usedW[(p["batch"], p["until"]) if p.get("until") else p["batch"]] += p["week"]


# ---------- 实际开过的窗口 ----------

_hist_off = 0
_starts: defaultdict = defaultdict(list)   # 号 → 实际开过的窗口的开始时刻（重置时刻减 5 小时）


def _read_history():
    global _hist_off
    try:
        size = HISTORY.stat().st_size
    except FileNotFoundError:
        return
    if size < _hist_off:   # 文件被换过，从头读
        _hist_off = 0
        _starts.clear()
        _weekR.clear()
        _week_dirty.update(_auto)
    if size == _hist_off:
        return
    with open(HISTORY, "rb") as f:
        f.seek(_hist_off)
        data = f.read(size - _hist_off)
    end = data.rfind(b"\n") + 1
    for raw in data[:end].splitlines():
        with contextlib.suppress(ValueError, KeyError, TypeError):
            r = json.loads(raw)
            _note_week(r)
            w = r.get("five_hour") or {}
            if w.get("resets_at"):
                s, got = float(w["resets_at"]) - FIVE, _starts[r["account"]]
                if not any(abs(x - s) < 600 for x in got[-5:]):   # 状态栏和 /usage 报的重置时刻会差一分钟左右
                    got.append(s)
    _hist_off += end


def window_at(account: str, ts: float) -> float | None:
    """这个号在 ts 那一刻实际在走的窗口是什么时候开的；额度历史里认不出来就是 None。"""
    with _lock:
        _read_history()
        hits = [s for s in _starts.get(account, ()) if s <= ts < s + FIVE]
    return max(hits) if hits else None


def current_start(snap: dict, now: float) -> float | None:
    """这个号此刻正在走的 5 小时窗口是什么时候开的；没有正在走的（已过重置时刻还没人用，或者从没查到）是 None。"""
    r = (snap.get("five_hour") or {}).get("resets_at")
    return r - FIVE if r and r > now else None


def phase(start: float, cur: float | None, now: float) -> str:
    """一个预定窗口（按预测的开始时刻）此刻是 now 当前的、future 以后的，还是 past 已经过去了。
    没有正在走的窗口时，该开的窗口马上就会开（keeper.py 补上），开始时刻已到、还没走完一个窗口长的都算当前的。"""
    if cur is not None:
        if abs(start - cur) < SAME:
            return "now"
        return "future" if start > cur else "past"
    if start > now + SAME:
        return "future"
    return "now" if start + FIVE + 600 > now else "past"


# ---------- 特惠（限时特惠 instance.PROMO 和自动特惠 instance.AUTO_PROMO） ----------

_weekR: defaultdict = defaultdict(list)   # 号 → [(读数时刻, 那次读数报的本周重置时刻)]：按文件顺序，只记重置时刻变了的那几条
_week_dirty: set = set()                  # 记了新的、要重算自动特惠时段的号
_auto: dict[str, list] = {}               # 号 → 自动特惠的时段 [(开始, 结束)]（unix 秒）
promo_gen = 0                             # 自动特惠的时段变一次加一：会话花费的缓存（users._cost_key）带上它，变了就重算
_auto_checked = -math.inf


def _note_week(r: dict):
    """额度历史的一行报的本周重置时刻：和上一条差一分钟以上（状态栏和 /usage 会差一分钟左右）才记下读数时刻和它。
    读数时已经过了重置时刻的是旧数，不算知道了新的。"""
    R, ts = (r.get("seven_day") or {}).get("resets_at"), r.get("ts")
    if not AUTO_PROMO or r.get("account") not in AUTO_ACCOUNTS or not R or not ts:
        return
    R = week_end(r["account"], float(R))   # 订阅在重置之前就到期的，「重置前 hours 小时」按到期时刻算
    if R <= float(ts):
        return
    got = _weekR[r["account"]]
    if not got or abs(got[-1][1] - float(R)) >= 600:
        got.append((float(ts), float(R)))
        _week_dirty.add(r["account"])


def _rebuild_auto():
    """按额度历史重算自动特惠的时段。某一刻最近一次读数报的本周重置时刻离它不到 hours 小时，这一刻就在特惠里，
    所以时段是「这次读数起、到下一次报了别的重置时刻的读数」和「重置前 hours 小时到重置」的交，从 since 起算。
    额度历史只增不改，同一次调用无论什么时候重算账本，算出来都一样；重置时刻临时挪了，也按当时知道的算。"""
    global promo_gen
    for acc in _week_dirty:
        pts, wins = sorted(_weekR.get(acc, ())), []
        # official_since 起只给官方号（号主是查看器账号），别人名下的号时段截到那一刻：以前免掉的照旧，以后由号主自己定折扣
        cut = math.inf if official(acc) else AUTO_PROMO.get("official_since") or math.inf
        for i, (ts, R) in enumerate(pts):
            nxt = pts[i + 1][0] if i + 1 < len(pts) else math.inf
            a, b = max(ts, R - AUTO_PROMO["hours"] * 3600, AUTO_PROMO["since"]), min(nxt, R, cut)
            if a >= b:
                continue
            if wins and abs(wins[-1][1] - a) < 1:   # 同一个重置时刻被几个来源先后报了，时段接起来
                wins[-1] = (wins[-1][0], b)
            else:
                wins.append((a, b))
        if wins != _auto.get(acc, []):
            _auto[acc] = wins
            promo_gen += 1
    _week_dirty.clear()


def _refresh_auto():
    """读额度历史新写的行、重算变了的时段；最多五秒一次（账本一算就是几千次调用，每次都去看文件太慢）。"""
    global _auto_checked
    if time.monotonic() - _auto_checked < 5:
        return
    with _lock:
        _auto_checked = time.monotonic()
        _read_history()
        if _week_dirty:
            _rebuild_auto()


def promo_version() -> int:
    """自动特惠时段的版本：先把额度历史新写的读进来（最多五秒一次），时段变了它就变。
    缓存按它判断要不要重算：直接读 promo_gen 的话，没人查过折扣时时段已经变了它也还没加（_refresh_auto 是查折扣时顺带做的）"""
    if AUTO_PROMO:
        _refresh_auto()
    return promo_gen


def official(account: str) -> bool:
    """官方号：号主是查看器账号的号。自动特惠在 official_since 以后只给它们"""
    return (OWNERS.get(account) or (None,))[0] in ADMINS


# 号主自己定的折扣：号主在预定面板里拖进度条，定自己名下的号收原价的百分之几，改一次追加一行
# {account, ts, factor, by}，文件只增不改。某一刻按那一刻以前最近的一次算，所以改了只管以后的调用，账本重算多少遍都一样。
# 规矩和限时特惠一样：免掉的那几成要有绿的来出，预定照原价卖，免掉的也不流向号主
_owner_promo: defaultdict = defaultdict(list)   # 号 → [(从这一刻起, 收几成)]，按时刻先后
for _r in _read_lines(OWNER_PROMO_FILE)[0]:
    _owner_promo[_r["account"]].append((float(_r["ts"]), float(_r["factor"])))
FOREVER = 4102444800.0   # 2100 年：号主定的折扣没有截止时刻，给前端的那一段拿它当结束


def owner_factor(account: str | None, t: float | None) -> float:
    """号主定的折扣：t 这一刻这个号收原价的几成，没定过（或 t 在第一次定之前）是 1。"""
    f = 1.0
    for ts, k in _owner_promo.get(account or "", ()):
        if t is None or ts > t:
            break
        f = k
    return f


def promo_label(f: float) -> str:
    """号主定的折扣在标签上怎么写，和手写特惠的叫法一样（「特惠 1折」「特惠半价」）。"""
    return "免费" if f == 0 else "特惠半价" if f == 0.5 else f"特惠 {round(f * 10, 1):g}折"


def set_owner_promo(account: str, factor: float, by: str, now: float):
    """号主定自己名下的号从现在起收原价的几成（谁能定由 server 的接口核好）。写不进文件就报错，内存里也不改。"""
    global promo_gen
    with _lock:
        _append(OWNER_PROMO_FILE, {"account": account, "ts": now, "factor": factor, "by": by})
        _owner_promo[account].append((now, factor))
        promo_gen += 1   # 会话花费的缓存跟着重算


def owner_promo_view() -> dict:
    """/api/me 给前端的号主折扣：号 → [{from, until, factor, label}]，和手写特惠同一个样子，前端并进 promo 一起用。不打折的那几段不给。"""
    with _lock:
        out = {}
        for acc, got in _owner_promo.items():
            segs = [{"from": ts, "until": got[i + 1][0] if i + 1 < len(got) else FOREVER, "factor": k, "label": promo_label(k)}
                    for i, (ts, k) in enumerate(got) if k < 1]
            if segs:
                out[acc] = segs
        return out


def promo_factor(account: str | None, t: float | None) -> float:
    """t 这一刻在这个号上花的钱按原价的几成进账本：手写的限时特惠、号主定的折扣和自动特惠都算，取低的；都不在就是 1。
    不知道时刻的按原价（取不到不能当成打折）。实际免了几成还要看绿条够不够（_settle、free_of）。"""
    f = fixed_promo_factor(account, t)
    if t is not None:
        f = min(f, owner_factor(account, t))
    if t is None or not AUTO_PROMO or account not in AUTO_ACCOUNTS or t < AUTO_PROMO["since"]:
        return f
    _refresh_auto()
    return min(f, AUTO_PROMO["factor"]) if any(a <= t < b for a, b in _auto.get(account, ())) else f


def auto_promo_view() -> dict:
    """/api/me 给前端的自动特惠：号 → {factor, label, windows: [[开始, 结束]]}，前端按它挂标签、算气泡上免掉的那份。"""
    if not AUTO_PROMO:
        return {}
    _refresh_auto()
    with _lock:
        return {a: {"factor": AUTO_PROMO["factor"], "label": AUTO_PROMO["label"], "windows": [list(w) for w in ws]}
                for a, ws in _auto.items() if ws}


def free_of(mid: str, account: str | None, t: float | None) -> float:
    """这次调用有几成是特惠免掉的（0～1）：结算时记下的（绿条不够就免得少，用完了就不免，见 _settle）；
    没结算的（会话主人在这个号上没有预定，只能花绿的，绿的总是够）按特惠折扣算。"""
    u = uses.get(mid)   # 不拿锁，见 uses
    if u is not None and "free" in u:
        return u["free"]
    return 1.0 - promo_factor(account, t)


def recorded_free(mid: str) -> float | None:
    """结算时记下的这次调用免了几成，没记的是 None（前端按特惠折扣算）。"""
    u = uses.get(mid)   # 不拿锁，见 uses
    return u.get("free") if u else None


def _promo_legacy(u: dict) -> dict:
    """老版本记的付款不认识特惠，按整次调用扣了预定。规矩改成免掉的那几成不扣预定以后，读进来时按特惠折扣把它折过、
    记上免了几成（文件一行不动；那时绿条够不够已经无从知道，按够算）；新记的带 promo，已经折过了。"""
    f = 1.0 if "promo" in u else promo_factor(u.get("account"), u.get("ts"))
    if f == 1.0:
        return u
    parts = [{**p, **{k: p[k] * f for k in ("five", "week", "points") if k in p}} for p in u["parts"]] if f > 0 else []
    return {**u, "points": u["points"] * f, "parts": parts, "free": 1.0 - f}


reload()
for _u in _read_lines(USE_FILE)[0]:
    _apply(_promo_legacy(_u))


# ---------- 谁手里还有多少 ----------

def _zero() -> dict:
    return {"ff": 0.0, "fb": 0.0, "wf": 0.0, "wb": 0.0, "lock": 0.0}


def holdings(account: str, snap: dict, now: float) -> dict[str, dict]:
    """这个号上每个人此刻手里的预定还剩多少：ff / fb 是当前窗口的 5 小时预定（先用的 / 保底的，5 小时百分点），
    wf / wb 是周预定（周百分点），lock 是一共占着多少周额度（周预定剩的，加上当前和以后窗口的 5 小时预定剩的 × FIVE_IN_WEEK）。"""
    cur, out = current_start(snap, now), {}
    with _lock:
        for b in batches:
            if b["account"] != account:
                continue
            h = out.setdefault(b["user"], _zero())
            side = "f" if b["front"] else "b"
            for s in b["five"]:
                ph = phase(s["start"], cur, now)
                if ph == "past":
                    continue
                left = max(0.0, s["pct"] - used5[(b["id"], s["start"])])
                if ph == "now":
                    h["f" + side] += left
                h["lock"] += left * five_in_week(account)
            for key, start, until, pct in week_slots(b):   # 以后的周到了那一周才算
                if _active(start, until, now):
                    left = max(0.0, pct - usedW[key])
                    h["w" + side] += left
                    h["lock"] += left
    return out


def decorate(accounts: dict, viewer: str | None, now: float):
    """/api/quota 给这个人看的额度（原地改）：别人预定的部分算成已经用掉，5 小时那份记在 five_hour.hold，
    周那份记在 seven_day.hold；自己的预定放在 mine 里画蓝条。没人预定的号不动。"""
    for acc, snap in accounts.items():
        hs = holdings(acc, snap, now)
        if not hs:
            continue
        others = [h for u, h in hs.items() if u != viewer]
        if snap.get("five_hour"):
            snap["five_hour"]["hold"] = sum(h["ff"] + h["fb"] for h in others)
        if snap.get("seven_day"):
            snap["seven_day"]["hold"] = sum(h["lock"] for h in others)
        if viewer in hs:
            snap["mine"] = hs[viewer]


def rest(w: dict | None, now: float, extra: float = 0.0) -> float | None:
    """一个窗口还剩多少（百分点）：已过重置时刻的按满的算，再扣掉别人的预定（hold）和读数之后估出来的消耗（extra）。没读数是 None。"""
    if not w or w.get("used_percentage") is None:
        return None
    used = 0.0 if w.get("resets_at") and w["resets_at"] <= now else float(w["used_percentage"])
    return max(0.0, 100 - used - extra - (w.get("hold") or 0))


def split(F: float, W: float | None, mine: dict | None, pay: bool = True, k: float | None = None) -> dict:
    """5 小时还剩 F、本周还剩 W（都已扣掉别人的预定；W 是 None 就是没查到本周的），这个人手里的预定 mine，
    k 是这个号 5 小时的 1% 折合本周百分之几（instance.five_in_week，不给就按实例的）：
    此刻能用的部分按 5 小时百分点拆成几段，顺序就是消耗的先后：ff 先用的 5 小时预定、wf 先用的周预定、
    g 绿的（大家共用，用了要付钱；pay 为假即付不起钱时是 0）、fb 保底的 5 小时预定、wb 保底的周预定。
    同一批里 5 小时的先用（它过期早）。周预定、绿的都受这个窗口共用的 5 小时空间限制。
    前端 quota.js 的 segments 是同一套算法，改一处要改两处。"""
    m, k = mine or _zero(), k or FIVE_IN_WEEK
    ff = min(m["ff"], F)
    fb = min(m["fb"], F - ff)
    room = F - ff - fb   # 这个窗口里大家共用的 5 小时空间
    free = (math.inf if W is None else max(0.0, W / k - m["lock"] / k)) if pay else 0.0
    wf_cap, wb_cap = m["wf"] / k, m["wb"] / k
    wf = min(wf_cap, room)
    g = min(free, room - wf)
    wb = min(wb_cap, room - wf - g)
    return {"ff": ff, "wf": wf, "g": g, "fb": fb, "wb": wb, "total": ff + wf + g + fb + wb,
            "weekBound": W is not None and wf_cap + free + wb_cap < room}


def blue_left(user: str | None, account: str, snap: dict, now: float) -> float:
    """这个人在这个号上此刻还有多少蓝（不管 5 小时空间够不够，只看手里还有没有）。"""
    h = holdings(account, snap, now).get(user) if user else None
    return h["ff"] + h["fb"] + h["wf"] + h["wb"] if h else 0.0


# ---------- 下一次请求要花多少 ----------

def _points(c: dict, table: dict) -> float:
    return rates.cost(table, c)


_paths: dict[str, Path] = {}


def _path_of(sid: str | None) -> Path | None:
    if not sid or sid.startswith("api-"):
        return None
    if sid not in _paths or not _paths[sid].exists():
        p = calls.find(sid)
        if not p:
            return None
        _paths[sid] = p
    return _paths[sid]


def spent_since(account: str, t: float, live: dict | None = None) -> float:
    """这个号在 t 以后的调用（所有人的）按单价估一共花了多少 5 小时百分点：读数是隔一阵才查一次的，预测拦截要把这段补上。
    live 是网页对话的驱动从流事件里当场记下的请求（回复编号 → {model, use}）：刚发出的那次 CLI 往往还没写进记录，靠它补上。"""
    table, by_sid = rates.load(), defaultdict(list)
    for _, mid, sid in api_calls.INDEX.since(account, t):
        by_sid[sid].append(mid)
    pts = 0.0
    for sid, mids in by_sid.items():
        p = _path_of(sid)
        got = api_usage.merge_for(sid, calls.session_calls(p) if p else {})
        pts += sum(_points(got[m] if m in got else live[m], table) for m in mids if m in got or m in (live or {}))
    return pts / pts_per_five(account)


def _base(model: str) -> str:
    """同一个模型的几种写法当成一个：去掉 [1m] 这类后缀，按时段计价的空闲标记（peak.OFF）也去掉。"""
    return (model or "").split("[")[0].removesuffix(peak.OFF)


def next_cost(path: Path | str | None, account: str, model: str | None, new_bytes: int = 0, images: int = 0,
              call_account: dict | None = None, last: dict | None = None, tool: bool = False,
              out_tokens: float | None = None) -> tuple[float, str]:
    """下一次请求大约要花多少 5 小时百分点，和怎么估的（给人看）。path 是这次请求所在的那份会话记录（主会话或子代理的），
    新会话是 None；last 是驱动从流事件里当场记下的上一次请求（{mid, model, use}），记录里还没写进去时靠它。
    上一次请求是同一个号、同一个模型发的、离现在不到 CACHE_TTL：旧的上下文算缓存命中，上一次的输出和新进来的内容算缓存写入；
    不是（换了号或模型，或者隔了一小时以上、缓存已经失效）：整段都算缓存写入。输出事先不知道，取这份记录最近 5 次请求的中位数。不留余量。
    tool 为真表示新进来的是一次工具结果，按 TOOL_TOKENS_MAX 封顶。out_tokens 给了就按它算输出（压缩上下文时输出的是一整份摘要）。"""
    cs = []
    if path:
        with calls._lock:   # file_calls 的缓存不是线程安全的，和 session_calls 用同一把锁
            got = dict(calls.file_calls(calls.Path(path)))
        cs = sorted(got.items(), key=lambda kv: api_calls.stamp(kv[1]["ts"]) if kv[1]["ts"] else 0)
    if last and last.get("mid") and all(m != last["mid"] for m, _ in cs):
        cs.append((last["mid"], {"model": last["model"], "ts": None, "use": last["use"]}))
    new = new_bytes / BYTES_PER_TOKEN
    if tool:
        new = min(new, TOOL_TOKENS_MAX)
    new += images * IMAGE_TOKENS
    if cs:
        mid, last = cs[-1]
        u = last["use"]
        ctx = u["in"] + u["cw"] + u["cr"]
        prev = (call_account or {}).get(mid) or api_calls.INDEX.account(mid)
        # 驱动当场记下的那次没有时间（ts 是 None），是刚刚发的，缓存不会失效
        # DeepSeek 的缓存不按一小时失效（官方：不再使用后一般几个小时到几天才清空，没有准的时限），不算失效
        expired = bool(last["ts"]) and time.time() - api_calls.stamp(last["ts"]) > CACHE_TTL \
            and model_family(last["model"]) != "deepseek"
        same = prev == account and (not model or _base(model) == _base(last["model"]))
        cached = same and not expired
        cw = u["out"] + new + (0 if cached else ctx)
        cr = ctx if cached else 0
        out = statistics.median(c["use"]["out"] for _, c in cs[-5:])
        use_model = model or last["model"]
        how = f"上下文 {ctx / 1000:.0f}k，" + ("接着上一次的缓存" if cached else "换了号或模型，整段要重新写入缓存" if not same
                                             else "离上一次请求超过一小时，缓存已失效，整段要重新写入")
    else:
        cw, cr, out, use_model = SYSTEM_GUESS + new, 0, 0, model or ""
        how = "会话的第一次请求"
    if out_tokens is not None:
        out = out_tokens
    table = rates.load()
    r = rates.rate(table, use_model) or max(table.values(), key=lambda x: x["cw"], default=None)
    if not r:
        return 0.0, how + "，没有单价，估不出来"
    return (cw * r["cw"] + cr * r["cr"] + out * r["out"]) / 1e6 / pts_per_five(account), how


# ---------- 调用记在哪一批上 ----------

_order_pos = 0
_pending: dict[str, tuple[float, str, str | None]] = {}   # 回复编号 → (时间, 号, 会话编号)：token 数还没写进记录，等一会儿再分


def process(owner_of, green_left):
    """后台循环隔几秒调一次：新出现的调用，会话主人在这个号上有预定的，决定由哪几批的蓝来付，记进 reserve_use.jsonl。
    先用的那几批（front）先付；绿的用完了、或者付不起钱（green_left(主人, 号) ≤ 0）才动保底的那几批。
    分三段：会话主人是谁、这次调用用了多少（要解析会话文件，大会话要好几秒；配了 run_as 时找主人还是跨用户调用）和预定的数据无关，
    放在中间那段不拿锁做；以前整段拿着 _lock，发话前核对余额（check_paid → holdings）、开预定面板都在线程里干等它。
    只有这里往 _pending 里放、往外拿，后台循环一次只跑一个 process，所以两次拿锁之间 _pending 不会被别人动。"""
    global _order_pos
    idx = api_calls.INDEX
    order = idx.order
    with _lock:
        for ts, mid, acc, sid in order[_order_pos:]:
            if mid not in uses:
                _pending[mid] = (ts, acc, sid)
        _order_pos = len(order)
        if not _pending:
            return
        todo = sorted(_pending.items(), key=lambda kv: kv[1][0])
        booked = {(b["user"], b["account"]) for b in batches}
    now, looked = time.time(), {}
    # 同一个会话的用量这一趟只读一次，它的各次调用共用：每次调用都把整个会话的调用表重读一遍、
    # 跨用户 stat 一遍的话，重启后 _pending 是全部历史调用，几千个会话的实例上这一步一趟要近十分钟。这一趟里会话又写了新调用的话，
    # 新调用在缓存里查不到，和以前一样当成还没读到用量（没过 GIVE_UP 的下一趟再查，见下面拿锁那段）
    per_sid: dict[str, dict] = {}
    for mid, (ts, acc, sid) in todo:   # 不拿锁
        user = owner_of(sid) if sid else None
        c = None
        if user and (user, acc) in booked and (api_usage.ready(mid) or now - ts > GIVE_UP):
            if sid not in per_sid:
                p = _path_of(sid)
                per_sid[sid] = api_usage.billable_for(sid, calls.session_calls(p) if p else {})
            c = per_sid[sid].get(mid)
        looked[mid] = (user, c)
    with _lock:
        table, done = rates.load(), []
        for mid, (ts, acc, sid) in todo:
            user, c = looked[mid]
            mine = [b for b in batches if b["user"] == user and b["account"] == acc and b["ts"] <= ts] if user else []
            if not mine:   # 会话主人在这个号上没有当时就有的预定：照常付钱，不用记
                done.append(mid)
                continue
            if (user, acc) not in booked:
                continue   # 不会有：当时就有的那批，第一次拿锁时就在 batches 里了。真碰上就下一圈再查用量，不拿没查过的结算
            if not api_usage.ready(mid) and now - ts <= GIVE_UP:
                continue   # API流还未结束，不拿半截用量结算预定
            if not c:
                if now - ts > GIVE_UP:
                    done.append(mid)
                continue
            _settle(mid, ts, user, acc, sid, c, mine, table, green_left)
            done.append(mid)
        for m in done:
            _pending.pop(m, None)


def _settle(mid, ts, user, acc, sid, c, mine, table, green_left):
    # 特惠（promo_factor）：免掉的那几成要有绿的（大家共用的额度）来出、不碰预定，进账本的那几成才由预定付；
    # 绿的不够就少免，用完了（剩下的全是自己的预定）就不免，整次由预定付
    k, full, free = promo_factor(acc, ts), _points(c, table), 0.0
    if k < 1.0 and full > 0:
        free = min(1.0 - k, max(0.0, green_left(user, acc)) * pts_per_five(acc) / full)
    pts, parts = full * (1 - free), []
    if pts > 0:
        c5 = pts / pts_per_five(acc)
        cw = c5 * five_in_week(acc)
        win, need = window_at(acc, ts), 1.0

        def slot_of(b):
            for s in b["five"]:
                if (abs(s["start"] - win) < SAME) if win is not None else (s["start"] - 600 <= ts < s["start"] + FIVE):
                    return s
            return None

        def take(b, kind):
            nonlocal need
            if need <= 1e-9:
                return
            if kind == "five":
                s = slot_of(b)
                if not s:
                    return
                frac = min(need, max(0.0, s["pct"] - used5[(b["id"], s["start"])]) / c5)
                part = {"batch": b["id"], "kind": "five", "start": s["start"], "five": frac * c5}
            else:
                slot = next((s for s in week_slots(b) if _active(s[1], s[2], ts)), None)
                if not slot:
                    return
                key = slot[0]
                frac = min(need, max(0.0, slot[3] - usedW[key]) / cw)
                part = {"batch": b["id"], "kind": "week", "week": frac * cw, **({"until": slot[2]} if slot[1] is not None else {})}
            if frac > 0:
                part["points"] = frac * pts
                parts.append(part)
                need -= frac

        front, back = [b for b in mine if b["front"]], [b for b in mine if not b["front"]]
        for kind in ("five", "week"):
            for b in front:
                take(b, kind)
        if need > 1e-9 and back and green_left(user, acc) <= 0:
            for kind in ("five", "week"):
                for b in back:
                    take(b, kind)
    rec = {"mid": mid, "ts": ts, "user": user, "account": acc, "sid": sid, "points": pts, "parts": parts,
           **({"promo": k, "free": free} if k != 1.0 else {})}   # 记下折扣和实际免了几成：读进来时就知道已经折过（_promo_legacy、free_of）
    _append(USE_FILE, rec)
    _apply(rec)


def frac(mid: str) -> float:
    """这次调用有几成由预定付了（0～1）。账本按它从花费里减（users.ledger），气泡和会话总花费按它把那一份写成蓝的。
    记比例不记点数：单价重新拟合后，花费按新单价重算，减掉的也跟着按新单价算，两边才对得上。"""
    u = uses.get(mid)   # 不拿锁，见 uses
    if not u or not u["points"] or not u["parts"]:
        return 0.0
    return min(1.0, sum(p["points"] for p in u["parts"]) / u["points"])


def shares(items) -> list[tuple[float, float]]:
    """一批调用 [(回复编号, 号, 时刻)] 各自的 (free_of, frac)，结果和逐次调这两个一样，只是整批只拿一次锁：
    账本拆一个会话要对每次调用各问一遍，以前每问一次拿一次锁，重启后几十个线程一起算账，十几万次抢锁，大半时间在排队。
    整批在同一把锁里读，读到的也是同一刻的预定和特惠（逐次拿锁时，中间可能插进一次结算）。"""
    out = []
    with _lock:
        for mid, account, t in items:
            u = uses.get(mid)
            free = u["free"] if u is not None and "free" in u else 1.0 - promo_factor(account, t)
            fr = 0.0 if not u or not u["points"] or not u["parts"] else min(1.0, sum(p["points"] for p in u["parts"]) / u["points"])
            out.append((free, fr))
    return out


def paid_batches(user: str) -> list[dict]:
    """这个人下过的每一批，按下单先后；撤销了的也在（带 cancel），账本按 charged 收钱。"""
    with _lock:
        return sorted((b for b in batches + dropped if b["user"] == user), key=lambda b: b["ts"])


def sold(account: str) -> list[dict]:
    """在这个号上下过的每一批，不管是谁下的（撤销了的也在，收钱按 charged）：号主的收入里有这份（users.earnings）。"""
    with _lock:
        return [b for b in batches + dropped if b["account"] == account]


def bought_since(since: float, price=lambda account: 1.0) -> dict[str, float]:
    """这个时刻以后各人下单付了多少（按单价算的点数，每批乘 price(号)，收钱比例按号不同所以由调用的地方给）：
    余额看板的「最近 N 分钟」把它算作花掉的。撤销了的只算没退的那部分。"""
    out: defaultdict = defaultdict(float)
    with _lock:
        for b in batches + dropped:
            if b["ts"] >= since and b["points"]:
                out[b["user"]] += charged(b) * price(b["account"])
    return dict(out)


# ---------- 下单 ----------

def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")


def plan(account: str, snap: dict, user: str, now: float, est5: float = 0.0, estW: float = 0.0) -> dict:
    """这个人此刻在这个号上能订什么：从当前窗口起、本周重置之前的每个 5 小时窗口（开始时刻按「每次重置立刻开下一个」推），
    各自最多还能订多少；本周还有多少没被任何人占（周预定和所有 5 小时预定折成的周额度加起来不能超过它）。
    当前窗口最多订这个人此刻的绿（和别人共用、还没被占的那部分）；以后的窗口最多订 100% 减去这个窗口已经被订的。
    est5 / estW 是读数之后估出来的消耗（和预测拦截一样补上）。出问题抛 ValueError，原因写给人看。
    weeks 是本周以后的几周（连本周最多 WEEKS 周），各自最多订 100% 减去那一周已经被订的。
    号到期（EXPIRES）以后才开始的周和窗口不列出来；当前窗口照样列；到期前开始、到期后才重置的那一周只算到到期时刻。
    没有 5 小时限额的号（WEEK_ONLY）没有窗口可订，只能周预定。不开放预定的号（NO_RESERVE）在这里拦下，下单也经过这里。"""
    if account in NO_RESERVE:
        raise ValueError(f"{account} 不开放预定")
    five, week = snap.get("five_hour"), snap.get("seven_day")
    week_only = account in WEEK_ONLY
    if not week_only and (not five or five.get("used_percentage") is None):
        raise ValueError(f"还没查到 {account} 的 5 小时额度，先在账号面板里刷新额度")
    if not week or week.get("used_percentage") is None or not week.get("resets_at"):
        raise ValueError(f"还没查到 {account} 本周额度和它的重置时刻，先在账号面板里刷新额度")
    if week["resets_at"] <= now:
        raise ValueError(f"{account} 的本周额度已经过了重置时刻、还没查到新的，先刷新额度")
    RW = week_end(account, week["resets_at"])   # 订阅在本周重置之前就到期的，本周到到期为止，以后的周也就没有了
    if RW <= now:
        raise ValueError(f"{account} 已经到期，不能再预定")
    hs = holdings(account, snap, now)
    mine = hs.get(user) or _zero()
    others = [h for u, h in hs.items() if u != user]
    F = math.inf if week_only else rest({**five, "hold": sum(h["ff"] + h["fb"] for h in others)}, now, est5)
    W = rest({**week, "hold": sum(h["lock"] for h in others)}, now, estW)
    k = five_in_week(account)
    sp = split(F, W, mine, k=k)
    week_free = max(0.0, W - mine["lock"])   # 本周还没被任何人占的（周百分点）
    cur = current_start(snap, now)
    t, wins, weeks, exp = cur if cur is not None else now, [], [], EXPIRES.get(account)
    with _lock:
        while t < RW and not week_only and (not wins or exp is None or t < exp):
            booked = mine_pct = 0.0
            for b in batches:
                if b["account"] == account:
                    for s in b["five"]:
                        if abs(s["start"] - t) < SAME:
                            booked += s["pct"]
                            mine_pct += s["pct"] if b["user"] == user else 0
            top = sp["g"] if not wins else max(0.0, 100 - booked)
            wins.append({"start": t, "end": t + FIVE, "now": not wins, "started": cur is not None if not wins else False,
                         "max": max(0.0, min(top, week_free / k)), "mine": mine_pct})
            t += FIVE
        for i in range(1, WEEKS):
            start, until = RW + (i - 1) * WEEK, RW + i * WEEK
            if exp is not None and start >= exp:
                break
            if exp is not None:
                until = min(until, exp)   # 到期前开始、到期后才重置的那一周，到期就没了，只算到到期时刻
            booked = mine_pct = 0.0
            for b in batches:
                if b["account"] == account:
                    for w in b.get("weeks") or ():
                        # 按开始时刻认是哪一周：各次下单时推的时刻会差一两分钟；最后一周截到到期时刻以后，重置时刻就对不上以前订的了
                        if abs(w["start"] - start) < WEEK / 2:
                            booked += w["pct"]
                            mine_pct += w["pct"] if b["user"] == user else 0
            weeks.append({"start": start, "end": until, "max": max(0.0, 100 - booked), "mine": mine_pct})
    return {"windows": wins, "weeks": weeks, "weekMax": week_free, "weekReset": RW, "weekExpires": RW < week["resets_at"],
            "split": sp, "five": None if week_only else F, "week": W,
            "lock": mine["lock"], "k": k, "ptsPerFive": pts_per_five(account), "weekOnly": week_only}


def _number(v, what: str) -> float:
    """下单填的数：只收有限的数。nan 和谁比都是假，会一路混过上限、余额这些检查，把账本变成 nan，所以当场拒绝。"""
    try:
        x = float(v or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{what}要填一个数") from None
    if not math.isfinite(x):
        raise ValueError(f"{what}要填一个数")
    return x


def book(account: str, snap: dict, user: str, now: float, five: list[dict], week: float, front: bool,
         est5: float, estW: float, charge: bool, check_money, weeks: list[dict] = ()) -> dict:
    """下单：重新按 plan 核一遍（填的每个窗口、以后的每一周不超过它的上限，这一批折成的本周额度不超过本周没被占的），
    算好价钱（限量号才收：订多少就收正常用掉这么多要花的点数），check_money(点数) 不让付就抛 ValueError，然后记下这一批。
    weeks 是以后各周的周预定：[{start 那一周的开始时刻（面板给的）, pct 订那一周的百分之几}]。"""
    week_pct = max(0.0, _number(week, "周预定"))   # 填的数先核，别的都在这之后
    rows = [(_number(r.get("pct"), "5 小时预定"), r.get("start")) for r in five]
    rows = [(pct, _number(start, "窗口的开始时刻")) for pct, start in rows if pct > 0]
    later = [(_number(r.get("pct"), "以后的周预定"), r.get("start")) for r in weeks]
    later = [(pct, _number(start, "那一周的开始时刻")) for pct, start in later if pct > 0]
    with _lock:
        p = plan(account, snap, user, now, est5, estW)
        chosen, need_week = [], week_pct
        for pct, start in rows:
            w = next((w for w in p["windows"] if abs(w["start"] - start) < SAME), None)
            if not w:
                raise ValueError(f"{clock(start)} 开始的窗口不在本周能订的范围里了，重新打开面板再订")
            if pct > w["max"] + 1e-6:
                raise ValueError(f"{clock(w['start'])} 开始的窗口最多还能订 {w['max']:.1f}%")
            chosen.append({"start": w["start"], "pct": round(pct, 3)})
            need_week += pct * p["k"]
        chosen_weeks = []
        for pct, start in later:
            w = next((w for w in p["weeks"] if abs(w["start"] - start) < WEEK / 2), None)
            if not w:
                raise ValueError(f"{clock(start)} 开始的那一周不在能订的范围里了，重新打开面板再订")
            if pct > w["max"] + 1e-6:
                raise ValueError(f"{clock(w['start'])} 开始的那一周最多还能订 {w['max']:.2f}%")
            chosen_weeks.append({"start": w["start"], "until": w["end"], "pct": round(pct, 3)})
        if not chosen and need_week <= 0 and not chosen_weeks:
            raise ValueError("没填要订多少")
        if need_week > p["weekMax"] + 1e-6:
            raise ValueError(f"本周额度不够：这一批折合本周 {need_week:.2f}%，本周只剩 {p['weekMax']:.2f}% 没被占")
        # 预定按原价收，不受限时特惠影响（老版本时段里下的单按折扣收，那时的批次里记着 promo）
        all_week = week_pct + sum(c["pct"] for c in chosen_weeks)
        cost = (sum(c["pct"] for c in chosen) + all_week / p["k"]) * p["ptsPerFive"] if charge else 0.0
        check_money(cost)
        b = {"id": uuid.uuid4().hex[:10], "ts": now, "user": user, "account": account, "front": bool(front),
             "five": chosen, "week": {"pct": round(week_pct, 3), "until": p["weekReset"]} if week_pct > 0 else None,
             **({"weeks": chosen_weeks} if chosen_weeks else {}),
             "points": round(cost, 4), "k": p["k"], "ptsPerFive": p["ptsPerFive"]}
        _append(FILE, b)
        batches.append(b)
        return b


def mine_list(account: str, user: str | None, now: float) -> list[dict]:
    """这个人在这个号上的每一批预定和各自用掉了多少，最新的在前（面板里列给自己看）。
    user 是 None 就列所有人的（查看器账号看，好挑出要撤销的）；撤销了的也列出来，带上 cancel（谁撤的、退了多少点）。"""
    with _lock:
        out = []
        for b in sorted(batches + dropped, key=lambda b: b["ts"]):
            if b["account"] != account or (user is not None and b["user"] != user):
                continue
            w = b.get("week")
            out.append({"id": b["id"], "ts": b["ts"], "user": b["user"], "front": b["front"], "points": b["points"],
                        "five": [{**s, "used": used5[(b["id"], s["start"])]} for s in b["five"]],
                        "week": {**w, "used": usedW[b["id"]]} if w else None,
                        "weeks": [{**x, "used": usedW[(b["id"], x["until"])]} for x in b.get("weeks") or ()],
                        "cancel": b.get("cancel")})
    return out[::-1]


def keeper_status(now: float) -> dict:
    """开窗服务（keeper.py）的心跳：多久前还活着、最近一次出了什么错。读不到就说读不到。"""
    try:
        st = json.loads(KEEPER_STATE.read_text())
    except FileNotFoundError:
        return {"alive": False, "why": "预定服务从未运行，无法保证后续窗口按时开启"}
    except (OSError, ValueError) as e:
        return {"alive": False, "why": f"无法读取预定服务状态：{e}"}
    age = now - (st.get("ts") or 0)
    return {"alive": age < 120, "age": age, "error": st.get("error") or "",
            "why": "" if age < 120 else f"开窗服务 {age / 60:.0f} 分钟没动静了，以后的窗口可能不会准时开"}
