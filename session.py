"""把 Claude Code 的会话记录 (.jsonl) 增量解析成前端要画的条目。"""
import base64
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import api_calls   # 每条回复是哪个号发的：API 号的思考是明文，按号认（见 on_assistant）
import calls   # 每次调用的四个数怎么取、怎么合并：和账本、拟合同一份
import memtrim
import peak   # 按时段计价的模型：空闲时段的调用换成单价表里那一条的名字
import review_board   # 自动审稿的会话：工具结果里认 review.py 打印的工作区（见 on_user）
from hostfs import Path   # 会话记录在运行 Claude 的那个用户名下，经它读（见 hostfs.py）
from instance import API, CALL   # API 号的登记；界面上怎么称呼用查看器的人（见 instance.py）

MAX_TEXT = 30_000
TAG_RE = re.compile(r"<([\w-]+)>(.*?)</\1>", re.S)
RESTART_TAG = "viewer-restart"   # 重启后自动续上时发给 AI 的提示包在这个标签里（server.RESUME_TAG）
BLOCKED_HEAD = "UserPromptSubmit operation blocked by hook:"   # CLI 记「提交前的钩子拦下了这句话」的固定开头
# 到达限额那一行里的恢复时刻：CLI 写「resets 6am (Asia/Shanghai)」「resets Oct 18, 1pm (UTC)」，括号里是 CLI 那边的时区
# （见过 UTC 的）；预测拦截照着写的（server.en_time）不带时区，就是本机时间
RESETS_AT = re.compile(r"resets (?:([A-Z][a-z]{2}) (\d{1,2}), )?(\d{1,2})(?::(\d{2}))?(am|pm)(?: \(([^)]+)\))?")


def resets_at(text: str, ts: float) -> float | None:
    """限额那一行写的恢复时刻（unix 秒），ts 是这一行写下的时刻，按它推是哪一天；写的不是这种格式就是 None。
    server.resets_of、server.resume_tick 共用这一份"""
    m = RESETS_AT.search(text or "")
    if not m:
        return None
    mon, day, h, mi, ap, zone = m.groups()
    try:
        tz = ZoneInfo(zone) if zone else None
    except (ValueError, ZoneInfoNotFoundError):   # 认不得的时区名按本机时间算（和以前一样），不当成没写恢复时刻
        tz = None
    base = datetime.fromtimestamp(ts, tz)
    t = base.replace(hour=int(h) % 12 + (12 if ap == "pm" else 0), minute=int(mi or 0), second=0, microsecond=0)
    if mon:
        t = t.replace(month=datetime.strptime(mon, "%b").month, day=int(day))
        if t < base:
            t = t.replace(year=t.year + 1)
    elif t <= base:
        t += timedelta(days=1)
    return t.timestamp()


# DeepSeek 官方分词器（HuggingFace 上 deepseek-ai/DeepSeek-V4.1-Flash 的 tokenizer.json；deepseek-flash 就是这个版本）。
# 核对过：一次输出 52039 的回复，拿它数思考 51940、正文 98，合计只差 1；原来按「中文一字 0.6、其他一字符 0.3」估，
# 只估出 40290（那轮思考多是英文和算式）。第一次用到才读（约 6MB），读不了只在服务输出里说一次，思考不写数
DS_TOKENIZER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokenizers", "deepseek-v4.1-flash.json")   # 代码目录，以查看器自己的身份读（不经 hostfs）
_ds_tok = None
_ds_tok_err = False


def ds_tokens(text: str) -> int | None:
    global _ds_tok, _ds_tok_err
    if not text:
        return 0
    if _ds_tok is None and not _ds_tok_err:
        try:
            from tokenizers import Tokenizer
            _ds_tok = Tokenizer.from_file(DS_TOKENIZER)
        except Exception as e:
            _ds_tok_err = True
            print(f"DeepSeek 分词器读不了（{DS_TOKENIZER}）：{type(e).__name__}: {e}，气泡上不写思考 token 数", flush=True)
    return len(_ds_tok.encode(text, add_special_tokens=False).ids) if _ds_tok else None


def clip(text, limit=MAX_TEXT):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n\n…（已截断，共 {len(text)} 字符）"


def api_error_hint(text: str) -> str:
    """Claude Code 记下的 API 报错（原文多是英文）里认得出的几种，换成一句说人话的中文：出了什么事、该怎么办。
    认不出的返回空，照旧按普通报错显示。说明里拿不准的只写「可能」，原文悬停看得到。"""
    t = text or ""
    if "Request too large" in t or "request_too_large" in t or "16 MiB" in t:
        return ("请求过大，已被拒绝：对话中累积的图片、附件和长内容使单次请求超过了上游的单条上限，原样重发仍会被拒。"
                "请使用 /compact 压缩上下文，或新建会话继续。")
    if "WebSocket connection closed before terminal Codex response event" in t:
        return ("ChatGPT 在回复完成前断开了连接。"
                "如反复出现，可能是请求过大，请先压缩上下文（/compact）后重试。")
    if "is not supported when using Codex with a ChatGPT account" in t:
        return "该 GPT 账号当前无法使用此模型，可能是订阅已到期或已变更，请更换账号后重试。"
    return ""


def block_text(c):
    """工具结果/附件的内容可能是字符串、块列表或字典，统一成文字。"""
    if isinstance(c, list):
        return "\n".join(map(block_text, c))
    if not isinstance(c, dict):
        return "" if c is None else str(c)
    if c.get("type") == "tool_reference":
        return f"[加载工具 {c.get('tool_name')}]"
    if c.get("text") or c.get("content"):
        return block_text(c.get("text") or c.get("content"))
    return f"[{c['type']}]" if "type" in c else json.dumps(c, ensure_ascii=False)


def waited(start, end):
    """两个记录时间戳之间隔了几秒；缺一个或格式不对就当 0。"""
    try:
        parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (parse(end) - parse(start)).total_seconds()
    except (TypeError, ValueError, AttributeError):
        return 0


def kilo(n) -> str:
    """970652 → 970.7k，和前端的写法一样。"""
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def task_text(summary: str) -> str:
    """后台任务通知的摘要是英文的固定说法（Background command "说明" completed (exit code 0)、Monitor event: "说明" 这类）：
    页面上只留引号里的说明，有退出码就带上；前面的「后台任务完成」「监视到」由前端按状态写。认不出的原样返回"""
    m = re.match(r'[^"]*"(.+)"(.*)$', summary or "", re.S)
    if not m:
        return summary
    code = re.search(r"exit code (-?\d+)", m.group(2))
    return m.group(1) + (f"（退出码 {code.group(1)}）" if code else "")


def is_image(block):
    return isinstance(block, dict) and block.get("type") == "image"


class Session:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.gen = uuid.uuid4().hex[:8]
        self.offset, self.buf, self.inode = 0, b"", None
        self.items, self.info = [], {}
        self.ai_title = ""   # 记录里最近的自动标题；没有它时标题退回最后一句话（on_last_prompt）
        self.images = {}     # 图片编号 → 它在记录文件里的位置（见 keep_image），条目里只放编号，前端要看才来取
        self.line_at = (0, 0)   # 正在解析的这一行在记录文件里从第几个字节开始、有多长
        self.enqueued = {}   # 排队中的话 → 排进去的时间
        self.usage = {}      # 回复编号 → 这次 API 调用的四个数（同一条回复拆成好几行写，只算一次）
        self.seq_of = {}     # 记录行编号 → 它的第一个条目是第几条：上下文环形图上点一块，跳到那个气泡
        self.effort_cmds = []   # 对话里执行过的 /effort：[(时间, 参数)]，服务器拿它跟上 CLI 实际的思考强度（Tab.sync_effort）
        self.compacts = 0       # 记录里有几个压缩分界：变多了，下一轮发话时服务器重新挑号（server.repick_after_compact）
        # 正在解析的这一行是不是前面已经有过的记录行（编号相同）：CLI 有时把一整段旧记录原样再追加一遍
        # （见过压缩时在新分界前面补了九百行），这些行生成的条目标上 dup，前端不画，也不拿来改会话的状态
        self.dup = False

    # ---------- 读取 ----------
    def poll(self, full=False):
        """读取文件新追加的部分；返回是否有变化。"""
        with self.lock:
            try:
                st = self.path.stat()
            except OSError:
                self.info["missing"] = True
                return False
            if full or st.st_ino != self.inode or st.st_size < self.offset:
                self.reset()
                self.inode = st.st_ino
            if st.st_size == self.offset:
                return False
            begin = self.offset
            # 分块读：第一次打开大会话时新增的部分就是整个文件（几百 MB），一口气读进来再切行，
            # 解析时内存要冲到文件的两倍多，释放以后也留在进程里（见 memtrim.py）
            with self.path.open("rb") as f:
                f.seek(self.offset)
                while chunk := f.read(calls.CHUNK):
                    start = self.offset - len(self.buf)   # 这一批第一行的开头（buf 是上次读到一半的那行）
                    self.offset += len(chunk)
                    *lines, self.buf = (self.buf + chunk).split(b"\n")
                    for line in lines:
                        self.line_at, start = (start, len(line)), start + len(line) + 1
                        if not line.strip():
                            continue
                        try:
                            self.ingest(json.loads(line))
                        except (ValueError, KeyError, TypeError, AttributeError):
                            self.add({"kind": "system", "text": "（有一行无法解析）"})
            self.info.update(missing=False, size=st.st_size, mtime=st.st_mtime)
            read = self.offset - begin
        memtrim.read(read)
        return True

    def add(self, item, entry=None):
        if entry:
            item.setdefault("ts", entry.get("timestamp"))
            if entry.get("isSidechain"):
                item["sidechain"] = True
            if entry.get("uuid"):
                self.seq_of.setdefault(entry["uuid"], len(self.items))
            if self.dup:
                item["dup"] = True
        self.items.append(item)

    def ingest(self, e):
        handler = getattr(self, "on_" + str(e.get("type")).replace("-", "_"), None)
        if handler:
            # seq_of 里有的就是前面生成过条目的记录行；一行拆成几个条目时要在处理之前判断，不然第二个条目会被当成重复
            self.dup = e.get("uuid") in self.seq_of
            handler(e)

    # ---------- 各类条目 ----------
    def on_user(self, e):
        content = e["message"]["content"]
        if e.get("isMeta"):
            item = {"kind": "meta", "text": clip(block_text(content))}
            # loop、定时任务到点时 CLI 塞进来的那句（turnOrigin 是 scheduled）：CLI 拿它开了新的一轮，和用户说话一样算一轮的开头（前端 tabs.js 的 cuts）。
            # 不当分界的话，上一轮的汇报会和醒来后的这一轮连成一轮，被收进工作过程
            if e.get("turnOrigin") == "scheduled":
                item["turn"] = True
            return self.add(item, e)
        if isinstance(content, str):
            return self.on_user_text(content, e)
        for i, b in enumerate(content):
            kind = b.get("type")
            if kind == "tool_result":
                body = b.get("content")
                shots = [(j, x) for j, x in enumerate(body) if is_image(x)] if isinstance(body, list) else []
                item = {"kind": "result", "id": b.get("tool_use_id"),
                        "text": clip(block_text([x for x in body if not is_image(x)] if shots else body)),
                        "error": bool(b.get("is_error"))}
                # 自动审稿：review.py 第一行打印审稿工作区，记进会话信息，前端据此给这个会话「审稿看板」的入口（review_board.py）。
                # 最近用的排最后，看板默认打开它
                if not self.dup and (ws := review_board.ws_from_result(item["text"])):
                    seen = [w for w in self.info.get("reviewWs") or [] if w != ws]
                    self.info["reviewWs"] = seen + [ws]
                # Read 的结果另外记着读了几行、文件一共几行（结果文本可能被截断，不能靠它数）
                tur = e.get("toolUseResult")
                if isinstance(tur, dict) and isinstance(tur.get("file"), dict) and "numLines" in tur["file"]:
                    f = tur["file"]
                    item["lines"] = {"num": f["numLines"], "start": f.get("startLine"), "total": f.get("totalLines")}
                # 工具结果里的图（Read 读的图片、截图类工具返回的）：和贴的图一样只给编号，前端点开了才来取
                if shots:
                    dims = (tur.get("file") or {}).get("dimensions") if isinstance(tur, dict) and len(shots) == 1 else None
                    item["images"] = [self.keep_image(x, f"{len(self.items)}.{j}", (i, j), dims) for j, x in shots]
                self.add(item, e)
            elif kind == "text":
                self.on_user_text(b["text"], e)
            elif kind == "image":
                key = str(len(self.items))
                self.add({"kind": "user", "text": "", "image": self.keep_image(b, key, (i,))["key"]}, e)

    def keep_image(self, block, key, where, dims=None):
        """记下一张图在记录文件里的位置（哪一行、这一行里的哪一块），图片数据本身不留在内存里，前端点开了才按位置回去读（read_image）。
        Agent 一轮能读十几张图，一张几百 KB，常驻内存的话会话开着就一直占着。
        返回给前端的说明：编号、类型、大概多大，Read 读的图还有模型看到的尺寸；记录里没有图片数据时编号为空、写明原因"""
        src = block.get("source") or {}
        if src.get("type") != "base64" or not src.get("data"):
            return {"key": None, "why": f"会话记录中没有该图片的数据（来源：{src.get('type') or '未知'}）"}
        self.images[key] = (*self.line_at, where)
        meta = {"key": key, "type": src.get("media_type") or "image/png", "bytes": len(src["data"]) * 3 // 4}
        if isinstance(dims, dict) and dims.get("displayWidth"):
            meta.update(w=dims["displayWidth"], h=dims.get("displayHeight"))
        return meta

    def read_image(self, key):
        """按 keep_image 记下的位置回到记录文件里读出这张图：返回 (类型, 图片字节)，没有这个编号返回 None。
        记录只往后追加，位置不会变；文件被整个换掉时 poll 会从头重新解析、重新记位置"""
        with self.lock:
            ref = self.images.get(key)
        if not ref:
            return None
        pos, size, where = ref
        with self.path.open("rb") as f:
            f.seek(pos)
            node = json.loads(f.read(size))["message"]["content"][where[0]]
        if len(where) > 1:
            node = node["content"][where[1]]
        src = node["source"]
        return src.get("media_type") or "image/png", base64.b64decode(src["data"])

    def on_user_text(self, text, e):
        tags = dict(TAG_RE.findall(text))
        if "task-notification" in tags and not TAG_RE.sub("", text).strip():
            # 单独一条用户消息的通知是 Claude 空闲时到的：CLI 拿它开了新的一轮，和用户说话一样算一轮的开头
            return self.on_task(tags["task-notification"], e, turn=True)
        if RESTART_TAG in tags and not TAG_RE.sub("", text).strip():
            # 实例重启后查看器发给 AI 的提示（server.RESUME_TEXT）：用户看不到，只当一轮的分界（前端 tabs.js 的 cuts）
            return self.add({"kind": "restart", "turn": True}, e)
        if tags and not TAG_RE.sub("", text).strip():
            self.add({"kind": "command", "tags": {k: clip(v.strip()) for k, v in tags.items()}}, e)
        elif text.startswith("[Request interrupted"):
            self.add({"kind": "notice", "text": f"{CALL}中断了本轮"}, e)
        else:
            # 网页（SDK）发的话在 Claude 忙时会先排队、等这一轮结束才送进去；排过队的也算插话。
            # 用户的话再长也不截，太长了在页面上右键折叠
            item = {"kind": "user", "text": text}
            if waited(self.enqueued.pop(text, None), e.get("timestamp")) > 1:
                item["queued"] = True
            # 压缩摘要、带来源标记的（CLI 自己塞的）不是用户说的话：先照旧画成绿色气泡，前端默认收起
            if e.get("isCompactSummary") or e.get("origin"):
                item["auto"] = True
            self.add(item, e)

    def on_task(self, body, e, turn=False):
        """后台任务（放到后台跑的命令、子代理、监视器）有了结果时，CLI 自动塞进会话的通知。它以用户消息的样子写进记录，
        其实不是用户说的话：单独成一类，页面上画成一行灰字（Claude 正忙时排队插进来的也一样，见 on_attachment）。
        turn：Claude 空闲时到的，CLI 拿它开了新的一轮，前端和用户说话一样按它把前后分成两轮；正忙时插进来的在这一轮中间，不分轮。
        监视器的通知不代表任务结束，事件内容另记在 event 里。"""
        def get(tag):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.S)
            return m.group(1).strip() if m else ""
        item = {"kind": "task", "status": get("status"), "text": clip(task_text(get("summary")) or body.strip())}
        if event := get("event"):
            item["event"] = clip(event)
        if turn:
            item["turn"] = True
        self.add(item, e)

    def on_queue_operation(self, e):
        content = e.get("content")
        if not isinstance(content, str):
            return
        if e.get("operation") == "enqueue":
            self.enqueued[content] = e.get("timestamp")
        elif e.get("operation") == "remove":   # 终端里的插话被当场吸收，另有记录显示，不用再等
            self.enqueued.pop(content, None)

    def api_account(self, mid) -> bool:
        """这条回复是不是 API 号（instance.json 的 api）发的。查不到先 refresh 一次：可能是刚记上、索引还没读进来的行。"""
        if not mid:
            return False
        if (acc := api_calls.INDEX.account(mid)) is None:
            api_calls.INDEX.refresh()
            acc = api_calls.INDEX.account(mid)
        return acc in API

    def on_assistant(self, e):
        msg = e["message"]
        # 重复追加的旧回复不算最新状态：模型、思考强度、上下文都照旧（用量按回复编号取大，重复了也不会多算）
        if not self.dup:
            self.info["model"] = msg.get("model", self.info.get("model"))
        # CLI 在每条回复上记着这次请求实际用的思考强度（老版本 CLI 写的记录里没有）。
        # 只取主对话的：子代理的强度可以不一样
        if e.get("effort") and not e.get("isSidechain") and not self.dup:
            self.info["usedEffort"], self.info["usedEffortTs"] = e["effort"], e.get("timestamp")
        # 每条都带上属于哪条回复（一个编号就是一次 API 调用）、这次调用的四个数、当时上下文多大：
        # 整轮卡片拿来算元信息，以后按气泡、按会话统计用量也靠它。
        # CLI 会把同一条回复拆成好几行写、每行都带一份用量，取数和合并都按 calls.py（和账本、拟合同一套）；
        # 会话累计 = 每次调用四个数之和
        # 模型：换算点数时单价按模型分。按时段计价的模型（DeepSeek）空闲时段的换成单价表里那一条的名字（peak.py），
        # 气泡上的花费、模型名后面的「空闲」都靠它；会话记着的模型（上面 info）照旧是原名
        meta = {"mid": msg.get("id"), "model": peak.tag(msg.get("model"), e.get("timestamp"))}
        if e.get("effort"):   # 这次请求实际用的思考强度，悬停气泡时写在模型名后面；老版本 CLI 的记录里没有就不写
            meta["effort"] = e["effort"]
        if now := calls.usage_of(msg):
            ctx = now["in"] + now["cw"] + now["cr"]
            if not self.dup:
                self.info["context"] = ctx
            key = msg.get("id") or e.get("uuid")
            prev = self.usage.get(key)
            cur = self.usage[key] = calls.merge(prev, now)
            tot = self.info.setdefault("tokens", dict.fromkeys(now, 0))
            for k in now:
                tot[k] += cur[k] - (prev[k] if prev else 0)
            meta.update(out=cur["out"], ctx=ctx, use=cur)
        if e.get("isApiErrorMessage"):
            text = block_text(msg.get("content"))
            if hint := api_error_hint(text):   # 认得出的报错单独成一行中文说明，不归在系统消息里藏起来（见 api_error_hint）
                return self.add({"kind": "apierr", "text": hint, "raw": text}, e)
            # 上游报的限额（CLI 标成 rate_limit：「You've hit your session / weekly limit」「Request rejected (429)」）记上 limit，
            # 「额度恢复后自动继续」认它（server.limit_stop），不靠原文：原文有好几种写法
            return self.add({"kind": "error", "text": text, **({"limit": True} if e.get("error") == "rate_limit" else {})}, e)
        for b in msg.get("content") or []:
            kind = b.get("type")
            # 正文和用户的话一样不截，太长了在页面上右键折叠
            if kind == "text":
                self.add({"kind": "text", "text": b["text"], **meta}, e)
            # API 号的思考块装的是真正的思考（明文全文），照思考画。按调用表里这条回复是哪个号发的来认：
            # ccapi1 的模型名和订阅号一样是 claude-opus-5-5，看模型名认不出来（英文思考链会混进正文）。
            # 网页对话在 CLI 写这一行之前就记上了号（api_calls.note）。
            # 模型名开头的那条留着：调用表里查不到号的老回复（开始记调用以前的、终端里的）照旧按模型名认。
            # DeepSeek 带上多少 token（气泡头部写「思考 51.9k」），用官方分词器数（ds_tokens）；数不了就不写，不拿估的数顶上；
            # 智谱和 ccapi1 没有分词器，不写 token 数
            elif kind == "thinking" and ((msg.get("model") or "").startswith(("deepseek-", "glm-"))
                                         or ((b.get("thinking") or "").strip() and self.api_account(msg.get("id")))):
                t = b.get("thinking") or ""
                tokens = ds_tokens(t) if (msg.get("model") or "").startswith("deepseek-") else None
                self.add({"kind": "thinking", "text": clip(t), "tokens": tokens, **meta}, e)
            # CC 不把真正的思考写进记录（思考块是空的）；带字的思考块装的是说给用户听的过渡话，算回复，
            # 不然用户折叠思考时它们会跟着被藏掉。标上 thought：工作过程里的正文够长就露在外面（item-row.js 的 TALK_CHARS），
            # 只数真正的正文块，这些过渡话和英文的内心独白不算
            elif kind == "thinking" and (b.get("thinking") or "").strip():
                self.add({"kind": "text", "text": b["thinking"], "thought": True, **meta}, e)
            elif kind in ("thinking", "redacted_thinking"):
                self.add({"kind": "thinking", "text": clip(b.get("thinking", "")), **meta}, e)
            elif kind in ("tool_use", "server_tool_use"):
                self.add({"kind": "tool", "id": b.get("id"), "name": b.get("name"), "input": b.get("input"), **meta}, e)

    def on_system(self, e):
        sub = e.get("subtype")
        if sub == "turn_duration":
            return self.add({"kind": "duration", "ms": e.get("durationMs")}, e)
        if sub == "compact_boundary":
            # 压缩分界里记着压缩前后各多少 token：上下文用量当场换成压缩后的，不用等下一句回复才掉下来
            meta = e.get("compactMetadata") or {}
            pre, post = meta.get("preTokens"), meta.get("postTokens")
            if not self.dup:
                self.compacts += 1
                if post:
                    self.info["context"] = post
            how = {"auto": "自动", "manual": "手动"}.get(meta.get("trigger"), "")
            size = f"：{kilo(pre)} → {kilo(post)}" if pre and post else ""
            return self.add({"kind": "notice", "text": f"上下文已{how}压缩{size}"}, e)
        if sub == "local_command" and not self.dup and (run := e.get("commandRun") or {}).get("command") == "effort":
            # 参数只在记录里有：SDK 交过来的结果消息只有一句英文说明，不带参数
            self.effort_cmds.append((e.get("timestamp"), str(run.get("args") or "").strip()))
        text = e.get("content") or e.get("apiRefusalExplanation") or ""
        if sub == "api_error" and not text:
            # 接口报错的原因记在 error 字段里，没有 content：不取出来的话网页上展开是空的
            err = e.get("error") or {}
            text = err.get("formatted") or err.get("message") or json.dumps(err, ensure_ascii=False)
            if e.get("retryAttempt"):
                wait = f"{e['retryInMs'] / 1000:g} 秒后" if e.get("retryInMs") is not None else ""
                text += f"\n{wait}第 {e['retryAttempt']} 次重试，最多 {e.get('maxRetries', '?')} 次"
        if sub == "informational" and text.startswith(BLOCKED_HEAD):
            return self.on_prompt_blocked(text[len(BLOCKED_HEAD):].strip(), e)
        self.add({"kind": "system", "subtype": sub, "level": e.get("level"), "text": clip(text)}, e)

    def on_prompt_blocked(self, reason, e):
        """提交前的钩子把一句话拦下了：CLI 不把这句话写进记录，只记一条英文的系统提示，以前页面上是收起来的一行
        「system · informational」，用户看不出这句话根本没发出去（查看器卡住时，新开会话发的话
        会全被拦掉，留下一串没标题的空会话）。改成一直看得到的一行，带上被拦下的原话，好放回输入框重发。
        钩子是查看器自己挂的（chat.Driver._guard），超时就是查看器那么久没回应；不是超时的，原因就是预测拦截写的说明。"""
        prompt = next(reversed(self.enqueued), None)   # 拦下的那句排过队，却没等到写进记录
        if prompt is not None:
            self.enqueued.pop(prompt)
        m = re.search(r"timed out after (\d+)\s*ms", reason)
        why = f"查看器 {int(m.group(1)) // 1000} 秒没回应提交前的钩子" if m else reason
        self.add({"kind": "blocked", "text": f"消息未发送：{why}", "prompt": prompt}, e)

    def on_attachment(self, e):
        a = e["attachment"]
        kind = a.get("type")
        if kind == "queued_command":
            prompt = str(a.get("prompt"))
            # 后台任务通知在 Claude 正忙时到，CLI 先排队、在这一轮中间插进去：和空闲时到的一样画成一行灰字，只是不开新的一轮。
            # 不能画成用户的绿色插话气泡，看着像用户说的，还把 Claude 的一轮从中间切成两段
            tags = dict(TAG_RE.findall(prompt))
            if "task-notification" in tags and not TAG_RE.sub("", prompt).strip():
                return self.on_task(tags["task-notification"], e)
            item = {"kind": "user", "text": prompt, "queued": True}
            # 排进来的不只是用户的插话（commandMode 是 prompt），还有 CLI 自己排的后台任务通知这些：
            # 不是用户说的，同上标成 auto，前端默认收起
            if a.get("commandMode", "prompt") != "prompt" or a.get("origin"):
                item["auto"] = True
            return self.add(item, e)
        if kind == "hook_stopped_continuation":
            # 钩子让 CLI 停在这里：查看器的预测拦截（预计下一次请求会把额度扣到 0 以下）就是这样停的，
            # 说明是仿照官方写的「到达限额」那一行，CLI 自己记进记录。和别的附件不一样，它要一直看得到
            return self.add({"kind": "limit", "text": str(a.get("message") or "Claude Code 被钩子停下了")}, e)
        text = a.get("text") or a.get("content") or a.get("filename") or a.get("path") \
            or "\n".join(r.get("content", "") for r in e.get("rendered") or [])
        self.add({"kind": "attachment", "subtype": kind, "text": clip(block_text(text), 3000)}, e)

    def on_ai_title(self, e):
        self.ai_title = e.get("aiTitle") or self.ai_title
        self.info["title"] = self.info.get("customTitle") or self.ai_title or self.info.get("title")

    def on_last_prompt(self, e):
        """CLI 没起自动标题（独立环境里就起不出来）时，标题用最后一句话，和会话卡片（server.read_title）同一个规则，
        不然标签页上只剩文件名，也就是一串会话编号。"""
        if not self.info.get("customTitle") and not self.ai_title and e.get("lastPrompt"):
            self.info["title"] = e["lastPrompt"][:60]

    def on_custom_title(self, e):
        """在 CLI 里手动改的名字，优先于自动生成的标题。"""
        self.info["customTitle"] = self.info["title"] = e.get("customTitle")

    def on_permission_mode(self, e):
        self.info["mode"] = e.get("permissionMode")

    def on_cost_state(self, e):
        self.info["cost"] = e.get("totalCostUSD")

    def on_continued_in(self, e):
        sid = e.get("continuedInSessionId")
        self.add({"kind": "continued", "path": str(self.path.with_name(f"{sid}.jsonl"))}, e)
