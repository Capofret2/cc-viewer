"""开窗服务：有预定计划的号，每次 5 小时窗口一重置就立刻开下一个窗口，以后各个窗口的时刻才和预定时说的一样（见 reserve.py）。

为什么要开：5 小时窗口从重置后的第一次请求算起，重置以后没人用，下一个窗口就往后推，后面预定的窗口全对不上。
所以只要某个号还有没过去的预定窗口，到最后一个为止，每次重置都要立刻发一次最小的请求（Haiku 回一个字，不存会话记录），
中间没人预定的窗口也一样开（链条不许断，允许有小的偏移，但要尽量没有）。
各来源报的重置时刻都正好落在整分钟上（额度历史里统计过），所以在重置后 KICK_DELAY 秒发；发完马上用 /usage 确认
新窗口开了，没开就隔 RETRY 秒再开，直到开成为止。开窗花的那一点点不记在任何人账上（不存会话记录，账本里没有它）。

和查看器分开跑：查看器重启、改代码都不影响它。由 crontab 每分钟用 flock 拉一次（已经在跑就直接退出），
机器重启后一分钟内自己起来，装法见 keeper_cron.sh。每 TICK 秒醒一次，心跳和最近的错误写在数据目录的 keeper_state.json，
查看器的预定面板会显示它是不是活着；日志在数据目录的 keeper.log。
开窗请求和查看器的对话一样经 claude_via_bashrc.sh 用别名启动（铁律：只能在加载了 .bashrc 的 bash 里用别名启动）。
"""
import asyncio
import json
import os
import time
import traceback
from datetime import datetime

import reserve
import chat
from chat import check_proxy, cli_launch, launch_env, use_proxy
from instance import ACCOUNTS, BANNED, DATA, GPT
from usage_probe import CWD, probe   # 刷新额度用的空目录（各号都信任过，在 Claude 那边）；-p 不问信任，放这里只是不在别处留东西

QUOTA_DIR = DATA / "quota"
TICK = 5            # 多久醒一次
KICK_DELAY = 3      # 重置后多少秒发开窗请求
RETRY = 15          # 没开成，隔多久再开
KICK_MODEL = "claude-haiku-5-5"
KICK_MODEL_GPT = "gpt-6-luna"   # GPT 号（见 instance.py）用不了 Claude 模型，开窗用 GPT 里最小的
KICK_PROMPT = "只回复一个字：好"
state = {"pid": os.getpid(), "ts": 0, "error": "", "kicks": []}


def log(msg: str):
    print(datetime.now().strftime("%m-%d %H:%M:%S"), msg, flush=True)


def save_state():
    state["ts"] = time.time()
    tmp = reserve.KEEPER_STATE.with_name(f".keeper_state.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(reserve.KEEPER_STATE)


def snap_of(acc: str) -> dict:
    """这个号最新的额度读数（quota/<号>.json，三个来源都写这里）；读不了就是空的，按「不知道窗口在不在走」处理，照样开。"""
    try:
        return json.loads((QUOTA_DIR / f"{acc}.json").read_text())
    except (OSError, ValueError):
        return {}


def planned() -> dict[str, list[float]]:
    """各号还有哪些预定窗口（开始时刻，只要订了的）。"""
    reserve.reload()
    out: dict[str, list[float]] = {}
    with reserve._lock:
        for b in reserve.batches:
            if b["account"] in ACCOUNTS and b["account"] not in BANNED:
                out.setdefault(b["account"], []).extend(s["start"] for s in b["five"] if s["pct"] > 0)
    return out


async def kick(acc: str) -> str:
    """发一次最小的请求开出新窗口；出错返回原因。"""
    check_proxy(acc)
    import cc_credentials
    if acc in cc_credentials.accounts():
        return await asyncio.to_thread(cc_credentials.kick, acc, KICK_PROMPT, KICK_MODEL)
    cli, cwd, more = cli_launch(str(CWD))   # 配了 RUN_AS 时经 sudo 换用户拉起（见 chat.cli_launch）
    p = await asyncio.create_subprocess_exec(
        cli, "-p", KICK_PROMPT, "--model", KICK_MODEL_GPT if acc in GPT else KICK_MODEL, "--no-session-persistence",
        cwd=cwd, env={**os.environ, **launch_env(acc), **more},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), 180)
    except asyncio.TimeoutError:
        p.terminate()   # 经 sudo 拉起时收到的是 sudo，它会把这个信号转给 CLI；SIGKILL 转不过去
        try:
            await asyncio.wait_for(p.wait(), 5)
        except asyncio.TimeoutError:
            p.kill()
        return "等待 180 秒未返回"
    if p.returncode:
        return f"退出码 {p.returncode}：{(err or out).decode(errors='ignore').strip()[-300:]}"
    return ""


async def open_window(acc: str) -> str:
    """开窗并确认：/usage 里的重置时刻离现在差不多 5 小时才算开成了。"""
    t0 = time.time()
    if err := await kick(acc):
        return f"开启新窗口失败：{err}"
    try:
        w = await probe(acc, QUOTA_DIR, "keeper")
    except Exception as e:
        return f"开启后查询额度失败：{e}"
    r = (w.get("five_hour") or {}).get("resets_at")
    if not r or r < t0 + reserve.FIVE - 600:
        return f"开启后查询到的重置时间为 {reserve.clock(r) if r else '空'}，新窗口尚未开启"
    state["kicks"] = (state["kicks"] + [{"account": acc, "ts": t0, "resetsAt": r}])[-20:]
    log(f"{acc} 开了新窗口，{reserve.clock(r)} 重置")
    return ""


async def main():
    CWD.mkdir(exist_ok=True)
    retry_at: dict[str, float] = {}
    log(f"开窗服务起来了（进程 {os.getpid()}，数据目录 {DATA}）")
    while True:
        try:
            now, errors = time.time(), []
            for acc, starts in planned().items():
                if not any(reserve.phase(s, None, now) != "past" for s in starts):
                    continue   # 这个号的预定窗口都过去了，链条不用再接
                r = (snap_of(acc).get("five_hour") or {}).get("resets_at")
                if r and now < r + KICK_DELAY:
                    continue   # 窗口还在走
                if retry_at.get(acc, 0) > now:
                    continue
                if err := await open_window(acc):
                    retry_at[acc] = time.time() + RETRY
                    errors.append(f"{acc}：{err}")
                    log(f"{acc} {err}，{RETRY} 秒后再开")
                else:
                    retry_at.pop(acc, None)
            state["error"] = "；".join(errors)
        except Exception as e:   # 一圈出错不能让服务停下，记下来接着转
            state["error"] = f"{type(e).__name__}: {e}"
            log(traceback.format_exc())
        save_state()
        await asyncio.sleep(TICK)


if __name__ == "__main__":
    # 和查看器一样把代理写进环境（这一组的默认端口）；开窗拉起的 CLI 另由 launch_env 的 CC_VIEWER_PROXY 按号换上自己的端口
    use_proxy(chat.PROXY)
    asyncio.run(main())
