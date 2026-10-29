"""手动刷新额度：在 tmux 里用 cc 别名打开一次交互界面，只输入 /usage（官方命令，显示套餐用量限额），
抄下屏幕解析出 5 小时和本周的已用百分比、重置时间，合并写进 quota/<号>.json，然后关掉。

不发任何消息，不花额度。CLI 2.1.296 起每跑一次 /usage 都会在这个目录对应的会话目录里留下一个只有这条命令的小会话，
查看器的会话列表和找话都跳过那个目录（server.PROBE_PROJECT）。经 bash -i -c 启动（~/.bashrc 里有和号同名的别名就用别名，没有就直接运行 claude），配了代理就在读完 .bashrc 之后换成它（instance.proxy_port）。
交互界面在一个专用的空目录（.usage_cwd）里打开。第一次进这个目录会问「信任这个文件夹吗」，这是安全确认，
脚本不替用户回答：遇到就停下来报错，请用户在终端里自己确认一次。
抄屏解析依赖 CLI 的界面文字，CLI 改了界面就要跟着调；解析不出来时把屏幕原文一起报回去。
tmux 以运行 Claude 的那个用户的身份跑（instance.RUN_AS，见 hostfs.py），空目录也在 Claude 那边的数据目录里
（instance.GUEST_DATA，各号信任过的就是那个路径）；解析出的额度记进查看器自己的数据目录。

GPT 号（见 instance.py）的 /usage 只会去问 Anthropic，查不到 ChatGPT 的额度，改成拿 Codex 的 ChatGPT 登录
直接问 ChatGPT 的用量接口（chatgpt.com/backend-api/wham/usage，Codex 自己查额度用的，不是公开文档里的接口），
同样不发消息、不花额度。令牌只读不刷新：刷新令牌用一次就作废，由翻译层和 Codex 自己去刷（它们都写回同一个 auth.json）。
"""
import asyncio
import json
import os
import re
import shlex
import time
import urllib.error
import urllib.request
import _strptime   # noqa: F401  datetime.strptime 第一次用时才去磁盘上导入它，在事件循环上卡过 3.6 秒，启动时先导进来
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import hostfs
from instance import ACCOUNTS, API, CALL, DATA, GPT, GUEST_DATA, proxy_port   # 额度历史记在这个实例的数据目录里（见 instance.py）

CWD = hostfs.Path(str(GUEST_DATA / ".usage_cwd"))
WAIT_SECS = 40   # 从打开界面到看到用量，最多等这么久


async def tmux(*args: str) -> str:
    rc, out, _ = await asyncio.to_thread(hostfs.run, ["tmux", *args], 10)
    if rc is None:
        raise RuntimeError(f"tmux {args[0]} 等了 10 秒没回来")
    return out.decode(errors="ignore")


def parse_reset(text: str, now: float, span: timedelta | None = None) -> int | None:
    """「11:40pm (Asia/Shanghai)」「Oct 8, 3am (Asia/Shanghai)」→ 时间戳。只有钟点的，是今天或明天的那个时刻。
    span 是窗口全长：窗口到点后、还没开新窗口时，面板仍挂着旧窗口的钟点（见过到点后十几分钟都是旧值），
    推到明天若比窗口全长还远，就只能是这个已经过去的旧钟点，留在今天，前端照「已重置」显示；不这样会显示成一天后才刷新。"""
    m = re.match(r"(?:([A-Za-z]{3})\w* (\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:\(([^)]+)\))?",
                 text.strip(), re.I)
    if not m:
        return None
    try:
        tz = ZoneInfo(m[6]) if m[6] else None
    except Exception:
        tz = None
    base = datetime.fromtimestamp(now, tz)
    hour = int(m[3]) % 12 + (12 if m[5].lower() == "pm" else 0)
    minute = int(m[4] or 0)
    if m[1]:
        month = datetime.strptime(m[1][:3].title(), "%b").month
        dt = base.replace(month=month, day=int(m[2]), hour=hour, minute=minute, second=0, microsecond=0)
        if dt < base - timedelta(days=2):   # 跨年
            dt = dt.replace(year=dt.year + 1)
    else:
        dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= base and (span is None or dt + timedelta(days=1) - base <= span):
            dt += timedelta(days=1)
    return int(dt.timestamp())


def window_key(title: str) -> str | None:
    """面板里每一段的标题 → 存的键：Current session → five_hour，Current week (all models) → seven_day，
    Current week (Sonnet only) 这类 → seven_day_sonnet。"""
    t = title.lower()
    if t.startswith("current session"):
        return "five_hour"
    if t.startswith("current week"):
        inner = re.search(r"\(([^)]*)\)", t)
        if not inner or "all models" in inner[1]:
            return "seven_day"
        return "seven_day_" + re.sub(r"[^a-z]", "", inner[1].split()[0])
    return None


def parse_usage(screen: str, now: float) -> dict:
    lines = [l.strip() for l in screen.splitlines() if l.strip()]
    out = {}
    for i, line in enumerate(lines):
        key = window_key(line)
        if not key:
            continue
        w = {}
        for nxt in lines[i + 1:i + 4]:
            if (m := re.search(r"(\d+(?:\.\d+)?)%\s*used", nxt)) and "used_percentage" not in w:
                w["used_percentage"] = float(m[1])
            elif nxt.lower().startswith("resets"):
                w["resets_at"] = parse_reset(nxt[6:], now, timedelta(hours=5) if key == "five_hour" else None)
        if "used_percentage" in w:
            out[key] = w
    return out


def save(alias: str, windows: dict, quota_dir: Path, source: str = "usage"):
    """合并进 quota/<号>.json（状态栏脚本也写这个文件，都是「这个号最新的额度」），先写临时文件再改名。"""
    quota_dir.mkdir(exist_ok=True)
    path = quota_dir / f"{alias}.json"
    try:
        snap = json.loads(path.read_text())
    except (OSError, ValueError):
        snap = {}
    snap.update(windows, ts=time.time(), account=alias, source=source)
    tmp = quota_dir / f".{alias}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(snap, ensure_ascii=False))
    tmp.replace(path)
    with open(DATA / "quota_history.jsonl", "a", encoding="utf-8") as f:   # 额度历史：每查一次追加一行
        f.write(json.dumps({"ts": snap["ts"], "account": alias, "source": source, **windows}, ensure_ascii=False) + "\n")


GPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
GPT_WINDOWS = {18000: "five_hour", 604800: "seven_day"}   # 用量接口里窗口的秒数 → 存的键


def gpt_usage(alias: str) -> dict:
    """问 ChatGPT 的用量接口，返回和 /usage 解析结果一样的 {five_hour: {used_percentage, resets_at}, seven_day: {...}}。
    配了代理就经代理（instance.proxy_port），没配就直连。
    Codex 的登录在查看器自己这边（分用户时放在查看器的系统用户名下，RUN_AS 读不到；翻译层也以查看器的用户跑），所以用 pathlib 读，不经 hostfs。"""
    home = Path(GPT[alias]["codex_home"])
    try:
        tokens = json.loads((home / "auth.json").read_text())["tokens"]
        token, acct = tokens["access_token"], tokens["account_id"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"读不了 {home}/auth.json 里 Codex 的 ChatGPT 登录（{type(e).__name__}: {e}）") from None
    port = proxy_port(alias)
    req = urllib.request.Request(GPT_USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "ChatGPT-Account-Id": acct, "User-Agent": "codex_cli_rs", "originator": "codex_cli_rs"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": f"http://127.0.0.1:{port}"} if port else {}))
    try:
        with opener.open(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        why = "Codex 的登录过期了，在运行查看器的机器上用一次 codex（或者网页里用一次这个号）让它刷新令牌" if e.code == 401 else e.reason
        raise RuntimeError(f"ChatGPT 用量接口回了 {e.code}：{why}") from None
    except (OSError, ValueError) as e:
        raise RuntimeError(f"问 ChatGPT 用量接口失败：{type(e).__name__}: {e}") from None
    out = {}
    for w in ((data.get("rate_limit") or {}).get("primary_window"), (data.get("rate_limit") or {}).get("secondary_window")):
        if isinstance(w, dict) and (key := GPT_WINDOWS.get(w.get("limit_window_seconds"))) and w.get("used_percent") is not None:
            out[key] = {"used_percentage": float(w["used_percent"]), "resets_at": w.get("reset_at")}
    if not out:
        # 只报套餐和各窗口的长度，不贴原文：原文里有订阅者的邮箱和账户编号，报错是给所有点刷新的人看的
        spans = [f"{w['limit_window_seconds'] / 86400:g} 天" for w in ((data.get("rate_limit") or {}).get("primary_window"),
                 (data.get("rate_limit") or {}).get("secondary_window")) if isinstance(w, dict) and w.get("limit_window_seconds")]
        raise RuntimeError(f"ChatGPT 用量接口说这个号现在是 {data.get('plan_type') or '取不到的'} 套餐，"
                           f"额度窗口是{'、'.join(spans) or '（没有）'}，认不出 5 小时和本周的窗口")
    return out


BALANCE_DIR = DATA / "balance"   # API 号（DeepSeek）查到的平台余额：一个号一份，最新的那次
BALANCE_HISTORY = DATA / "balance_history.jsonl"   # 每查一次追加一行：拿来核对账本按单价算的花费和平台实际扣的对不对得上


def save_balance(alias: str, bal: dict, source: str, history: bool = True):
    """history 为假时只换最新的那份、不往历史里追加（quota_poll.py 每分钟查一次，读数没变就不追加）。"""
    BALANCE_DIR.mkdir(exist_ok=True)
    rec = {**bal, "ts": time.time(), "account": alias, "source": source}
    tmp = BALANCE_DIR / f".{alias}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False))
    tmp.replace(BALANCE_DIR / f"{alias}.json")
    if history:
        with open(BALANCE_HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def probe(alias: str, quota_dir: Path, source: str = "usage") -> dict:
    """查一个号（source：手动刷新是 usage，网页一轮结束后自动查的是 usage-auto）。返回解析出的各个窗口；出错时抛 RuntimeError，带上屏幕最后几行。
    GPT 号改问 ChatGPT 的用量接口（gpt_usage）。API 号（DeepSeek）没有额度窗口，查的是平台余额（api_relay.balance），
    存进 BALANCE_DIR，不写 quota/：没有窗口读数，预测拦截、挑号、拟合都不会把它当成额度。
    存盘（save、save_balance）放到线程里等它写完再返回：它要先读状态栏脚本写的那份再合并，返回以后调用的地方马上就读，
    不能交给写盘线程不等；但在事件循环里同步写，磁盘慢时整个查看器跟着卡（卡顿记录里有二十几次）。"""
    import cc_credentials
    if alias in API:
        import api_relay
        bal = await asyncio.to_thread(api_relay.balance, alias)
        await asyncio.to_thread(save_balance, alias, bal, source)
        return bal
    if alias in cc_credentials.accounts():
        windows = await asyncio.to_thread(cc_credentials.usage, alias)
        await asyncio.to_thread(save, alias, windows, quota_dir, source)
        return windows
    if alias in GPT:
        windows = await asyncio.to_thread(gpt_usage, alias)
        await asyncio.to_thread(save, alias, windows, quota_dir, source)
        return windows
    CWD.mkdir(exist_ok=True)
    name = f"ccv-usage-{alias}-{os.getpid()}"
    # CC_VIEWER_LAUNCH=1：查看器拉起的，限量号的启动脚本才放行（见 chat.launch_env）。
    # 配了代理就在读完 .bashrc 之后换成它（instance.proxy_port），和网页对话走同一个；没配就照 .bashrc 原样。
    # ~/.bashrc 里有和号同名的别名就用别名，没有就直接运行 claude，配置目录按账号表设（instance.ACCOUNTS）
    port = proxy_port(alias)
    p, s = f"http://127.0.0.1:{port}", f"socks5://127.0.0.1:{port}"
    env = f"export http_proxy={p} https_proxy={p} HTTP_PROXY={p} HTTPS_PROXY={p} all_proxy={s} ALL_PROXY={s}; " if port else ""
    conf = f"CLAUDE_CONFIG_DIR={shlex.quote(ACCOUNTS[alias])} " if ACCOUNTS.get(alias) else ""
    run = f'if [ "$(type -t {alias})" = alias ]; then {alias}; else {conf}claude; fi'
    await tmux("new-session", "-d", "-s", name, "-x", "160", "-y", "50", "-c", str(CWD),
               f"CC_VIEWER_LAUNCH=1 bash -i -c {shlex.quote(env + run)}")
    screen, sent, t0 = "", False, time.monotonic()
    try:
        while time.monotonic() - t0 < WAIT_SECS:
            await asyncio.sleep(1)
            screen = await tmux("capture-pane", "-p", "-J", "-t", name)
            low = screen.lower()
            if "% used" in low and "current session" in low:
                await asyncio.sleep(1)   # 面板可能还在一段段画出来，稍等再抄一遍
                screen = await tmux("capture-pane", "-p", "-J", "-t", name)
                break
            if "trust" in low and "folder" in low:   # 「信任这个文件夹吗」是安全确认，不替用户答，停下来报给用户
                raise RuntimeError(f"{alias} 还没信任 {CWD}：请{CALL}在终端里进这个目录运行一次 {alias}，自己确认信任后再刷新")
            if not sent and "❯" in screen:
                await tmux("send-keys", "-t", name, "/usage")
                await asyncio.sleep(0.5)
                await tmux("send-keys", "-t", name, "Enter")
                sent = True
        else:
            raise RuntimeError("等了 %d 秒没看到用量面板：%s" % (WAIT_SECS, " / ".join(screen.strip().splitlines()[-4:])))
    finally:
        await tmux("send-keys", "-t", name, "Escape")
        await tmux("kill-session", "-t", name)
    windows = parse_usage(screen, time.time())
    if not windows:
        raise RuntimeError("看到了用量面板但没解析出来：" + " / ".join(screen.strip().splitlines()[-8:]))
    await asyncio.to_thread(save, alias, windows, quota_dir, source)
    return windows
