"""旁路观察 API 回包，仅提取请求编号和用量，不改转发字节，也不保存正文。"""
import json
import time

LIMIT = 1024 * 1024


class ResponseAudit:
    def __init__(self, session):
        self.record = {'session': session, 'ts': time.time(), 'complete': False}
        self.head = bytearray()
        self.wire = bytearray()
        self.body = bytearray()
        self.headers_done = False
        self.chunked = False
        self.remaining = None
        self.need_crlf = False
        self.ended = False
        self.sse = False
        self.usage = {}
        self.error = ''

    def feed(self, data):
        if self.error:
            return
        try:
            if not self.headers_done:
                self.head.extend(data)
                at = self.head.find(b'\r\n\r\n')
                if at < 0:
                    if len(self.head) > 65536:
                        raise ValueError('响应头超过限制')
                    return
                header = bytes(self.head[:at])
                data = bytes(self.head[at + 4:])
                self.head.clear()
                lines = header.split(b'\r\n')
                status = int(lines[0].split()[1])
                if 100 <= status < 200:
                    self.feed(data)
                    return
                self.record['status'] = status
                headers = {k.strip().lower(): v.strip() for line in lines[1:] for k, sep, v in [line.partition(b':')] if sep}
                self.record['request_id'] = headers.get(b'x-oneapi-request-id', b'').decode('ascii')
                self.headers_done = True
                if headers.get(b'content-encoding', b'identity') not in (b'', b'identity'):
                    raise ValueError('压缩响应尚不能读取用量')
                encoding = headers.get(b'transfer-encoding', b'').lower()
                if encoding not in (b'', b'chunked'):
                    raise ValueError('不支持的传输编码')
                self.chunked = encoding == b'chunked'
                self.sse = b'text/event-stream' in headers.get(b'content-type', b'')
            if not self.chunked:
                self._body(data)
                return
            self.wire.extend(data)
            while self.wire and not self.ended:
                if self.need_crlf:
                    if len(self.wire) < 2:
                        break
                    if self.wire[:2] != b'\r\n':
                        raise ValueError('分块响应结束符无效')
                    del self.wire[:2]
                    self.need_crlf = False
                if self.remaining is None:
                    at = self.wire.find(b'\r\n')
                    if at < 0:
                        break
                    self.remaining = int(self.wire[:at].split(b';')[0], 16)
                    del self.wire[:at + 2]
                    if self.remaining == 0:
                        self.ended = True
                        self.wire.clear()
                        break
                    if self.remaining < 0:
                        raise ValueError('分块长度无效')
                take = min(self.remaining, len(self.wire))
                self._body(bytes(self.wire[:take]))
                del self.wire[:take]
                self.remaining -= take
                if self.remaining == 0:
                    self.remaining = None
                    self.need_crlf = True
            if len(self.wire) > LIMIT:
                raise ValueError('分块头缓冲超过限制')
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError):
            self.error = '回包用量解析失败或格式不支持，原始响应仍照常转发'
            self.record['complete'] = False
            self.head.clear()
            self.wire.clear()
            self.body.clear()

    def _body(self, data):
        self.body.extend(data)
        if self.sse:
            # SSE 的换行可以跨网络包；只解析完整事件。
            self.body[:] = self.body.replace(b'\r\n', b'\n')
            while (at := self.body.find(b'\n\n')) >= 0:
                event = bytes(self.body[:at])
                del self.body[:at + 2]
                payload = b'\n'.join(line[5:].lstrip(b' ') for line in event.split(b'\n') if line.startswith(b'data:'))
                if payload and payload != b'[DONE]':
                    self._event(json.loads(payload))
        if len(self.body) > LIMIT:
            raise ValueError('单个事件或非流式回包超过观察限制')

    def _usage(self, usage):
        if not isinstance(usage, dict):
            raise ValueError('用量不是对象')
        for k, v in usage.items():
            if k == 'cache_creation':
                if not isinstance(v, dict):
                    raise ValueError('缓存用量不是对象')
                self.usage[k] = {**self.usage.get(k, {}), **v}
            elif k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'):
                self.usage[k] = v

    def _message(self, message):
        self.record['mid'] = message.get('id')
        self.record['model'] = message.get('model')
        self._usage(message.get('usage') or {})

    def _event(self, event):
        kind = event.get('type')
        if kind == 'message_start':
            self._message(event['message'])
        elif kind == 'message_delta':
            self._usage(event.get('usage') or {})
        elif kind == 'message_stop':
            self.record['complete'] = self.record.get('status') == 200
        elif kind == 'error':
            self.error = '上游流返回错误，未获得完整用量'
            self.record['complete'] = False

    def finish(self):
        if self.error:
            self.record['complete'] = False
        if not self.sse and not self.error:
            try:
                msg = json.loads(self.body)
                if msg.get('type') == 'message':
                    self._message(msg)
                    self.record['complete'] = self.record.get('status') == 200
            except (ValueError, TypeError, AttributeError):
                self.error = '非流式回包不是可识别的消息'
        use = {'in': self.usage.get('input_tokens'), 'out': self.usage.get('output_tokens'),
               'cw': self.usage.get('cache_creation_input_tokens', 0), 'cr': self.usage.get('cache_read_input_tokens', 0),
               'cw1h': (self.usage.get('cache_creation') or {}).get('ephemeral_1h_input_tokens', 0)}
        valid = all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in use.values())
        if valid and use['cw1h'] <= use['cw']:
            self.record['use'] = use
        else:
            self.record['complete'] = False
            self.error = self.error or '用量字段缺失或相互矛盾'
        if not self.record['complete']:
            self.record['error'] = self.error or '响应中断，未收到完整用量'
        return self.record
