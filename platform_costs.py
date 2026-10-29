"""平台消费日志、实际扣费与预估；不改原始会话，费用查询到后替换预估。"""
import argparse
import asyncio
import contextlib
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import threading
import time

import instance

INTERVAL = 60
MAX_RESPONSE = 8 * 1024 * 1024
_locks = {}


class CostError(ValueError):
    pass


# 能对账的是中转站号（instance.RELAY）里配置写了 cost_audit: true 的：平台须是 new-api 系，令牌日志 /api/log/token、
# 金额换算 /api/status 同一套写法（实测过两家）。只连号的配置里写的那个主站，不跟别的域名；
# 不是 new-api 的站开了也只会同步失败（格式认不出就报错、不入账），不会算错钱


def enabled(account):
    return account in instance.RELAY and instance.API[account].get('cost_audit') is True


def accounts():
    return [a for a in instance.API if enabled(a)]


def check_account(account):
    if not enabled(account):
        raise CostError('这个账号没有开启平台账单查询')


def _get(account, path, authenticated=False):
    """固定平台、固定接口、固定代理，不跟重定向，不把密钥放 URL。"""
    import api_relay
    if not enabled(account) or path not in ('/api/status', '/api/log/token'):
        raise CostError('平台账单接口未登记')
    host = instance.RELAY[account]['host']
    port = instance.proxy_port(account)
    if port:   # 配了代理就经它建隧道，没配就直连
        conn = http.client.HTTPSConnection('127.0.0.1', port, timeout=5, context=ssl.create_default_context())
        conn.set_tunnel(host, 443)
    else:
        conn = http.client.HTTPSConnection(host, 443, timeout=5, context=ssl.create_default_context())
    headers = {'Accept': 'application/json', 'Connection': 'close'}
    if authenticated:
        headers['Authorization'] = 'Bearer ' + api_relay.key(account)
    try:
        conn.request('GET', path, headers=headers)
        res = conn.getresponse()
        if res.status != 200:
            raise CostError(f'平台账单查询返回 HTTP {res.status}，未切换线路或登录方式')
        raw = res.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise CostError('平台日志响应超过 8 MB，未保存不完整结果')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('success') is not True:
            raise CostError('平台未确认日志查询成功；请检查令牌权限或平台状态')
        return data.get('data')
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if isinstance(exc, CostError):
            raise
        # 异常原文可能引用认证头或服务端回显，不能进入网页或日志。
        raise CostError(f'平台账单查询失败（{type(exc).__name__}），旧记录已保留') from None
    finally:
        conn.close()


def _decimal(value):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise CostError('平台返回了无法识别的金额') from None
    if not n.is_finite() or n < 0:
        raise CostError('平台返回了无效金额')
    return n


def conversion(status):
    """换算已用几笔真实账单逐笔核实；配置变了就停，不用新汇率重算历史。"""
    if not isinstance(status, dict) or status.get('quota_display_type') != 'CNY':
        raise CostError('平台金额显示单位已变，停止同步，避免误算人民币')
    if _decimal(status.get('quota_per_unit')) != 500000 or _decimal(status.get('usd_exchange_rate')) != 1:
        raise CostError('平台人民币换算规则已变，需要重新核对')
    return Decimal(500000)


def count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CostError('平台返回了无效的用量或时间')
    return value


def normalize(row, unit):
    if not isinstance(row, dict):
        raise CostError('平台日志格式不是对象')
    try:
        other = row.get('other') or '{}'
        other = json.loads(other) if isinstance(other, str) else other
        if not isinstance(other, dict):
            raise ValueError()
        kind = count(row['type'])
        # 一小时写入：有的平台直接给 cache_creation_tokens_1h；有的只给总数和 cache_creation_tokens_5m，差额就是一小时的
        total = count(other.get('cache_creation_tokens', 0))
        if 'cache_creation_tokens_1h' in other:
            hour = count(other['cache_creation_tokens_1h'])
        elif 'cache_creation_tokens_5m' in other:
            hour = total - count(other['cache_creation_tokens_5m'])
            if hour < 0:
                raise CostError('平台五分钟写入大于总写入，未保存矛盾数据')
        else:
            hour = 0
        stream = other.get('stream_status') or {}
        # 错误记录不冒充成功调用；保留种类和金额，但不保存可能含提示词的 content。
        record = {'request_id': str(row.get('request_id') or ''),
                  'upstream_request_id': str(row.get('upstream_request_id') or ''),
                  'created_at': count(row['created_at']), 'type': kind,
                  'model': str(row.get('model_name') or ''),
                  'actual_model': str(other.get('upstream_model_name') or row.get('model_name') or ''),
                  'group': str(row.get('group') or ''),
                  'actual_yuan': str(_decimal(row['quota']) / unit),
                  'use': {'in': count(row['prompt_tokens']), 'out': count(row['completion_tokens']),
                          'cw': total,
                          'cr': count(other.get('cache_tokens', 0)),
                          'cw1h': hour},
                  'billing_mode': str(other.get('billing_mode') or ''),
                  'billing_unit': str(other.get('billing_unit') or ''),
                  'usage_semantic': str(other.get('usage_semantic') or ''),
                  'tier': str(other.get('matched_tier') or ''),
                  # 有的平台流状态没有 response_status，只有 status: ok 和 end_reason
                  'complete': (stream.get('response_status') == 'completed' if 'response_status' in stream
                               else stream.get('status') == 'ok' and stream.get('end_reason') in ('eof', 'stop'))}
        if record['use']['cw1h'] > record['use']['cw']:
            raise CostError('平台一小时写入大于总写入，未保存矛盾数据')
        record['use']['cw5m'] = record['use']['cw'] - record['use']['cw1h']
        if 'cache_creation_tokens_5m' in other and count(other['cache_creation_tokens_5m']) != record['use']['cw5m']:
            raise CostError('平台两种缓存写入之和与总数不一致')
        if len(record['request_id']) > 256 or len(record['upstream_request_id']) > 256:
            raise CostError('平台请求编号过长')
        # 令牌接口可能把 id 重新编号，不能拿列表序号作持久主键。
        identity = (str(kind) + ':' + record['request_id']) if record['request_id'] else json.dumps(record, sort_keys=True)
        record['key'] = hashlib.sha256(identity.encode()).hexdigest()
        return record
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, CostError):
            raise
        raise CostError('平台消费日志字段不完整或格式变化，未更新缓存') from None


@contextlib.contextmanager
def database():
    folder = instance.DATA / 'platform_costs'
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / 'audit.sqlite3'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=3)
    try:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS logs (
          account TEXT, key TEXT, ts INTEGER, request_id TEXT, record TEXT,
          PRIMARY KEY (account, key));
        CREATE INDEX IF NOT EXISTS log_time ON logs(account, ts);
        CREATE TABLE IF NOT EXISTS local (
          account TEXT, request_id TEXT, record TEXT, PRIMARY KEY(account, request_id));
        CREATE TABLE IF NOT EXISTS state (account TEXT PRIMARY KEY, record TEXT);
        ''')
        yield db
    finally:
        db.close()


def _state(db, account):
    found = db.execute('SELECT record FROM state WHERE account=?', (account,)).fetchone()
    return json.loads(found[0]) if found else {}


def sync(account):
    check_account(account)
    lock = _locks.setdefault(account, threading.Lock())
    if not lock.acquire(blocking=False):
        raise CostError('这个账号的平台账单正在同步，请稍后查看')
    try:
        with database() as db:
            old = _state(db, account)
        # 手动刷新与后台共享节流，平台故障时也不反复打接口。
        if time.time() - old.get('attempt_at', 0) < 15:
            return old
        try:
            unit = conversion(_get(account, '/api/status'))
            data = _get(account, '/api/log/token', True)
            if not isinstance(data, list):
                raise CostError('平台令牌日志接口不再返回列表，未更新缓存')
            records = [normalize(row, unit) for row in data]
            keys = [r['key'] for r in records]
            if len(keys) != len(set(keys)):
                raise CostError('平台本次返回重复请求编号，无法安全去重，未更新缓存')
            now = time.time()
            with database() as db, db:
                known = {r[0] for r in db.execute('SELECT key FROM logs WHERE account=?', (account,))}
                new_keys = set(keys) - known
                # 接口只提供最近记录，停机期间可能发生缺口，不能宣称拉全了历史。
                gap = bool(known and keys and not known.intersection(keys))
                for r in records:
                    db.execute('INSERT OR REPLACE INTO logs VALUES (?,?,?,?,?)',
                               (account, r['key'], r['created_at'], r['request_id'], json.dumps(r, ensure_ascii=False)))
                state = {**old, 'attempt_at': now, 'success_at': now, 'error': '', 'received': len(records),
                         'new': len(new_keys), 'possible_gap': old.get('possible_gap', False) or gap,
                         'coverage': '仅覆盖平台接口返回的最近记录及此前已保存记录，不保证全历史；金额以本次读取时的平台日志为准'}
                db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (account, json.dumps(state, ensure_ascii=False)))
            refresh_prices()
            return state
        except (CostError, OSError, sqlite3.Error) as exc:
            state = {**old, 'attempt_at': time.time(), 'error': str(exc) if isinstance(exc, CostError) else f'同步保存失败（{type(exc).__name__}）'}
            with database() as db, db:
                db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (account, json.dumps(state, ensure_ascii=False)))
            raise CostError(state['error']) from None
    finally:
        lock.release()


def estimate(account, model, use):
    """人民币对账，不使用账本的折扣；未知一小时单价不当作零。"""
    row = instance.API[account].get('rates', {}).get(model)
    if not row or use is None:
        return {'estimated_yuan': None, 'known_yuan': None, 'price_note': '没有本地模型单价或完整用量'}
    try:
        hour, total = count(use.get('cw1h', 0)), count(use.get('cw', 0))
        if hour > total:
            raise CostError('用量中的一小时写入大于总写入')
        known = (Decimal(count(use['in'])) * _decimal(row['in']) + Decimal(count(use['out'])) * _decimal(row['out'])
                 + Decimal(count(use.get('cr', 0))) * _decimal(row['cr']) + Decimal(total - hour) * _decimal(row['cw'])) / 1000000
        if hour and 'cw1h' not in row:
            return {'estimated_yuan': None, 'known_yuan': str(known), 'unpriced_1h_tokens': hour,
                    'price_note': '缺少一小时缓存写入单价，不能计算完整预期费用'}
        expected = known + Decimal(hour) * _decimal(row.get('cw1h', 0)) / 1000000
        return {'estimated_yuan': str(expected), 'known_yuan': str(expected), 'price_note': ''}
    except (KeyError, CostError):
        return {'estimated_yuan': None, 'known_yuan': None, 'price_note': '本地单价或用量不完整'}


def _view(account, record, local):
    out = dict(record)
    # 只有消费日志、Anthropic 用量语义才按所配置的 token 单价作比较。
    if record['type'] != 2 or record['usage_semantic'] != 'anthropic':
        return {**out, 'match': '非支持的消费记录', 'estimated_yuan': None, 'delta_yuan': None, 'local': local}
    use = local.get('use') if local and local.get('complete') else None
    out['match'] = '请求编号精确匹配' if use is not None else '本地回包不完整' if local else '没有本地请求记录'
    out['local'] = local
    out['use_differences'] = ({k: {'local': use.get(k, 0), 'platform': record['use'].get(k, 0)}
                               for k in ('in', 'out', 'cw', 'cr', 'cw1h') if use.get(k, 0) != record['use'].get(k, 0)} if use is not None else None)
    out.update(estimate(account, record['actual_model'], use if use is not None else record['use']))
    out['estimate_source'] = '本地回包' if use is not None else '平台用量（尚未与本地对应）'
    actual = Decimal(record['actual_yuan'])
    out['delta_yuan'] = str(actual - Decimal(out['estimated_yuan'])) if out['estimated_yuan'] is not None else None
    out['within_precision'] = abs(Decimal(out['delta_yuan'])) < Decimal('0.000002') if out['delta_yuan'] is not None else None
    out['precision_yuan'] = '0.000002'   # 平台一个整数计量单位的人民币价值；不把原始差额抹成零。
    out['unexplained_yuan'] = str(actual - Decimal(out['known_yuan'])) if out['known_yuan'] is not None else None
    return out


def report(account, limit=100):
    check_account(account)
    if not 1 <= limit <= 500:
        raise CostError('查询条数须在 1～500 之间')
    with database() as db:
        state = _state(db, account)
        rows = db.execute('SELECT l.record, x.record FROM logs l LEFT JOIN local x ON x.account=l.account AND x.request_id=l.request_id '
                          'WHERE l.account=? ORDER BY l.ts DESC, l.key LIMIT ?', (account, limit)).fetchall()
        pending = db.execute('SELECT record FROM local x WHERE account=? AND NOT EXISTS '
                             '(SELECT 1 FROM logs l WHERE l.account=x.account AND l.request_id=x.request_id) '
                             'ORDER BY rowid DESC LIMIT ?', (account, limit)).fetchall()
        total = db.execute('SELECT count(*) FROM logs WHERE account=?', (account,)).fetchone()[0]
    return {'account': account, 'state': state, 'stored': total, 'limit': limit,
            'rows': [_view(account, json.loads(r), json.loads(loc) if loc else None) for r, loc in rows],
            'pending': [json.loads(r[0]) for r in pending], 'actual_billing': True}


def save_local(account, record):
    check_account(account)
    rid = record.get('request_id')
    if not rid or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', rid):
        raise CostError('本地回包缺少合法平台请求编号，不能精确对应')
    # 不存认证头、消息正文、思考或工具内容。
    safe = {k: record[k] for k in ('request_id', 'session', 'mid', 'model', 'status', 'ts', 'complete', 'use', 'error') if k in record}
    with database() as db, db:
        db.execute('INSERT OR REPLACE INTO local VALUES (?,?,?)', (account, rid, json.dumps(safe, ensure_ascii=False)))
    refresh_prices()


_prices = {}                  # (账号, 模型回复编号) → 人民币实扣，来自精确匹配的平台消费记录
_price_generation = 0
_price_loaded = None
_price_lock = threading.RLock()


def version():
    """内存版本；推送每轮都读，不在事件循环查磁盘。"""
    return _price_generation


def refresh_prices():
    """同步/本地回包落盘后更新费用快照；没有回复编号的消费不猜归属。"""
    global _prices, _price_generation, _price_loaded
    with _price_lock:
        current = {}
        if accounts() and (instance.DATA / 'platform_costs/audit.sqlite3').exists():
            with database() as db:
                rows = db.execute('SELECT l.account, l.record, x.record FROM logs l JOIN local x '
                                  'ON l.account=x.account AND l.request_id=x.request_id').fetchall()
            for account, raw, local in rows:
                r, loc = json.loads(raw), json.loads(local)
                mid = loc.get('mid')
                if not enabled(account) or r['type'] != 2 or not mid:
                    continue
                k = (account, mid)
                # 同一个回复编号如果确实产生多笔平台扣费，按不同请求编号相加，不漏重试实扣。
                current[k] = current.get(k, Decimal(0)) + _decimal(r['actual_yuan'])
        if current != _prices or _price_loaded != str(instance.DATA):
            _prices = current
            _price_generation += 1
        _price_loaded = str(instance.DATA)


def ensure_prices():
    if _price_loaded != str(instance.DATA):
        refresh_prices()


def price_for(account, mid):
    """不联网；已查到的零金额也是实扣，和还没查到不同。"""
    value = _prices.get((account, mid))
    return None if value is None else float(value)


def call_prices(account_by_mid):
    """只给当前可见会话的回复，不能把整把 API key 的日志送给普通会话参与者。"""
    return {mid: {'account': account, 'estimated': (value := price_for(account, mid)) is None, 'yuan': value}
            for mid, account in account_by_mid.items() if enabled(account)}


def quote(mid, account, model, use):
    """供账本/汇总使用：实扣优先；未查到时按配置预估，缺一小时价临时用普通写入价。"""
    if not enabled(account):
        return use
    ensure_prices()
    value = price_for(account, mid)
    estimated = value is None
    if estimated:
        base = model.split('@account=')[0].split('[')[0]
        r = instance.API[account].get('rates', {}).get(base)
        if not r:
            raise CostError('平台费用尚未取得，且此模型没有可用的预估单价')
        hour = use.get('cw1h', 0)
        amount = sum(Decimal(str(use.get(k, 0))) * _decimal(r[k]) for k in ('in', 'out', 'cr', 'cw'))
        amount += Decimal(str(hour)) * (_decimal(r.get('cw1h', r['cw'])) - _decimal(r['cw']))
        value = float(amount / 1000000)
    return {**use, 'charge_yuan': value, 'estimated_calls': int(estimated)}


async def poll():
    while True:
        for account in accounts():
            try:
                await asyncio.to_thread(sync, account)
            except (CostError, OSError, sqlite3.Error) as exc:
                print(f'{account} 平台账单同步失败：{exc}', flush=True)
        await asyncio.sleep(INTERVAL)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='读取平台实际扣费；请求未对应时预估，查到后替换')
    parser.add_argument('action', choices=['sync', 'show'])
    parser.add_argument('account')
    parser.add_argument('--limit', type=int, default=30)
    args = parser.parse_args()
    try:
        if args.action == 'sync':
            sync(args.account)
        print(json.dumps(report(args.account, args.limit), ensure_ascii=False, indent=2))
    except (CostError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'平台账单查询失败：{exc}\n')
