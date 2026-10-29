#!/usr/bin/env python3
"""给 Claude Code 的会话记录补硬链接存档（Claude Code 会把超过 cleanupPeriodDays（默认 30 天）的会话记录、
子代理记录和工具输出整个删掉，以前的会话就此回忆不到了）。

    python3 -I archive_transcripts.py <projects 目录> <存档目录>

把 projects 底下每个文件在存档目录的同一相对位置建一个硬链接：和原文件是同一份数据，不多占硬盘，Claude Code 往原文件追加的内容
存档里也看得到；原文件被删了，数据留在存档里。存档只增不删：
- 已有同一个文件（inode 相同）就跳过；
- 同一个位置换成了另一个文件（Claude Code 整份重写过），旧的改名成「原名.replaced-时刻」留着，再链新的；
- auto memory 的 memory/ 目录跳过：它不在清理范围里，编辑时又常整份重写，链进来只会堆出一串旧版本。
硬链接只能建在同一块硬盘上，存档目录要和 projects 在同一个文件系统；要以会话记录的属主运行（内核的 protected_hardlinks 不让链别人的文件）。
由各自系统用户的 crontab 每 10 分钟跑一次（README「会话记录的存档」）。只在链了新文件或出错时打印，免得日志每次一行。
"""
import os
import sys
import time


def main(src, dst):
    if os.stat(src).st_dev != os.stat(dst).st_dev:
        sys.exit(f"{dst} 和 {src} 不在同一个文件系统上，建不了硬链接")
    linked = replaced = errors = 0
    stamp = time.strftime("%Y%m%d%H%M%S")
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        dirs[:] = [d for d in dirs if not (d == "memory" and rel.count(os.sep) == 0 and rel != ".")]
        out = dst if rel == "." else os.path.join(dst, rel)
        for name in files:
            p, q = os.path.join(root, name), os.path.join(out, name)
            try:
                s = os.lstat(p)
                if not os.path.isfile(p) or os.path.islink(p):
                    continue
                try:
                    t = os.lstat(q)
                except FileNotFoundError:
                    t = None
                if t is not None:
                    if t.st_ino == s.st_ino:
                        continue
                    os.rename(q, f"{q}.replaced-{stamp}")
                    replaced += 1
                os.makedirs(out, mode=0o700, exist_ok=True)
                os.link(p, q)
                linked += 1
            except FileNotFoundError:
                continue   # 刚列出来就被删了或改名了，下次再说
            except OSError as e:
                errors += 1
                print(f"{time.strftime('%F %T')} 链 {p} 没成：{e}", flush=True)
    if linked or replaced or errors:
        print(f"{time.strftime('%F %T')} {src} → {dst}：新链 {linked} 个，整份重写过的旧版留了 {replaced} 个，出错 {errors} 个", flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])))
