"""开源版不含对外 API，也就没有网关那边看到的外部用量，账本只按本机的会话记录结算。这里一律按「没有外部用量」返回。"""


def ready(mid):
    return True


def external_sessions(user=None):
    return {}


def merge_for(sid, original):
    return dict(original)


def billable_for(sid, original):
    return merge_for(sid, original)


def version(sid):
    return 0


def session(sid):
    return {}


def owner_of(sid):
    return None


def extra(sid, have):
    return []
