"""经查看器本机接口跑的 query / ClaudeSDKClient。

每次调用交给查看器登记成一份任务（workflows.py），查看器用原生 SDK 起 CLI 去跑，这边按顺序取回每条消息，
还原成同名的官方类型交给调用方。选项只发跟官方默认值不一样的字段，查看器逐项核对；回调这类查看器那边调不到的，在这里就报错。
号由环境变量指定（都不设就由查看器自动挑）：
  CCVIEWER_ACCOUNT  直接指定号（先用 ccrun pool 看号池）
  CCVIEWER_GROUP    缓存分组：同一组的调用固定在第一次挑中的号上
  CCVIEWER_PARALLEL 同一个号上最多并行几份（默认 1）
  CCVIEWER_TIMEOUT  单次最长秒数（默认 2400）
  CCVIEWER_LABEL    任务名，查任务时好认
续接和分叉（resume / fork_session）自动用原会话的号，不用设。注意别和查看器自己设的 CC_VIEWER_ACCOUNT（当前网页会话的号）弄混。
"""
import asyncio
import dataclasses
import json
import os
import socket
import uuid
from pathlib import Path

import claude_agent_sdk as _sdk
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, CLIConnectionError, ResultMessage
from claude_agent_sdk import types as _types

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
# 只在调用方进程里起作用、不改变发给模型的东西，不交给查看器
LOCAL_ONLY = {"stderr", "debug_stderr", "max_buffer_size"}
POLL = 0.5


class ViewerError(ClaudeSDKError):
    """查看器拒绝、任务失败（额度不够、核验没过、超时……），说明在消息里。"""


def _request(req, timeout=20):
    path = os.environ.get("CC_VIEWER_PERSONA")
    if not path:
        raise CLIConnectionError("找不到查看器本机接口：脚本要由查看器网页会话拉起（比如会话里的 Bash 工具），不能经 SSH 或脱离会话运行")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(json.dumps({"op": "workflow", "request": req}, ensure_ascii=False).encode() + b"\n")
        buf = bytearray()
        while not buf.endswith(b"\n"):
            part = s.recv(1 << 16)
            if not part:
                raise CLIConnectionError("查看器没有回话就断开了")
            buf.extend(part)
    reply = json.loads(buf)
    if reply.get("exit"):
        raise ViewerError("查看器：" + (reply.get("err") or "拒绝了请求").strip())
    return json.loads(reply["out"])


def _submit(task):
    # 同一个 key 重交只会取回原任务，断线重试不会重复花钱
    last = None
    for _ in range(3):
        try:
            return _request({"op": "submit", "task": task})
        except (OSError, CLIConnectionError) as e:
            last = e
    raise CLIConnectionError(f"连不上查看器：{last}")


def _plain(name, v):
    if isinstance(v, Path):
        return str(v)
    if callable(v):
        raise ValueError(f"查看器入口不支持 {name}：回调在调用方进程里，查看器那边调不到")
    if isinstance(v, dict):
        return {str(k): _plain(name, x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(name, x) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    raise ValueError(f"查看器入口不支持 {name}（{type(v).__name__} 传不过去）")


def _options(options):
    """只取跟官方默认值不一样的字段；cwd 不写时和官方一样用本进程的当前目录。"""
    default = ClaudeAgentOptions()
    out = {}
    for f in dataclasses.fields(ClaudeAgentOptions):
        if f.name in LOCAL_ONLY:
            continue
        v = getattr(options, f.name)
        if v == getattr(default, f.name):
            continue
        out[f.name] = _plain(f.name, v)
    out["cwd"] = str(Path(options.cwd).resolve()) if options.cwd else os.getcwd()
    if options.add_dirs:
        out["add_dirs"] = [str(Path(d).resolve()) for d in options.add_dirs]
    s = out.get("settings")
    if isinstance(s, str) and not s.strip().startswith("{"):
        # 官方把文件路径交给 CLI 去读；这边读成 JSON 再交过去，查看器那边读不到调用方的文件
        out["settings"] = Path(s).read_text(encoding="utf-8")
    return out


async def _prompt(prompt):
    if isinstance(prompt, str):
        return prompt
    msgs = []
    if hasattr(prompt, "__aiter__"):
        async for m in prompt:
            msgs.append(m)
    else:
        msgs = list(prompt)
    return _plain("prompt", msgs)


def _task(options, prompt):
    def num(name, default):
        v = os.environ.get(name)
        try:
            return int(v) if v else default
        except ValueError:
            raise ValueError(f"{name} 要写整数") from None
    return {"key": uuid.uuid4().hex, "label": os.environ.get("CCVIEWER_LABEL") or "SDK 调用",
            "account": os.environ.get("CCVIEWER_ACCOUNT") or "auto", "group": os.environ.get("CCVIEWER_GROUP") or "",
            "parallel": num("CCVIEWER_PARALLEL", 1), "timeout": num("CCVIEWER_TIMEOUT", 2400),
            "sdk": {"options": _options(options), "prompt": prompt}}


def _decode(o):
    if isinstance(o, list):
        return [_decode(x) for x in o]
    if not isinstance(o, dict):
        return o
    if "__t" not in o:
        return {k: _decode(v) for k, v in o.items()}
    name = o["__t"]
    cls = getattr(_types, name, None) or getattr(_sdk, name, None)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise ViewerError(f"本机的 claude_agent_sdk（{_sdk.__version__}）认不出查看器发来的消息类型 {name}，两边 SDK 版本可能不一样")
    kw = {k: _decode(v) for k, v in o.items() if k != "__t"}
    names = {f.name for f in dataclasses.fields(cls) if f.init}
    if bad := set(kw) - names:
        raise ViewerError(f"本机的 claude_agent_sdk（{_sdk.__version__}）的 {name} 没有这些字段：{'、'.join(sorted(bad))}，两边 SDK 版本可能不一样")
    return cls(**kw)


async def _run(task, on_submit=None):
    """交一份任务，按顺序吐出它的每条消息；失败就在吐完已有的消息后抛 ViewerError。调用方中途不要了就取消任务。"""
    view = await asyncio.to_thread(_submit, task)
    jid, since, done = view["id"], 0, False
    if on_submit:
        on_submit(jid)
    try:
        while True:
            r = await asyncio.to_thread(_request, {"op": "messages", "id": jid, "since": since})
            for m in r["messages"]:
                yield _decode(m)
            since = r["next"]
            if r["state"] in TERMINAL and not r["messages"]:
                done = True
                if r["state"] != "completed":
                    raise ViewerError(f"查看器任务{_STATE.get(r['state'], r['state'])}（号 {r.get('account') or '未分配'}）："
                                      f"{r.get('error') or '没有说明'}")
                return
            if not r["messages"]:
                await asyncio.sleep(POLL)
    finally:
        if not done:
            try:
                await asyncio.shield(asyncio.to_thread(_request, {"op": "cancel", "id": jid}))
            except Exception:
                pass


_STATE = {"failed": "失败", "cancelled": "已取消", "interrupted": "被查看器重启打断"}


async def query(*, prompt, options=None, transport=None):
    """同官方 query：一次性调用，按顺序吐出消息。"""
    if transport is not None:
        raise ValueError("查看器入口不支持自定义 transport")
    task = _task(options or ClaudeAgentOptions(), await _prompt(prompt))
    async for m in _run(task):
        yield m


_END = object()   # 打断以后让正在等的 receive_response 结束


class ClaudeSDKClient:
    """同官方 ClaudeSDKClient 的常用部分：connect / query / receive_messages / receive_response / interrupt / disconnect。
    每次 query 是查看器里的一份任务，第二次起接着上一次的会话往下（resume），号不变，缓存照常命中。"""

    def __init__(self, options=None, transport=None):
        if transport is not None:
            raise ValueError("查看器入口不支持自定义 transport")
        self.options = options or ClaudeAgentOptions()
        self._session = None
        self._queue = asyncio.Queue()
        self._job = None
        self._jid = None
        self._connected = False

    async def connect(self, prompt=None):
        self._connected = True
        if prompt is not None:
            await self.query(prompt)

    async def query(self, prompt, session_id="default"):
        if not self._connected:
            raise CLIConnectionError("Not connected. Call connect() first.")
        if self._job and not self._job.done():
            await asyncio.wait({self._job})   # 上一轮跑完再接着发（它的消息照常进队列）
        opts = self.options if self._session is None else dataclasses.replace(
            self.options, resume=self._session, fork_session=False, session_id=None,
            resume_session_at=None, resume_drops_turn=None)
        task = _task(opts, await _prompt(prompt))
        self._job = asyncio.create_task(self._pump(task))

    async def _pump(self, task):
        try:
            async for m in _run(task, on_submit=lambda jid: setattr(self, "_jid", jid)):
                if isinstance(m, ResultMessage):
                    self._session = m.session_id
                await self._queue.put(m)
        except asyncio.CancelledError:
            await self._queue.put(_END)
            raise
        except Exception as e:
            await self._queue.put(e)

    async def receive_messages(self):
        while True:
            m = await self._queue.get()
            if m is _END:
                return
            if isinstance(m, BaseException):
                raise m
            yield m

    async def receive_response(self):
        async for m in self.receive_messages():
            yield m
            if isinstance(m, ResultMessage):
                return

    async def interrupt(self):
        """取消正在跑的这一轮（查看器那边收掉 CLI）。会话记录还在，下一次 query 接着它往下。"""
        if self._job and not self._job.done():
            self._job.cancel()
            await asyncio.gather(self._job, return_exceptions=True)

    async def disconnect(self):
        await self.interrupt()
        self._connected = False

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()
        return False

    def _unsupported(self, *a, **k):
        raise NotImplementedError("查看器入口暂不支持这个方法")

    set_permission_mode = set_model = rewind_files = reconnect_mcp_server = toggle_mcp_server = _unsupported
    stop_task = get_mcp_status = get_context_usage = get_server_info = _unsupported
