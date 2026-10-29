"""会话的工作设备：每个会话可以取消勾选一些设备，ssh 门卫不放行取消了的。

为什么要它：一个人登记的设备，他的每个会话都连得上，会话里的 Claude 也不知道该去哪台干活，要用户每次交代。
默认：登记了的设备每个会话都能用，后来加的也马上能用，除非用户在这个会话里主动取消勾选。
所以按会话记的是「取消了哪些」，不是「勾了哪些」：
- 「云电脑」就是 Claude 所在的这台机器（共用环境、独立环境都一样），一直在，取消不了，也不记在这里。
- 新建会话时可以先取消，会话里随时能改，两处都是输入框下面的设备小标签；会话里还有「⋯」菜单的「工作设备…」。
- 建会话时把能用的设备写进系统提示词（chat.session_prompt 调 prompt）。CLI 只在第一次请求和压缩以后才用系统提示词，
  所以能用的设备变了（取消、重新勾上、新加了设备、删了设备），下一句话由查看器的提交消息钩子（chat.Driver._guard）附上新清单（note），只附一次。
- 门卫（sshgw.run_ssh）连之前问 why_not：这个会话取消了它就拒绝，并告诉 Claude 去哪里改。
- 功能上线以前的会话没有记录，就是什么都没取消；用户在里面第一次改了以后才开始附新清单（以前从没告诉过 Claude 清单）。
- 改名、删除设备（sshgw.rename_target、remove_target）时取消的名字跟着改；会话里让 Claude 用 ccssh add 登记的设备，在这个会话里不算取消。
数据在数据目录的 session_devices.json：{"sessions": {会话编号: {user, off, told}}}，off 是取消了的设备名，
told 是 Claude 最近一次被告知的能用的设备（系统提示词或钩子附上的），和现在算出来的不一样就是还欠一次告知；None 是还没告知过、也不用告知（老会话）。
"""
import json
import os
import threading

from instance import DATA

FILE = DATA / "session_devices.json"
CLOUD = "云电脑"
_lock = threading.Lock()
_data: dict | None = None


class Bad(Exception):
    """说给用户听的错（接口回 400）。"""


def _load() -> dict:
    global _data
    if _data is None:
        try:
            _data = json.loads(FILE.read_text())
        except FileNotFoundError:
            _data = {}
        _data.setdefault("sessions", {})
    return _data


def _save():
    # 存不下来要报出去：没存下，门卫就会按旧的放行
    tmp = FILE.with_name(f".session_devices.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(_data, ensure_ascii=False))
    tmp.replace(FILE)


def off(sid: str) -> list[str]:
    """这个会话取消勾选的设备；没有记录（功能上线以前的会话、没取消过）是空的。"""
    with _lock:
        rec = _load()["sessions"].get(sid)
        return list(rec["off"]) if rec else []


def usable(off_names, mine: dict) -> list[str]:
    """能用的设备（不含云电脑）：登记了的、没取消的。"""
    return [n for n in sorted(mine) if n not in off_names]


def why_not(sid: str, name: str) -> str:
    """门卫问：这个会话能不能连 name。能就是空字符串。跳板不用管：经它跳过去是连目标的一部分。"""
    if name not in off(sid):
        return ""
    return (f"{name} 在这个会话里被取消了勾选，不是这个会话的工作设备，这个会话连不上它。要用就请用户在网页上把它勾上"
            "（输入框下面的设备小标签，或者右上角「⋯」里的「工作设备…」）")


def check(names, mine: dict) -> list[str]:
    """核对取消勾选的设备名：要是这个人登记过的。"""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise Bad("取消勾选的工作设备要给一串设备名")
    out = []
    for n in names:
        if n == CLOUD:
            raise Bad("云电脑一直在，取消不了")
        if n not in mine:
            raise Bad(f"你没有叫 {n} 的设备")
        if n not in out:
            out.append(n)
    return sorted(out)


def start(sid: str, user: str, off_names: list[str], mine: dict):
    """新建会话时记下（check 核对过的），拉起 CLI 之前调：系统提示词里写的就是此刻能用的那些，所以 told 是它。"""
    with _lock:
        _load()["sessions"][sid] = {"user": user, "off": off_names, "told": usable(off_names, mine)}
        _save()


def change(sid: str, user: str, names, mine: dict) -> list[str]:
    """会话中途改取消勾选的：下一句话由钩子附上新清单（note）。"""
    off_names = check(names, mine)
    with _lock:
        d = _load()
        rec = d["sessions"].get(sid)
        # 老会话第一次改：Claude 从没被告知过清单，记成全部能用（就是它一直以来的样子），和新的不一样就会附上
        told = rec["told"] if rec and rec["told"] is not None else usable([], mine)
        d["sessions"][sid] = {"user": user, "off": off_names, "told": told}
        _save()
    return off_names


def added(sid: str, user: str, name: str):
    """会话里让 Claude 用 ccssh add 登记了设备：用户显然想在这个会话里用它，以前取消过同名的也不再算取消。"""
    with _lock:
        rec = _load()["sessions"].get(sid)
        if rec and rec["user"] == user and name in rec["off"]:
            rec["off"] = [n for n in rec["off"] if n != name]
            _save()


def renamed(user: str, old: str, new: str):
    with _lock:
        hit = False
        for rec in _load()["sessions"].values():
            if rec["user"] != user:
                continue
            for key in ("off", "told"):
                if rec[key] and old in rec[key]:
                    rec[key] = sorted(new if n == old else n for n in rec[key])
                    hit = True
        if hit:
            _save()


def removed(user: str, name: str):
    """设备删掉了：取消勾选里也去掉（以后再加同名的就是新设备，默认能用）。told 不动，下一句话照样告诉 Claude 少了它。"""
    with _lock:
        hit = False
        for rec in _load()["sessions"].values():
            if rec["user"] == user and name in rec["off"]:
                rec["off"] = [n for n in rec["off"] if n != name]
                hit = True
        if hit:
            _save()


def _describe(names: list[str], mine: dict) -> str:
    import devices
    parts = [f"{CLOUD}（就是你现在所在的这台机器，直接在本地干活）"]
    for n in names:
        t = mine.get(n) or {}
        what = f"{({'windows': 'Windows', 'mac': 'Mac', 'linux': 'Linux'}).get(t.get('os'), '')} 电脑".strip() \
            if devices.is_pc(t) else "服务器"
        parts.append(f"{n}（{what}，用 `ssh {n} '<命令>'` 连）")
    return "、".join(parts)


PROMPT = """

工作设备：
- 这个会话能用的工作设备是：{list}。用户让你干活时就在这些设备上做，没说在哪台时按要做的事挑合适的那台，拿不准就问用户。
- 用户登记的设备默认都能用，以后新登记的也一样；用户在这个会话里取消勾选的设备连不上。用户要用被取消的设备时，请他在网页上把它勾上\
（输入框下面的设备小标签，或者右上角「⋯」里的「工作设备…」）；还没登记的设备，请他在网页的「设备」页添加。\
能用的设备变了，会在用户发来的消息旁边附上新的清单，以新的为准。"""

NOTE = "（网页提示，不是用户说的话）这个会话能用的工作设备变了，现在是：{list}。以后按这份来，以前说的作废；不在里面的设备这个会话连不上。"


def _rec(sid: str) -> dict | None:
    with _lock:
        rec = _load()["sessions"].get(sid)
        return dict(rec) if rec else None


def prompt(sid: str, user: str | None) -> str:
    """系统提示词里工作设备那一段（chat.session_prompt）。没开门卫、无主会话是空的；
    没有记录的会话（终端里建的、功能上线以前的）也写：它们什么都没取消，写出来 Claude 就知道有哪些可用（只在新会话和压缩后读到）。"""
    import devices
    if not devices.ENABLED or not user:
        return ""
    import sshgw
    mine = sshgw.targets(user)
    rec = _rec(sid)
    return PROMPT.format(list=_describe(usable(rec["off"] if rec else [], mine), mine))


def note(sid: str) -> str:
    """提交消息时附上的话（chat.Driver._guard）：能用的设备变了、Claude 还不知道才有，附上就记成已告知。"""
    rec = _rec(sid)
    if not rec or rec["told"] is None:
        return ""
    import sshgw
    mine = sshgw.targets(rec["user"])
    now = usable(rec["off"], mine)
    if now == rec["told"]:
        return ""
    text = NOTE.format(list=_describe(now, mine))
    with _lock:
        cur = _load()["sessions"].get(sid)
        if cur and cur["off"] == rec["off"]:
            cur["told"] = now
            _save()
    return text
