"""账号页上的几张图（前端 js/stats.js）：余额最近一个月的曲线、号主看自己名下的号、用量统计页。

都是只读的：用账本已经拆好、缓存好的调用（users._split_rows、users._taken），不另算单价、不改账本。
余额曲线把账本里的每一笔按时刻排开累加，最后一点和账本的余额是同一个数（对不上就写日志）：
  + 收到的额度（grants.jsonl 一笔笔、额度计划到期的各期）
  − 名下会话每次调用照常扣的钱（进账本的减去预定付掉的，同 _ledger_now 的 spent）
  − 下预定付的钱（下单那一刻，撤销了的只算没退的）
  + 号主收入（他的号上每次调用照常扣的钱，同 users.earnings；别人在他号上下的预定，下单那一刻）
用量统计按「这段时间里发的每次调用」算，免费号也算（同 users.recent_by_owner），花费写两种：按单价的全价和实际扣的钱。
点数只在这里和接口里出现，前端一律换成元或分。
"""
import math
import statistics
import threading
import time
from collections import defaultdict

from fastapi import HTTPException

import api_calls
import api_usage
import calls
import rates
import reserve
import users
from instance import ADMINS, OWNERS

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}
TTL = 60   # 同一份结果一分钟内沿用：几张图一起打开、来回切范围时不重算


def _cached(key, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def _charge(table, r) -> float:
    """一次调用照常扣了多少（点）：进账本的那份减去预定付掉的，同 users._taken"""
    return rates.cost(table, r[2]) - (rates.cost(table, r[3]) if r[3] else 0.0) if r[2] else 0.0


def _own_rows(name: str, snap) -> list[tuple[str, tuple]]:
    """名下会话（连外部接口的会话）逐次调用拆好的行，和 users._ledger_now 同一份会话名单、同一套拆法"""
    with users._lock:
        sids = [sid for sid, rec in users.owners.items() if rec["owner"] == name] if users.PAID else []
    out = []
    for sid in sids:
        try:
            path = calls.find_in(snap, sid)
        except (OSError, ValueError) as e:
            print(f"余额曲线：{name} 的会话 {sid} 读不了：{e}", flush=True)
            path = None
        if path:
            out += [(sid, r) for r in users._split_rows(path, snap)[0]]
    for sid in api_usage.external_sessions(name):
        out += [(sid, r) for r in users.api_rows(sid)]
    return out


def _earning_events(name: str, snap, table) -> list[tuple[float, float]]:
    """号主收入一笔笔的时刻和点数，同 users.earnings（会话文件找不到的那几次它也不算，这里同样不算）"""
    mine = {acc: since for acc, (user, since) in OWNERS.items() if user == name}
    if not mine:
        return []
    old = time.time() - users.SETTLE
    by_sid, last = defaultdict(list), {}
    for ts, mid, acc, sid in list(api_calls.INDEX.order):
        last[sid] = ts
        if acc in mine and ts >= mine[acc]:
            by_sid[sid].append((ts, mid))
    out = []
    for sid, got in by_sid.items():
        took = users._taken(sid, table, snap, last[sid] < old) if sid else None
        if took:
            out += [(ts, took.get(mid, 0.0)) for ts, mid in got]
    for acc, since in mine.items():
        out += [(b["ts"], reserve.charged(b) * rates.price_of(acc)) for b in reserve.sold(acc) if b["ts"] >= since]
    return out


def balance_series(name: str, days: float = 30, step: float = 3600) -> dict:
    """余额最近 days 天的曲线：每 step 秒一个点，最后一点是此刻。points 是 [时刻, 余额（点）]"""
    def run():
        users.refresh_calls()
        snap = users.all_sessions_snapshot()
        table = rates.load(price=True)
        ev = [(g["ts"], g["amount"]) for g in users.ledger(name, snap)["grants"]]   # 直接发的连同额度计划到期的各期
        ev += [(r[0] or 0.0, -_charge(table, r)) for _, r in _own_rows(name, snap) if r[2]]
        ev += [(b["ts"], -reserve.charged(b) * rates.price_of(b["account"])) for b in reserve.paid_batches(name)]
        ev += _earning_events(name, snap, table)
        ev.sort(key=lambda e: e[0])
        now = time.time()
        t0 = now - days * 86400
        n = int(days * 86400 // step)
        ts = [t0 + k * step for k in range(n)] + [now]
        out, acc, i = [], 0.0, 0
        for t in ts:
            while i < len(ev) and ev[i][0] <= t:
                acc += ev[i][1]
                i += 1
            out.append([round(t), round(acc, 4)])
        bal = users.ledger(name, snap)["balance"]
        if abs(bal - acc) > 0.01:
            print(f"余额曲线：{name} 累加到此刻是 {acc:.4f} 点，账本余额是 {bal:.4f} 点，对不上", flush=True)
        return {"points": out, "step": step, "balance": bal}
    return _cached(("balance", name, days, step), run)


def owned(name: str) -> list[dict]:
    """号主看自己名下的号：每个号从起算时刻以后，自己照常扣了多少、别人照常扣了多少（连同下预定付的），有几个别人用过，
    多少次调用找不到会话、没算进来（同 users.earnings 的 unearned）。都是点"""
    def run():
        users.refresh_calls()
        snap = users.all_sessions_snapshot()
        mine = {acc: since for acc, (user, since) in OWNERS.items() if user == name}
        table, old = rates.load(price=True), time.time() - users.SETTLE
        ext = api_usage.external_sessions()
        by, last, at = defaultdict(list), {}, {}
        for ts, mid, acc, sid in list(api_calls.INDEX.order):
            last[sid] = ts
            if acc in mine and ts >= mine[acc]:
                by[(acc, sid)].append(mid)
                at[acc] = ts   # 调用记录按时间排，最后记下的就是这个号最近一次调用
        out = {acc: {"account": acc, "since": since or None, "self": 0.0, "others": 0.0, "users": set(), "calls": 0,
                     "missing": 0, "last": at.get(acc)} for acc, since in mine.items()}
        for (acc, sid), mids in by.items():
            o = out[acc]
            o["calls"] += len(mids)
            took = users._taken(sid, table, snap, last[sid] < old) if sid else None
            if took is None:
                o["missing"] += len(mids)
                continue
            payer = ext.get(sid) if sid in ext else users.owner_of(sid)
            amt = sum(took.get(m, 0.0) for m in mids)
            if payer == name:
                o["self"] += amt
            else:
                o["others"] += amt
                if amt:
                    o["users"].add(payer)
        for acc, since in mine.items():
            for b in reserve.sold(acc):
                if b["ts"] >= since:
                    amt = reserve.charged(b) * rates.price_of(acc)
                    if b["user"] == name:
                        out[acc]["self"] += amt
                    else:
                        out[acc]["others"] += amt
                        out[acc]["users"].add(b["user"])
        # 同站第二把令牌（present_as 并号）的收入并到主号那张卡上一起看，不单列成两家
        for child, parent in list(instance.PRESENT_AS.items()):
            if child in out and parent in out:
                c, p = out[child], out[parent]
                p["self"], p["others"] = p["self"] + c["self"], p["others"] + c["others"]
                p["calls"], p["missing"] = p["calls"] + c["calls"], p["missing"] + c["missing"]
                p["users"] |= c["users"]
                p["last"] = max(p["last"] or 0, c["last"] or 0) or None
                del out[child]
        return [{**o, "users": len(o["users"]), "self": round(o["self"], 4), "others": round(o["others"], 4)}
                for o in out.values()]
    return _cached(("owned", name), run)


# ---------- 用量统计 ----------

def _window_calls(since: float) -> list[tuple]:
    """这个时刻以后发的每次调用：(时刻, 会话主人, 会话编号, 号, 模型, use, 全价点数, 实扣点数)。免费号也算"""
    def run():
        users.refresh_calls()
        snap = users.all_sessions_snapshot()
        table = rates.load(price=True)
        import isolation
        out = []
        def add(owner, sid, rows):
            for r in rows:
                if r[0] is None or r[0] < since:
                    continue
                c = r[1]
                full = rates.cost(table, c) if rates.rate(table, c["model"]) else 0.0
                out.append((r[0], owner, sid, api_calls.INDEX.account(r[5]), c["model"], c["use"], full, _charge(table, r)))
        for root in isolation.roots(internal=True):
            for p in users._main_files(root, snap):
                if users.touched_since(calls.Path(p), since, snap):
                    add(users.owner_of(p.stem), p.stem, users._split_rows(p, snap)[0])
        for sid, owner in api_usage.external_sessions().items():
            add(owner, sid, users.api_rows(sid, since))
        out.sort(key=lambda x: x[0])
        return out
    return _cached(("calls", round(since / 60)), run)


TOK = ("in", "cw", "cr", "out")
BINS = [0] + [round(10 ** (k / 4)) for k in range(4, 29)]   # 一次调用的 token 数分箱：0，10 起每档 ×1.78，到一千万


def _hist(vals: list[float]) -> list[int]:
    h = [0] * len(BINS)
    for v in vals:
        k = 0
        while k + 1 < len(BINS) and v >= BINS[k + 1]:
            k += 1
        h[k] += 1
    return h


def _quart(vals: list[float]) -> list[float] | None:
    if not vals:
        return None
    if len(vals) == 1:
        return [vals[0]] * 3
    q = statistics.quantiles(vals, n=4)
    return [round(q[0], 1), round(statistics.median(vals), 1), round(q[2], 1)]


def _profile(rows: list[tuple]) -> dict:
    """一群调用的画像：典型的一次请求（四种 token 和花费的四分位）、三种 token 的分布、按模型分、缓存命中占比"""
    if not rows:
        return {"n": 0}
    model = defaultdict(lambda: {"n": 0, "full": 0.0, "paid": 0.0, **dict.fromkeys(TOK, 0)})
    for r in rows:
        m = model[(r[4] or "?").split("@")[0]]   # 计费用的后缀（@account=api2、@offpeak）并回同一个模型
        m["n"] += 1
        m["full"] += r[6]
        m["paid"] += r[7]
        for k in TOK:
            m[k] += r[5].get(k, 0)
    tot = {k: sum(r[5].get(k, 0) for r in rows) for k in TOK}
    inp = tot["in"] + tot["cw"] + tot["cr"]
    return {"n": len(rows), "full": round(sum(r[6] for r in rows), 4), "paid": round(sum(r[7] for r in rows), 4),
            "tokens": tot, "hit": round(tot["cr"] / inp, 4) if inp else None,
            "typical": {k: _quart([r[5].get(k, 0) for r in rows]) for k in TOK} | {"cost": _quart([r[6] for r in rows])},
            "hist": {k: _hist([r[5].get(k, 0) for r in rows]) for k in ("cw", "cr", "out")},
            "models": sorted(({"model": k, **{kk: (round(v, 4) if isinstance(v, float) else v) for kk, v in m.items()}}
                              for k, m in model.items()), key=lambda m: -m["full"])}


def _series(rows: list[tuple], t0: float, n: int, step: float) -> dict:
    """按 step 分桶的时间序列：每桶全价、实扣、调用次数、四种 token"""
    s = {k: [0.0] * n for k in ("full", "paid", "n", *TOK)}
    for r in rows:
        k = int((r[0] - t0) // step)
        if 0 <= k < n:
            s["full"][k] += r[6]
            s["paid"][k] += r[7]
            s["n"][k] += 1
            for kk in TOK:
                s[kk][k] += r[5].get(kk, 0)
    return {k: [round(v, 4) for v in vs] for k, vs in s.items()}


def _sessions(rows: list[tuple], titled: bool) -> dict:
    """按会话汇总：会话数、每个会话调用次数和花费的四分位、每个会话的全价花费（从小到大，前端画分布）；
    titled 时给花得最多的十个会话的编号（前端自己找名字）"""
    by = defaultdict(lambda: [0, 0.0, 0.0, 0, 0])   # 次数、全价、实扣、最早、最晚
    for r in rows:
        b = by[r[2]]
        b[0] += 1
        b[1] += r[6]
        b[2] += r[7]
        b[3] = b[3] or r[0]
        b[4] = r[0]
    costs = sorted(b[1] for b in by.values())
    out = {"count": len(by), "callsPer": _quart([b[0] for b in by.values()]), "costPer": _quart(costs),
           "costs": [round(c, 4) for c in costs]}
    if titled:
        out["top"] = [{"sid": sid, "n": b[0], "full": round(b[1], 4), "paid": round(b[2], 4), "first": b[3], "last": b[4]}
                      for sid, b in sorted(by.items(), key=lambda x: -x[1][1])[:10]]
    return out


def usage(me, days: float = 3, who: str | None = None) -> dict:
    """用量统计页：days 天里，「看的人」（who：自己、all 全部人、或者查看器账号指定的某个人）和其余所有人的对照。
    时间序列按小时一桶（30 天也是一小时，720 个点），按一天里的钟点叠起来的作息另给一份；
    查看器账号多一份每人的合计。点数都是点，前端换成元"""
    if not me.name:
        raise HTTPException(401, "请登录后查看用量统计")
    who = who or me.name
    if who not in ("all", me.name) and not me.admin:
        raise HTTPException(403, "只有查看器账号可以查看其他用户的用量")
    days = max(1 / 24, min(float(days), 31))
    now = time.time()
    step = 3600
    n = math.ceil(days * 86400 / step)
    t0 = now - n * step
    rows = _window_calls(t0)
    mine = rows if who == "all" else [r for r in rows if r[1] == who]
    others = [] if who == "all" else [r for r in rows if r[1] != who]
    clock = lambda rs: [sum(r[6] for r in rs if time.localtime(r[0]).tm_hour == h) for h in range(24)]
    accts = defaultdict(lambda: [0, 0.0])
    for r in mine:
        a = accts[r[3] or "?"]
        a[0] += 1
        a[1] += r[6]
    out = {"who": who, "days": days, "t0": t0, "step": step, "now": now, "bins": BINS,
           "series": _series(mine, t0, n, step), "pool": _series(rows, t0, n, step),
           "profile": _profile(mine), "others": _profile(others), "all": _profile(rows),
           "clock": [round(v, 4) for v in clock(mine)], "clockAll": [round(v, 4) for v in clock(rows)],
           "sessions": _sessions(mine, titled=who == me.name or me.admin),
           "othersSessions": _sessions(others, titled=False),
           "accounts": sorted(({"account": a, "n": v[0], "full": round(v[1], 4)} for a, v in accts.items()), key=lambda x: -x["full"]),
           "people": len({r[1] for r in rows})}
    if me.admin:
        per = defaultdict(lambda: [0, 0.0, 0.0])
        for r in rows:
            p = per[r[1] or ""]
            p[0] += 1
            p[1] += r[6]
            p[2] += r[7]
        out["users"] = sorted(({"name": k or None, "n": v[0], "full": round(v[1], 4), "paid": round(v[2], 4)}
                               for k, v in per.items()), key=lambda x: -x["full"])
        out["names"] = [n for n in users.names() if n not in ADMINS or n == me.name]
    return out
