"""点数单价：各模型每百万 token 算几个点（1 点 = 5 小时额度的 1%），输入、缓存写入、缓存命中、输出各一个。

存在 points_rates.json，后端只在这里读它：账本（users.py）和发给前端的配置（server.py 的 /api/config）都从这里取。
一个模型可以写成另一个模型的倍数（{"of": 模型, "ratio": 倍数}，被引的那个也可以是倍数），读的时候换算成四个单价，前端拿到的都是换算好的。
表里没有的模型不算点数。
GPT 模型例外：表里写的是 GPT 号 5 小时额度的 1%，读的时候乘 instance.GPT_SCALE 换成点（周额度同价，见 instance.py）。
按 token 付费的 API 号的模型（比如 DeepSeek）也例外：表里写的是官方价，元 / 百万 token，读的时候换成点，
收钱用的单价乘过 PRICE 以后正好是官方价（YUAN）。空闲时段的调用模型名带 peak.OFF，表里单写一条（高峰价的一半）。
"""
import json
from pathlib import Path

from instance import API, GPT, GPT_SCALE, is_api_model, is_gpt_model, model_family

FILE = Path(__file__).parent / "points_rates.json"
KEYS = ("in", "cw", "cr", "out")
# 收钱按单价的这个比例（0.66）：按单价算，一个号用满一周的花费比实际订阅成本高，
# 所以花钱的地方（账本、气泡和标题前的分、花费环形图、预定的价格）一律乘它。账本每次都按全部历史重算，过去的花费也跟着降；
# 发过的额度不动。额度的预测和拦截（reserve.py、server.limit_block）、拟合用的是额度本身，不乘它
PRICE = 0.66
# GPT 模型收钱再打三折（让 GPT 号用满一周的花费接近订阅成本，单价按模型、和号无关，所以所有 GPT 模型一起打折）。
# 和 PRICE 一样只动收钱、不动额度，账本按全部历史重算，以前的花费和预定也跟着变
GPT_PRICE = 0.3
# 1 点 = 5 分（前端 points.js 的 FEN）。API 号的模型按官方价收，不打 PRICE 的折：
# 表里的元乘 YUAN 换成点，收钱时再乘 PRICE，界面上写的分（× FEN）正好是官方价 × 100
FEN = 5
YUAN = 100 / FEN / PRICE
# 有的中转站的 Opus 5.5 渠道回包出现过点号写法；只共用已配置的账号价，不改变请求或原始模型名。
# 写在这个号的配置里（instance.json 的 api 里那个号）："price_aliases": {"claude-opus-5.5": "claude-opus-5-5"}，回包里的左边按右边的单价算


def api_price(account, model):
    config = API.get(account, {})
    bare = (model or '').split('@account=')[0].split('[')[0]
    priced = config.get('price_aliases', {}).get(bare, bare)
    return config.get('rates', {}).get(priced)


# 长上下文分档（比如中转站的 GPT-6 Astra）：号的单价里多写一段 "long": {"over": 门槛, in, out, cr, cw, 有一小时价再写 cw1h}，
# 一次调用的完整输入（未命中输入 + 缓存写入 + 缓存命中）超过门槛，这一整笔都按长档的单价算，和平台「完整输入长度 ≤ 272K 为 standard」一致。
# 门槛按 token 个数写（272K 写 272000，平台没说 K 是 1000 还是 1024，按 1000 算）
def whole_input(use) -> int:
    return (use.get("in") or 0) + (use.get("cw") or 0) + (use.get("cr") or 0)


def tier_row(row: dict, use) -> dict:
    """号配置里的原始单价（元 / 百万 token）按这次调用的完整输入选档：超过门槛用 long 那一段，没有分档或没超就是原样。"""
    long = row.get("long")
    return long if long and use is not None and whole_input(use) > long["over"] else row


def tiered(r: dict, use) -> dict:
    """load 出来的单价表里的一行按这次调用选档（长档的几项存成 long_in 这样的键，门槛是 over）。"""
    if r and "over" in r and use is not None and whole_input(use) > r["over"]:
        return {**r, **{k: r["long_" + k] for k in (*KEYS, "cw1h") if "long_" + k in r}}
    return r


def _check_long(account, model, row):
    long = row.get("long")
    if long is None:
        return
    if not isinstance(long, dict) or not isinstance(long.get("over"), int) or long["over"] <= 0 or any(k not in long for k in KEYS):
        raise ValueError(f"{account} 的 {model} 长上下文单价要写 over（正整数 token 数）和 in、out、cr、cw")


def sub_gpt(key: str) -> bool:
    """按订阅 GPT 号的口径计价的 GPT 模型（额度换算、收钱再打 GPT_PRICE 的折）。号自己配了价的（比如中转站的 gpt-6-astra@account=某号）
    按平台标价收，不吃这个折。"""
    return is_gpt_model(key) and "@account=" not in key


def load(price: bool = False) -> dict[str, dict]:
    """{模型: {in, cw, cr, out}}。文件不在就是还没有单价；在却读不了、格式不对就报错，不能当成没有。
    price 为真时是收钱用的单价（乘过 PRICE，GPT 模型再乘 GPT_PRICE）；算额度用不乘的。"""
    try:
        raw = json.loads(FILE.read_text())
    except FileNotFoundError:
        return {}
    out = {k: v for k, v in raw.items() if not k.startswith("_") and "of" not in v}
    # 倍数可以套倍数（比如 GPT 的快速档写成普通档的倍数，普通档又是 gpt-6-sol 的倍数），一轮轮换算到全部有数
    todo = {k: v for k, v in raw.items() if not k.startswith("_") and "of" in v}
    while todo:
        ready = {k: v for k, v in todo.items() if v["of"] in out}
        if not ready:
            raise ValueError(f"points_rates.json 里这些模型写的 of 找不到或者互相套成了圈：{sorted(todo)}")
        for k, v in ready.items():
            out[k] = {f: out[v["of"]][f] * v["ratio"] for f in KEYS}
            del todo[k]
    out = {k: {f: v[f] * (GPT_SCALE if is_gpt_model(k) else YUAN if is_api_model(k) else 1) for f in KEYS} for k, v in out.items()}
    # 中转和订阅可以返回同一个模型名：账号价单独建计价键，绝不覆盖订阅的单价。
    for account, config in API.items():
        for model, row in config.get("rates", {}).items():
            _check_long(account, model, row)
            long = row.get("long")
            out[billing_model(model, account)] = {**{k: row[k] * YUAN for k in KEYS},
                                                **({"cw1h": (row["cw1h"] - row["cw"]) * YUAN} if "cw1h" in row else {}),
                                                **({"over": long["over"], **{"long_" + k: long[k] * YUAN for k in KEYS},
                                                    **({"long_cw1h": (long["cw1h"] - long["cw"]) * YUAN} if "cw1h" in long else {})}
                                                   if long else {}),
                                                'yuan': YUAN,
                                                **({"estimate_only": 1} if config.get("cost_audit") else {})}
        for alias, model in config.get('price_aliases', {}).items():
            if model in config.get('rates', {}):
                out[f'{alias}@account={account}'] = dict(out[billing_model(model, account)])
    # 智谱等原生 API 模型也能按远端实扣展示；换算只作用于金额，不再按峰谷乘一遍。
    for model, row in out.items():
        if is_api_model(model):
            row['yuan'] = YUAN
        elif sub_gpt(model):
            # 远端统一结算的 GPT 号也按那边已折扣的金额展示：收钱的表要再乘 GPT_PRICE，这里先除掉，免得金额被打两次折
            row['yuan'] = YUAN / GPT_PRICE
    if price:
        # 门槛 over 是 token 个数，不是钱，不乘
        out = {k: {f: value if f == "over" else value * PRICE * (GPT_PRICE if sub_gpt(k) else 1) for f, value in v.items()}
               for k, v in out.items()}
    return out


def billing_model(model: str, account: str | None) -> str:
    """只给统计用的模型名加账号后缀；发给上游的模型名和原始记录不改。"""
    bare = (model or "").split('@account=')[0].split("[")[0]
    if api_price(account, bare) is not None:
        return f"{bare}@account={account}"
    return model


def price_of(account: str | None) -> float:
    """预定按这个号的额度折成点数，收钱时乘的比例：GPT 号的额度只能给 GPT 模型用，和 GPT 模型的单价一起打折。"""
    return PRICE * (GPT_PRICE if account in GPT else 1)


def rate(table: dict, model: str) -> dict | None:
    """模型名后面可能带日期或 [1m]：取最长的前缀匹配，和前端 points.js 一样。"""
    hits = [k for k in table if (model or "").startswith(k)]
    return table[max(hits, key=len)] if hits else None


def cost(table: dict, c: dict) -> float:
    """一次调用（{model, use}）按这张单价表合多少点；没有单价的模型是 0。"""
    r = tiered(rate(table, c["model"]), c["use"])
    if r and "charge_yuan" in c["use"]:
        return c["use"]["charge_yuan"] * r["yuan"]
    if r and "@account=" in c["model"] and c["use"].get("cw1h", 0) and "cw1h" not in r and not r.get("estimate_only"):
        raise ValueError(f"{c['model']} 返回一小时缓存写入，但尚未配置对应价格，不能计费")
    return (sum(c["use"][k] * r[k] for k in KEYS)
            + c["use"].get("cw1h", 0) * r.get("cw1h", 0)) / 1e6 if r else 0.0


def unpriced(model: str | None) -> str:
    """这个模型表里没有单价就返回拦下的说明，有就是空字符串。没有单价的模型花了额度却不计费、也估不出预测拦截要的花费，
    所以直接拦下：起因是 CLI 被安全检查挡下一次回复后，自己退回了旧的 claude-opus-5 接着跑，
    那一串请求全都没算钱。这句 AI 也看得到，只说模型，不提查看器和号。读不了单价表照样往外抛，由调用的地方拦下。"""
    table = load()
    if not model or rate(table, model):
        return ""
    fam = model_family(model)
    ok = "、".join(sorted(k for k in table if model_family(k) == fam and "@" not in k))   # 空闲时段那一条不是能选的模型
    return f"模型 {model} 不可用，已停止。请使用 /model 切换到可用模型（{ok}）后继续"
