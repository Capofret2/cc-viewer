"""门卫连接电脑时的限速中转：ssh 的 ProxyCommand 拉起它，连接电脑在本机的反向端口，
在 ssh 和这个端口之间来回搬数据，两个方向各按一个令牌桶限速。

电脑连接可能和查看器共用中转带宽，大文件传输占满链路会让网页访问也变慢。
限速放在门卫这里，rsync、scp、sftp、sshfs、普通命令一样管得住，用户的命令和电脑上装的东西都不用改。
只限单条连接，同时开几个传输会叠加。

用法：gw_throttle.py 主机 端口 每秒字节数。由门卫写进配置（sshgw.config_for），不给别人用。
"""
import os
import socket
import sys
import threading
import time

CHUNK = 16 * 1024
BURST = 256 * 1024   # 敲命令、看输出这种小流量不等桶，突发之内直接过


class Bucket:
    def __init__(self, rate: float):
        self.rate, self.tokens, self.t = rate, float(BURST), time.monotonic()

    def take(self, n: int):
        now = time.monotonic()
        self.tokens = min(BURST, self.tokens + (now - self.t) * self.rate) - n
        self.t = now
        if self.tokens < 0:   # 欠着的下次补上：睡到正好还清
            time.sleep(-self.tokens / self.rate)


def main():
    host, port, rate = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    try:
        sock = socket.create_connection((host, port), timeout=15)
    except OSError as e:
        sys.stderr.write(f"ssh: connect to host {host} port {port}: {e.strerror or e}\n")
        sys.exit(255)
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def up():   # ssh → 电脑；ssh 写完了就半关，回来的方向照常
        b = Bucket(rate)
        try:
            while data := os.read(0, CHUNK):
                b.take(len(data))
                sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            os._exit(0)

    threading.Thread(target=up, daemon=True).start()
    b = Bucket(rate)
    try:   # 电脑 → ssh；这边断了整个就结束
        while data := sock.recv(CHUNK):
            b.take(len(data))
            view = memoryview(data)
            while view:
                view = view[os.write(1, view):]
    except OSError:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
