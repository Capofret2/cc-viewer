"""开源版不含独立环境（每人一个系统用户、家目录互相隔离）。这里只留其余代码要用的名字，一律按「只有共用环境」走：
会话都在共用环境里，没有私有家目录，也就没有要换身份去读的路径。"""
import os
import threading

_lock = threading.RLock()   # users.py 判断各把锁有没有被占时也看它


def enabled():
    return False


def get(owner):
    return None


def all_environments():
    return []


def accounts():
    return []


def private_only():
    return []


def select(owner, kind):
    if kind in (None, "", "shared"):
        return None
    raise ValueError("这个版本没有独立环境，只能用共用环境")


def for_sid(sid):
    return None


def can_see(name, sid):
    return True


def check_account(env, account):
    return None


def bind_session(sid, env):
    return None


def roots(owner=None, internal=False):
    import calls
    yield calls.PROJECTS


def session_path(sid):
    import calls
    return calls.find(sid)


def open_path(raw, owner, admin=False):
    import hostfs
    raw = os.path.abspath(os.path.expanduser(str(raw))) if str(raw).startswith("/") else str(raw)
    return hostfs.Path(raw).expanduser()


def info(owner):
    return {"enabled": False, "ready": False, "environment": None, "accounts": [], "private_only": []}


def create(owner):
    raise ValueError("这个版本没有独立环境")


def prepare(env):
    raise RuntimeError("这个版本没有独立环境")


def launch_env(env, account):
    raise RuntimeError("这个版本没有独立环境")
