"""按 token 付费的 API 号（instance.API，比如 DeepSeek）：CLI 的请求经网络闸门转到这家的 Anthropic 兼容接口，
闸门转发时才补上密钥。

为什么不把密钥交给 CLI：多人共用一个运行 Claude 的系统用户时（instance.RUN_AS），会话里的工具读得到 CLI 的环境变量和这个用户能读的文件，
密钥写进别名或配置，任何一个用户让 AI 打印一下就拿走了。所以 CLI 手里只有占位的令牌，ANTHROPIC_BASE_URL 指向只有闸门认得的
HOST（http，不加密，只在本机这一段），闸门认出通行证、核过拦截以后，配了代理就经它建 CONNECT 隧道、没配就直接连到这家的服务器，
校验证书，换掉认证头再发出去。一条连接只转一次请求，回包逐块转发，不缓冲整轮回复。配了代理而代理不通时不改成直连，直接报错。

这家的余额也在这里查（balance），同样走这个号的代理端口，账号面板上显示（usage_probe 存进数据目录的 balance/）。
"""
import asyncio
import contextlib
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HOST = "llm.internal"   # CLI 看到的接口地址里的名字：中性的，独立环境里的 AI 也看得到
BASE_URL = f"http://{HOST}"
PLACEHOLDER = "relay"   # CLI 带的令牌，闸门换成真的密钥
MAX_BODY = 32 * 1000 * 1000
TLS = ssl.create_default_context()
# 各家：服务器、Anthropic 兼容接口的路径前缀、查余额的接口（官方文档 https://api-docs.deepseek.com/zh-cn/）、
# 自动压缩窗口（各家官方接入说明写的数）。
# 智谱（GLM Coding Plan）是订阅、不是余额：没有余额接口，查的是 5 小时和本周额度用了百分之几（quota）。
# 接口是 https://docs.bigmodel.cn/cn/coding-plan/tool/claude 写给 Claude Code 的那个；x-api-key 和 Bearer 两种认证头实测都认
VENDORS = {"deepseek": {"name": "DeepSeek", "host": "api.deepseek.com", "prefix": "/anthropic", "balance": "/user/balance",
                        "compact": "786432"},
           "glm": {"name": "智谱", "host": "open.bigmodel.cn", "prefix": "/api/anthropic", "quota": "/api/monitor/usage/quota/limit",
                   "compact": "1000000"}}
# 中转站（instance.RELAY）的平台名、域名、路径前缀写在号的配置里，不在这张表里；自动压缩窗口都按 1M
RELAY_COMPACT = "1000000"


class RelayError(ValueError):
    pass


def vendor(account: str) -> dict:
    """这个号的平台：{name, host, prefix, compact, 有的还有 balance、quota}。"""
    from instance import API, RELAY
    if account in RELAY:
        return {**RELAY[account], "compact": RELAY_COMPACT}
    return VENDORS[API[account]["vendor"]]


def is_relay(account: str) -> bool:
    """是不是转 Claude 模型的中转站号（instance.RELAY）：网页只映射 model、缓存默认一小时、按号的账号价计费。"""
    from instance import RELAY
    return account in RELAY


def key(account: str) -> str:
    """这个号的密钥（只在查看器自己这边，见 instance.API）。读不到就报错：没有密钥也不能当成能用。"""
    from instance import API
    f = Path(API[account]["key_file"])
    try:
        k = f.read_text().strip()
    except OSError as e:
        raise RuntimeError(f"{account} 的密钥文件 {f} 读不了（{e}），不能启动") from None
    if not re.fullmatch(r"[\x21-\x7e]{8,}", k):
        raise RuntimeError(f"{account} 的密钥文件 {f} 里不是一个密钥，不能启动")
    return k


def launch_env(account: str) -> dict[str, str]:
    """拉起这个号的 CLI 要设的环境（chat.Driver 同时放进进程环境和 --settings 的 env：用户设置里的 env 会压过进程环境，
    共用环境的设置里写着 ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-6，不压回来的话派子代理指定 opus 就会发 Claude 的模型名出去）。
    模型照官方接入说明（https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/claude_code）：主模型和 opus、sonnet 两档
    用默认模型，haiku 一档、子代理、后台小模型用不带 [1m] 的同一个模型；自动压缩窗口按官方写的 786432。
    思考强度不照官方写死成 max，跟网页上选的走（实例默认 high）。"""
    from instance import API, api_models
    main = api_models(account)[0]
    small = main.split("[")[0]
    return {"ANTHROPIC_BASE_URL": BASE_URL, "ANTHROPIC_AUTH_TOKEN": PLACEHOLDER,
            **({"CLAUDE_CODE_PROMPT_CACHE_TTL": API[account].get("cache_ttl", "1h"),
                "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": API[account].get("cache_ttl", "1h")}
               if is_relay(account) else {}),
            "ANTHROPIC_MODEL": main, "ANTHROPIC_DEFAULT_OPUS_MODEL": main, "ANTHROPIC_DEFAULT_SONNET_MODEL": main,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": small, "ANTHROPIC_SMALL_FAST_MODEL": small, "CLAUDE_CODE_SUBAGENT_MODEL": small,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": vendor(account)["compact"],
            # 实测：不关的话 CLI 照样去连 api.anthropic.com（遥测这类），这个号和 Anthropic 没关系，不该有这些连接。
            # WebFetch 的域名检查这个变量管不到，由 chat.Driver 在 --settings 里写 skipWebFetchPreflight 关掉
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


def request_parts(head: bytes):
    """CLI 发给 HOST 的请求头 → (方法, 路径, 带查询串的路径, 请求头, 正文长度)；不认得的写法一律不转。"""
    lines = head.split(b"\r\n")
    first = lines[0].split(b" ")
    if len(first) != 3 or first[2] != b"HTTP/1.1":
        raise RelayError("中转只接受 HTTP/1.1 请求")
    method, target = first[:2]
    url = urlsplit(target.decode("ascii"))
    if url.scheme != "http" or url.hostname != HOST or url.port not in (None, 80) or url.username or url.fragment:
        raise RelayError("中转目标不合法")
    path = url.path or "/"
    if not path.startswith("/v1/"):
        raise RelayError("中转接口路径不支持")
    headers, lower = [], {}
    for line in lines[1:]:
        if not line:
            continue
        k, sep, v = line.partition(b":")
        if not sep or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k):
            raise RelayError("请求头格式错误")
        kl, v = k.lower(), v.strip()
        if kl in lower and kl in (b"content-length", b"transfer-encoding", b"host"):
            raise RelayError("请求存在重复的长度或路由字段")
        headers.append((k, v))
        lower[kl] = v
    if b"transfer-encoding" in lower:
        raise RelayError("中转不接受分块上传的请求，未发送")
    length = lower.get(b"content-length", b"0")
    if not re.fullmatch(rb"[0-9]+", length) or int(length) > MAX_BODY:
        raise RelayError("请求长度无效或超过 32 MB，未发送")
    return method, path, path + ("?" + url.query if url.query else ""), headers, int(length)


def outgoing(v: dict, secret: str, method: bytes, route: str, headers, body: bytes) -> bytes:
    # 认证头换成真的密钥；代理认证不离开本机；逐跳头不透传，长度按实际发出的正文重算
    removed = {b"host", b"connection", b"proxy-connection", b"proxy-authorization", b"keep-alive", b"transfer-encoding",
               b"te", b"trailer", b"upgrade", b"content-length", b"expect", b"authorization", b"x-api-key"}
    for k, val in headers:
        if k.lower() == b"connection":
            removed.update(p.strip().lower() for p in val.split(b","))
    lines = [method + b" " + (v["prefix"] + route).encode("ascii") + b" HTTP/1.1", b"Host: " + v["host"].encode(),
             b"Connection: close", b"Content-Length: " + str(len(body)).encode(), b"x-api-key: " + secret.encode()]
    lines.extend(k + b": " + val for k, val in headers if k.lower() not in removed)
    return b"\r\n".join(lines) + b"\r\n\r\n"


async def connect(host: str, port: int | None, link):
    """配了代理（port）就经它建 CONNECT 隧道，再校验对方的证书，代理不通时不改成直连；没配代理（instance.json 打开了 allow_direct）就直接连这家的服务器。"""
    if port is None:
        if link is not None:
            link.update(stage="dial", t=time.monotonic())
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 443, ssl=TLS, server_hostname=host,
                                                                        ssl_handshake_timeout=10), 20)
        if link is not None:
            link.update(stage="hello", t=time.monotonic())
        return reader, writer
    reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 10)
    try:
        if link is not None:
            link.update(stage="dial", t=time.monotonic())
        writer.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        await writer.drain()
        resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        if resp.split(b" ", 2)[1:2] != [b"200"]:
            first = resp.split(b"\r\n", 1)[0].decode(errors="replace")
            raise RelayError(f"代理 127.0.0.1:{port} 没连通 {host}：{first}")
        if link is not None:
            link.update(stage="hello", t=time.monotonic())
        await writer.start_tls(TLS, server_hostname=host, ssl_handshake_timeout=10)
        return reader, writer
    except BaseException:
        writer.close()
        raise


def model_body(account: str, body: bytes) -> tuple[bytes, str]:
    """平台渠道名只映射 model，不改消息内容，也不退到普通渠道。返回（发出的正文，这个模型实际用哪把钥匙的号）：
    同一站可以有几把令牌（比如同一个中转站里不同的分组），model_keys 把模型指到拿另一把钥匙的号上
    （那个号用 present_as 并进这个号，见 instance），钥匙、渠道映射和缓存价目都按那个号的来，界面上仍是一家。"""
    from instance import API, api_models
    if not is_relay(account):
        return body, account
    config = API[account]
    try:
        data = json.loads(body)
        model = data.get("model")
    except (ValueError, AttributeError):
        raise RelayError("模型请求不是合法的 JSON 对象") from None
    if model not in api_models(account):
        raise RelayError(f"{account} 只接入了 {'、'.join(api_models(account))}，未发送其他模型的请求")
    holder = config.get("model_keys", {}).get(model, account)
    holder_config = API[holder]
    if holder_config.get("cache_ttl") == "5m":
        def hour_cache(value):
            if isinstance(value, dict):
                return (isinstance(value.get("cache_control"), dict) and value["cache_control"].get("ttl") == "1h") or any(hour_cache(v) for v in value.values())
            return isinstance(value, list) and any(hour_cache(v) for v in value)
        if hour_cache(data):
            raise RelayError(f"{account} 尚未配置一小时缓存价格，未发送；请使用五分钟缓存")
    mapped = holder_config.get("model_overrides", {}).get(model)
    if mapped:
        data["model"] = mapped
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), holder
    return body, holder


async def forward(reader, writer, head, driver, proxy_port, link, meter):
    """在闸门认过通行证以后调用（gate._serve）：只给 API 号的会话转，被拦下的不转。"""
    import gate
    import platform_costs
    from instance import API, REMOTE
    upstream = pair = None
    audit = None
    replied = False
    try:
        # 远端统一结算的 GPT 号也走这里：它的请求只转给另一个查看器，不碰本机的翻译层
        remote = REMOTE.get(driver.account) if driver else None
        if not driver or (driver.account not in API and not (remote and remote.get('billing') == 'upstream')):
            await gate._reply(writer, "403 Forbidden", "代理：这个连接不能用这个接口")
            return
        if remote and remote.get('billing') == 'upstream':
            import users
            if users.owner_of(driver.session_id) != remote['user']:
                raise RelayError('这个远端入口未授权给当前用户')
            target = urlsplit(remote['url'])
            v = {'host': target.netloc, 'prefix': ''}
            secret = Path(remote['key_file']).read_text().strip()
            if not secret.startswith('ccv-api-'):
                raise RelayError('远端入口的导出密钥无效')
        else:
            remote = None
            v = vendor(driver.account)
        if link is not None:
            link["host"] = v["host"]
        if meter is not None:
            meter["host"] = v["host"]
        method, path, route, headers, length = request_parts(head)
        if method not in (b"GET", b"POST"):
            raise RelayError("中转只支持 GET 和 POST")
        body = await asyncio.wait_for(reader.readexactly(length), 60) if length else b""
        if remote and path not in ('/v1/messages', '/v1/messages/count_tokens', '/v1/models'):
            raise RelayError('远端入口不支持这个接口')
        holder = driver.account   # 这个模型实际用哪把钥匙的号（同站第二把令牌，model_keys 并号）：没指到别的号就是自己
        if method == b"POST" and path.startswith("/v1/messages") and not remote:
            body, holder = model_body(driver.account, body)
        if not remote:
            secret = key(holder)
        # 对账跟着钥匙走：这个模型的消费日志在持钥匙的号名下（同站第二把令牌并号），本地回包也记在它那边才配得上对
        if method == b'POST' and path == '/v1/messages' and not remote and platform_costs.enabled(holder):
            from platform_costs_wire import ResponseAudit
            audit = ResponseAudit(driver.session_id)
        if driver.gate_why():
            await gate._reply(writer, "403 Forbidden", driver.gate_why())
            return
        if remote:
            response, upstream = await asyncio.wait_for(asyncio.open_connection(target.hostname, target.port), 5)
        else:
            response, upstream = await connect(v["host"], proxy_port, link)
        pair = (writer, upstream)
        gate._conns.setdefault(driver.gate_token, set()).add(pair)
        # 上传正文期间也可能被拦下：连接登记好以后再核一次，之后由 gate.cut 当场掐断两头
        if why := driver.gate_why():
            await gate._reply(writer, "403 Forbidden", why)
            return
        packet = outgoing(v, secret, method, route, headers, body)
        upstream.write(packet)
        upstream.write(body)
        await upstream.drain()
        gate._count(meter, "tx", len(packet) + len(body))
        if link is not None:
            link["tx"] = time.monotonic()

        async def pump():
            nonlocal replied
            while chunk := await asyncio.wait_for(response.read(65536), 900):
                replied = True
                if audit:
                    audit.feed(chunk)
                if link is not None:
                    now = time.monotonic()
                    link.update(rx=now, stage="flow", t=now if link["stage"] != "flow" else link["t"])
                gate._count(meter, "rx", len(chunk))
                writer.write(chunk)
                await writer.drain()

        # CLI 取消请求（断开）时同时收掉上游，不让已经不要的回复在后台接着跑、接着花钱
        transfer = asyncio.create_task(pump())
        gone = asyncio.create_task(reader.read(1))
        try:
            done, _ = await asyncio.wait((transfer, gone), return_when=asyncio.FIRST_COMPLETED)
            if transfer in done:
                await transfer
        finally:
            for t in (transfer, gone):
                t.cancel()
            await asyncio.gather(transfer, gone, return_exceptions=True)
    except RelayError as e:
        if not replied:
            await gate._reply(writer, "400 Bad Request", str(e))
    except RuntimeError as e:   # 密钥读不到
        if not replied:
            await gate._reply(writer, "502 Bad Gateway", str(e))
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as e:
        if not replied:
            await gate._reply(writer, "502 Bad Gateway", f"模型中转失败（{type(e).__name__}: {e}），未改用其他线路")
    finally:
        if pair:
            gate._conns.get(driver.gate_token, set()).discard(pair)
        if upstream:
            upstream.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(upstream.wait_closed(), 2)
        writer.close()
        if audit:
            record = audit.finish()
            if record.get('request_id'):
                try:
                    await asyncio.to_thread(platform_costs.save_local, holder, record)
                except (OSError, ValueError, platform_costs.sqlite3.Error) as exc:
                    print(f'{holder} 平台对账的本地记录未保存（{type(exc).__name__}），不影响原响应', flush=True)
            elif replied:
                print(f'{holder} 回包没有平台请求编号，无法精确对账', flush=True)


def refuse_remote(account: str):
    """远端统一结算的号这边只有导出密钥：拿它去问原平台既查不到，还会把密钥发给原平台，所以一律不查。"""
    from instance import REMOTE
    if REMOTE.get(account, {}).get("billing") == "upstream":
        raise RuntimeError(f"{account} 由远端结算，余额和额度要到那边看，这边不查原平台")


def relay_balance(account: str, v: dict, token_file, port: int) -> dict:
    """new-api 系中转站的账号余额：API 密钥查不了账号信息，要访问令牌，
    放在 ~/.config/cc_viewer/<号>.token（0600，不放配置里，有这个文件才算接了）。金额换算照对账那套 /api/status 的参数核
    （platform_costs.conversion，参数变了就报错不算）。只读，不消耗额度。"""
    try:
        token = token_file.read_text().strip()
    except OSError as e:
        raise RuntimeError(f"读 {v['name']} 的访问令牌失败：{e}") from None
    if not re.fullmatch(r"[\x21-\x7e]{16,}", token):
        raise RuntimeError(f"{v['name']} 的访问令牌文件里不是一个令牌")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": f"http://127.0.0.1:{port}"}))

    def get(path, auth=False):
        headers = {"Accept": "application/json", **({"Authorization": f"Bearer {token}"} if auth else {})}
        req = urllib.request.Request(f"https://{v['host']}{path}", headers=headers)
        with opener.open(req, timeout=20) as r:
            return json.load(r)

    def _data(got):
        if not isinstance(got, dict) or got.get("success") is not True:
            raise RuntimeError(f"{v['name']} 没确认查询成功，请检查访问令牌权限或平台状态")
        return got.get("data")

    try:
        # 两个接口都回 {"success": true, "data": …}，只认 success 的那份（和对账的 _get 同一个规矩）
        status = _data(get("/api/status"))
        # 金额显示参数：有的站按人民币、有的按美元（一元对一美元：界面照数显示，不折算）。
        # 只认核对过的这两套参数，别的报错不算，不拿没核对的汇率硬算
        display, per_unit = status.get("quota_display_type"), status.get("quota_per_unit")
        if display not in ("CNY", "USD") or float(per_unit) != 500000 or float(status.get("usd_exchange_rate")) != 1:
            raise RuntimeError(f"{v['name']} 的金额显示参数变了（{display}、{per_unit}），先不查，要重新核对")
        user = _data(get("/api/user/self", True)) or {}
        yuan = float(user["quota"]) / float(per_unit)
        return {"total": yuan, "granted": 0.0, "topped": yuan, "currency": display, "available": True,
                "used": float(user.get("used_quota") or 0) / float(per_unit)}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{v['name']} 查余额的接口回了 {e.code}：{'访问令牌不对或已失效' if e.code == 401 else e.reason}") from None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"问 {v['name']} 余额失败：{type(e).__name__}: {e}") from None


def balance(account: str) -> dict:
    """问这家平台这个号还有多少钱（DeepSeek：GET /user/balance；new-api 系中转站：访问令牌查 /api/user/self）。
    返回 {total 元, granted 赠金, topped 充值, currency, available}；配了代理就经代理（instance.proxy_port），没配（打开了 allow_direct）就直连。出错抛 RuntimeError，说明里不带密钥。"""
    from instance import proxy_port, RELAY
    refuse_remote(account)
    v = vendor(account)
    if "quota" in v:
        return quota(account)
    if "balance" not in v:
        token_file = Path.home() / ".config/cc_viewer" / f"{account}.token"
        if account in RELAY and token_file.is_file():
            return relay_balance(account, v, token_file, proxy_port(account))
        return {"unavailable": "未接入余额查询"}
    req = urllib.request.Request(f"https://{v['host']}{v['balance']}",
                                 headers={"Authorization": f"Bearer {key(account)}", "Accept": "application/json"})
    port = proxy_port(account)   # ProxyHandler({}) 不读环境变量里的代理，没配（打开了 allow_direct）就是直连
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": f"http://127.0.0.1:{port}"} if port else {}))
    try:
        with opener.open(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{v['name']} 查余额的接口回了 {e.code}：{'密钥不对或已失效' if e.code == 401 else e.reason}") from None
    except (OSError, ValueError) as e:
        raise RuntimeError(f"问 {v['name']} 余额失败：{type(e).__name__}: {e}") from None
    infos = [i for i in data.get("balance_infos") or [] if isinstance(i, dict)]
    # 一个号可能有人民币和美元两份；界面上写人民币，没有人民币才写别的
    info = next((i for i in infos if i.get("currency") == "CNY"), infos[0] if infos else None)
    if not info:
        raise RuntimeError(f"{v['name']} 查余额的接口没给余额（is_available={data.get('is_available')}）")
    try:
        return {"total": float(info["total_balance"]), "granted": float(info.get("granted_balance") or 0),
                "topped": float(info.get("topped_up_balance") or 0), "currency": info.get("currency") or "",
                "available": bool(data.get("is_available"))}
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"{v['name']} 查余额的接口回的格式认不出") from None


def quota(account: str) -> dict:
    """订阅制的 API 号（智谱）：5 小时和本周额度各用了百分之几（接口只给整数百分比）、什么时候重置。
    返回 {five, week 用了百分之几, five_reset, week_reset（unix 秒）, level 套餐档}；和余额存在同一处（usage_probe.save_balance），
    另记一份历史，以后拿读数和调用的 token 数拟合单价。配了代理就经代理，没配（打开了 allow_direct）就直连。"""
    from instance import proxy_port
    refuse_remote(account)
    v = vendor(account)
    req = urllib.request.Request(f"https://{v['host']}{v['quota']}", headers={"Authorization": key(account), "Accept": "application/json"})
    port = proxy_port(account)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": f"http://127.0.0.1:{port}"} if port else {}))
    try:
        with opener.open(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{v['name']} 查额度的接口回了 {e.code}：{'密钥不对或已失效' if e.code == 401 else e.reason}") from None
    except (OSError, ValueError) as e:
        raise RuntimeError(f"问 {v['name']} 额度失败：{type(e).__name__}: {e}") from None
    out = {"level": (data.get("data") or {}).get("level")}
    # TOKENS_LIMIT 有两条：unit 3、number 5 是 5 小时，unit 6、number 1 是本周（实测）；TIME_LIMIT 是每月的 MCP 次数，不要
    for w in (data.get("data") or {}).get("limits") or []:
        if not isinstance(w, dict) or w.get("type") != "TOKENS_LIMIT":
            continue
        k = "five" if (w.get("unit"), w.get("number")) == (3, 5) else "week" if (w.get("unit"), w.get("number")) == (6, 1) else None
        if k:
            out[k] = w.get("percentage")
            out[k + "_reset"] = (w.get("nextResetTime") or 0) / 1000 or None
    if "five" not in out or "week" not in out:
        raise RuntimeError(f"{v['name']} 查额度的接口回的格式认不出（没有 5 小时或本周那一项）")
    return out
