"""开源版不含远端统一结算（几个查看器之间由一边统一记账、另一边只显示）。这里只留其余代码要用的名字，一律按「没开」走：
没有远端结算的账号，也没有哪条回复是由远端定价的。"""


class CostError(ValueError):
    pass


def accounts():
    return []


def marked(mid):
    return False


def ensure():
    return None


def version():
    return 0


def price(account, mid):
    raise CostError("开源版不含远端统一结算")


def error(account):
    return None


def sync(account):
    return None


def quote(mid, account, model, use):
    raise CostError("开源版不含远端统一结算")
