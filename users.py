"""网页的用户：注册登录、会话的主人和可见范围、额度账本。

共用环境保留原来的运行方式；可选的独立环境以不同 Linux uid 运行，私有文件由系统权限隔离。
独立会话的环境编号保存在查看器自己的主人登记中，只供本人使用，不随网页分享或转移主人改变运行身份。

两种实例（instance.py 的 MULTI）：
- 单用户（没写多用户配置的实例，比如测试实例）：没有注册登录，谁打开都是主人，主人就是查看器账号；没记主人的会话也算主人的。
- 多用户：网页上能注册、登录，不登录就是游客。查看器账号是 instance.json 里 admins 列出的用户名。
  配了 terminal_owner 的实例，终端里建的会话也算那个用户的、只有他看得到（instance.TERMINAL_OWNER），游客什么会话都看不到。

会话按会话编号（.jsonl 的文件名，子代理记录算它所属的会话）记主人，不按路径：同一个文件经不同的软链接打开，
路径不一样、编号一样，按路径记会被绕过去。
- 登录后在网页上新建的会话记下主人，默认只有主人自己看得到；可见范围是 "public"，或者一串用户名（主人自己总看得到）。
- 没记主人的（终端里建的、以前的、游客建的）是无主会话，谁都能看能用。
- 看得到就能用（发话、改名、换号……）；改可见范围只有主人和查看器账号，改主人只有查看器账号。

额度：单位和界面上的点数一样，1 点 = 5 小时额度的 1%。余额 = 查看器账号发的（grants.jsonl，可以是负数；额度计划 grant_plans.json 里已经到期的各期也算，见 plan_grants）加上号主收入（earnings）
减去名下会话在限量号上花的：token 数从会话记录里取（calls.py），哪个号发的查 api_calls.jsonl，按 rates.py 换算。
查不到是哪个号发的调用，只要是开始记调用以后的，一律按限量号算。
花费按会话现在的主人算：会话转给别人，以前花的也跟着过去。没有单价的模型不计费，单独数出来，界面上要说。
游客和无主会话不能用限量号：花掉的额度没有人可记。

文件都在实例的数据目录里，写的时候先写临时文件再整体换掉，写到一半出错旧文件也不坏。
读的时候文件不在就是还没有；在却读不了、格式不对，就报错停下，不能当成空的（比如用户表读坏了当成没人，别人就能抢注同名）。
"""
import contextlib
import platform_costs
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

import api_calls   # 每次调用是哪个号发的
import api_usage   # 外部API的可信用量，网页回环仍按CLI记录收费
import calls       # 每次调用的 token 数（和气泡、拟合同一份）
import rates       # 点数单价
import reserve     # 预定额度：付预定的钱、预定付掉的调用（见 ledger）
import instance
from instance import ADMINS, CALL, DATA, MULTI, OWNERS, PAID, PAID_SINCE, PROMO, TERMINAL_OWNER

USERS_FILE = DATA / "users.json"            # 用户名 → {salt, hash, ts, defaultAccount, features, autoSwitch, bottomTabs, sharedEnv, avatar}；newUser 是以前注册时打的标记，
                                            # simpleUi 是经典模式弃用以前存的界面选择，都不再用
LOGINS_FILE = DATA / "logins.json"          # 登录凭据的散列 → {user, exp}；cookie 里是凭据本身
GRANTS_FILE = DATA / "grants.jsonl"         # 发额度：一笔一行 {ts, user, amount, note, by}
PLANS_FILE = DATA / "grant_plans.json"      # 额度计划：计划编号 → {user, total, parts, days, start, note, by, ts, revoked?: {ts, by}}（见 grant_plan）
OWNERS_FILE = DATA / "session_owners.json"  # 会话编号 → {owner, visible, ts}
RESERVE_CLOSED_FILE = DATA / "reserve_closed.json"   # 号主关了别人预定的号 → {by, ts}（见 set_reserve_open）
# cookie 只认主机不认端口，两个实例经同一个地址访问时会互相看到，名字里带上数据目录区分开。
# 配置里写了名字就用它：数据目录搬了家，名字不能跟着变，不然已有的登录全都失效（见 instance.py 的 cookie）
COOKIE = instance.COOKIE or "ccv_" + hashlib.sha1(str(DATA).encode()).hexdigest()[:8]
# 「桌面应用全屏打开」（设备级）：记在这台设备的浏览器里，浏览器取桌面应用清单只带 cookie，
# 所以由前端写这个 cookie（值是 1），server 的 /manifest.json 看它。名字跟着登录 cookie，两个实例分开
PWA_COOKIE = COOKIE + "_pwafull"
LOGIN_DAYS = 30
PUBLIC = "public"
GUEST_NAME = "游客"

_lock = threading.RLock()   # 接口在线程池里跑，几个请求可能同时改同一份表


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _save(path: Path, data):
    """整份写回。只给查看器自己读写（0600）：数据目录可能就是代码目录，别的系统用户（比如 RUN_AS 下的 Claude）读得到，
    用户表里有密码散列，拿去慢慢试就能试出密码。"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)   # 上次写到一半留下的临时文件可能是别的权限
        tmp.replace(path)
    except OSError as e:
        raise HTTPException(500, f"无法写入 {path.name}：{e}")


def _load_lines(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(x) for x in lines if x.strip()]


users: dict[str, dict] = _load(USERS_FILE, {})
logins: dict[str, dict] = _load(LOGINS_FILE, {})
grants: list[dict] = _load_lines(GRANTS_FILE)
plans: dict[str, dict] = _load(PLANS_FILE, {})
owners: dict[str, dict] = _load(OWNERS_FILE, {})
reserve_closed: dict[str, dict] = _load(RESERVE_CLOSED_FILE, {})


@dataclass(frozen=True)
class Who:
    """这次请求是谁。name 为 None 是游客；stale 是带了 cookie 却认不出来的原因（过期了、被退出了），界面上要说出来。"""
    name: str | None
    admin: bool = False
    stale: str = ""


GUEST = Who(None)
MASTER = Who(CALL, True)   # 单用户实例里，谁打开都是同一个人
NOBODY = MASTER if not MULTI else GUEST   # 不知道是谁的时候按谁算（比如标签刚打开、还没人发话时先显示的号）


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def identify(token: str | None) -> Who:
    """按 cookie 认人。登录 30 天有效，用过就顺延（一天最多写一次文件）。"""
    if not MULTI:
        return MASTER
    if not token:
        return GUEST
    now = time.time()
    with _lock:
        rec = logins.get(_digest(token))
        if not rec:
            return Who(None, stale="这台设备上的登录已经失效（退出过，或者在别处改过密码），现在是游客")
        if rec["exp"] < now:
            return Who(None, stale=f"登录过期了（{LOGIN_DAYS} 天没用），现在是游客")
        if rec["user"] not in users:
            return Who(None, stale=f"用户「{rec['user']}」已经不在了，现在是游客")
        if rec["exp"] - now < (LOGIN_DAYS - 1) * 86400:
            rec["exp"] = now + LOGIN_DAYS * 86400
            _save(LOGINS_FILE, logins)
        return Who(rec["user"], rec["user"] in ADMINS)


def _check_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 20:
        raise HTTPException(400, "用户名须为 1 到 20 个字")
    if re.search(r"[\x00-\x1f\x7f,/\\]", name):
        raise HTTPException(400, "用户名不能包含逗号、斜杠或控制字符")
    if name in (GUEST_NAME, CALL) and name not in ADMINS:
        raise HTTPException(400, f"「{name}」为界面保留称呼，请使用其他用户名")
    return name


def _check_password(password: str):
    if len(password) < 4:
        raise HTTPException(400, "密码至少 4 位")


def _issue(name: str) -> str:
    """发一张新的登录凭据，cookie 里放它本身，文件里只记散列。顺手清掉过期的。"""
    token, now = secrets.token_urlsafe(32), time.time()
    for k in [k for k, v in logins.items() if v["exp"] < now]:
        del logins[k]
    logins[_digest(token)] = {"user": name, "exp": now + LOGIN_DAYS * 86400}
    _save(LOGINS_FILE, logins)
    return token


def _need_multi():
    if not MULTI:
        raise HTTPException(400, f"当前为单用户实例，仅供{CALL}使用，无需注册或登录")


def register(name: str, password: str) -> str:
    _need_multi()
    name = _check_name(name)
    _check_password(password)
    with _lock:
        if name in users:
            raise HTTPException(409, f"用户名「{name}」已被占用")
        salt = secrets.token_bytes(16)
        users[name] = {"salt": salt.hex(), "hash": _hash(password, salt), "ts": time.time()}
        _save(USERS_FILE, users)
        return _issue(name)


def shared_users() -> set[str]:
    """名下有共用环境会话（会话登记里没记独立环境）的人，扫一遍会话登记。"""
    with _lock:
        return {r["owner"] for r in owners.values() if r.get("owner") and not r.get("environment")}


def uses_shared(name: str | None, shared: set[str] | None = None) -> bool:
    """这个人的新会话能不能选共用环境（不按注册时的「新用户」标记一刀切成新老用户）：
    查看器账号在用户管理里改过的照存的来（sharedEnv）；没改过的按用没用过：名下有共用环境的会话就能选，
    没有就只用自己的独立环境（没建好的页面上自动建，见 js/sessions.js）。
    用户自己在设置里切的「共用环境」开关（features.shared）不算在这里：它只管新会话页上放不放这一项；
    算进来的话，关掉以后这个人就成了只用独立环境的人，设置里的开关跟着没了，再也打不开。
    shared 是 shared_users() 的结果，一次查好几个人时传进来，不用每人扫一遍。"""
    u = users.get(name, {}) if name else {}
    if "sharedEnv" in u:
        return bool(u["sharedEnv"])
    return name in (shared if shared is not None else shared_users())


def is_new(name: str | None, shared: set[str] | None = None) -> bool:
    """只用独立环境的人（接口里沿用 newUser 这个名字）：界面上只有这一种环境、不提共用环境，没建好的自动建，
    会话列表默认只列自己的（别人的收起来，只改显示，见 js/sessions.js）。就是 uses_shared 的反面：没用过共用环境、
    查看器账号也没给他打开的人，新注册的都是。游客和单用户实例都不是。"""
    return bool(MULTI and name and name in users and not uses_shared(name, shared))


def set_new(who: Who, name: str, on: bool):
    """查看器账号在用户管理里改某人只用独立环境（on）还是也能选共用环境，存成 sharedEnv。
    和他自己在设置里切的「共用环境」开关（features.shared）分开存，不动他自己的选择。"""
    _need_multi()
    need_admin(who)
    with _lock:
        if name not in users:
            raise HTTPException(404, f"用户「{name}」不存在")
        users[name].pop("newUser", None)   # 以前注册时打的标记，不再用
        users[name]["sharedEnv"] = not on
        _save(USERS_FILE, users)


def _verify(name: str, password: str) -> bool:
    u = users.get(name)
    return bool(u) and hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"])


def login(name: str, password: str) -> str:
    _need_multi()
    name = name.strip()
    with _lock:
        if not _verify(name, password):
            raise HTTPException(401, "用户名或密码错误")
        return _issue(name)


def logout(token: str | None):
    if token:
        with _lock:
            if logins.pop(_digest(token), None):
                _save(LOGINS_FILE, logins)


def _set_password(name: str, password: str):
    """换密码，并让这个人在所有设备上的登录都失效（改密码的这台由调用方重新发一张）。"""
    _check_password(password)
    salt = secrets.token_bytes(16)
    users[name].update(salt=salt.hex(), hash=_hash(password, salt))
    _save(USERS_FILE, users)
    for k in [k for k, v in logins.items() if v["user"] == name]:
        del logins[k]
    _save(LOGINS_FILE, logins)


def change_password(who: Who, old: str, new: str) -> str:
    _need_multi()
    if not who.name:
        raise HTTPException(401, "请先登录再修改密码")
    with _lock:
        if not _verify(who.name, old):
            raise HTTPException(401, "原密码错误")
        _set_password(who.name, new)
        return _issue(who.name)


def need_admin(who: Who):
    if not who.admin:
        raise HTTPException(403, "仅查看器账号可执行此操作")


def reset_password(who: Who, name: str, password: str):
    """查看器账号给忘了密码的人重设一个。"""
    _need_multi()
    need_admin(who)
    with _lock:
        if name not in users:
            raise HTTPException(404, f"用户「{name}」不存在")
        _set_password(name, password)


def names() -> list[str]:
    """全部用户名（选可见范围时用），按注册先后。单用户实例只有那一个人。"""
    if not MULTI:
        return [CALL]
    return sorted(users, key=lambda n: users[n]["ts"])


def user_default(who: Who) -> str | None:
    """这个人自己设的新会话默认账号，登录了没设就是 auto；
    游客和单用户实例没有，用全局设置（多用户实例里全局设置因此只管游客）。"""
    if not (MULTI and who.name):
        return None
    return users.get(who.name, {}).get("defaultAccount") or "auto"


def set_user_default(who: Who, account: str | None):
    if not (MULTI and who.name):
        raise HTTPException(400, "请登录后设置个人默认账号")
    with _lock:
        if account:
            users[who.name]["defaultAccount"] = account
        else:
            users[who.name].pop("defaultAccount", None)
        _save(USERS_FILE, users)


def fresh_tab(who: Who) -> dict | None:
    """这个人上次在新会话标签页上发第一句话时选的运行环境、工作区、标签，下次开新会话标签页照着填。
    游客和单用户实例没有，前端记在浏览器里。"""
    return users.get(who.name, {}).get("freshTab") if MULTI and who.name else None


def remember_fresh_tab(who: Who, environment: str, cwd: str, tags: list[str]):
    if not (MULTI and who.name):
        return
    with _lock:
        if who.name in users:
            users[who.name]["freshTab"] = {"environment": environment, "cwd": cwd, "tags": list(tags)}
            _save(USERS_FILE, users)


def show_tests(who: Who) -> bool | None:
    """这个人在全部会话、最近会话里要不要看到测试工作区的会话（server.is_test_cwd），没选过是不看。
    游客和单用户实例是 None，前端记在浏览器里。"""
    if not (MULTI and who.name):
        return None
    return bool(users.get(who.name, {}).get("showTests"))


def set_show_tests(who: Who, on: bool):
    if not (MULTI and who.name):
        raise HTTPException(400, "请登录后保存此设置")
    with _lock:
        if who.name not in users:
            raise HTTPException(404, f"用户 {who.name} 不存在")
        if on:
            users[who.name]["showTests"] = True
        else:
            users[who.name].pop("showTests", None)
        _save(USERS_FILE, users)


def auto_switch(who: Who) -> bool:
    """发话、新建会话时当前的号被预测拦截拦下，要不要按自动挑号的排法换一个过得去的号接着发（server.switch_target）。
    默认开着，所有人都一样，在设置里关了才记成 False；游客和单用户实例没处关，一律开着。"""
    return not (MULTI and who.name) or users.get(who.name, {}).get("autoSwitch", True) is not False


def set_auto_switch(who: Who, on: bool):
    if not (MULTI and who.name):
        raise HTTPException(400, "请登录后保存此设置")
    with _lock:
        if who.name not in users:
            raise HTTPException(404, f"用户 {who.name} 不存在")
        if on:
            users[who.name].pop("autoSwitch", None)
        else:
            users[who.name]["autoSwitch"] = False
        _save(USERS_FILE, users)


def bottom_tabs(who: Who) -> bool | None:
    """也留着页面底下的标签页栏（开关）：开着时换会话不把旧的收掉，一个个标签页切。
    默认开：在设置里关掉记成 False、打开记成 True，没有这一项的按开；游客和单用户实例是 None，不出这个开关。"""
    if not (MULTI and who.name):
        return None
    return users.get(who.name, {}).get("bottomTabs") is not False


def set_bottom_tabs(who: Who, on: bool):
    if not (MULTI and who.name):
        raise HTTPException(400, "请登录后保存此设置")
    with _lock:
        if who.name not in users:
            raise HTTPException(404, f"用户 {who.name} 不存在")
        users[who.name]["bottomTabs"] = bool(on)   # 两头都记下：默认值以后再变，已经选过的人不跟着变
        _save(USERS_FILE, users)


# 头像：页面先在浏览器里裁成 256×256、压成 WebP 再传（js/avatar.js），这里只认文件头是 PNG、JPEG、WebP 的，
# 超过 AVATAR_MAX 就拒收（绕过页面直接调接口也传不了大文件）。文件按用户名的散列和内容的散列起名放在 avatars/ 下（用户名里可能有空格、
# 汉字、@ 这些），users.json 里记 {"v": 内容散列, "type": 类型}；换了内容散列就变，页面按它取（/api/avatar?v=），浏览器能放心长缓存。
# 恢复成文字头像只去掉记录，旧文件不删（什么都不删）。现在只有本人看得到自己的头像
AVATAR_DIR = DATA / "avatars"
AVATAR_MAX = 256 * 1024


def _avatar_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def avatar(who: Who) -> str | None:
    """头像的版本（内容散列），没传过是空字符串；游客和单用户实例是 None（没有头像这一项）"""
    if not (MULTI and who.name):
        return None
    return (users.get(who.name, {}).get("avatar") or {}).get("v", "")


def _avatar_path(name: str, v: str) -> Path:
    return AVATAR_DIR / f"{hashlib.sha1(name.encode()).hexdigest()[:16]}-{v}"


def avatar_file(who: Who) -> tuple[Path, str] | None:
    """自己头像的文件和类型，没有就是 None"""
    if not (MULTI and who.name):
        return None
    rec = users.get(who.name, {}).get("avatar")
    return (_avatar_path(who.name, rec["v"]), rec["type"]) if rec else None


def set_avatar(who: Who, data: bytes | None) -> str:
    """换头像（data 为 None 是恢复成文字头像），返回新的版本"""
    if not (MULTI and who.name):
        raise HTTPException(400, "请登录后更换头像")
    rec = None
    if data is not None:
        if len(data) > AVATAR_MAX:
            raise HTTPException(413, f"头像大小为 {len(data) // 1024} KB，不能超过 {AVATAR_MAX // 1024} KB")
        kind = _avatar_type(data)
        if not kind:
            raise HTTPException(400, "头像仅支持 PNG、JPEG、WebP 格式")
        rec = {"v": hashlib.sha256(data).hexdigest()[:16], "type": kind}
        path = _avatar_path(who.name, rec["v"])
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            AVATAR_DIR.mkdir(mode=0o700, exist_ok=True)
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                f.write(data)
            tmp.replace(path)
        except OSError as e:
            raise HTTPException(500, f"头像保存失败：{e}")
    with _lock:
        if who.name not in users:
            raise HTTPException(404, f"用户 {who.name} 不存在")
        if rec:
            users[who.name]["avatar"] = rec
        else:
            users[who.name].pop("avatar", None)
        _save(USERS_FILE, users)
    return rec["v"] if rec else ""


# 简洁模式的新会话页上，这几项选不选得到由用户自己在设置里开关：人设卡、共用环境（关着就只用自己的独立环境）、标签。
# 标签那个开关默认关（DEFAULT_OFF）；只管新会话页上那一项，
# 侧边栏按标签筛、会话行上写标签、「⋯」菜单里编辑标签照旧都有。
# persona 在设置里叫「二次元」：前端拿它同时管新会话页的颜文字和人设卡，键名不变，以前存的选择照旧算数
FEATURES = ("persona", "shared", "tags")
DEFAULT_OFF = ("tags",)   # 没切过的一律关着，不按用没用过定


def features(who: Who, used) -> dict | None:
    """这几项开没开。用户自己切过的照存的来；没切过的：DEFAULT_OFF 里的关着，其余按用没用过定（used() 现算，server.used_features）：
    用过就开着，没用过就关着，以后用上了自然就开了。游客和单用户实例是 None，前端全都显示。"""
    if not (MULTI and who.name):
        return None
    mine = users.get(who.name, {}).get("features", {})
    seen = used() if any(k not in mine and k not in DEFAULT_OFF for k in FEATURES) else {}
    return {k: bool(mine[k]) if k in mine else bool(seen.get(k)) for k in FEATURES}


def set_feature(who: Who, key: str, on: bool):
    if not (MULTI and who.name):
        raise HTTPException(400, "请登录后保存此设置")
    if key not in FEATURES:
        raise HTTPException(400, f"不支持「{key}」设置")
    with _lock:
        if who.name not in users:
            raise HTTPException(404, f"用户 {who.name} 不存在")
        users[who.name].setdefault("features", {})[key] = bool(on)   # 关也要明写：没写过的按用没用过
        _save(USERS_FILE, users)


# ---------- 会话的主人和可见范围 ----------

def sid_of(path: Path | str) -> str:
    """会话编号：主会话是文件名；子代理记录（<会话编号>/subagents/agent-*.jsonl）算它所属的那个会话。"""
    p = Path(path)
    return p.parent.parent.name if p.parent.name == "subagents" else p.stem


def _rec(sid: str) -> dict | None:
    """会话的主人和可见范围。没记的，实例配了 terminal_owner 就算他的、只有他看得到（适合 Claude 这个系统用户
    只有一个人在用的实例，终端里新建的会话也是他的）；没配就是无主。后台循环随后会把它正式记下来（adopt）。
    只认长得像会话编号的：浏览文件夹时普通目录的名字也拿来问（以会话编号命名的是子代理目录），不能把它们都当成他的会话藏起来。"""
    if rec := owners.get(sid):
        return rec
    return {"owner": TERMINAL_OWNER, "visible": []} if TERMINAL_OWNER and SID_RE.fullmatch(sid) else None


SID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")   # Claude Code 的会话编号


def adopt(sids) -> int:
    """把还没记主人的会话正式记到 terminal_owner 名下（server 的后台循环定时调）：判断能不能看靠 _rec 当场就对，
    账本却是按记下的主人去找会话的，不记下来，终端里建的会话在限量号上的花费就算不到他头上。返回记了几个。"""
    if not TERMINAL_OWNER:
        return 0
    with _lock:
        new = [s for s in sids if s not in owners]
        if new:
            now = time.time()
            owners.update({s: {"owner": TERMINAL_OWNER, "visible": [], "ts": now} for s in new})
            _save(OWNERS_FILE, owners)
    return len(new)


def owner_of(sid: str) -> str | None:
    """会话的主人；无主就是 None。单用户实例里没记主人的也算主人的（只有这一个人）。"""
    if sid and sid.startswith("api-"):
        return api_usage.owner_of(sid)
    rec = _rec(sid)
    return rec["owner"] if rec else (None if MULTI else CALL)


def can_see(who: Who, sid: str) -> bool:
    import isolation
    # 独立环境的会话只给本人和查看器账号看，分享给别人照旧不行
    if not who.admin and not isolation.can_see(who.name, sid):
        return False
    rec = _rec(sid)
    if not rec or who.admin:
        return True
    return rec["visible"] == PUBLIC or (who.name is not None and (
        who.name == rec["owner"] or who.name in rec["visible"] or who.name in rec.get("links", ())))


def use_why(who: Who, sid: str) -> str:
    """看得到的会话能不能往里发话、驱动它（换号、换模式、打断……）；空字符串就是能。
    公开会话和靠分享链接看到的会话只有主人和查看器账号能用，别人只能看；指定了人的会话照旧看得到就能用，无主会话谁都能用。"""
    rec = _rec(sid)
    if not rec or who.admin or who.name == rec["owner"] or (rec["visible"] != PUBLIC and who.name in rec["visible"]):
        return ""
    # 操作被拒时回这句，要说清是谁才能操作，但别冲（「只有 X 能往里发话，你只能看」这种提示味太冲）
    return (f"这是 {rec['owner']} {'公开的' if rec['visible'] == PUBLIC else '分享给你的'}会话，"
            f"只有 {rec['owner']} 本人能更改设置或继续对话")


def add_link_viewer(sid: str, name: str):
    """拿分享链接打开会话的人（server 的 open_share）记进 links：之后和公开会话一样能看、不能发话。"""
    with _lock:
        rec = _rec(sid)
        if not rec or name == rec["owner"] or name in rec.get("links", ()):
            return
        rec = owners.setdefault(sid, {**rec, "ts": time.time()})   # 算在 terminal_owner 名下、还没记下来的，这时记下
        rec["links"] = [*rec.get("links", []), name]
        _save(OWNERS_FILE, owners)


def clear_link_viewers(sid: str):
    """撤销分享链接：靠链接看到的人一起看不到了。"""
    with _lock:
        if (owners.get(sid) or {}).pop("links", None) is not None:
            _save(OWNERS_FILE, owners)


def hidden_why(who: Who) -> str:
    return "没有权限看这个会话：它的主人没开放给" + ("游客，登录后也许能看" if not who.name else "你")


def share(sid: str) -> dict:
    """发给前端的主人和可见范围（谁都一样，前端按自己是谁决定能不能改）。单用户实例不显示。"""
    rec = _rec(sid)
    return {"owner": rec["owner"], "visible": rec["visible"]} if MULTI and rec else {"owner": None, "visible": None}


def share_key(sid: str):
    """比对用：主人或可见范围变了，推送要把新的发出去。"""
    rec = _rec(sid)
    return (rec["owner"], str(rec["visible"])) if rec else None


def clean_visible(visible, owner: str):
    """可见范围整理成要存的样子：PUBLIC 原样；一串用户名去重，不认识的名字和主人自己丢掉。"""
    if visible == PUBLIC:
        return visible
    if not isinstance(visible, list):
        raise HTTPException(400, "可见范围须为公开或用户名列表")
    return [n for n in dict.fromkeys(visible) if n in users and n != owner]


def claim(sid: str, who: Who, visible=None):
    """网页上新建的会话记下主人；可见范围是新会话标签页上选的，没选就只有自己看得到。
    游客建的、单用户实例里建的不记（无主 / 本来就是主人的）。"""
    if not (MULTI and who.name):
        return
    with _lock:
        owners[sid] = {"owner": who.name, "visible": clean_visible(visible or [], who.name), "ts": time.time()}
        _save(OWNERS_FILE, owners)


def set_visible(who: Who, sid: str, visible) -> dict:
    """改可见范围：PUBLIC，或者一串用户名（不认识的名字丢掉，主人自己不用写）。只有主人和查看器账号能改。"""
    with _lock:
        rec = _rec(sid)
        if not rec:
            raise HTTPException(400, "无主会话对所有人可见，无法修改可见范围")
        if not (who.admin or who.name == rec["owner"]):
            raise HTTPException(403, "仅会话主人和查看器账号可修改可见范围")
        if rec.get("environment") and visible:
            raise HTTPException(400, "独立环境里的会话只供本人使用，不能分享给其他用户")
        visible = clean_visible(visible, rec["owner"])
        rec = owners.setdefault(sid, {**rec, "ts": time.time()})   # 算在 terminal_owner 名下、还没记下来的，这时记下
        rec["visible"] = visible
        _save(OWNERS_FILE, owners)
        return share(sid)


def set_owner(who: Who, sid: str, name: str | None) -> dict:
    """查看器账号改主人：给无主会话指定一个，转给别人，或者清掉变回无主。可见范围照旧（新主人不在名单里也总看得到）。"""
    _need_multi()
    need_admin(who)
    with _lock:
        if (_rec(sid) or {}).get("environment"):
            raise HTTPException(400, "独立环境里的会话不能转给其他用户")
        if not name:
            owners.pop(sid, None)
        elif name not in users:
            raise HTTPException(404, f"用户「{name}」不存在")
        else:
            old = _rec(sid) or {"visible": []}
            vis = old["visible"] if old["visible"] == PUBLIC else [n for n in old["visible"] if n != name]
            owners[sid] = {"owner": name, "visible": vis, "ts": time.time()}
        _save(OWNERS_FILE, owners)
        return share(sid)


# ---------- 删掉的会话 ----------
# 只在查看器里删：记一笔，全部会话、最近会话、找话、分享链接里都不再出现，打开着的标签页关掉；
# 会话文件一个字节不动（铁律：什么都不删），账本照样按文件算花费，终端的 /resume 里也照样看得到。删错了能恢复。
# 和主人表一样按会话编号记，换个软链接路径、子代理记录都一起藏住

DELETED_FILE = DATA / "session_deleted.json"   # 会话编号 → {by 谁删的, ts 什么时候}
deleted: dict[str, dict] = _load(DELETED_FILE, {})


def is_deleted(sid: str) -> bool:
    return sid in deleted


def delete_why(who: Who, sid: str) -> str:
    """能不能删（和恢复）这个会话；空字符串就是能。删了所有人都看不到，所以只有主人和查看器账号能删，
    无主会话谁都能用、认不出该由谁做主，只有查看器账号能删。单用户实例里打开的人就是查看器账号。"""
    if who.admin:
        return ""
    owner = owner_of(sid)
    if owner is None:
        return "仅查看器账号可删除无主会话"
    return "" if who.name == owner else f"这是 {owner} 的会话，只有 {owner} 和查看器账号能删"


def deleted_why(sid: str) -> str:
    """打开、订阅一个删掉的会话时怎么说。"""
    rec = deleted.get(sid) or {}
    at = time.strftime("%m-%d %H:%M", time.localtime(rec["ts"])) if rec.get("ts") else "不知道什么时候"
    return f"该会话已由 {rec.get('by') or '未知用户'} 删除（{at}），会话主人和查看器账号可在全部会话底部的「已删除」中恢复"


def set_deleted(who: Who, sid: str, on: bool):
    """删除（on）或恢复。没存下来就撤回内存里的改动，免得页面上删掉了、重启后又冒出来。"""
    if why := delete_why(who, sid):
        raise HTTPException(403, why)
    with _lock:
        old = deleted.get(sid)
        if on:
            deleted[sid] = {"by": who.name or GUEST_NAME, "ts": time.time()}
        else:
            deleted.pop(sid, None)
        try:
            _save(DELETED_FILE, deleted)
        except HTTPException:
            if old is None:
                deleted.pop(sid, None)
            else:
                deleted[sid] = old
            raise


# ---------- 限量号 ----------

# 余额降到这个数（点）或更低，记在他头上的限量号就不能再用了，等查看器账号发额度
OVERDRAFT = 0


def paid_why(who: Who, sid: str | None = None, blue_on=None, account: str | None = None) -> str:
    """限量号现在为什么不能用；空字符串就是能用。sid 为 None 是新建会话（建出来就是自己的）。
    要登录（单用户实例总是登录着的）；已有的会话还得有主人；花费记在谁头上（会话主人，新建的就是自己），
    谁的余额就不能透支到 OVERDRAFT。单用户实例只有一个人，不按余额拦。
    blue_on(付钱的人) 为真：他在这个号上还有预定的蓝，用蓝不扣钱，余额不够也能用（用完蓝就由预测拦截停下）。
    给了 account、付钱的人又是这个号的号主，也不按余额拦：花的钱又流回他自己（earnings）。"""
    if who.name is None:
        return "请登录后使用"
    payer = who.name if sid is None else owner_of(sid)
    if payer is None:
        return "无主会话无法使用限量号（花费无人承担），请联系管理员为其指定主人"
    if account and instance.remote_billed(account):
        return "" if payer == instance.REMOTE[account].get('user') else "这个远端账号仅供指定用户使用"
    if account and instance.owns(payer, account):
        return ""
    if MULTI:
        balance = cached_balance(payer)
        if balance <= OVERDRAFT and not (blue_on and blue_on(payer)):
            whose = "您的" if payer == who.name else f"会话主人 {payer} 的"
            # 点只是存账的单位，给人看的一律换成元（和账本一样；1 点 = reserve.FEN 分）
            return (f"{whose}余额为 {balance * reserve.FEN / 100:.2f} 元，降至 {OVERDRAFT * reserve.FEN / 100:g} 元后无法继续使用限量号，"
                    "请联系管理员发放额度")
    return ""


def paid_ok(who: Who, sid: str | None = None) -> bool:
    return not paid_why(who, sid)


def paid_block(who: Who, sid: str | None, account: str, blue_on=None) -> str:
    """用这个号会不会被拦、为什么；空字符串就是不拦。blue_on 见 paid_why。"""
    if instance.remote_billed(account):
        payer = who.name if sid is None else owner_of(sid)
        return '' if payer is not None and payer == instance.REMOTE[account].get('user') else '这个远端账号仅供指定用户使用'
    if account not in PAID:
        return ""
    why = paid_why(who, sid, blue_on, account)
    return f"{account} 是限量号，{why}" if why else ""


# ---------- 账本 ----------

# 算账排队：多用户实例重启后内存里的缓存全空，三十多个页面同时打开，每个请求（看身份、取设置、看额度、全部会话）
# 都在接口线程里从头算整本账，同一个会话被好几个线程同时拆、同一个人的账同时算好几份，Python 同一时刻只有一个线程在算，
# 总时间是几十份相加；接口线程池（默认 40 个）也被占满，首页、推送都排在后面，卡了近四分钟。所以：
# - 同一样东西（一个会话按同样依据拆、一个人的账）正在算，后来的就等这一份，不另算（_flights）；
# - 真正去算的放进只有 HEAVY_WORKERS 个线程的算账线程池：同时在算的少，事件循环抢得到 CPU，先来的先算完；
# - 接口线程只是等，不占 CPU；查看器把接口线程池放大了（server.lifespan），等着的不会挤掉首页这些轻请求。
# 不排队、当场自己算的情况（_heavy）：事件循环自己（不能让它等）；已经在算账线程里（再排队等自己会卡死）；
# 拿着账本要用到的锁（下单时 reserve.book 拿着预定的锁查余额）：排队的那份要拿这把锁，等它就是互相等死。
HEAVY_WORKERS = 2
_pool = ThreadPoolExecutor(max_workers=HEAVY_WORKERS, thread_name_prefix="ledger")
_flights: dict = {}
_flights_lock = threading.Lock()


def _heavy(key, fn):
    """fn() 的结果：同一个 key 正在算就等它，不然交给算账线程池算，自己等着。几种情况当场自己算，见上面。"""
    import isolation
    t = threading.current_thread()
    if (t is threading.main_thread() or t.name.startswith("ledger")
            or _lock._is_owned() or reserve._lock._is_owned() or isolation._lock._is_owned()):
        return fn()
    with _flights_lock:
        fut = _flights.get(key)
        if fut is None:
            fut = _flights[key] = _pool.submit(_flight, key, fn)
    return fut.result()


def _flight(key, fn):
    try:
        return fn()
    finally:
        with _flights_lock:
            _flights.pop(key, None)   # 算完了，再来的按那时的依据重新排（多半直接命中缓存）


def refresh_calls():
    """把调用记录新增的行读进来。后台循环每一圈调一次。"""
    api_calls.INDEX.refresh()


def call_accounts(sid: str) -> dict[str, str]:
    """这个会话里记下的每条回复是哪个号发的：{回复编号: 号}。"""
    return api_calls.INDEX.accounts_of(sid)


def call_count(sid: str) -> int:
    return api_calls.INDEX.count(sid)


def touched_since(path: Path, since: float, snap: calls.Snapshot | None = None) -> bool:
    """会话（连同子代理）的文件在这个时刻以后写过没有。没写过的，这段时间里不可能有调用，不用逐条去看；
    子代理干活时主会话的文件可以很久不动，所以子代理的文件也要看。给了清点结果（calls.snapshot）就用里面的修改时间。"""
    for p in calls.files_of(path, snap):
        hit = snap.stats.get(str(p)) if snap is not None and snap.has(path) else None
        if hit is not None:
            if hit[1] >= since:
                return True
            continue
        with contextlib.suppress(OSError):
            if p.stat().st_mtime >= since:
                return True
    return False


def _paid_then(acc: str, ts) -> bool:
    """这次调用发出时，这个号是不是已经是限量号：免费号改成限量号（instance.PAID_SINCE）以前在它上面的调用不进账本。
    会话记录里没有时间的，按已经是限量号算（取不到不能当成没花）。"""
    start = PAID_SINCE.get(acc)
    return not start or not ts or api_calls.stamp(ts) >= start


def own_calls(path: Path | str) -> dict[str, dict]:
    """这个会话名下的调用（回复编号 → {model, ts, use}）：分叉续接的会话抄过来的历史不算，算在原来那个会话名下。
    总消耗、账本、上下文卡片的额度视角都按它，几处的数才对得上。"""
    sid, idx = sid_of(path), api_calls.INDEX
    return {mid: c for mid, c in calls.session_calls(path).items() if not ((o := idx.owner(mid)) and o != sid)}


# 文件与运行身份 → (依据, 这个会话名下的调用连同时间和号)，不跨身份复用。
# 依据是会话和子代理每个文件的大小（文件只往后追加，大小不变内容就不变）和调用记录索引读到哪儿（谁名下、哪个号都从它查）；
# 两样都没变，结果就和重新算一遍一样。总览每次打开都要给几百张卡片算，不缓存的话光是逐条解析时间、查号就要一两秒
_owned: dict[tuple, tuple[tuple, list]] = {}


def _own(path: Path | str, snap: calls.Snapshot | None = None) -> list[tuple[str, dict, float | None, str | None]]:
    """own_calls 的每一条连同调用时间（会话记录里没写的是 None）和哪个号发的：[(回复编号, 调用, 时间, 号)]，顺序同 own_calls。"""
    path = calls.Path(path)
    idx = api_calls.INDEX
    sizes = calls.file_sizes(path, snap)
    key = (tuple((str(p), s) for p, s in sizes), idx.gen, idx.off, platform_costs.version())   # 先取依据再算：算到一半索引变了，存下的依据就是旧的，下次自然对不上
    identity = (str(path), getattr(path, "run_as", None))
    hit = _owned.get(identity)
    if hit and hit[0] == key:
        return hit[1]
    sid = sid_of(path)
    out = [(mid, c, api_calls.stamp(c["ts"]) if c["ts"] else None, idx.account(mid))
           for mid, c in calls.calls_of(sizes).items() if not ((o := idx.owner(mid)) and o != sid)]
    _owned[identity] = (key, out)
    return out


# 会话与运行身份 → (依据, 逐条拆好的调用, 全部时间的 spending 结果)，不跨身份复用。
# 总览每次打开都给每张卡片算总消耗：查看器账号四百多张卡片、七万多次调用，每次都逐条查特惠免了几成、预定付了几成再按模型加，
# 热的时候也要 0.7 秒，可绝大多数会话根本没变。依据是：会话和子代理每个文件的大小（新调用写进来就变）、调用记录索引的代次和
# 这个会话记下的条数（哪个号发的、算在谁名下都从它查；不用全局读到哪儿，不然任何会话来一次调用就全部作废）、开始记调用的时刻、
# 这个会话分给预定或记下特惠免了几成的次数（reserve.settled_n）、自动特惠时段的版本。都没变，拆出来的就和重新拆一遍一样
_spent: dict[tuple, tuple[tuple, list, dict]] = {}


def _spent_key(path: Path, snap: calls.Snapshot | None) -> tuple:
    sid, idx = sid_of(path), api_calls.INDEX
    return (tuple((str(p), s) for p, s in calls.file_sizes(path, snap)), idx.gen, idx.count(sid), idx.start,
            reserve.settled_n.get(sid, 0), reserve.promo_version(), api_usage.version(sid), platform_costs.version())


def _split_rows(path: Path | str, snap: calls.Snapshot | None = None) -> tuple[list, dict]:
    """这个会话名下的每次调用拆成 (时间, 调用, 进账本的那份或 None, 其中预定付掉的那份或 None, 是不是查不到号按限量号算的, 回复编号)，
    连同全部时间的 spending 结果；按 _spent 的依据缓存。"""
    path = calls.Path(path)
    key = _spent_key(path, snap)   # 先取依据再拆：拆到一半变了，存下的依据就是旧的，下次自然对不上
    identity = (str(path), getattr(path, "run_as", None))
    hit = _spent.get(identity)
    if hit and hit[0] == key:
        return hit[1], hit[2]
    return _heavy(("split", identity, key), lambda: _split_rows_now(path, snap, identity, key))


def _split_rows_now(path: Path, snap: calls.Snapshot | None, identity: tuple, key: tuple) -> tuple[list, dict]:
    hit = _spent.get(identity)   # 排队的时候别人可能已经按同样的依据拆好了
    if hit and hit[0] == key:
        return hit[1], hit[2]
    own = _own(path, snap)
    # 回环多看到的后台辅助调用也要进账本，和外部API同一套算法（见 api_usage.billable_for）。
    own += [(mid, c, c["ts"], acc) for mid, c, acc in api_usage.extra(sid_of(path), {m for m, *_ in own})]
    rows = _charge_rows(own)
    total = _sum_rows(rows)
    _spent[identity] = (key, rows, total)
    return rows, total


def _charge_rows(own):
    """网页和外部API共用计价、特惠、预定分摊规则，保留整批读取的性能优化。"""
    idx, rows = api_calls.INDEX, []
    billed = [acc in PAID and _paid_then(acc, c["ts"]) and mid not in idx.remote for mid, c, _, acc in own]
    got = iter(reserve.shares([(mid, acc, t) for (mid, _, t, acc), b in zip(own, billed) if b]))
    for (mid, c, t, acc), b in zip(own, billed):
        paid = blue = None
        unknown = False
        if b:
            free, fr = next(got)
            paid = c
            if free > 0:
                paid = {**c, "use": {k: v * (1 - free) for k, v in c["use"].items()}}
            if fr > 0:
                blue = {**paid, "use": {k: v * fr for k, v in paid["use"].items()}}
        elif acc is None and PAID and idx.recorded(c["ts"]):
            paid, unknown = c, True
        rows.append((t, c, paid, blue, unknown, mid))
    return rows


def api_rows(sid, since=None):
    return _charge_rows([(mid, c, c["ts"], api_calls.INDEX.account(mid))
                         for mid, c in api_usage.session(sid).items()
                         if since is None or c["ts"] >= since])


def _sum_rows(rows: list) -> dict:
    return {"all": calls.by_model([r[1] for r in rows]), "paid": calls.by_model([r[2] for r in rows if r[2] is not None]),
            "blue": calls.by_model([r[3] for r in rows if r[3] is not None]), "unknown": sum(r[4] for r in rows),
            "remote": calls.by_model([r[1] for r in rows if r[5] in api_calls.INDEX.remote])}


def display_paid(sp):
    """远端已扣的金额只合进会话展示，不能写回本地账本的 paid。"""
    paid = {model: dict(use) for model, use in sp['paid'].items()}
    for model, use in sp.get('remote', {}).items():
        total = paid.setdefault(model, {})
        for key, value in use.items():
            total[key] = total.get(key, 0) + value
    return paid


def add_spending(a: dict, b: dict) -> dict:
    """两份 spending 的结果加在一起：会话卡片和底部的花费把从它派出去的工作流任务也算进来（server.spend_fields）。
    只用于展示，账本照旧按各自的会话记一次"""
    out = {}
    for k in ("all", "paid", "blue", "remote"):
        m = {model: dict(use) for model, use in a.get(k, {}).items()}
        for model, use in b.get(k, {}).items():
            tot = m.setdefault(model, {})
            for kk, v in use.items():
                tot[kk] = tot.get(kk, 0) + v
        out[k] = m
    out["unknown"] = a.get("unknown", 0) + b.get("unknown", 0)
    return out


def _split(path: Path | str, since: float | None = None, snap: calls.Snapshot | None = None) -> tuple[list, list, list, int]:
    """这个会话名下的调用（mine）、其中进账本的（paid）、进账本的里面由预定付掉的那几成（blue，用量按 reserve.frac 折过）、
    查不到号按限量号算的有几次。since 不为 None 时只要这个时刻以后发的（按会话记录里这次调用的时间）。
    snap 是总览一次清点好的文件大小和修改时间（calls.snapshot），不给就一个个去查。"""
    if since is not None and not touched_since(calls.Path(path), since, snap):
        return [], [], [], 0
    rows, _ = _split_rows(path, snap)
    if since is not None:
        rows = [r for r in rows if r[0] is not None and r[0] >= since]
    return ([r[1] for r in rows], [r[2] for r in rows if r[2] is not None], [r[3] for r in rows if r[3] is not None],
            sum(r[4] for r in rows))


def spending(path: Path | str, since: float | None = None, snap: calls.Snapshot | None = None) -> dict:
    """这个会话花了多少：all 是全部，paid 是其中进账本的（限量号发的，加上开始记调用以后查不到号的），
    blue 是 paid 里由预定付掉、不再扣钱的那部分（界面上写成蓝的），
    格式都是 {模型: {in, cw, cr, out, n}}，前端换算成点数；unknown 是查不到号、按限量号算了的有几次。
    一次调用只算在第一次记下它的会话里：分叉续接的会话抄过来的历史，算在原来那个会话名下（见 api_calls.py）。
    since 不为 None 就只算这个时刻以后的（按最近 N 分钟用量排序时用）。snap 见 _split。
    全部时间的直接给缓存里加好的（见 _spent），最近 N 分钟的从缓存里拆好的调用按时间挑出来加"""
    if since is None:
        return dict(_split_rows(path, snap)[1])
    if not touched_since(calls.Path(path), since, snap):
        return _sum_rows([])
    return _sum_rows([r for r in _split_rows(path, snap)[0] if r[0] is not None and r[0] >= since])


def all_sessions_snapshot() -> calls.Snapshot:
    """所有会话根目录（共用环境和每个独立环境）的一次清点：余额看板、用户管理要给每个人算账，各自开头清点一次往下传，
    找文件、查大小、看最近改没改都从这里拿，不再经小助手一个个去问（不然余额看板卡几十秒）"""
    import isolation
    return calls.snapshot(list(isolation.roots(internal=True)))


def _main_files(root, snap: calls.Snapshot | None):
    """一个根目录底下的主会话文件：清点成了从清点里拿，没清点成照旧列目录"""
    mains = snap.mains.get(str(root)) if snap is not None else None
    if mains is not None:
        yield from (root / rel for rel in mains.values())
        return
    for d in root.iterdir():
        if d.is_dir():
            yield from d.glob("*.jsonl")


def recent_by_owner(since: float, snap: calls.Snapshot | None = None) -> dict[str | None, dict]:
    """这个时刻以后每个人名下会话花了多少（主人 → {all, paid, blue}，格式同 spending），无主会话记在 None 下；
    bought 是这段时间下单付的预定（点数，乘过收钱比例，和前端的单价一个单位），这段时间只下了单、没用过的人也列出来。
    不只看账本里那些会话：免费号花的也算，查看器账号要看的是谁在用、用了多少。snap 见 all_sessions_snapshot。"""
    refresh_calls()
    lists: dict[str | None, tuple[list, list, list]] = {}
    import isolation
    for root in isolation.roots(internal=True):
        for p in _main_files(root, snap):
            mine, paid, blue, _ = _split(p, since, snap)
            if mine:
                got = lists.setdefault(owner_of(p.stem), ([], [], []))
                got[0].extend(mine)
                got[1].extend(paid)
                got[2].extend(blue)
    for sid, owner in api_usage.external_sessions().items():
        rows = api_rows(sid, since)
        got = lists.setdefault(owner, ([], [], []))
        got[0].extend(r[1] for r in rows)
        got[1].extend(r[2] for r in rows if r[2] is not None)
        got[2].extend(r[3] for r in rows if r[3] is not None)
    out = {o: {"all": calls.by_model(m), "paid": calls.by_model(pd), "blue": calls.by_model(b), "bought": 0.0}
           for o, (m, pd, b) in lists.items()}
    for user, pts in reserve.bought_since(since, rates.price_of).items():
        out.setdefault(user, {"all": {}, "paid": {}, "blue": {}})["bought"] = pts
    return out


_cost: dict[tuple, tuple[tuple, tuple]] = {}   # 文件与运行身份 → (依据, 结果)，不跨身份复用
_cost_table: dict | None = None


def _cost_key(path: Path, snap: calls.Snapshot | None = None) -> tuple:
    """会话和子代理的大小、调用归属及预定结算不变，就不用重算花费。给了清点结果就从里面拿大小（查不了的记 -1，同以前）。"""
    sizes = [(str(p), -1 if s is None else s) for p, s in calls.file_sizes(path, snap)]
    sid, idx = sid_of(path), api_calls.INDEX
    return tuple(sizes), idx.count(sid), idx.start, reserve.settled_n.get(sid, 0), reserve.promo_gen, platform_costs.version()   # 自动特惠的时段变了也要重算


def session_cost(path: Path, table: dict, snap: calls.Snapshot | None = None) -> tuple[float, float, int, int]:
    """按会话与运行身份缓存；沿用已有的余额性能优化，不混用不同用户的结果。snap 见 all_sessions_snapshot。"""
    global _cost_table
    if table != _cost_table:
        _cost.clear()
        _cost_table = table
    key = _cost_key(path, snap)
    identity = (str(path), getattr(path, "run_as", None))
    hit = _cost.get(identity)
    if hit and hit[0] == key:
        return hit[1]
    out = _session_cost(path, table, snap)
    _cost[identity] = (key, out)
    return out


def _session_cost(path: Path, table: dict, snap: calls.Snapshot | None = None) -> tuple[float, float, int, int]:
    return _cost_of_spending(spending(path, snap=snap), table)


def _cost_of_spending(sp, table):
    points, covered, unrated = 0.0, 0.0, 0
    for model, u in sp["paid"].items():
        r = rates.rate(table, model)
        if r:
            points += rates.cost(table, {"model": model, "use": u})
            if b := sp["blue"].get(model):
                covered += rates.cost(table, {"model": model, "use": b})
        else:
            unrated += u["n"]
    return points, covered, unrated, sp["unknown"]


# ---------- 号主 ----------

# 号主（instance.OWNERS）：谁在一个号上花了钱，同样的数记进这个号的号主的收入。
# 号主自己用，先扣后收、结果是零，「自己用免费」是这样自然算出来的，不是一条规则；界面上的红蓝、预定、拦截都照旧。

SETTLE = 600   # 调用记下以后过了这么久，token 数早已写全：会话在调用记录里有更新的调用就先不缓存

# 会话编号 → (依据, 回复编号 → 照常扣的钱)。依据是调用记录里记在它名下的条数、分给预定或记下特惠免了几成的次数、
# 自动特惠时段的版本，都没变扣的钱就没变：不用为了确认这一点去查文件大小，查看器账号是大多数号的号主，
# 几百个会话每个都经小助手问一遍要好几秒
_takings: dict[str, tuple[tuple, dict[str, float]]] = {}
_takings_table: dict | None = None


def _taken(sid: str, table: dict, snap: calls.Snapshot | None, settled: bool) -> dict[str, float] | None:
    """这个会话里每次调用照常扣了多少钱（进账本的减去预定付掉的，点数；不进账本的是 0）：回复编号 → 点数。会话文件找不到是 None。
    settled：这个会话在调用记录里最近一次调用也是 SETTLE 以前的，算出来的才缓存"""
    global _takings_table
    if sid.startswith("api-"):
        return {r[5]: rates.cost(table, r[2]) - (rates.cost(table, r[3]) if r[3] else 0.0)
                if r[2] else 0.0 for r in api_rows(sid)}
    if table != _takings_table:
        _takings.clear()
        _takings_table = table
    key = (api_calls.INDEX.count(sid), reserve.settled_n.get(sid, 0), reserve.promo_version(), platform_costs.version())   # 先取依据再算，理由同 _split_rows
    hit = _takings.get(sid)
    # 找不到的也记着：不带清点时每次去找，找不到就要把整个环境重列一遍（calls.find），号主收入一算就是几十个这样的会话。
    # 带了清点就照旧再找，从清点里找很便宜，文件回来了也认得出
    if hit and hit[0] == key and (hit[1] is not None or snap is None):
        return hit[1]
    try:
        path = calls.find_in(snap, sid)
    except (OSError, ValueError) as e:
        print(f"号主收入：会话 {sid} 读不了：{e}", flush=True)
        path = None
    if not path:
        _takings[sid] = (key, None)
        return None
    rows, _ = _split_rows(path, snap)
    out = {r[5]: rates.cost(table, r[2]) - (rates.cost(table, r[3]) if r[3] else 0.0) if r[2] else 0.0 for r in rows}
    if settled:
        _takings[sid] = (key, out)
    return out


def earnings(name: str, snap: calls.Snapshot | None = None) -> tuple[float, int]:
    """号主的收入（点数，收钱用的单价）：他是号主的那些号从起算时刻以后，所有人照常扣的钱，加上所有人在这些号上下预定付的钱。
    和付钱的人账本里扣掉的是同一份，单价改了、预定撤销了都跟着变。
    第二个数是算不进来的调用有几次：会话文件找不到，不知道扣了多少（取不到不能当成没扣钱）。
    调用记录里有、会话记录里却没有的调用（实际数出过几百次）付钱的人那边也没扣，号主这边同样不算，两边才对得上。snap 见 ledger。"""
    mine = {acc: since for acc, (user, since) in OWNERS.items() if user == name}
    if not mine:
        return 0.0, 0
    table, old = rates.load(price=True), time.time() - SETTLE
    by_sid: defaultdict = defaultdict(list)
    last: dict = {}   # 会话 → 调用记录里它最近一次调用的时刻（哪个号的都算）：决定能不能缓存
    for ts, mid, acc, sid in list(api_calls.INDEX.order):
        last[sid] = ts
        if acc in mine and ts >= mine[acc]:
            by_sid[sid].append(mid)
    earned, missing = 0.0, 0
    for sid, mids in by_sid.items():
        took = _taken(sid, table, snap, last[sid] < old) if sid else None
        if took is None:
            missing += len(mids)
        else:
            earned += sum(took.get(mid, 0.0) for mid in mids)
    for acc, since in mine.items():
        earned += sum(reserve.charged(b) for b in reserve.sold(acc) if b["ts"] >= since) * rates.price_of(acc)
    return earned, missing


def set_reserve_open(who: Who, account: str, on: bool):
    """号主开关别人在这个号上下新的预定：只有号主自己能开关，查看器账号也不能替别人开关。
    关了只挡别人打开预定面板、下新的单（server.reserve_ctx），号主自己照常订；已有的预定照旧占额度、付调用，一批都不动。
    没存下来就撤回内存里的改动，免得页面上关了、重启后又开着。"""
    if not instance.owns(who.name, account):
        raise HTTPException(403, f"仅 {account} 的号主可开启或关闭预定")
    with _lock:
        old = reserve_closed.get(account)
        if on:
            reserve_closed.pop(account, None)
        else:
            reserve_closed[account] = {"by": who.name, "ts": time.time()}
        try:
            _save(RESERVE_CLOSED_FILE, reserve_closed)
        except HTTPException:
            if old is None:
                reserve_closed.pop(account, None)
            else:
                reserve_closed[account] = old
            raise


def grant(who: Who, name: str, amount: float, note: str) -> dict:
    """查看器账号发额度（负数就是扣回来），一笔一行追加。"""
    need_admin(who)
    if name not in names():
        raise HTTPException(404, f"用户「{name}」不存在")
    if not math.isfinite(amount) or not amount:
        raise HTTPException(400, "请输入非零额度")
    rec = {"ts": time.time(), "user": name, "amount": round(amount, 4), "note": note.strip()[:200], "by": who.name}
    with _lock:
        try:
            with open(GRANTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            raise HTTPException(500, f"无法写入 {GRANTS_FILE.name}：{e}")
        grants.append(rec)
    forget_balance(name)
    return rec


# ---------- 额度计划 ----------
# 号主贡献一个号，查看器账号按月给他一笔额度（比如 178 元）。一次发完他很快就用光，别人一阵子缺号，所以分几期发：
# 从开始那一刻发第一期，之后每隔几天到期一期，默认 4 期、每 7 天一期，一个月发完。期数和间隔接口上能改，界面上只填总额和开始日期。
# 开始时刻可以是以前的某一天（比如从他的号上来的那天算起），已经到期的几期建好就算发了。
# 到期不靠定时任务：计划只记一次（PLANS_FILE），账本算账时把已经到期的那几期当成收到的额度并进去（plan_grants），
# 到点自然就进了余额，实例停着、重启都不会漏发或重发。
# 撤销就是当作这个计划没发生过（和别的撤销一个逻辑）：已经发出去的几期一起收回，记录留着、标上撤销。
PLAN_PARTS, PLAN_DAYS = 4, 7


def _plan_items(p: dict) -> list[dict]:
    """计划的每一期：第几期、到期时刻、金额。按总额均分，最后一期补齐零头，几期加起来正好是总额。"""
    n = p["parts"]
    each = round(p["total"] / n, 4)
    return [{"i": i + 1, "due": p["start"] + i * p["days"] * 86400,
             "amount": each if i < n - 1 else round(p["total"] - each * (n - 1), 4)} for i in range(n)]


def plan_grants(name: str, now: float | None = None) -> list[dict]:
    """名下计划里已经发出去的各期，写成和 grants.jsonl 一样的一笔笔（多 plan、part、parts 三个字段），账本把它们和直接发的并在一起。"""
    now = time.time() if now is None else now
    with _lock:
        mine = [(pid, dict(p)) for pid, p in plans.items() if p["user"] == name]
    out = []
    for pid, p in mine:
        if p.get("revoked"):
            continue   # 撤销了就当没发生过，发过的几期也不算
        for it in _plan_items(p):
            if it["due"] <= now:
                out.append({"ts": it["due"], "user": name, "amount": it["amount"], "note": p["note"], "by": p["by"],
                            "plan": pid, "part": it["i"], "parts": p["parts"]})
    return out


def plan_view(pid: str, p: dict, now: float | None = None) -> dict:
    """给界面的一份计划：每一期发了没有（done）、撤销掉了没有（dropped，撤销了就每期都是），已经发了多少（given）。"""
    now = time.time() if now is None else now
    rv = bool(p.get("revoked"))
    items = [{**it, "done": it["due"] <= now and not rv, "dropped": rv} for it in _plan_items(p)]
    return {"id": pid, "user": p["user"], "total": p["total"], "parts": p["parts"], "days": p["days"], "start": p["start"],
            "note": p["note"], "by": p["by"], "revoked": p.get("revoked"), "items": items,
            "given": round(sum(it["amount"] for it in items if it["done"]), 4)}


def plans_of(name: str, current: bool = False) -> list[dict]:
    """某人的计划，新的在前。current 是账号页上给本人看的：撤销了的不给（已经发的照旧在账本里），
    发完以后再留一期的工夫（最后一期之后再过一个间隔）就不再显示。"""
    now = time.time()
    with _lock:
        mine = [(pid, dict(p)) for pid, p in plans.items() if p["user"] == name]
    out = []
    for pid, p in mine:
        if current and (p.get("revoked") or now > p["start"] + p["parts"] * p["days"] * 86400):
            continue
        out.append(plan_view(pid, p, now))
    out.sort(key=lambda v: -v["start"])
    return out


def grant_plan(who: Who, name: str, total: float, note: str, parts: int = PLAN_PARTS, days: float = PLAN_DAYS,
               start: float | None = None) -> dict:
    """查看器账号建一个额度计划：total 分 parts 期，从 start（不给就是现在）发第一期，之后每 days 天到期一期。
    start 可以是以前，到期了的几期建好就算发了。只能是正数，扣钱用一次发放填负数。"""
    need_admin(who)
    if name not in names():
        raise HTTPException(404, f"用户「{name}」不存在")
    if not math.isfinite(total) or total <= 0:
        raise HTTPException(400, "请输入大于零的总额")
    if not 1 <= parts <= 52:
        raise HTTPException(400, "期数须在 1 到 52 之间")
    if not math.isfinite(days) or not 0 < days <= 366:
        raise HTTPException(400, "间隔天数须大于 0 且不超过 366")
    now = time.time()
    if start is not None and (not math.isfinite(start) or abs(start - now) > 366 * 86400):
        raise HTTPException(400, "开始日期须在一年以内")
    rec = {"user": name, "total": round(total, 4), "parts": parts, "days": days, "start": now if start is None else start,
           "note": note.strip()[:200], "by": who.name, "ts": now}
    with _lock:
        pid = secrets.token_hex(6)
        plans[pid] = rec
        try:
            _save(PLANS_FILE, plans)
        except HTTPException:
            plans.pop(pid, None)   # 没存下来就当没建，免得界面上有、重启后没了
            raise
    forget_balance(name)
    return plan_view(pid, rec, now)


def revoke_plan(who: Who, pid: str) -> dict:
    """撤销计划：当作没发生过，已经发了的几期一起收回，还没到期的不再发；记录留着、标上谁什么时候撤的。没存下来就撤回内存里的改动。"""
    need_admin(who)
    with _lock:
        p = plans.get(pid)
        if not p:
            raise HTTPException(404, "这个额度计划不存在")
        if p.get("revoked"):
            raise HTTPException(400, "这个额度计划已经撤销过了")
        now = time.time()
        p["revoked"] = {"ts": now, "by": who.name}
        try:
            _save(PLANS_FILE, plans)
        except HTTPException:
            p.pop("revoked", None)
            raise
        view = plan_view(pid, p, now)
    forget_balance(p["user"])
    return view


def ledger(name: str, snap: calls.Snapshot | None = None) -> dict:
    """一个人的账：收到的每一笔、号主收入（earned，见 earnings）、名下每个会话进账本花了多少、付的预定、余额。没有限量号的实例不记账。
    名下会话的文件找不到的，列在 lost 里（取不到不能当成没花）。
    预定额度（reserve.py）：下单时一次付清（reserved），之后由预定的蓝付掉的那部分调用不再扣钱（covered，从花掉里减掉）。
    covered 按名下各会话逐次调用算（这次调用有几成由预定付了 × 它现在按单价算的花费），和花费同一份调用、同一份单价：
    单价重新拟合、会话换了主人、号后来才改成限量号，减掉的都跟着花费走，不会多减或少减。
    snap：一次清点好的所有会话文件（all_sessions_snapshot），给了就从里面找文件、查大小，不给就一个个去问，结果一样。
    同一个人的账正在算就等那一份（_heavy）：等到的和自己算是同一刻的账，只是开始算的时刻早一点（最多早算一本账的工夫）；
    发额度、下单、撤单以后（forget_balance）换一代，之后来的不会等到那以前开始算的那份。每人拿到的是各自的一份拷贝，改了不影响别人"""
    key = ("ledger", name, _ledger_gen.get(name, 0), platform_costs.version())
    return copy.deepcopy(_heavy(key, lambda: _ledger_now(name, snap)))


_ledger_gen: dict[str, int] = {}   # 人 → 第几代账（forget_balance 加一）


def _ledger_now(name: str, snap: calls.Snapshot | None) -> dict:
    refresh_calls()
    table = rates.load(price=True)   # 收钱用的单价（乘过 rates.PRICE，GPT 模型再打折）
    with _lock:
        mine = [g for g in grants if g["user"] == name]
        sids = [sid for sid, rec in owners.items() if rec["owner"] == name] if PAID else []
    mine = sorted(mine + plan_grants(name), key=lambda g: g["ts"])   # 额度计划已经到期的各期，和直接发的按时间排在一起
    sessions, lost = [], []
    for sid in sids:
        try:
            path = calls.find_in(snap, sid)
        except (OSError, ValueError) as e:
            # 会话所在的独立环境读不了（比如系统用户的登记没了），或者环境已经删掉、会话登记还指着它
            # （isolation.for_sid 报登记不一致，比如删掉重建过）：和找不到一样列进 lost、日志里写明原因，
            # 不能让一个环境出错把整本账连同确认身份一起拖垮
            print(f"账本：{name} 的会话 {sid} 读不了：{e}", flush=True)
            path = None
        if not path:
            lost.append(sid)
            continue
        points, cov, unrated, unknown = session_cost(path, table, snap)
        if points or unrated:
            sessions.append({"sid": sid, "points": points, "covered": cov, "unrated": unrated, "unknown": unknown})
    for sid in api_usage.external_sessions(name):
        points, cov, unrated, unknown = _cost_of_spending(_sum_rows(api_rows(sid)), table)
        if points or unrated:
            sessions.append({"sid": sid, "source": "api", "points": points, "covered": cov,
                             "unrated": unrated, "unknown": unknown})
    sessions.sort(key=lambda s: -s["points"])
    covered = sum(s["covered"] for s in sessions)   # table 已经乘过 rates.PRICE
    batches = reserve.paid_batches(name)
    # 预定记的是按单价算的点数，收钱时和花费一样乘收钱比例（GPT 号再打折，rates.price_of；价格改了，以前的预定也按新价格算）
    # 查看器账号撤销了的批只收没退的那部分（reserve.charged）
    reserved = sum(reserve.charged(b) * rates.price_of(b["account"]) for b in batches)
    granted, spent = sum(g["amount"] for g in mine), sum(s["points"] for s in sessions) - covered
    earned, unearned = earnings(name, snap)
    return {"name": name, "granted": granted, "earned": earned, "unearned": unearned, "spent": spent,
            "balance": granted + earned - spent - reserved,
            "covered": covered, "reserved": reserved, "batches": batches[::-1],
            "unrated": sum(s["unrated"] for s in sessions), "unknown": sum(s["unknown"] for s in sessions),
            "lost": lost, "grants": mine[::-1], "sessions": sessions}


_bal: dict[str, tuple[float, dict]] = {}   # 人 → (算的时刻, {余额 balance, 累计花掉 spent})


def _brief(led: dict) -> dict:
    """整本账里 cached_balance、cached_spent 要的两个数。花掉和账本页一样，算上下预定付的"""
    return {"balance": led["balance"], "spent": led["spent"] + led["reserved"]}


def _cached(name: str) -> dict:
    hit = _bal.get(name)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    b = _brief(ledger(name))
    _bal[name] = (time.time(), b)
    return b


def cached_balance(name: str) -> float:
    """余额，10 秒内沿用上次算的：能不能用限量号（打开页面时的设置接口、预测拦截的每个工具结果）都要问，账本整份重算太贵。
    发额度、下单付钱以后由 forget_balance 立刻清掉，不等 10 秒。"""
    return _cached(name)["balance"]


def cached_spent(name: str) -> float:
    """累计花掉多少（点，进账本的花费加下预定付的），和 cached_balance 同一份、同样 10 秒内沿用：
    设置里二次元没切过的人按它定开不开（server.used_features），打开页面时设置接口要问。"""
    return _cached(name)["spent"]


def forget_balance(name: str | None):
    _bal.pop(name, None)
    with _flights_lock:
        _ledger_gen[name] = _ledger_gen.get(name, 0) + 1


def me_info(who: Who) -> dict:
    """界面上那行身份：是谁、是不是查看器账号、余额（只在有限量号或收到过额度时才有意义，前端自己决定显不显示）。"""
    # promo：限时特惠（号 → {from, until, factor, label}），前端按它挂标签、算气泡上的花费；
    # autoPromo：自动特惠（号 → {factor, label, windows}，reserve.auto_promo_view），用法一样
    # 号主自己定的折扣（reserve.owner_promo_view）和限时特惠同一个样子，并进 promo 一起给，前端不用另外认
    promo = {a: list(ps) for a, ps in PROMO.items()}
    for a, segs in reserve.owner_promo_view().items():
        promo[a] = promo.get(a, []) + segs
    out = {"name": who.name, "admin": who.admin, "multi": MULTI, "stale": who.stale, "paid": sorted(PAID),
           "paidSince": PAID_SINCE, "guestName": GUEST_NAME, "overdraft": OVERDRAFT, "promo": promo,
           "autoPromo": reserve.auto_promo_view()}
    for label in ("promo", "autoPromo"):
        values = out[label]
        out[label] = {**values, **{name: values[e["account"]] for name, e in instance.API_ENTRIES.items()
                                  if e["account"] in values}}
    if who.name:
        led = ledger(who.name)
        _bal[who.name] = (time.time(), _brief(led))   # 刚算的整本账，顺手给 cached_balance、cached_spent 用
        # owns：他是号主的号，账号表上挂「我的」，这些号用起来不按余额拦（paid_why）
        # avatar：头像的版本，没传过是空字符串（见 set_avatar）；旧后端不给，前端就没有换头像这一项
        # plans：还在发的额度计划，账号页上画进度条（plans_of 的 current）；旧后端不给，前端就不画
        out.update(balance=led["balance"], unrated=led["unrated"], granted=led["granted"], newUser=is_new(who.name),
                   owns=sorted(a for a in instance.ACCOUNTS if instance.owns(who.name, a)), avatar=avatar(who),
                   plans=plans_of(who.name, current=True))
    return out


def board(since: float | None = None) -> list[dict]:
    """余额看板：谁都能看，查看器账号不列；按余额从多到少排，一样多按名字。透支到 OVERDRAFT 的标出来（限量号已经用不了）。
    带上注册时间，since 不为 None 时再带上每人这个时刻以后的用量（recent，同 roster），换别的排法由前端做。"""
    snap = all_sessions_snapshot()   # 一次清点，所有人的账都从这里找文件、查大小（见 all_sessions_snapshot）
    recent = recent_by_owner(since, snap) if since is not None else {}
    out = []
    for n in names():
        if n in ADMINS:
            continue
        led = ledger(n, snap)
        # 花掉的连付预定的钱一起算（下了单钱就花掉了），收到的连号主收入一起算，收到 − 花掉 = 余额
        out.append({"name": n, "ts": users.get(n, {}).get("ts"), "granted": led["granted"] + led["earned"], "balance": led["balance"],
                    "spent": led["spent"] + led["reserved"], "blocked": MULTI and led["balance"] <= OVERDRAFT,
                    **({"recent": recent.get(n, {"all": {}, "paid": {}, "blue": {}, "bought": 0.0})} if since is not None else {})})
    out.sort(key=lambda u: (-u["balance"], u["name"]))
    return out


def roster(since: float | None = None) -> dict:
    """查看器账号看的用户列表：每人的注册时间和账。since 不为 None 时每人再带上这个时刻以后的用量（recent，格式同 spending），
    无主会话的那份单独放在 orphan（单用户实例没有无主会话）。"""
    snap = all_sessions_snapshot()   # 同余额看板：一次清点往下传
    recent = recent_by_owner(since, snap) if since is not None else {}
    out, shared = [], shared_users()
    for n in names():
        led = ledger(n, snap)
        out.append({"name": n, "ts": users.get(n, {}).get("ts"), "admin": n in ADMINS or not MULTI, "newUser": is_new(n, shared),
                    **{k: led[k] for k in ("balance", "unrated")}, "granted": led["granted"] + led["earned"],   # 同余额看板
                    "spent": led["spent"] + led["reserved"],
                    "grants": led["grants"], "plans": plans_of(n),
                    "recent": recent.get(n, {"all": {}, "paid": {}, "blue": {}, "bought": 0.0})})
    return {"users": out, "orphan": recent.get(None, {"all": {}, "paid": {}, "blue": {}}) if MULTI else None}
