"""Claude Code 会话查看器后端：后台增量解析被订阅会话的 .jsonl，有新内容就推送给前端。

用法：python3 server.py [--port 8765] [--root ~/.claude/projects] [--mode readonly|full]

默认是只读模式：只读会话记录、渲染出来，不加载 Claude SDK，也不调用它的任何接口
（不能发话、不查上下文和账号信息）。要在网页里和 Claude 对话，用 --mode full 启动，
可以再加 --account 指定默认账号（账号表见 instance.py）、--proxy 指定 server 自己环境里的代理（默认是 instance.json 的 proxy_port，没写就拒绝启动，除非亲手打开了 allow_direct；配了代理而代理连不上就拒绝对话）。
运行时数据写在环境变量 CC_VIEWER_DATA 指定的目录（不设就是代码目录），见 instance.py。
"""
import argparse
import asyncio
import atexit
import hashlib
import itertools
import json
import math
import mimetypes
import re
import secrets
import signal
import sys
import threading
import time
import traceback
import uuid
from collections import Counter, defaultdict
import contextlib
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

# 对话用的 SDK 只在 --mode full 时才加载（见 main），只读模式下连导入都不导入
Driver = None
MODES: tuple[str, ...] = ()
ACCOUNTS: dict[str, str | None] = {}
BANNED: dict[str, str] = {}   # 不能用的号 → 原因（暂时用不了 / 已被封 / 已搬走）
from session import BLOCKED_HEAD, Session, resets_at
import api_calls
import ctxmap   # 上下文里装着什么，拆到每个文件、每个气泡（上下文药丸的环形图）
import spendmap   # 会话花了多少、花在哪（同一张卡片的花费视角）
import calls   # 会话记录里每次调用的 token 数，全查看器只此一处
import diskq   # 数据目录的小文件交给后台写盘线程写，事件循环不等磁盘
# Claude 那边的路径（会话记录、工作目录、各号的配置目录，以及用户和 CLI 交过来的路径）一律用 hostfs.Path，
# 配了 run_as 的实例以那个系统用户的身份读写；pathlib.Path 只给查看器自己的数据目录和代码目录用（见 hostfs.py）
import hostfs
import isolation
import devices   # 「设备」页：每人自己的设备列表、添加电脑和服务器（见 devices.py）
import personas  # 人设卡和注入的说明（见 personas.py）
import naming    # 会话的名字是谁起的、会话里的 AI 给自己的会话起名（见 naming.py）
import todos     # 待办清单，网页上的待办页和会话里的 cctodo 改同一份（见 todos.py）
import voice     # 讯飞语音输入：各人的服务和密钥、给浏览器签连接地址（见 voice.py）
import summaries  # 会话摘要：闲了 45 分钟从主会话分叉出去写（见 summaries.py、下面的 summary_loop）
import review_board  # 自动审稿会话的审稿看板（见 review_board.py）
import workflows  # 本机工作流：任务归属、队列和执行，复用人设卡套接字认人
import sshgw
import workdev   # 会话勾选的工作设备（见 workdev.py）
import inbox   # Claude 那边的状态栏脚本记下的额度和调用，收进自己的数据目录
import rates   # 点数单价
import users   # 用户、会话的主人和可见范围、额度账本
import stats   # 账号页的余额曲线、我的号、用量统计（只读，见 stats.py）
import reserve   # 预定额度、预测拦截要用的算法
import gate   # 网络闸门：预测拦截拦下以后，CLI 一次请求都发不出去
import api_relay   # API 号（DeepSeek）：哪一家、接口在哪
import platform_costs   # 后台取得平台实扣后替换预估，不改原始会话记录
import api_gateway   # 对外API与网页回环，共用选号、额度、预定和账本
from instance import API_ENTRIES, WEB_ENTRIES, REMOTE, REMOTE_GPT, base_account, web_account
import peak   # 按时段计价的模型此刻是高峰还是空闲
from users import Who
from datetime import datetime, timedelta
from instance import CALL, DATA, HOST_NAME, PC_SETUP_URL, XXLB, FIVE_IN_WEEK, PAID, PROXIES, SHARE_URL   # 运行时小文件一律写在 DATA（见 instance.py），代码目录只放代码和前端
from instance import GPT, GPT_MODELS, WEEK_ONLY, five_in_week, is_gpt_model   # GPT 号（经本机翻译层用 ChatGPT 订阅的 Claude Code）
# API 号（DeepSeek，按 token 付费，请求经闸门补上密钥，见 api_relay.py）；模型分几家，号只能用自家的
from instance import API, API_MODELS, PRESENT_AS, RELAY, account_family, api_models, model_family
from instance import NO_RESERVE   # 不开放预定的号：号主打开面板只给折扣（reserve_plan）
from instance import EXPIRES, week_end   # 号的到期时间，额度接口一并给前端（倒计时取重置和到期里早的那个）；挑号按 week_end 排
from instance import owns   # 号主用自己的号不按余额拦，还能关掉别人的预定、撤销自己的单
# 网页对话没选过权限模式时用它，新建的会话和打开的已有会话都一样；各实例自己定（instance.json 的 default_mode，不写是 auto）
from instance import DEFAULT_MODE
# 没选过思考强度、设置里也没写这个模型的强度时带的 --effort（instance.json 的 default_effort，见 launch_effort）
from instance import DEFAULT_EFFORT
# Claude 号没选过模型、设置里也没写默认模型时带的 --model（instance.json 的 default_model，见 launch_model）
from instance import DEFAULT_MODEL
from usage_probe import CWD as USAGE_CWD

HERE = Path(__file__).parent
PROJECTS_DIR = calls.PROJECTS
# 刷新额度在专用空目录里开 CLI 跑 /usage，CLI 2.1.296 起每跑一次都在这里留下一个只有这条命令、没有标题的会话
# （会话列表里出现过一串文件名当标题、最近几分钟才更新的空会话）。这些不是对话，会话列表和找话都跳过它；
# 浏览文件夹是用户自己点进去的，照样列。目录名照 CLI 的编法：工作目录里字母数字以外的字符都换成 -
PROBE_PROJECT = re.sub(r"[^A-Za-z0-9]", "-", str(USAGE_CWD))
ROOT_CANDIDATES = ["~/.claude/projects"]
EVICT_SECS = 600    # 没人订阅、也这么久没人来取，就释放内存
THREADS = 200       # 接口线程池最多几个线程（同步接口、静态文件都在里面跑；默认 40，见 lifespan）
EFFORTS =["low", "medium", "high", "xhigh", "max"]   # 思考强度（CLI 的 --effort）；没选过的会话不传，按 CLI 自己的设置


_env_effort: dict[tuple, tuple] = {}   # (独立环境用户, 号) → (读的时刻, 结果)，见 settings_effort
ENV_EFFORT_SECS = 30


def settings_effort(account: str, env=None) -> dict:
    """这个号的 Claude Code 用户设置（配置目录下的 settings.json）里和思考强度有关的几项：会话没选过强度时 CLI 用的就是设置里的那档，
    界面上照实写出来，不写含糊的「默认」。强度可以按模型分别写（modelSettings[模型].effortLevel），也可以不分模型写（effortLevel），
    所以连默认模型一起交给前端，由它按会话实际用的模型去查（app.js 的 effortOf）。
    独立环境（env）的 CLI 用的是那个环境自己的配置目录（isolation.launch_env 的 CLAUDE_CONFIG_DIR），不读号的：
    以前这里一律读号的，独立环境里的会话设置里什么都没写、CLI 按自己的默认 medium 跑，看板却照号的设置写 high。
    独立环境的要经文件助手以那个用户的身份读，每次推送都要拼快照（Tab.brief），所以记一会儿，改了设置最多晚这么久才显示。
    读不到就是空的（界面上说设置里没写）。项目目录里的 .claude/settings.json 也能改这些，这里不去找。"""
    if env:
        key, now = (env.user, account), time.monotonic()
        if (hit := _env_effort.get(key)) and now - hit[0] < ENV_EFFORT_SECS:
            return hit[1]
        out = _read_effort(account, env.path(env.home + "/.claude/settings.json"))
        _env_effort[key] = (now, out)
        return out
    return _read_effort(account, hostfs.Path(ACCOUNTS.get(account) or "~/.claude").expanduser() / "settings.json")


def _read_effort(account: str, path) -> dict:
    try:
        s = json.loads(path.read_text())
        by_model = {m: v.get("effortLevel") for m, v in (s.get("modelSettings") or {}).items()
                    if isinstance(v, dict) and v.get("effortLevel") in EFFORTS}
        level = s.get("effortLevel")
        # GPT 号的别名里用 ANTHROPIC_MODEL 定了默认模型（GPT_MODELS 的第一个），压过设置里写的 Claude 模型；
        # API 号的默认模型是拉起时 --settings 里给的（api_relay.launch_env，instance.api_models 的第一个）
        model = GPT_MODELS[0] if account in GPT else api_models(account)[0] if account in API else s.get("model")
        return {"model": model, "level": level if level in EFFORTS else None, "byModel": by_model}
    except (OSError, ValueError, AttributeError):
        return {}


def settings_level(s: dict, model: str | None) -> str | None:
    """设置（settings_effort 读出来的）给这个模型写的强度，没写是 None。和前端 effortOf 一个查法：先去掉 [1m] 这类后缀按模型查，
    再看不分模型的。设置里的默认模型常写成别名（opus[1m]），按模型写的键却是全名（claude-opus-5-5），CLI 是认得的
    （查过：共用环境 model 写 opus[1m]、只按全名写了 high，没选过的会话都按 high 跑），所以别名去对全名里的那一段。"""
    m = re.sub(r"\[.*\]$", "", model or "")
    by = s.get("byModel") or {}
    hit = by.get(m) or (re.fullmatch(r"[a-z]+", m) and next((v for k, v in by.items() if f"-{m}-" in f"-{k}-"), None))
    return hit or s.get("level")


def launch_effort(chosen: str | None, account: str, env, model: str | None) -> str | None:
    """拉起 CLI 时带哪档 --effort：用户定过的就是它；没定过时，CLI 读的那份 Claude Code 设置（独立环境是那个环境自己的，见 settings_effort）
    给要跑的模型写了强度就不带、照设置，没写才带实例的默认档（DEFAULT_EFFORT）。默认档不是用户的选择，不存进 session_efforts.json：
    以后设置里写上了、或实例改了默认，没选过的会话下次续接就跟着变。
    以前没定过就一律不带，独立环境里没有设置文件、共用环境的设置只写了 Opus 和 Sonnet，CLI 都按自己的默认 medium 跑。"""
    if chosen or not DEFAULT_EFFORT:
        return chosen
    s = settings_effort(account, env)
    return None if settings_level(s, launch_model(account, env, model) or s.get("model")) else DEFAULT_EFFORT


def launch_model(account: str, env, model: str | None) -> str | None:
    """拉起 CLI 时带哪个 --model：选的模型和号是同一家就是它（run_model）；没选（或选的是另一家的）时，Claude 号读的那份
    Claude Code 设置（独立环境是那个环境自己的，见 settings_effort）写了默认模型就不带、照设置，没写才带实例的默认模型（DEFAULT_MODEL）。
    GPT 号的默认模型由启动脚本的 ANTHROPIC_MODEL 定，不带。默认模型不是用户的选择，不记到标签页上。
    以前没选就一律不带：独立环境里没有设置文件，CLI 按自己的默认跑（当时是 Opus 5.5），新会话页却写「模型未设」。"""
    if (picked := run_model(account, model)) or account in GPT or account in API or not DEFAULT_MODEL:
        return picked
    return None if settings_effort(account, env).get("model") else DEFAULT_MODEL


# GPT 号上用不了的两样，怎么处理：
# - auto 权限模式要 Claude Code 拿一个 Claude 模型审核每个动作，GPT 号没有 Claude 凭据，审核一律失败、动作全被拒。
#   那边就用 dontAsk：会话记着的照旧是选的 auto（换回 Claude 号还是 auto），GPT 号上实际按 dontAsk 跑，界面上写明（runMode）。
# - 模型分两家：Claude 号只能用 Claude 模型，GPT 号只能用 GPT 模型。会话选的模型和号不是一家的，这个号上就按它自己的默认模型跑
#   （不传 --model：Claude 号按设置里的，GPT 号按别名里的），选的照旧记着，换回去还是它
GPT_AUTO_MODE = "dontAsk"
# API 号（DeepSeek）照 GPT 号的做法：auto 要拿 Claude 模型审核，这家的号上同样按 dontAsk 跑；模型分成三家，号只能用自家的


def run_mode(account: str | None, mode: str) -> str:
    """这个号上 CLI 实际按哪个权限模式跑。"""
    return GPT_AUTO_MODE if account_family(account) != "claude" and mode == "auto" else mode


def run_model(account: str | None, model: str | None) -> str | None:
    """这个号上 CLI 实际传哪个 --model：选的模型和号是同一家才传，不是就按这个号的默认（None）。"""
    if account in RELAY:   # 中转站转的是 Claude 模型，看号的配置里接了哪几个，不按模型名分家
        if model and model not in api_models(account):
            raise ValueError(f"{account} 只接入了 {'、'.join(api_models(account))}，请先选择这个模型")
        return model
    return model if model and model_family(model) == account_family(account) else None


def stamp(ts) -> float | None:
    """记录里的时间（ISO 字符串）换成秒数，好和 time.time() 比先后；没有或认不出是 None。"""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None
# 推送心跳的间隔：心跳里带着它，前端两个间隔都没收到任何消息就标黄（查看器卡住了也看得出来），旧后端 15 秒、不带间隔
HEARTBEAT_SECS = 3
SPENT_GAP = 2        # 同一个会话的总消耗两次重算至少隔这么多秒（见 Tab.refresh_spent）
SPENT_RECHECK = 30   # 有人看着的会话，没迹象也每隔这么多秒重算一次（子代理的花费）


class Tab:
    """服务器上一个会话文件只解析一份，所有设备共用；每台设备开了哪些标签由前端自己记在浏览器里。

    一个会话的全部服务器状态都在这里：解析出来的内容、网页驱动它的 SDK 任务、
    网页上给它选的权限模式、模型和账号。标签释放了，这些跟着一起没；
    其中用户选的权限模式和账号另外记在小文件里，重新打开时照原样恢复。
    """

    def __init__(self, path: Path):
        self.id = tab_id(path)
        self.sid = users.sid_of(path)   # 会话编号：主人和可见范围按它记（同一个文件经不同路径打开，编号一样）
        tid_sid[self.id] = self.sid
        self.session = Session(path)
        self.seen = time.monotonic()
        self.pushed = None            # 上次推送时的状态，用来算出这次要推哪一截
        self.pushed_tables: dict | None = None   # 上次推送时附带的那几张表（DELTA_TABLES），算增量用，见 publish
        # 推送编号：publish 每真推一次加一，推送和快照（brief）里都带着（v）。心跳里给最新的编号，前端收到的比它旧就是漏了，
        # 自己补拉（对账）；整段拉取的回复比已经收到的推送旧，前端就不拿它覆盖（见 README「推送和增量」）
        self.push_n = 0
        self.driver: Driver | None = None   # 网页驱动这个会话的 SDK 任务，没人在网页里说话就是 None
        # 网页对话的权限模式：用户选过就用记下的，没选过用默认（终端里的模式在 session.info 里，互不相干）
        self.mode = modes.get(self.id, DEFAULT_MODE)
        self.model: str | None = None   # 网页上给它选的模型，没选过就用会话原来的
        # 这个会话的思考强度（续接时 --effort 带的那档），没定过是 None（按 CLI 自己的设置）。怎么定的见 sync_effort
        self.effort: str | None = (efforts.get(self.id) or {}).get("effort")
        self.effort_seen = (None, 0)   # 记录里的 /effort 看到第几条了（连同记录的代号，整份重读时从头看），见 sync_effort
        # 最近一次从 CLI 拿到的完整上下文明细：CLI 收掉以后，上下文环形图的系统部分还按它拆（会注明是上次连着时的）
        self.cli_usage: dict | None = None
        # 网页对话用哪个号；没选过就按全局设置的默认号（第一次发话时再定一次并记下，见 say）
        # （auto 一个能用的都挑不出来时先占着启动时的号，发话时 prepare_speak 会重新挑，挑不出来就拦下）
        self.account = accounts.get(self.id) or default_account(users.NOBODY, users.paid_ok(users.NOBODY, self.sid))[0] \
            or config.account
        # 发话前查看器自己在做的那一步（发话接口只排队就回，额度核对挪到后台，见 speak）：{kind: checking, step, t0}，没有是 None。
        # step 是此刻在做哪件事：quota 预测额度、必要时换号，handing 记下记账起点、交给 CLI
        self.prep: dict | None = None
        # 最近一句在后台被退回的话：{nonce 前端发话时带的编号, why 原因, n 第几次}，前端据此把这句话退回、给重试
        self.rejected: dict | None = None
        # 最近一次自动切号（hand_over）：{from 原来的号, to 换成的号, n 第几次, t 什么时候}，前端据此提示一行
        self.switched: dict | None = None
        # 上一轮发话时记录里有几个压缩分界（Session.compacts），没发过话是 None；变多了就是压缩后的第一轮，重新挑号（compact_pick）
        self.compacts_seen: int | None = None
        self.speak_lock = asyncio.Lock()   # 一句一句交给 CLI：前一句还在核对额度时，后一句要排在它后面
        # 发话前的检查（prepare_speak）要算余额，放在线程里跑：连着发的两句各自等线程，后一句可能先核完、先排进去，
        # 所以核对和排进 speak_lock 这两步在这把锁里一起做，先到的先排
        self.say_lock = asyncio.Lock()
        # 总消耗（spend_fields）的缓存，见 refresh_spent：(依据, 算的时刻, 结果)；spent_ver 是结果变过几次，进 state 好在变了时推一次
        self.spent_cache: tuple | None = None
        self.spent_ver = 0

    def cwd(self):
        """这个会话的工作目录，看板上显示它比显示 .jsonl 路径有用。推送摘要每圈都要，找到过就不再 stat（见 _cwd_found）。"""
        if hit := _cwd_found.get(self.session.path):
            return hit
        try:
            return read_cwd(self.session.path, self.session.path.stat().st_mtime)
        except OSError:
            return ""

    def chat_state(self):
        """网页对话那一侧的状态，brief 和 state 都从这里取，不用各写一遍。"""
        d, p = self.driver, self.prep
        return {"running": bool(d and d.running) or bool(p), "chatError": d.error if d else "",
                # 一轮的结果消息收到过几条：打断后 CLI 马上开新的一轮（排着的插话），前端靠它变了撤掉「已请求打断」
                "results": d.results if d else 0,
                # 这一轮走到哪一步（查看器在核对额度 / 启动 Claude Code / 等上游 / 思考 / 执行工具……）
                "phase": {"kind": p["kind"], "step": p.get("step"), "age": round(time.monotonic() - p["t0"], 1)} if p
                else d.status() if d else None,
                "rejected": self.rejected,   # 后台退回的那句话（见 speak）
                "switched": self.switched,   # 最近一次自动切号（见 hand_over）
                # 最大的一次请求多大、上游单条上限多大（chat.Driver.req_size）：到了上限的一定比例，上下文用量旁边提醒
                "reqSize": d.req_size() if d else None,
                "bg": d.background() if d else [],     # 这一轮结束后还挂着的后台任务（结束时 Claude 会自己接着说）
                # Claude 约好什么时候自己接着干（ScheduleWakeup、定时任务，time.time）：在那之前 CLI 不会被空闲回收
                "wake": d.next_wake() if d and d.alive and not d.turn else None,
                "autoGo": auto_go_state(self),         # 额度恢复后自动继续：开没开、是不是因为额度停着、打算几点发
                "webMode": self.mode, "webModel": d.model if d else None, "account": self.account,
                # 这个号上实际按哪个权限模式跑（GPT 号上选的 auto 按 dontAsk 跑，见 run_mode），和 webMode 一样就是没换
                "runMode": run_mode(self.account, self.mode),
                # 定下的强度还没有请求用上过：CLI 还没按它跑（Claude 正在回复时换的，/effort 排到这一轮结束才执行；
                # 没连着的，要等下次发话才起），或者最近一条回复记的实际强度还是别的档。面板上灰着，说明什么时候生效
                "effort": self.effort,
                "effortPending": bool((rec := efforts.get(self.id)) and (
                    rec["on"] is None or ((used := self.session.info.get("usedEffort")) and used != rec["effort"])))}

    def brief(self):
        """发给前端的一份完整快照（推送增量时也带上这些字段）。"""
        s, env = self.session, isolation.for_sid(self.sid)
        accs = self.call_accounts()   # 底层号：below 的 covered/free 和 prediction 都用它
        out = {"id": self.id, "path": str(s.path), "cwd": self.cwd(), "gen": s.gen, "total": len(s.items), "v": self.push_n,
               **s.info, **self.chat_state(), "modes": MODES, "ctx": self.ctx(), "tags": tags.get(self.id, []),
               "staged": staged.get(self.id, []),
               "environment": "private" if env else "shared",
               # 独立环境里 CLI 没选过强度时按那个环境自己的设置跑，不是号的（见 settings_effort）；共用环境的前端照旧用 /api/config 里号的
               "settingsEffort": settings_effort(self.account, env) if env else None,
               # 建会话时选的人设卡（personas.py 的快照），看板上写着；没选是 None
               "persona": (card := personas.of_session(self.sid)) and card["name"],
               # 跑过自动审稿 review.py 的工作区（session.py 认出来的，最近的在最后）。总是带上这个键：前端靠它知道后端有审稿看板
               "reviewWs": s.info.get("reviewWs") or [],
               # 这个会话取消勾选的工作设备（workdev.py），其余登记了的都能用。没开门卫就没有这个键，前端不显示
               **({"workOff": workdev.off(self.sid)} if devices.ENABLED else {}),
               **self.spent(),                          # 总消耗和其中进账本的，前端换算成点数、分两种颜色写
               # 每条回复是哪个号发的：气泡详情写号名，限量号的点数换成限量色。
               # 回环按网页上看到的入口名给（选号那边也用入口名），不然前端会一直以为换了号、提示缓存不通用；
               # 上面那个 accs 仍是底层号，covered/free 和预测都用它。
               "calls": {m: web_account(a) for m, a in accs.items()},
               "callPrices": platform_costs.call_prices(accs),
               # 由预定付了的回复各有几成（reserve.frac）：气泡上那一份花费写成蓝的
               "covered": {m: f for m in accs if (f := reserve.frac(m)) > 0},
               # 特惠结算时记下的免了几成（reserve.recorded_free）：绿条不够时免得比折扣少，前端照它拆出免掉的那份
               "free": {m: x for m in accs if (x := reserve.recorded_free(m)) is not None},
               **users.share(self.sid),                  # 主人和可见范围
               "think": think.get(s.path.stem, {})}      # 各次请求的思考跳数（只有网页对话记得到）
        if titles.get(self.id):   # 主人自己起的名字压过记录里的标题
            out["title"] = titles[self.id]
        return out

    def spent_key(self):
        """总消耗可能变了的迹象，都不碰文件系统：记录换代或长了、记下了新调用、又有调用分给了预定。"""
        return (self.session.gen, len(self.session.items), users.call_count(self.sid), reserve.settled_n[self.sid], platform_costs.version())

    def spent_due(self) -> bool:
        """总消耗该不该重算了。算一次要把会话名下每次调用逐条分账、查预定付了几成，长会话（两千多次调用）要一百多毫秒
        （实测：原来每推一次都在事件循环里整份重算，大会话忙的时候光这一项就占了事件循环一成左右，推送也跟着慢）。
        所以有迹象（spent_key 变了）才算，同一个会话两次之间至少隔 SPENT_GAP 秒，攒着的变化由后台循环到点补算、再推一次，不会停在旧数上；
        子代理写自己的文件时主会话可以很久不动、迹象里看不出来，所以有人看着的会话每 SPENT_RECHECK 秒无条件再算一次。"""
        if not self.spent_cache:
            return True
        old_key, at, _ = self.spent_cache
        return time.monotonic() - at >= (SPENT_GAP if self.spent_key() != old_key else SPENT_RECHECK)

    def refresh_spent(self):
        """重算总消耗（后台循环放在线程里调），结果变了就让 spent_ver 加一：它在 state 里，后台循环会因此再推一次。
        先取迹象再算：算到一半又有了新调用，存下的迹象就是旧的，下一圈自然再算。"""
        key, now = self.spent_key(), time.monotonic()
        out = spend_fields(self.session.path)
        if not self.spent_cache or out != self.spent_cache[2]:
            self.spent_ver += 1
        self.spent_cache = (key, now, out)

    def spent(self) -> dict:
        """推送和接口里带的总消耗：用缓存（见 spent_due），还没算过就当场算。"""
        if not self.spent_cache:
            self.refresh_spent()
        return self.spent_cache[2]

    def call_accounts(self) -> dict[str, str]:
        """回复编号 → 哪个号发的，以 api_calls.jsonl 的记录为准。网页对话的调用一发出请求就记，写进去、读回来之前的那一瞬，
        就看驱动这个会话的 CLI 自己发出过哪些回复编号：CLI 是用驱动的号启动的，所以就是这个号发的。
        同一个会话经不同路径打开（各号的 projects 是软链接、家目录也是）是不同的标签页，驱动只挂在发话的那个上，
        所以按会话编号把所有标签页的驱动都算上，不然别处打开来看的人看不到正在进行的这一轮是哪个号。"""
        out = {}
        for t in list(tabs.values()):
            if t.sid == self.sid and (d := t.driver):
                out.update(dict.fromkeys(d.mids, d.account))
        out.update(users.call_accounts(self.sid))
        return out

    def ctx(self):
        """上下文用量：CLI 连着就用它给的准数；没连就用记录里估的用量配上记下来的上限。"""
        d = self.driver
        if d and d.ctx and d.ctx.get("max"):
            remember_limit(self.id, d.ctx["max"])
            return d.ctx
        used, limit = self.session.info.get("context"), limits.get(self.id)
        if used and limit:
            return {"total": used, "max": limit, "percent": min(100, round(used / limit * 100)), "guess": True}
        return None

    def state(self):
        """比对用的轻量快照：变了才值得推一次，所以只取这几项，不去碰文件系统。"""
        s, c, d = self.session, self.chat_state(), self.driver
        return (s.gen, len(s.items), s.info.get("missing"), c["running"], c["chatError"], c["results"],
                d.phase_key() if d else None,   # 阶段只比内容不比秒数，不然每次都算变了
                (self.prep or {}).get("kind"), (self.prep or {}).get("step"),   # 查看器在核对额度 / 在交话
                (self.rejected or {}).get("n"),   # 有话被退回
                (self.switched or {}).get("n"),   # 自动切了号
                (d.ctx or {}).get("total") if d else None, self.account, self.effort,
                # 请求大小按上限的二十分之一分档：跨过一档才推，不然每过一批数据都要推一次
                (lambda r: r and int(r["bytes"] * 20 / r["limit"]))(c["reqSize"]),
                tuple(x["id"] for x in staged.get(self.id, [])),   # 待发的话变了也推给其他设备
                think_ver[s.path.stem],   # 记下了新的思考跳数（往往比那条回复写进记录还晚一点）
                users.call_count(self.sid),   # 网页对话的调用是一轮结束后才记的，比回复晚到，到了要再推一次
                reserve.settled_n[self.sid],  # 又有调用分给了预定（调用记下后几秒才分）：花费要变蓝
                self.spent_ver, platform_costs.version(),               # 总消耗补算出了新数（见 refresh_spent）
                users.share_key(self.sid),    # 主人、可见范围改了
                str(workdev.off(self.sid)) if devices.ENABLED else None,   # 工作设备改了，其他设备上也跟着变
                json.dumps(c["autoGo"], sort_keys=True))   # 额度恢复后自动继续：开关、停没停、几点发

    def connect(self) -> Driver:
        """拿到驱动这个会话的 SDK 任务；还没有或已经结束了，就在会话原来的工作目录里续接一个。"""
        if self.driver and self.driver.alive:
            return self.driver
        path = self.session.path
        if isolation.for_sid(self.sid) and path.stem != self.sid:
            raise HTTPException(400, "独立环境的子代理记录只读，请回到主会话继续对话")
        cwd = read_cwd(path, path.stat().st_mtime)
        if not cwd or not tab_path(self, cwd).is_dir():
            raise HTTPException(400, f"找不到该会话的工作目录：{cwd or '会话记录中未记载'}")
        env = isolation.for_sid(self.sid)
        self.driver = new_driver(path.stem, cwd, resume=True, account=self.account, mode=run_mode(self.account, self.mode),
                                 model=launch_model(self.account, env, self.model),
                                 effort=launch_effort(self.effort, self.account, env, self.model))
        return self.driver

    def drop_driver(self):
        """这个驱动不行了：收掉它，下次再要的时候重建一个。"""
        if self.driver:
            self.driver.task.cancel()
            self.driver = None

    def sync_effort(self):
        """会话的思考强度一律沿用用户最近一次指定的那档，存下来，续接时用 --effort 带上（续接时 CLI 不会恢复 /effort，实测过，
        不存的话 CLI 一收掉再续接就悄悄退回去）。存的是 {effort 哪档, t 什么时候定的, on 从什么时候起 CLI 真的按它跑，还没有是 None}。
        用户指定只有两种：
        - 面板上选（set_effort）：替用户往连着的 CLI 发一句 /effort，CLI 执行了才算用上；没连着的，下次带着 --effort 起来就算用上；
        - 对话里执行了 /effort（网页上打的、终端里打的都写在记录里那条 local_command，参数只在那里有）：从执行的时刻起就是这档。
          没写参数、写错的，CLI 只回用法或报错，不认；auto 是 CLI 认、--effort 不认的一档，续接带不过去，不认。
        回复上记着的实际强度（session.py 的 usedEffort）只用来显示、判断用上了没有（chat_state 的 effortPending），不拿来改存下的：
        它变了不一定是用户的意思（比如 Claude Code 设置里的默认强度被误删，CLI 续接时按自己的默认 medium 跑，
        当时以回复为准，就把两个会话错存成了 medium）。"""
        s, d = self.session, self.driver
        rec = new = efforts.get(self.id)
        gen, seen = self.effort_seen
        cmds = s.effort_cmds[seen if gen == s.gen else 0:]
        self.effort_seen = (s.gen, len(s.effort_cmds))
        for ts, arg in cmds:
            if arg not in EFFORTS:
                continue
            if (t := stamp(ts)) is None:
                print(f"会话 {s.path.stem} 的 /effort {arg} 认不出执行时间（{ts!r}），没跟着改思考强度", flush=True)
                continue
            if not new or t >= new["t"]:   # 比面板上后来选的早的不算
                new = {"effort": arg, "t": t, "on": t}
        if new and new["on"] is None and d and d.alive and d.since >= new["t"] and d.effort == new["effort"]:
            new = {**new, "on": d.since}   # 定下以后新起的 CLI，是带着 --effort 这档起的
        if new == rec:
            return
        if d:
            d.effort_to(new["effort"])
        # 这里可能在事件循环上、也可能在线程里（open_tab），不能 await：写完由写盘线程回调，没存下来要让页面看到，不然下次续接会悄悄退回去
        def done(fut):
            if e := fut.exception():
                if d:
                    d.error = f"思考强度保存失败：{e}"
                print(f"会话 {s.path.stem} 思考强度保存失败：{e}", flush=True)

        remember_effort(self, new).add_done_callback(done)

    async def live_driver(self) -> Driver:
        """拿一个确实能对话的驱动：等它连上并探活，进程已经没了就换一个新的重来。"""
        for attempt in range(2):
            d = self.connect()
            for _ in range(300):        # 等 CLI 连上，最多 30 秒
                if d.client or not d.alive:
                    break
                await asyncio.sleep(0.1)
            if d.client and await d.probe():
                return d
            self.drop_driver()          # 这个不行，收掉它，下一轮重建
            if attempt and d.error:
                raise HTTPException(500, d.error)
        raise HTTPException(500, "无法连接 Claude")


def new_driver(*args, **kw) -> Driver:
    """建驱动时账号不对、找不到 CLI 之类的问题，当成请求错误告诉前端。"""
    try:
        return Driver(*args, **kw)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))


tabs: dict[str, Tab] = {}
# 标签编号（路径的散列）→ 会话编号（文件名）。打标签这类只给标签编号、会话不用先打开的接口，靠它查主人和可见范围；
# 打开过的会话、列出过的卡片都会记进来
tid_sid: dict[str, str] = {}
# 会话编号 → 上下文上限。CLI 连过一次就记下来，存成小文件；以后没连 CLI 也能照常画进度环
LIMITS_FILE = DATA / "ctx_limits.json"
try:
    limits: dict[str, int] = json.loads(LIMITS_FILE.read_text())
except (OSError, ValueError):
    limits = {}


# 会话编号 → 主人自己起的名字。只记在这个小文件里，不去改主人的 .jsonl
TITLES_FILE = DATA / "titles.json"
try:
    titles: dict[str, str] = json.loads(TITLES_FILE.read_text())
except (OSError, ValueError):
    titles = {}

# 会话编号 → 网页上给它选的账号，同样只记在小文件里
ACCOUNTS_FILE = DATA / "session_accounts.json"
try:
    accounts: dict[str, str] = json.loads(ACCOUNTS_FILE.read_text())
except (OSError, ValueError):
    accounts = {}

# 会话编号 → 网页上给它选的权限模式。用户的选择不能只放在内存里：会话没人看一阵就被释放，
# 服务器也会重启，重新打开时要照原样恢复，不能悄悄变回默认
MODES_FILE = DATA / "session_modes.json"
try:
    modes: dict[str, str] = json.loads(MODES_FILE.read_text())
except (OSError, ValueError):
    modes = {}

# 会话编号 → 这个会话定下的思考强度 {effort, t, on}（见 Tab.sync_effort）。和权限模式一样是用户的选择，存下来，
# 释放、重启后照原样恢复；没定过的不在里面
EFFORTS_FILE = DATA / "session_efforts.json"
try:
    efforts: dict[str, dict] = json.loads(EFFORTS_FILE.read_text())
    # 旧数据只存了档位：当成文件最后一次写入时定的、当时就用上了。只会比实际晚，
    # 所以比它早的回复不会被拿来改掉用户选的那档
    _t = EFFORTS_FILE.stat().st_mtime
    efforts = {k: v if isinstance(v, dict) else {"effort": v, "t": _t, "on": _t} for k, v in efforts.items()}
except (OSError, ValueError):
    efforts = {}

# 全局设置：所有设备共用一份（网页设置里的「全局设置」那组），改了推给所有打开着的页面。
# defaultAccount：新会话、以及没选过号的会话用哪个号——某个别名，或 auto（额度最宽裕的）；没设就用启动参数 --account
SETTINGS_FILE = DATA / "settings.json"
try:
    settings: dict = json.loads(SETTINGS_FILE.read_text())
except (OSError, ValueError):
    settings = {}

# 待发的话：会话编号 → [{id, text, ts}]。输入框旁的「+」只存不发，点发送时和输入框里的话一起作为同一条消息发出
STAGED_FILE = DATA / "staged.json"
try:
    staged: dict[str, list[dict]] = json.loads(STAGED_FILE.read_text())
except (OSError, ValueError):
    staged = {}


def save_staged():
    diskq.rewrite(STAGED_FILE, json.dumps(staged, ensure_ascii=False))


# 会话编号 → 打的标签（可以有好几个），也只记在小文件里。标签列表就是这里出现过的全部标签
TAGS_FILE = DATA / "session_tags.json"
try:
    tags: dict[str, list[str]] = json.loads(TAGS_FILE.read_text())
except (OSError, ValueError):
    tags = {}


# 额度记录：按时间先后的一串 {ts, account, source, type, utilization, status, resetsAt}。
# 现在只有 CLI 推来的限额事件（source=event）；以后手动查询的结果也记在这里，所有设备看到的是同一份
QUOTA_FILE = DATA / "quota_log.json"
QUOTA_KEEP = 2000   # 只留最近这么多条
try:
    quota_log: list[dict] = json.loads(QUOTA_FILE.read_text())
except (OSError, ValueError):
    quota_log = []


def record_limit(account: str, info):
    """记下一条限额事件（SDK 的 RateLimitInfo）。"""
    quota_log.append({"ts": time.time(), "account": account, "source": "event",
                      "type": info.rate_limit_type, "utilization": info.utilization,
                      "status": info.status, "resetsAt": info.resets_at})
    del quota_log[:-QUOTA_KEEP]
    # 驱动收到限额事件就在事件循环里调这里：两份文件都交给写盘线程（卡顿记录里这一处有几十次）
    diskq.rewrite(QUOTA_FILE, json.dumps(quota_log, ensure_ascii=False))
    if info.rate_limit_type and info.utilization is not None:   # 额度历史：和状态栏、/usage 同一种格式
        rec = {"ts": quota_log[-1]["ts"], "account": account, "source": "event", "status": info.status,
               info.rate_limit_type: {"used_percentage": info.utilization * 100, "resets_at": info.resets_at}}
        diskq.append(DATA / "quota_history.jsonl", json.dumps(rec, ensure_ascii=False) + "\n")


# 标签名 → 主人给它挑的颜色（前端色板里的第几种）。没挑过的前端按名字自动配色
TAG_COLORS_FILE = DATA / "tag_colors.json"
try:
    tag_colors: dict[str, int] = json.loads(TAG_COLORS_FILE.read_text())
except (OSError, ValueError):
    tag_colors = {}

# 思考跳数：会话编号 → {回复编号 → [每一串连续思考进度事件的条数]}。思考进度是 CLI 实时吐出来的，
# 会话记录里不存，所以只有网页对话收得到；数法和观察台一样（见 chat.Driver._count_think）。
# 一行一条追加进小文件，启动时读回来
THINK_FILE = DATA / "thinking_runs.jsonl"
think: defaultdict[str, dict[str, list[int]]] = defaultdict(dict)
think_ver: Counter = Counter()   # 每个会话记过几回：变了才值得推一次
with contextlib.suppress(OSError):
    for line in THINK_FILE.open():
        with contextlib.suppress(ValueError, KeyError, TypeError):
            r = json.loads(line)
            think[r["session"]].setdefault(r["mid"], []).extend(r["runs"])


LAP_FILE = DATA / "turn_timing.jsonl"   # 每一轮各步的时刻，一轮一行（见 chat.Driver.lap_mark）


def record_lap(d, rec: dict):
    """驱动交来的一轮各步的时刻（chat.Driver._lap_close），追加进 LAP_FILE。以后排查「哪一段慢」时按数字排先后，不靠印象。"""
    diskq.append(LAP_FILE, json.dumps(rec, ensure_ascii=False) + "\n")


def record_lap_marks(sid: str, account: str | None, marks: list, end: str):
    """没交到驱动手上的一轮（核对额度时被拦下、出错）：只有查看器这边的几步，照驱动的格式记一行。"""
    if not marks:
        return
    t0 = min(t for _, t in marks)
    record_lap(None, {"t": round(t0, 3), "sid": sid, "account": account, "end": end, "from": "web",
                      "steps": {n: round(t - t0, 3) for n, t in sorted(marks, key=lambda x: x[1])}, "nReqs": 0, "reqs": []})


def record_think(session: str, mid: str, runs: list[int]):
    """记下这次请求（回复编号）前面的几串思考进度。"""
    think[session].setdefault(mid, []).extend(runs)
    think_ver[session] += 1
    diskq.append(THINK_FILE, json.dumps({"ts": time.time(), "session": session, "mid": mid, "runs": runs}) + "\n")


def clean_tags(names: list[str]) -> list[str]:
    """去掉首尾空白、空的和重复的，每个最长 24 个字，最多 20 个，顺序按主人给的。"""
    out = []
    for n in names:
        n = str(n).strip()[:24]
        if n and n not in out:
            out.append(n)
    return out[:20]


def remember_tags(tid: str, names: list[str]) -> list[str]:
    names = clean_tags(names)
    if names:
        tags[tid] = names
    else:
        tags.pop(tid, None)
    diskq.rewrite(TAGS_FILE, json.dumps(tags, ensure_ascii=False))
    return names


def remember_limit(tid: str, value: int | None):
    if not value or limits.get(tid) == value:
        return
    limits[tid] = value
    diskq.rewrite(LIMITS_FILE, json.dumps(limits))   # 推送时 brief 里会调到这里，不能在事件循环里等磁盘
subs: dict[str, set[asyncio.Queue]] = defaultdict(set)   # 会话编号 → 订阅它的推送连接
conn_who: dict[asyncio.Queue, Who] = {}   # 推送连接是谁开的：可见范围改了，看不到的连接要当场撤掉
# 前端带 delta=1 订阅的推送连接：认得 tablesDelta，附带的几张表（DELTA_TABLES）只收变了的条目、自己合并。
# 没带的（升级前打开、还没刷新的页面）照旧收整份：老前端会把思考跳数表整份换掉，收到半份就丢了以前的
delta_conns: set[asyncio.Queue] = set()
DELTA_TABLES = ("calls", "covered", "free", "think", "callPrices")
server: uvicorn.Server | None = None   # main 里建好；推送连接靠它知道服务器是不是要关了
config = argparse.Namespace(interval=0.2, root=None, readonly=True, account=None, gate_port=None, port=None)


def full_only():
    """只读模式下，凡是要用到 Claude SDK 的接口一律拒绝：发话、打断、新建会话、会话信息、切模型和权限。"""
    if config.readonly:
        raise HTTPException(403, "只读模式下无法与 Claude 对话")


def tab_id(path: Path) -> str:
    """同一个文件总是同一个编号，服务器重启后前端记住的编号也还能用。"""
    return hashlib.sha1(str(path).encode()).hexdigest()[:10]


def get_tab(tid) -> Tab:
    if tid not in tabs:
        raise HTTPException(404, "标签页不存在")
    tab = tabs[tid]
    tab.seen = time.monotonic()
    return tab


def who(request: Request) -> Who:
    """这次请求是谁：单用户实例一律是主人；多用户实例按登录 cookie 认，认不出就是游客。"""
    return users.identify(request.cookies.get(users.COOKIE))


def check_see(me: Who, sid: str):
    if not users.can_see(me, sid):
        raise HTTPException(403, users.hidden_why(me))


def check_alive(sid: str):
    """在查看器里删掉的会话（users.deleted）谁都打不开、用不了，要先恢复。410 让前端认得出是删掉了，不是文件不在。"""
    if users.is_deleted(sid):
        raise HTTPException(410, users.deleted_why(sid))


def seen_tab(tid, me: Who) -> Tab:
    """按编号拿会话，并确认这个人看得到它、它没被删掉。凡是按会话编号读写的接口都走这里。"""
    tab = get_tab(tid)
    check_see(me, tab.sid)
    check_alive(tab.sid)
    return tab


def used_tab(tid, me: Who) -> Tab:
    """要往会话里发话、驱动它的接口走这里：先得看得到，公开会话还得是它的主人（见 users.use_why）。"""
    tab = seen_tab(tid, me)
    if workflows.is_run(tab.sid):
        raise HTTPException(403, "工作流记录只读，请通过工作流接口操作任务")
    if why := users.use_why(me, tab.sid):
        raise HTTPException(403, why)
    return tab


def check_paid(me: Who, sid: str | None, account: str):
    """游客、无主会话、余额透支不能用限量号；余额透支但在这个号上还有预定的蓝，用蓝不扣钱，放行（用完由预测拦截停下）。"""
    account = base_account(account)
    def blue_on(payer):
        now = time.time()
        return reserve.blue_left(payer, account, quota(limit=0)["accounts"].get(account) or {}, now) > 0
    if why := users.paid_block(me, sid, account, blue_on):
        raise HTTPException(403, why)


def deny_hidden(sid: str):
    """可见范围或主人改了：订阅着这个会话、却从此看不到它的推送连接，当场告诉它并撤掉订阅。"""
    for tid, tab in tabs.items():
        if tab.sid != sid:
            continue
        for queue in list(subs.get(tid, ())):
            if not users.can_see(conn_who.get(queue, users.GUEST), sid):
                send(queue, json.dumps({"id": tid, "denied": users.hidden_why(conn_who.get(queue, users.GUEST))},
                                       ensure_ascii=False))
                subs[tid].discard(queue)


def send(queue: asyncio.Queue, data: str):
    """推给一条连接；它积压太多说明那台设备跟不上，清空后让它把所有标签重新拉一遍。"""
    try:
        queue.put_nowait(data)
    except asyncio.QueueFull:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait('{"all": true}')


def publish(tab: Tab):
    """和上次推送的状态比，有新条目就把新增的那一截推给所有订阅者；换代了就让它们重新拉。
    每次都附带整份 brief，可里面几张表（回复是哪个号发的、预定付了几成、思考跳数）跟着会话一起长，平时只多出一两条：
    实测一个两千多次调用的会话，一条没有新条目的推送有 376KB，几乎全是这三张表，忙的时候一个浏览器十六分钟收了 311MB。
    所以对认得增量的连接（delta_conns），这几张表只推比上一次推送变了的条目；新订阅的连接先整段拉一遍（前端 onopen 时 syncAll），
    拿到的是那一刻的整份，之后的增量都是相对更早一次推送算的，只会多给、不会漏。"""
    old, tab.pushed = tab.pushed, tab.state()
    if old == tab.pushed or not subs.get(tab.id):
        return
    tab.push_n += 1   # 先加再取快照，这一次推送带的 v 就是新编号（见 Tab.push_n）
    if old and old[0] == tab.pushed[0] and old[1] <= tab.pushed[1]:
        msg = {**tab.brief(), "start": old[1], "items": tab.session.items[old[1]:]}
        prev, tab.pushed_tables = tab.pushed_tables, {k: msg.get(k) or {} for k in DELTA_TABLES}
        delta = None if prev is None else {
            "tablesDelta": True,
            **{k: {m: v for m, v in tab.pushed_tables[k].items() if prev[k].get(m) != v} for k in DELTA_TABLES}}
    else:
        msg, delta = {"id": tab.id, "resync": True}, None
    full = small = None
    for queue in subs[tab.id]:
        if delta is not None and queue in delta_conns:
            small = small or json.dumps({**msg, **delta}, ensure_ascii=False)
            send(queue, small)
        else:
            full = full or json.dumps(msg, ensure_ascii=False)
            send(queue, full)


def adopt_new():
    """projects 下还没记主人的会话交给 users.adopt。目录清单按目录修改时间缓存（children），没新文件时几乎不花时间。"""
    if not users.TERMINAL_OWNER:
        return
    sids = [p.stem for d in children(PROJECTS_DIR, "*") if d.is_dir() for p in children(d, "*.jsonl")]
    if n := users.adopt(sids):
        print(f"{n} 个终端里建的会话记到了 {users.TERMINAL_OWNER} 名下", flush=True)


WATCH_SLOW_SECS = 3.0   # 推送循环一圈超过这么多秒就记一笔（见 watch_took）


def watch_took(total: float, took: dict[str, float], slow_tab: tuple[float, str] | None, n_tabs: int):
    """推送循环这一圈慢了：记下一共多久、最慢的几步、读记录最慢的是哪个会话。
    心跳是推送接口自己发的，这个循环卡着时前端不报断线，阶段停在发话时拿到的那一句、新消息也不来，刷新才看到进展
    （表现为「一直显示核对额度」），所以要看是这里哪一步拖的。"""
    steps = "，".join(f"{k} {v:.1f}" for k, v in sorted(took.items(), key=lambda x: -x[1])[:5] if v >= 0.1)
    worst = f"；读记录最慢的会话 {slow_tab[1]} {slow_tab[0]:.1f} 秒" if slow_tab and slow_tab[0] >= 0.1 else ""
    print(f"推送循环一圈花了 {total:.1f} 秒（这段时间新消息和状态都没推出去，{n_tabs} 个会话有人看着）：{steps}{worst}", flush=True)


async def settle_once():
    """一趟分预定：认领终端会话 → 收状态栏记录 → 把新调用分给预定（reserve.process），三步按这个顺序（前两步要赶在分配之前）。
    以前串在推送循环里，重启后第一趟要把全部历史调用过一遍，历史多的实例上一趟近十分钟，
    这段时间谁的新消息和状态都推不出去，用户碰到的「回复不显示」「一直显示核对额度」「停在正在启动」
    都落在重启后的这十分钟里。现在由 watch 另开任务跑，推送循环不等它；慢了照样记一笔。"""
    took: dict[str, float] = {}
    t_all = time.monotonic()
    for name, job, why in (("认领终端会话", adopt_new, "把终端里建的会话记到 terminal_owner 名下失败"),
                           ("收状态栏记录", inbox.collect, "收 Claude 那边记下的额度和调用失败"),
                           ("分预定", lambda: reserve.process(users.owner_of, green_left), "把调用分给预定失败")):
        t0 = time.monotonic()
        try:
            await asyncio.to_thread(job)
        except Exception as e:
            print(f"{why}：{type(e).__name__}: {e}", flush=True)
        took[name] = time.monotonic() - t0
    if (total := time.monotonic() - t_all) > WATCH_SLOW_SECS:
        steps = "，".join(f"{k} {v:.1f}" for k, v in took.items())
        print(f"分预定一趟花了 {total:.1f} 秒（推送不受影响，只是这段时间新调用晚一点分给预定）：{steps}", flush=True)


async def watch():
    """后台循环：只盯着有人订阅的会话，一个文件不管多少台设备在看都只检查一次；没人看的久了就释放。"""
    settled = went = resumed = named = 0.0
    working_err = ""
    settling: asyncio.Task | None = None   # 正在跑的那一趟分预定（settle_once）
    while True:
        now = time.monotonic()
        took: dict[str, float] = {}   # 这一圈各步花的秒数，慢了才打出来（watch_took）
        slow_tab = None               # 这一圈读记录最慢的会话：(秒数, 会话编号)

        def lap(name, t0):
            took[name] = took.get(name, 0.0) + time.monotonic() - t0

        if now - went > AUTO_GO_GAP:   # 额度恢复后自动继续：看停着的会话到点没有（见 auto_go_tick）
            went = now
            t0 = time.monotonic()
            try:
                await auto_go_tick()
            except Exception as e:
                print(f"额度恢复后自动继续那一圈出错：{type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
            lap("自动继续", t0)
        if not config.readonly:   # Claude 干到一半被预测拦截拦下：这一轮停下以后自动切号接着干（见 switch_tick）
            t0 = time.monotonic()
            try:
                await switch_tick()
            except Exception as e:
                print(f"拦下后自动切号那一圈出错：{type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
            lap("拦下后切号", t0)
        if not config.readonly:   # 重启后自动续上：一直记着谁在干活；起来一会儿以后把上次停下时在干活的续上（见 resume_tick）
            t0 = time.monotonic()
            try:
                note_working()
                working_err = ""
            except Exception as e:
                if f"{e}" != working_err:   # 同一个错只说一次，不每圈刷屏
                    working_err = f"{e}"
                    print(f"没记下哪些会话在干活（重启后自动续上要用）：{type(e).__name__}: {e}", flush=True)
            if resume_todo and now - resumed > RESUME_GAP and time.time() - STARTED > RESUME_SETTLE:
                resumed = now
                try:
                    await resume_tick()
                except Exception as e:
                    print(f"重启后自动续上那一圈出错：{type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
            lap("重启后续上", t0)
        if not config.readonly and now - named > NAME_GAP and naming.pending():   # AI 起的名字等 Claude 停下再写（见 apply_names）
            named = now
            t0 = time.monotonic()
            try:
                await apply_names()
            except Exception as e:
                print(f"写 AI 起的名字那一圈出错：{type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
            lap("AI 起名", t0)
        t0 = time.monotonic()
        try:   # 新记下的调用（网页对话一轮结束时才写）：气泡上的号、限量色和花费要跟上
            await asyncio.to_thread(users.refresh_calls)
        except Exception as e:
            print(f"读调用记录 api_calls.jsonl 失败：{e}", flush=True)
        lap("读调用记录", t0)
        # 新的调用里有预定的人发的：定下由哪几批的蓝来付。这一趟另开一个任务跑，推送循环不等它（见 settle_once）；
        # 上一趟还没跑完就不开新的，process 一次只跑一个
        if now - settled > SETTLE_SECS and not (settling and not settling.done()):
            settled = now
            settling = asyncio.create_task(settle_once())
            if devices.ENABLED and not config.readonly:   # 设备页：记电脑最后在线、登记报到好的电脑（自己的线程里跑，见 devices.tick）
                devices.kick()
        n_tabs = 0
        for tab in list(tabs.values()):
            if subs.get(tab.id):
                n_tabs += 1
                tab.seen = now
                t0 = time.monotonic()
                await asyncio.to_thread(tab.session.poll)
                lap("读会话记录", t0)
                dt = time.monotonic() - t0
                if slow_tab is None or dt > slow_tab[0]:
                    slow_tab = (dt, tab.sid)
                tab.sync_effort()
                # 总消耗放到线程里补算：长会话一次一百多毫秒，放在这里会卡住所有请求和推送（见 Tab.refresh_spent）
                if tab.spent_due():
                    t0 = time.monotonic()
                    await asyncio.to_thread(tab.refresh_spent)
                    lap("算总消耗", t0)
                t0 = time.monotonic()
                publish(tab)
                lap("推送", t0)
            elif now - tab.seen > EVICT_SECS and not (tab.driver and tab.driver.alive):
                tabs.pop(tab.id, None)
        t0 = time.monotonic()
        for b in list(births.values()):   # 网页新建、还没写出记录的会话（见 Birth）
            publish_birth(b)
            if b.done_at and now - b.done_at > BIRTH_KEEP_SECS:
                births.pop(b.id, None)
        lap("新建会话", t0)
        if (total := time.monotonic() - now) > WATCH_SLOW_SECS:
            watch_took(total, took, slow_tab, n_tabs)
        await asyncio.sleep(config.interval)


LOOP_LAG_SECS = 1.0   # 事件循环晚醒超过这么多秒就记一笔
SLOW_SECS = 3.0       # 请求过了这么多秒才回响应头就记一笔


LOOP_BEAT = 0.25      # 事件循环多久报一次平安，看门线程也按这个间隔看
loop_beat = time.monotonic()   # 事件循环最近一次报平安的时刻（看门线程读）


async def watch_loop_lag():
    """每 LOOP_BEAT 秒醒一次，报平安给看门线程（watchdog）；醒晚了说明事件循环被什么卡住了（有同步的慢操作占着它），
    这段时间所有请求和推送都停着。出过两个实例的推送一起断了三分多钟的事，事后分不清是查看器卡住还是通路卡住，所以记下来。
    这一行在卡完以后才写得出来，卡在哪一行由看门线程当场记。"""
    global loop_beat
    t = loop_beat = time.monotonic()
    while True:
        await asyncio.sleep(LOOP_BEAT)
        # 醒来先报平安再写日志：写日志也要抢全局锁，晚报的话看门线程会把写这一行的那一刻当成还卡着、记一份没用的调用栈
        now = loop_beat = time.monotonic()
        if (late := now - t - LOOP_BEAT) > LOOP_LAG_SECS:
            print(f"事件循环卡了 {late:.1f} 秒：这段时间所有请求和推送都停着", flush=True)
        t = now


# 线程最里面的那一帧是这些（文件名, 函数名）时算闲着：在等任务、等锁、等网络、等计时，不可能占着全局锁
IDLE_FRAMES = {("threading.py", "wait"), ("threading.py", "_wait_for_tstate_lock"), ("thread.py", "_worker"),
               ("queue.py", "get"), ("selectors.py", "select"), ("hostfs.py", "_recv")}
WATCH_THREAD_FRAMES = 14   # 其他线程每个只记最里面这么多帧


def watchdog(loop_thread: int, stop: threading.Event):
    """看门线程：事件循环超过 LOOP_LAG_SECS 秒没报平安，就把它这时正在执行的调用栈记进日志，一次卡顿只记一份。
    卡完以后才知道卡了多久（watch_loop_lag 那一行），卡在哪一行只能趁它卡着的时候看，所以单独开一个线程。
    只读调用栈，不碰事件循环里的任何东西；每 LOOP_BEAT 秒醒一次比一下时间，开销可以忽略。
    同时记下其他正在干活的线程（闲着的按 IDLE_FRAMES 跳过）：事件循环自己没做慢事、却被线程池里解析大文件的线程抢走了全局锁时
    （比如对一行几十 MB 的记录做 json.loads，整段不放锁），它的栈只停在等锁的那一行，看不出原因，原因在别的线程栈里
    （测试实例上记到过一次 1 秒的卡顿，栈停在写日志那一行，就是这样）。
    占着锁的是一整段 C 调用时，看门线程自己也抢不到锁，要等那段做完才记得下来，所以记到的是它刚做完、还没离开调用它的那几行
    （拿故意卡住事件循环的测试接口试过：栈里照样看得到 json.loads 那一行和调用它的函数；线程名可能已经取不到，只写编号）。"""
    # 已经记过的那次卡顿是从哪次报平安以后开始的。起头当成记过了：线程起来时 watch_loop_lag 可能还没轮上，
    # 手上的报平安时刻还是导入模块那一刻的，不能当成卡顿
    dumped = loop_beat
    me = threading.get_ident()
    while not stop.wait(LOOP_BEAT):
        beat = loop_beat
        if time.monotonic() - beat <= LOOP_LAG_SECS or beat == dumped:
            continue
        dumped = beat
        # 先把各线程的栈一口气抽出来再慢慢排版：抽的时候别的线程还在跑，拖久了看到的就是它们后来走到的地方
        names = {t.ident: t.name for t in threading.enumerate()}
        frames = sys._current_frames()
        frame = frames.get(loop_thread)
        loop_stack = traceback.extract_stack(frame)[-25:] if frame else None
        busy_stacks = [(ident, traceback.extract_stack(f)[-WATCH_THREAD_FRAMES:]) for ident, f in frames.items()
                       if ident not in (loop_thread, me)
                       and (f.f_code.co_filename.rsplit("/", 1)[-1], f.f_code.co_name) not in IDLE_FRAMES]
        del frames, frame
        stack = "".join(traceback.format_list(loop_stack)) if loop_stack else "（取不到事件循环线程的调用栈）\n"
        others = [f"--- 线程 {names.get(ident, ident)}\n" + "".join(traceback.format_list(s)).rstrip() for ident, s in busy_stacks]
        busy = ("\n其他正在干活的线程（事件循环自己没做慢事时，多半是它们占着全局锁）：\n" + "\n".join(others)) if others \
            else "\n没有别的线程在干活"
        print(f"事件循环已经卡了 {time.monotonic() - beat:.1f} 秒，它这时正在执行（最里面的在最后）：\n{stack.rstrip()}{busy}", flush=True)


class SlowRequests:
    """过了 SLOW_SECS 秒才回响应头的请求记一笔（哪个接口、等了多久）。推送连接只看它多久回头，连着的时长不算。
    写成原生 ASGI 中间件：只多包一层 send，不碰响应体，推送的长连接照常一段段往外送。
    上传大文件时收请求体的时间也算在里面。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0 = time.monotonic()
        # 分享凭证放在地址里，慢请求日志也不能把它原样记下来。
        route = "/share/[凭证]" if scope["path"].startswith("/share/") else scope["path"]
        answered = False

        async def timed_send(message):
            nonlocal answered
            if not answered and message["type"] == "http.response.start":
                answered = True
                if (spent := time.monotonic() - t0) > SLOW_SECS:
                    print(f"慢请求：{scope['method']} {route} 过了 {spent:.1f} 秒才回响应头（{message['status']}）", flush=True)
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            if not answered and (spent := time.monotonic() - t0) > SLOW_SECS:
                print(f"慢请求：{scope['method']} {route} 过了 {spent:.1f} 秒，没回响应头就结束了（浏览器断开或出错）", flush=True)


class StampedStream:
    """标准输出、标准错误每行前面加上时间：日志原来没有时间戳，出了状况对不上是几点。
    print 可能从好几个线程来（asyncio.to_thread），加锁免得两行的时间戳插到一起；子进程直接写文件描述符的输出不经过这里，没有时间戳。
    真正往文件里写、刷盘的是一个「日志」线程：日志文件所在的磁盘慢时事件循环上 print(flush=True) 那一下卡过 3.6 秒。
    标准输出、标准错误共用这一个线程和一条队列，两边交进来的先后就是写进文件的先后；退出前 drain 把排着的写完（atexit）。"""
    _cv = threading.Condition()
    _queue: list = []          # (原来的流, 一段文字)
    _thread: threading.Thread | None = None

    def __init__(self, raw):
        self.raw, self.fresh = raw, True

    def write(self, s):
        if not s:
            return 0
        with self._cv:
            out = []
            for piece in re.findall(r"[^\n]*\n|[^\n]+", s):
                if self.fresh:
                    out.append(time.strftime("%m-%d %H:%M:%S "))
                out.append(piece)
                self.fresh = piece.endswith("\n")
            self._queue.append((self.raw, "".join(out)))
            if StampedStream._thread is None:
                StampedStream._thread = threading.Thread(target=StampedStream._run, name="日志", daemon=True)
                StampedStream._thread.start()
                atexit.register(StampedStream.drain)
            self._cv.notify_all()
        return len(s)

    def flush(self):   # 刷盘由日志线程每批写完就做，这里不等
        pass

    _busy = False              # 日志线程手上正有一批在写（drain 要等它写完）

    @classmethod
    def _run(cls):
        while True:
            with cls._cv:
                while not cls._queue:
                    cls._cv.wait()
                batch, cls._queue[:] = list(cls._queue), []
                cls._busy = True
            left = []
            try:
                touched = []
                for i, (raw, text) in enumerate(batch):
                    try:
                        raw.write(text)
                    except (OSError, ValueError):   # 日志文件本身写不进去（磁盘满）没处可说：这段和后面的排回去，过一会儿再写，不丢
                        left = batch[i:]
                        break
                    if raw not in touched:
                        touched.append(raw)
                for raw in touched:
                    with contextlib.suppress(OSError, ValueError):   # 没刷下去的留在流的缓冲里，下一批写完再刷
                        raw.flush()
            finally:
                with cls._cv:
                    cls._queue[:0] = left
                    cls._busy = False
                    cls._cv.notify_all()
            if left:
                time.sleep(5)

    @classmethod
    def drain(cls, timeout: float = 5.0):
        """等排着的日志写完，最多等 timeout 秒。"""
        end = time.monotonic() + timeout
        with cls._cv:
            while (cls._queue or cls._busy) and (left := end - time.monotonic()) > 0:
                cls._cv.wait(left)

    def __getattr__(self, name):   # isatty、fileno、encoding 这些照原样
        return getattr(self.raw, name)


@asynccontextmanager
async def lifespan(_):
    # 接口线程池放大（默认 40）：算账交给 users 的算账线程池，接口线程在那儿等着、不占 CPU（users._heavy）。
    # 重启后几十个页面一起打开时等着的有好几十个，默认的 40 个被它们占满，首页、静态文件、推送都要排队（首页等过 56 秒）
    import anyio.to_thread
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADS
    # 调用表存盘（见 calls.py「存盘」）：重启后不用把全组的会话记录从头读一遍才拿得到调用；
    # 花费视图里不再变化的文件拆好的也存盘（见 spendmap.py「存盘」）：重启后第一次打开大会话的花费不用从头拆
    try:
        calls.enable_disk(DATA / "calls_cache")
    except (OSError, ValueError) as e:   # ValueError：算存盘版本时出错（codever.py）
        print(f"调用表存盘开不了（{e}），重启后照旧从头读", flush=True)
    try:
        spendmap.enable_disk(DATA / "spend_cache")
    except (OSError, ValueError) as e:
        print(f"花费存盘开不了（{e}），重启后照旧从头拆", flush=True)
    if not config.readonly:   # 网络闸门要在这个事件循环里起；起不来查看器照旧能看，但不许拉起 CLI（chat.check_proxy）
        import chat
        try:
            await gate.start(config.gate_port)
            from instance import DEFAULT_PROXY_PORT, PROXY_TABLE
            print(f"网络闸门在 127.0.0.1:{config.gate_port}，"
                  + (f"按号转给各自的代理端口（{PROXY_TABLE}，没写的号走 {DEFAULT_PROXY_PORT}）" if PROXY_TABLE
                     else f"经本机代理 127.0.0.1:{DEFAULT_PROXY_PORT} 出网" if DEFAULT_PROXY_PORT else "直连出网"), flush=True)
        except OSError as e:
            print(f"网络闸门起不来（端口 {config.gate_port}：{e}），网页对话拉不起 Claude，换个 --gate-port 重启", flush=True)
        import sshgw   # ssh 门卫：配了 gw_socket 才开（见 sshgw.py）
        sshgw.drivers = lambda: list(chat.LIVE)
        try:
            sshgw.start()
        except OSError as e:
            print(f"ssh 门卫起不来（{e}），会话里的 ccssh 用不了", flush=True)
        await workflows.start(sys.modules[__name__])
        summary_tasks.add(asyncio.create_task(summary_loop()))   # 会话摘要的后台清点（summaries.py）
        personas.drivers = lambda: list(chat.LIVE)   # 人设卡接口 ccpersona：认人和门卫一样（见 personas.py）
        try:
            personas.start(config.port)
        except OSError as e:
            print(f"人设卡接口起不来（{e}），会话里的 ccpersona 用不了，网页上照样能管人设卡", flush=True)
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, reload_personas)
        # 运行 Claude 的用户要用的文件权限（guest_files.py）：缺了 CLI 拉不起来。只记进日志、照常起服务，
        # 这时停下来 systemd 会反复拉起，大家连网页都打不开；拉起失败时用户那边看得到 CLI 报的原因（chat.Driver 的 stderr）
        import guest_files
        for line in await asyncio.to_thread(guest_files.problems):
            print(f"启动自检：{line}，这个实例拉起的 CLI 会失败（补法见 guest_files.py）", flush=True)
    if not config.readonly:
        await api_gateway._ACTIVE.start_bridge()
    await asyncio.to_thread(platform_costs.ensure_prices)
    task = asyncio.create_task(watch())
    lag = asyncio.create_task(watch_loop_lag())
    cost_poll = asyncio.create_task(platform_costs.poll()) if not config.readonly and (platform_costs.accounts() or any(r.get('billing') == 'upstream' for r in REMOTE.values())) else None
    stop_watchdog = threading.Event()
    threading.Thread(target=watchdog, args=(threading.get_ident(), stop_watchdog), name="看门", daemon=True).start()
    yield
    task.cancel()
    lag.cancel()
    if cost_poll:
        cost_poll.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cost_poll
    stop_watchdog.set()
    # 工作流先停止接单、收掉任务；再关闭闸门，避免后台任务仍在发请求。
    await workflows.stop()
    # 先等驱动把跨用户的 sudo/CLI 收尾；立即关闭事件循环会中断 disconnect，留下后台重试进程。
    pending = []
    for tab in tabs.values():
        if tab.driver:
            pending.append(tab.driver.task)
        tab.drop_driver()
    for birth in births.values():
        if birth.driver and birth.driver.alive:
            birth.driver.task.cancel()
            pending.append(birth.driver.task)
    # 调用表最后存一次（和等驱动收尾一起做，不多占停服务的时间）：上次存盘以后读到的新调用，重启后就不用再读
    saving = asyncio.create_task(asyncio.to_thread(calls.save_dirty))
    if pending:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 10)
    try:
        await asyncio.wait_for(saving, 10)
    except Exception as e:
        print(f"退出前调用表没存完（{type(e).__name__}: {e}），重启后这部分从头读", flush=True)
    await api_gateway._ACTIVE.stop_bridge()
    gate.stop()
    if not config.readonly:
        import sshgw
        sshgw.stop()
        personas.stop()
    # 写盘线程排着的小文件最后写完（驱动收尾、闸门关连接时还会交来几份，所以放在最后）
    await asyncio.to_thread(diskq.flush, 10)


def reload_personas():
    """kill -USR1 <查看器的进程号>：就地重新加载 personas.py，改了人设卡的规矩（核对、上限、钩子怎么交给 Claude Code）不用重启实例、
    不打断正在跑的对话。卡和快照每改一次都已经落盘，重新加载时从文件读回来；
    各处都是用到时才取 personas.xxx，换掉的是同一个模块对象里的内容。ccpersona 的套接字关掉重开，中间那一下连不上会报错让 AI 重试。
    只管 personas.py：别的后端代码改了照样要重启。文字钩子由 persona_hook 每次现读，改它连这一步都不用。"""
    import importlib
    try:
        with personas._lock:
            personas.stop()
            importlib.reload(personas)
        print("人设卡模块重新加载好了", flush=True)
    except Exception:
        print(f"人设卡模块重新加载失败，还在用原来的：{traceback.format_exc()}", flush=True)
    personas.drivers = lambda: list(chat.LIVE)
    try:
        personas.start(config.port)
    except OSError as e:
        print(f"人设卡接口起不来（{e}），会话里的 ccpersona 用不了，网页上照样能管人设卡", flush=True)


app = FastAPI(lifespan=lifespan)
api_gateway.install(sys.modules[__name__])


@app.exception_handler(PermissionError)
async def file_permission_denied(request, exc):
    return JSONResponse(status_code=403, content={"detail": "当前系统用户没有访问该文件或目录的权限"})


@app.middleware("http")
async def no_cache(request, call_next):
    """让浏览器每次都先确认文件有没有更新，免得改完前端还在用缓存里的旧脚本。
    图片例外：一张图只对应记录里固定的那一处，永远不会变，让浏览器放心存着。
    头像取成功时也例外：地址带着版本，换了头像地址就变（get_avatar 自己写长缓存）；没取到（404）照旧不缓存"""
    response = await call_next(request)
    if request.url.path.startswith(("/v1/", "/api/access-keys", "/api/api-sessions")):
        response.headers["Cache-Control"] = "no-store"   # 密钥及模型内容不能进浏览器缓存
    elif request.url.path == "/api/avatar" and request.method == "GET" and response.status_code == 200:
        pass
    elif "/image/" not in request.url.path:
        response.headers["Cache-Control"] = "no-cache"
    return response


app.add_middleware(SlowRequests)   # 最后加的在最外层，量到的时间包括上面那层



def tab_path(tab, value):
    env = isolation.for_sid(tab.sid)
    if not env:
        return hostfs.Path(value).expanduser()
    # 图片等相对路径仍由调用方按会话工作目录展开，不能偷偷改成家目录。
    return env.path(value) if str(value) == "~" or str(value).startswith("~/") else hostfs.for_user(value, env.user)


def project_roots(me=None):
    return isolation.roots(owner=me.name if me else None, internal=me is None or me.admin)   # 查看器账号看得到所有独立环境


def project_dirs(me=None):
    for root in project_roots(me):
        yield from children(root, "*")


def private_call(fn, *args):
    try:
        return fn(*args)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except (ValueError, OSError, TimeoutError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/environments")
def get_environments(me: Who = Depends(who)):
    out = isolation.info(me.name)
    # 新会话标签页选了独立环境、没选强度时，CLI 按这个环境自己的设置跑（见 settings_effort），前端照它写；GPT 号的默认模型前端另换
    env = isolation.get(me.name) if me.name else None
    out["effort"] = settings_effort("", env) if env else None
    # 共用环境运行 Claude 的那个用户的家目录：新会话页工作区那颗药丸填它的绝对路径（独立环境的在 environment.home）
    out["sharedHome"] = str(hostfs.HOME)
    return out


@app.post("/api/environments", dependencies=[Depends(full_only)])
def create_environment(me: Who = Depends(who)):
    if not me.name:
        raise HTTPException(401, "请登录后创建独立环境")
    return private_call(isolation.create, me.name)


class NewTab(BaseModel):
    path: str = ""
    sid: str = ""   # 分享链接只带会话编号，服务器自己去找文件
    # 是不是用户正在看的那个（前端带上）：正在看的直接解析，后台恢复的排队（见 open_tab_async）。
    # 旧网页不带（None），当作后台排队：只改先后、不丢东西，重启后旧网页一起冲上来也压得住
    focus: bool | None = None


@app.get("/api/config")
def get_config():
    exposed = set(api_gateway.allowed_accounts()) if api_gateway.enabled() and not config.readonly else set()
    return {"root": config.root, "readonly": config.readonly, "account": default_account()[0], "call": CALL,
            # host：机器在界面上的叫法；xxlb：部署者放好了看板娘的模型；pcSetupUrl：装卸电脑脚本对外的地址（都见 instance.py）
            "host": HOST_NAME, "xxlb": XXLB, "pcSetupUrl": PC_SETUP_URL,
            # proxy 是手工登记的代理端口和节点地区（见 instance.py），没登记的给 null
            # effort：这个号设置里写的思考强度，会话没选过强度时就用它（见 settings_effort）
            # gpt：是不是 GPT 号（模型下拉换成 GPT 的模型，auto 权限模式在它上面按 dontAsk 跑，见 run_mode）
            # family：哪一家的号（claude / gpt / deepseek，模型下拉按它列这家的模型，见 instance.account_family）；
            # api：按 token 付费的 API 号写这家的名字（账号面板上写余额，不画额度血条），不是就是 null
            "accounts": [{"name": a, "banned": a in BANNED, "note": BANNED.get(a, ""), "paid": a in PAID,
                          "proxy": PROXIES.get(a), "effort": settings_effort(a), "gpt": a in GPT, "family": account_family(a),
                          "api": api_relay.vendor(a)["name"] if a in API else None,
                          # relay：中转站号能选的模型（[{value, label}]，第一个是默认；label 是配置里写的下拉名字，没写是 null，前端照 Claude 模型的叫法），
                          # 前端的模型下拉、缓存估价（中转站缓存默认一小时）按它认，不认站名
                          "relay": {"models": [{"value": m, "label": RELAY[a]["labels"].get(m)} for m in RELAY[a]["models"]]}
                                   if a in RELAY else None,
                          "costAudit": platform_costs.enabled(a),
                          # hidden：界面上不单列的号（API 回环的替代入口、同站第二把令牌 present_as 并进主号名下）
                          "apiAccess": a in exposed, "hidden": a in WEB_ENTRIES or a in PRESENT_AS} for a in ACCOUNTS],
            "gptModels": GPT_MODELS, "gptAutoMode": GPT_AUTO_MODE, "apiModels": API_MODELS,
            # 单价（rates.py 换算好的、乘过收钱的比例 rates.PRICE，GPT 模型再乘 rates.GPT_PRICE），前端按最长前缀匹配模型名，只用来写花了多少分
            "rates": rates.load(price=True), "multi": users.MULTI,
            # 新会话标签页还没有会话，权限模式下拉的选项和默认值从这里拿
            "modes": MODES, "defaultMode": DEFAULT_MODE,
            "efforts": EFFORTS,   # 思考强度的几档（模型下拉里选）
            # 没选过、设置里也没写这个模型的强度时 CLI 带的那档（launch_effort），前端照它写；null 是不带、按 CLI 自己的默认
            "defaultEffort": DEFAULT_EFFORT,
            # Claude 号没选过模型、设置里也没写默认模型时 CLI 带的那个（launch_model），前端照它写；null 是不带
            "defaultModel": DEFAULT_MODEL,
            "shareUrl": SHARE_URL,   # 分享链接用的地址，null 就用浏览器当时的地址（见 instance.py）
            "publicShare": True,    # 前端先探测：旧后端运行期间不能复制一个并未开放的链接
            "firstMessageImages": True,  # 新前端只有看到后端明确支持，才会把首句图片交给新建接口
            "sidePages": True,   # 认得 /api/side（侧边栏一页页取）；旧后端不给，前端照旧一次取全部
            "sideStatus": True,  # 认得 /api/side/status（侧边栏的状态点）；旧后端不给，只有这台设备上开着的会话有点
            "sideFilter": True,  # /api/side 认得 state、tag 两个筛选，也有 /api/side/tags；旧后端不给，前端只在取到的那些里筛
            "sidePins": True,    # /api/side 第一页带 pinned，有 /api/side/pin；旧后端不给，前端不出置顶
            # 设备级的「桌面应用全屏打开」：前端在这台设备上写这个名字的 cookie，/manifest.json 看它；旧后端不给，前端不出这个开关
            "pwaCookie": users.PWA_COOKIE}


AUTO = "auto"
NO_ACCOUNT = "现在没有能用的号，等额度恢复了再试"   # auto 一个能用的号都挑不出来时，发话、建会话就这样拦下
# 5 小时窗口用满大约等于本周额度的多少（FIVE_IN_WEEK）按实例配在 instance.py（各组的号拟合出来不一样）。
# 挑号、前端的 5 小时血条、预定额度都用它：本周快用完时，5 小时窗口剩得再多也只能用到本周剩的 ÷ 它
STALE_SECS = 3600   # 额度超过这么久没查过，挑号时照样算，但说明里要标出来

def room(snap: dict, now: float, account: str | None = None) -> tuple[tuple | None, str]:
    """一个号还能用多少：两个窗口都换算成本周额度的百分点（5 小时剩的 × 这个号的 five_in_week、本周剩的），取较小的那个；
    一样多时（比如几个号的 5 小时窗口都刚重置）再比本周剩的。返回可以直接比大小的元组，两个窗口都没查到就是 None。
    snap 要先经 reserve.decorate：别人订掉的部分（hold）当成已经用掉，和界面上写的剩余是同一个数；已过重置时间的窗口按满的算。
    第二项是写进日志、给开发者看的依据，不上界面。"""
    F, W = reserve.rest(snap.get("five_hour"), now), reserve.rest(snap.get("seven_day"), now)
    if F is None and W is None:
        return None, "还没查到额度"
    rest = [x for x in (None if F is None else F * five_in_week(account), W) if x is not None]
    why = "，".join(f"{label}剩 {x:.0f}%" for x, label in ((F, "5 小时"), (W, "本周")) if x is not None)
    ts = max((snap.get(k) or {}).get("ts") or 0 for k in ("five_hour", "seven_day"))
    if ts:
        mins = int((now - ts) // 60)
        why += f"，{mins // 60} 小时 {mins % 60} 分钟前查的（有点旧了）" if now - ts > STALE_SECS else f"，{mins} 分钟前查的"
    return (round(min(rest), 3), W or 0), why


ROOMY = 50   # 自动挑号：5 小时窗口剩的超过这么多（百分点）就算足够充裕，再多也不比


def auto_ranked(me: Who, paid_ok: bool, environment: str | None, family: str = "claude") -> list[tuple[str, str]]:
    """auto 的排法，每个带上依据 [(号, 依据)]：这个人自己有先用的预定、血条右边此刻有蓝条的号整组排最前，
    组里、组外都照下面的规矩排。只看「预测绿条」：5 小时血条上剩的（本周不够时血条已经砍短了）减掉最近 30 分钟用掉的一半（血条上红的那段）。
    它超过 ROOMY 的都算足够充裕、不再比多少，这些排在最前，其中本周额度最快重置的在前（快重置的号这周剩下的额度不用就作废了）；
    没有足够充裕的，就按它多的在前；一样时按账号表的顺序。没有 5 小时限额的号（WEEK_ONLY）拿本周剩的当它的 5 小时。
    用光了的号排在最后、照样列着：都用光了挑出来的那个由预测拦截拦下，说明里写着几点恢复。
    号本来就都要花钱，限量号照样挑；游客、无主会话、余额透支的人用不了限量号（除非在那个号上手里还有预定的蓝，
    或者他就是这个号的号主，和 check_paid 一样），这些号整组排在后面，不剔掉；
    别人订掉的部分算成已经用掉（被别人订光的就是用光了）。共用环境不挑留给独立环境的集中登录号
    （isolation.private_only，标了 shared 的集中登录号两边都挑），独立环境只挑支持它的号（和前端 accountGroups 一样）。
    新建会话的自动挑号（auto_account）和被拦下时的自动切号（switch_target）用的都是它。
    family 是只在哪一家的号里挑（instance.account_family）：新建会话只挑 Claude 号，自动切号按当前的号是哪一家就只在那一家里挑。"""
    now, snaps = time.time(), quota(limit=0)["accounts"]
    seen = json.loads(json.dumps(snaps))   # 给这个人看的那份（扣掉别人的预定）；算蓝要用没扣过的原样
    reserve.decorate(seen, me.name, now)
    private, iso, solo = environment == "private", isolation.accounts(), isolation.private_only()
    out = []
    for a in ACCOUNTS:
        # 只挑 family 这一家的号：自动挑号不该悄悄把新会话换成另一家的模型（GPT 号、API 号要用就自己选），
        # 自动切号也只能在同一家的号之间换，GPT 号、DeepSeek 号不切到 Claude 号，反过来也不行
        if a in BANNED or account_family(a) != family or a in API_ENTRIES or (a not in iso if private else a in solo):
            continue
        # 这个人此刻付不起的限量号不剔掉，整组排在付得起的后面（用户反馈余额是 0 也要有挑好的号）：
        # 一个付得起的都没有时照样挑出最宽裕的那个，新会话页上看得到它，发话时由 check_paid 说清为什么用不了
        cant = a in PAID and not paid_ok and not owns(me.name, a) \
            and not (me.name and reserve.blue_left(me.name, a, snaps.get(a) or {}, now) > 0)
        snap = seen.get(a) or {}
        _, why = room(snap, now, a)
        week = snap.get("seven_day") or {}
        # 5 小时剩的照血条上写的那个数：本周不够这个窗口用满时砍到本周剩的 ÷ five_in_week（同 quota.js），
        # 所以本周快用完、用完了的号自然排到后面，不用另外判断
        five, W = reserve.rest(snap.get("five_hour"), now), reserve.rest(week, now)
        if a in WEEK_ONLY:
            five = W
        elif five is not None and W is not None:
            five = min(five, W / five_in_week(a))
        # 这个人在这个号上有设成先用的预定、此刻血条右边画着蓝条的（reserve.split 的 ff + wf，和 quota.js 画血条同一个口径，
        # 只有本周限额的号拿本周当 5 小时、系数按 1），整组排在最前，组里照下面的规矩排
        F5 = reserve.rest(week if a in WEEK_ONLY else snap.get("five_hour"), now)
        sp = reserve.split(F5, W, snap["mine"], k=1 if a in WEEK_ONLY else five_in_week(a)) \
            if F5 is not None and snap.get("mine") else None
        front = bool(sp) and sp["ff"] + sp["wf"] > 0.05
        if front:
            why += "，自己有先用的预定"
        # 下面判断充裕、比多少用的都是「预测绿条」= 绿的减掉一半红的：最近 30 分钟用得多的号接下来多半还会被用掉一些。
        # 红的照血条上画的那段（同 quota.js 的 recent）：已过重置时刻的是 0，最多到已经用掉的；只有本周限额的号不画红的
        fh = snap.get("five_hour") or {}
        if five is not None and a not in WEEK_ONLY and (fh.get("resets_at") or 0) > now:
            red = min(fh.get("recent") or 0.0, 100 - F5)
            if red > 0.05:
                five -= red / 2
                why += f"，近 30 分钟用了 {red:.0f}%，预测剩 {five:.0f}%"
        if five is None:
            key = (2, 0)   # 没查到 5 小时额度的排在最后
        elif five > ROOMY:
            # 本周重置时刻：已经过了的（新的一周还没读到）按 7 天一轮往后推；不知道的排在充裕的里面最后。
            # 订阅在那之前就到期的按到期时刻排（week_end），已经到期的照旧按重置时刻
            at = week.get("resets_at") or math.inf
            while at <= now:
                at += 7 * 86400
            if (end := week_end(a, at)) > now:
                at = end
            key, why = (0, at), f"{why}，5 小时足够充裕，本周额度 {datetime.fromtimestamp(at):%m-%d %H:%M} 到头" if at < math.inf else why
        else:
            key = (1, -five)
        out.append(((not front, cant, *key), a, f"{why}（这个人付不起这个限量号）" if cant else why))
    out.sort(key=lambda x: x[0])   # 排序是稳定的：一样的保持账号表的顺序
    return [(a, why) for _, a, why in out]


def web_ranked(me: Who, paid_ok: bool, environment: str | None, family: str = "claude") -> list[tuple[str, str]]:
    """按底层容量排序后换成网页入口；网关仍使用 auto_ranked 的真实号，不把回环当第二个订阅。"""
    return [(web_account(a), why) for a, why in auto_ranked(me, paid_ok, environment, family)
            if web_account(a) not in BANNED]


def plain_sub(account: str) -> bool:
    """共用的 Claude 订阅号（ccNN 这类，连同它的API回环入口）：不是 GPT 号，也不是按 token 付费的 API 号、中转站。
    个人默认账号只有号主能固定成这种号（set_settings），前端设置页的下拉同一个规矩"""
    b = base_account(account)
    return account_family(b) == "claude" and b not in API


def check_web_account(account: str):
    if account in WEB_ENTRIES:
        raise HTTPException(400, f"{account} 原生入口已隐藏，请选择 {web_account(account)}")


def auto_account(me: Who, paid_ok: bool, environment: str | None) -> tuple[str | None, str]:
    """auto：在这个人此刻能用的号里挑额度最宽裕的（auto_ranked 的第一个）。一个都没有就是 None，不拿用不了的号顶上。"""
    if ranked := web_ranked(me, paid_ok, environment):
        return ranked[0][0], f"自动挑了 {ranked[0][0]}：{ranked[0][1]}"
    return None, "没有能用的号"


def switch_target(me: Who, paid_ok: bool, environment: str | None, current: str, path, payer: str | None,
                  model: str | None, new_bytes: int, images: int = 0, call_account: dict | None = None) -> str | None:
    """自动切号（users.auto_switch 开着时）：当前的号被预测拦截拦下了、或者上游报了限额，按自动挑号同一套排法（auto_ranked）从最宽裕的往下，
    挑第一个这次请求过得去的号（带着上下文换号整段算缓存写入，limit_block 会算进去）；上游 LIMIT_AVOID 之内拒过的号不挑（见 limit_hits）。
    一个都没有是 None，照旧拦下。回环入口照常切（额度用完了那边也要换号接着干），
    只是不切回同一个底层号。只在和当前的号同一家的号里挑（auto_ranked 的 family），同一家没有过得去的就照旧拦下。"""
    now = time.time()
    for a, why in web_ranked(me, paid_ok, environment, account_family(current)):
        if base_account(a) == base_account(current) or now < cooling_until(a):
            continue
        if not limit_block(path, payer, a, run_model(a, model), new_bytes, images, call_account):
            print(f"[自动切号] {current} 被拦下，换成 {a}（{why}）", flush=True)
            return a
    return None


switched_n = itertools.count(1)


def compact_pick(tab: "Tab", me: Who) -> str | None:
    """上下文压缩以后的第一轮发话（记录里的压缩分界比上一轮发话时多了）：按新建会话同一套挑号（default_account，
    auto 时先用的预定优先、再挑最宽裕的）重新挑一次，挑出来的和现在用的不一样就返回它。
    压缩后上下文整段换成了摘要，这一轮的缓存本来就要重写，换号不会多写多少。发话的人关了自动切号就不换。
    这个标签页头一回发话（查看器刚启动、或标签释放后重新打开）只记下数到几、不换：不知道之前那几次压缩以后发没发过话。"""
    n, seen = tab.session.compacts, tab.compacts_seen
    tab.compacts_seen = n
    if tab.account in API_ENTRIES or seen is None or n <= seen or not users.auto_switch(me):
        return None
    to, why = default_account(me, users.paid_ok(me, tab.sid), "private" if isolation.for_sid(tab.sid) else None)
    if not to or to == tab.account:
        return None
    if account_family(to) != account_family(tab.account):   # 不换到另一家的号，同 switch_target
        print(f"[压缩后重新挑号] 会话 {tab.sid}：挑了 {to}，和现在的 {tab.account} 不是同一家的号，不换", flush=True)
        return None
    print(f"[压缩后重新挑号] 会话 {tab.sid}：{tab.account} → {to}（{why or '按设置'}）", flush=True)
    return to


def default_account(me: Who = users.NOBODY, paid_ok: bool | None = None,
                    environment: str | None = None) -> tuple[str | None, str]:
    """新会话（和没选过号的会话）用哪个号，以及为什么（为什么只写进日志、给开发者看，不上界面）。
    登录的人看自己设的（没设就是 auto，users.user_default），游客看全局设置：固定某个号，或 auto 挑额度最宽裕的（auto_account，挑不出来是 None）；
    没设、或设的号现在用不了，就用启动参数 --account。paid_ok 为 False（游客、无主会话、余额透支）时设的限量号也算用不了，
    除非这个人就是那个号的号主（花的钱流回他自己，不按余额拦）。
    environment 是新会话的运行环境（shared / private），只影响 auto 在哪些号里挑。"""
    if config.readonly:
        return config.account, ""
    if paid_ok is None:   # 没说是哪个会话：按新建会话算
        paid_ok = users.paid_ok(me)
    want = web_account(users.user_default(me) or settings.get("defaultAccount"))
    if want == AUTO:
        return auto_account(me, paid_ok, environment)
    fallback = web_account(config.account)
    if want in PAID and not paid_ok and not owns(me.name, want):
        why ="游客不能用" if me.name is None else "会话没有主人或余额透支了"
        return fallback, f"设的 {want} 是限量号，{why}，先用启动时的 {fallback}"
    if want and want in ACCOUNTS and want not in BANNED:
        return want, ""
    if want:
        return fallback, f"设置的 {want} 现在用不了（{BANNED.get(want, '没有这个号')}），先用启动时的 {fallback}"
    return fallback, ""


@app.get("/api/settings")
def get_settings(me: Who = Depends(who)):
    """全局设置、这个人自己设的默认账号（多用户实例里登录了才有），外加此刻新建会话会用哪个号、为什么
    （auto 时随额度变，所以每次现算）。canGlobal：能不能改全局那份（单用户实例的主人、多用户实例的查看器账号）。"""
    account, why = default_account(me)
    # private：独立环境的新会话标签页此刻会用的号（auto 在支持独立环境的号里挑）。why 只给开发者看，前端不显示
    return {"settings": settings, "mine": users.user_default(me), "default": {"account": account, "why": why},
            "private": default_account(me, environment="private")[0],
            # fresh：上次在新会话标签页上发话时选的运行环境、工作区、标签（users.fresh_tab），游客没有
            "fresh": users.fresh_tab(me),
            # showTests：全部会话、最近会话里显示不显示测试会话（users.show_tests），游客没有
            "showTests": users.show_tests(me),
            # features：新会话页上人设卡、共用环境、标签这几项开没开（users.features），游客没有
            "features": my_features(me),
            # autoSwitch：当前的号被预测拦截拦下时自动切号（users.auto_switch），游客没处关、不给（照样开着）
            "autoSwitch": users.auto_switch(me) if users.MULTI and me.name else None,
            # bottomTabs：也留着底部标签页栏（users.bottom_tabs），游客没有；旧后端不给，前端照旧没有标签页栏
            "bottomTabs": users.bottom_tabs(me),
            # voice：讯飞语音输入的配置（voice.info，没配是 {}），游客没有；旧后端不给，前端不出语音按钮和这项设置
            "voice": voice.info(me), "voiceServices": voice.services(),
            "fallback": web_account(config.account), "canGlobal": me.admin}


ACG_SPENT_FEN = 50   # 累计花掉超过 5 毛的人，二次元没切过就开着


def used_features(name: str) -> dict:
    """这个人用过哪几样（users.features 没切过的开关按它定）：能选共用环境（users.uses_shared：名下有共用环境的会话，
    或者查看器账号给他打开了）、用过人设卡或者累计花掉超过 ACG_SPENT_FEN 分（二次元）。没用过共用环境的只用独立环境，没建好的页面上自动建（js/sessions.js）。
    自己在设置里切过二次元的照他切的，花得再多也不改（users.features）。"""
    with users._lock:
        mine = [(sid, r) for sid, r in users.owners.items() if r.get("owner") == name]
    return {"shared": users.uses_shared(name),
            "persona": personas.used_by(name, [sid for sid, _ in mine])
                       or users.cached_spent(name) * reserve.FEN > ACG_SPENT_FEN}


def my_features(me: Who) -> dict | None:
    return users.features(me, lambda: used_features(me.name))


class ShowTestsBody(BaseModel):
    on: bool


@app.post("/api/settings/show-tests")
def set_show_tests(body: ShowTestsBody, me: Who = Depends(who)):
    """「显示测试会话」开关：登录的人一选就存下来，各设备共用。游客和单用户实例前端记在浏览器里，不走这里。"""
    users.set_show_tests(me, body.on)
    return {"showTests": users.show_tests(me)}


@app.post("/api/settings/auto-switch")
def set_auto_switch(body: ShowTestsBody, me: Who = Depends(who)):
    """「自动切号」开关：登录的人一选就存下来，各设备一样。"""
    users.set_auto_switch(me, body.on)
    return {"autoSwitch": users.auto_switch(me)}


@app.post("/api/settings/bottom-tabs")
def set_bottom_tabs(body: ShowTestsBody, me: Who = Depends(who)):
    """「底部标签页」开关（简洁模式里也留着标签页栏）：登录的人一选就存下来，各设备一样。"""
    users.set_bottom_tabs(me, body.on)
    return {"bottomTabs": users.bottom_tabs(me)}


class VoiceBody(BaseModel):
    service: int
    appid: str
    apiKey: str = ""      # 留空沿用已存的
    apiSecret: str = ""


@app.put("/api/voice")
def put_voice(body: VoiceBody, me: Who = Depends(who)):
    """保存自己的讯飞语音输入配置（voice.set_keys）"""
    return {"voice": voice.set_keys(me, body.service, body.appid, body.apiKey, body.apiSecret)}


@app.delete("/api/voice")
def delete_voice(me: Who = Depends(who)):
    return {"voice": voice.clear(me)}


@app.post("/api/voice/sign")
def sign_voice(me: Who = Depends(who)):
    """每次开口现签一个连讯飞的地址（voice.sign），浏览器拿它直连讯飞"""
    return voice.sign(me)


class FeatureBody(BaseModel):
    key: str   # users.FEATURES 里的一个
    on: bool


@app.post("/api/settings/feature")
def set_feature(body: FeatureBody, me: Who = Depends(who)):
    """简洁模式新会话页上人设卡、共用环境、标签那几项的开关：一选就存下来，各设备一样。"""
    users.set_feature(me, body.key, body.on)
    return {"features": my_features(me)}


class SettingsBody(BaseModel):
    defaultAccount: str | None = None   # 别名、auto，或空（不设：自己的就跟全局，全局的就用启动参数）
    scope: str = "global"               # global 全局那份（多用户实例里只给游客用），mine 自己的


@app.post("/api/settings", dependencies=[Depends(full_only)])
def set_settings(body: SettingsBody, me: Who = Depends(who)):
    """改默认账号，存下来。全局那份改了推给所有打开着的页面（各自重新取，因为每人看到的「此刻会用哪个号」不一样）。"""
    want = (body.defaultAccount or "").strip()
    check_web_account(want)
    if want and want != AUTO and (want not in ACCOUNTS or want in BANNED):
        raise HTTPException(400, f"{want} 不能设为默认账号：{BANNED.get(want, '账号不存在')}")
    if want and want != AUTO and EXPIRES.get(base_account(want), math.inf) < time.time():
        raise HTTPException(400, f"{want} 不能设为默认账号：订阅已到期")
    # 个人默认账号不能固定成共用的 Claude 订阅号：大家共用的号额度此起彼伏，固定了反而常常挤在用满的号上，
    # 选「自动」会挑此刻最宽裕的；自己是号主的号除外。已经设了的不动（选择是用户的数据），只是不能再新设
    if body.scope == "mine" and want and want != AUTO and plain_sub(want) and not owns(me.name, want) \
            and want != users.user_default(me):
        raise HTTPException(400, f"{want} 是共用的订阅号，只有号主可以设为默认账号；选「自动」会挑额度最充足的号")
    if body.scope == "mine":
        users.set_user_default(me, want or None)
        return get_settings(me)
    users.need_admin(me)
    if want in PAID and users.MULTI:
        raise HTTPException(400, f"{want} 是限量号，不能设为全局默认账号：全局默认账号供游客和未设置个人默认账号的用户使用，游客无法使用限量号")
    if want:
        settings["defaultAccount"] = want
    else:
        settings.pop("defaultAccount", None)
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=1))
    except OSError as e:
        raise HTTPException(500, f"设置保存失败：{e}")
    for queue in {q for qs in subs.values() for q in qs}:   # 一条连接订阅了好几个会话，只推一次
        send(queue, '{"settings": true}')
    return get_settings(me)


def new_tab(path: Path) -> Tab:
    """从头解析一个会话（大会话要几秒，在线程里调）。"""
    tab = Tab(path)
    tab.session.poll()
    tab.pushed = tab.state()
    return tab


def open_tab(path: Path) -> Tab:
    """打开一个会话；别的设备已经打开过的就直接共用那一份。"""
    tid = tab_id(path)
    if tid not in tabs:
        # setdefault：同一时刻 open_tab_async 那边可能刚解析好放进去，不拿自己这份顶掉它
        tabs.setdefault(tid, new_tab(path))
    tab = get_tab(tid)
    tab.session.poll()   # 没人订阅的会话不会被后台循环更新，打开时顺手读一下新内容
    tab.sync_effort()
    return tab


# 重启后大家一起重开标签页：十几个网页同时把几十个大会话各开好几遍，几十个线程一起从头解析、抢全局锁，
# 打开一个要 5～32 秒，别的请求跟着慢。所以网页打开会话分两种：用户正在看的（focus）直接解析；后台恢复的排队，
# 同时只解析 BG_OPEN_AT_ONCE 个。同一个会话不管谁、几个请求一起来，都只解析一遍，后来的等它解析好共用。
# 已经在内存里的不排队。排队在事件循环里等，不占线程池（add_tab 原来是同步接口，排着的请求会把线程池占满）
BG_OPEN_AT_ONCE = 1
bg_open = asyncio.Semaphore(BG_OPEN_AT_ONCE)
opening: dict[str, asyncio.Future] = {}   # 正在从头解析的会话 → 解析完（成没成都算）时置上


async def parse_once(tid: str, path: Path):
    while tid not in tabs:
        if (fut := opening.get(tid)) is not None:
            await asyncio.shield(fut)   # 别人正在解析同一个会话：等它；它没成的话下一圈自己来，出错原因各自报
            continue
        fut = opening[tid] = asyncio.get_running_loop().create_future()
        try:
            tabs.setdefault(tid, await asyncio.to_thread(new_tab, path))
        finally:
            opening.pop(tid, None)
            fut.set_result(None)


async def open_tab_async(path: Path, focus: bool) -> Tab:
    tid = tab_id(path)
    if tid not in tabs:
        if focus:
            await parse_once(tid, path)
        else:
            async with bg_open:
                await parse_once(tid, path)   # 排队时正在看的请求先把它解析好了，这里一进来就出去
    return await asyncio.to_thread(open_tab, path)   # 已经在内存里了：照旧顺手读一下新内容


def open_target(body: "NewTab", me: Who) -> Path:
    """网页要打开的会话文件，连同能不能打开的检查（在线程里调：经小助手查文件要跑来回）。"""
    if sid := body.sid.strip():
        # 会话编号是文件名，只许字母数字和横杠：要拿去拼 glob，不能让人塞通配符或路径进来
        if not re.fullmatch(r"[0-9A-Za-z-]+", sid):
            raise HTTPException(400, f"会话编号无效：{sid}")
        check_see(me, sid)   # 和按路径打开一样查：链接谁都能转发，能不能看还是按可见范围
        check_alive(sid)
        path = calls.find(sid)
        if not path:
            raise HTTPException(404, f"找不到该会话：{sid}")
        return path
    path = private_call(isolation.open_path, body.path.strip(), me.name, me.admin)
    env = isolation.for_sid(users.sid_of(path))
    expected = env.projects if env else PROJECTS_DIR
    if env and getattr(path, "run_as", None) != env.user:
        raise HTTPException(403, "会话路径与登记的运行环境不一致")
    if path.suffix != ".jsonl":
        raise HTTPException(400, f"仅可打开会话记录（.jsonl）：{path}")
    if not path.is_file():
        raise HTTPException(400, f"找不到文件：{path}")
    # 会话记录都在 projects 下。别处的 .jsonl（比如查看器自己的账本、调用记录）也能按会话来解析，内容就露出去了，
    # 所以不是查看器账号只许打开 projects 里的（按软链接指向的真文件认）
    if not me.admin and not path.resolve().is_relative_to(expected.resolve()):
        raise HTTPException(403, f"仅可打开会话目录中的会话记录：{path}")
    check_see(me, users.sid_of(path))   # 按路径直接打开，不能绕过列表里的筛选
    check_alive(users.sid_of(path))     # 删掉的会话浏览文件夹还列得出来，打开要先恢复
    return path


@app.post("/api/tabs")
async def add_tab(body: NewTab, me: Who = Depends(who)):
    path = await asyncio.to_thread(open_target, body, me)
    tab = await open_tab_async(path, bool(body.focus))
    return await asyncio.to_thread(tab.brief)


@app.get("/api/stream")
async def stream(ids: str = "", delta: int = 0, me: Who = Depends(who)):
    """推送连接：订阅这几个会话，有新条目就推过来；服务器不认识的编号会让前端重新打开，看不到的告诉它没有权限。
    delta=1 是前端说自己认得表的增量（见 publish）。"""
    wanted = [t for t in ids.split(",") if t]
    known = [t for t in wanted if t in tabs and users.can_see(me, tabs[t].sid) and not users.is_deleted(tabs[t].sid)]
    dead = [t for t in wanted if t in tabs and t not in known and users.is_deleted(tabs[t].sid)]   # 别的设备上删掉了
    hidden = [t for t in wanted if t in tabs and t not in known and t not in dead]
    # 网页新建、还没写出记录的会话（见 Birth）：只有建它的人订阅得到
    born = [t for t in wanted if t in births and births[t].me.name == me.name]
    queue = asyncio.Queue(maxsize=256)
    conn_who[queue] = me
    if delta:
        delta_conns.add(queue)
    for tid in known + born:
        subs[tid].add(queue)

    async def events():
        try:
            yield "retry: 2000\n\n"
            for tid in born:   # 订阅前那段时间的进展前端没收到：先把这一刻的状态给它
                yield f"data: {json.dumps(births[tid].message(), ensure_ascii=False)}\n\n"
            for tid in set(wanted) - set(known) - set(hidden) - set(dead) - set(born):
                if tid.startswith("birth-"):   # 服务器重启过（或者早就清掉了），不知道它后来建成没有
                    yield f"data: {json.dumps({'id': tid, 'birth': True, 'gone': True})}\n\n"
                else:
                    yield f"data: {json.dumps({'id': tid, 'reopen': True})}\n\n"
            for tid in hidden:
                yield f"data: {json.dumps({'id': tid, 'denied': users.hidden_why(me)}, ensure_ascii=False)}\n\n"
            for tid in dead:
                yield f"data: {json.dumps({'id': tid, 'deleted': users.deleted_why(tabs[tid].sid)}, ensure_ascii=False)}\n\n"
            beat = time.monotonic()
            # 服务器要关了（Ctrl+C）就自己结束：这条连接不断，uvicorn 关停时就得干等
            while not (server and server.should_exit):
                try:
                    yield f"data: {await asyncio.wait_for(queue.get(), 1)}\n\n"
                except asyncio.TimeoutError:
                    pass
                # 心跳：让服务器发现已经断开的连接；发成一条前端收得到的消息（注释行前端看不见），
                # 前端据此判断连接是不是已经「半死」（手机切后台、隧道断过一下时，浏览器不报错也不重连）。
                # 带上这条连接订阅的各会话推到了第几次（v，见 Tab.push_n），前端拿来对账。忙的时候也照样按间隔发，
                # 以前只在闲了一个间隔时才发，一直有推送时就对不了账。排在它前面的推送都已经先发出去了，所以前端收到的比它旧就是真漏了
                if time.monotonic() - beat >= HEARTBEAT_SECS:
                    beat = time.monotonic()
                    v = {t: tabs[t].push_n for t in known if t in tabs and queue in subs.get(t, ())}
                    yield f"data: {json.dumps({'ping': 1, 'every': HEARTBEAT_SECS, 'v': v})}\n\n"
        finally:
            conn_who.pop(queue, None)
            delta_conns.discard(queue)
            for tid in known + born:
                if tid in subs:   # 可见范围改了被撤掉的，这里可能已经没了
                    subs[tid].discard(queue)
                    if not subs[tid]:
                        subs.pop(tid, None)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


class Say(BaseModel):
    text: str
    images: list[dict] | None = None   # 网页里贴的图：[{media_type, data(base64)}]
    files: list[str] | None = None     # 回形针传好的文件（upload 回的路径），发话时在话后面写明放在哪
    staged: bool = False              # 把这个会话的待发的话一起带上（排在输入框这句前面）
    nonce: str | None = None          # 前端给这句话起的编号：后台核对额度拦下时，推送里带着它，前端认得出是哪一句被退回


class NewSession(BaseModel):
    environment: str = "shared"
    cwd: str
    text: str = ""
    images: list[dict] | None = None   # 首句贴的图：同 Say.images；不传时旧的纯文字新建照旧
    account: str | None = None
    tags: list[str] = []
    mode: str | None = None    # 新会话标签页上选的权限模式，不选就是 DEFAULT_MODE
    model: str | None = None   # 新会话标签页上选的模型，不选就按 Claude Code 自己的设置
    effort: str | None = None  # 新会话标签页上选的思考强度，不选就按 Claude Code 自己的设置
    visible: str | list[str] | None = None   # 新会话标签页上选的可见范围（登录的人才有），不选就是只有自己
    persona: str | None = None   # 新会话标签页上选的人设卡编号，空字符串是不用；不给就按这个人设的默认（personas.pick）
    personaConsent: str | None = None   # 弹窗里同意过的那一版的指纹（带命令、脚本或很长的卡第一次用时，见 personas.pick）
    remember: bool = False       # 从新会话标签页发的：记下这次选的，下次开新会话标签页照着填（选文件夹新建的对话框不记）
    rememberPersona: bool = True  # 交 False 是这次的卡不记成默认；现在的页面不交了（二次元关着也照样用默认卡），旧页面关了人设卡那一项时还会交
    devicesOff: list[str] = []   # 新会话页上取消勾选的工作设备（workdev.py）；默认什么都不取消，登记了的都能用
    todos: list[str] = []        # 从待办页勾选了几条发到新会话（todos.py）：建会话时记上从它们开出、替它认领


class TitleBody(BaseModel):
    title: str


def save_titles():
    diskq.rewrite(TITLES_FILE, json.dumps(titles, ensure_ascii=False))   # 写不进去由写盘线程在服务输出里说


@app.post("/api/tabs/{tid}/title")
async def set_title(tid: str, body: TitleBody, me: Who = Depends(who)):
    """给会话起个自己的名字。
    可对话模式：和 CLI 里的 /rename 一样，往 .jsonl 末尾追加一条 custom-title 记录，终端的 /resume 列表里也是新名字。
    只读模式：只记在 titles.json 这个小文件里，绝不去改主人的 .jsonl；留空就恢复成记录里的标题。"""
    tab = used_tab(tid, me)
    name = body.title.strip()[:80]
    if not config.readonly:
        rename_in_log(tab, name)
        naming.forget(tab.sid)   # 用户自己起的名字，AI 之前起的、还没写进去的都作废（naming.py）
    elif name:
        titles[tid] = name
        save_titles()
    elif titles.pop(tid, None):
        save_titles()
    return tab.brief()


def rename_in_log(tab: Tab, name: str):
    """照 /rename 的样子写一条 custom-title。网页连着的 CLI 进程内存里还记着旧名字，以后可能把旧名字再写一遍，
    所以 Claude 正在干活时不让改；空闲的就收掉，下次发话再续接，续接时读到的就是新名字。"""
    if not name:
        raise HTTPException(400, "名称不能为空")
    if (d := tab.driver) and d.running:
        raise HTTPException(409, "Claude 正在运行，请在本轮结束后重命名")
    try:
        append_title(tab.session.path, name)
    except OSError as e:
        raise HTTPException(500, f"无法写入会话记录：{e}")
    tab.drop_driver()
    if titles.pop(tab.id, None):   # 以前在只读模式下起的名字会压过记录里的，去掉它
        save_titles()
    tab.session.poll()


def append_title(path, name: str):
    """往会话记录末尾追加一条 custom-title（网页改名和 AI 起名都走这里）；写不进去抛 OSError。"""
    line = json.dumps({"type": "custom-title", "customTitle": name, "sessionId": path.stem},
                      ensure_ascii=False, separators=(",", ":"))
    with path.open("rb") as f:   # 万一最后一行没换行，先补上，别把两条记录粘在一起
        size = f.seek(0, 2)
        if size:
            f.seek(-1, 2)
        gap = bool(size) and f.read(1) != b"\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(("\n" if gap else "") + line + "\n")


def last_custom_title(path) -> str:
    """整份记录里最近的 custom-title：会话没开着标签页时用（AI 起的名字等到标签页收掉了才写）。
    只看尾巴可能漏掉很早以前起的名字，漏了就会把用户起的名字当成没有，所以整份看；先按字节筛，含那个词的行才解析。"""
    found = ""
    with path.open("rb") as f:
        for line in f:
            if b'"custom-title"' in line:
                with contextlib.suppress(ValueError):
                    found = json.loads(line).get("customTitle") or found
    return found


def name_title(sid: str) -> str:
    """会话记录里最近的 custom-title（naming.title_of，ccname 在线程里问）。ccname 只会从连着 CLI 的会话里来：
    要么开着标签页，要么是还没写出记录的新会话（Birth，那就还没有名字）。都不是就说找不到，不能当成没有名字。"""
    tab = next((t for t in list(tabs.values()) if t.sid == sid), None)
    if tab:
        return tab.session.info.get("customTitle") or ""
    if f"birth-{sid}" in births:
        return ""
    raise HTTPException(409, "查看器这会儿找不到这个会话的标签页，过一会儿再试")


naming.title_of = name_title


def name_nudge(d: Driver) -> str:
    """起名提醒（chat.Driver.on_name：主对话里工具执行后、用户发话时，在线程里跑）。条件见 naming.py：
    上下文到了、用户说了够多句、AI 调用了够多次工具、还没有用户起的名字，每个会话只提醒一次。
    便宜的先看：提醒过的、无主会话（ccname 认不出人）、上下文还小的直接返回，不去数条目。"""
    sid = d.session_id
    if not personas.socket_path or naming.nudged(sid) or not users.owner_of(sid):
        return ""
    tab = next((t for t in list(tabs.values()) if t.driver is d), None)
    if not tab:
        return ""
    s = tab.session
    # 驱动的准数一轮结束才更新，一轮里头用记录里最后一次调用的大小（session.py 的 context）
    ctx = max((d.ctx or {}).get("total") or 0, s.info.get("context") or 0)
    if ctx < naming.NUDGE_CTX:
        return ""
    kind, title = naming.status(sid, s.info.get("customTitle"))
    if kind == "user":
        return ""
    items = list(s.items)
    # 用户说的话：不算 CLI 自己塞的（auto）、重复追加的（dup），一条消息里单独拆出来的图片也不另算
    said = sum(1 for x in items if x.get("kind") == "user" and x.get("text") and not x.get("auto") and not x.get("dup"))
    tools = sum(1 for x in items if x.get("kind") == "tool" and not x.get("dup"))
    import chat
    return naming.nudge(sid, kind, title, ctx, said, tools, chat.name_cmd(bool(d.environment)))


NAME_GAP = 2   # 后台循环每隔这么多秒看一次有没有 AI 起的名字等着写（apply_names）


async def apply_names():
    """AI 起的名字（naming.py 的待写）等 Claude 停下再写进会话记录：这一轮结束、没有排着的话、后台任务和约好的叫醒，
    或者 CLI 已经收掉了。写完和网页改名一样收掉 CLI（它内存里记着旧名字，见 rename_in_log）；有后台任务、约好叫醒时收掉会断了它们，所以接着等。
    写之前再看一次：这期间用户自己起了名字，又不是用户要 AI 改的（没加 --force），就不写。"""
    import chat
    for sid, p in naming.pending().items():
        tab = next((t for t in list(tabs.values()) if t.sid == sid), None)
        d = tab.driver if tab else next((x for x in list(chat.LIVE) if x.session_id == sid and x.alive), None)
        if d and d.alive and (d.running or d.waiting_bg() or d.next_wake()):
            continue
        if d and not tab:
            continue   # 新建的会话还没交给标签页（Birth），等它交过去
        try:
            path = None
            if tab:
                title = tab.session.info.get("customTitle") or ""
            else:
                path = await asyncio.to_thread(calls.find, sid)
                if not path:
                    if f"birth-{sid}" not in births:
                        naming.drop(sid)
                        print(f"会话 {sid} AI 起的名字「{p['name']}」没写：找不到这个会话的记录", flush=True)
                    continue
                title = await asyncio.to_thread(last_custom_title, path)
            kind, _ = naming.status(sid, title)
            if kind == "user" and not p.get("force"):
                naming.drop(sid)
                print(f"会话 {sid} AI 起的名字「{p['name']}」没写：用户自己起了名字「{title}」", flush=True)
                continue
            if title != p["name"]:
                if tab:
                    rename_in_log(tab, p["name"])
                else:
                    await asyncio.to_thread(append_title, path, p["name"])
            naming.written(sid, p["name"])
            print(f"会话 {sid} 改名为 AI 起的「{p['name']}」", flush=True)
        except Exception as e:
            why = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
            if naming.failed(sid, why):
                print(f"会话 {sid} AI 起的名字「{p['name']}」没写进去：{why}，过一会儿再试", flush=True)


# 会话摘要（summaries.py）：后台每 SUMMARY_GAP 秒清点一次，到点的会话从主会话分叉出去写，不进主会话的上下文
SUMMARY_GAP = 30
SUMMARY_TAIL = 2 << 20   # 找主会话最后一条回复只读文件末尾这么多字节
summary_running: set[str] = set()
summary_tasks: set[asyncio.Task] = set()


def _iso_ts(ts: str) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def summary_tail(path: Path) -> tuple[str, str, str] | None:
    """主会话最后一条真的回复：(时刻, 回复编号, 模型)。侧链、CLI 自己补的 <synthetic> 不算；读文件末尾一段就够。
    CLI 按内容块一块写一行，最后一行的时刻就是这次调用结束的时候。"""
    with path.open("rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - SUMMARY_TAIL))
        lines = f.read().splitlines()
    for raw in reversed(lines):
        if b'"type":"assistant"' not in raw:
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        m = e.get("message") or {}
        if e.get("type") == "assistant" and not e.get("isSidechain") and m.get("id") and m.get("model") != "<synthetic>":
            return e.get("timestamp") or "", m["id"], m.get("model") or ""
    return None


def summary_candidates(now: float) -> list[dict]:
    """该写摘要的会话（在线程里跑）：会话主人在名单里（summaries.USERS）、不是工作流和独立环境、上一次调用结束正好过了 45 分钟，
    号是做的那几家、和会话现在选的号和模型一样，上下文和调用次数比上次触发多出够多。"""
    if summaries.broken or config.readonly:
        return []
    out = []
    roots = list(project_roots())
    snap = calls.snapshot(roots)
    for root in roots:
        base = str(root).rstrip("/") + "/"
        for sid, rel in (snap.mains.get(str(root)) or {}).items():
            st = snap.stats.get(base + rel)
            if not st or st[1] < now - summaries.LATE - 60 or sid in summary_running:
                continue   # 最后一次调用结束不会晚于文件最后修改的时候
            owner = users.owner_of(sid)
            if not owner or (summaries.USERS is not None and owner not in summaries.USERS):
                continue
            if workflows.is_run(sid) or isolation.for_sid(sid):
                continue
            path = root / rel
            try:
                tail = summary_tail(path)
                if not tail or (end := _iso_ts(tail[0])) is None or not summaries.IDLE <= now - end <= summaries.LATE:
                    continue
                main = calls.file_calls(path)
            except OSError:
                continue
            ts, mid, model = tail
            use = (main.get(mid) or {}).get("use") or {}
            ctx = (use.get("in") or 0) + (use.get("cw") or 0) + (use.get("cr") or 0)
            account = api_calls.INDEX.account(mid)
            family = account_family(account) if account else ""
            if not account or family not in summaries.FAMILIES or summaries.stopped(family):
                continue
            tid = tab_id(path)
            tab = tabs.get(tid)
            now_account = tab.account if tab else accounts.get(tid)
            if now_account and base_account(now_account) != base_account(account):
                continue   # 换过号：缓存在原来的号上，不切号才做
            if tab and tab.model and tab.model.split("[")[0] != model.split("[")[0]:
                continue   # 网页上换过模型
            if not summaries.due(sid, ctx, len(main)):
                continue
            out.append({"sid": sid, "path": path, "mtime": st[1], "tid": tid, "owner": owner, "account": account,
                        "family": family, "model": model, "ctx": ctx, "calls": len(main), "end": ts})
    return out


SummaryDriver = None   # 写摘要的分叉驱动，第一次用时才建：Driver 要等启动时才从 chat 导入（见 summary_driver_class）


def summary_driver_class():
    """写摘要的分叉：选项和主会话一样（缓存才命得中），工具一律拒绝，一轮结束就收。"""
    global SummaryDriver
    import chat
    from claude_agent_sdk import ResultMessage

    class _SummaryDriver(chat.Driver):
        async def _guard(self, inp, tool_use_id, ctx):
            out = await super()._guard(inp, tool_use_id, ctx)
            if out.get("continue_") is False:
                return out
            if inp.get("hook_event_name") == "PreToolUse":
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": "现在是在后台写会话摘要，不能调用工具，请直接按要求写出来"}}
            return out

        async def _serve(self, client, retry):
            text = await self.inbox.get()
            self._new_turn()
            self.turn = True
            await client.query(text)
            async for msg in client.receive_response():
                self._handle(client, msg)
                if isinstance(msg, ResultMessage):
                    self.task_result = {"result": msg.result, "usage": msg.usage, "is_error": msg.is_error,
                                        "stop_reason": msg.stop_reason}
                    return None
            raise RuntimeError("运行结束前没有收到结果标记")

    SummaryDriver = _SummaryDriver
    return SummaryDriver


def summary_claim(fork: str, me: Who, base_sid: str):
    """分叉出来的会话记在原会话主人名下（照常计费），标成工作流：会话列表、搜索、回忆都不显示它。"""
    users.claim(fork, me, [])
    with users._lock:
        if fork in users.owners:
            users.owners[fork]["workflow"] = {"summary": base_sid}
            users._save(users.OWNERS_FILE, users.owners)


async def summary_run(c: dict):
    import chat
    from dataclasses import replace
    sid, d, summary, why, usage, fork = c["sid"], None, None, "", None, ""
    extra = {"account": c["account"], "model": c["model"], "ctx": c["ctx"]}
    try:
        me = users.Who(c["owner"], c["owner"] in users.ADMINS)
        tab = tabs.get(c["tid"])
        account = c["account"]
        model = launch_model(account, None, tab.model if tab else None)
        effort = launch_effort((efforts.get(c["tid"]) or {}).get("effort"), account, None, model)
        mode = run_mode(account, modes.get(c["tid"], DEFAULT_MODE))
        cwd = await asyncio.to_thread(read_cwd, c["path"], c["mtime"])
        if not cwd:
            raise RuntimeError("会话记录里没有工作目录")
        await asyncio.to_thread(check_paid, me, sid, account)
        if block := await asyncio.to_thread(limit_block, c["path"], c["owner"], account, model,
                                            len(summaries.PROMPT.encode())):
            raise RuntimeError(block)
        fork = str(uuid.uuid4())
        extra["fork"] = fork
        await asyncio.to_thread(summary_claim, fork, me, sid)
        d = (SummaryDriver or summary_driver_class())(fork, cwd, False, account, mode=mode, model=model, effort=effort)
        # 建好以后、事件循环还没跑到它的任务之前换成分叉：从主会话读历史，系统提示词照主会话的拼（CLI 续接时其实沿用记录里第一次的）
        d.options = replace(d.options, resume=sid, fork_session=True, session_id=fork,
                            system_prompt={"type": "preset", "preset": "claude_code",
                                           "append": chat.session_prompt(sid, False)})
        d.send(summaries.PROMPT)
        try:
            await asyncio.wait_for(asyncio.shield(d.task), summaries.TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(f"超过 {summaries.TIMEOUT} 秒没写完") from None
        result = getattr(d, "task_result", None)
        if d.blocked or d.error:
            raise RuntimeError(d.blocked or d.error)
        if not result:
            raise RuntimeError("没有拿到结果")
        usage = result.get("usage")
        if result.get("is_error"):
            raise RuntimeError(f"回复出错（{result.get('stop_reason')}）")
        text = result.get("result") or ""
        try:
            summary = summaries.parse(text)
        except ValueError as e:   # 回复开头一起记下：只记「没有长摘要」的话查不出是什么回复
            raise RuntimeError(f"{e}（回复开头：{text[:200] or '空的'}）") from None
    except Exception as e:
        why = str(getattr(e, "detail", e))
    finally:
        if d:
            if not d.task.done():
                d.task.cancel()
            await asyncio.gather(d.task, return_exceptions=True)
            try:   # 和网页、工作流一样按真实会话记录记调用是哪个号发的，账照常算
                if path := await asyncio.to_thread(calls.find, fork):
                    await asyncio.to_thread(api_calls.record, path, d.account, "web")
            except Exception as e:
                why = why or f"调用归属收尾失败：{e}"
        hit = summaries.done(sid, c["family"], summary, why, usage, extra)
        named = naming.propose(sid, summary["name"]) if summary else ""
        rate = f"，缓存命中 {hit:.0%}" if hit is not None else ""
        if summary:
            print(f"会话 {sid} 写好了摘要（{c['account']}，上下文 {c['ctx'] // 1000}K{rate}）"
                  + (f"，名字没起：{named}" if named else ""), flush=True)
        else:
            print(f"会话 {sid} 的摘要没写成（{c['account']}{rate}）：{why}", flush=True)
        if summaries.stopped(c["family"]):
            print(f"{c['family']} 这一家的号连着 {summaries.BAD_LIMIT} 次没命中缓存，会话摘要在这家的号上先停做，重启后重新开始", flush=True)
        summary_running.discard(sid)


async def summary_loop():
    while True:
        await asyncio.sleep(SUMMARY_GAP)
        try:
            found = await asyncio.to_thread(summary_candidates, time.time())
        except Exception as e:
            print(f"会话摘要清点出错：{type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
            continue
        for c in found:
            if len(summary_running) >= summaries.AT_ONCE:
                break
            summaries.begin(c["sid"], c["ctx"], c["calls"], c["end"])
            summary_running.add(c["sid"])
            t = asyncio.create_task(summary_run(c))
            summary_tasks.add(t)
            t.add_done_callback(summary_tasks.discard)


@app.get("/api/tabs/{tid}/summary")
def session_summary(tid: str, me: Who = Depends(who)):
    """会话信息里显示的摘要（短的在前，点开看长的）；还没有是 null。前端把 404 当成旧后端没有这个接口，所以别的情况不回 404。"""
    if tid not in tid_sid:
        raise HTTPException(409, "服务器已重启，尚未载入该会话：请重新打开该会话后重试")
    check_see(me, tid_sid[tid])
    s = summaries.get(tid_sid[tid])
    return {"summary": {k: s.get(k) for k in ("short", "long", "at")} if s else None}


class TagsBody(BaseModel):
    tags: list[str]


@app.post("/api/sessions/{tid}/tags")
def set_tags(tid: str, body: TagsBody, me: Who = Depends(who)):
    """给会话打标签。按会话编号来，会话不用先打开；只动 session_tags.json，只读模式也能改。"""
    if not re.fullmatch(r"[0-9a-f]{10}", tid):
        raise HTTPException(400, f"会话编号无效：{tid}")
    if tid not in tid_sid:   # 不知道是哪个会话，就没法确认看不看得到；服务器重启后还没列出过它才会这样
        raise HTTPException(404, "服务器已重启，尚未载入该会话：请刷新全部会话或重新打开该会话后重试")
    check_see(me, tid_sid[tid])
    check_alive(tid_sid[tid])
    if why := users.use_why(me, tid_sid[tid]):
        raise HTTPException(403, why)
    return {"id": tid, "tags": remember_tags(tid, body.tags)}


# 分享链接：拿到链接的人先过网页密码、登录，点开就在自己的查看器里打开这个会话，和公开会话一样只能看、不能发话
# （users.add_link_viewer）。只存随机凭证的散列；会话转了主人、被删除时即使旧链接仍在也不再有效。
PUBLIC_SHARES_FILE = DATA / "public_shares.json"
public_shares: dict[str, dict] = users._load(PUBLIC_SHARES_FILE, {})
PUBLIC_TOKEN = re.compile(r"[A-Za-z0-9_-]{40,80}\Z")


def share_manager(me: Who, sid: str):
    if not me.name or (not me.admin and me.name != users.owner_of(sid)):
        raise HTTPException(403, "仅会话主人和查看器账号可分享或撤销分享")
    if (users._rec(sid) or {}).get("environment"):   # 和 users.set_visible 一个规矩
        raise HTTPException(400, "独立环境里的会话只供本人使用，不能分享给其他用户")


def share_record(token: str) -> tuple[str, dict]:
    if not PUBLIC_TOKEN.fullmatch(token):
        raise HTTPException(404, "分享链接不存在或已撤销")
    rec = public_shares.get(hashlib.sha256(token.encode()).hexdigest())
    if not isinstance(rec, dict) or not users.SID_RE.fullmatch(rec.get("sid", "")):
        raise HTTPException(404, "分享链接不存在或已撤销")
    sid = rec["sid"]
    if users.is_deleted(sid) or users.owner_of(sid) != rec.get("owner"):
        raise HTTPException(404, "分享链接不存在或已撤销")
    return sid, rec


@app.post("/api/tabs/{tid}/public-share")
def create_public_share(tid: str, request: Request, me: Who = Depends(who)):
    tab = seen_tab(tid, me)
    share_manager(me, tab.sid)
    token = secrets.token_urlsafe(36)
    with users._lock:
        public_shares[hashlib.sha256(token.encode()).hexdigest()] = {"sid": tab.sid, "owner": users.owner_of(tab.sid)}
        try:
            users._save(PUBLIC_SHARES_FILE, public_shares)
        except HTTPException:
            public_shares.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            raise
    base = (SHARE_URL or str(request.base_url)).rstrip("/")
    return {"url": f"{base}/share/{token}"}


@app.delete("/api/tabs/{tid}/public-share")
def revoke_public_share(tid: str, me: Who = Depends(who)):
    tab = seen_tab(tid, me)
    share_manager(me, tab.sid)
    with users._lock:
        removed = {k: v for k, v in public_shares.items() if v.get("sid") == tab.sid}
        for k in removed:
            del public_shares[k]
        try:
            users._save(PUBLIC_SHARES_FILE, public_shares)
        except HTTPException:
            public_shares.update(removed)
            raise
    users.clear_link_viewers(tab.sid)
    deny_hidden(tab.sid)   # 靠链接正开着它的标签页当场收到「没有权限」
    return {"revoked": len(removed)}


@app.get("/share/{token}")
def share_link(token: str):
    """分享链接：转进查看器，登录以后由 app.js 的 openShared 凭它打开（下面的 open_share）"""
    return RedirectResponse(f"/#share={token}", status_code=303)


@app.post("/api/share/{token}")
def open_share(token: str, me: Who = Depends(who)):
    """记下这个人能看分享的会话，交回会话编号，前端照常打开"""
    if not me.name:
        raise HTTPException(403, "请登录后打开分享的会话")
    sid, _ = share_record(token)
    users.add_link_viewer(sid, me.name)
    return {"sid": sid}


class ShareBody(BaseModel):
    visible: str | list[str]   # "public"，或者一串用户名


@app.post("/api/tabs/{tid}/share")
def set_share(tid: str, body: ShareBody, me: Who = Depends(who)):
    """改会话的可见范围（主人和查看器账号）。从此看不到它的人，正开着的标签页当场收到「没有权限」。"""
    tab = seen_tab(tid, me)
    users.set_visible(me, tab.sid, body.visible)
    deny_hidden(tab.sid)
    publish(tab)
    return tab.brief()


class OwnerBody(BaseModel):
    owner: str | None = None   # 空就是清掉主人，变回无主


class WorkDevicesBody(BaseModel):
    off: list[str]   # 这个会话取消勾选的工作设备（整份替换）


@app.post("/api/tabs/{tid}/devices", dependencies=[Depends(full_only)])
def set_work_devices(tid: str, body: WorkDevicesBody, me: Who = Depends(who)):
    """改会话的工作设备（workdev.py）：只有会话主人能改，设备是他名下的。门卫当场按新的放行，Claude 在下一句话收到新清单。"""
    user = device_user(me)
    tab = seen_tab(tid, me)
    if users.owner_of(tab.sid) != user:
        raise HTTPException(403, "仅会话主人可修改工作设备")
    try:
        workdev.change(tab.sid, user, body.off, sshgw.targets(user))
    except workdev.Bad as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"工作设备保存失败：{e}")
    publish(tab)
    return tab.brief()


@app.post("/api/tabs/{tid}/owner")
def set_owner(tid: str, body: OwnerBody, me: Who = Depends(who)):
    """查看器账号改会话的主人。"""
    tab = seen_tab(tid, me)
    users.set_owner(me, tab.sid, (body.owner or "").strip() or None)
    deny_hidden(tab.sid)
    publish(tab)
    return tab.brief()


class DeletedBody(BaseModel):
    deleted: bool   # True 删除，False 恢复


@app.post("/api/sessions/{tid}/deleted")
def set_deleted(tid: str, body: DeletedBody, me: Who = Depends(who)):
    """删除或恢复会话（标签页右键删，全部会话最下面的「已删除」里恢复）。只在查看器里记一笔（users.deleted），
    会话文件不动，所以只读模式也能做。按标签编号来：恢复时会话没打开，靠列出卡片时记下的 tid_sid 找会话编号。
    Claude 正在干活时不让删（删了就没人看得到它在干什么）；空闲连着的 CLI 收掉。
    同一个会话经不同路径开着的标签页、别的设备上开着的，当场推一条 deleted 让它们关掉。"""
    if not re.fullmatch(r"[0-9a-f]{10}", tid):
        raise HTTPException(400, f"会话编号无效：{tid}")
    sid = tabs[tid].sid if tid in tabs else tid_sid.get(tid)
    if not sid:
        raise HTTPException(404, "服务器已重启，尚未载入该会话：请刷新全部会话后重试")
    check_see(me, sid)
    if not body.deleted:
        users.set_deleted(me, sid, False)
        return {"id": tid, "deleted": False}
    same = [(t, tab) for t, tab in tabs.items() if tab.sid == sid]
    if any(tab.driver and tab.driver.running for _, tab in same):
        raise HTTPException(409, "Claude 正在运行，请在本轮结束后删除")
    users.set_deleted(me, sid, True)
    why = users.deleted_why(sid)
    for t, tab in same:
        tab.drop_driver()
        for queue in subs.pop(t, ()):
            send(queue, json.dumps({"id": t, "deleted": why}, ensure_ascii=False))
    return {"id": tid, "deleted": True}


# 状态栏脚本（quota_statusline.py）按号存下的最新额度：quota/<号>.json
QUOTA_DIR = DATA / "quota"
LIMIT_KEYS = {"five_hour": "five_hour", "seven_day": "seven_day"}   # 限额事件的种类 → 快照里的键

# 账号血条上红的那段：最近 RECENT_SECS 秒用掉的 5 小时额度，拿额度历史里的读数前后相减。
# 历史文件只会追加，所以记着读到哪儿、每次只读新写的行，内存里只留最近 6 小时的 5 小时窗口读数
RECENT_SECS = 1800
FIVE_SECS = 5 * 3600
history_five: list[tuple[float, str, float, float]] = []   # (ts, 号, 用量百分比, 重置时刻)，按文件里的先后
history_pos = 0
history_lock = threading.Lock()   # quota() 在线程池里跑，几个请求可能同时来读


def read_history(now: float):
    global history_pos
    path = DATA / "quota_history.jsonl"
    try:
        with path.open("rb") as f:
            if path.stat().st_size < history_pos:   # 文件被换过，从头读
                history_pos = 0
                history_five.clear()
            f.seek(history_pos)
            chunk = f.read()
    except OSError:
        return
    end = chunk.rfind(b"\n") + 1   # 最后一行可能还没写完，留到下次
    history_pos += end
    for line in chunk[:end].splitlines():
        with contextlib.suppress(ValueError, KeyError, TypeError):
            r = json.loads(line)
            w = r.get("five_hour") or {}
            if w.get("used_percentage") is not None and w.get("resets_at"):
                history_five.append((float(r["ts"]), r["account"], float(w["used_percentage"]), float(w["resets_at"])))
    keep = now - FIVE_SECS - 3600
    while history_five and history_five[0][0] < keep:
        history_five.pop(0)


def five_recent(account: str, cur: dict, now: float) -> float | None:
    """这个号最近 RECENT_SECS 秒用掉的 5 小时额度（百分点）= 现在的读数 − 这段开始那一刻的用量。
    血条上红的那段就是它，任何时候都要给出这个数，不能因为手上的读数不凑巧就不画。
    开始那一刻的用量按那一刻前后最近的两个读数（同一个窗口里的）连线估出来；开窗那一刻用量是 0，
    所以前面总有一个点（这段之前没有读数就用开窗那一刻），后面至少有现在这次读数。
    窗口是这段时间里才开的，整窗用量都算。只有现在这次读数本身缺东西时才给 None。"""
    if cur.get("used_percentage") is None or not cur.get("resets_at"):
        return None
    used, reset, cutoff = float(cur["used_percentage"]), float(cur["resets_at"]), now - RECENT_SECS
    if (cur.get("ts") or 0) <= cutoff:   # 这段时间里一次读数都没有，就是没用过
        return 0.0
    start = reset - FIVE_SECS
    if start >= cutoff:
        return used
    (t0, u0), (t1, u1) = (start, 0.0), (float(cur["ts"]), used)
    # 状态栏和 /usage 报的重置时刻会差一分钟左右，差 10 分钟以内都算同一个窗口
    for ts, a, u, r in history_five:
        if a != account or abs(r - reset) > 600:
            continue
        if t0 <= ts <= cutoff:
            t0, u0 = ts, u
        elif cutoff < ts < t1:
            t1, u1 = ts, u
    at_cutoff = u0 + (u1 - u0) * (cutoff - t0) / (t1 - t0) if t1 > t0 else u0
    return min(max(used - at_cutoff, 0.0), used)


@app.get("/api/quota")
def get_quota(limit: int = 50, me: Who = Depends(who)):
    """给这个人看的额度：别人预定的部分算成已经用掉（five_hour.hold、seven_day.hold），自己的预定在 mine 里画蓝条
    （见 reserve.decorate）。其余同 quota。"""
    q = quota(limit)
    reserve.decorate(q["accounts"], me.name, time.time())
    for name, entry in API_ENTRIES.items():
        q["accounts"][name] = q["accounts"].get(entry["account"], {})
    q["expires"] = {**q["expires"], **{name: EXPIRES[e["account"]] for name, e in API_ENTRIES.items()
                                        if e["account"] in EXPIRES}}
    return q


def quota(limit: int = 50):
    """各号最新的 5 小时和本周额度，外加最近的限额事件记录（最新的在前）。

    accounts[号] = {five_hour: {used_percentage, resets_at, ts, recent}, seven_day: {...}}，ts 是这一项记下的时间，
    fiveInWeek 是 5 小时窗口用满约合本周的多少（FIVE_IN_WEEK），
    recent 是最近 30 分钟用掉的 5 小时额度（百分点，不知道是 null，见 five_recent）。
    主要来自状态栏脚本（终端里每次收到回复都会更新）；网页对话收到的限额事件要是更新，就用它盖上去。"""
    accounts: dict[str, dict] = {}
    for p in sorted(QUOTA_DIR.glob("*.json")) if QUOTA_DIR.is_dir() else []:
        try:
            snap = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        accounts[snap.get("account") or p.stem] = {
            k: {**snap[k], "ts": snap.get("ts")} for k in LIMIT_KEYS.values() if isinstance(snap.get(k), dict)}
    for e in quota_log:   # 按时间先后，后来的盖掉先来的
        key = LIMIT_KEYS.get(e.get("type") or "")
        if not key or e.get("utilization") is None:
            continue
        cur = accounts.setdefault(e["account"], {}).get(key)
        if not cur or (cur.get("ts") or 0) < e["ts"]:
            accounts[e["account"]][key] = {"used_percentage": e["utilization"] * 100,
                                           "resets_at": e.get("resetsAt"), "ts": e["ts"]}
    now = time.time()
    with history_lock:
        read_history(now)
        for name, snap in accounts.items():
            if snap.get("five_hour"):
                snap["five_hour"] = {**snap["five_hour"], "recent": five_recent(name, snap["five_hour"], now)}
    # fiveInWeek：前端按它把本周快用完的号的 5 小时血条砍短；GPT 号各有各的，写在 fiveInWeekOf 里。
    # weekOnly：没有 5 小时限额的号，血条画本周额度。
    # expires：号 → 到期时刻（unix 秒）。号在本周重置之前就到期时，前端的「in 2d5h」改按到期时刻倒数，
    # 不然用户看到的是一整周，实际到期以后就用不了了
    # API 号（DeepSeek）没有额度窗口：balances 是最近一次查到的平台余额（usage_probe.save_balance，号 → {total 元, available, ts…}），
    # peak 是此刻这个号上的调用按高峰还是空闲价算、到几点换（peak.py），账号面板上挂「高峰」「空闲半价」
    from usage_probe import BALANCE_DIR
    balances = {}
    for a in API:
        with contextlib.suppress(OSError, ValueError):
            balances[a] = json.loads((BALANCE_DIR / f"{a}.json").read_text())
    return {"accounts": accounts, "log": quota_log[::-1][:limit], "fiveInWeek": FIVE_IN_WEEK,
            "fiveInWeekOf": {a: five_in_week(a) for a in GPT}, "weekOnly": sorted(WEEK_ONLY), "expires": EXPIRES,
            "balances": balances, "peak": {a: {"peak": peak.is_peak(now, m), "until": peak.next_switch(now, m)}
                                           for a in API for m in [api_models(a)[0]] if peak.priced(m)}}


quota_lock = asyncio.Lock()
probe_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)   # 同一个号同时只跑一个 /usage
last_probe: dict[str, float] = {}   # 号 → 上次查 /usage 的时间
probe_waiting: set[str] = set()     # 已经排好一次延后自动查的号
AUTO_PROBE_GAP = 180   # 自动查同一个号至少隔这么久


async def probe_account(account: str, source: str):
    if account in REMOTE or account in REMOTE_GPT:
        return None   # 远端回环和远端 GPT 的额度不在这边查，要看就去那边看
    account = base_account(account)
    from usage_probe import probe
    async with probe_locks[account]:
        last_probe[account] = time.time()
        return await probe(account, QUOTA_DIR, source)


def after_turn(d: Driver):
    """网页对话一轮结束：按这个驱动的号记下这一轮的每次调用，再自动查一次这个号的限额百分比；
    记下这个会话干完的时刻（侧边栏的未读，见 turn_done）。"""
    tab = next((t for t in tabs.values() if t.driver is d), None)
    sid = tab.sid if tab else d.session_id
    turn_done[sid] = time.time()
    save_seen()   # 写不进去由写盘线程在服务输出里说；记不下来只是重启后侧边栏少一颗未读点
    path = (tab.session.path if tab else None) or calls.find(d.session_id)
    if path:
        asyncio.create_task(asyncio.to_thread(api_calls.record, path, d.account, api_calls.web_source(d.account)))
    asyncio.create_task(auto_probe(d.account))


def note_mid(d: Driver, mid: str):
    """网页对话的 CLI 一发出请求，就记下这次调用是驱动的号发的（见 api_calls.note）。"""
    async def run():
        try:
            await asyncio.to_thread(api_calls.note, mid, d.account, api_calls.web_source(d.account), d.session_id)
        except Exception as e:
            print(f"记回复编号 {mid} 是 {d.account} 发的失败：{e}", flush=True)
    asyncio.create_task(run())


async def auto_probe(account: str):
    """离上次查不到 AUTO_PROBE_GAP 秒就等到够了再查，这期间又结束的轮次共用这一次。"""
    if account in BANNED or account in REMOTE or account in REMOTE_GPT or account in probe_waiting:
        return
    probe_waiting.add(account)
    try:
        await asyncio.sleep(max(0, last_probe.get(account, 0) + AUTO_PROBE_GAP - time.time()))
        await probe_account(account, "usage-auto")
    except Exception as e:
        print(f"自动查 {account} 的限额百分比失败：{e}", flush=True)
    finally:
        probe_waiting.discard(account)


# 号 → 上游上次报限额的时刻（chat.Driver._upstream_limit）。上游刚拒过的号，读数说还有余量也不可信（那次拒绝就是在读数说还有的时候发生的），
# 这么久之内不当自动切号的目标：不然 A 被拒切到 B、B 被拒又按 A 的读数切回 A，能一直来回空转。和 AUTO_GO_MIN_GAP 同一个考虑
limit_hits: dict[str, float] = {}
limit_until: dict[str, float] = {}
LIMIT_AVOID = 1800      # 一次最多停这么久（上游给的再长也截到这里）
LIMIT_UNKNOWN = 0       # 上游没说停多久就不停：号可能只是被瞬时拒了一下，锁住它比空转更糟


def cool(account: str, wait: float | None = None):
    """号被上游拒了：停一段。上游说了停多久就按它说的（retry-after），没说就按 LIMIT_UNKNOWN。
    网页和对外接口共用这一处，不能一边半小时、一边按上游。"""
    account = base_account(account)
    now = time.time()
    limit_hits[account] = now          # 只作记录，查“上次被拒是什么时候”
    limit_until[account] = now + min(LIMIT_UNKNOWN if wait is None else max(0.0, wait), LIMIT_AVOID)


def cooling_until(account: str) -> float:
    """这个号停到什么时候。"""
    return limit_until.get(base_account(account), 0.0)


def upstream_limit(d: Driver):
    """上游报了限额：记下时刻，当场重查这个号的额度（不等 AUTO_PROBE_GAP，/usage 不花额度）。
    别的会话挑号、这个会话额度恢复后自动继续（resets_of）都要按新读数来，上一次读数正是没看出它已经满了的那个"""
    cool(d.account)   # CLI 报的限额查不到要停多久，按 LIMIT_AVOID

    async def run():
        try:
            await probe_account(d.account, "usage-limit")   # 正在查的话等它查完再查一次：那次可能是在被拒之前开始的
        except Exception as e:
            print(f"上游报了限额，重查 {d.account} 的额度失败：{e}", flush=True)
    asyncio.create_task(run())


@app.post("/api/quota/refresh", dependencies=[Depends(full_only)])
async def refresh_quota(me: Who = Depends(who)):
    """手动刷新额度：能用的号同时各开一次交互界面跑 /usage（不发消息、不花额度），结果记进 quota/。
    一个号大约十秒；同一时间只跑一次。"""
    if quota_lock.locked():
        raise HTTPException(409, "正在刷新，请稍候")
    async with quota_lock:
        names = [a for a in ACCOUNTS if a not in BANNED and a not in API_ENTRIES
                 and a not in REMOTE and a not in REMOTE_GPT]
        got = await asyncio.gather(*(probe_account(a, "usage") for a in names), return_exceptions=True)
    errors = {a: str(r) for a, r in zip(names, got) if isinstance(r, BaseException)}
    return {**get_quota(me=me), "errors": errors}


# ---------- 预定额度和预测拦截（算法在 reserve.py） ----------

SETTLE_SECS = 2   # 后台循环隔多久把新调用分给预定一次


def can_pay(payer: str | None, account: str) -> bool:
    """用绿的要付钱：限量号在多用户实例里要余额还没透支到 OVERDRAFT（和 users.paid_why 同一条线）。
    号主用自己的号不看余额：花的钱又流回他自己。"""
    account = base_account(account)
    if account in REMOTE and REMOTE[account].get('billing') == 'upstream':
        return payer is not None and payer == REMOTE[account].get('user')
    if account not in PAID or not users.MULTI or owns(payer, account):
        return True
    return payer is not None and users.cached_balance(payer) > users.OVERDRAFT


def live_uses() -> dict[str, dict]:
    """正在跑的网页对话从流事件里当场记下的最近几次请求（会话记录里可能还没写进去）：回复编号 → {model, use}。"""
    out = {}
    if Driver is None:
        return out
    import chat
    # 尚未挂到标签页上的新会话、工作流任务也要算，不能等落盘后才看见用量。
    for d in list(chat.LIVE):
        out.update({v["mid"]: v for v in list(d.last_use.values()) if v.get("mid")})
    return out


def estimated(account: str, snap: dict, now: float) -> tuple[float, float]:
    """读数之后这个号又用了多少（所有人的调用按单价估）：(5 小时百分点, 本周百分点)。
    读数要隔一阵才查一次，拦截和下单都要把这段补上；窗口已经过了重置时刻的，从重置那一刻算起。"""
    out, live = [], live_uses()
    for key in ("five_hour", "seven_day"):
        w = snap.get(key)
        if not w:
            out.append(0.0)
            continue
        t = w.get("ts") or 0
        if w.get("resets_at") and w["resets_at"] <= now:
            t = max(t, w["resets_at"])
        out.append(reserve.spent_since(account, t, live))
    return out[0], out[1] * five_in_week(account)


def room_for(payer: str | None, account: str, now: float, pay: bool | None = None) -> tuple[dict | None, dict]:
    # API用量与在途预算一起切换，预测必须拿到同一个时刻的快照。
    with reserve._lock:
        return _room_for(payer, account, now, pay)


def _room_for(payer: str | None, account: str, now: float, pay: bool | None = None) -> tuple[dict | None, dict]:
    """这个人此刻在这个号上还能用多少（reserve.split 拆好的几段，按读数加上之后的估算，别人的预定算用掉），和用到的读数。
    从没查到过这个号的额度是 None：不知道就没法预测（界面上这个号本来就写着「–」）。"""
    account = base_account(account)
    snap = quota(limit=0)["accounts"].get(account) or {}
    if not snap.get("five_hour") and not snap.get("seven_day"):
        return None, snap
    view = {k: dict(v) for k, v in snap.items() if isinstance(v, dict)}
    reserve.decorate({account: view}, payer, now)
    est5, estW = estimated(account, snap, now)
    pending = api_gateway.held(account)
    five = reserve.rest(view.get("five_hour"), now, est5 + pending)
    week = reserve.rest(view.get("seven_day"), now, estW + pending * five_in_week(account))
    # 没有 5 小时限额的号（WEEK_ONLY）只受本周额度限制，5 小时这层当成无穷大
    F = math.inf if account in WEEK_ONLY else 100.0 if five is None else five
    sp = reserve.split(F, week, view.get("mine"), can_pay(payer, account) if pay is None else pay, k=five_in_week(account))
    return sp, snap


def green_left(user: str, account: str) -> float:
    """这个人此刻在这个号上还剩多少绿的（5 小时百分点，付不起钱就是 0）：保底的预定只在它用完时才动，特惠免掉的那几成要它来出。
    不知道额度就当还有，而且足够（特惠按折扣全免，reserve._settle）。"""
    sp, _ = room_for(user, account, time.time())
    return sp["g"] if sp else math.inf


def en_time(ts: float, now: float) -> str:
    """照官方限额提示的写法：当天的「3:40pm」「3pm」，不是当天的带上「Oct 14, 」。"""
    d = datetime.fromtimestamp(ts)
    ap, h = ("am" if d.hour < 12 else "pm"), d.hour % 12 or 12
    t = f"{h}:{d.minute:02d}{ap}" if d.minute else f"{h}{ap}"
    return t if d.date() == datetime.fromtimestamp(now).date() else f"{d.strftime('%b')} {d.day}, {t}"


def pct(x: float) -> str:
    """给人看的百分点：小的多留一位，免得「要用 0.1%、只能用 0.1%」看不出差在哪。"""
    return f"{x:.2f}%" if x < 10 else f"{x:.1f}%"


def limit_head(snap: dict, sp: dict, now: float) -> str:
    """仿照官方写的到达限额那一行的开头。"""
    five, week = snap.get("five_hour") or {}, snap.get("seven_day") or {}
    if sp["weekBound"] and week.get("resets_at"):
        return f"Weekly limit reached ∙ resets {en_time(week['resets_at'], now)}"
    if five.get("resets_at") and five["resets_at"] > now:
        return f"5-hour limit reached ∙ resets {en_time(five['resets_at'], now)}"
    return "5-hour limit reached"


def limit_block(path, payer: str | None, account: str, model: str | None, new_bytes: int = 0, images: int = 0,
                call_account: dict | None = None, last: dict | None = None, tool: bool = False,
                extra: float = 0.0, extra_how: str = "", out_tokens: float | None = None) -> str:
    """预测拦截：预计下一次请求会把这个人在这个号上能用的额度（绿的加自己的蓝，5 小时和本周都算，
    别人的预定在他看来就是用掉了）扣到 0 以下，就不让它发出去，返回仿照官方写的到达限额那一行；能发是空字符串。
    不留余量、不问、没有强制继续：等重置，或者别的条件解除（发了额度、换了号、预定的窗口到了）才能接着用。
    最要防的是带着长上下文换号（整段重新写入缓存）和一次塞进很长的输入；别人预定的额度也靠它保住。
    extra 是同时还要花的（5 小时百分点，比如派出去的子代理的第一次请求），extra_how 说它是什么；out_tokens 指定输出多少（压缩用）。"""
    account = base_account(account)
    now = time.time()
    sp, snap = room_for(payer, account, now)
    if sp is None:
        return ""
    cost, how = reserve.next_cost(path, account, model, new_bytes, images, call_account, last, tool, out_tokens)
    cost += extra
    if cost <= sp["total"]:
        return ""
    # 没有 5 小时限额的号，换成本周额度的百分点给人看（算的时候用的是计价单位，见 instance.py 的 week_only）
    # 这段说明会话里的 AI 也看得到（工具被拒的理由），所以不提查看器、号和查看器账号，只说额度
    # （不能有让 AI 觉察到是共用环境、有人管着的提醒）
    k, of = (five_in_week(account), "本周额度") if account in WEEK_ONLY else (1.0, "5 小时额度")
    detail = (f"预测拦截：下一次请求预计要用{of}的 {pct(cost * k)}（{how}{extra_how}），"
              f"此刻只能用 {pct(sp['total'] * k)}")
    if not can_pay(payer, account):
        full, _ = room_for(payer, account, now, pay=True)
        if full and cost <= full["total"]:
            return f"余额不足，预定额度也不足以支付本次请求（{detail}）"
    return f"{limit_head(snap, sp, now)}（{detail}）"


COMPACT_OUT = 20000   # 压缩上下文那次请求大约输出多少 token（写摘要；实测一次 20673）


def guard(d: Driver, inp: dict) -> str:
    """网页对话里 CLI 发请求之前的预测拦截（chat.Driver._guard 调它），按钩子分：
    - 工具执行后 / 执行出错：下一次请求带上这次工具结果（出错时是出错信息）；
    - 工具执行前：被拒绝的工具不会再有执行后的钩子，下一次请求在这里就要核，结果多大还不知道，按 0 算；
      派子代理（Agent）还要加上子代理的第一次请求（系统提示词加上派给它的任务说明，整段算缓存写入）；
    - 开新的一轮（用户的话、后台任务完成的通知）：下一次请求带上这段话；
    - 压缩之前：整段上下文加上一份摘要的输出。
    子代理里的钩子按子代理自己那份记录估。"""
    ev = inp.get("hook_event_name") or ""
    main = d.environment.path(inp.get("transcript_path") or ".") if d.environment else hostfs.Path(inp.get("transcript_path") or "")
    path = main
    if aid := inp.get("agent_id"):
        sub = main.with_suffix("") / "subagents" / f"agent-{aid}.jsonl"
        path = sub if sub.exists() else main
    if not path.is_file():
        path = calls.find(d.session_id)
    tab = next((t for t in list(tabs.values()) if t.driver is d), None)
    accs = tab.call_accounts() if tab else dict.fromkeys(d.mids, d.account)
    # 主对话的上一次请求从驱动当场记的流事件里拿（这时 CLI 多半还没把它写进记录）；子代理对不上号，按它自己的记录
    last = None if inp.get("agent_id") else d.last_use.get(None)
    payer = users.owner_of(d.session_id)
    if ev in ("PostToolUse", "PostToolUseFailure"):
        got = inp.get("tool_response") if ev == "PostToolUse" else inp.get("error")
        nbytes = len(json.dumps(got, ensure_ascii=False, default=str).encode())
        return limit_block(path, payer, d.account, d.model, nbytes, 0, accs, last, tool=True)
    if ev == "PreToolUse":
        extra, extra_how = 0.0, ""
        if inp.get("tool_name") in ("Agent", "Task"):
            ti = inp.get("tool_input") or {}
            model = ti.get("model") or (last or {}).get("model") or d.model
            extra, _ = reserve.next_cost(None, d.account, model, len(json.dumps(ti, ensure_ascii=False).encode()))
            extra_how = f"，再加上派出去的子代理的第一次请求约 {pct(extra)}"
        return limit_block(path, payer, d.account, d.model, 0, 0, accs, last, tool=True, extra=extra, extra_how=extra_how)
    if ev == "UserPromptSubmit":
        return limit_block(path, payer, d.account, d.model, len((inp.get("prompt") or "").encode()), 0, accs, last)
    if ev == "PreCompact":
        return limit_block(path, payer, d.account, d.model, 0, 0, accs, last, out_tokens=COMPACT_OUT)
    return ""


def check(d: Driver) -> str:
    """一次请求刚发出（chat.Driver._check 调它）：此刻这个人在这个号上已经没有能用的了，就拦下，后面的一次都不许再发。
    钩子没问到就发出去的请求最多漏这一次。"""
    if getattr(d, "entry_account", d.account) in API_ENTRIES:
        return ""   # 网关已在发送前为整笔请求预留预算，不用自己的预留再把自己截断
    now = time.time()
    sp, snap = room_for(users.owner_of(d.session_id), d.account, now)
    if sp is None or sp["total"] > 0:
        return ""
    return f"{limit_head(snap, sp, now)}（当前已无可用额度，后续请求不再发送）"   # 不提查看器和号，见 limit_block


class ReserveBody(BaseModel):
    account: str
    front: bool = True        # 先用预定的（蓝条在绿条右边）；False 是当保底，最后才用（蓝条在左边）
    five: list[dict] = []     # 5 小时预定：[{start 窗口开始时刻（面板给的）, pct 订百分之几}]
    week: float = 0           # 周预定：本周额度的百分之几
    weeks: list[dict] = []    # 以后几周的周预定：[{start 那一周的开始时刻（面板给的）, pct 订那一周的百分之几}]


def reserve_ctx(me: Who, account: str, book: bool = False) -> tuple[float, dict, float, float]:
    """预定面板和下单共用的前置检查；book 为真是下单。"""
    account = base_account(account)
    if account not in ACCOUNTS or account in BANNED:
        raise HTTPException(400, f"{account} 当前不可用，无法预定")
    if not me.name:
        raise HTTPException(401, "请登录后预定")
    # 号主关了别人的预定（users.set_reserve_open）：别人打开面板、下单都在这里挡下，号主自己照常订。
    # 查看器账号照样能打开面板看这个号上的预定、撤销，只是也不能下新单（closed 告诉前端）。
    # 用 403 和别的出错分开，前端认出它就只写这一句，不写成「打开失败」
    if account in users.reserve_closed and not owns(me.name, account) and (book or not me.admin):
        raise HTTPException(403, f"{account} 暂不接受预定")
    now = time.time()
    snap = quota(limit=0)["accounts"].get(account) or {}
    est5, estW = estimated(account, snap, now)
    pending = api_gateway.held(account)
    return now, snap, est5 + pending, estW + pending * five_in_week(account)


@app.get("/api/reserve/{account}")
def reserve_plan(account: str, me: Who = Depends(who)):
    """预定面板：从当前窗口起本周每个窗口最多还能订多少、本周还能订多少、自己在这个号上的每一批、开窗服务活着没有。"""
    account = base_account(account)
    now, snap, est5, estW = reserve_ctx(me, account)
    # 不开放预定的号（NO_RESERVE，智谱这类订阅制 API 号也在里面）：号主的折扣进度条长在这个面板里，号主打开时不报错，
    # 只给折扣要用的几项（noReserve 告诉前端藏起预定部分）；别人打开照旧由 reserve.plan 报「不开放预定」
    if account in NO_RESERVE and owns(me.name, account):
        return {"account": account, "noReserve": True, "owner": True, "charge": account in PAID,
                "ownerPromo": reserve.owner_factor(account, now), "keeper": reserve.keeper_status(now)}
    try:
        p = reserve.plan(account, snap, me.name, now, est5, estW)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**p, "account": account, "charge": account in PAID, "price": rates.price_of(account),   # 收钱时乘的比例（GPT 号再打折）
            "mine": reserve.mine_list(account, me.name, now),
            # 这个号上所有人的每一批谁都看得到（血条上照旧看不出有人预定，点进面板才看详情）；
            # 别人的单只有查看器账号能逐批撤销（/api/admin/reserve/cancel），canCancel 告诉前端放不放撤销按钮
            "all": reserve.mine_list(account, None, now), "canCancel": bool(me.admin),
            # owner：看的人是这个号的号主，面板上有「接受别人预定」的开关（closed 是关着），「我的预定」每一批能自己撤销（/api/reserve/cancel）
            "owner": owns(me.name, account), "closed": account in users.reserve_closed,
            # ownerPromo：号主才给，这个号现在收原价的几成（号主自己定的折扣，reserve.owner_factor），面板上拖进度条改
            "ownerPromo": reserve.owner_factor(account, now) if owns(me.name, account) else None,
            "keeper": reserve.keeper_status(now), "estimated": {"five": est5, "week": estW}}


@app.post("/api/reserve", dependencies=[Depends(full_only)])
def make_reservation(body: ReserveBody, me: Who = Depends(who)):
    # 从读余量到落下预定之间不让API插入另一笔预算，避免同一份容量被重复出售。
    with reserve._lock:
        return _make_reservation(body, me)


def _make_reservation(body: ReserveBody, me: Who):
    """下一批预定，一次付清（限量号才收钱），返回这一批和刷新后的面板。"""
    body.account = base_account(body.account)
    now, snap, est5, estW = reserve_ctx(me, body.account, book=True)

    def check_money(cost: float):
        cost *= rates.price_of(body.account)   # 预定按单价算出的点数，收钱时和花费一样打折（账本里也这样算）
        # 号主订自己的号不看余额：付的钱马上又记进他的号上收入（users.earnings）
        if cost > 0 and users.MULTI and not owns(me.name, body.account):
            bal = users.ledger(me.name)["balance"]
            if bal - cost < users.OVERDRAFT:
                raise ValueError(f"余额不够：这一批要 {cost * reserve.FEN:.1f} 分，余额只有 {bal * reserve.FEN:.1f} 分")
    try:
        b = reserve.book(body.account, snap, me.name, now, body.five, body.week, body.front, est5, estW,
                         body.account in PAID, check_money, body.weeks)
    except ValueError as e:
        raise HTTPException(400, str(e))
    users.forget_balance(me.name)
    return {"batch": b, "plan": reserve_plan(body.account, me)}


class CancelBody(BaseModel):
    id: str


@app.post("/api/admin/reserve/cancel")
def cancel_reservation(body: CancelBody, me: Who = Depends(who)):
    """查看器账号撤销某人的一批预定：没用掉的那几成退钱，这一批作废、占着的额度还回去（见 reserve.cancel）。
    用户自己只能撤销自己在自己号上的单（/api/reserve/cancel）。返回这一批（带 cancel）、退回的钱（乘过收钱比例的点数）和刷新后的面板。"""
    users.need_admin(me)
    return do_cancel(body.id, me)


@app.post("/api/reserve/cancel", dependencies=[Depends(full_only)])
def cancel_own_reservation(body: CancelBody, me: Who = Depends(who)):
    """号主撤销自己在自己号上的一批预定：付的钱本来就流回号主自己，撤了是把占着的额度还给别人。
    别人的单、在别人号上下的单都撤不了，那些只有查看器账号能撤。退钱、作废同 /api/admin/reserve/cancel。"""
    with reserve._lock:
        b = next((x for x in reserve.batches if x["id"] == body.id), None)
    if not b:
        raise HTTPException(400, "该批预定不存在，可能已被撤销")
    if b["user"] != me.name or not owns(me.name, b["account"]):
        raise HTTPException(403, "仅可撤销您在自己账号上的预定")
    return do_cancel(body.id, me)


class OpenBody(BaseModel):
    open: bool


@app.post("/api/reserve/{account}/open", dependencies=[Depends(full_only)])
def set_reserve_open(account: str, body: OpenBody, me: Who = Depends(who)):
    """号主开关别人在这个号上下新的预定（users.set_reserve_open）。只回开关现在的状态，不连带算面板：
    额度没查到时面板算不出来，开关却已经存下了，不能报成没改成。"""
    if account not in ACCOUNTS:
        raise HTTPException(400, f"账号 {account} 不存在")
    account = base_account(account)
    users.set_reserve_open(me, account, body.open)
    print(f"预定：{me.name} {'打开' if body.open else '关掉'}了别人在 {account} 上的预定", flush=True)
    return {"account": account, "closed": account in users.reserve_closed}


class PromoBody(BaseModel):
    factor: float   # 收原价的几成，0～1


@app.post("/api/reserve/{account}/promo", dependencies=[Depends(full_only)])
def set_owner_promo(account: str, body: PromoBody, me: Who = Depends(who)):
    """号主定自己名下的号从现在起收原价的几成（reserve.set_owner_promo）：只管以后的调用，以前的账不动。
    只有号主自己能定，查看器账号也不能替别人定。只回现在的折扣。"""
    if account not in ACCOUNTS:
        raise HTTPException(400, f"账号 {account} 不存在")
    account = base_account(account)
    if not owns(me.name, account):
        raise HTTPException(403, f"仅 {account} 的号主可设置折扣")
    if not 0 <= body.factor <= 1:
        raise HTTPException(400, "折扣须在原价的 0%～100% 之间")
    f, now = round(body.factor, 2), time.time()
    if f != reserve.owner_factor(account, now):
        try:
            reserve.set_owner_promo(account, f, me.name, now)
        except OSError as e:
            raise HTTPException(500, f"折扣保存失败：{e}")
        print(f"折扣：{me.name} 把 {account} 改成收原价的 {f * 100:g}%", flush=True)
    return {"account": account, "ownerPromo": f}


def do_cancel(batch_id: str, me: Who) -> dict:
    """撤销一批预定（谁能撤由调用的接口先核好），写日志，返回这一批、退回的钱和刷新后的面板。"""
    try:
        b = reserve.cancel(batch_id, me.name, time.time())
    except ValueError as e:
        raise HTTPException(400, str(e))
    users.forget_balance(b["user"])
    users.forget_balance((users.OWNERS.get(b["account"]) or (None,))[0])   # 退的钱也从号主的号上收入里减掉
    refund = b["cancel"]["refund"] * rates.price_of(b["account"])
    print(f"预定：{me.name} 撤销了 {b['user']} 在 {b['account']} 上的一批预定 {b['id']}，"
          f"下单 {b['points']:.4f} 点、退回 {b['cancel']['refund']:.4f} 点（收钱时乘 {rates.price_of(b['account'])}）", flush=True)
    return {"batch": b, "refund": refund, "plan": reserve_plan(b["account"], me)}


@app.get("/api/tags")
def all_tags(me: Who = Depends(who)):
    """只列这个用户看得到的会话标签，私有标签和颜色键也不能泄露给别人。"""
    for d in project_dirs(me):
        if d.is_dir():
            for p in children(d, "*.jsonl"):
                tid_sid[tab_id(p)] = p.stem
    count = Counter(n for tid, names in tags.items()
                    if tid in tid_sid and users.can_see(me, tid_sid[tid])
                    for n in names)
    return {"tags": [n for n, _ in count.most_common()], "colors": {n: c for n, c in tag_colors.items() if n in count}}


class TagColorBody(BaseModel):
    tag: str
    color: int | None = None   # 色板里的第几种；不给就回到按名字自动配色


@app.post("/api/tags/color")
def set_tag_color(body: TagColorBody, me: Who = Depends(who)):
    """给标签挑颜色，只记在 tag_colors.json 里，只读模式也能改；匿名看分享的人不能改。"""
    if not me.name:
        raise HTTPException(401, "请登录后修改标签颜色")
    name = body.tag.strip()[:24]
    if not name:
        raise HTTPException(400, "标签名不能为空")
    if body.color is None:
        tag_colors.pop(name, None)
    elif 0 <= body.color < 16:
        tag_colors[name] = body.color
    else:
        raise HTTPException(400, f"不支持该颜色：{body.color}")
    with contextlib.suppress(OSError):
        TAG_COLORS_FILE.write_text(json.dumps(tag_colors, ensure_ascii=False))
    return {"colors": all_tags(me)["colors"]}


class ModeBody(BaseModel):
    mode: str


@app.post("/api/tabs/{tid}/mode", dependencies=[Depends(full_only)])
async def set_mode(tid: str, body: ModeBody, me: Who = Depends(who)):
    """换网页对话用的权限模式；Claude 正在跑就当场切换，否则下次发话时生效。"""
    if body.mode not in MODES:
        raise HTTPException(400, f"不支持的模式：{body.mode}")
    tab = used_tab(tid, me)
    # 先让 CLI 切，切成功了再记下来：先记的话 CLI 一拒绝，页面上显示的、文件里存的就成了没切成功的模式，
    # 实际还按原来的模式跑，下次重新拉起 CLI 又会悄悄变过去
    if (d := tab.driver) and d.alive:
        try:
            await d.set_mode(run_mode(d.account, body.mode))   # GPT 号上选 auto 实际按 dontAsk 跑（见 run_mode）
        except Exception as e:
            raise HTTPException(500, f"切换失败，仍为 {tab.mode}：{e}")
    await remember_mode(tab, body.mode)
    return tab.brief()


@app.post("/api/tabs/{tid}/say", dependencies=[Depends(full_only)])
async def say(tid: str, body: Say, me: Who = Depends(who)):
    """往这个会话里发一句话；Claude 正忙就排在后面。回复会照常写进 .jsonl、推到页面上。
    staged=true 时，待发的话和这一句作为同一条消息里的几段文字一起发出（Claude 只回一次，记录里是几个气泡），交给 CLI 以后清空。
    只做马上能做完的检查就回复（前端「正在把这句话送到查看器」只代表浏览器到服务器这段路）：
    核对额度、拉起 CLI 都在后台，走到哪一步照常推送；被拦下就推一条 rejected（带着 nonce），前端把这句话退回、给重试。"""
    marks = [("say", time.time())]   # 这一轮各步的时刻（见 chat.Driver.lap_mark），交话时交给驱动
    extra = [x["text"] for x in staged.get(tid, [])] if body.staged else []
    if not body.text.strip() and not body.images and not body.files and not extra:
        raise HTTPException(400, "请输入内容")
    tab = used_tab(tid, me)
    if body.files:   # 文件的说明接在这句话后面，记录里、气泡里都看得到传了什么、放在哪
        note = await asyncio.to_thread(upload_note, tab, body.files)
        body.text = f"{body.text.rstrip()}\n\n{note}" if body.text.strip() else note
    async with tab.say_lock:
        await asyncio.to_thread(prepare_speak, tab, me)
        marks.append(("prepared", time.time()))
        if not (tab.driver and tab.driver.running) and not tab.prep:   # 回复里就带上「查看器在核对额度」，不空着一拍
            tab.prep = {"kind": "checking", "step": "quota", "t0": time.monotonic()}
        asyncio.create_task(speak_later(tab, me, body.text, body.images, extra, body.nonce, marks))
    return tab.brief()


rejected_n = itertools.count(1)


async def speak_later(tab: Tab, me: Who, text: str, images: list[dict] | None, extra: list[str], nonce: str | None,
                      marks: list | None = None):
    """say 的后台那一半：交给 CLI；发不出去就记成退回（tab.rejected），推给前端。待发的话交出去了才清空，退回时还留着。"""
    marks = marks if marks is not None else []
    try:
        await hand_over(tab, me, text, images, extra, marks)
        if extra:
            staged.pop(tab.id, None)
            save_staged()
    except Exception as e:
        why = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        if not isinstance(e, HTTPException):
            print(f"会话 {tab.sid} 交话给 Claude Code 时出错：{why}\n{traceback.format_exc()}", flush=True)
        tab.rejected = {"nonce": nonce, "why": why, "n": next(rejected_n)}
        record_lap_marks(tab.sid, tab.account, marks, "rejected")
    finally:
        if tab.prep and not tab.speak_lock.locked():
            tab.prep = None


def prepare_speak(tab: Tab, me: Who):
    """发话前马上能做完的：从没选过号的会话按发话的人的设置当场定一次号（auto 挑的是此刻最宽裕的），记下来以后就固定用它；
    查能不能用限量号。不行就抛 HTTPException。
    要算余额（账本里逐个会话 stat 记录文件，配了 run_as 的实例是跨用户调用），所以在线程里调（tab.say_lock 管先后）：
    在事件循环里调时，卡顿记录里有二十几次卡在这条路上。"""
    if tab.id not in accounts and not (tab.driver and tab.driver.alive):
        account = default_account(me, users.paid_ok(me, tab.sid), "private" if isolation.for_sid(tab.sid) else None)[0]
        if not account:
            raise HTTPException(409, NO_ACCOUNT)
        remember_account(tab, account)
    check_paid(me, tab.sid, tab.account)   # 游客、无主会话不能用限量号发话


async def hand_over(tab: Tab, me: Who, text: str, images: list[dict] | None = None, extra: list[str] | None = None,
                    marks: list | None = None):
    """预测拦截、记下这一轮用哪个号，再交给 CLI；一句一句来（speak_lock）。新的一轮在核对额度时 tab.prep 一直挂着，
    交给 CLI 以后驱动自己就算在干活了，中间不留「没在干活」的空当（不然前端会以为这一轮结束、撤掉发送中的气泡）。
    发不出去抛 HTTPException，说明是写给人看的。marks 是这一轮各步的时刻（chat.Driver.lap_mark），就地往后加，交话时交给驱动。"""
    extra = extra or []
    marks = marks if marks is not None else []

    def mark(name):
        marks.append((name, time.time()))

    async with tab.speak_lock:
        mark("locked")
        fresh = not (tab.driver and tab.driver.running)   # 新的一轮；Claude 正在干活时插的话并进下一次请求，由钩子那边管（guard）
        if fresh:
            tab.prep = {"kind": "checking", "step": "quota", "t0": time.monotonic()}
        try:
            if fresh and (to := await asyncio.to_thread(compact_pick, tab, me)):
                # 压缩后的第一轮：换成重新挑出来的号（同自动切号：记下、收掉旧号的 CLI，用新号续接），页面提示一行。
                # 新号这个人此刻用不了（付不起、独立环境不支持）就照旧用原来的号，原因写进日志
                old = tab.account
                try:
                    await asyncio.to_thread(check_paid, me, tab.sid, to)   # 要算余额，不在事件循环里算（见 prepare_speak）
                    remember_account(tab, to)
                except HTTPException as e:
                    print(f"[压缩后重新挑号] 会话 {tab.sid} 挑了 {to} 但用不了（{e.detail}），照旧用 {old}", flush=True)
                else:
                    if tab.driver and getattr(tab.driver, "entry_account", tab.driver.account) != to:
                        tab.drop_driver()
                    tab.switched = {"from": old, "to": to, "n": next(switched_n), "t": time.time(), "kind": "compact"}
            if fresh:
                mark("compact_end")
                nbytes, payer = len((text + "".join(extra)).encode()), users.owner_of(tab.sid)
                mark("quota_start")
                why = await asyncio.to_thread(limit_block, tab.session.path, payer, tab.account,
                                              run_model(tab.account, tab.model), nbytes, len(images or []),
                                              tab.call_accounts())
                mark("quota_end")
                if why:
                    # 自动切号（发话的人没在设置里关掉）：换一个这次过得去的号接着发，CLI 用新号续接（同 set_account）；
                    # 页面上只提示一行（chat_state 的 switched）。一个都没有就照旧拦下
                    # paid_ok 要算余额，写在 lambda 里才在线程里算（直接当参数的话是在事件循环里先算好再交过去）
                    to = users.auto_switch(me) and await asyncio.to_thread(lambda: switch_target(
                        me, users.paid_ok(me, tab.sid), "private" if isolation.for_sid(tab.sid) else None,
                        tab.account, tab.session.path, payer, tab.model, nbytes, len(images or []), tab.call_accounts()))
                    if not to:
                        raise HTTPException(429, why)
                    old = tab.account
                    remember_account(tab, to)
                    if tab.driver and getattr(tab.driver, "entry_account", tab.driver.account) != to:
                        tab.drop_driver()
                    tab.switched = {"from": old, "to": to, "n": next(switched_n), "t": time.time()}
                    mark("switch_end")
                # 额度核过了，接下来记下记账起点（要抢调用记录的锁，最多等 5 秒）、交给 CLI：界面上换一句说，秒数从这里重新数
                tab.prep = {"kind": "checking", "step": "handing", "t0": time.monotonic()}
                if tab.driver:   # 核过了：之前拦下的放开（被拦下的 CLI 一次请求都发不出去，见 chat.Driver.block）
                    tab.driver.unblock()
                mark("handing_start")
                with contextlib.suppress(OSError, TimeoutError):   # 新的一轮：从这里起的调用记在这个号头上
                    await asyncio.to_thread(api_calls.start, tab.session.path, tab.account)
                mark("handing_end")
            tab.connect().send(text, images, extra, marks)
        finally:
            if fresh:
                tab.prep = None


async def speak(tab: Tab, me: Who, text: str, images: list[dict] | None = None, extra: list[str] | None = None):
    """以 me 的名义往会话里发一句话，等交给 CLI 再返回（额度恢复后自动继续走这里，它要当场知道发没发出去）。
    发不出去抛 HTTPException，说明是写给人看的。"""
    marks = [("say", time.time())]   # 查看器替人发的话也记各步的时刻（见 say）
    try:
        async with tab.say_lock:
            await asyncio.to_thread(prepare_speak, tab, me)
            marks.append(("prepared", time.time()))
        await hand_over(tab, me, text, images, extra, marks)
    except HTTPException:
        record_lap_marks(tab.sid, tab.account, marks, "rejected")
        raise


# 额度恢复后自动继续（按会话开关，默认关）：网页对话因为额度停下（CLI 自己报到达限额，或者查看器的
# 预测拦截仿照官方写的那一行），到了额度恢复的时刻，替打开开关的人发一句「继续」。花的是会话主人的额度，和自己发话一样记账，
# 所以由用户自己打开。标签编号 → {by 谁打开的（替谁发话）, path 会话文件, due 打算什么时候发（停下以后才定）, why 到点了还发不出去的原因,
# done 已经接过的那一行到达限额的时间（同一行不接第二次）, sent 上次自动发话的时刻}。在这里面就是开着；开关是用户的选择，存成小文件
AUTO_GO_FILE = DATA / "auto_continue.json"
try:
    auto_go: dict[str, dict] = json.loads(AUTO_GO_FILE.read_text())
except (OSError, ValueError):
    auto_go = {}
AUTO_GO_TEXT = "继续（额度恢复了。这句是查看器按「额度恢复后自动继续」开关替用户自动发的）"
AUTO_GO_GAP = 30          # 后台循环隔多久看一次
AUTO_GO_SLACK = 60        # 恢复时刻过了这么久再发：免得两边钟差一点、上游还没切到新窗口
# 两次自动发话至少隔这么久：读数不准、刚恢复又马上停下时，不至于一停就发、空耗额度
AUTO_GO_MIN_GAP = 1800
# 原文里写着到了限额的报错：上游报的已经由 session 记上 limit，这个留给别的写法（比如 GPT 号被网络闸门拦下时 CLI 记的「… limit reached ∙ resets …」）
LIMIT_TEXT = re.compile(r"hit your .*limit|limit reached", re.I)


def save_auto_go():
    AUTO_GO_FILE.write_text(json.dumps(auto_go, ensure_ascii=False))


def limit_stop(tab: Tab) -> dict | None:
    """这个会话的网页对话是不是因为额度停着：是就返回到达限额的那一条（text、ts），不是返回 None。
    从末尾往前找第一条算数的：之后又有人说话、Claude 又回了、后台任务又来了通知，就是已经接着动过了。"""
    d = tab.driver
    if d and d.running:
        return None
    for it in reversed(tab.session.items):
        k = it.get("kind")
        if k == "limit" or (k == "error" and (it.get("limit") or LIMIT_TEXT.search(it.get("text") or ""))):
            return it
        if k in ("user", "command", "text", "tool", "result", "task", "error"):
            return None
    return None


def resets_of(tab: Tab, line: dict) -> float | None:
    """那一行到达限额说的恢复时刻（time.time）。按那一行写下的时刻推是哪一天（session.resets_at，认括号里的时区）；
    没写的就看这个号的额度读数：写的是本周的、或者这个号只有本周额度，看本周的窗口；上游只说拒了、没说是哪个窗口的
    （「Request rejected (429)」），看读数里已经用满的窗口（都满了取晚的那个），都没满就看 5 小时的。都不知道是 None。"""
    text, ts = line.get("text") or "", stamp(line.get("ts")) or time.time()
    if t := resets_at(text, ts):
        return t
    snap = quota(limit=0)["accounts"].get(tab.account) or {}
    if "week" in text.lower() or tab.account in WEEK_ONLY:
        keys = ["seven_day"]
    else:
        keys = [k for k in ("five_hour", "seven_day") if ((snap.get(k) or {}).get("used_percentage") or 0) >= 100] or ["five_hour"]
    due = [w["resets_at"] for k in keys if (w := snap.get(k) or {}).get("resets_at") and w["resets_at"] > ts]
    return max(due) if due else None


def plan_auto_go(tab: Tab, rec: dict) -> bool:
    """看这个开着开关的会话现在是不是因为额度停着、该几点发，记进 rec；变了返回 True（要存）。"""
    old = dict(rec)
    line = limit_stop(tab)
    if not line or line.get("ts") == rec.get("done"):
        rec.update(due=None, why="")
    elif rec.get("due") is None or rec.get("line") != line.get("ts"):
        due = resets_of(tab, line)
        rec.update(line=line.get("ts"), why="" if due else "认不出额度什么时候恢复，查到这个号的额度读数以后再定")
        rec["due"] = max(due, (rec.get("sent") or 0) + AUTO_GO_MIN_GAP) if due else None
    return rec != old


def auto_go_state(tab: Tab) -> dict | None:
    """给前端：开关开没开、是不是因为额度停着、打算几点发、到点了为什么还没发。都不沾边就是 None（不显示那一行）。"""
    rec, line = auto_go.get(tab.id) or {}, limit_stop(tab)
    if not rec and not line:
        return None
    # 停着的这一行已经替人发过「继续」（记录里还没出现那句话，或者 CLI 没起来、错误另有一行写着）：写明几点发的
    sent = rec.get("sent") if line and line.get("ts") == rec.get("done") else None
    return {"on": bool(rec), "stop": bool(line), "due": rec.get("due"), "why": rec.get("why") or "", "sent": sent}


async def auto_go_tick():
    """后台循环每 AUTO_GO_GAP 秒调一次：开着开关的会话停在到达限额上，到了恢复时刻就替打开开关的人发一句「继续」。
    没人看的会话标签可能已经释放了，到点时按存下的路径重新打开；发不出去（还被拦着、余额不够……）把原因写在那一行上，过一会儿再试。"""
    now, dirty = time.time(), False
    for tid, rec in list(auto_go.items()):
        tab = tabs.get(tid)
        # 删掉的会话不替人发话（开关照旧记着，恢复以后接着管）：没人看得到它，不能在背后接着花额度
        if users.is_deleted(tab.sid if tab else users.sid_of(rec["path"])):
            continue
        if tab is None:
            if not rec.get("due") or now < rec["due"] + AUTO_GO_SLACK:
                continue
            try:
                tab = await asyncio.to_thread(open_tab, isolation.session_path(users.sid_of(rec["path"])) or hostfs.Path(rec["path"]))
            except Exception as e:
                why = f"到点了，但打不开这个会话：{type(e).__name__}: {e}"
                dirty |= rec.get("why") != why
                rec["why"] = why
                continue
        await asyncio.to_thread(tab.session.poll)
        dirty |= plan_auto_go(tab, rec)
        if not rec.get("due") or now < rec["due"] + AUTO_GO_SLACK:
            continue
        me = Who(rec.get("by")) if users.MULTI else users.MASTER
        try:
            if why := users.use_why(me, tab.sid) or (not users.can_see(me, tab.sid) and users.hidden_why(me)):
                raise HTTPException(403, why)
            await speak(tab, me, AUTO_GO_TEXT)
        except HTTPException as e:
            why = f"到点了，但还发不出去，过一会儿再试：{e.detail}"
            dirty |= rec.get("why") != why
            rec["why"] = why
            continue
        except Exception as e:   # 拉不起 CLI 之类：同样写在那一行上，下一次接着试
            rec["why"] = f"到点了，但发的时候出错，过一会儿再试：{type(e).__name__}: {e}"
            print(f"会话 {tab.sid} 额度恢复后自动继续出错：{rec['why']}\n{traceback.format_exc()}", flush=True)
            dirty = True
            continue
        print(f"会话 {tab.sid} 额度恢复了，替 {rec.get('by') or CALL} 自动发了一句继续（{tab.account}）", flush=True)
        rec.update(due=None, why="", done=rec.get("line"), sent=now)
        dirty = True
    if dirty:
        try:
            save_auto_go()
        except OSError as e:
            print(f"「额度恢复后自动继续」的状态没存下来：{e}", flush=True)


class AutoGoBody(BaseModel):
    on: bool


@app.post("/api/tabs/{tid}/autogo", dependencies=[Depends(full_only)])
def set_auto_go(tid: str, body: AutoGoBody, me: Who = Depends(who)):
    """开关「额度恢复后自动继续」。打开时记下是谁开的：到点替他发话，和他自己发话一样要过限量号、预测拦截那几道关。"""
    tab = used_tab(tid, me)
    if body.on and users.MULTI and not me.name:
        raise HTTPException(401, "请登录后开启：额度恢复后将以您的名义发送消息")
    old = auto_go.get(tab.id)
    if body.on:
        rec = auto_go[tab.id] = {**(old or {}), "by": me.name, "path": str(tab.session.path)}
        plan_auto_go(tab, rec)
    else:
        auto_go.pop(tab.id, None)
    try:
        save_auto_go()
    except OSError as e:   # 没存下来：撤回去，免得页面上显示开着、重启后却没了
        if old is None:
            auto_go.pop(tab.id, None)
        else:
            auto_go[tab.id] = old
        raise HTTPException(500, f"设置保存失败，未作更改：{e}")
    return tab.brief()


# 重启后自动续上：网页对话的 CLI 是实例的子进程，实例一重启，正在干的一轮、挂着的后台任务、
# 约好的叫醒全没了，无人值守的会话就此停住，只能一个个手动发话续上。
# 后台循环一直记着哪些会话在干活（变了才写文件），起来以后发一条只给 AI 看的提示叫醒它（
# 以前是以会话主人的名义发一句「继续（刚才连接断了一下……）」，页面上是一个绿色气泡）。提示包在 <viewer-restart> 里，
# session.on_user_text 认出来记成 restart 条目，页面上不显示、只当一轮的分界；对 AI 直说刚刚重启了，接不接、查不查后台由它自己判断。
# 不等退出时才记：systemd 停服务时 CLI 和查看器同时收到信号，等查看器收尾时驱动多半已经看到 CLI 没了，记下来的就不准了；
# 一直记着的，被 kill -9、内存不够被杀也照样接得上。
# 文件里是会话编号 → {path 会话文件, at 什么时候发（time.time，0 是起来就发；被额度拦下的改成额度恢复的时刻）,
# kind turn 在干活或挂着后台任务 / wake 约了叫醒}。
# 起来以后读进 resume_todo，提示进了会话记录或者不用发了就删掉；还没续上又重启的，照旧留在文件里
RESUME_FILE = DATA / "resume_after_restart.json"
try:
    resume_todo: dict[str, dict] = json.loads(RESUME_FILE.read_text())
except FileNotFoundError:
    resume_todo = {}
except (OSError, ValueError) as e:
    print(f"读不了要续上的会话（{RESUME_FILE.name}），这次不自动续：{e}", flush=True)
    resume_todo = {}
STARTED = time.time()      # 这个实例起来的时刻：记录里在这之后又有动静，就是有人接着管了
resume_why: dict[str, str] = {}   # 会话编号 → 上次没发出去的原因（变了才打日志）
resume_sent: dict[str, float] = {}   # 会话编号 → 这次起来以后发出续上提示的时刻（只在内存里：重启了就重新发）
resume_last = ""           # 上次写进文件的内容
RESUME_TAG = "viewer-restart"   # 和 session.RESTART_TAG 一致
RESUME_TEXT = {k: f"<{RESUME_TAG}>{v}</{RESUME_TAG}>" for k, v in {
    "turn": "查看器刚刚重启了一下（这条是查看器自动发给你的，用户看不到）。如果你手上的工作被打断了，就接着做；"
            "如果没有打断什么，忽略这条就好；在后台运行的命令或任务可能被中断了，有的话检查一下。"
            "重启那一刻正在跑的工具调用会显示成被用户拒绝（The user doesn't want to take this action right now…），"
            "那是重启掐断的，不是用户拒绝，该重做就重做。",
    "wake": "查看器刚才重启过（这条是查看器自动发给你的，用户看不到），你约好的叫醒随之没了，现在到了约好的时间，照原来的打算接着做；"
            "在后台运行的命令或任务可能被中断了，有的话检查一下；后面还要等的话重新约。",
}.items()}
RESUME_SETTLE = 15         # 起来以后先等这么久再发：网络闸门、ssh 门卫、后台循环都就绪
RESUME_GAP = 60            # 发不出去（被拦着、拉不起 CLI）隔多久再试
RESUME_GIVE_UP = 3600      # 停了这么久还没续上就不管了：停太久再自动接着干，用户反而会意外
NOT_LANDED = "交出去了，但没进记录、CLI 也没了（多半没起来）"
PERCENT = re.compile(r"\d+(?:\.\d+)?%")


def note_working():
    """后台循环每圈调：现在在干活的网页对话（正在跑一轮、挂着后台任务、约了叫醒）连同还没续上的，变了就写进文件。
    被预测拦截拦着的不算：那是额度不够故意停的，额度停着的归「额度恢复后自动继续」管。"""
    global resume_last
    out = dict(resume_todo)
    for tab in list(tabs.values()):
        d = tab.driver
        if not (d and d.alive) or d.blocked:
            continue
        if d.running or (d.tasks and d.waiting_bg()):
            out[tab.sid] = {"path": str(tab.session.path), "at": 0, "kind": "turn"}
        elif wake := d.next_wake():
            out[tab.sid] = {"path": str(tab.session.path), "at": round(wake), "kind": "wake"}
    text = json.dumps(out, ensure_ascii=False, sort_keys=True)
    if text != resume_last:
        diskq.rewrite(RESUME_FILE, text)   # 写盘线程写；写不进去由它打日志、过一会儿再试，所以这里照样记下已交出
        resume_last = text


def touched_since(tab: Tab, t: float) -> bool:
    """t 以后这个会话又动过没有：有人说了话、Claude 又回了、后台任务又来了通知，就是已经有人接着管了，不替人发。"""
    for it in reversed(tab.session.items):
        ts = stamp(it.get("ts"))
        if ts is None:
            continue
        if ts <= t:
            return False
        if it.get("kind") in ("user", "command", "text", "tool", "result", "task"):
            return True
    return False


def landed(tab: Tab, t: float) -> bool:
    """t 以后发的续上提示已经进了会话记录（CLI 收下了）。"""
    return any(it.get("kind") == "restart" and (stamp(it.get("ts")) or 0) > t for it in tab.session.items[-200:])


async def resume_tick():
    """后台循环每 RESUME_GAP 秒调一次（起来 RESUME_SETTLE 秒以后才开始）：把上次停下时还在干活的会话续上，
    约了叫醒的到点才发；发不出去的下一圈再试，起来太久还没续上的就不管了。
    交给 CLI 不等于续上了：CLI 可能在后台起不来（比如挂载没响应），所以发出去以后先留着，提示进了记录才算数，没进记录、CLI 也没了就再发。
    被额度拦下的改约到额度恢复的时刻再发，放弃的期限也从那时算起。"""
    now = time.time()
    for sid, rec in list(resume_todo.items()):
        if now < rec.get("at", 0):
            continue
        if now - max(rec.get("at", 0), STARTED) > RESUME_GIVE_UP or users.is_deleted(sid):
            print(f"会话 {sid} 没有自动续上：{resume_why.get(sid) or '停得太久或已删除'}", flush=True)
            resume_todo.pop(sid)
            continue
        owner = users.owner_of(sid)
        sent = resume_sent.get(sid)
        tab = None
        try:
            tab = await asyncio.to_thread(open_tab, isolation.session_path(sid) or hostfs.Path(rec["path"]))
            await asyncio.to_thread(tab.session.poll)
            if sent and landed(tab, STARTED):
                print(f"会话 {sid} 自动续上了，提示已经进了记录", flush=True)
                resume_todo.pop(sid)
                continue
            if tab.driver and tab.driver.alive:
                if not sent:   # 起来以后有人连上了，归他管
                    resume_todo.pop(sid)
                continue       # 是这边发的，CLI 还在起：下一圈再看
            # 已经有人接着管了（起来以后有人说了话、Claude 又动了），或者是额度停着（归额度恢复后自动继续管），不替人发
            if touched_since(tab, STARTED) or limit_stop(tab):
                resume_todo.pop(sid)
                continue
            if sent and tab.driver and tab.driver.stuck:   # 挂载没响应：再拉起也是卡住、进程还收不掉，页面上已经说了原因
                print(f"会话 {sid} 没有自动续上：{tab.driver.error}", flush=True)
                resume_todo.pop(sid)
                continue
            if sent and resume_why.get(sid) != NOT_LANDED:
                print(f"会话 {sid} 的续上提示{NOT_LANDED}，再发一次", flush=True)
                resume_why[sid] = NOT_LANDED
            if users.MULTI and not owner:   # 无主会话没有人可以替他说话
                resume_todo.pop(sid)
                continue
            me = Who(owner, owner in users.ADMINS) if users.MULTI else users.MASTER
            if why := users.use_why(me, sid):
                raise HTTPException(403, why)
            await speak(tab, me, RESUME_TEXT["wake" if rec.get("kind") == "wake" else "turn"])
        except Exception as e:
            why = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
            # 拦截原因里的「此刻只能用百分之几」每分钟都在变，比较时去掉，同一种原因只打一次日志
            if PERCENT.sub("", why) != PERCENT.sub("", resume_why.get(sid, "")):
                print(f"会话 {sid} 自动续上还发不出去，过一会儿再试：{why}", flush=True)
            resume_why[sid] = why
            if tab and (t := resets_at(why, now)) and t > now:
                rec["at"] = round(t + AUTO_GO_SLACK)   # 额度用完了：等恢复了再发
            continue
        print(f"会话 {sid} 上次停下时还在干活，替 {owner or CALL} 发了续上提示（{tab.account}）", flush=True)
        resume_sent[sid] = now


# 额度用完后切号接着干（照着重启后自动续上写）：Claude 干到一半，当前的号被预测拦截拦下（钩子、请求刚发出那两层），
# 或者预测没拦住、上游自己报了限额（chat.Driver._upstream_limit 同样记成拦下），
# 等这一轮停下，按自动挑号的排法换一个过得去的号，替会话主人发一句提示让 Claude 接着做。包在同一个标签里（session.RESTART_TAG），
# 记录里是 restart 条目，页面上不显示，和续上提示一样把前后分成两轮。说明 AI 看得到，所以不提查看器、号和换号（同 limit_block）
SWITCH_TEXT = (f"<{RESUME_TAG}>刚才因为用量到了上限被停下，现在可以接着用了（这条是自动发给你的，用户看不到）。"
               "如果你手上的工作被打断了，就接着做；如果没有打断什么，忽略这条就好。"
               "被停下那一刻正在跑的工具调用会显示成被用户拒绝（The user doesn't want to take this action right now…），"
               f"那不是用户拒绝，该重做就重做。</{RESUME_TAG}>")


async def switch_tick():
    """后台循环每圈调：被预测拦截拦着（或上游报了限额）、这一轮已经停下的网页对话，会话主人没关自动切号（users.auto_switch）的，
    挑一个这句提示过得去的号（switch_target），记成这个会话的号、收掉旧号的 CLI，再替会话主人发 SWITCH_TEXT（用新号续接）。
    一次拦下只试一回（Driver.switch_tried）：挑不出号就照旧拦着，等用户自己发话或者额度恢复后自动继续。无主会话没有人可以替他说话，不切。"""
    for tab in list(tabs.values()):
        d = tab.driver
        if not (d and d.alive and d.blocked) or d.running or d.switch_tried:
            continue
        d.switch_tried = True
        owner = users.owner_of(tab.sid)
        if users.MULTI and not owner:
            continue
        me = Who(owner, owner in users.ADMINS) if users.MULTI else users.MASTER
        if not users.auto_switch(me) or users.use_why(me, tab.sid):
            continue
        # paid_ok 要算余额，写在 lambda 里才在线程里算（见 hand_over 里同样的一处）
        to = await asyncio.to_thread(lambda: switch_target(
            me, users.paid_ok(me, tab.sid), "private" if isolation.for_sid(tab.sid) else None,
            tab.account, tab.session.path, owner, tab.model, len(SWITCH_TEXT.encode()), 0, tab.call_accounts()))
        if not to:
            print(f"会话 {tab.sid}（{tab.account}）被拦下，没有别的号过得去，不自动切号", flush=True)
            continue
        old = tab.account
        remember_account(tab, to)
        tab.drop_driver()
        tab.switched = {"from": old, "to": to, "n": next(switched_n), "t": time.time()}
        asyncio.create_task(switch_go(tab, me, old))


async def switch_go(tab: Tab, me: Who, old: str):
    try:
        await speak(tab, me, SWITCH_TEXT)
        print(f"会话 {tab.sid} 从 {old} 切到 {tab.account}，替 {me.name or CALL} 发了接着干的提示", flush=True)
    except Exception as e:
        why = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        print(f"会话 {tab.sid} 切到 {tab.account} 以后接着干的提示没发出去：{why}", flush=True)


class StageBody(BaseModel):
    text: str


@app.post("/api/tabs/{tid}/staged", dependencies=[Depends(full_only)])
def add_staged(tid: str, body: StageBody, me: Who = Depends(who)):
    """输入框旁的「+」：只存不发。存在服务器上，换设备、刷新都还在。"""
    if not body.text.strip():
        raise HTTPException(400, "请输入内容")
    tab = used_tab(tid, me)
    staged.setdefault(tid, []).append({"id": uuid.uuid4().hex[:8], "text": body.text, "ts": time.time()})
    save_staged()
    return tab.brief()


@app.delete("/api/tabs/{tid}/staged/{sid}", dependencies=[Depends(full_only)])
def drop_staged(tid: str, sid: str, me: Who = Depends(who)):
    """删掉一条待发的话。"""
    tab = used_tab(tid, me)
    left = [x for x in staged.get(tid, []) if x["id"] != sid]
    if left:
        staged[tid] = left
    else:
        staged.pop(tid, None)
    save_staged()
    return tab.brief()


@app.post("/api/tabs/{tid}/interrupt", dependencies=[Depends(full_only)])
async def interrupt(tid: str, me: Who = Depends(who)):
    if (tab := tabs.get(tid)) and tab.driver:
        used_tab(tid, me)
        try:
            await tab.driver.interrupt()
        except Exception as e:
            # SDK 等满 60 秒没等到回应。不接住的话回的是一张出错网页，前端只能说「看不懂的内容」。
            # 卡在挂载上的驱动自己认得出来，不走到这里
            raise HTTPException(504, f"Claude Code 未响应中断请求（{type(e).__name__}: {e}），请稍后重试，或先切换到其他会话")
    return {"ok": True}


class Birth:
    """网页新建、还没写出会话记录的会话（新会话的第一句话也要能看出卡在链路的哪一段）。
    原来新建接口要等 CLI 起来、写出会话记录（最多 30 秒）才回复，这段时间前端只能写「交给服务器」，启动、准备请求、
    节点连不上全看不出来。现在接口做完马上能做的检查就回一个编号（birth-<会话编号>），前端订阅它的推送：
    查看器核对额度 → 启动 Claude Code → 准备请求 → 出网 …… 和已有会话同一套阶段；会话记录出现就推 born（正式标签页的快照），
    没建成就推 error（连同最后停在哪一步）。只有建它的人订阅得到。建完或失败后留一会儿，晚到的订阅也能拿到结果。"""

    def __init__(self, me: Who, body: "NewSession", cwd, account: str, model: str | None, mode: str, card: dict | None,
                 work: tuple | None = None):
        self.sid = str(uuid.uuid4())
        self.id = f"birth-{self.sid}"
        self.me, self.body, self.cwd, self.account, self.model, self.mode = me, body, cwd, account, model, mode
        self.card = card   # 选的人设卡（personas.pick），拉起 CLI 之前存成这个会话的快照
        self.work = work   # (取消勾选的工作设备, 这个人登记的设备)，workdev.check 核对过；没开门卫、游客是 None
        self.t0 = time.monotonic()
        self.marks: list = []   # 第一句话各步的时刻（见 chat.Driver.lap_mark），new_session 填上发话和检查做完的时刻
        self.driver: Driver | None = None
        self.error, self.last, self.born = "", None, None   # 没建成的原因、那时停在哪一步；建成后的正式标签页快照
        self.switched: dict | None = None   # 第一句话被预测拦截拦下、自动切了号（同 Tab.switched）
        self.todos_born = False   # 已经在待办上记了从它开出（body.todos），没建成要撤掉
        self.todo_groups: list[str] = []   # 那几条待办的分组名：写出记录时当标签打上
        self.done_at: float | None = None
        self.pushed = None

    def status(self) -> dict | None:
        if self.born or self.error:
            return None
        if self.driver:
            return self.driver.status() or {"kind": "starting", "age": 0}
        return {"kind": "checking", "age": round(time.monotonic() - self.t0, 1)}

    def message(self) -> dict:
        return {"id": self.id, "birth": True, "running": not (self.born or self.error), "phase": self.status(),
                "error": self.error, "last": self.last, "born": self.born, "switched": self.switched}

    def key(self) -> str:
        s = self.status()
        view = {k: v for k, v in s.items() if k not in ("age", "quiet", "rxQuiet")} if s else None
        return json.dumps([view, self.error, bool(self.born), bool(self.switched)], sort_keys=True, ensure_ascii=False)

    def fail(self, why: str):
        if not self.driver:   # 没交到驱动手上就停了（额度拦下、存不下设置……）：只有查看器这边的几步
            record_lap_marks(self.sid, self.account, self.marks, "rejected")
        self.last, self.error = self.status(), why
        if self.driver and self.driver.alive:
            self.driver.task.cancel()

    async def run(self):
        body, me = self.body, self.me
        try:
            # 预测拦截：第一次请求也可能把额度扣穿。图片和文字都计入，拦下时自动切号也要按同一份内容核对
            nbytes, images = len(body.text.encode()), len(body.images or [])
            self.marks.append(("quota_start", time.time()))
            why = await asyncio.to_thread(limit_block, None, me.name, self.account, self.model, nbytes, images)
            self.marks.append(("quota_end", time.time()))
            if why:
                to = users.auto_switch(me) and await asyncio.to_thread(lambda: switch_target(   # paid_ok 在线程里算，见 hand_over
                    me, users.paid_ok(me), body.environment, self.account, None, me.name, body.model, nbytes, images))
                if not to:
                    return self.fail(why)
                self.switched = {"from": self.account, "to": to, "n": next(switched_n), "t": time.time()}
                self.account, self.model = to, run_model(to, body.model)
                self.marks.append(("switch_end", time.time()))
            # 先记主人和可见范围再起 CLI；它整份重写用户表，放到线程里等它写完（存不下来照旧抛错、建会话失败）
            await asyncio.to_thread(users.claim, self.sid, me, body.visible)
            isolation.bind_session(self.sid, isolation.select(me.name, body.environment))
            if self.work:   # 工作设备也要在拉起 CLI 之前记下：系统提示词里写它，门卫按它放行
                try:
                    await asyncio.to_thread(workdev.start, self.sid, me.name, *self.work)   # 系统提示词按它写能用的设备
                except OSError as e:
                    return self.fail(f"工作设备保存失败：{e}")
            try:   # 拼系统提示词时读它（chat.session_prompt），补充资料这时写到 Claude 那边
                await asyncio.to_thread(personas.remember, self.sid, self.card, personas.folder(self.sid))
            except OSError as e:
                return self.fail(f"人设卡「{self.card['name']}」的资料、钩子写不进 {personas.folder(self.sid)}：{e}")
            if body.todos:   # 拉起 CLI 之前记上：会话一开口就能用 cctodo mine 读到这几条；没建成在 finally 里撤掉
                try:
                    self.todo_groups = await asyncio.to_thread(todos.born, me.name, body.todos, self.sid)
                    self.todos_born = True
                except todos.Bad as e:
                    return self.fail(f"没能记下这个会话是从哪几条待办开出的：{e}")
            env = isolation.for_sid(self.sid)
            d = self.driver = new_driver(self.sid, str(self.cwd), resume=False, account=self.account,
                                         mode=run_mode(self.account, self.mode), model=launch_model(self.account, env, self.model),
                                         effort=launch_effort(body.effort, self.account, env, self.model))
            d.send(body.text, body.images, marks=self.marks)
            for _ in range(300):   # 最多等 30 秒：启动 CLI 加上写下第一句话
                if hit := calls.find(self.sid):
                    return await self.found(hit)
                if not d.alive:
                    return self.fail(d.error or "Claude Code 没能启动")
                await asyncio.sleep(0.1)
            self.fail("等了 30 秒，Claude Code 还没写出这个会话的记录，已经放弃")
        except HTTPException as e:
            self.fail(e.detail)
        except Exception as e:
            print(f"新建会话 {self.sid} 出错：{type(e).__name__}: {e}\n{traceback.format_exc()}", flush=True)
            self.fail(f"{type(e).__name__}: {e}")
        finally:
            self.done_at = time.monotonic()
            if self.error and self.todos_born:   # 会话没建成，待办上记的开出、认领都不成立
                try:
                    await asyncio.to_thread(todos.unborn, me.name, self.sid)
                except todos.Bad as e:
                    print(f"新建会话 {self.sid} 没建成，待办上的开出记录撤不掉：{e}", flush=True)

    async def found(self, path: Path):
        body, d = self.body, self.driver
        tab = open_tab(path)
        remember_account(tab, self.account)
        remember_tags(tab.id, body.tags + self.todo_groups)   # 从待办页开出的，连同那几条的分组名（todos.born）
        tab.model = body.model
        # 选的权限模式一建好就存下来（和换模式一样），释放、重启后照原样恢复。
        # 会话已经建出来了，存不下来不能当成建失败（前端会再建一个），写进这个标签页的出错原因里
        warn = ""
        try:
            await remember_mode(tab, self.mode)
            # CLI 已经带着 --effort 起来了，选的这档当场就用上。第一句就是 /effort 的话，open_tab 里已经看过了，
            # 存完从头按时间再比一遍（它比 CLI 起来晚，照样压过新建时选的）
            if body.effort:
                tab.effort_seen = (None, 0)
                await saved(remember_effort(tab, {"effort": body.effort, "t": d.since, "on": d.since}), "思考强度")
        except HTTPException as e:
            warn = e.detail
        with contextlib.suppress(OSError, TimeoutError):   # 网页新建的会话，从第一次调用起就记账号
            await asyncio.to_thread(api_calls.start, tab.session.path, self.account, True)
        tab.driver = d   # 刚才开会话用的就是这个驱动，直接交给这个标签接着用
        asyncio.create_task(mark_as_cli(tab.session.path, d))
        brief = tab.brief()
        if warn:
            brief["chatError"] = warn
        self.born = brief


births: dict[str, Birth] = {}
BIRTH_KEEP_SECS = 300   # 建完或失败后还留多久


def publish_birth(b: Birth):
    """和上次推送的比，变了才推（阶段内容变了、出错了、建成了）。"""
    old, b.pushed = b.pushed, b.key()
    if old == b.pushed or not subs.get(b.id):
        return
    data = json.dumps(b.message(), ensure_ascii=False)
    for queue in subs[b.id]:
        send(queue, data)


@app.post("/api/sessions", dependencies=[Depends(full_only)])
async def new_session(body: NewSession, me: Who = Depends(who)):
    """在某个工作目录里开一个新会话、说出第一句话。只做马上能做完的检查就回复，后面的在后台做（见 Birth）。
    登录的人建的会话记在他名下、默认只有他看得到；游客建的是无主会话。"""
    said = time.time()   # 第一句话到了查看器（计时，见 Birth.marks）
    env = private_call(isolation.select, me.name, body.environment)
    if env and body.visible:
        raise HTTPException(400, "独立环境的会话只供本人使用")
    cwd = env.path(body.cwd.strip() or "~") if env else hostfs.Path(body.cwd.strip() or "~").expanduser()
    if not await asyncio.to_thread(cwd.is_dir):   # 配了 run_as 时是跨用户调用，不在事件循环里等
        raise HTTPException(400, f"目录不存在：{cwd}")
    if not body.text.strip() and not body.images:
        raise HTTPException(400, "请输入内容")
    mode = body.mode or DEFAULT_MODE
    if mode not in MODES:
        raise HTTPException(400, f"不支持该权限模式：{mode}")
    if body.effort is not None and body.effort not in EFFORTS:
        raise HTTPException(400, f"不支持该思考强度：{body.effort}")
    if body.todos:   # 从待办页发来的：先核对这几条都在、没做完（游客没有待办）
        if not me.name:
            raise HTTPException(400, "待办只给登录的用户用")
        body.todos = await asyncio.to_thread(todo_call, todos.check, me.name, body.todos)   # 第一次要读文件，不在事件循环里等
    # 没指定就按这个人的设置（没设就是全局设置）。挑号和下面的 check_paid 都要算余额，放在线程里（见 prepare_speak）
    account = body.account or (await asyncio.to_thread(default_account, me, None, body.environment))[0]
    check_web_account(account)
    if not account:
        raise HTTPException(409, NO_ACCOUNT)
    private_call(isolation.check_account, env, account)
    await asyncio.to_thread(check_paid, me, None, account)
    model = run_model(account, body.model)   # 选的模型和号不是一家的，这个号上按它自己的默认模型（见 run_model）
    # 没有单价的模型不许用（rates.unpriced）；中转站的模型按号的单价查（Gemini 只在号的配置里有价，单价表里没有）
    if why := rates.unpriced(rates.billing_model(model, account)):
        raise HTTPException(400, why)
    card = persona_call(personas.pick, me.name, body.persona, body.personaConsent)
    work = None
    if devices.ENABLED and me.name:   # 开了门卫才有设备；游客建的无主会话门卫本来就不认
        mine = sshgw.targets(me.name)
        try:
            work = (workdev.check(body.devicesOff, mine), mine)
        except workdev.Bad as e:
            raise HTTPException(400, str(e))
    if body.remember and me.name:
        # 检查都过了、真要建了才记：人设卡就记成自己的默认，和人设卡页上设的是同一个值
        # 整份重写用户表：放到线程里等它写完，不在事件循环里等磁盘；存不下来照旧抛错给前端
        await asyncio.to_thread(users.remember_fresh_tab, me, body.environment, body.cwd.strip() or "~", body.tags)
        if body.persona is not None and body.rememberPersona:
            persona_call(personas.set_default, me.name, card and card["id"])
    b = Birth(me, body, cwd, account, model, mode, card, work)
    b.marks = [("say", said), ("prepared", time.time())]
    births[b.id] = b
    asyncio.create_task(b.run())
    b.pushed = b.key()   # 回复里已经带着这一刻的状态
    return b.message()


ENTRY_SDK, ENTRY_CLI = b'"entrypoint":"sdk-cli"', b'"entrypoint":    "cli"'   # 一样长，原地换不改文件长度


async def mark_as_cli(path: Path, d: Driver):
    """CLI 的 /resume 列表会藏起开头入口标记是 sdk-cli、sdk-py 的会话，网页新建的会话在终端里就看不到。
    CLI 在非交互模式下不认 CLAUDE_CODE_ENTRYPOINT=cli，只好等第一轮结束后，把记录里的 sdk-cli
    原地换成等长的 cli 写法（冒号后补空格，还是合法 JSON）。文件长度不变，CLI 同时往末尾追加也不受影响。"""
    while d.alive and d.running:
        await asyncio.sleep(1)
    try:
        with hostfs.Path(path).open("r+b") as f:
            data = f.read()
            at = data.find(ENTRY_SDK)
            while at >= 0:
                f.seek(at)
                f.write(ENTRY_CLI)
                at = data.find(ENTRY_SDK, at + 1)
    except OSError:
        pass


def remember_account(tab: Tab, account: str):
    private_call(isolation.check_account, isolation.for_sid(tab.sid), account)
    tab.account = accounts[tab.id] = account
    diskq.rewrite(ACCOUNTS_FILE, json.dumps(accounts))


async def saved(fut, what: str):
    """等写盘线程把用户的选择写进文件（diskq.rewrite_wait）：存不下来要说出来，否则释放或重启后会悄悄变回去；
    等的是线程，事件循环照常跑（磁盘慢时同步写这一下卡过 7 秒）。"""
    try:
        await asyncio.wrap_future(fut)
    except OSError as e:
        raise HTTPException(500, f"{what}保存失败：{e}")


async def remember_mode(tab: Tab, mode: str):
    tab.mode = modes[tab.id] = mode
    await saved(diskq.rewrite_wait(MODES_FILE, json.dumps(modes)), "权限模式")


def remember_effort(tab: Tab, rec: dict | None):
    """rec 是 {effort 哪档, t 什么时候定的, on 从什么时候起 CLI 真的按它跑（还没有是 None）}，None 是不指定（见 Tab.sync_effort）。
    内存里当场改好；返回写盘的 Future，异步的地方交给 saved 等它，存不下来就报出来，否则会悄悄变回 CLI 自己的设置。"""
    tab.effort = rec["effort"] if rec else None
    if rec:
        efforts[tab.id] = rec
    else:
        efforts.pop(tab.id, None)
    return diskq.rewrite_wait(EFFORTS_FILE, json.dumps(efforts))


class EffortBody(BaseModel):
    effort: str | None = None   # None：不指定，按 CLI 自己的设置


@app.post("/api/tabs/{tid}/effort", dependencies=[Depends(full_only)])
async def set_effort(tid: str, body: EffortBody, me: Who = Depends(who)):
    """换网页对话的思考强度。CLI 连着就替用户发一句 /effort，不收掉 CLI（后台任务也不会跟着没了）：
    CLI 空着时当场执行，下一次请求起生效；Claude 正在回复时，这句排到这一轮结束才执行（不会当成插话交给 Claude，
    这一轮剩下的请求还是旧档，实测过），所以 Claude 干活时也能换。
    没连着的，下次发话带着 --effort 起来。用上之前面板上灰着（chat_state 的 effortPending）。"""
    if body.effort is not None and body.effort not in EFFORTS:
        raise HTTPException(400, f"不支持该思考强度：{body.effort}")
    tab = used_tab(tid, me)
    d = tab.driver if tab.driver and tab.driver.alive else None
    if body.effort is None:
        # 不指定、回到 CLI 自己的设置：/effort 说不出这个意思（auto 不是一回事），只能像以前一样收掉 CLI 另起
        if d and d.running:
            raise HTTPException(409, "Claude 正在运行，请在本轮结束后恢复 Claude Code 的默认设置")
        await saved(remember_effort(tab, None), "思考强度")
        # 连着的 CLI 起来时带的就是不指定时该带的那档（设置没写时的默认档，见 launch_effort），就不用另起
        if d and d.effort != launch_effort(None, tab.account, isolation.for_sid(tab.sid), tab.model):
            tab.drop_driver()
        return tab.brief()
    await saved(remember_effort(tab, {"effort": body.effort, "t": time.time(), "on": None}), "思考强度")
    # 快照在发 /effort 之前取：CLI 空着时执行这句只要一眨眼，但发出去那一下驱动算「在跑」，
    # 页面会闪一下「Claude 正在回复」，说明也会写错
    brief = tab.brief()
    if d:
        d.effort_to(body.effort)   # CLI 要是没等到执行这句就没了，自己续接时也带上新的这档
        d.send(f"/effort {body.effort}")
    return brief


class AccountBody(BaseModel):
    account: str


@app.post("/api/tabs/{tid}/account", dependencies=[Depends(full_only)])
async def set_account(tid: str, body: AccountBody, me: Who = Depends(who)):
    """换网页对话用的账号。CLI 进程启动后就换不了号，所以要把连着的 CLI 收掉，下次发话用新号续接；
    Claude 正在干活时不让换，免得把这一轮掐断。此刻付不起的限量号照样能选（选号不花钱），
    发话、连 CLI 时 check_paid 才拦。"""
    check_web_account(body.account)
    if body.account not in ACCOUNTS:
        raise HTTPException(400, f"账号不存在：{body.account}")
    if body.account in BANNED:
        raise HTTPException(400, f"{body.account} {BANNED[body.account]}，无法使用")
    tab = used_tab(tid, me)
    if (d := tab.driver) and d.running:
        raise HTTPException(409, "Claude 正在运行，请在本轮结束后切换账号")
    remember_account(tab, body.account)
    if d and getattr(d, "entry_account", d.account) != body.account:
        tab.drop_driver()
    return tab.brief()


class ModelBody(BaseModel):
    model: str | None = None


@app.get("/api/tabs/{tid}/info", dependencies=[Depends(full_only)])
async def session_info(tid: str, connect: bool = True, me: Who = Depends(who)):
    """会话信息面板：模型、账号、上下文明细、MCP 状态等。会话没在跑的话，这里会先把 CLI 连起来。
    connect=0（前端悬停时用）：只取已经连着的，没连着就回 409，不为了看一眼去启动 CLI。"""
    tab = seen_tab(tid, me)
    if workflows.is_run(tab.sid):
        raise HTTPException(409, "工作流记录不通过网页重新启动，请查询任务状态")
    # 只能看的人（别人的公开会话）不许为了看信息去启动 CLI；已经连着的照样能看
    if connect and (why := users.use_why(me, tab.sid)):
        if not (tab.driver and tab.driver.client):
            raise HTTPException(403, f"{why}；会话当前未连接，无法查看信息")
        connect = False
    if connect:
        await asyncio.to_thread(check_paid, me, tab.sid, tab.account)   # 连起来就是用这个号启动 CLI；要算余额，不在事件循环里算
        d = await tab.live_driver()
    elif not ((d := tab.driver) and d.client):
        raise HTTPException(409, "会话未连接")
    try:
        return await d.info()
    except Exception as e:
        raise HTTPException(500, f"获取会话信息失败：{e}")


# 输入框打「/」时弹出的指令清单：CLI 连上时把它在网页对话（SDK）里认得的指令交给查看器（初始化结果里的 commands），
# 和终端里能用的不完全一样，以它为准。清单跟着 CLI 的版本、各人的自定义指令和技能变，会话没连着时用最近一次见到的（存在数据目录，重启后照样有）
COMMANDS_FILE = DATA / "slash_commands.json"
try:
    slash_seen = json.loads(COMMANDS_FILE.read_text())
except (OSError, ValueError):
    slash_seen = {}


def _commands_of(raw) -> list[dict]:
    """CLI 给的指令（一项一个 {name, description, argumentHint}，个别版本只给名字）整理成前端要的 {name, desc, hint}"""
    out = []
    for c in raw or []:
        if isinstance(c, str) and c.strip("/"):
            out.append({"name": c.strip().lstrip("/"), "desc": "", "hint": ""})
        elif isinstance(c, dict) and str(c.get("name") or "").strip("/"):
            out.append({"name": str(c["name"]).strip().lstrip("/"), "desc": str(c.get("description") or ""),
                        "hint": str(c.get("argumentHint") or "")})
    return out


@app.get("/api/commands", dependencies=[Depends(full_only)])
async def slash_commands(tab: str | None = None, me: Who = Depends(who)):
    """可用的指令。tab 给了、那个会话的 CLI 正连着，就当场取它的（只读初始化时记下的结果，不发请求）并记下来；
    否则用最近一次见到的。一次都没见过（实例起来以后还没有会话连上过 CLI，以前也没记过）就回空清单和原因，不拿别的东西顶替"""
    global slash_seen
    d = seen_tab(tab, me).driver if tab else None
    if d and d.client:
        info = await d.client.get_server_info() or {}
        got = _commands_of(info.get("commands"))
        if got and got != slash_seen.get("commands"):
            slash_seen = {"commands": got, "ts": time.time()}
            # 交给写盘线程（存不下由它在服务输出里说，重启后要等会话连上 CLI 才有）：磁盘慢时同步写这一下卡过 5 秒
            diskq.rewrite(COMMANDS_FILE, json.dumps(slash_seen, ensure_ascii=False))
        if got:
            return {"commands": got, "live": True}
    if slash_seen.get("commands"):
        return {"commands": slash_seen["commands"], "live": False, "ts": slash_seen.get("ts")}
    return {"commands": [], "why": "尚未有会话连接过 Claude，暂时无法列出可用指令；发送任意消息后再输入「/」即可查看"}


@app.get("/api/tabs/{tid}/context")
async def context_map(tid: str, me: Who = Depends(who)):
    """上下文的组成，一层层拆到每个文件、每个气泡（拆法见 ctxmap.py），给上下文药丸悬停卡片里的环形图。
    系统部分用 CLI 给的明细：连着就用刚取的，没连着用上次连着时记下的（stale 标出来），都没有就和开头几条合成一块估。
    不为了看一眼去启动 CLI；只读模式、只能看的人也能看（只读会话记录）。叶子上的记录行编号换成条目序号，前端点了能跳过去"""
    tab = seen_tab(tid, me)
    d = tab.driver
    if d and d.client and d.usage:
        tab.cli_usage = d.usage
    usage = tab.cli_usage
    try:
        tree = await asyncio.to_thread(ctxmap.cached, tab.session.path, usage, tab.cwd())
    except Exception as e:
        raise HTTPException(500, f"上下文分析失败：{e}")
    return {**with_seq(tree, tab.session.seq_of), "stale": bool(usage) and not (d and d.client and d.usage is usage)}


def with_seq(n: dict, seq: dict) -> dict:
    """环形图的树：叶子上的记录行编号（u）换成条目序号（s），前端点了能跳到那个气泡；子代理的行不在主会话的条目里，没有序号"""
    out = {k: v for k, v in n.items() if k not in ("c", "u")}
    if n.get("u") in seq:
        out["s"] = seq[n["u"]]
    if n.get("c"):
        out["c"] = [with_seq(c, seq) for c in n["c"]]
    return out


@app.get("/api/tabs/{tid}/spend")
async def spend_map(tid: str, me: Who = Depends(who)):
    """这个会话花了多少、花在哪（拆法见 spendmap.py），给上下文药丸卡片的「花费」视角：第一圈是缓存写入、缓存命中、输出，
    往外是哪一轮、哪次请求、具体花在什么上。t 是点数，前端换成分。叶子的记录行编号换成条目序号，点了能跳过去"""
    tab = seen_tab(tid, me)
    try:
        own = await asyncio.to_thread(users.own_calls, tab.session.path)
        tree = await asyncio.to_thread(spendmap.cached, tab.session.path, own, tab.cwd())
    except Exception as e:
        raise HTTPException(500, f"花费分析失败：{e}")
    return with_seq(tree, tab.session.seq_of)


# ---------- 审稿看板（见 review_board.py）：自动审稿的会话点开的整页面板，只读 ----------

def review_ws(tab: Tab, ws: str) -> str | None:
    """看哪个工作区：只认这个会话的 review.py 打印过的（reviewWs），不给就是最近那个；一个都没有是 None"""
    known = tab.session.info.get("reviewWs") or []
    if not ws:
        return known[-1] if known else None
    if ws not in known:
        raise HTTPException(400, f"这个会话没有审过这个工作区：{ws}")
    return ws


def recent_said(tab: Tab, n: int = 8) -> list[dict]:
    """会话里 AI 最近说的几段话（不算思考、子代理、重复的行），看板上「AI 最近说的话」"""
    out = []
    for it in reversed(tab.session.items):
        if it.get("kind") == "text" and not (it.get("thought") or it.get("sidechain") or it.get("dup")) and (it.get("text") or "").strip():
            out.append({"ts": it.get("ts"), "text": it["text"][:4000]})
            if len(out) >= n:
                break
    return out[::-1]


@app.get("/api/tabs/{tid}/review")
async def review_board_get(tid: str, ws: str = "", me: Who = Depends(who)):
    tab = seen_tab(tid, me)
    path = review_ws(tab, ws)
    out = {"known": tab.session.info.get("reviewWs") or [], "said": recent_said(tab), "board": None}
    if path:
        try:
            out["board"] = await asyncio.to_thread(review_board.board, path, getattr(tab.session.path, "run_as", None))
        except OSError as e:
            raise HTTPException(500, f"读不到审稿工作区 {path}：{e}")
    return out


@app.get("/api/tabs/{tid}/review/diff")
async def review_board_diff(tid: str, v: str, ws: str = "", me: Who = Depends(who)):
    """某一版和上一版差在哪（看板上点开某一版时取）"""
    tab = seen_tab(tid, me)
    if not (path := review_ws(tab, ws)):
        raise HTTPException(400, "这个会话还没有送审过")
    try:
        return await asyncio.to_thread(review_board.version_diff, path, v, getattr(tab.session.path, "run_as", None))
    except OSError as e:
        raise HTTPException(500, f"读不到这一版：{e}")


@app.get("/api/tabs/{tid}/review/notes")
async def review_board_notes(tid: str, ws: str = "", me: Who = Depends(who)):
    """小铃铛.md 全文（看板上平时只给末尾，点「看全部」时取）"""
    tab = seen_tab(tid, me)
    if not (path := review_ws(tab, ws)):
        raise HTTPException(400, "这个会话还没有送审过")
    try:
        return {"text": await asyncio.to_thread(review_board.notes_full, path, getattr(tab.session.path, "run_as", None))}
    except OSError as e:
        raise HTTPException(500, f"读不到 小铃铛.md：{e}")


@app.post("/api/tabs/{tid}/model", dependencies=[Depends(full_only)])
async def set_model(tid: str, body: ModelBody, me: Who = Depends(who)):
    """换网页对话用的模型；Claude 正连着就当场切换，否则下次发话时生效。"""
    tab = used_tab(tid, me)
    if body.model and tab.account in RELAY:
        if body.model not in api_models(tab.account):
            raise HTTPException(400, f"{tab.account} 只接入了 {'、'.join(api_models(tab.account))}")
    elif body.model and model_family(body.model) != account_family(tab.account):
        names = {"claude": "Claude", "gpt": "GPT", "deepseek": "DeepSeek", "glm": "智谱", "gemini": "Gemini"}
        kind, other = names.get(account_family(tab.account), account_family(tab.account)), names[model_family(body.model)]
        raise HTTPException(400, f"{tab.account} 是 {kind} 账号，不支持 {body.model}；如需使用，请先切换到 {other} 账号")
    if why := rates.unpriced(rates.billing_model(body.model, tab.account)):   # 没有单价的模型不许用；中转站按号的单价查
        raise HTTPException(400, why)
    await asyncio.to_thread(check_paid, me, tab.sid, tab.account)   # 要把 CLI 连起来才能换；要算余额，不在事件循环里算
    d = await tab.live_driver()
    try:
        await d.set_model(body.model)
        tab.model = body.model
    except Exception as e:
        raise HTTPException(500, f"切换失败：{e}")
    return tab.brief()


@app.get("/api/tabs/{tid}/items")
def get_items(tid: str, since: int = 0, gen: str = "", tail: int = 0, start: int = -1, end: int = -1,
              me: Who = Depends(who)):
    """三种取法：
    - 默认：接着 since 往后取（前端已经有前面的了）
    - start/end：往上补一段更早的（前端翻到顶的时候用）
    - tail：第一次打开、或者换代了，只取末尾这些条。
      长会话的首屏就不用等着几千条一起画出来了
    """
    tab = seen_tab(tid, me)
    s = tab.session
    if start >= 0:
        stop = len(s.items) if end < 0 else max(start, min(end, len(s.items)))
        return {**tab.brief(), "start": start, "items": s.items[start:stop]}
    # 先读一下记录新写的：内存里的条目只在后台循环那一圈才更新，可快照里「在不在跑」是从 CLI 那边现取的，
    # 一轮刚结束时不读就会给出「已经不跑了、回复还没有」
    s.poll()
    at = since if gen == s.gen else (max(0, len(s.items) - tail) if tail else 0)
    return {**tab.brief(), "start": at, "items": s.items[at:]}


class PushGap(BaseModel):
    id: str
    kind: str = "behind"     # behind 心跳说服务器推得比前端收到的多；stale 整段拉取的回复比已经收到的推送旧，没拿来覆盖
    have: int | None = None  # 前端收到的推送编号（见 Tab.push_n）
    want: int | None = None  # behind 是心跳里的编号，stale 是那份拉取回复带的编号
    n: int | None = None     # 前端手上一共几条


@app.post("/api/push_gap")
def push_gap(body: PushGap, me: Who = Depends(who)):
    """前端对账时发现前后端没对上，只记一笔日志（补拉由前端自己做），查漏推、查拉取和推送交错用。"""
    tab = tabs.get(body.id)
    if not tab or not users.can_see(me, tab.sid):
        return {"ok": False}
    pushed = tab.pushed[1] if tab.pushed else None
    what = (f"前端收到第 {body.have} 次推送、服务器已推到第 {body.want} 次，前端补拉一次" if body.kind == "behind"
            else f"整段拉取的回复是第 {body.want} 次推送时的、比前端已收到的第 {body.have} 次旧，没拿来覆盖")
    print(f"推送对账：{me.name or '游客'} 的会话 {tab.sid}：{what}（前端 {body.n} 条，服务器推过 {pushed} 条）", flush=True)
    return {"ok": True}


def snippet(text: str, at: int, n: int, around: int = 46):
    """命中的那一小段，前后各留一点上下文。"""
    start, stop = max(0, at - around), min(len(text), at + n + around)
    return ("…" if start else "") + text[start:stop].replace("\n", " ") + ("…" if stop < len(text) else "")


# 找话的筛选分类：用户说的（含斜杠命令）、Claude 的回复、思考、工具（调用和结果）、其他
SEARCH_GROUPS = ("user", "text", "thinking", "tool", "other")
KIND_GROUP = {"user": "user", "command": "user", "text": "text", "thinking": "thinking", "tool": "tool", "result": "tool"}
# 带字的思考块是说给用户听的过渡话（见 session.py），能搜到字的思考块就算回复
BLOCK_GROUP = {"text": "text", "thinking": "text", "tool_use": "tool"}


def line_group(line: bytes, q: str) -> str:
    """全部会话里命中的一行记录算哪一类。助手的一行里有好几块时，看词落在哪一块里。"""
    try:
        d = json.loads(line)
    except ValueError:
        return "other"
    content = (d.get("message") or {}).get("content")
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    if d.get("type") == "user" and not d.get("isMeta"):
        return "tool" if any(b.get("type") == "tool_result" for b in blocks) else "user"
    if d.get("type") == "assistant":
        for b in blocks:
            if q in json.dumps(b, ensure_ascii=False):
                return BLOCK_GROUP.get(b.get("type"), "other")
    return "other"


def file_hits(data: bytes, needle: bytes, q: str) -> Counter:
    """一个会话文件里每一类各有几行命中。只解析含有这个词的那几行，其余的不碰。"""
    counts, at = Counter(), data.find(needle)
    while at >= 0:
        start, end = data.rfind(b"\n", 0, at) + 1, data.find(b"\n", at)
        end = len(data) if end < 0 else end
        counts[line_group(data[start:end], q)] += 1
        at = data.find(needle, end)
    return counts


@app.get("/api/search")
def search(q: str, tid: str = "", limit: int = 80, kinds: str = "", me: Who = Depends(who)):
    """找文字。给了会话编号就在那个会话里精确找到第几条；不给就扫一遍全部会话，看哪些里面有。
    kinds 是要看的分类（逗号隔开，见 SEARCH_GROUPS），不给就是全部；counts 是每一类各命中多少，不受筛选影响。

    大小写敏感：中文用不上转换，英文精确匹配也更符合找代码、找路径的直觉。
    """
    q = q.strip()
    if not q or (len(q) < 2 and q.isascii()):   # 中文一个字也有得找，英文单个字母命中太多
        raise HTTPException(400, "英文至少两个字母，中文至少一个字")
    want = set(kinds.split(",")) & set(SEARCH_GROUPS) if kinds else set(SEARCH_GROUPS)

    if tid:
        tab = seen_tab(tid, me)
        hits, counts = [], Counter()
        for i, it in enumerate(tab.session.items):
            text = it.get("text") or ""
            if it.get("kind") == "tool":
                text = f"{it.get('name', '')} {json.dumps(it.get('input', {}), ensure_ascii=False)}"
            at = text.find(q)
            if at >= 0:
                group = KIND_GROUP.get(it.get("kind"), "other")
                counts[group] += 1
                if group in want and len(hits) < limit:
                    hits.append({"seq": i, "kind": it.get("kind"), "ts": it.get("ts"),
                                 "text": snippet(text, at, len(q))})
        shown = sum(counts[g] for g in want)
        return {"hits": hits, "more": shown > len(hits), "counts": counts, "total": len(tab.session.items)}

    needle = q.encode()
    found, counts = [], Counter()
    for d in project_dirs(me):
        if not d.is_dir() or d.name == PROBE_PROJECT:
            continue
        for p in children(d, "*.jsonl"):
            if workflows.is_run(p.stem):
                continue
            if not users.can_see(me, p.stem) or users.is_deleted(p.stem):   # 看不到的、删掉的会话，里面的话也搜不出来
                continue
            try:
                data = p.read_bytes()
                if needle not in data:
                    continue
                st = p.stat()
            except OSError:
                continue
            per = file_hits(data, needle, q)
            counts.update(per)
            if n := sum(per[g] for g in want):
                found.append({**card_of(p, st.st_mtime, st.st_size), "hits": n})
    found.sort(key=lambda s: (-s["hits"], -s["mtime"]))
    return {"sessions": found[:limit], "counts": counts}


@app.get("/api/tabs/{tid}/image/{key}")
def get_image(tid: str, key: str, me: Who = Depends(who)):
    """会话里的图片（贴的、工具结果里的）。条目里只带编号，看到了、点开了才来取这一张，
    服务器这时才回会话记录里读（Session.read_image），而且能被浏览器缓存住。"""
    try:
        got = seen_tab(tid, me).session.read_image(key)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        raise HTTPException(500, f"无法从会话记录中读取该图片（{type(e).__name__}: {e}），请刷新页面后重试")
    if not got:
        raise HTTPException(404, "图片不存在")
    media, raw = got
    return Response(raw, media_type=media, headers={"Cache-Control": "public, max-age=604800, immutable"})


# 回复里能引用的服务器图片（前端 markdown.js 认的格式和这里一致）
IMAGE_FILES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".svg": "image/svg+xml"}


@app.get("/api/tabs/{tid}/file")
def get_file(tid: str, path: str, download: bool = False, check: bool = False, me: Who = Depends(who)):
    """回复和示意图里引用的服务器上的图片（Claude 用脚本画好存成文件的图），相对路径按会话的工作目录算。
    看得到这个会话才给，而且只给图片（按软链接指向的真文件认），不然谁都能借它把服务器上的登录凭据之类下载走。
    文件会被重新画，不让浏览器长存；svg 里能带脚本，单独打开这个地址时不能让它以查看器的身份跑，所以加 sandbox。
    download=1 是回复里的下载链接（见 download_file），check=1 只查能不能下、不回文件内容。"""
    tab = used_tab(tid, me) if download else seen_tab(tid, me)
    p = tab_path(tab, path)
    if not p.is_absolute():
        if not (cwd := tab.cwd()):
            raise HTTPException(400, f"会话记录中缺少工作目录，无法解析相对路径：{path}")
        p = tab_path(tab, cwd) / p
    p = p.resolve()
    if download:
        return download_file(tab, p, me, check)
    if not (media := IMAGE_FILES.get(p.suffix.lower())):
        raise HTTPException(403, f"仅支持查看图片（{'、'.join(IMAGE_FILES)}）：{p}")
    if not p.is_file():
        raise HTTPException(404, f"文件不存在：{p}")
    headers = {"Content-Security-Policy": "sandbox"}
    if hostfs.SPLIT or isolation.for_sid(tab.sid):
        try:
            return Response(p.read_bytes(), media_type=media, headers=headers)
        except OSError as e:
            raise HTTPException(500, f"无法读取该文件：{e}")
    return FileResponse(p, media_type=media, headers=headers)


# 网页上传的文件放在运行 Claude 的那个用户的家目录下、按会话分开：Claude 读得到，又不弄乱项目目录。
# 目录名不带点：下载不许经过隐藏目录（见 download_file），传上去的文件也要能原样下载回来
UPLOADS = hostfs.HOME / "cc_uploads"
MAX_UPLOAD = 50 << 20    # 单个文件的上限（50MB）：公网入口 cloud 的 nginx 只收 50MB 以内的请求（client_max_body_size 50m），再大从公网传不上来
NAME_BYTES = 150         # 文件名留多长（按 UTF-8 字节，前面还要加时间）


def upload_dir(tab) -> Path:
    env = isolation.for_sid(tab.sid)
    return (env.path(env.home) / "cc_uploads" if env else UPLOADS) / tab.sid


def size_text(n: int) -> str:
    return f"{n / (1 << 20):.1f} MB" if n >= 1 << 20 else f"{n / 1024:.1f} KB" if n >= 1024 else f"{n} 字节"


def clean_name(name: str) -> str:
    """只留文件名本身：去掉目录和控制字符；开头的点去掉，不然成了隐藏文件、下载不回来；太长的截短，保留扩展名。"""
    name = "".join(c for c in name.replace("\\", "/").rsplit("/", 1)[-1] if c.isprintable()).strip().lstrip(".")
    stem, ext = (name.rsplit(".", 1)[0], "." + name.rsplit(".", 1)[1]) if "." in name else (name, "")
    if len(ext.encode()) > 20:   # 不像扩展名，整个当名字截
        stem, ext = name, ""
    while stem and len((stem + ext).encode()) > NAME_BYTES:
        stem = stem[:-1]
    return (stem + ext) or "文件"


def open_new(folder, name: str):
    """在上传目录里新建一个文件（时间打头，同一秒重名的加 -2、-3），不覆盖任何已有的文件。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    for k in range(1, 100):
        p = folder / (f"{stamp}-{name}" if k == 1 else f"{stamp}-{stem}-{k}{dot}{ext}")
        try:
            return p.open("xb"), p
        except FileExistsError:
            continue
    raise HTTPException(500, f"上传目录中同一秒内的同名文件过多：{folder}")


@app.post("/api/tabs/{tid}/upload", dependencies=[Depends(full_only)])
async def upload(tid: str, name: str, request: Request, me: Who = Depends(who)):
    """网页上传一个文件（请求体就是文件内容），存进这个会话的上传目录，回给前端存在哪；发话时由 say 告诉 Claude。
    能往会话里发话的人才能传。写入以运行 Claude 的用户的身份（hostfs），文件归它，Claude 读得到。"""
    tab = used_tab(tid, me)
    try:
        size = int(request.headers["content-length"])
    except (KeyError, ValueError):
        raise HTTPException(411, f"「{name}」：请求中缺少文件大小")
    if size > MAX_UPLOAD:
        raise HTTPException(413, f"「{name}」大小为 {size_text(size)}，超过上限 {size_text(MAX_UPLOAD)}")
    folder = upload_dir(tab)
    try:
        await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
        f, p = await asyncio.to_thread(open_new, folder, clean_name(name))
    except OSError as e:
        raise HTTPException(500, f"「{name}」：无法在上传目录 {folder} 中创建文件：{e}")
    got = 0
    with f:
        try:
            async for chunk in request.stream():
                got += len(chunk)
                if got > size:
                    raise HTTPException(400, f"「{name}」：收到的数据超过声明的 {size_text(size)}，已停止接收（已收到的部分保留在 {p}）")
                await asyncio.to_thread(f.write, chunk)
        except ClientDisconnect:
            raise HTTPException(400, f"「{name}」上传至 {size_text(got)} 时连接中断（已收到的部分保留在 {p}）")
        except OSError as e:
            raise HTTPException(500, f"「{name}」写入至 {size_text(got)} 时出错：{e}（已收到的部分保留在 {p}）")
    if got != size:
        raise HTTPException(400, f"「{name}」仅收到 {size_text(got)}，应为 {size_text(size)}（已收到的部分保留在 {p}）")
    # 回解析过软链接的真路径：和发话时告诉 Claude 的（upload_note）是同一个写法
    return {"path": str(await asyncio.to_thread(p.resolve)), "name": p.name, "size": got}


def upload_note(tab, files: list[str]) -> str:
    """发话时附在话后面，告诉 Claude 这次传了哪些文件、放在哪。只认这个会话上传目录里真有的文件，别的路径当场报错。"""
    folder = upload_dir(tab).resolve()
    lines = []
    for f in files:
        p = tab_path(tab, f).resolve()
        if p.parent != folder:
            raise HTTPException(400, f"该文件不是此会话上传的：{f}")
        try:
            lines.append(f"{p}（{size_text(p.stat().st_size)}）")
        except OSError as e:
            raise HTTPException(400, f"找不到上传的文件：{f}（{e}）")
    return f"（通过网页上传了 {len(lines)} 个文件）\n" + "\n".join(lines)


def download_file(tab, p, me: Who, check: bool):
    """回复里的下载链接。查看器账号什么都能下；别人只能下这个会话工作目录和上传目录里的文件，
    而且不能经过隐藏目录、不能是隐藏文件：工作目录常常就是家目录，各号的登录凭据、ssh 钥匙都在点开头的目录里。"""
    if not me.admin:
        roots = [tab_path(tab, r).resolve() for r in (tab.cwd(), str(upload_dir(tab))) if r]
        rel = next((p.relative_to(r) for r in roots if p.is_relative_to(r)), None)
        if rel is None:
            raise HTTPException(403, f"仅可下载此会话工作目录和上传目录中的文件：{p}")
        if any(part.startswith(".") for part in rel.parts):
            raise HTTPException(403, f"不提供隐藏文件及隐藏目录中文件的下载（其中常存放登录凭据）：{p}")
    try:
        if not p.is_file():
            raise HTTPException(404, f"文件不存在：{p}")
        size = p.stat().st_size
    except OSError as e:
        raise HTTPException(500, f"无法查看该文件信息：{p}（{e}）")
    if check:
        return {"name": p.name, "size": size}
    media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(p.name)}", "Content-Length": str(size),
               "Content-Security-Policy": "sandbox"}
    try:
        f = p.open("rb")   # 经小助手打开（同一个用户时就是自己开），读还在这边，大文件也不用整个读进内存
    except OSError as e:
        raise HTTPException(500, f"无法打开该文件：{p}（{e}）")

    def chunks():
        with f:
            while data := f.read(1 << 20):
                yield data
    return StreamingResponse(chunks(), media_type=media, headers=headers)


@app.get("/api/browse")
def browse(path: str = "", environment: str = "shared", me: Who = Depends(who)):
    """列出目录下的子目录和 .jsonl 会话，隐藏的也列（按修改时间倒序，附带会话标题）。
    看不到的会话不列，以它的编号命名的目录（里面是它的子代理记录）也不列。
    这是以运行 Claude 的系统用户的身份去列的，谁都能列哪里都行的话，服务器上的目录结构就全露出去了：
    游客不能用；不是查看器账号的只能列这个用户家目录里面的（按软链接指向的真目录认），新建会话挑工作目录够用了。"""
    if not me.name:
        raise HTTPException(401, "请登录后浏览文件夹")
    env = private_call(isolation.select, me.name, environment)
    folder = env.path(path or "~") if env else hostfs.Path(path or config.root or "~").expanduser()
    home = env.path(env.home) if env else hostfs.HOME
    if not folder.is_dir():
        raise HTTPException(400, f"不是文件夹：{folder}")
    if not me.admin and not folder.resolve().is_relative_to(home.resolve()):
        raise HTTPException(403, f"仅可浏览 {home} 中的文件夹：{folder}")
    # 直接输入路径进到看不到的会话的子代理目录里，也不行（不是会话编号的目录名查不到主人，照常放行）
    check_see(me, folder.parent.name if folder.name == "subagents" else folder.name)
    entries = []
    for p in folder.iterdir():
        try:
            if not (p.is_dir() or p.suffix == ".jsonl"):
                continue
            st = p.stat()
        except OSError:   # 读不了的（比如掉线的挂载目录）就跳过，不让整个列表出错
            continue
        is_dir = p.is_dir()
        if not users.can_see(me, p.name if is_dir else users.sid_of(p)):
            continue
        entries.append({"name": p.name, "path": str(p), "dir": is_dir,
                        "mtime": st.st_mtime, "size": st.st_size,
                        "title": "" if is_dir else read_title(p, st.st_mtime),
                        **({"spent": {}, "spentPaid": {}} if is_dir else spend_fields(p))})
    entries.sort(key=lambda x: (not x["dir"], -x["mtime"]))
    return {"path": str(folder), "parent": str(folder.parent), "entries": entries[:300]}


@lru_cache(maxsize=1024)
def read_title(path: Path, mtime: float, tail=64_000):
    """从文件末尾找会话标题：手动改的名字优先，其次是最近的自动标题，都没有就用最后一句用户说的话（按修改时间缓存）。"""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - tail))
        lines = f.read().decode("utf-8", "ignore").splitlines()
    ai_title = fallback = ""
    for line in reversed(lines):
        if '"custom-title"' in line or '"ai-title"' in line or '"last-prompt"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("customTitle"):
                return d["customTitle"]
            ai_title = ai_title or d.get("aiTitle", "")
            fallback = fallback or d.get("lastPrompt", "")[:60]
    return ai_title or fallback


# 会话文件 → 找到的工作目录。会话记录只往后追加，第一条写了工作目录的行找到以后就不会变，
# 不用再按修改时间重读：正在聊的会话每回一句修改时间都变，推送摘要（brief）每圈都要它，以前每变一次就在事件循环上重读一遍文件开头，
# 磁盘慢的时候一次能卡两秒多（看门线程记到过）；配了 run_as 时连 stat 都是跨用户调用
_cwd_found: dict = {}


def read_cwd(path: Path, mtime: float):
    """从文件开头往下找第一条写了工作目录的行，用来给工作区起名、续接对话。找到了就一直记着；没找到的按修改时间缓存，文件变了再读。"""
    if hit := _cwd_found.get(path):
        return hit
    if cwd := _read_cwd(path, mtime):
        _cwd_found[path] = cwd
    return cwd


@lru_cache(maxsize=256)
def _read_cwd(path: Path, mtime: float):
    """不能只读开头固定的一截：第一条消息贴了长文时，开头那几行（排队记录、消息本身）能有几十 KB，
    截断的行解析不了，就会把明明写着的工作目录当成没有。正常会话几行之内就找到了。"""
    with path.open("rb") as f:
        for line in f:
            if b'"cwd"' in line:
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    continue
                if cwd:
                    return cwd
    return ""


# 测试工作区：agent 起测试时真的拉起 Claude Code 的会话，工作目录都放在这下面（README「测试工作区」）。
# 全部会话和最近会话里默认不显示，前端「显示测试会话」打开才显示；属主、花费照常记。
# 启动时经过的路径和解析软链接后的路径都认（实例可能是从一个软链接起的），只比字符串，不去碰 Claude 那边的文件
TEST_WORKSPACES = sorted({str(HERE.absolute() / "tests" / "scratchpad"), str(HERE.resolve() / "tests" / "scratchpad")})


def is_test_cwd(cwd: str) -> bool:
    """工作目录是不是测试工作区或它下面的子目录。整段比，tests/scratchpad-x 这种隔壁目录不算。"""
    return any(cwd == root or cwd.startswith(root + "/") for root in TEST_WORKSPACES)


# 目录路径 → (上次看到的目录修改时间, 里面的文件清单)。
# 目录的修改时间只在增删文件时才变，会话写入新内容不会变，所以清单可以放心留着用
_listing: dict[Path, tuple[float, list[Path]]] = {}


def children(folder: Path, pattern: str) -> list[Path]:
    """目录里符合条件的东西；目录没动过就直接用上次列好的清单，不再翻一遍磁盘。"""
    try:
        mtime = folder.stat().st_mtime
    except OSError:
        return []
    hit = _listing.get(folder)
    if hit and hit[0] == mtime:
        return hit[1]
    found = sorted(folder.glob(pattern))
    _listing[folder] = (mtime, found)
    return found


@lru_cache(maxsize=4096)
def session_card(path: Path, mtime: float, size: int):
    """看板上的一张会话卡片。按文件的修改时间和大小缓存：文件没变就不用再去读标题。"""
    return {"id": tab_id(path), "path": str(path), "title": read_title(path, mtime), "mtime": mtime, "size": size}


@lru_cache(maxsize=4096)
def blocked_empty(path: Path, mtime: float, size: int) -> bool:
    """第一句话就被提交前的钩子拦下、此后再没人说过话的会话：记录里只有排队和一条拦下的系统提示，没有标题，
    列表里会显示成文件名（实例卡住时新开的会话可能这样）。会话列表跳过它们；
    对话里那一行「这句话没发出去」照样看得到（session.on_prompt_blocked），浏览文件夹也照样列。
    这种会话只有一两 KB，大的不用读（按文件的修改时间和大小缓存）。"""
    if size > 16_000:
        return False
    blocked = False
    try:
        with path.open("rb") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") in ("user", "assistant"):
                    return False
                blocked = blocked or str(d.get("content") or "").startswith(BLOCKED_HEAD)
    except OSError:
        return False
    return blocked


def spend_fields(path: Path, since: float | None = None, snap: calls.Snapshot | None = None) -> dict:
    """会话标题前的总消耗：spent 全部、spentPaid 用户付的金额（含远端已结算的展示），spentBlue 由预定付掉的（见 users.spending），
    前端换算成点数，免费号的、照常扣钱的、预定付掉的各用各的颜色写。since、snap 见 users.spending。"""
    sp = users._sum_rows(users.api_rows(path.stem, since)) if str(path).startswith("api-session/") else users.spending(path, since, snap)
    # 从这个会话派出去的工作流任务（ccagent、ccrun、包装 SDK）也算进来：子代理是它的工具，花的钱该显示在它头上。
    # 只加在展示上，账本照旧按各自的会话记一次；任务会话本身不出现在会话列表里，不会被算两遍
    if not str(path).startswith("api-session/") and Path(path).parent.name != "subagents":
        for kid in workflows.runs_of(Path(path).stem):
            if p := calls.find_in(snap, kid):
                sp = users.add_spending(sp, users.spending(p, since, snap))
    return {"spent": sp["all"], "spentPaid": users.display_paid(sp), "spentBlue": sp["blue"]}


def card_of(path: Path, mtime: float, size: int, snap: calls.Snapshot | None = None):
    """卡片再盖一层：主人自己起过名字的就显示那个名字，再带上打的标签、会话的主人和可见范围、总消耗。
    这几样会变而文件不一定变（总消耗还看调用记录：哪个号发的、算在哪个会话名下），所以不进按文件缓存的 session_card。"""
    if str(path).startswith("api-session/"):
        rec = api_gateway._ACTIVE.conversations.get(path.stem)
        return {**api_gateway.api_sessions.card(rec), **spend_fields(path, snap=snap)}
    card = session_card(path, mtime, size)
    name, sid = titles.get(card["id"]), users.sid_of(path)
    tid_sid[card["id"]] = sid
    return {**card, "tags": tags.get(card["id"], []), **({"title": name} if name else {}),
            **users.share(sid), **spend_fields(path, snap=snap)}


def mtime_size(p: Path, snap: calls.Snapshot) -> tuple[float, int]:
    """会话文件的修改时间和大小：清点里有就用清点的，没有再去查（查不了照样抛 OSError）。"""
    if hit := snap.stats.get(str(p)):
        return hit[1], hit[0]
    st = p.stat()
    return st.st_mtime, st.st_size


def recent_since(mins: float | None) -> float | None:
    """按最近 N 分钟用量排序时，从哪个时刻算起；没给 N 就是 None。谁都能用（普通用户也能用，
    全部会话里各人只看得到自己看得到的会话的用量）。N 最多 30 天，再长和总消耗没什么区别，还白白多扫。"""
    if mins is None:
        return None
    if not 0 < mins <= 30 * 1440:
        raise HTTPException(400, "分钟数须大于 0 且不超过 43200")
    users.refresh_calls()
    return time.time() - mins * 60


@app.get("/api/overview")
def overview(mins: float | None = None, mine: int = 0, me: Who = Depends(who)):
    """家目录 .claude/projects 下的所有会话（这个人看得到的），按工作区分组，组内和组间都是最近更新的在前。
    给了 mins 时，每张卡片再带上最近这么多分钟的用量（recent，格式同 spent / spentPaid），按它怎么排、怎么分组由前端做。
    在查看器里删掉的会话不进分组；这个人能恢复的（主人和查看器账号，见 users.delete_why）另外放在 deleted 里，最近删的在前。
    mine=1 只列自己名下的（简洁模式侧边栏的「最近」只列自己的）：卡片上的总消耗每张都要算，
    查看器账号看得到全组几百个会话，以前算完了前端再扔掉别人的。游客没有名下的会话，照旧全列"""
    since = recent_since(mins)
    only = me.name if mine and me.name else None
    groups, trash = [], []
    # 几百个会话连同子代理的文件大小和修改时间一趟清点好，下面各处都从这里拿（配了 run_as 时每查一个文件都要经小助手跑一个来回）
    snap = calls.snapshot(project_roots(me))
    for d in project_dirs(me):
        if not d.is_dir() or d.name == PROBE_PROJECT:
            continue
        sessions = []
        for p in children(d, "*.jsonl"):
            if workflows.is_run(p.stem):
                continue
            if not users.can_see(me, p.stem) or (only and users.owner_of(p.stem) != only):
                continue
            try:
                mtime, size = mtime_size(p, snap)
            except OSError:   # 刚被删掉的，跳过就好
                continue
            if blocked_empty(p, mtime, size):
                continue
            if users.is_deleted(p.stem):
                if not users.delete_why(me, p.stem):
                    trash.append({**card_of(p, mtime, size, snap), "deleted": users.deleted.get(p.stem, {})})
                continue
            card = card_of(p, mtime, size, snap)
            if since is not None:
                card["recent"] = spend_fields(p, since, snap)
            sessions.append(card)
        if not sessions:
            continue
        sessions.sort(key=lambda s: -s["mtime"])
        newest = isolation.open_path(sessions[0]["path"], me.name, me.admin)
        cwd = read_cwd(newest, sessions[0]["mtime"])
        # test：这一组是测试工作区里的会话（一个目录就是一个工作目录，按组标就够了），前端默认藏起来
        groups.append({"name": cwd or d.name, "sessions": sessions, "test": is_test_cwd(cwd)})
    external = api_gateway.listed_cards(only or (None if me.admin else me.name)) if me.name else []
    for c in external:
        c.update(spend_fields(Path(c["path"])))
        if since is not None:
            c["recent"] = spend_fields(Path(c["path"]), since)
    if external:
        groups.append({"name": "API 会话", "sessions": external, "test": False})
    groups.sort(key=lambda g: -g["sessions"][0]["mtime"])
    trash.sort(key=lambda s: -s["deleted"].get("ts", 0))
    # testWorkspace：前端见到它才出「显示测试会话」开关（旧后端不给组上的 test，开关开关都一样，就不出）
    return {"groups": groups, "deleted": trash, "testWorkspace": TEST_WORKSPACES[0]}


SIDE_PAGE_MAX = 200   # 侧边栏一页最多要多少行


# 漏斗的几项（前端 app.js 的 sideStateMenu）→ 算进去的状态（side_states 给的）
SIDE_FILTERS = {"unread": {"unread"}, "attention": {"limit", "error"}, "run": {"run"}}


def side_rows(me: Who, tests: int, snap: calls.Snapshot) -> list[tuple]:
    """侧边栏「最近」列的会话，按最近更新排好（一样时按路径）：[(-修改时间, 路径字符串, 修改时间, 大小, 路径, 工作区, 是不是测试会话)]。
    筛法和前端原来对 /api/overview 的筛法一样：只列主人是自己的（游客是无主的），测试会话 tests=1 才列，
    在查看器里删掉的、第一句话就被钩子拦下的不列。分页（side_page）和列标签（side_tags）都从这里拿"""
    rows = []
    for d in project_dirs(me):
        if not d.is_dir() or d.name == PROBE_PROJECT:
            continue
        mine = []
        for p in children(d, "*.jsonl"):
            if workflows.is_run(p.stem):
                continue
            # 用 owner_of 不用 share：share 是发给前端的，单用户实例里不写主人，按它判断「最近」就永远是空的
            if not users.can_see(me, p.stem) or users.owner_of(users.sid_of(p)) != me.name:
                continue
            try:
                mtime, size = mtime_size(p, snap)
            except OSError:   # 刚被删掉的，跳过就好
                continue
            if blocked_empty(p, mtime, size) or users.is_deleted(p.stem):
                continue
            mine.append((mtime, size, p))
        if not mine:
            continue
        # 测试工作区按组判断，和 /api/overview 一样从这一组最新的会话里读工作目录
        newest = max(mine, key=lambda r: r[0])
        cwd = read_cwd(isolation.open_path(str(newest[2]), me.name, me.admin), newest[0])
        test = is_test_cwd(cwd)
        if test and not tests:
            continue
        rows += [(-m, str(p), m, s, p, cwd or d.name, test) for m, s, p in mine]
    if me.name:
        rows += [(-c["mtime"], c["path"], c["mtime"], 0, Path(c["path"]), "API 会话", False)
                 for c in api_gateway.listed_cards(me.name)]
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


@app.get("/api/side")
def side_page(limit: int = 50, before: str = "", tests: int = 0, state: str = "", tag: str = "", me: Who = Depends(who)):
    """简洁模式侧边栏的「最近」，一页页给（用户反馈开侧边栏卡）：以前一次把名下全部会话连同总消耗交给前端、前端全画出来，
    主人名下八百多个，开侧边栏、换会话都要重画几百行。现在按最近更新排好（side_rows），只给 before 后面的 limit 个，
    总消耗也只给这几张卡片算，前端滚到哪儿再要下一页。before 是上一页最后一行的「修改时间:路径」；
    翻页的工夫里有会话更新了会挪到最上面，前端重新取第一页时拿到。more：后面还有没给的。
    state、tag 是「最近」右边漏斗和标签两个筛选：只列这个状态的（SIDE_FILTERS）、带这个标签的，
    在这里筛好再分页，没取到的那几页里符合的也不会漏"""
    if not 0 < limit <= SIDE_PAGE_MAX:
        raise HTTPException(400, f"每页行数须在 1 到 {SIDE_PAGE_MAX} 之间")
    if state and state not in SIDE_FILTERS:
        raise HTTPException(400, f"不支持「{state}」状态筛选")
    cut = None
    if before:
        t, _, at = before.partition(":")
        try:
            cut = (-float(t), at)
        except ValueError:
            raise HTTPException(400, "翻页位置格式错误，应为「修改时间:路径」")
    snap = calls.snapshot(project_roots(me))
    rows = side_rows(me, tests, snap)
    if state:
        want, states = SIDE_FILTERS[state], side_states(me)
        rows = [r for r in rows if states.get(users.sid_of(r[4])) in want]
    if tag:
        rows = [r for r in rows if tag in tags.get(tab_id(r[4]), ())]
    # 置顶的（side_pin）从分页里拿出来，第一页（没有 before）一次全给，照样按最近更新排、照样受两个筛选管
    mine = set(pins.get(me.name, ())) if me.name else set()
    top = [r for r in rows if tab_id(r[4]) in mine]
    if top:
        rows = [r for r in rows if tab_id(r[4]) not in mine]
    if cut is not None:
        rows = [r for r in rows if (r[0], r[1]) > cut]
    card = lambda r: {**card_of(r[4], r[2], r[3], snap), "ws": r[5], "test": r[6]}
    out = {"items": [card(r) for r in rows[:limit]], "more": len(rows) > limit}
    if cut is None:
        out["pinned"] = [{**card(r), "pinned": True} for r in top]
    return out


@app.get("/api/side/tags")
def side_tags(tests: int = 0, me: Who = Depends(who)):
    """「最近」右边标签图标点开的那张单子：侧边栏列得出来的会话上打过的全部标签，连同各有几个会话，多的在前。"""
    counts: dict[str, int] = {}
    for r in side_rows(me, tests, calls.snapshot(project_roots(me))):
        for name in tags.get(tab_id(r[4]), ()):
            counts[name] = counts.get(name, 0) + 1
    return {"tags": [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]}


SIDE_RANK = {"run": 3, "limit": 2, "error": 1}   # 同一个会话经几条路径开着时取最要紧的那个；有未读（unread）排在它们后面

# 侧边栏的未读（干完了留一颗不闪的绿点）：turn_done 是会话编号 → 网页对话最后一轮干完的时刻（after_turn 记），
# seen_at 是人 → 会话编号 → 最后一次看它的时刻（前端正看着哪个会话、页面在前台时报上来，哪台设备都算，见 side_seen）。
# 干完比看晚，就是有这个人还没看过的内容。存在数据目录，重启后照样在；只算网页对话，终端里的会话本来就是盯着终端看的
SEEN_FILE = DATA / "side_seen.json"
try:
    _seen = json.loads(SEEN_FILE.read_text())
except (OSError, ValueError):
    _seen = {}
turn_done: dict[str, float] = _seen.get("done", {})
seen_at: dict[str, dict[str, float]] = _seen.get("seen", {})


def save_seen():
    # 页面每看一下、每干完一轮都要存，都在事件循环里：交给写盘线程（卡顿记录里最多的几处之一）
    diskq.rewrite(SEEN_FILE, json.dumps({"done": turn_done, "seen": seen_at}))


def side_state(t: Tab) -> str | None:
    """侧边栏状态点（和前端 sessions.js 的 sideDot 对开着的标签页的判断一样）：run 正在干活（含查看器在核对额度），
    limit 停在额度上（被拦下、或这一轮停在到达限额上），error 上一轮出了错；闲着是 None。"""
    d = t.driver
    if (d and d.running) or t.prep:
        return "run"
    if (d and d.blocked) or limit_stop(t):
        return "limit"
    if d and d.error:
        return "error"
    return None


@app.get("/api/side/status")
async def side_status(me: Who = Depends(who)):
    """侧边栏的状态点：自己名下（筛法同 /api/side）、此刻不闲着的会话，会话编号 → run / limit / error / unread。
    简洁模式一次只开一个会话，别的会话前端没有标签页、不知道状态，所以由这里给；侧边栏开着时前端每几秒问一次。
    在事件循环里跑，驱动的状态都是在这里改的，读到的是一致的"""
    return {"states": await asyncio.to_thread(side_states, me)}


def side_states(me: Who) -> dict[str, str]:
    """自己名下此刻不闲着的会话 → 状态（见 side_status）。只看内存里的标签页和驱动，不碰文件；
    没打开过、已经释放了的会话没有网页对话在跑，算闲着。分页按状态筛（side_page，在线程池里）也用它，只读，读到半截的也就差一拍"""
    out: dict[str, str] = api_gateway._ACTIVE.conversations.states(me.name)
    for t in list(tabs.values()):
        if users.owner_of(t.sid) != me.name or not users.can_see(me, t.sid) or users.is_deleted(t.sid):
            continue
        if (st := side_state(t)) and SIDE_RANK[st] > SIDE_RANK.get(out.get(t.sid), 0):
            out[t.sid] = st
    # 闲着的里面有没看过的内容：unread。游客不记看过没有，也就不给
    mine = seen_at.get(me.name, {}) if me.name else None
    for sid, at in list(turn_done.items()) if mine is not None else ():
        if sid in out or at <= mine.get(sid, 0):
            continue
        if users.owner_of(sid) == me.name and users.can_see(me, sid) and not users.is_deleted(sid):
            out[sid] = "unread"
    return out


class Seen(BaseModel):
    id: str   # 标签页编号（tab_id）


@app.post("/api/side/seen")
async def side_seen(body: Seen, me: Who = Depends(who)):
    """前端报上来：这个人此刻正看着这个会话（正在看的那个、页面在前台；换到它、切回页面、它这一轮干完时都报一次）。
    记下时刻，比它早干完的就不算未读。游客不记"""
    t = tabs.get(body.id)
    if not t:
        raise HTTPException(404, "服务器未载入该会话，请重新打开后重试")
    if not users.can_see(me, t.sid):
        raise HTTPException(403, users.hidden_why(me))
    if me.name:
        seen_at.setdefault(me.name, {})[t.sid] = time.time()
        save_seen()
        if err := diskq.error(SEEN_FILE):   # 这次交出去的还没写，报的是上一次写这个文件时的错
            raise HTTPException(500, f"无法记录已读状态：{err}")
    return {"ok": True}


# 侧边栏置顶（手机上往右划一行露出主题色的底，松手就钉住或取消）：人 → 置顶的会话编号（tab_id）。
# 跟着网页账号存，哪台设备都一样，各人只管自己的；游客不能置顶。删掉的、不归自己了的照样留在单子里，side_rows 列不出来也就不显示
PINS_FILE = DATA / "session_pins.json"
try:
    pins: dict[str, list[str]] = json.loads(PINS_FILE.read_text())
except (OSError, ValueError):
    pins = {}


class Pin(BaseModel):
    id: str        # 会话编号（侧边栏卡片的 id）
    pinned: bool


@app.post("/api/side/pin")
async def side_pin(body: Pin, me: Who = Depends(who)):
    """置顶或取消置顶侧边栏里的一个会话，只动 session_pins.json，只读模式也能改。"""
    if not me.name:
        raise HTTPException(403, "请登录后置顶会话")
    old = pins.get(me.name, [])
    mine = [i for i in old if i != body.id]
    pins[me.name] = [body.id, *mine] if body.pinned else mine
    try:
        await saved(diskq.rewrite_wait(PINS_FILE, json.dumps(pins)), "置顶")
    except HTTPException:
        pins[me.name] = old   # 没存上就当没改，免得重启前后不一样；写盘线程排着重试的是没撤回的那份，换成撤回后的
        diskq.rewrite(PINS_FILE, json.dumps(pins))
        raise
    return {"ok": True}


# ---------- 用户（见 users.py）----------

class Cred(BaseModel):
    name: str
    password: str


def with_login(response: Response, token: str, me: Who) -> dict:
    """发 cookie：只给服务器看（httponly），30 天；局域网是 http，不加 secure。"""
    response.set_cookie(users.COOKIE, token, max_age=users.LOGIN_DAYS * 86400, httponly=True, samesite="lax")
    return users.me_info(me)


@app.get("/api/me")
def get_me(me: Who = Depends(who)):
    # devices：这个实例有没有「设备」页（开了 ssh 门卫、对话模式），登录了才给入口。personas：「人设卡」页，对话模式就有（游客也能看、能选公开的）
    # bottomTabs：留不留底部标签页栏（账号级的偏好，同 /api/settings 那一项）。
    # 跟着身份给，是因为页面恢复标签页之前就要知道（没开时只恢复正在看的那一个，见 js/tabs.js 的 load），设置是另一个请求、不等它
    # todos：侧边栏的「待办」入口（todos.py），对话模式、登录了才有（待办按网页账号存，游客没有）
    return {**users.me_info(me), "devices": devices.ENABLED and not config.readonly and bool(me.name),
            "personas": not config.readonly, "todos": not config.readonly and bool(me.name),
            "bottomTabs": None if config.readonly else users.bottom_tabs(me)}


# ---------- 头像（见 users.set_avatar、js/avatar.js）：只有本人取得到、换得了自己的 ----------

@app.get("/api/avatar")
def get_avatar(me: Who = Depends(who)):
    """自己的头像。地址带着版本（?v=），换了头像地址就变，所以能长缓存；只给本人（private）"""
    got = users.avatar_file(me)
    if not got or not got[0].is_file():
        raise HTTPException(404, "未设置头像")
    return FileResponse(got[0], media_type=got[1], headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.put("/api/avatar")
async def put_avatar(request: Request, me: Who = Depends(who)):
    """换头像：请求体就是图片。边收边数，超过上限当场拒收，不先整个读进内存"""
    if int(request.headers.get("content-length") or 0) > users.AVATAR_MAX:
        raise HTTPException(413, f"头像不能超过 {users.AVATAR_MAX // 1024} KB")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > users.AVATAR_MAX:
            raise HTTPException(413, f"头像不能超过 {users.AVATAR_MAX // 1024} KB")
    return {"avatar": await asyncio.to_thread(users.set_avatar, me, bytes(data))}


@app.delete("/api/avatar")
def delete_avatar(me: Who = Depends(who)):
    """恢复成文字头像"""
    return {"avatar": users.set_avatar(me, None)}


# ---------- 人设卡（见 personas.py）：建、改、公开、删、设默认；新建会话时选一张 ----------

def persona_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except personas.Bad as e:
        raise HTTPException(400, str(e))


class PersonaFile(BaseModel):
    name: str
    text: str | None = None   # 新加的带全文
    sha: str | None = None    # 留着原来的（或者复制别人的卡时沿用）只交指纹，几十 MB 的资料不用来回传


class PersonaScript(BaseModel):
    name: str
    text: str


class PersonaBody(BaseModel):
    name: str | None = None
    text: str | None = None
    public: bool | None = None
    files: list[PersonaFile] | None = None       # 补充资料，整份替换；不给就是不改
    hooks: dict | None = None                    # 钩子，写法同 settings.json 的 hooks（见 personas.py），整份替换
    scripts: list[PersonaScript] | None = None   # 钩子用的脚本，整份替换


class PersonaDefault(BaseModel):
    id: str | None = None   # 空：新建会话默认不用人设卡


@app.get("/api/personas")
def persona_list(me: Who = Depends(who)):
    return personas.overview(me.name)


@app.post("/api/personas", dependencies=[Depends(full_only)])
def persona_new(body: PersonaBody, me: Who = Depends(who)):
    return persona_call(personas.create, me.name, body.name or "", body.text or "", bool(body.public),
                        [f.model_dump() for f in body.files or []], body.hooks, [s.model_dump() for s in body.scripts or []])


@app.get("/api/personas/{cid}")
def persona_get(cid: str, me: Who = Depends(who)):
    """一张卡连脚本的全文（列表里只有脚本名和字数）：编辑、复制成自己的、第一次用之前弹窗给人看的时候取。"""
    return persona_call(personas.card, me.name, cid)


@app.post("/api/personas/default", dependencies=[Depends(full_only)])
def persona_default(body: PersonaDefault, me: Who = Depends(who)):
    persona_call(personas.set_default, me.name, body.id or None)
    return personas.overview(me.name)


@app.put("/api/personas/{cid}", dependencies=[Depends(full_only)])
def persona_edit(cid: str, body: PersonaBody, me: Who = Depends(who)):
    return persona_call(personas.update, me.name, cid, body.name, body.text, body.public, admin=me.admin,
                        files=None if body.files is None else [f.model_dump() for f in body.files], hooks=body.hooks,
                        scripts=None if body.scripts is None else [s.model_dump() for s in body.scripts])


@app.delete("/api/personas/{cid}", dependencies=[Depends(full_only)])
def persona_delete(cid: str, me: Who = Depends(who)):
    return {"name": persona_call(personas.delete, me.name, cid)}


# ---------- 待办（清单入口，见 todos.py）：侧边栏「待办」页列出登记的清单文件、快速新建、改几样（攒成操作，AI 下次读清单时写进文件）、勾选几条发到新会话 ----------

def todo_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except todos.Bad as e:
        raise HTTPException(400, str(e))


def todo_tag(user: str, sid: str, group: str):
    """分组名当会话标签（todos.on_take）：认领了有分组的待办、待办换了分组时，给正在做它的会话加上这个标签，原有的不动。
    找不到会话记录、标签已经满了就记一行日志：待办照样认领、改好，标签是顺带的"""
    p = calls.find(sid)
    if not p:
        print(f"待办分组「{group}」没能打到会话 {sid} 上：找不到它的会话记录", flush=True)
        return
    tid = tab_id(p)
    have = tags.get(tid, [])
    if group in have:
        return
    if len(have) >= 20:   # clean_tags 最多留 20 个
        print(f"待办分组「{group}」没能打到会话 {sid} 上：它已经有 20 个标签", flush=True)
        return
    tid_sid[tid] = sid
    remember_tags(tid, have + [group])


todos.on_take = todo_tag


def todo_me(me: Who) -> str:
    if not me.name:
        raise HTTPException(403, "待办只给登录的用户用")
    return me.name


class TodoBody(BaseModel):
    """新建、修改：给了的字段才动（exclude_unset），null 是清掉。重要性、时限、耗时是 {v, src}，网页上点的 src 是 user"""
    title: str | None = None
    group: str | None = None
    parent: str | None = None
    level: int | None = None
    status: str | None = None
    importance: dict | None = None
    deadline: dict | None = None
    effort: dict | None = None
    summary: str | None = None
    body: str | None = None
    dir: str | None = None


def todo_view(me: Who) -> dict:
    """待办页要的全部：分组、每条待办（日志换成算好的会话链接，不整本给）、挂着的会话各自的卡片和此刻的状态点。
    会话卡片和侧边栏「最近」同一种（card_of）；找不到、看不到的写 missing，被删了的写 deleted。
    正在做它的会话被删了就放手（记进日志），页面上不再挂在「正在做」底下。
    共用的待办（todos_shared）里另一个实例上的会话这边找不到，写 elsewhere（那个实例的端口），页面上照实写在哪"""
    user = todo_me(me)
    book = todo_call(todos.overview, user)
    marks = {x["sid"]: x.get("at", "") for it in book["items"].values() for v in todos.links(it).values() for x in v}
    cards = {sid: {"sid": sid, "missing": True, "elsewhere": at} for sid, at in marks.items() if at and at != todos.HERE}
    sids = set(marks) - set(cards)
    snap = calls.snapshot(project_roots(me)) if sids else None
    states = side_states(me) if sids else {}
    gone = []
    for sid in sids:
        p = calls.find_in(snap, sid)
        if not p or not users.can_see(me, sid):
            cards[sid] = {"sid": sid, "missing": True}
            continue
        try:
            mtime, size = mtime_size(p, snap)
        except OSError:
            cards[sid] = {"sid": sid, "missing": True}
            continue
        deleted = users.is_deleted(sid)
        cards[sid] = {**card_of(p, mtime, size, snap), "sid": sid, "state": states.get(sid), "deleted": deleted}
        if deleted:
            gone.append(sid)
    for sid in gone:
        if any(w["sid"] == sid for it in book["items"].values() for w in it["working"]):
            todo_call(todos.drop_session, user, sid, "会话已删除")
            book = todo_call(todos.overview, user)
    items = [{**{k: v for k, v in it.items() if k != "log"}, "links": todos.links(it)} for it in book["items"].values()]
    # lists：登记的几份清单（在哪、哪份默认、读不出来或格式不对的原因）；pending：网页上攒着、还没写进文件的操作有几条（见 todos.py 开头）
    return {"groups": book["groups"], "items": items, "sessions": cards, "lists": book.get("lists", []), "pending": book.get("pending", 0),
            "max": {"title": todos.MAX_TITLE, "group": todos.MAX_GROUP, "summary": todos.MAX_SUMMARY}}


@app.get("/api/todos")
def todo_list(me: Who = Depends(who)):
    return todo_view(me)


@app.post("/api/todos", dependencies=[Depends(full_only)])
def todo_new(body: TodoBody, me: Who = Depends(who)):
    it = todo_call(todos.add, todo_me(me), body.model_dump(exclude_unset=True))
    return {**{k: v for k, v in it.items() if k != "log"}, "links": todos.links(it)}


@app.put("/api/todos/{tid}", dependencies=[Depends(full_only)])
def todo_edit(tid: str, body: TodoBody, me: Who = Depends(who)):
    it, _ = todo_call(todos.edit, todo_me(me), tid, body.model_dump(exclude_unset=True))
    return {**{k: v for k, v in it.items() if k != "log"}, "links": todos.links(it)}


@app.post("/api/register")
def register(body: Cred, response: Response):
    """注册完直接登录。只用独立环境的人（users.is_new，刚注册的名下还没有会话，都是）顺手建好独立环境：他们的界面上只有这一种环境。
    没建成（系统组件出错、超时）注册照样算数，记一笔日志；网页打开时会再建一次，还不成就在新会话标签页上写明原因、不让发话，
    不会改用共用环境（js/sessions.js 的 autoEnvironment）。"""
    token = users.register(body.name, body.password)
    me = users.identify(token)
    if users.is_new(me.name) and isolation.enabled():
        try:
            isolation.create(me.name)
        except (ValueError, OSError, TimeoutError) as e:
            print(f"新用户 {me.name} 注册时没建成独立环境：{type(e).__name__}: {e}", flush=True)
    return with_login(response, token, me)


@app.post("/api/login")
def login(body: Cred, response: Response):
    token = users.login(body.name, body.password)
    return with_login(response, token, users.identify(token))


@app.post("/api/logout")
def logout(request: Request, response: Response):
    users.logout(request.cookies.get(users.COOKIE))
    response.delete_cookie(users.COOKIE)
    return users.me_info(users.GUEST)


class PasswordBody(BaseModel):
    old: str
    new: str


@app.post("/api/password")
def change_password(body: PasswordBody, request: Request, response: Response):
    """改自己的密码。别的设备上的登录随之失效，这台换一张新的凭据接着用。"""
    me = who(request)
    token = users.change_password(me, body.old, body.new)
    return with_login(response, token, me)


@app.get("/api/users")
def list_users(me: Who = Depends(who)):
    """全部用户名：选可见范围、查看器账号换主人时用。游客用不上，也不给看。"""
    if not me.name:
        raise HTTPException(401, "请登录后查看用户列表")
    return {"users": users.names()}


def session_titles(sids) -> dict[str, dict]:
    """账本里列会话用：会话编号 → 标题和路径（找不到文件的只给编号）。"""
    out = {}
    for sid in sids:
        hit = calls.find(sid)
        if hit:
            with contextlib.suppress(OSError):
                out[sid] = {"title": titles.get(tab_id(hit)) or read_title(hit, hit.stat().st_mtime), "path": str(hit)}
    return out


def with_titles(led: dict) -> dict:
    names = session_titles(s["sid"] for s in led["sessions"] if s.get("source") != "api")
    key_names = {"api-" + k["id"]: k["name"] for k in api_gateway._ACTIVE.keys.list(led["name"])}
    for s in led["sessions"]:
        if s.get("source") == "api":
            rec = api_gateway._ACTIVE.conversations.get(s["sid"])
            s["title"] = "API · " + (rec["key_name"] if rec else key_names.get(s["sid"], "历史密钥"))
            if rec:
                s["path"] = "api-session/" + s["sid"] + ".jsonl"
        else:
            s.update(names.get(s["sid"], {}))
    return led


@app.get("/api/ledger")
def my_ledger(me: Who = Depends(who)):
    """自己的账：收到的每一笔、名下每个会话在限量号上花了多少、余额。"""
    if not me.name:
        raise HTTPException(401, "游客没有账本，请登录后查看")
    return with_titles(users.ledger(me.name))


# ---------- 账号页的几张图（stats.py，前端 js/stats.js）----------

@app.get("/api/balance-history")
def balance_history(days: float = 30, me: Who = Depends(who)):
    """余额最近 days 天的曲线，每小时一个点，最后一点就是此刻的余额（和账本同一个数）"""
    if not me.name:
        raise HTTPException(401, "游客没有账本，请登录后查看")
    return stats.balance_series(me.name, max(1.0, min(days, 31)))


@app.get("/api/owned")
def owned_accounts(me: Who = Depends(who)):
    """自己是号主的号：起算以后自己和别人各在上面花了多少、几个人用过（号的额度、到期时间前端从额度接口拿）"""
    if not me.name:
        raise HTTPException(401, "请登录后查看")
    return {"accounts": stats.owned(me.name)}


@app.get("/api/usage-stats")
def usage_stats(days: float = 3, user: str | None = None, me: Who = Depends(who)):
    """用量统计页：user 不给是自己，all 是所有人，查看器账号还能写某个人。花得最多的几个会话配上名字（看得到的才配）"""
    out = stats.usage(me, days, user)
    top = out["sessions"].get("top") or []
    names = session_titles(s["sid"] for s in top if not s["sid"].startswith("api-"))
    for s in top:
        s["title"] = "API 调用" if s["sid"].startswith("api-") else (names.get(s["sid"]) or {}).get("title") or ""
    return out


@app.get("/api/board")
def balance_board(mins: float | None = None):
    """余额看板：除查看器账号以外每个人的余额，按余额从多到少。谁都能看（游客也能），不含账本明细。
    给了 mins 就再带上每人最近这么多分钟的用量，前端拿来排序（和用户管理一样）。"""
    return {"users": users.board(recent_since(mins)), "overdraft": users.OVERDRAFT}


@app.get("/api/admin/users")
def admin_users(mins: float | None = None, me: Who = Depends(who)):
    """用户名单和每人的账；给了 mins 就再带上每人（和无主会话）最近这么多分钟的用量。"""
    users.need_admin(me)
    return users.roster(recent_since(mins))


TRAFFIC_ALERT_PER_HOUR = 1024 ** 3   # 一个会话一小时里往外传超过这么多就标红（出过一个会话一小时空转传掉好几 GB 的事）


def cost_audit_access(account: str, me: Who):
    if not me.name or not (me.admin or owns(me.name, account)):
        raise HTTPException(403, '只有号主或查看器账号可以查看平台账单')
    if not platform_costs.enabled(account):
        raise HTTPException(404, '此账号尚未开启平台账单查询')


@app.get('/api/platform-costs/{account}')
def get_platform_costs(account: str, limit: int = 100, me: Who = Depends(who)):
    cost_audit_access(account, me)
    try:
        return JSONResponse(platform_costs.report(account, limit), headers={'Cache-Control': 'no-store'})
    except platform_costs.CostError as exc:
        raise HTTPException(400, str(exc)) from None
    except (OSError, platform_costs.sqlite3.Error) as exc:
        raise HTTPException(503, f'平台账单存盘读不了（{type(exc).__name__}）') from None


@app.post('/api/platform-costs/{account}/refresh')
def refresh_platform_costs(account: str, me: Who = Depends(who)):
    cost_audit_access(account, me)
    if config.readonly:
        raise HTTPException(403, '只读实例不能同步平台账单')
    try:
        platform_costs.sync(account)
        return JSONResponse(platform_costs.report(account), headers={'Cache-Control': 'no-store'})
    except platform_costs.CostError as exc:
        raise HTTPException(502, str(exc)) from None
    except (OSError, platform_costs.sqlite3.Error) as exc:
        raise HTTPException(503, f'平台账单同步存不下来（{type(exc).__name__}）') from None


@app.get('/platform-costs')
def platform_costs_page(account: str, me: Who = Depends(who)):
    cost_audit_access(account, me)
    return FileResponse(HERE / 'static/platform-costs.html', headers={'Cache-Control': 'no-store'})


@app.get("/api/admin/traffic")
def admin_traffic(hours: float = 24, me: Who = Depends(who)):
    """流量（查看器账号）：网络闸门记下的每条连接（gate.py「数字节」、数据目录的 traffic.jsonl），最近 hours 小时按会话、按号加起来。
    按会话：这个会话的 CLI 自己的连接（GPT 号到本机翻译层的那段也算，量的就是它发出去的请求）；按号：真正出网的部分
    （不算本机那段，翻译层去 chatgpt.com 的算在号上，分不到会话）。某个小时传得特别多的会话标出来（alert）。
    文件读不了要报错，不能当成没有流量。"""
    users.need_admin(me)
    since = time.time() - hours * 3600
    path = DATA / "traffic.jsonl"
    sess: dict[str, dict] = {}
    accts: dict[str, dict] = {}
    if path.exists():   # 还没记过一条（新装的闸门还没有连接结束过）是真的没有
        try:
            with open(path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("t", 0) < since:
                        continue
                    if not r.get("local"):
                        a = accts.setdefault(r.get("account") or "?", {"account": r.get("account"), "up": 0, "down": 0, "n": 0})
                        a["up"] += r["up"]
                        a["down"] += r["down"]
                        a["n"] += 1
                    if sid := r.get("session"):
                        s = sess.setdefault(sid, {"sid": sid, "account": r.get("account"), "up": 0, "down": 0, "n": 0, "hours": {}})
                        s["up"] += r["up"]
                        s["down"] += r["down"]
                        s["n"] += 1
                        h = int(r["t"] // 3600)
                        s["hours"][h] = s["hours"].get(h, 0) + r["up"]
        except OSError as e:
            raise HTTPException(500, f"无法读取流量记录 {path}：{e}")
    names = session_titles(sess)
    out = []
    for s in sess.values():
        peak = max(s.pop("hours").values(), default=0)
        out.append({**s, **names.get(s["sid"], {}), "owner": users.owner_of(s["sid"]), "peakHour": peak,
                    "alert": peak >= TRAFFIC_ALERT_PER_HOUR})
    out.sort(key=lambda x: -x["up"])
    return {"hours": hours, "sessions": out, "accounts": sorted(accts.values(), key=lambda x: -x["up"]),
            "alertPerHour": TRAFFIC_ALERT_PER_HOUR}


@app.get("/api/admin/ledger")
def admin_ledger(name: str, me: Who = Depends(who)):
    users.need_admin(me)
    return with_titles(users.ledger(name))


class GrantBody(BaseModel):
    user: str
    amount: float
    note: str = ""


@app.post("/api/admin/grant")
def admin_grant(body: GrantBody, me: Who = Depends(who)):
    """发额度（负数就是扣回来），单位是点（5 小时额度的百分之几）。
    发完前端自己重取名单（要带上最近 N 分钟的用量），这里只回这一笔。"""
    return users.grant(me, body.user, body.amount, body.note)


class GrantPlanBody(BaseModel):
    user: str
    amount: float
    note: str = ""
    parts: int = users.PLAN_PARTS
    days: float = users.PLAN_DAYS
    start: float | None = None   # 开始时刻（unix 秒），不给就是现在；可以是以前


@app.post("/api/admin/grant-plan")
def admin_grant_plan(body: GrantPlanBody, me: Who = Depends(who)):
    """额度计划（users.grant_plan）：总额分几期发，从开始时刻发第一期，之后每隔几天到期一期。单位同发额度，是点。
    界面上只填总额和开始日期，期数和间隔用默认的（4 期、每 7 天）。回这份计划，前端自己重取名单。"""
    return users.grant_plan(me, body.user, body.amount, body.note, body.parts, body.days, body.start)


class PlanIdBody(BaseModel):
    id: str


@app.post("/api/admin/grant-plan/revoke")
def admin_revoke_plan(body: PlanIdBody, me: Who = Depends(who)):
    """撤销额度计划：当作没发生过，已经发了的几期一起收回（users.revoke_plan）。"""
    return users.revoke_plan(me, body.id)


@app.post("/api/admin/password")
def admin_password(body: Cred, me: Who = Depends(who)):
    """给忘了密码的人重设一个；这个人在所有设备上的登录都失效。"""
    users.reset_password(me, body.name, body.password)
    return {"ok": True}


class NewUserBody(BaseModel):
    name: str
    on: bool


@app.post("/api/admin/newuser")
def admin_newuser(body: NewUserBody, me: Who = Depends(who)):
    """查看器账号改某人只用独立环境（on）还是也能选共用环境（users.set_new；接口名沿用以前的新用户）。只改界面上怎么给他看：
    已经建好的独立环境、已有的会话都不动；改成只用独立环境时他还没有独立环境，下次打开网页会自动建。"""
    users.set_new(me, body.name, body.on)
    return {"ok": True, "newUser": users.is_new(body.name)}


# ---------- 用户电脑的安装脚本下载（见添加电脑的安装脚本 make-pc-setup.sh）----------
# 给用户电脑配反向通路时，Claude 在 Claude 那边的 ~/tunnels/dl/ 放一份随机名的安装脚本，用户在电脑上一行命令从这里下载运行，
# 不用钥匙、不用密码；公网入口（反向代理）对这个路径不要网页密码。给不了时回一段报错的脚本，用户那边运行就能看到原因。
# 几条做法：
# - 名字是 128 位随机数，60 分钟内能反复下载，不再「用一次就作废」：第一次没装成、Windows 脚本自己弹出管理员窗口重新下载，都不用再找 Claude。
# - 电脑边装边把进度和报错 POST 回 <名字>.log，和每次下载一起记进 Claude 那边的 ~/tunnels/dl/<名字>.log（以那边的身份追加），
#   会话里的 wait-pc.py 看着它，装好、出错、卡住都会叫醒 Claude，用户不用把输出复制回来。
# - Windows 的 SSH 服务装微软官方的 OpenSSH 安装包，也从这里下（~/tunnels/files/，不要名字，谁都能下），不再走常常卡住的 Windows Update。

PC_SETUP_NAME = re.compile(r"([0-9a-f]{32})\.(sh|ps1)")
PC_SETUP_LOG = re.compile(r"([0-9a-f]{32})\.log")
PC_SETUP_FILE = re.compile(r"OpenSSH-(Win64|ARM64)-v[0-9.]+\.msi|uninstall\.(sh|ps1)")   # 卸载脚本：设备页、用户自己卸都用它
PC_SETUP_SECS = 3600           # 链接多久内能下载
PC_SETUP_REPORT_SECS = 3 * 3600   # 电脑多久内能报进度：下载以后还要装一阵，链接快过期时下载的也要报得上来
PC_SETUP_LOG_MAX = 64 << 10    # 一个链接的进度记录最多这么大，免得被灌满
PC_SETUP_JUNK = re.compile(r"[\x00-\x1f\x7f]")


def pc_setup_error(kind: str, why: str) -> str:
    why = why.replace("'", "")   # 下面用单引号包着；电脑上的控制台不一定显示得了中文，所以说明用英文
    return f"echo '{why}' >&2; exit 1\n" if kind == "sh" else f"throw '{why}'\n"


def pc_setup_note(tok: str, src: str, text: str):
    """往这个链接的进度记录追加一行（时刻、谁记的、内容，用制表符隔开）。记不下来只写日志：不能因此不给下载。"""
    path = hostfs.HOME / "tunnels" / "dl" / f"{tok}.log"
    try:
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_size > PC_SETUP_LOG_MAX:
                print(f"用户电脑的安装进度 {tok} 记满了，这一行没记：{text[:200]}", flush=True)
                return
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.time():.0f}\t{src}\t{text}\n")
    except OSError as e:
        print(f"用户电脑的安装进度 {tok} 记不下来（{e}）：{text[:200]}", flush=True)


def pc_setup_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


@app.get("/pc-setup/{name}")
def pc_setup(name: str, request: Request):
    if PC_SETUP_FILE.fullmatch(name):
        try:
            body = (hostfs.HOME / "tunnels" / "files" / name).read_bytes()
        except FileNotFoundError:
            raise HTTPException(404, "文件不存在")
        except OSError as e:
            print(f"用户电脑要的 {name} 读不出来：{e}", flush=True)
            raise HTTPException(500, f"The viewer could not read {name} ({e.strerror})")
        print(f"用户电脑下载了 {name}（{pc_setup_ip(request)}）", flush=True)
        return Response(body, media_type="application/x-msi" if name.endswith(".msi") else "text/plain; charset=utf-8")
    m = PC_SETUP_NAME.fullmatch(name)
    if not m:
        raise HTTPException(404, "下载不存在")
    path = hostfs.HOME / "tunnels" / "dl" / name
    kind = "Windows" if m[2] == "ps1" else "Mac/Linux"
    try:
        if time.time() - path.stat().st_mtime > PC_SETUP_SECS:
            print(f"用户电脑的安装脚本 {name}：过期了，没给", flush=True)
            pc_setup_note(m[1], "web", f"expired\t{kind} {pc_setup_ip(request)}")
            return Response(pc_setup_error(m[2], "This link has expired (links work for 60 minutes). Ask Claude for a new command."),
                            media_type="text/plain; charset=utf-8")
        body = path.read_text()
    except FileNotFoundError:
        return Response(pc_setup_error(m[2], "This link does not exist. Ask Claude for a new command."),
                        media_type="text/plain; charset=utf-8")
    except OSError as e:   # 读不到不能当成没有：说清楚是查看器这边出错了
        print(f"用户电脑的安装脚本 {name} 读不出来：{e}", flush=True)
        return Response(pc_setup_error(m[2], f"The viewer could not read the setup script ({e.strerror}). Tell Claude."),
                        media_type="text/plain; charset=utf-8")
    pc_setup_note(m[1], "web", f"download\t{kind} {pc_setup_ip(request)}")
    print(f"用户电脑的安装脚本 {name} 下载走了（{pc_setup_ip(request)}）", flush=True)
    return Response(body, media_type="text/plain; charset=utf-8")


@app.post("/pc-setup/{name}")
async def pc_setup_report(name: str, request: Request):
    """电脑上的安装脚本报进度、报错：一次最多 5 行，每行最多 300 字，去掉控制字符后记进进度记录。"""
    m = PC_SETUP_LOG.fullmatch(name)
    if not m:
        raise HTTPException(404, "地址不存在")
    try:
        raw = (await request.body())[:4096]
    except ClientDisconnect:
        return Response(status_code=400)

    def note() -> int:
        try:
            if time.time() - (hostfs.HOME / "tunnels" / "dl" / f"{m[1]}.sh").stat().st_mtime > PC_SETUP_REPORT_SECS:
                return 410
        except FileNotFoundError:
            return 404
        except OSError as e:
            print(f"用户电脑的安装进度 {m[1]}：查不了链接（{e}）", flush=True)
            return 500
        for line in [x for x in raw.decode("utf-8", "replace").splitlines() if x.strip()][:5]:
            pc_setup_note(m[1], "pc", PC_SETUP_JUNK.sub(" ", line).strip()[:300])
        return 200

    code = await asyncio.to_thread(note)
    return Response("ok\n" if code == 200 else "no such link\n", status_code=code, media_type="text/plain; charset=utf-8")


# ---------- 「设备」页（见 devices.py）：每人自己的设备列表，添加电脑和服务器、测试、改名、暂停访问、删除 ----------

def device_user(me: Who) -> str:
    if not devices.ENABLED or config.readonly:
        raise HTTPException(404, "当前实例未启用 SSH 门卫，无设备页")
    if not me.name:
        raise HTTPException(401, "请登录后管理设备：设备登记在各用户名下")
    return me.name


def device_call(fn, *args):
    try:
        return fn(*args)
    except devices.DeviceError as e:
        raise HTTPException(400, str(e))


@app.get("/api/devices")
def device_list(me: Who = Depends(who)):
    # work：认得会话的工作设备（workdev.py）；旧后端没有这个键，前端就不出工作设备这一项
    return {**device_call(devices.overview, device_user(me)), "work": True}


@app.post("/api/devices/links")
def device_new_link(me: Who = Depends(who)):
    return device_call(devices.create_link, device_user(me))


@app.get("/api/devices/links/{tok}")
def device_link(tok: str, me: Who = Depends(who)):
    return device_call(devices.link_state, tok, device_user(me))


class ServerBody(BaseModel):
    name: str
    login: str
    host: str
    port: int = 22
    via: str | None = None


@app.post("/api/devices/servers")
def device_add_server(body: ServerBody, me: Who = Depends(who)):
    return device_call(devices.add_server, device_user(me), body.name, body.login, body.host, body.port, body.via)


@app.post("/api/devices/{name}/server")
def device_edit_server(name: str, body: ServerBody, me: Who = Depends(who)):
    return device_call(devices.edit_server, device_user(me), name, body.login, body.host, body.port, body.via)


@app.post("/api/devices/{name}/test")
def device_test(name: str, me: Who = Depends(who)):
    return device_call(devices.test, device_user(me), name)


@app.get("/api/devices/{name}/history")
def device_history(name: str, me: Who = Depends(who)):
    return {"rows": device_call(devices.history, device_user(me), name)}


class DeviceEdit(BaseModel):
    to: str | None = None        # 改名
    paused: bool | None = None   # 暂停访问
    note: str | None = None      # 备注


@app.post("/api/devices/{name}")
def device_edit(name: str, body: DeviceEdit, me: Who = Depends(who)):
    user = device_user(me)
    out = {}
    if body.paused is not None:
        out["device"] = device_call(devices.set_paused, user, name, body.paused)
    if body.note is not None:
        out["device"] = device_call(devices.set_note, user, name, body.note)
    if body.to is not None and body.to.strip() != name:
        out["text"] = device_call(devices.rename, user, name, body.to)
    return out


@app.delete("/api/devices/{name}")
def device_delete(name: str, cleanup: bool = True, me: Who = Depends(who)):
    return device_call(devices.remove, device_user(me), name, cleanup)


@app.get("/manifest.json")
def manifest(request: Request):
    """装到桌面（PWA）的清单：内容照 static/manifest.json，只是这台设备开了「桌面应用全屏打开」（cookie users.PWA_COOKIE 是 1）时 display 换成 fullscreen。
    这个开关是设备级的（以前跟着账号）：浏览器取清单带着 cookie（index.html 的 crossorigin="use-credentials"），
    前端在本地偏好里写这个 cookie；不让缓存，改了开关下次取就是新的。要写在下面挂静态文件之前，不然被静态文件先接走"""
    data = json.loads((HERE / "static" / "manifest.json").read_text(encoding="utf-8"))
    if request.cookies.get(users.PWA_COOKIE) == "1":
        data["display"] = "fullscreen"
    return JSONResponse(data, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")


def main():
    global Driver, MODES, ACCOUNTS, BANNED
    # 要在 uvicorn 配日志之前换掉：它的日志处理器起来时取的是当时的 sys.stderr
    sys.stdout, sys.stderr = StampedStream(sys.stdout), StampedStream(sys.stderr)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址，默认只让本机访问；对话模式能在这台机器上跑任意命令，别随便改成 0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=0.2, help="检查被订阅会话有没有新内容的间隔（秒）")
    ap.add_argument("--root", help="「浏览」默认打开的文件夹")
    ap.add_argument("--mode", choices=("readonly", "full"), default="readonly",
                    help="readonly（默认）：只看会话，不加载也不调用 Claude SDK；full：还能在网页里和 Claude 对话")
    ap.add_argument("--account", help="网页对话默认用的账号（账号表里的某个 cc 别名），不填就用启动 server 的 shell 所用的那个号")
    ap.add_argument("--proxy", help="server 自己的环境里写的代理，默认是 instance.json 的 proxy_port（没写就拒绝启动，除非 instance.json 打开了 allow_direct）；"
                                    "CLI 经网络闸门出网，配了代理而代理连不上就拒绝对话")
    ap.add_argument("--gate-port", type=int, help="网络闸门（gate.py）听的端口，默认 --port 加 1000")
    args = ap.parse_args()
    from instance import ALLOW_DIRECT, DEFAULT_PROXY_PORT, NO_PROXY
    if DEFAULT_PROXY_PORT is None and not ALLOW_DIRECT:   # 只读模式也查：没配代理一律不起，不用先证明哪种模式一定不出网
        ap.error(NO_PROXY)
    if DEFAULT_PROXY_PORT is None:
        print("注意：instance.json 打开了 allow_direct、没写 proxy_port，出网都不经代理，直连", flush=True)
    config.gate_port = args.gate_port or args.port + 1000
    config.port = args.port   # 人设卡接口的套接字没配地址时按端口起名（personas.start）
    todos.HERE = str(args.port)   # 共用的待办里记下会话是哪个实例上的（todos.py 开头）
    roots = [hostfs.Path(r).expanduser() for r in ([args.root] if args.root else ROOT_CANDIDATES)]
    config.interval = args.interval
    config.readonly = args.mode == "readonly"
    if not config.readonly:
        from chat import ACCOUNTS, BANNED, MODES, Driver, launched_account, use_proxy
        if DEFAULT_MODE not in MODES:   # instance.json 里写错了就停下，不能拿认不得的模式去拉 CLI
            ap.error(f"instance.json 的 default_mode 写的是 {DEFAULT_MODE}，不是认得的权限模式（{'、'.join(MODES)}）")
        if DEFAULT_EFFORT is not None and DEFAULT_EFFORT not in EFFORTS:   # 同上，认不得的一档 CLI 会拒绝启动
            ap.error(f"instance.json 的 default_effort 写的是 {DEFAULT_EFFORT}，不是认得的思考强度（{'、'.join(EFFORTS)}）")
        if DEFAULT_MODEL and (model_family(DEFAULT_MODEL) != "claude" or rates.unpriced(DEFAULT_MODEL)):   # 只给 Claude 号用，没有单价的会被拦下
            ap.error(f"instance.json 的 default_model 写的是 {DEFAULT_MODEL}，要写一个有单价的 Claude 模型")
        config.account = args.account or launched_account()   # 要在 use_proxy 去掉 CLAUDE_CONFIG_DIR 之前反查
        if config.account not in ACCOUNTS or config.account in BANNED:
            why = BANNED.get(config.account, "没有这个别名")
            ap.error(f"不能用的账号：{config.account}（{why}），用 --account 换一个")
        import chat
        args.proxy = args.proxy or chat.PROXY
        use_proxy(args.proxy)
        Driver.on_limit = record_limit   # 网页对话收到限额事件就记进额度记录
        Driver.on_turn = after_turn      # 一轮结束按会话文件补记每次调用是哪个号发的，再自动查一次限额百分比
        Driver.on_mid = note_mid         # 一发出请求就记下这次调用是哪个号发的
        Driver.on_think = record_think   # 每次请求前面的思考跳数
        Driver.on_lap = record_lap       # 每一轮各步的时刻
        Driver.on_guard = guard          # CLI 发请求之前（各个钩子）的预测拦截
        Driver.on_name = name_nudge      # 聊了一阵子还没有用户起的名字：提醒 AI 起一个，只提醒一次（naming.py）
        Driver.on_check = check          # 每次请求一发出再核一次，已经没有能用的就拦下
        Driver.on_upstream_limit = upstream_limit   # 上游报了限额：记下、当场重查这个号的额度
        chat.GATE_REQUIRED = True        # 网络闸门没起来就不许拉起 CLI
    config.root = str(next((r for r in roots if r.is_dir()), hostfs.HOME))
    mode = "只读模式" if config.readonly else f"可对话模式，默认账号 {config.account}，代理 {args.proxy or '不用（直连）'}"
    print(f"喵～打开 http://{args.host}:{args.port} 就能看会话啦（{mode}）", flush=True)
    # Python 3.12 下 uvicorn 关停时要等所有连接断开，Ctrl+C 按几次都不管用。推送连接看到 should_exit 会自己结束；
    # 万一还有没断的，等 2 秒就掐掉，接着照常收尾（收掉网页对话的 CLI）
    global server
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning",
                                           timeout_graceful_shutdown=2))
    with contextlib.suppress(KeyboardInterrupt):   # 收尾后 uvicorn 会把 Ctrl+C 再抛一次，和 uvicorn.run 一样吞掉
        server.run()


if __name__ == "__main__":
    main()
