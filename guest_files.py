"""运行 Claude 的那个用户（instance.RUN_AS）要直接运行或读取的代码目录文件，以及以它的身份查一遍权限还在不在。

代码目录对别的用户不可读时，这些文件要按 ACL 单独放行。放行挂在文件上，
改文件的工具要是写一份新文件再换掉旧的，新文件只带目录的默认 ACL，放行就没了：比如 claude_via_bashrc.sh 改完丢了放行，
RUN_AS 执行不了它，所有会话都拉不起 CLI，只报「退出码 1」。
所以实例启动时（server.py）查一遍，重启脚本里也可以先跑一次。
单独运行：CC_VIEWER_DATA=<数据目录> python3 guest_files.py，有问题退出码 1。"""
import os
import pwd
import shlex
import subprocess
import sys
from pathlib import Path

import instance

CODE = Path(__file__).resolve().parent
# 文件 → 要的权限（r 读、x 运行）。都是 Claude 那边直接用到的：拉起 CLI 的启动脚本、hostfs 的读写小助手、
# 集中登录的 apiKeyHelper、人设卡钩子，以及会话里会敲的 ccssh、ccpersona、ccname、ccrun 和实验脚本导入的包装 SDK
NEED = {
    "claude_via_bashrc.sh": "rx",
    "hostfs_helper.py": "r",
    "cc_auth_helper.py": "r",
    "persona_hook": "rx",
    "ccssh": "rx",
    "ccpersona": "rx",
    "ccname": "rx",
    "ccrun": "rx",
    # 包装 SDK（实验脚本 import ccviewer_sdk）：目录上设了 RUN_AS 的默认权限，新文件会带上，改写过的文件照样要查
    "sdk_client": "rx",
    "sdk_client/ccviewer_sdk": "rx",
    "sdk_client/ccviewer_sdk/__init__.py": "r",
    "sdk_client/ccviewer_sdk/_client.py": "r",
}


def problems() -> list[str]:
    """以 RUN_AS 的身份查 NEED 里每个文件，返回缺了的权限（中文说明）；查看器和 Claude 是同一个用户时不用查，返回空。
    sudo 本身用不了也算一条问题，「查不了」不能当成「没问题」。"""
    me = pwd.getpwuid(os.getuid()).pw_name
    user = instance.RUN_AS
    if not user or user == me:
        return []
    checks = [("x", CODE, "进入代码目录")]
    for name, need in NEED.items():
        for flag in need:
            checks.append((flag, CODE / name, "读" if flag == "r" else "运行"))
    # 一次 sudo 查完：每一项没过就打印它的序号
    script = "; ".join(f"[ -{flag} {shlex.quote(str(path))} ] || echo {i}" for i, (flag, path, _) in enumerate(checks))
    try:
        p = subprocess.run(["sudo", "-n", "-u", user, "--", "/bin/sh", "-c", script],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"没能以 {user} 的身份检查（{type(e).__name__}: {e}）"]
    if p.returncode != 0:
        return [f"没能以 {user} 的身份检查（sudo 退出码 {p.returncode}：{p.stderr.strip()}）"]
    out = []
    for i in p.stdout.split():
        flag, path, what = checks[int(i)]
        out.append(f"{user} 不能{what} {path}")
    return out


if __name__ == "__main__":
    bad = problems()
    for line in bad:
        print(line)
    if bad:
        print(f"补法：setfacl -m u:{instance.RUN_AS}:rx <文件>（只读的用 r），由代码目录的主人执行")
    sys.exit(1 if bad else 0)
