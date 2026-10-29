"""以运行 Claude 的那个系统用户的身份读写「Claude 那边」的东西：会话记录、各号的配置目录、工作目录、Claude 跑起来的进程。

多人共用的实例可以让查看器以专用系统用户运行，Claude 以另一个用户运行（instance.RUN_AS）：
大家的 Claude 都是 RUN_AS，查看器要是也是这个用户，它们就能改查看器的数据（用户和密码、会话主人、账本、预定、调用记录），
也能杀掉、冒充查看器和网络闸门。分开以后，查看器自己却读不到 RUN_AS 的会话了：
Claude Code 写会话记录时把文件权限定成只有 RUN_AS 能读，加用户组、ACL 都不管用。所以查看器经 sudo 起一个以 RUN_AS
身份运行的小助手（hostfs_helper.py，和查看器之间只有一对 socketpair），Claude 那边的文件一律由它打开，
再把打开的文件描述符交回来（SCM_RIGHTS），读写照常在查看器这边做；查状态、列目录、找文件、在那边跑个小命令、
给那边的进程发信号也都由它代办。拉起 CLI 不经它，经 sudo 直接换用户（chat.cli_launch、claude_as_user.sh）。

用法：凡是指向 Claude 那边的路径，一律用这里的 Path（和 pathlib.Path 一样用）和 HOME（那个用户的家目录）；
查看器自己的数据目录（instance.DATA）照旧用 pathlib.Path。用户或 CLI 交过来的路径（按路径打开会话、浏览文件夹、取图片、
钩子里的记录路径）尤其要用这里的 Path：用 pathlib.Path 就成了以查看器自己的身份去读，读得到它存密码和账本的目录。
Path 对象固定携带运行身份，复制和派生路径保持身份；共用环境仍使用 instance.RUN_AS。
本机同身份直接读写，跨身份通过各自的小助手；for_user 只接受已登记的独立用户。
测试时设环境变量 CC_VIEWER_HOSTFS=helper，同一个用户也走小助手。
只用标准库（状态栏脚本、只读模式都会经 calls.py 导入它）。
"""
import array
import base64
import contextlib
import errno
import io
import json
import os
import pathlib
import pwd
import socket
import struct
import subprocess
import sys
import threading
import time

import instance

ME = pwd.getpwuid(os.getuid()).pw_name
SPLIT = bool(instance.RUN_AS) and (instance.RUN_AS != ME or os.environ.get("CC_VIEWER_HOSTFS") == "helper")
HELPER = pathlib.Path(__file__).resolve().parent / "hostfs_helper.py"
TIMEOUT = 20   # 一件事最多等这么久（比如路过一个没响应的 sshfs 挂载），过了就当出错，不让调用的地方一直卡着
OPS = {"stat": "查看", "open": "打开", "listdir": "列出", "glob": "查找", "realpath": "解析", "readlink": "读软链接",
       "mkdir": "建目录", "read": "读取", "run": "执行命令", "kill": "发信号", "tree": "清点"}


class _Client:
    """和小助手之间的那条连接。几个线程可以同时发请求：按编号认回复，由一个收信线程分发；
    文件描述符随回复的头几个字节到达，按到达的先后排队，解析到带描述符的那一帧时认领。
    小助手没了（被杀、sudo 起不来）就让还在等的请求都报错，下一个请求再重新拉起。"""

    def __init__(self, run_as=None):
        self.run_as = run_as or instance.RUN_AS or ME
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None
        self.proc: subprocess.Popen | None = None
        self.pending: dict[int, list] = {}   # 编号 → [Event, 回复, 描述符]
        self.seq = 0
        self.started = 0.0

    def _start(self):
        if self.started and time.monotonic() - self.started < 1:   # 刚起过又没了：别一直狂拉
            time.sleep(1)
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        argv = [sys.executable, "-I", str(HELPER)]
        if self.run_as in _private:
            argv = ["sudo", "-n", "--", "/usr/local/libexec/cc-viewer-isolation", "hostfs", _private[self.run_as]]
        elif self.run_as != ME:
            argv = ["sudo", "-n", "-u", self.run_as, "--", *argv]
        try:
            # 输出接到查看器自己的日志里；单独一个会话，终端里按 Ctrl+C 不会直接打到它
            self.proc = subprocess.Popen(argv, stdin=b, stdout=subprocess.DEVNULL, start_new_session=True)
        finally:
            b.close()
        self.sock, self.started = a, time.monotonic()
        threading.Thread(target=self._recv, args=(a, self.proc), daemon=True, name="hostfs-recv").start()

    def _recv(self, sock: socket.socket, proc: subprocess.Popen):
        buf, fds = b"", []
        why = "小助手退出了"
        try:
            while True:
                data, anc, _, _ = sock.recvmsg(1 << 16, socket.CMSG_SPACE(64 * 4), socket.MSG_CMSG_CLOEXEC)
                for level, kind, raw in anc:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        got = array.array("i")
                        got.frombytes(raw[:len(raw) - len(raw) % got.itemsize])
                        fds.extend(got)
                if not data:
                    break
                buf += data
                while len(buf) >= 4:
                    (n,) = struct.unpack("!I", buf[:4])
                    if len(buf) < 4 + n:
                        break
                    msg, buf = json.loads(buf[4:4 + n]), buf[4 + n:]
                    fd = fds.pop(0) if msg.get("fd") and fds else None
                    slot = self.pending.pop(msg.get("id"), None)
                    if slot:
                        slot[1], slot[2] = msg, fd
                        slot[0].set()
                    elif fd is not None:   # 等的人已经超时走了
                        os.close(fd)
        except (OSError, ValueError) as e:
            why = f"和小助手的连接出错：{e}"
        with self.lock:
            if self.sock is sock:
                self.sock = None
            dead = [k for k, v in self.pending.items() if v[3] is sock]
            for k in dead:
                slot = self.pending.pop(k)
                slot[1] = {"ok": False, "errno": errno.EIO, "msg": f"{why}（以 {self.run_as} 身份读写的小助手，看查看器的日志）"}
                slot[0].set()
        sock.close()
        for fd in fds:
            os.close(fd)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    def call(self, op: str, timeout: float = TIMEOUT, **kw):
        """办一件事，返回 (结果, 文件描述符或 None)；那边出错就在这边抛同样的 OSError。"""
        with self.lock:
            if self.sock is None:
                self._start()
            self.seq += 1
            rid, sock = self.seq, self.sock
            slot = self.pending[rid] = [threading.Event(), None, None, sock]
            data = json.dumps({"id": rid, "op": op, **kw}, ensure_ascii=False).encode()
            try:
                sock.sendall(struct.pack("!I", len(data)) + data)
            except OSError as e:
                self.pending.pop(rid, None)
                raise OSError(errno.EIO, f"发不出去给小助手：{e}") from None
        if not slot[0].wait(timeout):
            with self.lock:
                self.pending.pop(rid, None)
            raise OSError(errno.ETIMEDOUT, f"以 {self.run_as} 身份{OPS.get(op, op)}等了 {timeout:.0f} 秒没回来",
                          kw.get("path") or kw.get("root"))
        msg = slot[1]
        if not msg.get("ok"):
            raise OSError(msg.get("errno") or errno.EIO, msg.get("msg") or "小助手出错", msg.get("file"))
        return msg.get("r"), slot[2]


_client = _Client()
_private = {}
_clients = {}
_clients_lock = threading.Lock()


def register_private(user, key):
    import re
    # 用户名只是这边认客户端用的；真正换成谁由系统组件按编号查它自己的登记（private_root.py），这里只防乱写的名字。
    # 新建的环境按登录名起用户名（isolation.create），老版本建的是 cciso_ 加编号
    if not re.fullmatch(r"[0-9a-f]{32}", key) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", user):
        raise ValueError("独立环境身份不合法")
    with _clients_lock:
        _private[user] = key


def client_for(user=None):
    if not user or user == (instance.RUN_AS or ME):
        return _client if SPLIT else None
    with _clients_lock:
        if user not in _private:
            raise PermissionError("没有登记这个独立系统用户")
        if user not in _clients:
            _clients[user] = _Client(user)
        return _clients[user]


def for_user(path, user):
    client_for(user)   # 未登记的名字不能借文件接口变成 sudo 的目标
    return HostPath(path, run_as=user)


class HostPath(pathlib.PosixPath):
    """和 pathlib.Path 一样用，只是碰文件系统的那几样（查状态、打开、列目录、找文件、解析软链接、建目录）由小助手以
    运行 Claude 的用户的身份去做。exists、is_file、is_dir、read_bytes、read_text 这些都是基于 stat 和 open 的，跟着就对了。
    拼出来的路径（/、parent、with_suffix……）还是 HostPath。没列在这里的写操作（删除、改名……）照旧以查看器自己的身份做，
    对那边的文件做不成，本来也不该做。"""

    def __init__(self, *segments, run_as=None):
        self.run_as = run_as or (getattr(segments[0], "run_as", None) if segments else None) or instance.RUN_AS or ME
        super().__init__(*segments)

    def with_segments(self, *segments):
        return type(self)(*segments, run_as=self.run_as)

    def __eq__(self, other):
        return isinstance(other, HostPath) and super().__eq__(other) is True and self.run_as == other.run_as

    def __hash__(self):
        return hash((super().__hash__(), self.run_as))

    @property
    def client(self):
        return client_for(self.run_as)

    def stat(self, *, follow_symlinks=True):
        if not (c := self.client):
            return super().stat(follow_symlinks=follow_symlinks)
        r, _ = c.call("stat", path=str(self), follow=follow_symlinks)
        return os.stat_result(r)

    def open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if not (c := self.client):
            return super().open(mode, buffering, encoding, errors, newline)
        if "b" not in mode:
            encoding = io.text_encoding(encoding)
        _, fd = c.call("open", path=str(self), mode=mode)
        return io.open(fd, mode, buffering, encoding, errors, newline)

    def iterdir(self):
        if not (c := self.client):
            yield from super().iterdir()
            return
        names, _ = c.call("listdir", path=str(self))
        for name in names:
            yield self / name

    def glob(self, pattern, *, case_sensitive=None):
        if not (c := self.client):
            yield from super().glob(pattern, case_sensitive=case_sensitive)
            return
        found, _ = c.call("glob", root=str(self), pattern=pattern)
        for p in found:
            yield self.with_segments(p)

    def rglob(self, pattern, *, case_sensitive=None):
        return self.glob(f"**/{pattern}", case_sensitive=case_sensitive)

    def resolve(self, strict=False):
        if not (c := self.client):
            return super().resolve(strict=strict)
        r, _ = c.call("realpath", path=str(self.absolute()), strict=strict)
        return self.with_segments(r)

    def readlink(self):
        if not (c := self.client):
            return super().readlink()
        r, _ = c.call("readlink", path=str(self))
        return self.with_segments(r)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if not (c := self.client):
            return super().mkdir(mode, parents, exist_ok)
        c.call("mkdir", path=str(self), mode=mode, parents=parents, exist_ok=exist_ok)

    def expanduser(self):
        """运行身份随路径传播，转换、拼接和解析软链接都不改变它。"""
        if not (self.drive or self.root) and self.parts and self.parts[0] == "~":
            home = pwd.getpwnam(self.run_as).pw_dir if self.run_as in _private else instance.HOME
            return self.with_segments(home, *self.parts[1:])
        return self


class Path(pathlib.PosixPath):
    """保留原来同身份时使用标准 Path 的行为（状态栏可能仍由 Python 3.10 执行）。
    跨身份对象只由 Python 3.12 的查看器构造；再次包装时不得丢掉已有身份。
    """
    def __new__(cls, *segments, run_as=None):
        user = run_as or (getattr(segments[0], "run_as", None) if segments else None)
        if user or SPLIT:
            return HostPath(*segments, run_as=user)
        return pathlib.Path(*segments)


HOME = Path(instance.HOME)


def read_small(path, limit: int = 1 << 20, run_as=None) -> bytes:
    """在那边读完再拿回来。/proc 里有的文件（比如 wchan）按读的那一刻是谁来决定给不给看，打开了交过来再读就看不到了。"""
    c = client_for(run_as or getattr(path, "run_as", None))
    if c is None:
        with open(path, "rb") as f:
            return f.read(limit)
    r, _ = c.call("read", path=str(path), limit=limit)
    return base64.b64decode(r)


def tree(root, patterns) -> dict[str, list]:
    """root 底下符合这几个式样（glob 的写法）的文件 → [大小, 修改时间]，取的时候没了的不列。
    总览要知道几千个会话和子代理文件变没变，经小助手一个个查要几秒，所以一趟清点完再拿回来（代码和小助手的 tree 那段一样）。
    独立环境的小助手是系统组件装的一份拷贝，重装以前不认这件事，会报「不认识的操作」，由调用的地方改回一个个查。"""
    c = client_for(getattr(root, "run_as", None))
    if c is not None:
        r, _ = c.call("tree", root=str(root), patterns=list(patterns))
        return r
    out = {}
    for pattern in patterns:
        for p in pathlib.Path(root).glob(pattern):
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[str(p)] = [st.st_size, st.st_mtime]
    return out


def run(argv: list[str], timeout: float = 30, cwd=None, run_as=None) -> tuple[int | None, bytes, bytes]:
    """以那边的身份跑一个小命令（tmux、探挂载的 stat……），等它跑完：(退出码，超时是 None；标准输出；标准错误)。"""
    c = client_for(run_as or getattr(cwd, "run_as", None))
    if c is None:
        try:
            p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                               cwd=str(cwd) if cwd else None)
        except subprocess.TimeoutExpired as e:
            return None, e.stdout or b"", e.stderr or b""
        return p.returncode, p.stdout, p.stderr
    r, _ = c.call("run", timeout=timeout + 5, secs=timeout, argv=[str(a) for a in argv], cwd=str(cwd) if cwd else None)
    return r["rc"], base64.b64decode(r["out"]), base64.b64decode(r["err"])


def kill(pid: int, sig: int, run_as=None):
    """给那边的进程发信号（比如收掉停在 FUSE 上的 CLI）。查看器自己没有权限动别的用户的进程。"""
    c = client_for(run_as)
    if c is None:
        os.kill(pid, sig)
    else:
        c.call("kill", pid=pid, sig=int(sig))
