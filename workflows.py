"""本机 SDK 编程入口：ccrun / ccviewer_sdk → 人设卡套接字认出会话 → 持久任务 → 原生 SDK Driver。

两种请求。旧的 ccrun 请求（prompt/system/tools/文件清单）照旧套一份查看器定的干净配置，自动审稿在用；
带 sdk 的是包装 SDK（sdk_client/ccviewer_sdk，接口同官方 claude_agent_sdk）交来的：影响模型看到什么的选项
都按调用方写明的来，这里只核对、不改写，查看器补的只有部署相关的（号、代理、登录、计费标记、网络闸门、额度钩子）。
不提供 Messages API，不拿走或复制账号凭据。访问名单在 DATA/workflow_access.json，缺文件就不开；
提交的 key 用来防止断线重试重复花钱。任务归属在启动前写进现有用户表，用量仍从会话记录结算，不另外记一份钱。
用法见 README「脚本接口」。
"""
import asyncio
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

import hostfs
import isolation
import reserve
import users
from instance import DATA

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
TOOLS = {"Read", "Edit", "Write"}
# 干净的运行里只该有这几种附加内容：环境、模型、用户邮箱、日期、组织编号、系统提示快照、工具调用底账（带工具才有，不发给模型），
# 以及 Claude Code 自带的两种提醒：剩余 token 提醒（只有一个数字）、长时间只调工具不说话时的静默轮提醒（一句固定的英文，
# 连调几十次工具时出现）。只认这几种：几十次实际运行里出现的就是这些，认不出的新类型一律判不干净
AUDIT_ATTACHMENTS = {"environment", "model", "session_context", "date", "credential_org", "prompt_snapshot",
                     "deferred_tools_record", "total_tokens_reminder", "silent_turn_reminder"}
IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
# 每人未结束的任务最多收这么多份，同时执行也以它为上限，一个人排满不挡别人。
# 号那一侧另有保护：同一个号上默认一份接一份，调用方可以要求并行（parallel）
MAX_JOBS = 24
MAX_PARALLEL = 8
_service = None

# ---------- 包装 SDK 的选项 ----------
# 调用方只发跟官方默认值不一样的字段（ccviewer_sdk 里比对 ClaudeAgentOptions()）。下面没列的字段一律拒绝，不悄悄丢掉。
# 环境变量、设置、额外参数只收这几个：别的能把请求改道（接口地址、密钥、代理）或读进机器上的东西
SDK_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY", "ENABLE_CLAUDEAI_MCP_SERVERS", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL",
           "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "MAX_THINKING_TOKENS", "CLAUDE_AGENT_SDK_CLIENT_APP"}
SDK_SETTINGS = {"autoMemoryEnabled", "disableClaudeAiConnectors"}
SDK_EXTRA = {"disable-slash-commands"}
# bypassPermissions 放开（ccagent --bypass）：派出去的子代理是主代理的工具，由主代理负责，和 Claude Code 自带的子代理一样能全开
SDK_MODES = {"default", "dontAsk", "acceptEdits", "bypassPermissions"}
TOOLS_PRESET = {"type": "preset", "preset": "claude_code"}   # 工具写成这个就是 Claude Code 默认的全套工具
SDK_FIELDS = {"system_prompt", "tools", "allowed_tools", "disallowed_tools", "setting_sources", "skills", "mcp_servers",
              "strict_mcp_config", "model", "effort", "thinking", "max_turns", "max_budget_usd", "permission_mode",
              "cwd", "add_dirs", "env", "settings", "extra_args", "verbatim_prompts",
              "resume", "fork_session", "session_id", "resume_session_at", "resume_drops_turn"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_run(sid):
    return bool((users.owners.get(sid) or {}).get("workflow"))


_kids = (0, 0.0, {})   # (登记的会话数, 建的时刻, 发起会话 → 它派出去的工作流会话)


def runs_of(parent):
    """从这个会话派出去的工作流任务（各自的会话编号）。会话的花费要把它们算进来（server.spend_fields）。
    登记一份任务时先记会话主人、再补 workflow，会话数变了或者过了十秒就重建，不会一直漏掉刚补上的"""
    global _kids
    n, at, kids = _kids
    if n != len(users.owners) or time.monotonic() - at > 10:
        kids = {}
        for sid, o in list(users.owners.items()):
            w = (o or {}).get("workflow")
            if isinstance(w, dict) and w.get("parent"):
                kids.setdefault(w["parent"], []).append(sid)
        _kids = (len(users.owners), time.monotonic(), kids)
    return kids.get(parent, [])


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def check_sdk(raw):
    """包装 SDK 交来的 {options, prompt}：核对、整理，不补任何影响模型看到什么的东西。不合规就说清楚该怎么写。"""
    if not isinstance(raw, dict) or set(raw) - {"options", "prompt"}:
        raise ValueError("sdk 只收 options 和 prompt")
    o, prompt = dict(raw.get("options") or {}), raw.get("prompt")
    if bad := set(o) - SDK_FIELDS:
        raise ValueError("查看器入口不支持这些选项：" + "、".join(sorted(bad)) + "（见 README「脚本接口」）")
    # 共用机器上不许读进运行 Claude 的那个用户自己的东西：设置、CLAUDE.md、技能、MCP、自动记忆、claude.ai 连接器
    if o.get("setting_sources") != []:
        raise ValueError("请明写 setting_sources=[]：不写时官方 SDK 会加载全部设置和 CLAUDE.md")
    if o.get("skills") != []:
        raise ValueError("请明写 skills=[]：不写时 Claude Code 照旧列出机器上装的技能")
    if o.get("mcp_servers"):
        raise ValueError("查看器入口不支持 MCP 服务器")
    if o.get("strict_mcp_config") is not True:
        raise ValueError("请明写 strict_mcp_config=True，不读机器上其他地方的 MCP 配置")
    env = o.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ValueError("env 必须是文字到文字的字典")
    if bad := set(env) - SDK_ENV:
        raise ValueError("env 里不支持这些变量：" + "、".join(sorted(bad)))
    settings = o.get("settings")
    if settings is not None:
        try:
            settings = json.loads(settings) if isinstance(settings, str) else settings
        except ValueError:
            raise ValueError("settings 要写成 JSON 文字（文件路径由客户端读成 JSON 再交过来）") from None
        if not isinstance(settings, dict) or (bad := set(settings) - SDK_SETTINGS):
            raise ValueError("settings 只支持 " + "、".join(sorted(SDK_SETTINGS)))
        o["settings"] = settings
    settings = settings or {}
    if env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") != "1" and settings.get("autoMemoryEnabled") is not False:
        raise ValueError("请关掉自动记忆：env 里写 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1，或 settings 里写 autoMemoryEnabled: false")
    if env.get("ENABLE_CLAUDEAI_MCP_SERVERS") != "false" and settings.get("disableClaudeAiConnectors") is not True:
        raise ValueError("请关掉 claude.ai 连接器：env 里写 ENABLE_CLAUDEAI_MCP_SERVERS=false，或 settings 里写 disableClaudeAiConnectors: true")
    extra = o.get("extra_args") or {}
    if not isinstance(extra, dict) or (bad := set(extra) - SDK_EXTRA) or any(v is not None for v in extra.values()):
        raise ValueError("extra_args 只支持 " + "、".join(sorted(SDK_EXTRA)) + "（值写 None）")
    sp = o.get("system_prompt")
    if not (sp is None or isinstance(sp, str) or isinstance(sp, dict) and (
            sp.get("type") == "custom" and isinstance(sp.get("prompt"), str)
            or sp.get("type") == "preset" and sp.get("preset") == "claude_code" and isinstance(sp.get("append", ""), str))):
        raise ValueError("system_prompt 只支持文字、custom 或 claude_code 预设（不支持从文件读）")
    tools = o.get("tools")
    # 工具名不只限 Read、Edit、Write：子代理是调用方的工具，用什么由调用方定、由调用方负责；
    # 仍要明写，不写时 Claude Code 默认全开，写成 claude_code 预设才是全开
    if tools != TOOLS_PRESET and (not isinstance(tools, list) or not all(isinstance(t, str) for t in tools)):
        raise ValueError("请明写 tools（不用工具就写 []，全套工具写 {\"type\": \"preset\", \"preset\": \"claude_code\"}）："
                         "不写时 Claude Code 默认的工具全开")
    for k in ("allowed_tools", "disallowed_tools", "add_dirs"):
        if not isinstance(o.get(k, []), list) or not all(isinstance(x, str) for x in o.get(k, [])):
            raise ValueError(f"{k} 必须是文字列表")
    if isinstance(tools, list) and not set(o.get("allowed_tools", [])) <= set(tools):
        raise ValueError("allowed_tools 只能写 tools 里有的工具名")
    if not isinstance(o.get("model"), str) or not o["model"]:
        raise ValueError("请明写 model：查看器按模型挑号、计价，不按 CLI 的默认模型猜")
    if o.get("effort") not in (None, "low", "medium", "high", "xhigh", "max"):
        raise ValueError("不支持这个思考强度")
    th = o.get("thinking")
    if th is not None and not (isinstance(th, dict) and th.get("type") in ("adaptive", "enabled", "disabled")):
        raise ValueError("thinking 只支持 adaptive、enabled、disabled")
    if o.get("permission_mode") not in (None, *SDK_MODES):
        raise ValueError("permission_mode 只支持 default、dontAsk、acceptEdits、bypassPermissions")
    if not isinstance(o.get("cwd"), str) or not os.path.isabs(o["cwd"]):
        raise ValueError("缺少绝对路径的 cwd（客户端不写时用自己的当前目录）")
    for d in o.get("add_dirs", []):
        if not os.path.isabs(d):
            raise ValueError("add_dirs 只收绝对路径")
    if o.get("max_turns") is not None and (type(o["max_turns"]) is not int or o["max_turns"] < 1):
        raise ValueError("max_turns 必须是正整数")
    if o.get("max_budget_usd") is not None and not isinstance(o["max_budget_usd"], (int, float)):
        raise ValueError("max_budget_usd 必须是数")
    for k in ("resume", "session_id"):
        if o.get(k) is not None and not (isinstance(o[k], str) and UUID.fullmatch(o[k])):
            raise ValueError(f"{k} 必须是会话编号（UUID）")
    if o.get("fork_session") and not o.get("resume"):
        raise ValueError("fork_session 要和 resume 一起用")
    if o.get("session_id") and o.get("resume") and not o.get("fork_session"):
        raise ValueError("session_id 和 resume 一起用时要 fork_session=True（同官方）")
    if (o.get("resume_session_at") or o.get("resume_drops_turn")) and not o.get("resume"):
        raise ValueError("resume_session_at、resume_drops_turn 要和 resume 一起用")
    # 提示：一句话，或者一条用户消息（带图片时官方也是这样发的）
    if isinstance(prompt, str):
        if not prompt.strip():
            raise ValueError("提示词不能为空")
    elif isinstance(prompt, list) and len(prompt) == 1 and isinstance(prompt[0], dict):
        m = prompt[0]
        content = (m.get("message") or {}).get("content")
        if m.get("type") != "user" or (m.get("message") or {}).get("role") != "user" or not content:
            raise ValueError("消息流里只收用户消息")
        if isinstance(content, list) and not all(isinstance(b, dict) and b.get("type") in ("text", "image") for b in content):
            raise ValueError("用户消息的内容只支持文字和图片块")
    else:
        raise ValueError("prompt 要么是文字，要么是只含一条用户消息的消息流（多条暂不支持）")
    if len(json.dumps(prompt, ensure_ascii=False).encode()) > 32 << 20:
        raise ValueError("提示词连图片超过 32 MB；不会自动截断或压缩")
    return {"options": o, "prompt": prompt}


def validate(raw):
    if not isinstance(raw, dict):
        raise ValueError("任务必须是 JSON 对象")
    known = {"key", "prompt", "system", "account", "model", "effort", "backend", "label", "cwd",
             "tools", "read_files", "write_files", "timeout", "sdk", "group", "parallel", "audit"}
    # audit 写 false 是不做上下文核验（ccagent 派的子代理用）：核验是给实验和自动审稿保证上下文干净的，
    # 当子代理用时读到工作区的 CLAUDE.md、用 Bash 都是正常的，核验只会把好好的结果判成失败
    if "audit" in raw and not isinstance(raw["audit"], bool):
        raise ValueError("audit 只能写 true 或 false")
    if set(raw) - known:
        raise ValueError("不认识的任务字段：" + "、".join(sorted(set(raw) - known)))
    if "sdk" in raw:
        if legacy := set(raw) & {"prompt", "system", "model", "effort", "backend", "cwd", "tools", "read_files", "write_files"}:
            raise ValueError("包装 SDK 的请求不收这些字段，请写在 options 里：" + "、".join(sorted(legacy)))
        sdk = check_sdk(raw["sdk"])
        o = sdk["options"]
        sp = o.get("system_prompt")
        r = {"account": "auto", "label": "SDK 调用", "timeout": 2400, "group": "", "parallel": 1, **raw, "sdk": sdk,
             "backend": "viewer", "model": o["model"], "effort": o.get("effort") or "",
             "prompt": json.dumps(sdk["prompt"], ensure_ascii=False) if not isinstance(sdk["prompt"], str) else sdk["prompt"],
             "system": sp if isinstance(sp, str) else (sp or {}).get("prompt") or (sp or {}).get("append") or "",
             "tools": list(o["tools"]) if isinstance(o["tools"], list) else [], "read_files": [], "write_files": []}
    else:
        r = {"system": "", "account": "auto", "model": "claude-opus-5-5", "effort": "high",
             "backend": "viewer", "label": "工作流任务", "tools": [], "read_files": [], "write_files": [],
             "timeout": 2400, "group": "", "parallel": 1, **raw}
    for k in ("key", "prompt", "system", "account", "model", "effort", "backend", "label", "group"):
        if not isinstance(r.get(k), str):
            raise ValueError(f"{k} 必须是文字")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", r["key"]):
        raise ValueError("key 必须是 1～120 个字母、数字、点、横线或下划线")
    if len(r["group"]) > 120:
        raise ValueError("缓存分组名太长")
    if type(r["parallel"]) is not int or not 1 <= r["parallel"] <= MAX_PARALLEL:
        raise ValueError(f"parallel（同一个号上最多并行几份）要在 1～{MAX_PARALLEL} 之间")
    if type(r["timeout"]) is not int or not 10 <= r["timeout"] <= 7200:
        raise ValueError("timeout 要在 10～7200 秒之间")
    if "sdk" in r:
        return r
    if not r["prompt"].strip() or len(r["prompt"].encode()) > 8 << 20:
        raise ValueError("提示词不能为空，也不能超过 8 MB；不会自动截断")
    if len(r["system"].encode()) > 1 << 20 or len(r["label"]) > 120:
        raise ValueError("系统提示或任务名称太长")
    if r["effort"] not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("不支持这个思考强度")
    if r["backend"] != "viewer":
        raise ValueError("这种执行后端尚未接入，未改用其他后端")
    for k in ("tools", "read_files", "write_files"):
        if not isinstance(r[k], list) or not all(isinstance(x, str) for x in r[k]):
            raise ValueError(f"{k} 必须是文字列表")
    if not set(r["tools"]) <= TOOLS:
        raise ValueError("内测只开放 Read、Edit、Write，不开放命令行或代理工具")
    if any(r[k] for k in ("tools", "read_files", "write_files")):
        if not isinstance(r.get("cwd"), str) or not os.path.isabs(r["cwd"]):
            raise ValueError("文件任务必须指定绝对工作目录 cwd")
        for name in r["read_files"] + r["write_files"]:
            if not os.path.isabs(name) or len(name) > 4096:
                raise ValueError("文件清单只能列绝对文件路径，不支持目录和通配符")
        if set(r["tools"]) & {"Edit", "Write"} and not r["write_files"]:
            raise ValueError("有写工具就必须指定可写文件")
    elif r.get("cwd"):
        raise ValueError("无工具任务使用服务分配的空目录，不接受 cwd")
    return r


def file_policy(req):
    """登记文件和工作区的真实路径。旧请求只收现有普通文件；包装 SDK 的请求和官方一样，工具能碰 cwd 和 add_dirs 底下的文件。
    逐次调用时再检查路径是否被换成软链接、是否出了范围。"""
    if "sdk" in req:
        o = req["sdk"]["options"]
        roots = []
        for d in [o["cwd"], *o.get("add_dirs", [])]:
            real = hostfs.Path(d).resolve(strict=True)
            if not real.is_dir():
                raise ValueError("目录不存在：" + d)
            roots.append(str(real))
        # tools 为 None 是 claude_code 预设（全套）；bypass 和 Claude Code 的 bypass 一样不再逐次拦，由调用方负责
        return {"tools": list(o["tools"]) if isinstance(o["tools"], list) else None, "system": req["system"],
                "roots": roots, "sdk": o, "bypass": o.get("permission_mode") == "bypassPermissions"}
    if not req["tools"]:
        return {"tools": [], "system": req["system"], "files": {}, "write": []}
    root = hostfs.Path(req["cwd"]).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("工作目录不存在")
    files = {}
    for raw in set(req["read_files"] + req["write_files"]):
        p = hostfs.Path(raw)
        real = p.resolve(strict=True)
        if not real.is_relative_to(root) or not real.is_file():
            raise ValueError("文件必须是工作区内已有的普通文件：" + raw)
        if p != real:
            raise ValueError("文件路径不能经过软链接：" + raw)
        files[raw] = str(real)
    return {"tools": req["tools"], "system": req["system"], "files": files,
            "write": req["write_files"]}


def tool_denial(profile, inp):
    name, args = inp.get("tool_name"), inp.get("tool_input") or {}
    if profile.get("bypass"):
        return ""
    if profile["tools"] is not None and name not in profile["tools"]:
        return "这个任务没有开放该工具"
    raw = args.get("file_path")
    if "roots" in profile:
        if raw is None and name not in TOOLS:   # 不碰具体文件的工具（Bash、Grep 之类）不按目录拦，放不放行照权限模式
            return ""
        if not isinstance(raw, str) or not os.path.isabs(raw):
            return "只收绝对路径"
        try:
            real = hostfs.Path(raw).resolve(strict=False)
        except OSError:
            return "无法核实文件路径，停止这次访问"
        if not any(real.is_relative_to(r) for r in profile["roots"]):
            return "文件不在 cwd 和 add_dirs 底下"
        return ""
    if raw not in profile["files"]:
        return "该文件不在本次任务的文件清单里"
    if name in {"Edit", "Write"} and raw not in profile["write"]:
        return "该文件只读，不允许修改"
    try:
        if str(hostfs.Path(raw).resolve(strict=True)) != profile["files"][raw]:
            return "文件路径已变化，停止这次访问"
    except OSError:
        return "无法核实文件路径，停止这次访问"
    return ""


def expected_context(request):
    """核验时系统提示应该是什么样（None 是不核文字：调用方用了 Claude Code 预设），以及允许出现哪些工具（None 是不核：全套工具）。"""
    tools = set(request["tools"]) | {"DeferredToolPlaceholder"}
    if "sdk" not in request:
        return [IDENTITY] + ([request["system"]] if request["system"] else []), tools
    o = request["sdk"]["options"]
    sp = o.get("system_prompt")
    if not isinstance(o["tools"], list):
        tools = None
    elif (o.get("env") or {}).get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL") != "1" and o["tools"]:
        tools.add("advisor")   # 带工具时 CLI 自己加的服务端工具；调用方没关它，官方 SDK 下也会有，照实放行
    if isinstance(sp, dict) and sp.get("type") == "preset":
        return None, tools
    text = sp if isinstance(sp, str) else (sp or {}).get("prompt") or ""
    return [IDENTITY] + ([text] if text else []), tools


def audit_context(path, request):
    """核对这次运行实际记录的请求，缺记录不能当成已验证。只查结构，不回传提示词内容。"""
    problems, shapes = set(), 0
    if not path:
        return {"verified": False, "problems": ["找不到请求记录"]}
    expected, tools = expected_context(request)
    with path.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") == "attachment":
                a = d.get("attachment") or {}
                if a.get("type") not in AUDIT_ATTACHMENTS:
                    problems.add("发现未请求的附加上下文：" + str(a.get("type")))
            if d.get("type") == "api-request-shape":
                shapes += 1
                shape = d["shape"]
                texts = [b.get("text", "") for b in shape.get("system", []) if b.get("text")]
                if expected is not None and texts != expected:
                    problems.add("系统提示与指定内容不一致")
                actual = {t.get("name") for t in shape.get("tools", [])}
                if tools is not None and not actual <= tools:
                    problems.add("请求带了额外工具")
    if not shapes:
        problems.add("记录没有请求快照，尚未核验")
    out = {"verified": bool(shapes) and not problems, "problems": sorted(problems)}
    if expected is None:
        out["note"] = "调用方用了 Claude Code 预设系统提示，没有逐字核对系统提示"
    return out


def encode(o):
    """SDK 的消息对象（dataclass）转成能经套接字传的 JSON：带上类名，客户端按同名的官方类型还原。"""
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {"__t": type(o).__name__, **{f.name: encode(getattr(o, f.name)) for f in dataclasses.fields(o)}}
    if isinstance(o, dict):
        return {str(k): encode(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [encode(v) for v in o]
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    return str(o)


def serves(host, account, model):
    """这个号接不接指定的模型。和网页上一样号只能用自家的（server.run_model），这里接不了就拒绝、不替换：
    API 号（中转站、DeepSeek、智谱）看它接了哪几个，GPT 号看 GPT 的模型表，原生号只认 Claude 模型。"""
    if account in host.API:
        return model in host.api_models(account)
    if account in host.GPT:
        return model in host.GPT_MODELS
    return host.model_family(model) == "claude"


def make_driver(sid, cwd, account, req, profile):
    import chat
    from claude_agent_sdk import ResultMessage, StreamEvent

    class RunDriver(chat.Driver):
        async def _guard(self, inp, tool_use_id, ctx):
            # 额度钩子始终保留；文件限制独立于提示词，也不能被父会话的跳过权限覆盖。
            out = await super()._guard(inp, tool_use_id, ctx)
            if out.get("continue_") is False:
                return out
            if inp.get("hook_event_name") == "PreToolUse":
                why = await asyncio.to_thread(tool_denial, self.task_profile, inp)
                if why:
                    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                   "permissionDecisionReason": why}}
            return out

        async def _serve(self, client, retry):
            text = await self.inbox.get()
            self._new_turn()
            self.turn = True
            self.lap_mark("query")
            await client.query(text if isinstance(text, str) else chat.one(text))
            self.lap_mark("queried")
            async for msg in client.receive_response():
                # 包装 SDK 要把每条消息原样交回调用方（客户端还原成同名的官方类型）。流式事件不交：
                # 那是 Driver 自己为了跟进度打开的（include_partial_messages），官方默认不给，调用方也不许打开
                if not isinstance(msg, StreamEvent):
                    self.out.append(encode(msg))
                self._handle(client, msg)
                if isinstance(msg, ResultMessage):
                    self.task_result = {"result": msg.result, "usage": msg.usage,
                                        "session_id": msg.session_id, "num_turns": msg.num_turns,
                                        "stop_reason": msg.stop_reason, "is_error": msg.is_error,
                                        "permission_denials": msg.permission_denials,
                                        "total_cost_usd": msg.total_cost_usd}
                    return None
            raise RuntimeError("运行结束前没有收到结果标记")

    if "sdk" in profile:
        o = profile["sdk"]
        d = RunDriver(sid, cwd, bool(o.get("resume")) and not o.get("fork_session"), account,
                      mode=o.get("permission_mode") or "default", model=o["model"], effort=o.get("effort"),
                      task_profile=profile)
    else:
        d = RunDriver(sid, cwd, False, account, mode="dontAsk", model=req["model"], effort=req["effort"],
                      task_profile=profile)
    d.out = []
    return d


def iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


class Service:
    def __init__(self, host, data=DATA, driver_factory=make_driver):
        self.host, self.data, self.factory = host, Path(data), driver_factory
        self.loop = asyncio.get_running_loop()
        self.jobs, self.tasks, self.drivers = {}, {}, {}
        self.closing = False
        self.slots = {}   # 用户 → 他同时执行的名额（MAX_JOBS），按人分开
        self.submit_lock = asyncio.Lock()
        # 每个号上正在跑和已经挑定、等着开跑的份数。挑号和占位在同一把锁里做完，几份同时挑号时不会都挑中同一个空号
        self.busy, self.pending = {}, {}
        self.choose_lock = asyncio.Lock()
        self.slot_free = asyncio.Condition()
        self.groups_file = self.data / "workflow_groups.json"
        try:
            self.groups = json.loads(self.groups_file.read_text())
        except FileNotFoundError:
            self.groups = {}

    def authorize(self, user, parent):
        try:
            allowed = json.loads((self.data / "workflow_access.json").read_text())["users"]
            opened = True
        except FileNotFoundError:
            allowed, opened = [], False
        if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
            raise ValueError("工作流访问名单格式错误")
        # 名单里写 "*" 就是开给这个实例的所有网页账号，仍然只认已注册的账号；
        # 单用户实例没有注册用户表，只有一个人：有名单文件就算开放，不看名单里写了谁
        if users.MULTI:
            ok = bool(user) and user in users.users and ("*" in allowed or user in allowed)
        else:
            ok = opened and user == users.CALL
        if not ok:
            raise PermissionError("工作流接口尚未向此用户开放")
        if users.owner_of(parent) != user or users.is_deleted(parent):
            raise PermissionError("发起会话的归属已经变化或会话已删除")
        if isolation.for_sid(parent):
            raise PermissionError("只支持公共环境，不自动换运行身份")
        if is_run(parent):
            raise PermissionError("工作流任务不能递归创建任务")

    def path(self, jid):
        if not isinstance(jid, str) or not re.fullmatch(r"[0-9a-f]{32}", jid):
            raise ValueError("任务编号不合法")
        return self.data / "workflow_runs" / (jid + ".json")

    async def store(self, rec):
        await asyncio.to_thread(save, self.path(rec["id"]), rec)

    async def recover(self):
        # 中断的任务不自动重发，尤其改稿可能已经写过一部分。
        def load():
            return [json.loads(p.read_text()) for p in (self.data / "workflow_runs").glob("*.json")]
        for rec in await asyncio.to_thread(load):
            if rec["state"] not in TERMINAL:
                rec.update(state="interrupted", error="查看器重启，执行结果未确认；未自动重发", finished=time.time())
                await self.store(rec)
            self.jobs[rec["id"]] = rec

    def lineage(self, r, user):
        """包装 SDK 续接或分叉：只认自己名下、已经结束的工作流会话，号跟着它走（缓存按号存）。返回 (会话编号, 原任务)。"""
        o = r["sdk"]["options"]
        base = o.get("resume")
        if not base:
            return o.get("session_id") or str(uuid.uuid4()), None
        src = max((j for j in self.jobs.values() if j["session_id"] == base and j["owner"] == user),
                  key=lambda j: j["created"], default=None)
        if src is None or not is_run(base) or users.owner_of(base) != user:
            raise PermissionError("只能续接或分叉自己名下的工作流会话")
        if any(j["session_id"] == base and j["state"] not in TERMINAL for j in self.jobs.values()):
            raise ValueError("这个会话还有任务没结束，等它结束再续接或分叉")
        if not src.get("account"):
            raise ValueError("原任务没有开跑过，不知道缓存在哪个号上")
        # 没指定号就跟着原会话的号（缓存按号存）。分叉时明写了别的号才换（原会话的号额度用完时不用干等），
        # 结果里写明换了号、缓存要整段重写；续接是往原会话里接着写，不换号
        if r["account"] not in ("auto", src["account"]) and not o.get("fork_session"):
            raise ValueError(f"原会话在 {src['account']} 上，续接要用同一个号（缓存按号存），没有换号；要换号请改用分叉")
        sid = (o.get("session_id") or str(uuid.uuid4())) if o.get("fork_session") else base
        if o.get("fork_session"):
            o["session_id"] = sid   # 分叉出来的会话编号由查看器先定，好在开跑前就登记到提交者名下
        return sid, src

    async def command(self, req, user, parent):
        await asyncio.to_thread(self.authorize, user, parent)
        if self.closing:
            raise RuntimeError("查看器正在关闭，未接收新请求")
        if not isinstance(req, dict):
            raise ValueError("请求必须是 JSON 对象")
        op = req.get("op")
        if op == "submit":
            r = validate(req.get("task"))
            # 身份来自连接，key 只负责幂等；相同 key 改材料不准悄悄启动新调用。
            jid = hashlib.sha256((user + "\0" + r["key"]).encode()).hexdigest()[:32]
            fingerprint = hashlib.sha256(json.dumps(r, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            async with self.submit_lock:
                if old := self.jobs.get(jid):
                    if old["fingerprint"] != fingerprint:
                        raise ValueError("这个 key 已提交过其他内容，请明确使用新 key")
                    return self.view(old)
                if sum(x["state"] not in TERMINAL and x["owner"] == user for x in self.jobs.values()) >= MAX_JOBS:
                    raise RuntimeError(f"这个用户还没结束的任务已经有 {MAX_JOBS} 份，请先等现有任务结束")
                sid, src = self.lineage(r, user) if "sdk" in r else (str(uuid.uuid4()), None)
                rec = {"id": jid, "owner": user, "parent": parent, "session_id": sid,
                       "fingerprint": fingerprint, "request": r, "state": "queued", "created": time.time()}
                if src:
                    rec["from"] = {"id": src["id"], "session_id": src["session_id"], "account": src["account"],
                                   "fork": bool(r["sdk"]["options"].get("fork_session"))}
                    if r["account"] not in ("auto", src["account"]):
                        rec["note"] = f"换号分叉：原会话在 {src['account']} 上，这次用 {r['account']}，缓存要整段重写"
                await self.store(rec)
                self.jobs[jid] = rec
                self.tasks[jid] = asyncio.create_task(self.run(rec))
            return self.view(rec)
        if op == "list":
            return {"runs": [self.view(r) for r in sorted(self.jobs.values(), key=lambda x: x["created"], reverse=True)
                             if r["owner"] == user][:100]}
        if op == "pool":
            return {"accounts": await asyncio.to_thread(self.pool, user)}
        if op == "models":
            return await asyncio.to_thread(self.models, user)
        rec = self.jobs.get(req.get("id"))
        if not rec or rec["owner"] != user:
            raise PermissionError("没有这个用户能访问的任务")
        if op == "status":
            return self.view(rec)
        if op == "messages":
            since = req.get("since", 0)
            if type(since) is not int or since < 0:
                raise ValueError("since 必须是非负整数")
            d = self.drivers.get(rec["id"])
            msgs = d.out if d is not None else rec.get("messages") or []
            part = msgs[since:since + 200]
            return {**self.view(rec), "messages": part, "next": since + len(part)}
        if op == "cancel":
            if rec["state"] not in TERMINAL:
                self.tasks[rec["id"]].cancel()
                await asyncio.gather(self.tasks[rec["id"]], return_exceptions=True)
                if rec["state"] not in TERMINAL:  # 排队时尚未进 run 的 try/finally
                    rec.update(state="cancelled", error="用户取消", finished=time.time())
                    await self.store(rec)
            return self.view(rec)
        raise ValueError("不支持这个操作")

    def view(self, rec):
        out = {k: v for k, v in rec.items() if k not in {"request", "fingerprint", "messages"}}
        out.update(label=rec["request"]["label"], backend=rec["request"]["backend"],
                   model=rec["request"]["model"], effort=rec["request"]["effort"])
        if d := self.drivers.get(rec["id"]):
            out["phase"] = d.status()
        return out

    def load(self, account):
        return self.busy.get(account, 0) + self.pending.get(account, 0)

    def choose(self, rec):
        host, r = self.host, rec["request"]
        me = users.Who(rec["owner"], rec["owner"] in users.ADMINS)
        group = self.groups.get(rec["owner"], {}).get(r["group"]) if r["group"] else None
        if rec.get("from"):
            candidates = [r["account"] if r["account"] != "auto" else rec["from"]["account"]]
        elif r["account"] != "auto":
            if group and group != r["account"]:
                raise ValueError(f"缓存分组「{r['group']}」已经固定在 {group} 上，和指定的 {r['account']} 不一致，没有换号")
            candidates = [r["account"]]
        elif group:
            # 同一分组固定在第一次挑中的号上；那个号用不了就报错，不换号（换号要整段重写缓存）
            candidates = [group]
        else:
            candidates = [a for a, _ in host.web_ranked(me, users.paid_ok(me), "shared", "claude")]
            candidates.sort(key=lambda a: self.load(host.base_account(a)) > 0)
        failures = []
        for account in candidates:
            # 点名时除了原生 Claude 号，API 号（中转站 api2、DeepSeek、智谱）和 GPT 号也放行：自动审稿的审稿位
            # 各用一家的模型，API 号的上游还把思考明文返回、读得到审稿人的思维链。自动挑号照旧只挑原生 Claude 号，
            # 不会被悄悄换成别家的模型或按 token 付费的号；回环入口和远端的号不经这里
            named = r["account"] != "auto" or bool(group) or bool(rec.get("from"))
            named = named and (account in host.API or account in host.GPT)
            if (account not in host.ACCOUNTS or account in host.BANNED or account in host.API_ENTRIES
                    or account in host.REMOTE or account in host.REMOTE_GPT
                    or not named and (host.account_family(account) != "claude" or account in host.API)):
                failures.append(f"{account} 不在开放的原生 Claude 账号或点名的 API、GPT 号中")
                continue
            if not serves(host, account, r["model"]):
                raise ValueError(f"{account} 没有接入 {r['model']} 这个模型，未替换模型")
            try:
                host.check_web_account(account)
                host.check_paid(me, None, account)
                why = host.rates.unpriced(host.rates.billing_model(r["model"], account)) or host.limit_block(
                    None, me.name, account, r["model"], len((r["prompt"] + r["system"]).encode()))
                if why:
                    raise ValueError(why)
                return account
            except (HTTPException, ValueError) as e:
                failures.append(str(getattr(e, "detail", e)))
        raise RuntimeError("没有满足权限和额度条件的账号：" + "；".join(failures or ["可用列表为空"]))

    def remember_group(self, rec, account):
        r = rec["request"]
        if not r["group"] or self.groups.get(rec["owner"], {}).get(r["group"]) == account:
            return
        self.groups.setdefault(rec["owner"], {})[r["group"]] = account
        save(self.groups_file, self.groups)

    def claim(self, rec):
        me = users.Who(rec["owner"], rec["owner"] in users.ADMINS)
        users.claim(rec["session_id"], me, [])
        with users._lock:
            # 单用户实例的 users.claim 不记主人，这里补一条，工作流标记才有地方挂（认出工作流会话、列表跳过它都靠这个标记）
            own = users.owners.setdefault(rec["session_id"], {"owner": rec["owner"], "visible": [], "ts": time.time()})
            own["workflow"] = {"id": rec["id"], "parent": rec["parent"]}
            users._save(users.OWNERS_FILE, users.owners)

    def pool(self, user):
        """号池此刻的样子，给 agent 跑之前挑号用（ccrun pool）：和网页上这个人看到的血条同一套数，别人的预定已经扣掉。
        都是各自窗口的百分点；读数比上游慢，附上读数时刻。不给挑号依据这类内部说明。"""
        host, now = self.host, time.time()
        me = users.Who(user, user in users.ADMINS)
        snaps = host.quota(limit=0)["accounts"]
        seen = json.loads(json.dumps(snaps))
        reserve.decorate(seen, me.name, now)
        out = []
        for a, _ in host.web_ranked(me, users.paid_ok(me), "shared", "claude"):
            b = host.base_account(a)
            s = seen.get(b) or {}
            fh, wk = s.get("five_hour") or {}, s.get("seven_day") or {}
            F, W = reserve.rest(fh or None, now), reserve.rest(wk or None, now)
            week_only = b in host.WEEK_ONLY
            cap = W if week_only else (min(F, W / host.five_in_week(b)) if F is not None and W is not None else F)
            red = 0.0
            if F is not None and not week_only and (fh.get("resets_at") or 0) > now:
                red = max(0.0, min(fh.get("recent") or 0.0, 100 - F))
            try:
                host.check_paid(me, None, a)
                why = ""
            except (HTTPException, ValueError) as e:
                why = str(getattr(e, "detail", e))
            group = sorted(g for g, acc in self.groups.get(user, {}).items() if acc == a)
            out.append({
                "account": a,
                "five_hour_left": None if week_only or F is None else round(F, 1),
                "five_hour_usable": None if cap is None else round(cap, 1),
                "recent_30min_used": round(red, 1),
                "predicted_left": None if cap is None else round(cap - red / 2, 1),
                "five_hour_resets": iso(fh.get("resets_at")),
                "week_left": None if W is None else round(W, 1),
                "week_resets": iso(wk.get("resets_at")),
                "held_by_others": {"five_hour": round(fh.get("hold") or 0, 1), "week": round(wk.get("hold") or 0, 1)},
                "my_reservation": bool(s.get("mine")),
                "usable": not why, "why_not": why,
                "workflows": self.load(b), "groups": group,
                "read_at": iso(max(fh.get("ts") or 0, wk.get("ts") or 0)),
            })
        return out

    def models(self, user):
        """ccagent models：这个人能派哪些号、哪些模型。Claude 号就是号池（不写号时自动从里面挑）；
        点名才用的 API 号、GPT 号各列出它接了哪几个模型，付不付得起照网页上的规矩（check_paid）"""
        host = self.host
        me = users.Who(user, user in users.ADMINS)
        claude = [{"account": p["account"], "usable": p["usable"], "why_not": p["why_not"],
                   "predicted_left": p["predicted_left"]} for p in self.pool(user)]
        named = []
        for a in [*host.API, *host.GPT]:
            if (a not in host.ACCOUNTS or a in host.BANNED or a in host.API_ENTRIES
                    or a in host.REMOTE or a in host.REMOTE_GPT):
                continue
            try:
                host.check_web_account(a)
                host.check_paid(me, None, a)
                why = ""
            except (HTTPException, ValueError) as e:
                why = str(getattr(e, "detail", e))
            named.append({"account": a, "models": list(host.api_models(a)) if a in host.API else list(host.GPT_MODELS),
                          "usable": not why, "why_not": why})
        # Claude 号能选的模型就是单价表里有价的 Claude 模型（没价的会被 unpriced 拦下）；带 @ 的是空闲时段那一条，不是能选的模型
        table = host.rates.load()
        claude_models = sorted(k for k in table if host.rates.model_family(k) == "claude" and "@" not in k)
        return {"claude": claude, "claude_models": claude_models, "named": named}

    async def take(self, rec):
        """挑号并占一个位置：挑号和占位在同一把锁里，几份同时挑号时按占位数错开。返回号。"""
        async with self.choose_lock:
            account = await asyncio.to_thread(self.choose, rec)
            base = self.host.base_account(account)
            self.pending[base] = self.pending.get(base, 0) + 1
        limit = rec["request"]["parallel"]
        try:
            async with self.slot_free:
                await self.slot_free.wait_for(lambda: self.busy.get(base, 0) < limit)
                self.busy[base] = self.busy.get(base, 0) + 1
        finally:
            self.pending[base] -= 1
        return account

    async def give_back(self, account):
        base = self.host.base_account(account)
        async with self.slot_free:
            self.busy[base] -= 1
            self.slot_free.notify_all()

    async def run(self, rec):
        jid, r = rec["id"], rec["request"]
        d = None
        account = None
        try:
            async with self.slots.setdefault(rec["owner"], asyncio.Semaphore(MAX_JOBS)):
                await asyncio.to_thread(self.authorize, rec["owner"], rec["parent"])
                rec.update(state="checking")
                await self.store(rec)
                account = await self.take(rec)
                me = users.Who(rec["owner"], rec["owner"] in users.ADMINS)
                await asyncio.to_thread(self.host.check_paid, me, None, account)
                why = await asyncio.to_thread(self.host.limit_block, None, me.name, account, r["model"],
                                              len((r["prompt"] + r["system"]).encode()))
                if why:
                    raise RuntimeError(why)
                await asyncio.to_thread(self.remember_group, rec, account)
                profile = await asyncio.to_thread(file_policy, r)
                if "sdk" in r:
                    cwd = hostfs.Path(r["sdk"]["options"]["cwd"])
                else:
                    cwd = hostfs.Path(r["cwd"]) if r["tools"] else hostfs.HOME / ".cache" / "ccrun"
                    await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(self.claim, rec)
                rec.update(account=account, state="running", started=time.time())
                await self.store(rec)
                d = self.drivers[jid] = self.factory(rec["session_id"], str(cwd), account, r, profile)
                prompt = r["sdk"]["prompt"] if "sdk" in r else r["prompt"]
                if isinstance(prompt, str):
                    d.send(prompt)
                else:
                    d.inbox.put_nowait(prompt[0])   # 一条用户消息（带图片），官方 SDK 也是这样按消息流发的
                try:
                    await asyncio.wait_for(asyncio.shield(d.task), r["timeout"])
                except asyncio.TimeoutError:
                    raise RuntimeError("运行超过设定时间，已停止；未自动重试") from None
                result = getattr(d, "task_result", None)
                if d.blocked or d.error:
                    raise RuntimeError(d.blocked or d.error)
                if result is None:
                    raise RuntimeError("没有取得完整结果，未自动重试")
                rec["result"] = result
                if "sdk" not in r and (result["is_error"] or result.get("stop_reason") in {"refusal", "max_tokens"}
                                       or result.get("permission_denials")):
                    raise RuntimeError("运行没有正常完成，详情见任务结果；未自动重试")
                rec["state"] = "completed"
        except asyncio.CancelledError:
            rec.update(state="interrupted" if self.closing else "cancelled", error="查看器停止" if self.closing else "用户取消")
        except Exception as e:
            rec.update(state="failed", error=str(getattr(e, "detail", e)))
        finally:
            if d:
                if not d.task.done():
                    d.task.cancel()
                await asyncio.gather(d.task, return_exceptions=True)
                # 和网页一样按真实会话记录记调用，不能把 SDK 的美元标价当成实际账单。
                try:
                    path = await asyncio.to_thread(self.host.calls.find, rec["session_id"])
                    if path:
                        await asyncio.to_thread(self.host.api_calls.record, path, d.account, "web")
                    if r.get("audit") is False:
                        rec["context_audit"] = {"verified": None, "skipped": "调用方关掉了上下文核验（audit: false）"}
                    else:
                        rec["context_audit"] = await asyncio.to_thread(audit_context, path, r)
                    if rec["state"] == "completed" and rec["context_audit"]["verified"] is False:
                        rec.update(state="failed", error="上下文核验未通过；结果已保留，但不作为合格输出")
                except Exception as e:
                    rec.update(state="failed", error=f"调用归属收尾失败：{e}")
                rec["messages"] = list(getattr(d, "out", []))
                self.drivers.pop(jid, None)
            if account:
                await self.give_back(account)
            rec["finished"] = time.time()
            await self.store(rec)

    async def stop(self):
        self.closing = True
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


async def start(host):
    global _service
    service = Service(host)
    await service.recover()
    _service = service


async def stop():
    global _service
    if _service:
        await _service.stop()
        _service = None


def dispatch(req, user, parent):
    """人设卡套接字的工作线程调用，不阻塞服务的事件循环。"""
    if _service is None:
        raise HTTPException(503, "工作流接口尚未启动")
    future = asyncio.run_coroutine_threadsafe(_service.command(req, user, parent), _service.loop)
    try:
        return future.result(timeout=15)
    except concurrent.futures.TimeoutError:
        # 提交可能已经落盘；客户端用同一 key 重试取回，不取消到一半导致状态不明。
        raise HTTPException(504, "请求尚未确认，可用相同 key 重试查询，不会重复启动") from None
    except (ValueError, PermissionError, RuntimeError) as e:
        raise HTTPException(403 if isinstance(e, PermissionError) else 400, str(e)) from None
