"""开源版不含审稿看板（配合另一套自动审稿流程用的整页面板）。这里只留其余代码要用的名字，一律按「没开」走：
认不出任何审稿工作区，所以前端也不会出现看板的入口。"""


def ws_from_result(text):
    return None


def board(ws_path, run_as=None):
    raise FileNotFoundError("开源版不含审稿看板")


def notes_full(ws_path, run_as=None):
    raise FileNotFoundError("开源版不含审稿看板")


def version_diff(ws_path, name, run_as=None):
    raise FileNotFoundError("开源版不含审稿看板")
