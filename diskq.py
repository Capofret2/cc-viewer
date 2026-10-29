"""后台写盘线程：数据目录里那些「内存为准、文件只是存一份」的小文件，交给这一个线程去写，事件循环不等磁盘。
数据目录所在的磁盘压力大的时候，事件循环上同步写一个小文件就能卡 1 到 15 秒（看门线程实际记到过），
这期间所有请求和推送都停着；卡顿的大头就是侧边栏已读、限额事件、在干活的会话、思考跳数、闸门的字节数这几处写入。

两种写法：
- rewrite(路径, 全文)：整份重写的 json。同一个文件排着好几份时只写最新的那份（内存里的数据才是准的，中间那几份写了也马上被盖掉）。
  全文在调用的地方当场序列化好再交过来：这些表在事件循环里随时在改，放到线程里再 json.dumps 会碰上「字典在遍历时变了」。
  rewrite_wait 一样排进去，另给一个 Future 报这一份写没写进去：用户在界面上做的选择存不下来要当场告诉他，等的是线程，不是事件循环。
- append(路径, 一段文字)：追加的 jsonl。按交进来的先后排队，同一个文件里的顺序和交进来的一样。
写不进去（磁盘满、权限）就在服务输出里说一次是哪个文件、为什么，隔 RETRY 秒再试，好了再说一句；排着的不丢，整份的只留最新一份。
关服务前 flush 把排着的写完（lifespan 收尾时，atexit 兜底），超时没写完的列出来。

就地写：整份的也是截断后原样写回同一个文件（和以前的 write_text 一样），inode 不变，文件上的 ACL 跟着留下；
只用 O_NOFOLLOW 打开：路径的最后一段是软链接就不写、报错，不经软链接写到别处去。
"""
import atexit
import os
import threading
import time
from collections import deque
from concurrent.futures import Future

RETRY = 5.0   # 写不进去隔多久再试

_cv = threading.Condition()
_whole: dict[str, str] = {}      # 路径 → 最新的全文（还没写）
_lines: deque = deque()          # (路径, 文字)，按交进来的先后
_busy = False                    # 线程手上正有一批在写（flush 要等它写完）
_failing: dict[str, str] = {}    # 路径 → 上一次写不进去的原因：同样的错只说一次，好了再说一句
_waiters: dict[str, list[Future]] = {}   # 路径 → 等着这个文件下一次写完的（rewrite_wait）
_thread: threading.Thread | None = None


def _start():
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_run, name="写盘", daemon=True)
        _thread.start()


def rewrite(path, text: str):
    """整份重写这个文件（排着的旧版本作废）。马上返回，不等写完。"""
    with _cv:
        _whole[os.fspath(path)] = text
        _start()
        _cv.notify_all()


def rewrite_wait(path, text: str) -> Future:
    """和 rewrite 一样排进去，另给一个 Future：这一份（或盖掉它的更新的一份）写进去了就完成，写不进去就带着 OSError 完成。
    给用户在界面上做的选择用（权限模式、思考强度、置顶）：存不下来要当场告诉用户，又不能让事件循环等磁盘；
    异步的地方 await asyncio.wrap_future(它)。写不进去照样隔 RETRY 秒重试，Future 只报第一次的结果。"""
    fut = Future()
    with _cv:
        p = os.fspath(path)
        _whole[p] = text
        _waiters.setdefault(p, []).append(fut)
        _start()
        _cv.notify_all()
    return fut


def append(path, text: str):
    """往这个文件末尾追加一段（自己带好换行）。马上返回，不等写完。"""
    with _cv:
        _lines.append((os.fspath(path), text))
        _start()
        _cv.notify_all()


def _open(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666)


def _write_all(fd: int, data: bytes):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _write_whole(path: str, text: str):
    fd = _open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        _write_all(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _write_lines(path: str, text: str):
    # O_APPEND：别的进程（状态栏脚本、开窗服务）同时往同一个文件追加也不会互相盖掉
    fd = _open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        _write_all(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _note(path: str, err: OSError | None):
    """写不进去时说一次；同一个文件好了再说一句。"""
    if err is None:
        if _failing.pop(path, None) is not None:
            print(f"写盘线程：{path} 又写得进去了", flush=True)
        return
    why = f"{type(err).__name__}: {err}"
    if _failing.get(path) != why:
        _failing[path] = why
        print(f"写盘线程：写不进 {path}（{why}），{RETRY:g} 秒后再试，内存里的照旧是准的", flush=True)


def _run():
    global _busy
    while True:
        with _cv:
            while not (_whole or _lines):
                _cv.wait()
            lines, whole, waiters = list(_lines), dict(_whole), dict(_waiters)
            _lines.clear()
            _whole.clear()
            _waiters.clear()
            _busy = True
        failed_lines, failed_whole = [], {}
        # 追加的：同一个文件连着的几段并成一次写；某个文件写不进去，它后面排着的也先不写，保住顺序
        stuck: set[str] = set()
        i = 0
        while i < len(lines):
            path, j = lines[i][0], i
            while j < len(lines) and lines[j][0] == path:
                j += 1
            chunk = lines[i:j]
            if path in stuck:
                failed_lines += chunk
            else:
                try:
                    _write_lines(path, "".join(t for _, t in chunk))
                    _note(path, None)
                except OSError as e:
                    _note(path, e)
                    stuck.add(path)
                    failed_lines += chunk
            i = j
        for path, text in whole.items():
            err = None
            try:
                _write_whole(path, text)
                _note(path, None)
            except OSError as e:
                _note(path, e)
                failed_whole[path] = text
                err = e
            for fut in waiters.get(path, ()):
                if err is None:
                    fut.set_result(None)
                else:
                    fut.set_exception(err)
        with _cv:
            _lines.extendleft(reversed(failed_lines))   # 没写进去的排回最前面，顺序不变
            for path, text in failed_whole.items():
                _whole.setdefault(path, text)   # 这期间又交来了新的一份就写新的
            _busy = False
            _cv.notify_all()
        if failed_lines or failed_whole:
            time.sleep(RETRY)


def error(path) -> str | None:
    """这个文件最近一次写不进去的原因；写得进去（或还没写过）是 None。要把存盘失败告诉用户的接口用它。"""
    return _failing.get(os.fspath(path))


def pending() -> int:
    """还排着几份没写（整份的按文件数，追加的按段数）。"""
    with _cv:
        return len(_whole) + len(_lines) + (1 if _busy else 0)


def flush(timeout: float = 10.0) -> bool:
    """等排着的全部写完，最多等 timeout 秒。没写完就把剩下的是哪些文件记进服务输出，返回 False。"""
    end = time.monotonic() + timeout
    with _cv:
        while _whole or _lines or _busy:
            left = end - time.monotonic()
            if left <= 0:
                files = sorted(set(_whole) | {p for p, _ in _lines})
                print(f"写盘线程：等了 {timeout:g} 秒还有 {len(_whole) + len(_lines)} 份没写进去：{'、'.join(files) or '（正在写的那一批）'}",
                      flush=True)
                return False
            _cv.wait(left)
    return True


atexit.register(flush, 5.0)
