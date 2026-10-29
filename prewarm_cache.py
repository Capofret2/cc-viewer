"""重启前替实例预写调用表和花费存盘：实例启动后第一次打开「全部会话」、大会话的花费视图就直接读存盘，不用冷读。
存盘只认文件内容和代码版本，不在乎是谁写的（见 calls.py、spendmap.py「存盘」），所以可以趁实例还在跑旧代码时先写好：
旧进程不认这两个目录，不冲突；写完到重启之间新增的内容，启动后按增量接着读。代码（调用表：calls.py、peak.py 整个文件；
花费：spendmap.DISK_CODE，codever.py 算版本）预写以后又改过，存盘就整份作废、退回冷读（不会算错），所以放在代码不再改、准备重启之前跑。
重启脚本（restart.sh）重启前先跑它：存盘本来就对得上时只是接着读，几秒就完；
代码改过才真的从头读一遍（几千个会话约 45 秒），这期间旧实例照常服务。

以实例的运行用户、指着它的数据目录跑（不导入 server.py：对着真数据目录不加载索引、用户这些，只读会话记录、只写两个存盘目录）：
  cd <代码目录>; CC_VIEWER_DATA=<数据目录> nice -n 10 python3 prewarm_cache.py
共用环境和数据目录里登记的每个独立环境都写（isolation.roots）：独立环境的文件和服务器一样经那个环境的小助手读，
存盘的键带着运行身份，和服务器读到的是同一份。哪个环境清点不成，就按路径一个个找；读不了的文件跳过，最后列出来，重启后那些照旧冷读。
花费只拆大会话（主文件超过 --spend-mb，或子代理超过 --spend-subs 个），其余冷拆本来就快；只有半小时没动过的文件会存下（spendmap.STABLE_SECS）。
出错（导入失败、登记损坏）退出码不是 0，重启脚本看到就不重启。

抽查（调用表直接进账本，要谨慎）：从存盘读回来的调用表里随机挑一批（最多 --audit-files 个、合计 --audit-gb GB），
用现在的代码从头读到存下的位置，逐条比；有一个对不上，就说明存盘的版本没认出该作废的改动，把两种存盘整份删掉、全部从头读一遍再存，
日志里写明哪些对不上。--audit-all 每个都比（全量对照用，要把全部会话记录读一遍）。
"""
import argparse
import json
import os
import random
import threading
import time

import calls
import hostfs
import isolation
import spendmap
from instance import DATA

ap = argparse.ArgumentParser()
ap.add_argument("--spend-mb", type=float, default=100)
ap.add_argument("--spend-subs", type=int, default=100)
ap.add_argument("--audit-files", type=int, default=300)
ap.add_argument("--audit-gb", type=float, default=2)
ap.add_argument("--audit-all", action="store_true")
args = ap.parse_args()

restored: list = []   # 这次从存盘读回来的文件（抽查从里面挑）
_restore = calls._restore


def _counting_restore(path):
    st = _restore(path)
    if st is not None:
        restored.append(path)
    return st


calls._restore = _counting_restore   # file_calls 按名字找 _restore，换掉就数得到


def audit() -> list:
    """抽查从存盘读回来的调用表，返回对不上的 [(文件, 原因)]。"""
    pool = [p for p in restored if p in calls._files]
    random.shuffle(pool)
    budget, n, bad = (float("inf") if args.audit_all else args.audit_gb * 1e9), 0, []
    t = time.perf_counter()
    for p in pool:
        if not args.audit_all and n >= args.audit_files:
            break
        st = calls._files[p]
        if st["off"] > budget:
            continue
        budget -= st["off"]
        n += 1
        cold, off, meta = calls._scan(p, 0, st["off"])
        if meta is None or meta["ino"] != st.get("ino"):
            continue   # 读不了、或者文件刚换掉：不是存盘的问题（下次读到时 file_calls 自己会从头来）
        if off != st["off"]:
            bad.append((p, f"从头读到 {off}，存下的读到 {st['off']}"))
        elif cold != st["calls"]:
            diff = sum(1 for k in cold.keys() | st["calls"].keys() if cold.get(k) != st["calls"].get(k))
            bad.append((p, f"{diff} 次调用不一样"))
    print(f"抽查存盘：{len(restored)} 个从存盘读回来的文件里比了 {n} 个，{len(bad)} 个对不上，{time.perf_counter() - t:.1f} 秒", flush=True)
    return bad


def read_cwd(path) -> str:
    """和 server.read_cwd 一样：第一条写了工作目录的行（花费存盘的键带着它，得和服务器算的一样）。"""
    with path.open("rb") as f:
        for line in f:
            if b'"cwd"' in line:
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    continue
                if cwd:
                    return cwd
    return ""


def listing(root) -> dict[str, list]:
    """这个根目录底下的会话和子代理文件 → [大小, 修改时间]：和服务器一样先一趟清点（calls.snapshot 的做法），
    小助手不认清点（独立环境装的旧拷贝）就按式样找、一个个查。"""
    try:
        return hostfs.tree(root, calls.TREE)
    except OSError as e:
        print(f"  清点没成（{e}），改成一个个查", flush=True)
    out = {}
    for pattern in calls.TREE:
        for p in root.glob(pattern):
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p)] = [st.st_size, st.st_mtime]
    return out


calls.enable_disk(DATA / "calls_cache")
spendmap.enable_disk(DATA / "spend_cache")
print(f"存盘版本：调用表 {calls.VERSION[:12]}，花费 {spendmap.VERSION[:12]}", flush=True)
roots = list(isolation.roots(internal=True))   # 共用环境在最前；登记损坏在这里就报错退出
skipped: list[str] = []
t0 = time.perf_counter()
listed = []   # [(根目录, 运行身份, 清点结果, Snapshot, 路径字符串 → 带身份的路径)]
for root in roots:
    who = getattr(root, "run_as", None)
    t = time.perf_counter()
    try:
        stats = listing(root)
    except OSError as e:
        print(f"{root}（{who}）：列不出来，跳过：{e}", flush=True)
        skipped.append(f"{root}：{e}")
        continue
    snap = calls.Snapshot()
    snap.add(stats)
    paths = {s: calls.Path(s, run_as=who) for s in stats}   # 带上运行身份：存盘的键是「身份 + 路径」，要和服务器读到的一样
    print(f"{root}（{who}）：{len(stats)} 个会话和子代理文件，{sum(s for s, _ in stats.values()) / 1e9:.1f}GB，"
          f"清点 {time.perf_counter() - t:.1f} 秒", flush=True)
    listed.append((root, who, stats, snap, paths))


def read_calls():
    for root, who, stats, _, paths in listed:
        t = time.perf_counter()
        with calls._lock:   # file_calls 的缓存不是线程安全的（同 calls_of）
            for s, (size, _) in stats.items():
                calls.file_calls(paths[s], size)   # 读不了的文件 file_calls 自己当作没有调用，存盘时也不会写它
        n = calls.save_dirty()
        print(f"{root}（{who}）：读调用表 {time.perf_counter() - t:.1f} 秒，存盘 {n} 个文件", flush=True)


def wipe(folder):
    gone = 0
    with os.scandir(folder) as it:
        for e in it:
            if e.name.endswith(".pkl"):
                os.remove(e.path)
                gone += 1
    print(f"  删掉了 {folder} 里的 {gone} 份", flush=True)


read_calls()
if bad := audit():
    print(f"抽查对不上 {len(bad)} 个：存盘的版本没认出该作废的改动，两种存盘整份删掉、全部从头读一遍再存。对不上的（最多列 20 个）：", flush=True)
    for p, why in bad[:20]:
        print(f"  {p}：{why}", flush=True)
    wipe(DATA / "calls_cache")
    wipe(DATA / "spend_cache")
    with calls._lock:
        calls._files.clear()
        calls._dirty.clear()
    restored.clear()
    read_calls()

for root, who, stats, snap, paths in listed:
    big = [paths[s] for s in stats if "/subagents/" not in s
           and (stats[s][0] > args.spend_mb * 1e6 or len(calls.files_of(paths[s], snap)) - 1 > args.spend_subs)]
    for p in big:
        t = time.perf_counter()
        try:
            spendmap.SessionSpend(read_cwd(p)).refresh(p, {})   # 只为把拆好的存下去，调用给空的（存的状态不依赖它）
        except OSError as e:
            print(f"  花费没拆成 {p}：{e}", flush=True)
            skipped.append(f"{p}：{e}")
            continue
        print(f"  拆花费 {p}：{time.perf_counter() - t:.1f} 秒", flush=True)
for th in [x for x in threading.enumerate() if x.name == "spend-save"]:
    th.join()
print(f"预写完了：{len(roots)} 个环境，共 {time.perf_counter() - t0:.0f} 秒", flush=True)
if skipped:
    print(f"有 {len(skipped)} 处没写成，重启后那些照旧冷读：", flush=True)
    for s in skipped:
        print("  " + s, flush=True)
