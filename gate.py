"""网络闸门（预测拦截拦下以后，一次请求都不许再发出去，别人的额度一点都不能用到，这是安全问题）。

钩子只能管到 CLI 肯问的那几个时刻，CLI 自己发起的请求（后台任务完成后开的新一轮、子代理的第一次请求等）钩子不一定管得到，
所以在网络上再加一道：查看器拉起的每个 CLI，代理都设成这里（启动脚本在 .bashrc 之后再换成闸门，见 claude_via_bashrc.sh），
闸门再转出去：配了上游代理（instance.proxy_port）就原样转给它，没配就由闸门直接连目标网站。
配了代理而代理不通时连不上，不会悄悄改成直连。
代理地址里带着这个 CLI 的通行证（http://通行证:x@127.0.0.1:端口），CLI 连代理时放在 Proxy-Authorization 里，闸门据此认出是哪个驱动。
去 anthropic.com 的连接：没带通行证的先回 407 要它带上；带了但认不出来（不知道是谁、没法拦）的一律不放；
这个驱动已经被拦下的也不放，而且拦下的那一刻把它正连着 anthropic.com 的连接全掐断——请求走的是长连接，不掐断的话，
下一次请求照样从老连接发出去。别的网站（工具里的 pip、git 之类）照常转发，不认通行证。
每个实例一个固定端口（实例端口 + 1000）：CLI 起的后台命令可能活得比 CLI 久、带着这个代理地址，实例重启后马上又是同一个端口。

GPT 号（见 instance.py）的 CLI 不去 anthropic.com，去的是本机的翻译层（别名里写 http://127.0.0.1:翻译层端口），
由它转到 ChatGPT。127.0.0.1 在 no_proxy 里，CLI 会直连，所以启动脚本（claude_via_bashrc.sh）把地址换成只有闸门认得的名字
BRIDGE_HOST，请求就经闸门走：和 anthropic.com 一样要认通行证、拦下时一样掐断，闸门再直接连到本机的翻译层（不转给代理）。
翻译层的端口直接经闸门去的（127.0.0.1:端口）也一样要通行证（LOCAL_GUARDED）。
去本机的普通请求，请求行照旧是完整网址：翻译层认这种写法；别的本机服务不一定认（uvicorn 就不认），所以不让 CLI 别的本机流量经闸门。

上游按号取（instance.proxy_port，没配代理是 None、直连）：按通行证认出这个 CLI 是哪个号再决定怎么转。
所以去任何外网的连接都要认通行证（原来只有 anthropic.com 要）：没带的先回 407，认不出的不放行。
通行证到上游的对应在 CLI 收掉以后也留着（只在内存里）：它留下的后台命令去别的网站照旧能走；去 anthropic.com 照旧不放。

每条出网连接走到哪一步（网页上的状态要能直接看出链路卡在哪一段）：闸门是唯一看得见 CLI 到代理、
代理到目标网站这几段的地方，所以每条连接记一份进度，挂在发起它的驱动上（driver.links），驱动据此把「等上游」细分开：
proxy 正在连本机代理端口 → dial 代理正在经节点连目标网站（等代理回 CONNECT）→ hello 隧道通了、等对方第一批数据（TLS 握手）
→ flow 有数据回来了；同时记着最后一次收到、发出数据的时刻，复用的老连接一直没回包也看得出来。
GPT 号的翻译层去 chatgpt.com 也可以经这里：把它的代理设成带固定口令 bridge-<号> 的闸门地址，
照这个号的上游转出去，进度记在这个号名下（bridge_links），这个号的会话等 ChatGPT 时就看这一份。翻译层同时服务这个号的几个会话，
所以只分得到号、分不到会话。

API 号（比如 DeepSeek，见 instance.API）的 CLI 去的是只有闸门认得的 api_relay.HOST：和 anthropic.com 一样认通行证、拦下时掐断，
闸门补上密钥，经这个号的代理端口转到那家的服务器（api_relay.forward）。

数字节（失败重试可能在一晚上用掉好几 GB 流量，事后要查得出每字节是谁传的）：带通行证的连接都另记一份字节数
（和上面的进度分开，不影响转圈那行），连接结束时记一行进数据目录的 traffic.jsonl：哪个号、哪个会话（翻译层的连接只有号）、去哪、
是不是本机（GPT 号的 CLI 到翻译层这一段是本机，翻译层去 chatgpt.com 那一段才出网）、上行、下行。
同时把去模型服务器的上行量累计到驱动上（driver.api_up，驱动据此判断重试是不是在空转），并按「一段连续发出的数据」量出最大的一次请求有多大
（driver.note_req：一条连接上发一阵、收一阵，发的那一阵就是一次请求），网页上据此提醒请求快到上游的单条上限了。"""
import asyncio
import base64
import contextlib
import time
from urllib.parse import urlsplit

import account_identity
import api_relay

GUARDED = "anthropic.com"   # 这个域名和它的子域名要认通行证
# GPT 号的翻译层在闸门这里的名字，要认通行证，连到本机。独立环境里的 AI 看得到接口地址，所以给它中性的名字
# （isolation.launch_env）；共用环境照旧用原来的名字（claude_via_bashrc.sh），两个都认
BRIDGE_HOST = "api.internal"
BRIDGE_HOSTS = (BRIDGE_HOST, "gpt-bridge.cc-viewer")
LOCAL = ("127.0.0.1", "localhost", "::1", *BRIDGE_HOSTS)
LOCAL_GUARDED: set[int] = set()   # 本机这些端口也要认通行证（GPT 号的翻译层，start 时按 instance.py 填）

port: int | None = None             # 闸门在听的端口；None 是没起来（这时查看器不许拉起 CLI，见 chat.check_proxy）
_drivers: dict = {}                 # 通行证 → 驱动：要有 gate_why()（被拦下时返回说明，否则空字符串）和 account
_routes: dict[str, int] = {}        # 通行证 → 这个号的代理端口；CLI 收掉以后也不删
_conns: dict[str, set] = {}         # 通行证 → 正连着 anthropic.com 的连接（客户端和上游两头的 writer）
_bridges: dict[str, str] = {}       # 翻译层用的固定口令 → 哪个 GPT 号
_bridge_links: dict[str, dict] = {}  # GPT 号 → 它的翻译层正开着的出网连接的进度（同 driver.links）
_server: asyncio.Server | None = None
trace = None   # 实验脚本用：trace(结果, 主机, 通行证)，结果是 seen（收到）/ ask（要通行证）/ refuse（不放行）
TRAFFIC = None   # 每条连接的字节数记在哪（数据目录的 traffic.jsonl），start 时填；None 时不记（实验脚本直接用闸门时）


def _count(meter: dict | None, mark: str, n: int):
    """一条连接又过了 n 字节（见开头「数字节」）。mark 是 tx（发出去的）或 rx（收回来的）。"""
    if meter is None or not n:
        return
    if mark == "tx":
        if meter["dir"] != "tx":   # 收过东西以后又开始发：是新的一次请求
            meter["burst"] = 0
        meter["burst"] += n
        meter["up"] += n
        if meter["api"] and (d := meter["drv"]):
            d.api_up += n
            d.note_req(meter["burst"])
    else:
        meter["down"] += n
    meter["dir"] = mark


def _log_traffic(meter: dict):
    """连接结束：记一行。闸门跑在事件循环里，这一行交给写盘线程（diskq）追加，不在这里等磁盘（卡顿记录里这一处最多）；
    写不进去由写盘线程在服务输出里说、过一会儿再试，不会悄悄少记（出错不影响转发）。"""
    if TRAFFIC is None or not (meter["up"] or meter["down"]):
        return
    import json
    import diskq
    rec = {"t": round(time.time(), 1), "dur": round(time.time() - meter["t0"], 1), "account": meter["acct"],
           "session": meter["sid"], "host": meter["host"], "local": meter["local"], "api": meter["api"],
           "up": meter["up"], "down": meter["down"]}
    diskq.append(TRAFFIC, json.dumps(rec, ensure_ascii=False) + "\n")


def url(token: str) -> str:
    return f"http://{token}:x@127.0.0.1:{port}"


def register(token: str, driver):
    from instance import proxy_port
    _drivers[token] = driver
    _routes[token] = proxy_port(driver.account)


def forget(token: str):
    _drivers.pop(token, None)
    cut(token)


def cut(token: str):
    """掐断这个通行证正连着 anthropic.com 的所有连接（要在事件循环里调）。"""
    for pair in _conns.pop(token, set()):
        for w in pair:
            with contextlib.suppress(Exception):
                w.close()


def bridge_token(account: str) -> str:
    return f"bridge-{account}"


def bridge_links(account: str) -> list[dict]:
    """这个 GPT 号的翻译层正开着的出网连接的进度。"""
    return list(_bridge_links.get(account, {}).values())


def _guarded(host: str, port: int | None = None) -> bool:
    host = (host or "").lower().rstrip(".")
    return (host == GUARDED or host.endswith("." + GUARDED) or host in BRIDGE_HOSTS
            or host in (account_identity.HOST, api_relay.HOST) or (host in LOCAL and port in LOCAL_GUARDED))


def _token(value: bytes) -> str | None:
    """Proxy-Authorization: Basic base64(通行证:x) → 通行证。"""
    kind, _, b64 = value.strip().partition(b" ")
    if kind.lower() != b"basic":
        return None
    with contextlib.suppress(ValueError):
        return base64.b64decode(b64).decode().split(":", 1)[0]
    return None


async def _reply(writer: asyncio.StreamWriter, status: str, text: str, extra: str = ""):
    body = text.encode()
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
                 f"{extra}Connection: close\r\n\r\n".encode() + body)
    with contextlib.suppress(Exception):
        await writer.drain()
    writer.close()


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter, link: dict | None = None, mark: str = "",
                meter: dict | None = None):
    """原样转发。link 是这条连接的进度，mark 是 rx（上游回来的）或 tx（发出去的）：每过一批数据记下时刻，
    上游第一次回数据时进度改成 flow。meter 是这条连接的字节数（见开头「数字节」）。"""
    with contextlib.suppress(Exception):
        while data := await r.read(65536):
            if link is not None:
                link[mark] = time.monotonic()
                if mark == "rx" and link["stage"] == "hello":
                    link["stage"], link["t"] = "flow", link[mark]
            _count(meter, mark, len(data))
            w.write(data)
            await w.drain()
    with contextlib.suppress(Exception):
        w.close()


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    pair, token, links, key, meter = None, None, None, id(writer), None
    try:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        lines = head.split(b"\r\n")
        method, target = (lines[0].split(b" ") + [b"", b""])[:2]
        target = target.decode(errors="replace")
        if method == b"CONNECT":
            host, _, p = target.rpartition(":")
            host, port = host.strip("[]"), int(p) if p.isdigit() else 443
        else:
            u = urlsplit(target)
            host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
        guarded = _guarded(host, port)
        kept, auth = [], None
        for line in lines:   # Proxy-Authorization 是给闸门的，不往上游传
            if line[:20].lower() == b"proxy-authorization:":
                auth = line[20:]
            else:
                kept.append(line)
        local = host.lower() in LOCAL
        if trace:
            trace("ask" if (guarded or not local) and auth is None else "seen", host, _token(auth) if auth else None)
        if (guarded or not local) and auth is None:   # 有的客户端要先被要一次才带上
            # 这几句会话里的 AI 都可能看到（CLI 把代理的回答当报错），说成普通的代理，不提查看器
            await _reply(writer, "407 Proxy Authentication Required", "代理要求认证",
                         'Proxy-Authenticate: Basic realm="proxy"\r\n')
            return
        token = _token(auth) if auth else None
        up = _routes.get(token) if token else None
        if not local and token not in _routes:
            if trace:
                trace("refuse", host, token)
            # 认不出是哪个号，就不知道该怎么转（上游按号取，见 instance.proxy_port）
            await _reply(writer, "403 Forbidden", "代理：认不出这个连接的口令，不放行")
            return
        if guarded:
            d = _drivers.get(token)
            # 认不出是哪个会话就没法拦，所以不放
            why = d.gate_why() if d else "代理：这个口令已经失效，不放行"
            if why:
                if trace:
                    trace("refuse", host, token)
                await _reply(writer, "403 Forbidden", why)   # CLI 会当成请求出错一直重试，由驱动看到重试就打断（chat.Driver._handle）
                return
        # 出网的连接记进度：会话的 CLI 挂在它的驱动上，翻译层挂在它的号上（见开头的说明）
        link = None
        if not local:
            d, acct = _drivers.get(token), _bridges.get(token)
            links = d.links if d else _bridge_links.setdefault(acct, {}) if acct else None
            if links is not None:
                now = time.monotonic()
                # api：是不是这个会话和模型服务器之间的那条（会话里工具去别的网站的连接不算）
                link = links[key] = {"host": host, "port": port, "up": up, "api": guarded or acct is not None,
                                     "stage": "proxy", "t0": now, "t": now, "rx": 0.0, "tx": 0.0}
        # 字节数：认得出是谁的连接都记，包括 GPT 号的 CLI 到本机翻译层那一段（见开头「数字节」）
        drv, acct = (_drivers.get(token), _bridges.get(token)) if token else (None, None)
        if drv or acct:
            meter = {"t0": time.time(), "acct": drv.account if drv else acct, "sid": drv.session_id if drv else None,
                     "host": host, "local": local, "api": guarded or acct is not None, "drv": drv,
                     "up": 0, "down": 0, "burst": 0, "dir": ""}
        if host.lower() == account_identity.HOST:
            if link is not None:
                link['host'] = account_identity.UPSTREAM
            if meter is not None:
                meter['host'] = account_identity.UPSTREAM
            await account_identity.forward(reader, writer, head, drv, up, link, meter)
            return
        if host.lower() == api_relay.HOST:   # API 号（DeepSeek）：闸门补上密钥、转到那家的服务器（api_relay.py）
            await api_relay.forward(reader, writer, head, drv, up, link, meter)
            return
        try:
            # 去本机的、没配上游代理的（up 是 None）由闸门直接连过去；配了代理的交给代理
            ur, uw = await asyncio.wait_for(asyncio.open_connection(
                *(("127.0.0.1" if host.lower() in BRIDGE_HOSTS else host, port) if local or up is None else ("127.0.0.1", up))), 10)
        except (OSError, asyncio.TimeoutError) as e:
            # 配了代理而代理不通时不改成直连
            why = (f"代理连不上 {host}:{port}（{e or '10 秒没连上'}）" if local or up is None
                   else f"代理连不上上游 127.0.0.1:{up}（{e or '10 秒没连上'}）")
            if not local and (d := _drivers.get(token)):
                d.gate_err = ({"kind": "proxy_down", "port": up, "why": why, "t": time.monotonic()} if up is not None
                              else {"kind": "dial", "port": None, "host": host, "why": why, "t": time.monotonic()})
            await _reply(writer, "502 Bad Gateway", why)
            return
        if (local or up is None) and method == b"CONNECT":   # 直接连过去的隧道：闸门自己回「连上了」，请求头不往下传
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:   # 去本机的普通请求，请求行照旧写完整网址（翻译层认这种写法），去别处的原样交给代理
            uw.write(b"\r\n".join(kept))
            _count(meter, "tx", len(head))   # 请求头也是这次请求发出去的
        if link is not None:
            via = up is not None   # 经上游代理的隧道：要等代理连通目标网站；直连的上面已经连上了
            link["stage"], link["t"] = ("dial" if method == b"CONNECT" and via else "hello"), time.monotonic()
            if method == b"CONNECT" and via:
                # 代理要先经节点连上目标网站才回「连通了」：把它的回话读出来再转给 CLI，就知道卡没卡在节点这一段
                try:
                    resp = await ur.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError) as e:
                    why = f"代理 127.0.0.1:{up} 没连通 {host}（{type(e).__name__}），多半是节点连不上目标网站"
                    if d := _drivers.get(token):
                        d.gate_err = {"kind": "dial", "port": up, "host": host, "why": why, "t": time.monotonic()}
                    await _reply(writer, "502 Bad Gateway", why)
                    return
                writer.write(resp)
                code = resp.split(b" ", 2)[1:2]
                if code != [b"200"]:
                    first = resp.split(b"\r\n", 1)[0].decode(errors="replace")
                    if d := _drivers.get(token):
                        d.gate_err = {"kind": "dial", "port": up, "host": host, "t": time.monotonic(),
                                      "why": f"代理 127.0.0.1:{up} 连接 {host} 时返回 {first}"}
                    return
                link["stage"], link["t"] = "hello", time.monotonic()
        if guarded:
            pair = (writer, uw)
            _conns.setdefault(token, set()).add(pair)
        await asyncio.gather(_pipe(reader, uw, link, "tx", meter), _pipe(ur, writer, link, "rx", meter))
    except Exception as e:   # 一条连接出错只关这一条，不能让闸门停掉
        print(f"网络闸门处理一条连接出错：{type(e).__name__}: {e}", flush=True)
    finally:
        if pair is not None:
            _conns.get(token, set()).discard(pair)
        if links is not None:
            links.pop(key, None)
        if meter is not None:
            _log_traffic(meter)
        with contextlib.suppress(Exception):
            writer.close()


async def start(listen_port: int):
    """在当前事件循环里起闸门；端口占着就报错往外抛（查看器照旧能看，但不许拉起 CLI）。"""
    global port, _server, TRAFFIC
    from instance import BANNED, DATA, GPT, proxy_port
    TRAFFIC = DATA / "traffic.jsonl"
    LOCAL_GUARDED.update(g["bridge_port"] for g in GPT.values())   # 一个 GPT 号一份翻译层
    for acct in GPT:   # 翻译层去 ChatGPT 用的固定口令：按这个号的端口走（见开头的说明）
        if acct not in BANNED:
            _bridges[bridge_token(acct)] = acct
            _routes[bridge_token(acct)] = proxy_port(acct)
    _server = await asyncio.start_server(_serve, "127.0.0.1", listen_port)
    port = listen_port


def stop():
    global port
    port = None
    if _server:
        _server.close()
