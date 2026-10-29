"""hostfs.py 的小助手：以运行 Claude 的那个系统用户（instance.RUN_AS）的身份，替查看器打开文件、查状态、列目录、
找文件、跑个小命令、给那边的进程发信号（为什么要它、怎么用见 hostfs.py 开头）。

由查看器经 sudo 拉起（python -I，不从当前目录导入任何东西），标准输入是和查看器之间的一个 socketpair，别的什么都不听。
一来一回都是「4 字节长度 + JSON」：请求 {"id", "op", ...参数}，回复 {"id", "ok": true, "r": 结果, "fd": 带没带文件描述符}
或 {"id", "ok": false, "errno", "msg", "file"}。打开文件时把文件描述符随回复一起交回去（SCM_RIGHTS），读写在查看器那边做，
大文件也不用经这里转一遍。每个请求一个线程，某个挂载卡住只卡住那一个请求。查看器那头一关就退出。
只用标准库。
"""
import array
import base64
import errno
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import threading

sock = socket.socket(fileno=os.dup(0))
os.close(0)
send_lock = threading.Lock()


def flags_of(mode: str) -> int:
    """open() 的模式字符串 → os.open 的标志，和内置 open 的规矩一样。"""
    plus = "+" in mode
    if "r" in mode:
        f = os.O_RDWR if plus else os.O_RDONLY
    elif "w" in mode:
        f = (os.O_RDWR if plus else os.O_WRONLY) | os.O_CREAT | os.O_TRUNC
    elif "a" in mode:
        f = (os.O_RDWR if plus else os.O_WRONLY) | os.O_CREAT | os.O_APPEND
    elif "x" in mode:
        f = (os.O_RDWR if plus else os.O_WRONLY) | os.O_CREAT | os.O_EXCL
    else:
        raise ValueError(f"认不出的打开方式：{mode}")
    return f | os.O_CLOEXEC


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def handle(req: dict):
    """办一件事：返回 (结果, 要交回去的文件描述符或 None)。"""
    op = req["op"]
    if op == "stat":
        st = os.stat(req["path"], follow_symlinks=req.get("follow", True))
        return [*st[:10], st.st_atime, st.st_mtime, st.st_ctime, st.st_atime_ns, st.st_mtime_ns, st.st_ctime_ns], None
    if op == "open":
        return None, os.open(req["path"], flags_of(req["mode"]), 0o666)
    if op == "listdir":
        return os.listdir(req["path"]), None
    if op == "glob":   # 和 pathlib 的 glob 同一套规矩（隐藏文件也算），所以直接用它
        return [str(p) for p in pathlib.Path(req["root"]).glob(req["pattern"])], None
    if op == "tree":   # 一趟清点 root 底下符合这几个式样的文件 → [大小, 修改时间]，取的时候没了的不列（hostfs.tree，两边是同一段代码）
        out = {}
        for pattern in req["patterns"]:
            for p in pathlib.Path(req["root"]).glob(pattern):
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out[str(p)] = [st.st_size, st.st_mtime]
        return out, None
    if op == "realpath":
        return os.path.realpath(req["path"], strict=req.get("strict", False)), None
    if op == "readlink":
        return os.readlink(req["path"]), None
    if op == "mkdir":
        pathlib.Path(req["path"]).mkdir(req.get("mode", 0o777), req.get("parents", False), req.get("exist_ok", False))
        return None, None
    if op == "read":   # 在这边读完再交回去：/proc 里有的文件（比如 wchan）按读的那一刻是谁来判断给不给看
        with open(req["path"], "rb") as f:
            return b64(f.read(req.get("limit", 1 << 20))), None
    if op == "run":
        try:
            p = subprocess.run(req["argv"], stdin=subprocess.DEVNULL, capture_output=True, timeout=req.get("secs", 30),
                               cwd=req.get("cwd"))
        except subprocess.TimeoutExpired as e:
            return {"rc": None, "out": b64(e.stdout or b""), "err": b64(e.stderr or b"")}, None
        return {"rc": p.returncode, "out": b64(p.stdout), "err": b64(p.stderr)}, None
    if op == "kill":
        os.kill(int(req["pid"]), int(req["sig"]))
        return None, None
    raise ValueError(f"不认识的操作：{op}")


def reply(rid, body: dict, fd: int | None = None):
    data = json.dumps({"id": rid, **body}, ensure_ascii=False).encode()
    frame = struct.pack("!I", len(data)) + data
    with send_lock:
        try:
            if fd is None:
                sock.sendall(frame)
            else:   # 描述符挂在这一帧的头几个字节上，查看器那边按到达顺序认领
                n = sock.sendmsg([frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))])
                sock.sendall(frame[n:])
        except OSError:
            os._exit(0)   # 查看器那头没了
        finally:
            if fd is not None:
                os.close(fd)


def serve(req: dict):
    rid = req.get("id")
    try:
        r, fd = handle(req)
    except OSError as e:
        reply(rid, {"ok": False, "errno": e.errno or errno.EIO, "msg": e.strerror or str(e), "file": e.filename})
        return
    except Exception as e:   # 参数不对之类：照样回一句，不能让查看器那边干等到超时
        reply(rid, {"ok": False, "errno": errno.EINVAL, "msg": f"{type(e).__name__}: {e}", "file": None})
        return
    reply(rid, {"ok": True, "r": r, "fd": fd is not None}, fd)


def read_exact(n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def main():
    try:
        while True:
            (n,) = struct.unpack("!I", read_exact(4))
            req = json.loads(read_exact(n))
            threading.Thread(target=serve, args=(req,), daemon=True).start()
    except (EOFError, OSError):
        pass
    except Exception as e:   # 收到的东西解析不了：说明两边对不上，退出让查看器重新拉起
        print(f"hostfs 小助手（{os.getpid()}）收到解析不了的请求，退出：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
