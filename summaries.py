"""会话摘要：在后台从主会话分叉出去，一次调用写出约 1000 字的长摘要、约 100 字的短摘要和一个名字，存在这里。

规矩：
- 不进主会话的上下文：每次都从主会话分叉（fork_session），不接着上一次的分叉，提示里也不提以前的摘要，第 n 次不知道之前触发过。
- 只在大概率命中缓存时做：主会话上一次 API 调用结束后正好 IDLE（45 分钟）触发，最晚到 LATE（46 分钟），过了就等下次；
  号、模型、思考强度、权限模式、系统提示词都和主会话一样（server.summary_run），没切号、没换模型才做（server.summary_candidates）。
  命中缓存还顺带把主会话一小时的缓存期限重新算起，相当于保温。后台任务、约好的叫醒都不管。
- 可以重复触发：和上次触发时比，上下文多了超过 STEP_CTX（100K）、主会话的 API 调用多了超过 STEP_CALLS（10）次；用户说话不做要求。
  第一次触发就是和 0 比。
- 名字照起名的规矩：AI 起的可以换，用户起的不覆盖（naming.propose）。
- Claude 订阅号、GPT 号、DeepSeek 号都做（FAMILIES）；独立环境暂时弃用，不做。
- 默认给所有用户做（USERS 为 None）；要收窄就写一组网页账号名。
每次分叉都记下用量，缓存命中的比例低于 MIN_HIT 连着 BAD_LIMIT 次，这一家的号就停做（只在内存里记，重启后重新开始），在服务输出里说明。
文件在数据目录，只给查看器自己读写；读不出来（不是没有）就整个停用、说清楚，不当成空的：那样会把已经做过的会话当成没做过、重复花钱。
"""
import json
import re
import threading
import time

import diskq
from instance import DATA

FILE = DATA / "session_summaries.json"
IDLE, LATE = 45 * 60, 46 * 60   # 上一次 API 调用结束后多少秒触发、最晚到多少秒
STEP_CTX, STEP_CALLS = 100_000, 10
USERS: set[str] | None = None   # 只给哪些网页账号做；None 是所有用户
FAMILIES = ("claude", "gpt", "deepseek")   # instance.account_family 的说法；中转站、智谱这些先不做
AT_ONCE = 2           # 同时最多分叉几份
TIMEOUT = 900         # 一份最多跑多久（秒）
MIN_HIT, BAD_LIMIT = 0.8, 3
LONG_MAX, SHORT_MAX = 3000, 400   # 解析时的上限（字），模型写得太长就算没写好，不截断

PROMPT = """【写会话摘要】这是查看器在后台给这个会话写摘要，用户看不到这条消息，也不会看到你这次回复的原文，只会看到下面解析出来的三样东西。
不要调用任何工具，也不要接着做手上的事，只根据上面的对话，用平实的中文按第三人称来写，不用角色口吻。按下面的格式一次写完：

<长摘要>
约 1000 字：这个会话要做什么（用户的目标和要求），做到了哪一步，做了哪些事、得出了哪些结论，现在的状态，还没解决的问题和下一步。
文件、机器、数字照实写；没做过、没核实的事不要写成做过、核实过。
</长摘要>
<短摘要>
约 100 字：一两句话说清这个会话在做什么、现在到了哪一步。
</短摘要>
<名字>
给这个会话起个名字，写清在做什么，不超过 6 个汉字（字母、数字和半角符号各算半个）。
</名字>"""

_lock = threading.Lock()
broken = ""
bad: dict[str, int] = {}   # 这一家的号连着几次没命中缓存


def _load() -> dict:
    global broken
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        d = {}
    except (OSError, ValueError) as e:
        broken = f"会话摘要的记录 {FILE} 读不出来（{type(e).__name__}: {e}），会话摘要先停用"
        print(broken, flush=True)
        d = {}
    return {"sessions": dict(d.get("sessions") or {})}


_state = _load()


def _save():
    """拿着 _lock 调。写不进去由写盘线程在服务输出里说，内存里的照旧是准的。"""
    if not broken:
        diskq.rewrite(FILE, json.dumps(_state, ensure_ascii=False))


def get(sid: str) -> dict | None:
    """这个会话最新的摘要 {long, short, name, at, ...}，没有是 None。"""
    with _lock:
        rec = _state["sessions"].get(sid) or {}
        return dict(rec["summary"]) if rec.get("summary") else None


def due(sid: str, ctx: int, calls: int) -> bool:
    """和上次触发时比，上下文和主会话的调用次数都多出够多了没有（第一次和 0 比）。"""
    if broken:
        return False
    with _lock:
        base = (_state["sessions"].get(sid) or {}).get("base") or {}
    return ctx - (base.get("ctx") or 0) > STEP_CTX and calls - (base.get("calls") or 0) > STEP_CALLS


def begin(sid: str, ctx: int, calls: int, end: str):
    """触发了：先记下这一次的基线，成没成都不在同一个空闲里再触发。"""
    with _lock:
        rec = _state["sessions"].setdefault(sid, {})
        rec["base"] = {"ctx": ctx, "calls": calls, "end": end, "at": time.time()}
        _save()


def parse(text: str) -> dict:
    """回复里的三样东西；缺了、空的、太长的抛 ValueError，写明是哪一样。"""
    out = {}
    for key, tag, most in (("long", "长摘要", LONG_MAX), ("short", "短摘要", SHORT_MAX), ("name", "名字", 40)):
        m = re.findall(rf"<{tag}>\s*(.*?)\s*</{tag}>", text or "", re.S)
        if not m or not m[-1].strip():
            raise ValueError(f"回复里没有{tag}")
        val = m[-1].strip()
        if len(val) > most:
            raise ValueError(f"{tag}有 {len(val)} 字，超过 {most}")
        out[key] = val
    return out


def hit_of(usage: dict | None) -> float | None:
    """这次分叉的输入里命中缓存的比例（缓存命中 /（输入 + 缓存写入 + 缓存命中））；没有用量是 None。"""
    u = usage or {}
    cr = u.get("cache_read_input_tokens") or 0
    total = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + cr
    return cr / total if total else None


def done(sid: str, family: str, summary: dict | None, why: str, usage: dict | None, extra: dict) -> float | None:
    """记下这一次的结果（成了存摘要，没成存原因）和用量；按命中比例数这一家连着没命中的次数。返回命中比例。"""
    hit = hit_of(usage)
    with _lock:
        rec = _state["sessions"].setdefault(sid, {})
        last = {"at": time.time(), "ok": summary is not None, "why": why, "hit": hit,
                "usage": {k: (usage or {}).get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                                  "cache_read_input_tokens", "output_tokens")}, **extra}
        rec["last"] = last
        if summary is not None:
            rec["summary"] = {**summary, "at": last["at"], **extra}
        _save()
        if hit is not None:
            bad[family] = 0 if hit >= MIN_HIT else bad.get(family, 0) + 1
    return hit


def stopped(family: str) -> bool:
    return bad.get(family, 0) >= BAD_LIMIT


def all_short() -> dict[str, tuple[float, str]]:
    """会话编号 → (写摘要的时刻, 短摘要)，回忆把它当成一条气泡（sessions.py 直接读文件，这里给查看器自己用）。"""
    with _lock:
        return {sid: (r["summary"]["at"], r["summary"]["short"]) for sid, r in _state["sessions"].items() if r.get("summary")}
