"""读完、解析完大块的会话记录以后，把空出来的内存还给系统（glibc 的 malloc_trim）。

实测：多用户实例跑了七十多分钟常驻就有 5.5GB，主堆只有 131MB，其余几乎全在线程各自的内存池里（75 块几十 MB 的匿名映射）。
另起进程照线上的做法打开那个 291MB 的会话、算两次花费视图，留下来的 Python 对象只有几 MB，进程常驻却从 50MB 涨到 1.3GB：
解析大文件时冲上去的几百 MB 释放以后留在内存池里，glibc 不会自己还，只有重启才收得回来。
所以几处读大文件的地方（session.Session.poll、calls.scan、spendmap）读完都报一下读了多少，攒够 TRIM_BYTES 就还一次。
malloc_trim 会挨个内存池整理（glibc 2.8 起），ctypes 调用时放开全局锁，不挡别的线程；一次几十毫秒。
不是 glibc 的系统（找不到这个函数）就不还，导入时打一行日志说明。只用标准库：状态栏脚本导入 calls 时也会带上它。
"""
import ctypes
import ctypes.util
import threading

TRIM_BYTES = 64 << 20   # 读过这么多字节才还一次：平常每 0.2 秒读几 KB 新内容，不值得每次都整理

_lock = threading.Lock()
_since = 0
try:
    _trim = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6").malloc_trim
    _trim.argtypes, _trim.restype = [ctypes.c_size_t], ctypes.c_int
except (OSError, AttributeError) as e:
    _trim = None
    print(f"找不到 glibc 的 malloc_trim，读完大文件后空出来的内存不会还给系统：{e}", flush=True)


def read(nbytes: int):
    """报一下刚读、解析完多少字节；攒够了就还一次内存。"""
    global _since
    if not _trim or nbytes <= 0:
        return
    with _lock:
        _since += nbytes
        if _since < TRIM_BYTES:
            return
        _since = 0
    _trim(0)
