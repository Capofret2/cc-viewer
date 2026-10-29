"""开源版不含集中订阅认证（一份登录给多人取令牌），每个号都在自己的配置目录里正常登录。这里一律按「没有集中登录的号」返回。"""


def accounts():
    return {}


def helper_settings(account):
    return None


def issue(driver):
    raise PermissionError("这个版本没有集中认证")


def usage(account):
    raise RuntimeError("这个版本没有集中认证")


def kick(account, prompt, model):
    raise RuntimeError("这个版本没有集中认证")
