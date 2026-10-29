"""按时段计价的模型（DeepSeek、智谱）：同一个模型高峰、空闲两个价，每次调用按它发生的时刻算。

DeepSeek 官方定价（https://api-docs.deepseek.com/zh-cn/quick_start/pricing）：
「空闲时段价格为高峰时段价格的一半。北京时间周一至周五（不含中国法定节假日）9:00 - 12:00、14:00 - 18:00 为高峰时段；
其余时段，包括周末及中国法定节假日全天均为空闲时段。」所以周末调休上班的那几天照样算空闲，只有下面登记的法定节假日要另外排除。

单价表按模型查，所以空闲时段的调用在模型名后面接上 OFF（deepseek-flash → deepseek-flash@offpeak），
points_rates.json 里给它单写一条（高峰价的一半）。接后缀只在算钱的两处做：calls.parse（账本、会话总消耗、花费视角、拟合）
和 session.py 给气泡的 meta（气泡上这一次花了几分、整轮卡片）；会话记着的模型（拉起 CLI 用的）照旧是原名。
前端认这个后缀只为了把模型名写成「DeepSeek Flash 空闲」（util.modelName）。
时刻用会话记录里这次回复写下的时间（timestamp），和账本里别的按时刻算的规矩（特惠、限量号起算）一致；
请求恰好跨过时段分界的，按回复写下那一刻算，没有核对过 DeepSeek 是按请求开始还是结束算。

法定节假日要每年按国务院办公厅的放假通知补上（2026 年 10 月 8 日以后到年底没有节假日；2027 年的通知还没发，
只先登记了元旦当天）。表改了以后 calls.py 的调用表存盘按源码版本作废（这份文件整个算进版本），账本按全部历史重算；花费存盘的拆分不用时段，不受影响。
只用标准库：状态栏脚本经 calls.py 也会导入它。"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
OFF = "@offpeak"
# 这些前缀的模型按时段计价 → 工作日的高峰时段，[开始, 结束) 的钟点。
# 智谱（GLM Coding Plan，https://docs.bigmodel.cn/cn/coding-plan/overview）：「高峰时段：每周一至周五的 14:00～18:00」，
# 7 月 30 日的改版公告写周末全天按非高峰；法定节假日原文没写，按高峰算（取不到不能当成打折）
PEAK_HOURS = {"deepseek-": ((9, 12), (14, 18)), "glm-": ((14, 18),)}
MODELS = tuple(PEAK_HOURS)
HOLIDAY_MODELS = ("deepseek-",)   # 法定节假日全天空闲的只有 DeepSeek（官方原文写了）
# 法定节假日（放假的那几天，全天空闲）。只登记接入按时段计价以后的
HOLIDAYS = {
    date(2027, 1, 1),   # 元旦；2027 年其余节假日等放假通知发了再补
}


def priced(model: str | None) -> bool:
    return bool(model) and model.startswith(MODELS)


def is_peak(t: float, model: str = "deepseek-") -> bool:
    """t（unix 秒）这一刻 model 这家是不是高峰时段。"""
    d = datetime.fromtimestamp(t, TZ)
    if d.weekday() >= 5 or (model.startswith(HOLIDAY_MODELS) and d.date() in HOLIDAYS):
        return False
    hours = next((h for p, h in PEAK_HOURS.items() if model.startswith(p)), ())
    return any(a <= d.hour < b for a, b in hours)


def next_switch(t: float, model: str = "deepseek-") -> float:
    """t 以后第一次换时段（高峰变空闲或反过来）的时刻，给界面上的小标签写到几点。按整点往后找，最多找八天。"""
    now = is_peak(t, model)
    d = datetime.fromtimestamp(t, TZ).replace(minute=0, second=0, microsecond=0)
    for h in range(1, 24 * 8):
        x = d.timestamp() + h * 3600
        if is_peak(x, model) != now:
            return x
    return t + 24 * 8 * 3600


def stamp(ts) -> float | None:
    """会话记录里的时间（ISO 字符串）→ unix 秒；认不出是 None。"""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def tag(model: str | None, ts) -> str | None:
    """算钱用的模型名：按时段计价的模型在空闲时段接上 OFF。不知道时刻的按高峰价（取不到不能当成打折）。"""
    if not priced(model) or model.endswith(OFF):
        return model
    t = stamp(ts) if ts else None
    return model + OFF if t is not None and not is_peak(t, model) else model
