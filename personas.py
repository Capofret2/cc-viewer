"""人设卡：代替 Claude 那边的全局 CLAUDE.md，新建会话时把说明和人设卡注入系统提示词。

- 说明：instance.json 的 instructions 写一个文件，共用环境的网页会话每次拉起 CLI 都现读，
  接在 Claude Code 自带的系统提示词后面（chat.session_prompt）。独立环境不注入它：里面写的是共用账号的事。写了却读不到就拒绝启动，不当成没有。
- 人设卡：每人可以建几张，自己用、设成自己新建会话时默认选的、公开给大家用（别人只能选用，不能改）、改自己的、删自己的。
  默认是空的（不用人设卡）。新建会话时选一张，正文存一份快照（session_personas.json），这个会话以后拉起 CLI 都用这份：
  CLI 只在会话第一次请求和压缩以后才用新的系统提示词（实测：续接时追加的内容进不去，沿用第一次的），
  卡改了或撤回公开，已经开着的会话不受影响，压缩以后也还是建会话时选的那张。
- 给 AI 的接口：会话里执行 ccpersona（同目录的客户端），连到本机的 Unix 套接字。和 ssh 门卫一样靠内核报来的进程号认人：
  它在哪个网页对话的 CLI 底下，就是那个会话的主人在操作，只能动他自己的卡；无主会话、不在网页对话底下的一律不认。
- 删除只是撤下（记上 deleted），文件里留着；设成默认的卡被删、被撤回公开以后，默认就落空，页面上说明白。
- 补充资料：不进系统提示词，新建会话时写到运行 Claude 的那个用户家目录下按会话分开的目录里（folder），系统提示词里只列路径，
  Claude 要用时自己去读，不读不花钱，所以每份不设上限，只防误传：一张卡的资料加起来不超过 MAX_FILES_BYTES。
  全文按内容的指纹单独存在数据目录的 persona_files/ 里（personas.json 每改一次就整份重写，不能把几十 MB 的资料放进去），卡上只记名字和指纹。
- 钩子：完全照 Claude Code 自己 settings.json 里的 hooks 约定（时机 → [{matcher, hooks: [钩子]}]，
  command 钩子的命令、超时、输出和退出码规矩都一样），由 Claude Code 原生执行；只多一种 text 类型给不会写命令的人用：
  固定文字，choices 里的句子每次随机抽一句换掉文字里的「{随机}」（没写这个记号就接在后面），由代码目录的 persona_hook 现读现打。
  卡上还能挂几个脚本，命令里用 $CC_PERSONA_DIR/scripts/<脚本名> 找到它们（相当于文件夹钩子里的 $CLAUDE_PROJECT_DIR）。
  新建会话时钩子配置、脚本跟资料一起写进那个目录，每次拉起 CLI 由 launch_hooks 换成命令钩子、经启动参数 --settings 交给它，
  所以钩子挂在会话上，不挂在文件夹上；它在没信任过的文件夹里也照样执行，和文件夹自己的钩子叠在一起
  （实测过）。text 只开放实测过能附文字的四个时机，command 哪个时机都行。
- 用之前先问：命令和脚本会以会话的身份执行；正文、文字钩子超过提醒线（SOFT_*）照样能存，但每次请求都会多带很多字。
  这两样别人第一次用这张卡时要先看过、同意（按这一版记住，改了要重新同意；建会话时已经进了快照，卡主以后改也影响不到），
  卡主自己用不问。游客不能用带命令、脚本的卡（无主会话，出了事找不到人）；只是内容长的卡游客每次都要交同意的指纹（记不住游客）。
  钩子写坏了（拖慢、卡住、乱拦工具）只影响用了这张卡的会话，由用卡的人自担。
文件都在数据目录里，只给查看器自己读写（0600）：卡片里有没公开的内容。
"""
import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import struct
import threading
import time
import traceback
from pathlib import Path

from fastapi import HTTPException

import hostfs
import users
from instance import DATA, INSTRUCTIONS, PERSONA_SOCKET

# {"cards": {编号: {owner, name, text, files?: [{name, sha, size}], hooks?: {时机: [{matcher, hooks: [...]}]}, scripts?: [{name, text}],
#  public, ts, updated, deleted?}}, "defaults": {用户名: 编号}, "consents": {用户名: {编号: 同意过的那一版的指纹}}}
FILE = DATA / "personas.json"
BLOBS = DATA / "persona_files"          # 补充资料的全文：<指纹>.txt，只增不删（老会话、别人复制的卡可能还指着它）
# 会话编号 → {id, name, owner, text, files: [{name, path, size}], hooks, scripts: [脚本名], dir, ts}：建会话时选的那张卡的快照，
# 资料和脚本只记写到了哪里（dir）
SNAP = DATA / "session_personas.json"
MAX_NAME, MAX_FILE_NAME = 40, 80
# 正文和文字钩子每次请求都会发出去：超过提醒线照样能存，用的时候先问（见开头）；硬上限只防误操作，太大的系统提示词请求会直接失败
SOFT_TEXT, MAX_TEXT = 20000, 100000
TEXT_EVENTS = {"SessionStart": 5000, "UserPromptSubmit": 2000, "PreToolUse": 2000, "PostToolUse": 2000}   # 文字钩子能挂的时机和提醒线
MAX_HOOK_TEXT = 20000                   # 文字钩子每次附上的字数（文字加上最长的那句随机句子）的硬上限
MAX_FILES_BYTES = 50 << 20
MAX_SCRIPTS, MAX_SCRIPT, MAX_TIMEOUT = 5, 20000, 60  # 脚本：几个、每个多少字；命令一次最多跑几秒（Claude Code 默认也是 60 秒）
MAX_HOOKS, MAX_CHOICES, MAX_COMMAND, MAX_MATCHER = 20, 50, 2000, 200
EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
          "PermissionRequest", "Notification", "Stop", "SubagentStart", "SubagentStop", "PreCompact")
RESERVED = ("scripts", "hooks.json")    # 资料和它们写在同一个目录里
HOOK_HELPER = str(Path(__file__).with_name("persona_hook"))
_lock = threading.RLock()


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _save(path: Path, data):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as e:
        raise HTTPException(500, f"写不进 {path.name}：{e}")


def _put(text: str) -> str:
    """存一份资料全文，返回指纹。同样的内容只存一份。"""
    data = text.encode()
    sha = hashlib.sha256(data).hexdigest()
    path = BLOBS / f"{sha}.txt"
    if not path.exists():
        BLOBS.mkdir(mode=0o700, exist_ok=True)
        tmp = path.with_name(f".{sha}.{os.getpid()}.tmp")
        try:
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
                f.write(data)
            tmp.replace(path)
        except OSError as e:
            raise HTTPException(500, f"补充资料存不下：{e}")
    return sha


def _get(sha: str) -> str:
    try:
        return (BLOBS / f"{sha}.txt").read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"补充资料读不到（{sha[:12]}）：{e}")


def _legacy(c: dict) -> dict:
    """早期版本的每轮提醒、压缩后提醒，换成同样效果的文字钩子（卡和快照都可能是老写法）。"""
    hooks = dict(c.get("hooks") or {})
    if (r := (c.get("remind") or "").strip()):
        hooks.setdefault("UserPromptSubmit", []).append({"matcher": "", "hooks": [{"type": "text", "text": r}]})
    if (r := (c.get("compact_remind") or "").strip()):
        hooks.setdefault("SessionStart", []).append({"matcher": "compact|resume", "hooks": [{"type": "text", "text": r}]})
    return hooks


def _upgrade(c: dict):
    """老写法的卡换成现在的：两种提醒换成钩子，资料全文搬进 persona_files（卡上只留指纹）。读进来时做，下次保存时落盘。"""
    if "remind" in c or "compact_remind" in c:
        c["hooks"] = _legacy(c)
        c.pop("remind", None)
        c.pop("compact_remind", None)
    for f in c.get("files") or []:
        if "text" in f:
            f["sha"], f["size"] = _put(f["text"]), len(f["text"])
            del f["text"]


# 读坏了就报错停下（users.py 同样的规矩）：当成空的会把大家的卡都弄丢
_db: dict = _load(FILE, {"cards": {}, "defaults": {}})
_db.setdefault("consents", {})
for _c in _db["cards"].values():
    _upgrade(_c)
_snaps: dict = _load(SNAP, {})


class Bad(Exception):
    """用户的操作不成立，消息是写给用户看的（网页回 400，ccpersona 打到标准错误上）。"""


def _check(name: str | None, text: str | None):
    if name is not None:
        if not name.strip():
            raise Bad("人设卡要有个名字")
        if len(name.strip()) > MAX_NAME:
            raise Bad(f"名字最多 {MAX_NAME} 个字")
    if text is not None and len(text) > MAX_TEXT:
        raise Bad(f"人设卡正文最多 {MAX_TEXT} 个字，现在有 {len(text)} 个（超过 {SOFT_TEXT} 字就已经很占上下文了）")


def _filename(name: str, what: str, reserved=()) -> str:
    """名字就是写到 Claude 那边的文件名，所以不许带目录、不许点开头（成了隐藏文件）。"""
    if not name:
        raise Bad(f"{what}要有个文件名")
    if "/" in name or "\\" in name or name.startswith(".") or not name.isprintable():
        raise Bad(f"{what}的文件名「{name}」不能带斜杠、不能用点开头、不能有控制字符")
    if name in reserved:
        raise Bad(f"{what}不能叫「{name}」，这个名字留给钩子用")
    if len(name) > MAX_FILE_NAME:
        raise Bad(f"{what}的文件名最多 {MAX_FILE_NAME} 个字：{name}")
    return name


def _scripts(items: list | None) -> list[dict] | None:
    """核对脚本（整份替换），返回要存的 [{name, text}]；None 是不改。"""
    if items is None:
        return None
    if len(items) > MAX_SCRIPTS:
        raise Bad(f"脚本最多 {MAX_SCRIPTS} 个，现在有 {len(items)} 个")
    out, seen = [], set()
    for s in items:
        name, text = _filename(str(s.get("name") or "").strip(), "脚本"), str(s.get("text") or "")
        if name in seen:
            raise Bad(f"有两个脚本都叫「{name}」")
        if len(text) > MAX_SCRIPT:
            raise Bad(f"脚本「{name}」最多 {MAX_SCRIPT} 个字，现在有 {len(text)} 个")
        if not text.strip():
            raise Bad(f"脚本「{name}」是空的")
        seen.add(name)
        out.append({"name": name, "text": text})
    return out


def _files(items: list | None, user: str | None) -> list[dict] | None:
    """核对补充资料（整份替换），返回要存的 [{name, sha, size}]；None 是不改。每一份要么带全文（text，新加的），
    要么只带指纹（sha，留着原来的、复制别人的卡时沿用），指纹只认这个人看得到的卡上挂着的。"""
    if items is None:
        return None
    out, seen, total = [], set(), 0
    for f in items:
        name = _filename(str(f.get("name") or "").strip(), "补充资料", RESERVED)
        if name in seen:
            raise Bad(f"补充资料里有两份都叫「{name}」")
        if "text" in f and f["text"] is not None:
            text = str(f["text"])
            if not text.strip():
                raise Bad(f"补充资料「{name}」是空的")
            size, sha = len(text), _put(text)
            total += len(text.encode())
        else:
            sha = str(f.get("sha") or "")
            known = [g for c in _db["cards"].values() if _usable(c, user) for g in c.get("files") or [] if g["sha"] == sha]
            if not known:
                raise Bad(f"补充资料「{name}」没给内容")
            size = known[0]["size"]
            total += (BLOBS / f"{sha}.txt").stat().st_size
        if total > MAX_FILES_BYTES:
            raise Bad(f"一张卡的补充资料加起来最多 {MAX_FILES_BYTES >> 20} MB")
        seen.add(name)
        out.append({"name": name, "sha": sha, "size": size})
    return out


def _hooks(hooks, scripts: list[str]) -> dict:
    """核对钩子配置，返回整理好的 {时机: [{matcher, hooks: [...]}]}。也收 settings.json 那种外面再包一层 hooks 的写法，原样粘进来就行。"""
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise Bad("钩子要写成 {时机: [{matcher, hooks: [...]}]}，和 settings.json 里的 hooks 一样")
    if set(hooks) == {"hooks"} and isinstance(hooks["hooks"], dict):
        hooks = hooks["hooks"]
    out, n = {}, 0
    for event, groups in hooks.items():
        if event not in EVENTS:
            raise Bad(f"不认识的时机 {event}，能用的有：{'、'.join(EVENTS)}")
        if not isinstance(groups, list):
            raise Bad(f"{event} 下面要是一个列表")
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                raise Bad(f"{event} 的每一组要写成 {{matcher, hooks: [...]}}")
            matcher = str(g.get("matcher") or "")
            if len(matcher) > MAX_MATCHER:
                raise Bad(f"{event} 的匹配条件最多 {MAX_MATCHER} 个字")
            rows = [_hook(event, h, scripts) for h in g["hooks"]]
            n += len(rows)
            if rows:
                out.setdefault(event, []).append({"matcher": matcher, "hooks": rows})
    if n > MAX_HOOKS:
        raise Bad(f"钩子最多 {MAX_HOOKS} 个，现在有 {n} 个")
    return out


def _timeout(h: dict) -> int:
    try:
        t = int(h.get("timeout") or MAX_TIMEOUT)
    except (TypeError, ValueError):
        raise Bad("超时要写秒数") from None
    if not 1 <= t <= MAX_TIMEOUT:
        raise Bad(f"超时要在 1 到 {MAX_TIMEOUT} 秒之间")
    return t


def _hook(event: str, h, scripts: list[str]) -> dict:
    kind = h.get("type") if isinstance(h, dict) else None
    if kind == "text":
        if event not in TEXT_EVENTS:
            raise Bad(f"文字钩子只能挂在 {'、'.join(TEXT_EVENTS)} 这几个时机（别的时机 Claude Code 不往对话里附文字），{event} 要写成命令")
        text, choices = str(h.get("text") or ""), h.get("choices") or []
        if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
            raise Bad("随机句子 choices 要是一组字符串")
        choices = [c.strip() for c in choices if c.strip()]
        if len(choices) > MAX_CHOICES:
            raise Bad(f"随机句子最多 {MAX_CHOICES} 句")
        if not text.strip() and not choices:
            raise Bad(f"{event} 有一个文字钩子什么都没写")
        if _size(h) > MAX_HOOK_TEXT:
            raise Bad(f"{event} 的文字钩子每次最多附 {MAX_HOOK_TEXT} 个字（文字加上最长的一句随机句子），现在是 {_size(h)} 个")
        return {"type": "text", "text": text.strip(), **({"choices": choices} if choices else {})}
    if kind == "command":
        cmd = str(h.get("command") or "").strip()
        if not cmd:
            raise Bad(f"{event} 有一个命令钩子没写命令")
        if len(cmd) > MAX_COMMAND:
            raise Bad(f"命令最多 {MAX_COMMAND} 个字，长的写进脚本里")
        for name in scripts_named(cmd):
            if name not in scripts:
                raise Bad(f"命令里用到的脚本「{name}」不在这张卡上")
        return {"type": "command", "command": cmd, "timeout": _timeout(h)}
    raise Bad("钩子的类型只能是 command（和 settings.json 里一样执行一条命令）或 text（附一段文字）")


def scripts_named(cmd: str) -> list[str]:
    """命令里写的 $CC_PERSONA_DIR/scripts/<名字>，用来核对脚本在不在卡上（引号、花括号两种写法都认）。"""
    return re.findall(r"\$\{?CC_PERSONA_DIR\}?/scripts/([^\s\"'/;|&)]+)", cmd)


def _size(h: dict) -> int:
    return len(str(h.get("text") or "").strip()) + max(map(len, h.get("choices") or []), default=0)


def heavy(c: dict) -> list[str]:
    """超过提醒线、每次请求都要多带很多字的地方，写给用户看的。"""
    out = []
    if len(c["text"]) > SOFT_TEXT:
        out.append(f"正文 {len(c['text'])} 字，每次请求都会带上")
    for ev, groups in (c.get("hooks") or {}).items():
        for g in groups:
            for h in g["hooks"]:
                if h["type"] == "text" and _size(h) > TEXT_EVENTS[ev]:
                    out.append(f"{ev} 的文字钩子每次附上 {_size(h)} 字（提醒线 {TEXT_EVENTS[ev]} 字）")
    return out


def runs_code(c: dict) -> bool:
    return bool(c.get("scripts")) or any(h["type"] == "command" for groups in (c.get("hooks") or {}).values()
                                         for g in groups for h in g["hooks"])


def _fingerprint(c: dict) -> str:
    """要先问过才能用的「这一版」：脚本全文、所有命令钩子（时机、匹配、命令、超时）和超过提醒线的地方。
    只改了正文的措辞、短的文字钩子不用重新同意。什么都不用问就是空的。"""
    calls = sorted(json.dumps([ev, g["matcher"], h], ensure_ascii=False, sort_keys=True)
                   for ev, groups in (c.get("hooks") or {}).items() for g in groups for h in g["hooks"] if h["type"] == "command")
    warn = heavy(c)
    if not c.get("scripts") and not calls and not warn:
        return ""
    blob = json.dumps({"scripts": c.get("scripts") or [], "calls": calls, "heavy": warn}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _consented(c: dict, cid: str, user: str | None) -> bool:
    fp = _fingerprint(c)
    return not fp or c["owner"] == user or bool(user and _db["consents"].get(user, {}).get(cid) == fp)


def _view(cid: str, c: dict, user: str | None, full: bool = False) -> dict:
    """列表里脚本只给名字和字数（每个人打开页面都要取一遍列表）；full 连脚本全文一起给（编辑、复制、同意之前看）。
    资料全文不给：可能有几十 MB，编辑时留着原来的只交指纹。"""
    return {"id": cid, "name": c["name"], "text": c["text"], "public": c["public"],
            "owner": c["owner"], "mine": c["owner"] == user, "ts": c["ts"], "updated": c["updated"],
            "files": [dict(f) for f in c.get("files") or []], "hooks": c.get("hooks") or {},
            "scripts": [{"name": s["name"], "size": len(s["text"]), **({"text": s["text"]} if full else {})} for s in c.get("scripts") or []],
            "runsCode": runs_code(c), "heavy": heavy(c), "consentHash": _fingerprint(c), "consented": _consented(c, cid, user)}


def _usable(c: dict | None, user: str | None) -> bool:
    return bool(c) and not c.get("deleted") and (c["owner"] == user or c["public"])


def overview(user: str | None) -> dict:
    """这个人能选的卡：自己的（不管公开没公开）在前，别人公开的在后，各按最近改的排。
    defaultGone：设成默认的卡已经被删了、被撤回公开了，默认落空，页面上要说出来。"""
    with _lock:
        cards = _db["cards"]
        rows = [_view(k, c, user) for k, c in cards.items() if _usable(c, user)]
        want = _db["defaults"].get(user) if user else None
        gone = cards[want]["name"] if want and want in cards and not _usable(cards[want], user) else ""
    rows.sort(key=lambda r: (not r["mine"], -r["updated"]))
    return {"cards": rows, "default": want if want and not gone else None, "defaultGone": gone,
            "max": {"name": MAX_NAME, "text": MAX_TEXT, "softText": SOFT_TEXT, "textEvents": TEXT_EVENTS, "hookText": MAX_HOOK_TEXT,
                    "filesMB": MAX_FILES_BYTES >> 20, "fileName": MAX_FILE_NAME, "scripts": MAX_SCRIPTS, "script": MAX_SCRIPT,
                    "timeout": MAX_TIMEOUT, "hooks": MAX_HOOKS, "events": EVENTS}}


def card(user: str | None, cid: str) -> dict:
    """一张能用的卡的全部内容，脚本带全文。"""
    with _lock:
        c = _db["cards"].get(cid)
        if not _usable(c, user):
            raise Bad(f"没有编号是 {cid}、你能看的人设卡")
        return _view(cid, c, user, full=True)


def file_text(user: str | None, cid: str, name: str) -> str:
    c = card(user, cid)
    for s in c["scripts"]:
        if s["name"] == name:
            return s["text"]
    for f in c["files"]:
        if f["name"] == name:
            return _get(f["sha"])
    raise Bad(f"人设卡「{c['name']}」没有叫「{name}」的脚本或补充资料")


def _need_user(user: str | None):
    if not user:
        raise Bad("登录以后才能建、改人设卡，游客只能选用大家公开的")


def _own(cid: str, user: str) -> dict:
    c = _db["cards"].get(cid)
    if not c or c.get("deleted"):
        raise Bad(f"没有编号是 {cid} 的人设卡")
    if c["owner"] != user:
        raise Bad(f"人设卡「{c['name']}」是 {c['owner']} 的，只能选用，不能改；想改就复制一份成自己的")
    return c


def create(user: str | None, name: str, text: str, public: bool = False, files: list | None = None,
           hooks: dict | None = None, scripts: list | None = None) -> dict:
    _need_user(user)
    _check(name, text)
    scripts = _scripts(scripts) or []
    hooks = _hooks(hooks, [s["name"] for s in scripts])
    now = time.time()
    with _lock:
        files = _files(files, user) or []
        cid = secrets.token_hex(4)
        while cid in _db["cards"]:
            cid = secrets.token_hex(4)
        c = _db["cards"][cid] = {"owner": user, "name": name.strip(), "text": text, "files": files, "hooks": hooks,
                                 "scripts": scripts, "public": bool(public), "ts": now, "updated": now}
        _save(FILE, _db)
        return _view(cid, c, user)


def update(user: str | None, cid: str, name: str | None = None, text: str | None = None, public: bool | None = None,
           admin: bool = False, files: list | None = None, hooks: dict | None = None, scripts: list | None = None) -> dict:
    """改自己的卡（资料、钩子、脚本都是整份替换，None 是不改）。查看器账号能撤回别人公开的卡（只能撤回，不能改别人写的内容）。"""
    _need_user(user)
    _check(name, text)
    scripts = _scripts(scripts)
    with _lock:
        c = _db["cards"].get(cid)
        if admin and c and not c.get("deleted") and c["owner"] != user:
            if any(x is not None for x in (name, text, files, hooks, scripts)) or public is not False:
                raise Bad(f"人设卡「{c['name']}」是 {c['owner']} 的，只能撤回公开，不能改内容")
        else:
            c = _own(cid, user)
        files = _files(files, user)
        # 只换脚本不换钩子时，原来的钩子也要核一遍：不能留着执行已经去掉的脚本
        new_scripts = (c.get("scripts") or []) if scripts is None else scripts
        new_hooks = _hooks(c.get("hooks") if hooks is None else hooks, [s["name"] for s in new_scripts])
        if name is not None:
            c["name"] = name.strip()
        if text is not None:
            c["text"] = text
        if files is not None:
            c["files"] = files
        c["scripts"], c["hooks"] = new_scripts, new_hooks
        if public is not None:
            c["public"] = bool(public)
        c["updated"] = time.time()
        _save(FILE, _db)
        return _view(cid, c, user)


def delete(user: str | None, cid: str) -> str:
    """撤下自己的卡（文件里留着）。已经用它建好的会话照旧用建会话时的快照。"""
    _need_user(user)
    with _lock:
        c = _own(cid, user)
        c["deleted"] = time.time()
        if _db["defaults"].get(user) == cid:
            _db["defaults"].pop(user)
        _save(FILE, _db)
        return c["name"]


def set_default(user: str | None, cid: str | None) -> str:
    """这个人新建会话时默认选哪张卡；None 是不用人设卡。返回卡名（不用就是空的）。"""
    if not user:
        raise Bad("登录以后才能设自己默认的人设卡")
    with _lock:
        if cid:
            c = _db["cards"].get(cid)
            if not _usable(c, user):
                raise Bad(f"没有编号是 {cid}、你能用的人设卡")
            _db["defaults"][user] = cid
        else:
            _db["defaults"].pop(user, None)
        _save(FILE, _db)
        return _db["cards"][cid]["name"] if cid else ""


def pick(user: str | None, cid: str | None, consent: str | None = None) -> dict | None:
    """新建会话选的卡：None 按这个人设的默认（没设、默认落空都是不用），空字符串是明确不用。选了用不了的卡就报错，不悄悄换成别的。
    要先问的卡（见开头）：游客不能用带命令、脚本的；没同意过这一版时，consent 交来的正是这一版的指纹就记下同意，否则报错让页面先弹窗。"""
    with _lock:
        if cid is None:
            cid = _db["defaults"].get(user) if user else None
            c = _db["cards"].get(cid) if cid else None
            if not _usable(c, user):
                return None
        elif not cid:
            return None
        else:
            c = _db["cards"].get(cid)
            if not _usable(c, user):
                raise Bad("选的人设卡用不了了（被删了，或者作者撤回了公开），请重新选一张")
        if (fp := _fingerprint(c)) and not _consented(c, cid, user):
            if not user and runs_code(c):
                raise Bad(f"人设卡「{c['name']}」带命令或脚本，游客不能用，登录以后再选")
            if consent != fp:
                seen = cid in _db["consents"].get(user, {}) if user else False
                raise Bad(f"人设卡「{c['name']}」要先看过说明、同意以后才能用" + ("（卡改过了，要重新同意）" if seen else ""))
            if user:
                _db["consents"].setdefault(user, {})[cid] = fp
                _save(FILE, _db)
        out = {"id": cid, **c}
        out["files"] = [{**f, "text": _get(f["sha"])} for f in c.get("files") or []]
        return out


def folder(sid: str):
    """这个会话的资料、钩子配置和脚本写在哪：和上传目录一样放在运行 Claude 的那个用户的家目录下、按会话分开。"""
    import isolation
    env = isolation.for_sid(sid)
    return (env.path(env.home) if env else hostfs.HOME) / "cc_persona" / sid


def _write(path, text: str):
    with path.open("w", encoding="utf-8") as out:
        out.write(text)


def remember(sid: str, card: dict | None, where=None):
    """新建会话、拉起 CLI 之前记下这个会话用哪张卡（正文和钩子一起存）。不用人设卡就不记。card 是 pick 的结果，资料带全文。
    资料、脚本和钩子配置写进 where（运行 Claude 的那个用户那边的路径，folder），快照里只记写到了哪里；
    写不进去就抛 OSError，会话不建：没有资料却照样告诉 Claude 有，Claude 会去读一个不存在的文件，钩子也会找不到脚本。"""
    if not card:
        return
    hooks, files, scripts = card.get("hooks") or {}, [], card.get("scripts") or []
    if card.get("files") or scripts or hooks:
        where.mkdir(parents=True, exist_ok=True)
        for f in card.get("files") or []:
            _write(where / f["name"], f["text"])
            files.append({"name": f["name"], "path": str(where / f["name"]), "size": f["size"]})
        if scripts:
            (where / "scripts").mkdir(exist_ok=True)
            for s in scripts:
                _write(where / "scripts" / s["name"], s["text"])
        if hooks:
            _write(where / "hooks.json", json.dumps(hooks, ensure_ascii=False, indent=1))
    with _lock:
        _snaps[sid] = {"id": card["id"], "name": card["name"], "owner": card["owner"], "text": card["text"], "files": files,
                       "hooks": hooks, "scripts": [s["name"] for s in scripts],
                       "dir": str(where) if files or hooks or scripts else None, "ts": time.time()}
        _save(SNAP, _snaps)


def of_session(sid: str) -> dict | None:
    with _lock:
        return _snaps.get(sid)


def used_by(user: str, sids) -> bool:
    """这个人用过人设卡没有：自己建过卡（删了的也算）、设过默认，或者名下有会话选了卡。设置里「二次元」（新会话页的颜文字和人设卡）没切过时，
    用过卡或者累计花掉超过 5 毛就开着（server.used_features，users.features 的 persona）。"""
    with _lock:
        return (user in _db["defaults"] or any(c.get("owner") == user for c in _db["cards"].values())
                or any(sid in _snaps for sid in sids))


def launch_hooks(sid: str) -> dict | None:
    """拉起 CLI 之前（chat.Driver.run，在线程里）：这个会话的卡上的钩子换成 Claude Code 的命令钩子，交给 --settings。没有就是 None。
    命令前面先导出 CC_PERSONA_DIR（快照目录）；文字钩子交给 persona_hook 现读现打。
    老写法的快照（只有每轮提醒、压缩后提醒）这时补写钩子配置。"""
    snap = of_session(sid)
    if not snap:
        return None
    hooks = snap.get("hooks")
    if hooks is None:
        if not (hooks := _legacy(snap)):
            return None
        where = folder(sid)
        where.mkdir(parents=True, exist_ok=True)
        _write(where / "hooks.json", json.dumps(hooks, ensure_ascii=False, indent=1))
        with _lock:
            snap.update(hooks=hooks, scripts=[], dir=str(where))
            _save(SNAP, _snaps)
    if not hooks:
        return None
    head = f"export CC_PERSONA_DIR={shlex.quote(snap['dir'])}; "
    out = {}
    for event, groups in hooks.items():
        for gi, g in enumerate(groups):
            rows = []
            for hi, h in enumerate(g["hooks"]):
                if h["type"] == "text":
                    rows.append({"type": "command", "timeout": 10,
                                 "command": head + f"exec /usr/bin/python3 -I {shlex.quote(HOOK_HELPER)} {event} {gi} {hi}"})
                else:
                    rows.append({"type": "command", "timeout": h["timeout"], "command": head + h["command"]})
            out.setdefault(event, []).append({"matcher": g["matcher"], "hooks": rows})
    return out


def instructions() -> str:
    """共用环境注入的说明（instance.json 的 instructions），每次拉起 CLI 都现读：改了文件，之后新建（和压缩过）的会话就用新的。"""
    if not INSTRUCTIONS:
        return ""
    try:
        return Path(INSTRUCTIONS).read_text().strip()
    except OSError as e:
        raise RuntimeError(f"读不到要注入的说明（{INSTRUCTIONS}：{e}），为了不在没有说明的情况下跑，拒绝启动 Claude") from None


# ---------- ccpersona 的套接字 ----------

drivers = lambda: []   # noqa: E731  server 设成「现在所有的网页对话驱动」
_sock: socket.socket | None = None
socket_path: str | None = None


def _who(pid: int) -> tuple[str, str | None]:
    import chat
    for d in list(drivers()):
        client = d.client or getattr(d, "starting_client", None)
        if client and pid in chat.cli_pids(client):
            return d.session_id, users.owner_of(d.session_id)
    raise Bad("只能在网页上开的会话里使用（认不出这条命令是哪个会话发出来的）")


def _count(c: dict) -> str:
    n = sum(len(g["hooks"]) for groups in c["hooks"].values() for g in groups)
    return (f"，{n} 个钩子" if n else "") + (f"，{len(c['scripts'])} 个脚本" if c["scripts"] else "") + \
        (f"，{len(c['files'])} 份补充资料" if c["files"] else "")


def _line(c: dict) -> str:
    return f"{c['id']}\t{c['name']}\t" + ("自己的" if c["mine"] else f"{c['owner']} 公开的") + \
        ("，已公开" if c["mine"] and c["public"] else "") + f"\t{len(c['text'])} 字" + _count(c)


def _show(c: dict) -> str:
    out = f"# {c['name']}（{'自己的' if c['mine'] else c['owner'] + ' 公开的'}）\n\n{c['text']}\n"
    if c["hooks"]:
        out += "\n## 钩子\n\n" + json.dumps(c["hooks"], ensure_ascii=False, indent=2) + "\n"
    for title, items in (("脚本", c["scripts"]), ("补充资料", c["files"])):
        if items:
            out += f"\n## {title}（ccpersona show <编号> <文件名> 看全文）\n\n" + "".join(f"- {f['name']}（{f['size']} 字）\n" for f in items)
    if c["heavy"]:
        out += "\n## 很占上下文的地方\n\n" + "".join(f"- {w}\n" for w in c["heavy"])
    return out


def _patch(old: list[dict], add: list | None, drop: list | None, what: str, keep) -> list[dict] | None:
    """加几份（同名的换掉）、去掉几份，都在原来那几份上改，再整份交给 update 核对。都没给是不改。"""
    if not add and not drop:
        return None
    drop = set(drop or [])
    if missing := drop - {f["name"] for f in old}:
        raise Bad(f"没有这些{what}：" + "、".join(sorted(missing)))
    out = [keep(f) for f in old if f["name"] not in drop]
    for a in add or []:
        out = [f for f in out if f["name"] != a.get("name")] + [a]
    return out


def handle(req: dict, user: str, sid: str) -> str:
    """ccpersona 的一条命令，返回要打到标准输出上的字；不成立就抛 Bad。"""
    op = req.get("op")
    if op == "workflow":
        # 沿用本机连接的进程归属；工作流不接受调用方自报用户名。
        import workflows
        return json.dumps(workflows.dispatch(req.get("request"), user, sid), ensure_ascii=False) + "\n"
    if op == "name":
        # 会话里的 ccname 给自己的会话起名（naming.py），借这个套接字认出是哪个会话
        import naming
        return naming.handle(req, sid)
    if op == "todo":
        # 会话里的 cctodo 读写会话主人的待办（todos.py），借这个套接字认出是哪个会话、主人是谁
        import todos
        try:
            return todos.handle(req, user, sid)
        except todos.Bad as e:
            raise Bad(str(e)) from None
    if op == "chan":
        # 会话里的 ccchan 用频道（channels.py），借这个套接字认出是哪个会话、主人是谁
        import channels
        try:
            return channels.handle(req, user, sid)
        except channels.Bad as e:
            raise Bad(str(e)) from None
    if op == "recall":
        # 会话里的 ccrecall 回忆（sessions.serve_request）：查看器以自己的身份只搜这个实例；
        # 普通用户只搜主人是他的会话，instance.json 写了 recall_users 才开放，查看器账号不限
        import chat
        import instance
        import sessions
        admin = chat.owner_admin(sid)
        if not admin and not instance.RECALL_USERS:
            raise Bad("回忆功能还没有开放")
        try:
            return sessions.serve_request(req.get("argv") or [], None if admin else user)
        except sessions.Refused as e:
            raise Bad(str(e)) from None
    if op == "ls":
        ov = overview(user)
        mine = of_session(sid)
        out = [_line(c) + ("\t← 新建会话默认选它" if c["id"] == ov["default"] else "") for c in ov["cards"]] or ["还没有能用的人设卡"]
        if ov["defaultGone"]:
            out.append(f"（设成默认的「{ov['defaultGone']}」已经用不了了，新建会话时不用人设卡）")
        out.append(f"这个会话用的是「{mine['name']}」" if mine else "这个会话没用人设卡")
        return "\n".join(out) + "\n"
    if op == "show":
        if req.get("file"):
            text = file_text(user, str(req.get("id") or ""), req["file"])
            return text + ("" if text.endswith("\n") else "\n")
        return _show(card(user, str(req.get("id") or "")))
    old = [k for k in ("remind", "compact_remind") if k in req]   # 上午那一版的参数（见 ccpersona 的 OLD）
    if op == "edit" and old:
        raise Bad("--remind、--compact-remind 已经换成钩子了：把提醒写成 --hooks 文件里的文字钩子"
                  "（每轮提醒挂 UserPromptSubmit，压缩后提醒挂 SessionStart、matcher 写 compact|resume）")
    if op == "add":
        hooks = req.get("hooks")
        if old:
            hooks = _legacy({"hooks": _hooks(hooks, [s.get("name") for s in req.get("script") or []]),
                             **{k: req[k] for k in old}})
        c = create(user, str(req.get("name") or ""), str(req.get("text") or ""), bool(req.get("public")),
                   req.get("attach") or [], hooks, req.get("script") or [])
        return f"建好了：{c['id']}「{c['name']}」" + ("，已公开" if c["public"] else "") + _count(c) + "\n"
    if op == "edit":
        cid = str(req.get("id") or "")
        old = card(user, cid) if any(req.get(k) for k in ("attach", "detach", "script", "drop_script")) else None
        files = _patch(old["files"], req.get("attach"), req.get("detach"), "补充资料",
                       lambda f: {"name": f["name"], "sha": f["sha"]}) if old else None
        scripts = _patch(old["scripts"], req.get("script"), req.get("drop_script"), "脚本",
                         lambda s: {"name": s["name"], "text": s["text"]}) if old else None
        c = update(user, cid, req.get("name"), req.get("text"), req.get("public"), files=files, hooks=req.get("hooks"),
                   scripts=scripts)
        return f"改好了：{c['id']}「{c['name']}」" + ("，已公开" if c["public"] else "，没公开") + _count(c) + "\n"
    if op == "rm":
        return f"删掉了「{delete(user, str(req.get('id') or ''))}」\n"
    if op == "default":
        name = set_default(user, req.get("id") or None)
        return f"以后新建会话默认选「{name}」\n" if name else "以后新建会话默认不用人设卡\n"
    raise Bad(f"不认识的操作 {op}")


def serve(conn: socket.socket):
    reply = {"exit": 1, "out": "", "err": ""}
    try:
        pid, _, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_FILES_BYTES + (8 << 20):   # 资料加上正文、脚本、钩子
                raise Bad("请求太长了")
        req = json.loads(buf)
        sid, user = _who(pid)
        if not user:
            raise Bad("这个会话没有主人（无主会话），不知道是谁的人设卡，登录后新建的会话才能用")
        reply = {"exit": 0, "out": handle(req, user, sid), "err": ""}
    except (Bad, HTTPException) as e:
        reply = {"exit": 2, "out": "", "err": f"ccpersona：{e.detail if isinstance(e, HTTPException) else e}\n"}
    except Exception as e:   # 出错要让 ccpersona 那边看到，不能只是断开
        reply = {"exit": 1, "out": "", "err": f"ccpersona：查看器出错了：{type(e).__name__}: {e}\n"}
        print(f"人设卡接口出错：{traceback.format_exc()}", flush=True)
    finally:
        with contextlib.suppress(OSError):
            conn.sendall(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
        conn.close()


def _accept(sock: socket.socket):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=serve, args=(conn,), daemon=True, name="ccpersona").start()


def start(port: int):
    """开 ccpersona 的套接字（对话模式才开）。instance.json 的 persona_socket 写了就用它，没写放在数据目录里、按端口起名
    （另起的测试实例和正式实例共用代码目录当数据目录，不能抢同一个）。所在目录要让运行 Claude 的用户进得去。
    认人靠内核报来的进程号；Claude 以别的用户运行（run_as）或开了独立环境时，套接字要谁都能连，
    否则 CLI 和查看器是同一个用户，只给本用户连，别的用户连门都摸不到。"""
    import instance
    import isolation
    global _sock, socket_path
    if _sock:
        return
    path = Path(PERSONA_SOCKET or DATA / f"persona-{port}.sock")
    path.unlink(missing_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    os.chmod(path, 0o666 if instance.RUN_AS or isolation.enabled() else 0o600)
    s.listen(16)
    _sock, socket_path = s, str(path)
    threading.Thread(target=_accept, args=(s,), daemon=True, name="ccpersona-accept").start()
    print(f"人设卡接口在 {path}", flush=True)


def stop():
    global _sock
    if _sock:
        with contextlib.suppress(OSError):
            _sock.close()
        _sock = None
        if socket_path:
            Path(socket_path).unlink(missing_ok=True)
