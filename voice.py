"""讯飞语音输入：每个网页账号自己配讯飞开放平台的服务和密钥，配过的人输入框发送按钮左边多一个语音按钮。

浏览器直接连讯飞的 WebSocket 送音频、收文字，这里只做两件事：存各人的配置，按配置签好连接地址交给本人的浏览器。
APISecret 只在服务器上用来签名，任何接口都不往外给；APIKey 在签好的地址里本来就带着（讯飞的鉴权就是这样），界面上只显示末四位。
协议照一套已在用的讯飞语音输入脚本：1、2 是听写 v2（iat-api.xfyun.cn/v2/iat），
5 是中英识别大模型 v1（iat.xf-yun.com/v1）。实时转写（rtasr，那边的 3、4）鉴权和帧格式另是一套，这里没做。

存在数据目录的 voice_keys.json（0600）：用户名 → {service, appid, apiKey, apiSecret, ts}。
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
from email.utils import formatdate
from urllib.parse import urlencode

from fastapi import HTTPException

from instance import DATA, MULTI

VOICE_FILE = DATA / "voice_keys.json"

# 服务编号沿用那套脚本的 config.json 里的 service，抄过来的配置不用改号。
# proto 决定帧格式（前端 js/voice.js 照它拼），resId 是中文识别大模型要带的资源号（同一份脚本里的）
SERVICES = {
    1: {"name": "语音听写（基础版）", "proto": "v2", "host": "iat-api.xfyun.cn", "path": "/v2/iat", "resId": None},
    2: {"name": "中文识别大模型", "proto": "v2", "host": "iat-api.xfyun.cn", "path": "/v2/iat",
        "resId": "ZDVjMTZiYWUyMTg1NzkxOTQ5OWJtYw=="},
    5: {"name": "中英识别大模型", "proto": "v1", "host": "iat.xf-yun.com", "path": "/v1", "resId": None},
}

_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(VOICE_FILE.read_text())
    except FileNotFoundError:
        return {}


def _save(data: dict):
    """整份写回，只给查看器自己读写（0600）：里面是各人的密钥"""
    tmp = VOICE_FILE.with_name(f".{VOICE_FILE.name}.{os.getpid()}.tmp")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(VOICE_FILE)
    except OSError as e:
        raise HTTPException(500, f"无法写入 {VOICE_FILE.name}：{e}")


keys: dict[str, dict] = _load()


def info(who) -> dict | None:
    """给前端的：没配是 {}，配了是服务、APPID 和 APIKey 末四位；游客和单用户实例是 None（设置里不出这一项）"""
    if not (MULTI and who.name):
        return None
    rec = keys.get(who.name)
    if not rec:
        return {}
    svc = SERVICES.get(rec["service"])
    return {"service": rec["service"], "serviceName": svc["name"] if svc else f"服务 {rec['service']}",
            "appid": rec["appid"], "keyTail": rec["apiKey"][-4:]}


def services() -> list[dict]:
    return [{"id": k, "name": v["name"]} for k, v in SERVICES.items()]


def set_keys(who, service: int, appid: str, api_key: str, api_secret: str) -> dict:
    """保存自己的配置。APIKey、APISecret 留空是沿用已存的（改服务、改 APPID 不用重填密钥）"""
    if not (MULTI and who.name):
        raise HTTPException(400, "请登录后配置语音输入")
    if service not in SERVICES:
        raise HTTPException(400, f"不支持的识别服务：{service}")
    appid, api_key, api_secret = appid.strip(), api_key.strip(), api_secret.strip()
    with _lock:
        old = keys.get(who.name) or {}
        api_key = api_key or old.get("apiKey", "")
        api_secret = api_secret or old.get("apiSecret", "")
        if not (appid and api_key and api_secret):
            raise HTTPException(400, "APPID、APIKey、APISecret 都要填")
        keys[who.name] = {"service": service, "appid": appid, "apiKey": api_key, "apiSecret": api_secret,
                          "ts": int(time.time())}
        _save(keys)
    return info(who)


def clear(who) -> dict:
    if not (MULTI and who.name):
        raise HTTPException(400, "请登录后配置语音输入")
    with _lock:
        if keys.pop(who.name, None) is not None:
            _save(keys)
    return info(who)


def sign(who) -> dict:
    """签一个连讯飞的地址给本人的浏览器（讯飞只认签名时刻前后 5 分钟内的，每次开口都现签一个）。
    签名原文是 host、date、请求行三行，用 APISecret 做 HMAC-SHA256，和笔记本上那份 create_url 一样"""
    rec = keys.get(who.name) if MULTI and who.name else None
    if not rec:
        raise HTTPException(400, "还没有配置语音输入（设置 → 个人设置 → 语音输入）")
    svc = SERVICES.get(rec["service"])
    if not svc:
        raise HTTPException(400, f"不支持的识别服务：{rec['service']}，请在设置里重新选择")
    date = formatdate(usegmt=True)
    origin = f"host: {svc['host']}\ndate: {date}\nGET {svc['path']} HTTP/1.1"
    signature = base64.b64encode(hmac.new(rec["apiSecret"].encode(), origin.encode(), hashlib.sha256).digest()).decode()
    auth = (f'api_key="{rec["apiKey"]}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"')
    query = urlencode({"authorization": base64.b64encode(auth.encode()).decode(), "date": date, "host": svc["host"]})
    return {"url": f"wss://{svc['host']}{svc['path']}?{query}", "appid": rec["appid"], "proto": svc["proto"],
            "resId": svc["resId"]}
