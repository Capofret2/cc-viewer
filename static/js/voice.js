// 讯飞语音输入：设置里配过讯飞服务和密钥的人（服务器 voice.py，跟着账号存），输入框发送按钮左边多一个波形按钮。
// 点一下开始说，识别出的字边说边写进输入框（光标处），再点一下结束；不自动发送。
// 浏览器直接连讯飞：每次开口先问服务器要一个签好的地址（/api/voice/sign，密钥不出服务器），
// 麦克风经 voice-worklet.js 降到 16kHz、每 40ms 一帧发过去。协议照一套已在用的讯飞语音输入脚本：
// v1（中英识别大模型）和 v2（听写、中文识别大模型）两种帧格式；dwa=wpgs 时讯飞会回头改前面的字（rpl 带 rg 指明换掉哪几段），按 sn 记各段。
// 讯飞一次连接最多 60 秒，说到 55 秒先接上一条新连接、再让旧的收尾，旧的最后几个字照样收得到，接缝处不丢音频（没连上时攒着）。
// 浏览器只在 https（或本机地址）的页面上给麦克风，局域网的 http 地址用不了，点了照实说
const { reactive } = Vue;
import { api, say, fail } from './util.js';

const SEGMENT_MS = 55_000;     // 一条连接用到这么久就接新的
const MAX_MS = 10 * 60_000;    // 一次最长说这么久（防忘了关，一直耗讯飞的额度）
const FINAL_WAIT_MS = 5_000;   // 点了结束以后最多等讯飞这么久给最后的字

const b64 = buf => {
  const u = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
  return btoa(s);
};
const unb64 = s => new TextDecoder().decode(Uint8Array.from(atob(s), c => c.charCodeAt(0)));

export function useVoice({ current, composerInput, autoGrow, globalSettings }) {
  // 正在说的那一次：tab 是开口时的标签页（中途切走了字照样写回它），state 是 connecting（取地址、开麦克风）/ live / closing（等最后的字）
  const rec = reactive({ tab: null, state: '' });
  let run = null;   // 这一次的内部状态，不放进响应式（每 40ms 改一回，不必让整页重画）

  // 波形按钮跟着音量动：直接改按钮上的 --lv，不经 Vue
  const setLevel = lv => {
    for (const b of document.querySelectorAll('.composer .voice.on')) b.style.setProperty('--lv', Math.min(1, lv * 6).toFixed(2));
  };

  // 输入框里的字 = 开口时光标前的 + 各条连接认出的 + 光标后的
  // 说着说着输入框被别的改了（点了发送清空、自己动手改了字）：以改后的内容为准接着往后写，已经写进去的那些字不再管
  function render() {
    if (!run) return;
    const text = run.sessions.map(s => s.text).join('');
    if (run.shown != null && run.tab.draft !== run.shown) {
      Object.assign(run, { before: run.tab.draft, after: '', skip: run.written });
    }
    run.written = text.length;
    run.tab.draft = run.shown = run.before + text.slice(run.skip) + run.after;
    if (current.value === run.tab) autoGrow();
  }

  async function sign() {
    return api('/api/voice/sign', { method: 'POST' });
  }

  // 一条讯飞连接：音频先攒在 queue 里，连上了一起发；收到 status 2（最后的结果）或出错、断开都算完（done）
  function openSession(signed) {
    const s = { ws: null, open: false, first: true, seq: 0, queue: [], segs: {}, text: '', ended: false, finished: false };
    s.done = new Promise(res => (s.finish = () => { if (!s.finished) { s.finished = true; res(); } }));
    s.ws = new WebSocket(signed.url);
    s.ws.onopen = () => { s.open = true; for (const f of s.queue.splice(0)) sendFrame(s, signed, f.pcm, f.status); };
    s.ws.onmessage = e => onMessage(s, signed, e.data);
    s.ws.onerror = () => {};   // 具体原因在 onclose 里说
    s.ws.onclose = e => {
      if (!s.finished && run && !s.ended) stopWith(`讯飞连接断开了（${e.code}${e.reason ? `：${e.reason}` : ''}）`);
      s.finish();
    };
    return s;
  }
  function push(s, signed, pcm, status = 1) {
    if (s.open) sendFrame(s, signed, pcm, status);
    else s.queue.push({ pcm, status });
  }
  function sendFrame(s, signed, pcm, status) {
    const audio = pcm ? b64(pcm) : '';
    const st = s.first ? 0 : status;
    let frame;
    if (signed.proto === 'v1') {
      s.seq += 1;
      frame = { header: { app_id: signed.appid, status: st },
                payload: { audio: { encoding: 'raw', sample_rate: 16000, channels: 1, bit_depth: 16, seq: s.seq, status: st, audio } } };
      if (s.first) frame.parameter = { iat: { domain: 'slm', language: 'zh_cn', accent: 'mandarin', eos: 10000, dwa: 'wpgs',
                                              result: { encoding: 'utf8', compress: 'raw', format: 'json' } } };
    } else {
      frame = { data: { status: st, format: 'audio/L16;rate=16000', encoding: 'raw', audio } };
      if (s.first) {
        frame.common = { app_id: signed.appid };
        frame.business = { language: 'zh_cn', domain: 'iat', accent: 'mandarin', vad_eos: 10000, dwa: 'wpgs',
                           ...(signed.resId ? { res_id: signed.resId } : {}) };
      }
    }
    // 还没发过音频就要结束（刚开口就点了结束）：先发一帧空的开头，再发结尾
    if (s.first && status === 2) { s.first = false; s.ws.send(JSON.stringify(frame)); return sendFrame(s, signed, null, 2); }
    s.first = false;
    try { s.ws.send(JSON.stringify(frame)); } catch { /* 断了由 onclose 报 */ }
  }
  function onMessage(s, signed, raw) {
    let d;
    try { d = JSON.parse(raw); } catch { return; }
    let code, msg, status, result;
    if (signed.proto === 'v1') {
      code = d.header?.code; msg = d.header?.message; status = d.header?.status;
      const t = d.payload?.result?.text;
      if (t) { try { result = JSON.parse(unb64(t)); } catch { /* 坏的一条不管 */ } }
    } else {
      code = d.code; msg = d.message; status = d.data?.status; result = d.data?.result;
    }
    if (code !== 0) {
      s.ended = true;
      stopWith(`讯飞返回错误 ${code}：${msg || '未知原因'}`);
      return s.finish();
    }
    if (result) {
      const word = (result.ws || []).map(w => w.cw?.[0]?.w || '').join('');
      if (result.pgs) {
        if (result.pgs === 'rpl' && result.rg) for (let i = result.rg[0]; i <= result.rg[1]; i++) delete s.segs[i];
        s.segs[result.sn] = word;
        s.text = Object.keys(s.segs).map(Number).sort((a, b) => a - b).map(k => s.segs[k]).join('');
      } else s.text += word;
      render();
    }
    if (status === 2) { s.ended = true; try { s.ws.close(); } catch {} s.finish(); }
  }

  async function startVoice(tab) {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      return fail('语音输入', new Error('浏览器只在 https 地址上允许使用麦克风，请改用 https 地址打开'));
    }
    // 手机上 AudioContext 要在点按的那一下里建，等过别的再建会是暂停的
    const ctx = new AudioContext();
    const el = composerInput.value;
    const at = el && current.value === tab ? el.selectionEnd : tab.draft.length;
    run = { tab, ctx, stream: null, node: null, sessions: [], signed: null, started: 0, segStart: 0, pending: null, shown: null, written: 0, skip: 0,
            before: tab.draft.slice(0, at), after: tab.draft.slice(at) };
    Object.assign(rec, { tab, state: 'connecting' });
    const mine = run;
    try {
      const [stream, signed] = await Promise.all([
        navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } }),
        sign()]);
      if (run !== mine) { stream.getTracks().forEach(t => t.stop()); return; }   // 等的时候已经点了结束
      run.stream = stream;
      if (ctx.sampleRate < 16000) throw new Error(`麦克风采样率只有 ${ctx.sampleRate}Hz，低于讯飞要求的 16kHz`);
      await ctx.audioWorklet.addModule(new URL('./voice-worklet.js', import.meta.url));
      await ctx.resume();
      if (run !== mine) return;
      run.signed = signed;
      run.sessions.push(openSession(signed));
      run.started = run.segStart = Date.now();
      const node = run.node = new AudioWorkletNode(ctx, 'pcm16k');
      node.port.onmessage = e => onFrame(mine, e.data);
      ctx.createMediaStreamSource(stream).connect(node);
      rec.state = 'live';
    } catch (e) {
      const why = e?.name === 'NotAllowedError' ? '没有麦克风权限（浏览器或系统拒绝了）'
        : e?.name === 'NotFoundError' ? '没有找到麦克风' : e?.message || String(e);
      stopWith(why);
    }
  }

  function onFrame(mine, { pcm, level }) {
    if (run !== mine || rec.state !== 'live') return;
    setLevel(level);
    const now = Date.now();
    if (now - run.started > MAX_MS) {
      push(last(), run.signed, pcm);
      return stopVoice('已说满 10 分钟，自动结束');
    }
    if (now - run.segStart > SEGMENT_MS) {
      // 先接上新连接，再让旧的收尾：这一帧起的音频都进新连接，旧连接把最后的字送回来就关
      const old = last();
      run.segStart = now;
      sign().then(signed => {
        if (run !== mine) return;
        push(old, run.signed, null, 2);
        run.signed = signed;
        run.sessions.push(openSession(signed));
        for (const p of run.pending.splice(0)) push(last(), signed, p);
        run.pending = null;
      }).catch(e => stopWith(`续接讯飞连接失败：${e.message || e}`));
      run.pending = [pcm];
      return;
    }
    if (run.pending) return void run.pending.push(pcm);   // 新地址还没签回来，攒着
    push(last(), run.signed, pcm);
  }
  const last = () => run.sessions[run.sessions.length - 1];

  // 点了结束：停麦克风，把尾巴发出去，告诉讯飞说完了，等最后的字（最多 FINAL_WAIT_MS）
  async function stopVoice(note) {
    const mine = run;
    if (!mine || rec.state === 'closing') return;
    if (rec.state === 'connecting') return stopWith('');
    rec.state = 'closing';
    // 先让处理器把不满一帧的尾巴交出来，等它到了再停
    await new Promise(res => {
      const t = setTimeout(res, 300);
      mine.node.port.onmessage = e => {
        clearTimeout(t);
        if (mine.pending) mine.pending.push(e.data.pcm);
        else push(last(), mine.signed, e.data.pcm);
        res();
      };
      mine.node.port.postMessage('flush');
    });
    release(mine);
    if (mine.pending) {
      // 正赶上续接、新地址还没回来：等它一会儿
      const until = Date.now() + 3000;
      while (mine.pending && Date.now() < until) await new Promise(r => setTimeout(r, 50));
    }
    if (run !== mine) return;
    push(last(), mine.signed, null, 2);
    const all = Promise.all(mine.sessions.map(s => s.done));
    const timeout = new Promise(res => setTimeout(() => res('timeout'), FINAL_WAIT_MS));
    const r = await Promise.race([all, timeout]);
    if (run !== mine) return;
    for (const s of mine.sessions) { s.ended = true; try { s.ws.close(); } catch {} }
    render();
    run = null;
    Object.assign(rec, { tab: null, state: '' });
    if (r === 'timeout') fail('语音输入', new Error('讯飞没有及时返回最后的结果，已保留目前识别出的文字'));
    else if (note) say(note);
  }
  // 出错或者还没开始说就结束：停掉一切，已经认出来的字留在输入框里
  function stopWith(why) {
    const mine = run;
    if (!mine) return;
    release(mine);
    for (const s of mine.sessions) { s.ended = true; try { s.ws.close(); } catch {} }
    run = null;
    Object.assign(rec, { tab: null, state: '' });
    if (why) fail('语音输入', new Error(why));
  }
  function release(mine) {
    try { mine.node?.disconnect(); } catch {}
    mine.stream?.getTracks().forEach(t => t.stop());
    mine.ctx.close().catch(() => {});
    setLevel(0);
  }

  function toggleVoice(tab) {
    if (rec.tab) return rec.tab === tab ? stopVoice('') : fail('语音输入', new Error('另一个标签页正在语音输入，请先在那里结束'));
    startVoice(tab);
  }
  const voiceOn = tab => rec.tab === tab;

  // 设置里「语音输入」那一项：服务器给了 voice 才有（{} 是没配），表单展开时填上已存的服务和 APPID，两个密钥留空是沿用
  const voiceForm = reactive({ open: false, service: 5, appid: '', apiKey: '', apiSecret: '', busy: false });
  function openVoiceForm() {
    const v = globalSettings.voice || {};
    Object.assign(voiceForm, { open: !voiceForm.open, service: v.service || 5, appid: v.appid || '', apiKey: '', apiSecret: '' });
  }
  async function saveVoice() {
    if (voiceForm.busy) return;
    voiceForm.busy = true;
    try {
      globalSettings.voice = (await api('/api/voice', { method: 'PUT', body: {
        service: voiceForm.service, appid: voiceForm.appid, apiKey: voiceForm.apiKey, apiSecret: voiceForm.apiSecret } })).voice;
      voiceForm.open = false;
    } catch (e) {
      fail('保存语音输入配置', e);
    }
    voiceForm.busy = false;
  }
  async function clearVoice() {
    if (voiceForm.busy) return;
    voiceForm.busy = true;
    try {
      globalSettings.voice = (await api('/api/voice', { method: 'DELETE' })).voice;
      voiceForm.open = false;
    } catch (e) {
      fail('清除语音输入配置', e);
    }
    voiceForm.busy = false;
  }

  return { voiceRec: rec, toggleVoice, voiceOn, voiceForm, openVoiceForm, saveVoice, clearVoice };
}
