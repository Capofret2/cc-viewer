"""订阅制的 API 号（比如智谱）每分钟查一次额度，给以后拟合单价攒读数。

为什么要它：查看器只在网页对话一轮结束后（同一个号至少隔 3 分钟）和有人点「刷新额度」时才查，一轮可能跑很久、中间调用几十次，
读数又只有整数百分比，太稀就拟合不出来。官方额度接口不花额度，一分钟查一次，就能知道读数大概在哪一刻跳格（实验里就是这样测的，
analysis/glm_exp_20261027.py）。

和开窗服务 keeper.py 一样不归实例管，由查看器那个系统用户的 crontab 每分钟拉起一次（flock 防重叠）：
  * * * * * cd <代码目录> && CC_VIEWER_DATA=<数据目录> flock -n <数据目录>/.quota_poll.lock timeout 50 <python> quota_poll.py >> <数据目录>/quota_poll.log 2>&1
最新的读数照常换掉 balance/<号>.json（账号面板显示的就是它）；读数（5 小时、本周、两个重置时刻）和上次记进历史的不一样，
或者离上次记已经过了 HEARTBEAT 秒，才往 balance_history.jsonl 追加一行（source 写 poll），不让历史每分钟一行地涨。
上次记了什么存在数据目录的 quota_poll_state.json。查不到就在日志里写一行，什么都不写进数据，不拿旧值顶。
"""
import json
import time

import api_relay
from instance import API, BANNED, DATA
from usage_probe import save_balance

STATE = DATA / "quota_poll_state.json"
HEARTBEAT = 600   # 读数没变时多久记一行：拟合时知道这段时间里确实没涨


def main():
    try:
        state = json.loads(STATE.read_text())
    except FileNotFoundError:
        state = {}
    except ValueError as e:
        print(f"{time.strftime('%F %T')} {STATE} 读不了（{e}），当成没记过", flush=True)
        state = {}
    for acct, conf in API.items():
        if acct in BANNED or "quota" not in api_relay.vendor(acct):
            continue
        try:
            q = api_relay.quota(acct)
        except Exception as e:
            print(f"{time.strftime('%F %T')} 查 {acct} 的额度失败：{type(e).__name__}: {e}", flush=True)
            continue
        key = [q.get("five"), q.get("week"), round(q.get("five_reset") or 0), round(q.get("week_reset") or 0)]
        last = state.get(acct) or {}
        record = key != last.get("key") or time.time() - (last.get("ts") or 0) >= HEARTBEAT
        save_balance(acct, q, "poll", history=record)
        if record:
            state[acct] = {"key": key, "ts": time.time()}
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


if __name__ == "__main__":
    main()
