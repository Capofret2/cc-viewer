"""上下文里此刻装着什么，一层层拆到每个文件、每个气泡：给上下文药丸悬停卡片里的环形图用
（像 GNOME 磁盘用量分析那个圆环：里圈粗、外圈细，外圈的扇形都落在它所属的那块扇形的角度范围里）。

CLI 的 get_context_usage（和 /context 同一份）只拆到系统提示词、系统工具、记忆文件、技能、消息这一层，
最大的「消息」不再往下分。这里从会话记录里把消息逐条找出来：用户的话、Claude 的回复（到每个气泡）、思考、
工具调用和工具结果（按工具，再按文件、命令、搜索词）、图片、附加信息、压缩摘要。

每一条占多少：记录里每次请求都带着当时整个上下文多大（输入 + 缓存写入 + 缓存命中），相邻两次请求之差
正好是中间新加进去的东西——上一条回复（它的输出 token 数就是它在上下文里占的，包括思考：之前的思考一直留在上下文里），
加上回复之后进来的工具结果、用户的话、附加信息。所以每一组的总量是 API 给的准数，组里只有一样东西时它就是准数；
有好几样时按字数分摊（中文一字约一个 token，其余约 3.3 个字符一个，再按整个会话里准数和字数的比例校一下）。
回复里看得见的字按字数算，剩下的输出算思考（记录里的思考块是空的，只有签名）。
最后一次请求之后才进来的东西还没有准数，按字数估。第一次请求之前的那段（压缩摘要、开头几条）
要从第一次请求的上下文里减掉系统部分，系统部分只有 CLI 知道；CLI 没连着时系统部分只能和它们合成一块估。

当前上下文是哪些行：从最新的一行顺着 parentUuid 往回走到压缩分界（倒回去重来过的支路自然不在链上）；
并行工具调用里挂在旁边一支上的结果补回来（见 with_parallel_results）；
压缩时原样留下的那几条（compactMetadata.preservedMessages）接在压缩摘要后面。
"""
import base64
import json
import re
import struct
import threading
from datetime import datetime

import hostfs
import memtrim
from calls import CHUNK   # 大文件分块读，每块多大
from hostfs import Path   # 会话记录在运行 Claude 的那个用户名下，经它读（见 hostfs.py）
from instance import CALL   # 界面上怎么称呼用查看器的人

CJK = re.compile(r"[⺀-鿿가-힯豈-﫿＀-￯]")
TAG_RE = re.compile(r"<([\w-]+)>(.*?)</\1>", re.S)
BOUNDARY = b'"subtype":"compact_boundary"'
KEEP = 30          # 每一格最多分出几块，再多的合成「其余 n 项」
TINY = 0.001       # 不到总量千分之一的也合进「其余」，画出来只是一根线
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}

# CLI 分类的中文名；不认识的照原样写
CATS = {"System prompt": "系统提示词", "System tools": "系统工具", "MCP tools": "MCP 工具",
        "MCP server instructions": "MCP 说明", "Custom agents": "自定义子代理", "Memory files": "记忆文件",
        "Skills": "技能", "Messages": "消息"}
# 附加信息（CLI 每轮自动塞进来的东西）的中文名
ATTACH = {"total_tokens_reminder": "用量提醒", "deferred_tools_record": "延迟加载工具清单",
          "deferred_tools_delta": "延迟加载工具变动", "mcp_instructions_delta": "MCP 说明变动",
          "agent_listing_delta": "子代理清单变动", "skill_listing": "技能清单", "instructions": "说明文件",
          "prompt_snapshot": "提示词快照", "environment": "环境信息", "session_context": "会话背景",
          "date": "日期", "model": "模型", "credential_org": "账号组织", "auto_mode": "自动模式说明",
          "remote_session_change": "远程会话变动", "silent_turn_reminder": "静默轮提醒",
          "compact_file_reference": "压缩前读过的文件", "file": "带进来的文件", "edited_text_file": "被改过的文件",
          "command_permissions": "命令权限", "todo_reminder": "待办提醒", "plan_mode": "计划模式说明"}


def est(s: str) -> float:
    """按字数估 token：中日韩字约一个一个，其余约 3.3 个字符一个。只拿来在一组里分摊，不当准数用。"""
    n = len(CJK.findall(s))
    return n + (len(s) - n) / 3.3


def image_tokens(block) -> float:
    """一张图约占多少 token：按 Anthropic 的算法，长边先缩到 1568 以内、总像素不超过约 115 万，再按每 750 像素一个。
    尺寸从 PNG、GIF、JPEG 的文件头里读；读不出来按一张缩满的图算。"""
    data = (block.get("source") or {}).get("data") or ""
    w = h = 0
    try:
        head = base64.b64decode(data[:200_000][: len(data[:200_000]) // 4 * 4])
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
        elif head[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", head[6:10])
        elif head[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(head) and head[i] == 0xFF:
                m = head[i + 1]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", head[i + 5:i + 9])
                    break
                i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    except (ValueError, struct.error):
        pass
    if not (w and h):
        return 1600
    s = min(1, 1568 / max(w, h), (1_150_000 / (w * h)) ** 0.5)
    return w * s * h * s / 750


def text_of(c) -> str:
    """工具结果、附加信息的内容可能是字符串、块列表或字典，统一成文字（图片另算，这里跳过）。"""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(text_of(x) for x in c)
    if isinstance(c, dict):
        if c.get("type") == "image":
            return ""
        return text_of(c.get("text") or c.get("content") or "")
    return "" if c is None else str(c)


def images_in(c) -> list:
    if isinstance(c, list):
        return [x for x in c if isinstance(x, dict) and x.get("type") == "image"]
    return []


def short_path(p: str, cwd: str) -> str:
    """路径写短：工作目录下的写相对路径，家目录下的写 ~ 开头。"""
    if cwd and p.startswith(cwd.rstrip("/") + "/"):
        return p[len(cwd.rstrip("/")) + 1:]
    home = str(hostfs.HOME)
    return "~" + p[len(home):] if p.startswith(home + "/") else p


def one_line(s, n=48) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def tool_key(name: str, inp, cwd: str) -> str:
    """工具调用按什么再往下分：读写文件的按文件，Bash 按它的说明（没有就按命令），搜索按搜索词，子代理按任务说明。"""
    inp = inp if isinstance(inp, dict) else {}
    if name in FILE_TOOLS:
        p = inp.get("file_path") or inp.get("notebook_path")
        if p:
            return short_path(str(p), cwd)
    for k in ("description", "pattern", "url", "query", "skill", "command", "file_path", "path", "prompt"):
        if inp.get(k):
            v = inp[k]
            return short_path(v, cwd) if k in ("file_path", "path") else one_line(v)
    return name


def clock(ts) -> str:
    """记录里的时间（UTC）换成本机时间的时:分，叶子上写着好认是哪一条。"""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return ""


# ---------- 当前上下文里是哪些行 ----------

def blocks_of(d: dict) -> list[dict]:
    c = (d.get("message") or {}).get("content")
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def with_parallel_results(chain: list[dict], lines: list[dict]) -> list[dict]:
    """把顺着 parentUuid 走不到的并行工具结果插回链里。
    一次回复里并行调了几个工具时，CLI 有时先把几个调用都写完、结果才回来：「调用 A → 调用 B → 结果 B」在链上，
    结果 A 挂在调用 A 底下成了旁边一支，可它照样跟着这一轮发给了 API（续接会话时 CLI 也会把它拼回去）。
    漏了它，这一组按字数分摊时它那份就全算到别的结果头上（实际遇到过一次读文件记成两倍）。
    只收挂在链上调用底下、链上还没有这个调用的结果的那一行，连同紧跟在它后面的附加信息，按记录里的先后插回去；
    倒回去重来过的支路不受影响：那些结果对应的调用不在链上，或者链上已经有它的结果。"""
    on = {d.get("uuid") for d in chain}
    calls, done = {}, set()   # 链上的工具调用编号 → 所在行；链上已经有结果的调用编号
    for d in chain:
        for b in blocks_of(d):
            if d.get("type") == "assistant" and b.get("type") in ("tool_use", "server_tool_use"):
                calls[b.get("id")] = d.get("uuid")
            elif d.get("type") == "user" and b.get("type") == "tool_result":
                done.add(b.get("tool_use_id"))
    extra, took = [], set()
    for d in lines:
        if not isinstance(d, dict) or d.get("uuid") in on or d.get("isSidechain"):
            continue
        if d.get("type") == "user":
            ids = [b.get("tool_use_id") for b in blocks_of(d) if b.get("type") == "tool_result"]
            if ids and all(calls.get(i) == d.get("parentUuid") for i in ids) and not done.intersection(ids):
                extra.append(d)
                done.update(ids)
                took.add(d.get("uuid"))
        elif d.get("type") == "attachment" and d.get("parentUuid") in took:
            extra.append(d)
            took.add(d.get("uuid"))
    if not extra:
        return chain
    pos = {id(d): i for i, d in enumerate(lines)}
    out, k = [], 0
    for d in chain:
        while k < len(extra) and pos[id(extra[k])] < pos[id(d)]:
            out.append(extra[k])
            k += 1
        out.append(d)
    return out + extra[k:]


def last_newline(f, size: int) -> int:
    """文件前 size 个字节里最后一个换行之后的位置（没有换行就是 0），从末尾一块块往前找。"""
    pos = size
    while pos > 0:
        n = min(CHUNK, pos)
        pos -= n
        f.seek(pos)
        if (i := f.read(n).rfind(b"\n")) >= 0:
            return pos + i + 1
    return 0


def blocks_back(f, end: int):
    """文件 [0, end) 从后往前一块块给：(块在文件里的开头, 块)，每块都是若干整行（以换行结尾）。end 要在行尾（换行符之后）。
    分块倒着读，不把整个文件读进来；在块里找东西用 bytes 自己的 find，比一行行切开快得多。"""
    pos, carry = end, b""   # carry：后面那块开头没凑齐的半行（连同它的换行），接在前一块后面
    while pos > 0:
        n = min(CHUNK, pos)
        pos -= n
        f.seek(pos)
        data = f.read(n) + carry
        k = data.find(b"\n") if pos > 0 else -1   # 到了文件开头，整块都是完整的行
        if pos > 0 and k < 0:   # 一行比一块还长，接着往前凑
            carry = data
            continue
        carry, body = data[:k + 1], data[k + 1:]
        if body:
            yield pos + k + 1, body


def blocks_fwd(f, a: int, b: int):
    """文件 [a, b) 从前往后一块块给：(块在文件里的开头, 块)，每块都是若干整行。a、b 都要在行的边界上。"""
    f.seek(a)
    pos, rest = a, b""   # pos：rest 开头在文件里的位置
    while (left := b - pos - len(rest)) > 0:
        chunk = f.read(min(CHUNK, left))
        if not chunk:
            break
        data = rest + chunk
        end = data.rfind(b"\n") + 1
        if end:
            yield pos, data[:end]
        pos, rest = pos + end, data[end:]


def line_at(block: bytes, i: int) -> tuple[int, bytes]:
    """块里第 i 个字节所在的那一行：(行首在块里的位置, 这一行不带换行)。"""
    s = block.rfind(b"\n", 0, i) + 1
    return s, block[s:block.find(b"\n", i)]


def active_lines(path: Path) -> list[dict]:
    """当前上下文里的记录行，按顺序。只解析最后一次压缩之后的部分：压缩前的早就不在上下文里了，大会话也省得全读。
    分块读：整个文件读进来再切一份副本的话，291MB 的会话每悬停一次内存就冲高好几百 MB（见 memtrim.py）。
    先从末尾倒着一行行找最后一次压缩的分界，再从分界往后解析；压缩时原样留下的那几条只在分界之前按编号找。"""
    with path.open("rb") as f:
        end = last_newline(f, f.seek(0, 2))   # 只看到这一刻为止的完整行：最后一行可能还没写完，读的时候文件也可能还在长
        start, boundary = 0, None
        for off, block in blocks_back(f, end):
            pos = len(block)
            while boundary is None and (i := block.rfind(BOUNDARY, 0, pos)) >= 0:
                s, x = line_at(block, i)
                try:
                    d = json.loads(x)
                except ValueError:
                    d = None
                if isinstance(d, dict) and d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                    start, boundary = off + s, d
                pos = s   # 是写在别的内容里的同样几个字（比如工具结果里），接着往前找
            if boundary is not None:
                break
        lines = []
        for _, block in blocks_fwd(f, start, end):
            for x in block.split(b"\n"):
                if x.strip():
                    try:
                        lines.append(json.loads(x))
                    except ValueError:
                        pass
        kept = ((boundary or {}).get("compactMetadata") or {}).get("preservedMessages") or {}
        found: dict[str, dict] = {}
        if kept.get("uuids"):   # 每个编号认分界之前第一次出现它的那一行
            want = {f'"uuid":"{k}"'.encode(): k for k in dict.fromkeys(kept["uuids"])}
            for _, block in blocks_fwd(f, 0, start):
                for pat in [p for p in want if p in block]:
                    k = want.pop(pat)
                    try:   # 记着压缩前那次请求的用量，和压缩后的对不上：标出来，拆的时候不当请求用
                        found[k] = {**json.loads(line_at(block, block.find(pat))[1]), "kept": True}
                    except ValueError:
                        pass
                if not want:
                    break
    memtrim.read(end)
    by = {d["uuid"]: d for d in lines if isinstance(d, dict) and d.get("uuid")}
    tip = next((d for d in reversed(lines) if isinstance(d, dict) and d.get("uuid") and not d.get("isSidechain")
                and d.get("type") in ("user", "assistant", "attachment", "system")), None)
    chain, u = [], tip and tip["uuid"]
    while u in by and len(chain) <= len(by):
        chain.append(by[u])
        u = by[u].get("parentUuid")
    chain = with_parallel_results(chain[::-1], lines)
    if kept.get("uuids"):
        old = [found[k] for k in kept["uuids"] if k in found]
        at = next((n + 1 for n, d in enumerate(chain) if d.get("uuid") == kept.get("anchorUuid")),
                  next((n + 1 for n, d in enumerate(chain) if d.get("isCompactSummary")), 0))
        chain[at:at] = old
    return chain


# ---------- 拆成一条条 ----------

def units_of(d: dict, tools: dict, cwd: str) -> list[dict]:
    """一行记录拆成几条「东西」：p 在环形图里的路径，w 按字数估的 token，u 记录行编号（前端跳到那个气泡），
    reply 是不是回复（属于哪次请求），think 是不是空的思考块（回复里扣掉看得见的字以后剩下的给它）。"""
    t, uid, at = d.get("type"), d.get("uuid"), clock(d.get("timestamp"))
    base = {"u": uid, "ts": at}
    out = []

    def add(path, w, **kw):
        out.append({**base, "p": path, "w": max(w, 1.0), **kw})

    def talk(text):
        """用户消息里的一段字：用户的话、命令输出、后台任务通知、打断。"""
        tags = TAG_RE.findall(text)
        if tags and not TAG_RE.sub("", text).strip():
            names = [k for k, _ in tags]
            if "task-notification" in names:
                return add(["后台任务通知", f"{at} 通知"], est(text))
            cmd = next((v.strip() for k, v in tags if k == "command-name"), "")
            return add(["命令和输出", f"{at} {cmd or names[0]}"], est(text))
        if text.startswith("[Request interrupted"):
            return add(["命令和输出", f"{at} 打断"], est(text))
        add([f"{CALL}的话", one_line(text, 30) or at], est(text))

    if t == "assistant":
        msg = d.get("message") or {}
        use = msg.get("usage")
        if not use or msg.get("model") == "<synthetic>":
            return [{**base, "p": ["其他", f"{at} CLI 补的消息"], "w": max(est(text_of(msg.get("content"))), 1.0)}]
        ctx = sum(use.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        # 压缩时原样留下的回复：用量是压缩前那次请求的，不算请求，和压缩摘要一样按字数估
        req = {} if d.get("kept") else {"reply": msg.get("id") or uid, "ctx": ctx, "out": use.get("output_tokens") or 0}
        for b in msg.get("content") or []:
            kind = b.get("type")
            if kind == "text":
                add(["Claude 的回复", one_line(b.get("text"), 30) or at], est(b.get("text") or ""), **req)
            elif kind in ("thinking", "redacted_thinking"):
                words = (b.get("thinking") or "").strip()
                if words:   # 带字的思考块是说给用户听的过渡话，和回复一样算
                    add(["Claude 的回复", one_line(words, 30)], est(words), **req)
                else:
                    out.append({**base, "p": ["思考", f"{at} 思考"], "w": 0.0, "think": True, **req})
            elif kind in ("tool_use", "server_tool_use"):
                name = b.get("name") or "?"
                key = tool_key(name, b.get("input"), cwd)
                tools[b.get("id")] = (name, key)
                add(["工具调用", name, key, f"{at} 调用"], est(name + json.dumps(b.get("input"), ensure_ascii=False)), **req)
        return out
    if t == "user":
        content = (d.get("message") or {}).get("content")
        if d.get("isCompactSummary"):
            return [{**base, "p": ["压缩摘要"], "w": max(est(text_of(content)), 1.0)}]
        if d.get("isMeta"):
            text = text_of(content)
            return [{**base, "p": ["插进来的说明", one_line(text, 30) or at], "w": max(est(text), 1.0)}]
        if isinstance(content, str):
            talk(content)
            return out
        for b in content or []:
            kind = b.get("type") if isinstance(b, dict) else None
            if kind == "tool_result":
                name, key = tools.get(b.get("tool_use_id"), ("不知是哪个工具", "（调用在压缩前）"))
                c = b.get("content")
                add(["工具结果", name, key, f"{at} 结果"], est(text_of(c)) + sum(map(image_tokens, images_in(c))))
            elif kind == "text":
                talk(b.get("text") or "")
            elif kind == "image":
                add(["图片", f"{at} 图片"], image_tokens(b))
            elif kind == "document":
                add(["文档", f"{at} 文档"], est(json.dumps(b.get("source"), ensure_ascii=False)) / 3)
        return out
    if t == "attachment":
        a = d.get("attachment") or {}
        kind = a.get("type") or "?"
        if kind == "queued_command":
            talk(str(a.get("prompt") or ""))
            return out
        name = a.get("filename") or a.get("path")
        add(["附加信息", ATTACH.get(kind, kind), short_path(str(name), cwd) if name else f"{at}"],
            est(json.dumps(a, ensure_ascii=False)))
    return out


# ---------- 按请求之间的准数分摊 ----------

def spread(units, total, exact):
    w = sum(u["w"] for u in units)
    for u in units:
        u["t"] = total * u["w"] / w if w else total / len(units)
        u["e"] = not (exact and len(units) == 1)


def split_reply(units, total, exact, ratio):
    """一条回复的输出分给它的几块：看得见的字按字数，剩下的给空的思考块。"""
    think = [u for u in units if u.get("think")]
    seen = [u for u in units if not u.get("think")]
    seen_w = sum(u["w"] for u in seen) * ratio
    if think and seen_w < total:
        for u in seen:
            u["t"], u["e"] = u["w"] * ratio, True
        for u in think:
            u["t"], u["e"] = (total - seen_w) / len(think), True
    else:
        spread(seen or think, total, exact)
        for u in (think if seen else []):
            u["t"], u["e"] = 0, True


def attribute(units: list[dict], base: int | None):
    """给每条定 t（占多少 token）和 e（是不是按字数估的）。base 是系统部分（CLI 给的，不知道就是 None）。
    返回系统部分（不知道时按第一次请求估出来的，没有请求就是 None）。"""
    reqs, pre = [], []   # reqs：每次请求 {ctx, out, reply 这次回复的几块, after 回复之后进来的}
    for u in units:
        if "reply" in u:
            if not reqs or reqs[-1]["id"] != u["reply"]:
                reqs.append({"id": u["reply"], "ctx": u["ctx"], "out": u["out"], "reply": [], "after": []})
            r = reqs[-1]
            r["ctx"], r["out"] = max(r["ctx"], u["ctx"]), max(r["out"], u["out"])   # 同一条回复拆成几行写，取最大的
            r["reply"].append(u)
        else:
            (reqs[-1]["after"] if reqs else pre).append(u)
    # 字数和准数的比例：拿两次请求之间只差回复之后那些东西的组来校（代码、英文按字数会估少）
    got = [(b["ctx"] - a["ctx"] - a["out"], sum(u["w"] for u in a["after"])) for a, b in zip(reqs, reqs[1:])]
    got = [(x, w) for x, w in got if x > 0 and w > 0]
    ratio = min(3.0, max(0.6, sum(x for x, _ in got) / sum(w for _, w in got))) if got else 1.3
    for k, r in enumerate(reqs):
        delta = reqs[k + 1]["ctx"] - r["ctx"] if k + 1 < len(reqs) else None
        if delta is None:   # 最后一次请求：回复的输出是准数，之后进来的只能估
            split_reply(r["reply"], r["out"], True, ratio)
            for u in r["after"]:
                u["t"], u["e"] = u["w"] * ratio, True
        elif delta > 0 and (not r["after"] or delta > r["out"]):
            reply = min(r["out"], delta) if r["after"] else delta
            split_reply(r["reply"], reply, True, ratio)
            if r["after"]:
                spread(r["after"], delta - reply, True)
        else:   # 两次请求之间上下文没涨（中途改写过上下文之类），这一组没有准数，各自估
            split_reply(r["reply"], r["out"], False, ratio)
            for u in r["after"]:
                u["t"], u["e"] = u["w"] * ratio, True
    if not reqs:
        for u in pre:
            u["t"], u["e"] = u["w"] * ratio, True
        return base
    if base is not None and reqs[0]["ctx"] - base > 0:
        if pre:
            spread(pre, reqs[0]["ctx"] - base, True)
        return base
    for u in pre:
        u["t"], u["e"] = u["w"] * ratio, True
    return max(0, reqs[0]["ctx"] - round(sum(u["t"] for u in pre)))


# ---------- 拼成一棵树 ----------

def grow(root: dict, path: list, leaf: dict):
    node = root
    for name in path:
        node = node.setdefault("kids", {}).setdefault(name, {"n": name})
    node.setdefault("units", []).append(leaf)


def finish(node: dict, total: float, nd: int | None = None) -> dict:
    """叶子按 t 从大到小排，太碎的合成「其余 n 项」；只有一条的格子直接当叶子（点了能跳到那个气泡）。
    t 是 token 数（额度视角里是点数，nd 是留几位小数），k 是底下有几条，e 是里面有没有按字数估的，
    u 是记录行编号（叶子才有），tok 是额度视角里这一块对应多少 token。"""
    def leaf(u, name):
        out = {"n": name, "t": round(u["t"], nd), "k": 1, "e": int(u["e"]), "u": u["u"]}
        if "tok" in u:
            out["tok"] = round(u["tok"])
        return out

    def group(name, kids, **kw):
        out = {"n": name, "t": round(sum(c["t"] for c in kids), nd), "k": sum(c["k"] for c in kids),
               "e": int(any(c["e"] for c in kids)), **kw}
        if any("tok" in c for c in kids):
            out["tok"] = sum(c.get("tok", 0) for c in kids)
        return out

    kids = [finish(c, total, nd) for c in (node.get("kids") or {}).values()]
    units = node.get("units") or []
    if len(units) == 1 and not kids:
        return leaf(units[0], node["n"])
    kids += [leaf(u, u.get("label") or u["ts"] or "这一条") for u in units]
    if len(kids) == 1 and "c" not in kids[0] and "u" in kids[0]:
        return {**kids[0], "n": node["n"]}
    kids.sort(key=lambda c: -c["t"])
    keep = [c for i, c in enumerate(kids) if i < KEEP and c["t"] >= total * TINY]
    rest = kids[len(keep):]
    if len(rest) > 1:
        keep.append(group(f"其余 {len(rest)} 项", rest, rest=1))
    else:
        keep += rest
    return group(node["n"], keep, c=keep)


def cli_parts(usage: dict, cwd: str) -> list[dict]:
    """CLI 给的系统部分各格，能往下拆的（记忆文件、技能、MCP 工具、子代理、系统工具、系统提示词分段）拆开。都是准数。"""
    def leaves(items, name, unit=lambda x: x):
        return [{"n": unit(name(x)), "t": x.get("tokens") or 0, "k": 1, "e": 0} for x in items or [] if x.get("tokens")]

    more = {"Memory files": leaves(usage.get("memoryFiles"), lambda x: x.get("path") or "?", lambda p: short_path(p, cwd)),
            "Skills": leaves((usage.get("skills") or {}).get("skillFrontmatter"), lambda x: x.get("name") or "?"),
            "MCP tools": leaves([x for x in usage.get("mcpTools") or [] if x.get("isLoaded", True)],
                                lambda x: f"{x.get('serverName')} · {x.get('name')}"),
            "Custom agents": leaves(usage.get("agents"), lambda x: x.get("agentType") or "?"),
            "System tools": leaves(usage.get("systemTools"), lambda x: x.get("name") or "?"),
            "System prompt": leaves(usage.get("systemPromptSections"), lambda x: x.get("name") or "?")}
    out = []
    for c in usage.get("categories") or []:
        if c.get("kind", "used") != "used" or c.get("isDeferred") or not c.get("tokens") or c.get("name") == "Messages":
            continue
        kids = sorted(more.get(c["name"]) or [], key=lambda x: -x["t"])
        node = {"n": CATS.get(c["name"], c["name"]), "t": c["tokens"], "k": max(1, len(kids)), "e": 0}
        if len(kids) > 1:
            node["c"] = kids
            node["t"] = max(c["tokens"], sum(x["t"] for x in kids))
        out.append(node)
    return out


def compose(path: Path, usage: dict | None, cwd: str) -> dict:
    """整棵树：根是上下文总量，第一圈是系统各部分和消息，消息往下按类别、工具、文件、气泡分。
    usage 是 CLI 的 get_context_usage（没连着就是 None，系统部分只能估成一块）。"""
    tools: dict = {}
    units = [x for d in active_lines(Path(path)) for x in units_of(d, tools, cwd)]
    # 思考块是空的，光写时间分不清是哪一条：写上想完以后说了什么、或者调用了什么
    replies: dict = {}
    for u in units:
        if "reply" in u:
            replies.setdefault(u["reply"], []).append(u)
    for us in replies.values():
        then = next((u["p"][-1] for u in us if u["p"][0] == "Claude 的回复"), None) \
            or next((f"调用 {u['p'][1]}" for u in us if u["p"][0] == "工具调用"), None)
        for u in us:
            if u.get("think") and then:
                u["p"] = ["思考", f"{u['ts']} 想完：{then}"]
    sys_parts = cli_parts(usage, cwd) if usage else []
    cli_msgs = next((c.get("tokens") for c in (usage or {}).get("categories") or [] if c.get("name") == "Messages"), None)
    base = (usage.get("totalTokens") or 0) - (cli_msgs or 0) if usage and cli_msgs is not None else None
    base = attribute(units, base)
    msgs = {"n": "消息"}
    for u in units:
        grow(msgs, u["p"][:-1] if len(u["p"]) > 1 else u["p"], {**u, "label": u["p"][-1] if len(u["p"]) > 1 else None})
    total = sum(u["t"] for u in units) + (base or 0)
    msg_node = finish(msgs, total) if units else None
    top = [*sys_parts] if usage else ([{"n": "系统部分（CLI 没连着，拆不开）", "t": base, "k": 1, "e": 1}] if base else [])
    if msg_node:
        top.append(msg_node)
    top.sort(key=lambda c: -c["t"])
    return {"n": "上下文", "t": sum(c["t"] for c in top), "k": sum(c["k"] for c in top), "e": int(any(c["e"] for c in top)),
            "c": top, "cli": bool(usage), "cliTotal": (usage or {}).get("totalTokens"), "cliMessages": cli_msgs,
            "max": (usage or {}).get("maxTokens")}


_cache: dict = {}
_lock = threading.Lock()


def cached(path: Path, usage: dict | None, cwd: str) -> dict:
    """同一个文件、同一份 CLI 用量算过就不再算：悬停卡片可能一会儿开好几次。"""
    st = Path(path).stat()
    key = (str(path), st.st_size, st.st_mtime_ns, (usage or {}).get("totalTokens"), cwd)
    with _lock:
        if key in _cache:
            return _cache[key]
    tree = compose(path, usage, cwd)
    with _lock:
        _cache.clear()   # 只留最近一份：换了会话或者有新内容就是另一份了
        _cache[key] = tree
    return tree


if __name__ == "__main__":
    import sys

    def show(n, depth=0, limit=3):
        print("  " * depth + f"{n['n']}  {n['t']}{' 估' if n.get('e') else ''}  ({n['k']} 条)")
        if depth < limit:
            for c in n.get("c") or []:
                show(c, depth + 1, limit)

    show(compose(Path(sys.argv[1]), None, sys.argv[2] if len(sys.argv) > 2 else ""), limit=int(sys.argv[3]) if len(sys.argv) > 3 else 3)
