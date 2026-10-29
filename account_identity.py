"""开源版不含「独立环境按账号固定设备标识」。HOST 是 None，闸门就不会把任何连接认成要经它转发；
for_account 一律返回 None，CLI 照常直接连官方接口。"""
HOST = None
BASE_URL = None
UPSTREAM = None


class IdentityError(RuntimeError):
    pass


def for_account(account, private):
    return None


async def forward(*args, **kwargs):
    raise IdentityError("这个版本没有设备标识中转")
