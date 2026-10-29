#!/usr/bin/env python3
"""Claude Code 的状态栏脚本（配在一组号共享的 ~/.claude/settings.json 里；可以直接用它，
也可以带上 CC_VIEWER_DATA 和 --quiet，排在自己的状态栏前面悄悄记一下）。

CLI 每次收到回复都会把一段 JSON 从标准输入交给它（官方文档「Customize your status line」）。
这里做三件事：
1. 里面有 rate_limits（Pro / Max 订阅、会话收到第一次回复之后才有）就按号存进 quota/<号>.json（最新值，覆盖写），
   viewer 后端读这些文件，网页上看各号的 5 小时和本周额度；同时往 quota_history.jsonl 追加一行留历史；
2. 按 transcript_path 把这个会话新增的每次 API 调用记进 api_calls.jsonl，带上是哪个号发的（见 api_calls.py）；
3. 打印一行状态：号、模型、上下文、5 小时和本周用量。

是哪个号：拿启动时的 CLAUDE_CONFIG_DIR 去对这个实例账号表里各号的配置目录（instance.py；没设就是不带目录的那个号）。
不解析 ~/.bashrc 里的别名：别名的写法各式各样，认不全，按账号表认才可靠。
状态栏在本地运行、不消耗 API token；这里任何出错都吞掉，绝不能影响 CLI。
--quiet：只记不打印（排在别人的状态栏前面时用）。
"""
import json
import os
import sys
import time
from pathlib import Path

from instance import ACCOUNTS, DATA   # 账号表和数据目录都按这个实例来（环境变量 CC_VIEWER_DATA，见 instance.py）

QUOTA_DIR = DATA / "quota"
HISTORY = DATA / "quota_history.jsonl"   # 额度历史，三个来源共用


def account() -> str:
    """当前是哪个 cc 别名；认不出来就用配置目录的名字。
    查看器拉起的会话带 CC_VIEWER_ACCOUNT（远端回环、GPT 号用默认配置目录，看目录会认错）。"""
    named = os.environ.get("CC_VIEWER_ACCOUNT")
    if named and named in ACCOUNTS:
        return named
    want = os.environ.get("CLAUDE_CONFIG_DIR")
    want = Path(os.path.expandvars(want)).expanduser().resolve() if want else None
    for name, d in ACCOUNTS.items():
        if (d and want and Path(d).resolve() == want) or (not d and not want):
            return name
    return want.name if want else "default"


def save(name: str, data: dict, limits: dict):
    """存成 quota/<号>.json：先写临时文件再改名，几个会话同时写也不会写出半截。"""
    QUOTA_DIR.mkdir(exist_ok=True)
    snap = {"ts": time.time(), "account": name, "session": data.get("session_id"),
            **{k: limits[k] for k in ("five_hour", "seven_day") if isinstance(limits.get(k), dict)}}
    tmp = QUOTA_DIR / f".{name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(snap, ensure_ascii=False))
    tmp.replace(QUOTA_DIR / f"{name}.json")
    append_history({**snap, "source": "statusline"})


def append_history(rec: dict):
    """额度历史：每记一次就追加一行，不去重（以后拿来拟合限额和 token 的关系）。
    一行不到 4KB，追加写是原子的，几个会话同时写也不会把两行写混。"""
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def clock(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else ""


def line(name: str, data: dict, limits: dict) -> str:
    bits = [name]
    model = (data.get("model") or {}).get("display_name")
    if model:
        bits.append(model)
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        bits.append(f"上下文 {ctx:.0f}%")
    five, week = limits.get("five_hour") or {}, limits.get("seven_day") or {}
    if five.get("used_percentage") is not None:
        at = clock(five.get("resets_at"))
        bits.append(f"5小时 {five['used_percentage']:.0f}%" + (f"（{at} 重置）" if at else ""))
    if week.get("used_percentage") is not None:
        bits.append(f"本周 {week['used_percentage']:.0f}%")
    return " │ ".join(bits)


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    name = account()
    limits = data.get("rate_limits") or {}
    if limits.get("five_hour") or limits.get("seven_day"):
        try:
            save(name, data, limits)
        except OSError:
            pass
    if data.get("transcript_path"):
        try:
            import api_calls
            # 用这一个 CLI 的启动标记，不按刚装好的配置把仍在直连的旧进程误记成远端。
            source = api_calls.web_source(name) if os.environ.get('CC_VIEWER_REMOTE') == '1' else 'terminal'
            api_calls.record(data["transcript_path"], name, source, wait=1)
        except Exception:
            pass
    if "--quiet" not in sys.argv:
        print(line(name, data, limits))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if "--quiet" not in sys.argv:
            print("")
