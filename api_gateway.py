"""开源版不含对外 API（Claude Messages 接口、用户的 API 密钥、网页回环入口）。这里只留其余代码要用的名字，一律按「没开」走。"""


class _Conversations:
    def get(self, sid):
        return None

    def cards(self, owner=None):
        return []

    def states(self, owner=None):
        return {}


class _Keys:
    def list(self, owner=None):
        return []


class _Off:
    """server.py 有几处直接用运行中的网关对象（列外部会话、取密钥名、起停转发进程），没开时给一个什么都没有的。"""
    conversations = _Conversations()
    keys = _Keys()

    async def start_bridge(self):
        return None

    async def stop_bridge(self):
        return None


class _NoSessions:
    @staticmethod
    def card(rec):
        raise LookupError("这个版本没有 API 会话")


_ACTIVE = _Off()
api_sessions = _NoSessions()


def install(server):
    return None


def enabled():
    return False


def allowed_accounts():
    return []


def held(account):
    return 0.0


def cut(driver):
    return None


def listed_cards(user=None):
    """账号页列外部会话用：开源版没有对外接口，一张都没有。"""
    return []
