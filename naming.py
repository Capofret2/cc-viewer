"""会话的名字是谁起的，以及会话里的 AI 给自己的会话起名（很多用户不会自己去改名）。

- 名字只认会话记录里最近的一条 custom-title（网页改名、CLI 的 /rename、AI 起名都是追加它，见 server.append_title）；
  CLI 自动起的标题、最后一句话都不算名字。记录里分不出是谁写的，所以 AI 写下的名字记在这里（ai）：
  最近的 custom-title 正是它就算 AI 起的，是别的就算用户起的，没有就是还没有名字。网页上改名时把这里的记录去掉（forget），
  用户改成和 AI 一样的字也算用户起的。
- AI 用会话里的 ccname（同目录的客户端，走人设卡接口的套接字，personas.handle 的 op=name 转到这里的 handle）：
  只能改自己所在的会话（认人照 personas._who），用户起的名字要加 --force 才覆盖，什么时候加由 AI 按用户的话来定（系统提示词 chat.VIEWER_NAME）；
  名字宽度不超过 MAX_WIDTH 个半角（汉字、全角字符算两个，字母、数字、半角符号算一个），用户要了也一样，长了请用户自己在网页上改。
- 写进记录要等 Claude 停下：CLI 内存里记着旧名字，正在干活时写进去以后可能被它写回旧的（和网页改名同一个顾虑，server.rename_in_log），
  所以先记成待写（pending），server.apply_names 等这一轮结束、没有后台任务和约好的叫醒（或者 CLI 已经收掉了）时再写，写完收掉 CLI。
- 提醒起名：会话聊了一阵子（上下文到 NUDGE_CTX、用户说了超过 NUDGE_USER 句话、AI 调用了超过 NUDGE_TOOLS 次工具）、
  又没有用户起的名字时，chat.Driver._guard 在工具执行后或用户发话时把提醒附进对话。每个会话只提醒一次（nudged），AI 起没起都不再提醒。
文件在数据目录，只给查看器自己读写。读不出来（不是没有）就整个停用、说清楚，不能当成空的：
那样 AI 起的名字全会被当成用户起的，提醒过的会话也会再提醒一遍。
"""
import json
import threading
import time
import unicodedata

from fastapi import HTTPException

import diskq
from instance import DATA

# {"ai": {会话编号: AI 写下的名字}, "pending": {会话编号: {name, force, ts, error?}}, "nudged": {会话编号: 提醒的时刻}}
FILE = DATA / "session_names.json"
MAX_WIDTH = 12                          # 半角个数：6 个汉字，或者 12 个字母、数字
NUDGE_CTX, NUDGE_USER, NUDGE_TOOLS = 100_000, 3, 10

NUDGE = ("【会话起名】这个会话已经聊了一阵子，还没有用户起的名字。{now}请用 `{cmd} set <名字>` 按到现在为止在做的事给它起个名字"
         "（不超过 {width} 个字，字母、数字和半角符号各算半个），顺手做就行，不用问用户，也不用停下手上的事。这个提醒只有这一次。")

# 会话编号 → 记录里最近的 custom-title（没有是空字符串），由 server 设成 server.name_title；没设说明查看器还是没有这块的旧进程
title_of = None

_lock = threading.Lock()
broken = ""


def _load() -> dict:
    global broken
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        d = {}
    except (OSError, ValueError) as e:
        broken = f"会话名字的记录 {FILE} 读不出来（{type(e).__name__}: {e}），起名和起名提醒先停用"
        print(broken, flush=True)
        d = {}
    return {k: dict(d.get(k) or {}) for k in ("ai", "pending", "nudged")}


_state = _load()


def _save():
    """拿着 _lock 调。写不进去由写盘线程在服务输出里说，内存里的照旧是准的。"""
    if not broken:
        diskq.rewrite(FILE, json.dumps(_state, ensure_ascii=False))


def width(s: str) -> int:
    """按半角数：东亚宽字符、全角字符算两个；歧义宽度的（“”…—·这些）在中文里也是全角显示，一样算两个。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WFA" else 1 for c in s)


def status(sid: str, title: str | None) -> tuple[str, str]:
    """(谁起的, 名字)：user 用户起的，ai AI 起的，none 还没有名字。title 是记录里最近的 custom-title。"""
    if not title:
        return "none", ""
    with _lock:
        return ("ai" if _state["ai"].get(sid) == title else "user"), title


def _check(name: str) -> str:
    name = name.strip()
    if not name:
        raise HTTPException(400, "名字不能是空的")
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise HTTPException(400, "名字里不能有换行、制表符这类控制字符")
    if (w := width(name)) > MAX_WIDTH:
        raise HTTPException(400, f"「{name}」有 {w / 2:g} 个字宽，最多 {MAX_WIDTH // 2} 个"
                                 "（汉字和全角字符算一个，字母、数字和半角符号算半个）")
    return name


def handle(req: dict, sid: str) -> str:
    """ccname 的一条命令（personas.serve 在线程里调），返回要打到标准输出上的字；不成立就抛 HTTPException（ccname 那边看到原因）。"""
    if broken:
        raise HTTPException(503, broken)
    if title_of is None:
        raise HTTPException(503, "这个查看器还不支持给会话起名（新代码要等查看器重启后才生效）")
    kind, title = status(sid, title_of(sid))
    if req.get("set") is None:
        out = {"none": "这个会话还没有名字。",
               "ai": f"现在的名字是「{title}」，是你起的。",
               "user": f"现在的名字是「{title}」，是用户自己起的，用户明确要你改的时候才加 --force 覆盖。"}[kind]
        with _lock:
            p = _state["pending"].get(sid)
        if p:
            out += f"\n「{p['name']}」等这一轮结束、后台任务都停下以后写进去。"
            if p.get("error"):
                out += f"上次写的时候出错了：{p['error']}，查看器会接着再试。"
        return out + "\n"
    name = _check(str(req["set"]))
    if kind == "user" and not req.get("force"):
        raise HTTPException(409, f"现在的名字「{title}」是用户自己起的，不能覆盖；用户明确要你改的时候加 --force")
    with _lock:
        if name == title:
            if _state["pending"].pop(sid, None) is not None:
                _save()
            return f"名字已经是「{name}」了，不用改。\n"
        _state["pending"][sid] = {"name": name, "force": bool(req.get("force")), "ts": time.time()}
        _save()
    return f"好了：等这一轮结束、后台任务都停下以后，会话名改成「{name}」。\n"


def pending() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in _state["pending"].items()}


def written(sid: str, name: str):
    """待写的名字写进记录了（或者记录里本来就是它）：从此算 AI 起的。"""
    with _lock:
        _state["ai"][sid] = name
        if _state["pending"].get(sid, {}).get("name") == name:
            _state["pending"].pop(sid)
        _save()


def drop(sid: str):
    with _lock:
        if _state["pending"].pop(sid, None) is not None:
            _save()


def failed(sid: str, error: str) -> bool:
    """写的时候出错：记下原因（ccname 看得到），接着再试。返回这是不是一个新的错（新的才打进服务输出，不每圈刷屏）。"""
    with _lock:
        p = _state["pending"].get(sid)
        if not p or p.get("error") == error:
            return False
        p["error"] = error
        _save()
        return True


def forget(sid: str):
    """用户在网页上改了名：这个会话的名字从此是用户起的，AI 之前起的和还没写进去的都作废。"""
    with _lock:
        had = _state["ai"].pop(sid, None) is not None
        had = _state["pending"].pop(sid, None) is not None or had
        if had:
            _save()


def nudged(sid: str) -> bool:
    with _lock:
        return broken != "" or sid in _state["nudged"]


def nudge(sid: str, kind: str, title: str, ctx: int, said: int, tools: int, cmd: str) -> str:
    """条件都满足、还没提醒过就记下「提醒过了」，返回要附进对话的提醒；否则返回空字符串。"""
    if broken or kind == "user" or ctx < NUDGE_CTX or said <= NUDGE_USER or tools <= NUDGE_TOOLS:
        return ""
    with _lock:
        if sid in _state["nudged"]:
            return ""
        _state["nudged"][sid] = time.time()
        _save()
    now = f"现在的名字「{title}」是你以前起的，还合适就不用改。" if kind == "ai" else ""
    return NUDGE.format(now=now, cmd=cmd, width=MAX_WIDTH // 2)
