// 对话与会话设置：输入框、发送、打断，以及模型、权限模式、上下文用量、信息面板
const { ref, reactive, computed, nextTick, watch } = Vue;
import { api, touch, say, fail, fileSize, clock } from './util.js';
import { withTab, takeRejected } from './tabs.js';
import { callPoints, fen, accountOf } from './points.js';
import { phaseLine, phaseStep, STEPS, SRC_CLASS, SRC_TIPS } from './phase.js';

// 权限模式：只影响网页发起的对话，终端里的会话不受影响
export const MODE_TIPS = {
  dontAsk: '拒绝所有未预先允许的操作',
  acceptEdits: '自动允许修改文件，命令按设置执行',
  auto: '由 Claude Code 自动判断是否放行',
  plan: '仅制定计划，不修改文件、不执行命令',
  bypassPermissions: '全部放行，不做任何检查',
};

// createFrom：新会话标签页说出第一句话时，用它把会话建出来（在 sessions.js 里）
// pushQuiet：推送连接多久没收到东西了（毫秒），用来看出网页和服务器之间是不是断了（在 tabs.js 里）
// pushEvery：服务器说的心跳间隔（秒），旧后端不说是 0
// gptTab(tab)：这个标签页用的是不是 GPT 号（经本机翻译层用 ChatGPT 订阅的 Claude Code，见 instance.py）；
// familyTab(tab)：这个标签页的号是哪一家的（claude / gpt / deepseek，服务器 instance.account_family），没给就按 gptTab 分两家
export function useChat({ current, stickToBottom, readonly, createFrom, pushQuiet, pushEvery = () => 0, gptTab, familyTab, relayTab, personaName }) {
  const familyOf = tab => familyTab?.(tab) || (gptTab?.(tab) ? 'gpt' : 'claude');
  // 中转站号（转 Claude 模型的平台）能选的模型 [{value, label}]，不是中转站是 null（app.js 的 relayOf）
  const relayOf = tab => relayTab?.(tab) || null;
  // 草稿跟着标签走（tab.draft），回复不从这里拿，会照常写进会话记录、推送过来
  const composerInput = ref(null);
  function autoGrow() {
    const el = composerInput.value;
    if (!el) return;
    fitWidth(el);   // 先定宽再量高：宽度变了，折不折行跟着变
    el.style.height = 'auto';
    // 缓存失效的提示（cache-ph）写在透明的 placeholder 里，比平时的提示长，窄屏上会折行；浏览器量 scrollHeight 时把折行的提示也算进去，
    // 删空时输入框平白高成两行。显示它的那行字本来就只有一行（.cache-hint），所以空着时按一行（rows=1）的高度算
    const empty = !el.value && el.classList.contains('cache-ph');
    el.style.height = Math.min(empty ? el.clientHeight : el.scrollHeight, 200) + 'px';
    fitButtons();
    // 把输入框的实际高度告诉样式，「回到底部」按钮据此往上让
    const box = el.closest('.composer');
    if (box) document.documentElement.style.setProperty('--composer-h', `${box.offsetHeight}px`);
  }
  // 输入框先窄（样式里的 --composer-min），打字时先往两边变宽，宽到正文那一栏才折行变高（电脑上一开始就很长不好看）。
  // 拿一个不折行的影子量最长那一行多宽，加上左边（「＋」那一截）、按钮那一块和两头的边，写进外层的 --composer-w，
  // 样式按 min(100%, max(起始宽度, 它)) 定宽；手机上那一栏本来就比起始宽度窄，照旧撑满。底下小标签那一排跟着一样宽
  let liner = null;
  function fitWidth(el) {
    const box = el.closest('.composer');
    const host = box?.parentElement;
    if (!host) return;
    if (!liner) {
      liner = document.createElement('span');
      liner.setAttribute('aria-hidden', 'true');
      Object.assign(liner.style, { position: 'fixed', left: '-9999px', top: '0', visibility: 'hidden', whiteSpace: 'pre' });
      document.body.append(liner);
    }
    const cs = getComputedStyle(el), bs = getComputedStyle(box);
    for (const k of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing', 'tabSize']) liner.style[k] = cs[k];
    liner.textContent = el.value;   // 多行时影子宽度就是最长那一行
    const text = el.value ? liner.getBoundingClientRect().width : 0;
    // 左边一律按没让到底下时算（让了以后左边少了「＋」那一截，按它算会来回跳）；右边是按钮离边 --pad、按钮、按钮前留的 8px（同 fitButtons）
    const shift = box.classList.contains('drop') ? parseFloat(bs.getPropertyValue('--drop-shift')) || 0 : 0;
    const btns = box.querySelector('.composer-btns');
    const need = parseFloat(bs.borderLeftWidth) + parseFloat(bs.paddingLeft) + shift + text + 8
      + (btns?.offsetWidth || 0) + (parseFloat(bs.getPropertyValue('--pad')) || 0) + parseFloat(bs.borderRightWidth) + 4;
    host.style.setProperty('--composer-w', Math.ceil(need) + 'px');
  }
  // 按钮和文字互相让：文字占满整宽，按钮浮在右下角。拿一个看不见的影子照着输入框排一遍字（没字就排占位提示），
  // 有哪一行伸进了按钮那一块（左边再留 8px），就给输入框加 drop，按钮落到文字下面另起一排；
  // 输入框到了最高、里面能滚时，哪一行都可能滚到按钮底下，也算碰到
  let shadow = null;
  function fitButtons() {
    const el = composerInput.value;
    const box = el?.closest('.composer');
    const btns = box?.querySelector('.composer-btns');
    if (!btns || !el.clientWidth) return;
    if (!shadow) {
      shadow = document.createElement('div');
      shadow.setAttribute('aria-hidden', 'true');
      Object.assign(shadow.style, { position: 'fixed', left: '-9999px', top: '0', visibility: 'hidden',
                                    whiteSpace: 'pre-wrap', overflowWrap: 'break-word', boxSizing: 'border-box' });
      document.body.append(shadow);
    }
    const cs = getComputedStyle(el);
    for (const k of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'lineHeight', 'letterSpacing', 'wordBreak',
                     'tabSize', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'minHeight']) {
      shadow.style[k] = cs[k];
    }
    // 让到底下以后文字往左多出「＋」那一截（--drop-shift）：一律按没让时的宽度量，
    // 不然变宽以后字又排得下、取消让位，变窄了又碰到按钮，来回跳
    const shift = box.classList.contains('drop') ? parseFloat(getComputedStyle(box).getPropertyValue('--drop-shift')) || 0 : 0;
    shadow.style.width = el.clientWidth - shift + 'px';
    // 末尾补一个零宽字符：结尾是换行时，影子里也要有那一行空行。
    // 缓存失效的提示（cache-ph，见 index.html）不量：它比平时的提示长，量了按钮就让到底下、输入框平白高出一行，
    // 叠上去显示它的那行字从文字区的左边起、到按钮左边 8px 为止（--hint-left、--hint-w），放不下截成省略号
    const ph = el.classList.contains('cache-ph') ? '' : el.placeholder;
    shadow.textContent = (el.value || ph) + '​';
    box.style.setProperty('--hint-left', `${el.offsetLeft}px`);
    box.style.setProperty('--hint-w', `${Math.max(0, btns.getBoundingClientRect().left - el.getBoundingClientRect().left - 8)}px`);
    const s = shadow.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(shadow);
    // 按钮比文字区更靠右（文字两边留得一样宽，按钮贴得更近），横向按按钮实际的位置算
    const left = btns.getBoundingClientRect().left - el.getBoundingClientRect().left - shift - 8;
    const top = s.height - btns.offsetHeight;
    // 药丸：字一超过一行就让（「＋」、按钮都落到底下那一行，文字左右一样宽，谷歌的输入框也是这样；
    // 不然多行时左边让着「＋」、右边没让，左右边距对不齐）
    const multi = s.height > parseFloat(cs.minHeight) + 1;
    // 空着挂缓存失效提示时不看能不能滚：浏览器把折行的透明提示算进 scrollHeight，窄屏上就判成碰到、让到底下，
    // 让了文字区宽出 --drop-shift 提示又排得下、取消让位，变窄又折行，每一帧来回切，输入框上下跳个不停（手机上碰到过）
    const scrolls = !(!el.value && el.classList.contains('cache-ph')) && el.scrollHeight > el.clientHeight + 1;
    const hit = multi || scrolls
      || [...range.getClientRects()].some(r => r.width > 0.5 && r.right - s.left > left && r.bottom - s.top > top);
    box.classList.toggle('drop', hit);
  }
  // 输入框的高度一变（包括刚画出来的那一下）就告诉样式，「折叠」「回到底部」按钮据此往上让。
  // 只在打字、切标签页时量不够：页面刚打开时输入框还没画出来，量到的是 0，按钮会压住发送和打断键
  const composerWatcher = new ResizeObserver(([e]) =>
    document.documentElement.style.setProperty('--composer-h', `${e.target.offsetHeight}px`));
  // 按钮多了少了（开始干活多出打断键）、输入框变宽变窄，都要重新看一遍让不让
  const btnsWatcher = new ResizeObserver(() => fitButtons());
  watch(composerInput, el => {
    composerWatcher.disconnect();
    btnsWatcher.disconnect();
    const box = el?.closest('.composer');
    // 按整个边框盒子盯：让到底下（drop）变的是下内边距，内容区不变，只盯内容区就察觉不到，
    // 「回到底部」这些按钮停在旧高度、压到输入框上
    if (box) composerWatcher.observe(box, { box: 'border-box' });
    const btns = box?.querySelector('.composer-btns');
    if (btns) { btnsWatcher.observe(btns); btnsWatcher.observe(el); }
  });
  function onEnter(e) {
    if (touch) return;                                // 手机上回车就是换行，发送点按钮
    if (e.isComposing || e.keyCode === 229) return;   // 输入法还在选字，这个回车不算发送
    e.preventDefault();
    sendChat();
  }
  // 粘贴、拖进来或者点按钮选的图片，跟着这一条一起发给 Claude
  const MAX_IMAGE = 4_000_000;   // 单张上限，太大 CLI 那边也吃不下
  const MAX_SIDE = 2000;         // 超过上限的图缩到这个长边再压成 JPEG，Claude 看图本来也用不到更大
  // 手机拍的原图动不动就超过上限，与其拒掉不如在浏览器里缩一下。按照片里记的方向摆正，免得竖拍的变横
  async function shrink(f) {
    const bmp = await createImageBitmap(f, { imageOrientation: 'from-image' });
    const k = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas');
    c.width = Math.round(bmp.width * k);
    c.height = Math.round(bmp.height * k);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close();
    const blob = await new Promise(res => c.toBlob(res, 'image/jpeg', 0.85));
    if (!blob) throw new Error('浏览器无法将图片压缩为 JPEG');
    return blob;
  }
  async function addImages(files) {
    const tab = current.value;
    if (!tab) return;
    for (const f of files) {
      if (!f.type?.startsWith('image/')) continue;
      const name = f.name || '截图';
      let img = f;
      if (f.size > MAX_IMAGE) {
        try { img = await shrink(f); }
        catch (e) { tab.chatError = `「${name}」超过 4 MB，压缩失败：${e.message || e}`; continue; }
        if (img.size > MAX_IMAGE) { tab.chatError = `「${name}」压缩至长边 ${MAX_SIDE} 像素后仍超过 4 MB`; continue; }
      }
      const url = await new Promise(res => {
        const r = new FileReader();
        r.onload = () => res(r.result);
        r.onerror = () => res('');
        r.readAsDataURL(img);
      });
      const m = /^data:([^;]+);base64,(.*)$/.exec(url || '');
      if (m) tab.attach.push({ media_type: m[1], data: m[2], url, name });
      else tab.chatError = `无法读取「${name}」`;
    }
    nextTick(autoGrow);
  }
  // 手机上没法拖、剪贴板里的图也粘不进来，只能点按钮选（安卓上可以直接拍照）。
  // 选完把输入框清空，不然同一张图再选一次不会触发 change
  function onPickFiles(e) {
    const files = [...(e.target.files || [])];
    e.target.value = '';
    addImages(files);
  }

  // 回形针：什么文件都行，选好就开始传到服务器上这个会话的上传目录（server.upload），发话时由服务器在话后面写明放在哪。
  // 图片走上面那条（直接给 Claude 看）；要把图片原样存到服务器上，就用回形针选
  const MAX_UPLOAD = 50 * 1024 * 1024;   // 和 server.MAX_UPLOAD 一致
  function uploadFiles(files) {
    const tab = current.value;
    if (!tab || !files.length) return;
    if (tab.fresh) return say('新会话需先发送一条消息，才能上传文件', { err: true });
    for (const f of files) {
      const item = reactive({ name: f.name || '文件', size: f.size, loaded: 0, path: '', error: '', xhr: null });
      tab.files.push(item);
      if (f.size > MAX_UPLOAD) { item.error = '超过 50 MB 上限'; continue; }
      // 用 XMLHttpRequest 是为了拿到上传进度（fetch 给不了）
      const xhr = item.xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/tabs/${tab.id}/upload?name=${encodeURIComponent(item.name)}`);
      xhr.upload.onprogress = e => { item.loaded = e.loaded; };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { /* 下面按状态码说 */ }
        if (xhr.status === 200 && data?.path) Object.assign(item, { path: data.path, size: data.size, loaded: data.size });
        else item.error = data?.detail || `服务器返回异常（${xhr.status} ${xhr.statusText}）`;
        item.xhr = null;
      };
      xhr.onerror = () => { item.error = '无法连接服务器'; item.xhr = null; };
      xhr.onabort = () => { item.error = '已取消'; item.xhr = null; };
      xhr.send(f);
    }
    nextTick(autoGrow);
  }
  // 卡片上的叉：还在传就取消，传好的只是这次不带上（服务器上的文件留着）
  function dropFile(tab, i) {
    tab.files[i].xhr?.abort();
    tab.files.splice(i, 1);
    nextTick(autoGrow);
  }
  // 新会话标签页还没有会话、没有上传目录，点了当场说清楚，不弹选文件。
  // 隐藏的选文件框按 class 在这个输入框里找（简洁模式从「＋」菜单点进来，按钮旁边不是它）
  function pickUpload(e) {
    if (current.value?.fresh) return say('新会话需先发送一条消息，才能上传文件', { err: true });
    e.currentTarget.closest('.composer')?.querySelector('.pick-any')?.click();
  }
  function onPickAny(e) {
    const files = [...(e.target.files || [])];
    e.target.value = '';
    uploadFiles(files);
  }
  const isImage = f => f.type?.startsWith('image/');
  function onPaste(e) {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) {
      e.preventDefault();   // 别再把图片的文件名当文字粘进来
      addImages(files.filter(isImage));
      uploadFiles(files.filter(f => !isImage(f)));
    }
  }
  // 拖进来的：图片照旧直接给 Claude 看，别的文件传到服务器
  function onDropFiles(e) {
    const files = [...(e.dataTransfer?.files || [])];
    addImages(files.filter(isImage));
    uploadFiles(files.filter(f => !isImage(f)));
  }

  async function sendChat() {
    const tab = current.value, text = tab && tab.draft.trim();
    if (!tab || (!text && !tab.attach.length && !tab.files.length && !tab.staged?.length)) return;
    // 先查再清空：上一句还没发出去时直接清掉输入框，这句就丢了
    if (tab.sending) return say('上一条消息仍在发送，请稍候');
    if (tab.files.some(f => f.xhr)) return say('文件仍在上传，请稍候');
    if (tab.files.some(f => f.error)) return say('有文件上传失败，请先移除', { err: true });
    const images = tab.attach.splice(0), files = tab.files.splice(0);
    const shrink = shrinkFrom();
    tab.draft = '';
    nextTick(() => { autoGrow(); shrink(); });
    await deliver(tab, text, images, files);
  }
  // 发出去以后输入框缩回一行，带动画（大段话发出去后不能一下子缩回去）：清空前记下输入框和底下那排小标签（简洁模式）的宽高，
  // 清空、重新量好以后从旧的宽高动画到新的。字已经清空了，中间只是框在变：输入框是贴底的，文字和按钮都靠底，看着是上沿往下收、两边往里收。
  // 动画结束不留样式，宽高照旧由样式和 --composer-w 定
  function shrinkFrom() {
    const box = composerInput.value?.closest('.composer');
    if (!box || matchMedia('(prefers-reduced-motion: reduce)').matches) return () => {};
    const els = [box, box.parentElement?.querySelector(':scope > .tools-slot > .bar-tools')].filter(Boolean);
    const was = els.map(e => [e.offsetWidth, e.offsetHeight]);
    return () => els.forEach((e, k) => {
      const [w, h] = was[k], tall = k === 0;   // 小标签那排只变宽窄
      if (!e.isConnected || (Math.abs(w - e.offsetWidth) < 2 && (!tall || Math.abs(h - e.offsetHeight) < 2))) return;
      const at = (ww, hh) => ({ boxSizing: 'border-box', width: `${ww}px`, ...(tall ? { height: `${hh}px` } : {}) });
      e.animate([at(w, h), at(e.offsetWidth, e.offsetHeight)], { duration: 260, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
    });
  }
  // 输入框旁的「+」：只存不发。存在服务器上，显示成空心气泡，点发送时和输入框里的话一起发出
  async function stageChat() {
    const tab = current.value, text = tab?.draft.trim();
    if (!tab || tab.fresh || !text || tab.staging) return;
    tab.staging = true;
    try {
      Object.assign(tab, await withTab(tab, () => api(`/api/tabs/${tab.id}/staged`, { method: 'POST', body: { text } })));
      if (tab.draft.trim() === text) tab.draft = '';   // 存的时候又接着写了，就别把新写的清掉
      nextTick(autoGrow);
    } catch (e) { fail('保存待发消息', e); }
    tab.staging = false;
  }
  async function unstage(tab, s) {
    try {
      Object.assign(tab, await withTab(tab, () => api(`/api/tabs/${tab.id}/staged/${s.id}`, { method: 'DELETE' })));
    } catch (e) { fail('删除待发消息', e); }
  }
  // 上次没发出去的，原样再发一遍（草稿里可能已经写了别的，所以不往回塞）
  async function retrySend() {
    const tab = current.value;
    if (!tab?.failed) return;
    if (tab.sending) return say('上一条消息仍在发送，请稍候');   // 先查再清，不然这句就丢了
    const { text, images, files } = tab.failed;
    tab.failed = null;
    await deliver(tab, text, images, files);
  }
  // 没发出去的内容放回输入框，图片也一并放回；输入框里已有新内容时不要盖掉
  function restoreFailed(tab) {
    if (!tab?.failed) return;
    const { text, images, files } = tab.failed;
    tab.draft = (tab.draft.trim() ? `${tab.draft.replace(/\s*$/, '')}\n\n` : '') + text;
    tab.attach.push(...(images || []));
    tab.files.push(...(files || []));
    tab.failed = null;
    nextTick(autoGrow);
  }
  async function deliver(tab, text, images = [], files = []) {
    if (tab.sending) return say('上一条消息仍在发送，请稍候');
    tab.sending = true;
    // 先把话挂出来、清空输入框，用户立刻看到反馈。待发的话排在前面，每一条各挂一个「发送中」气泡
    const withStaged = !tab.fresh && tab.staged?.length > 0;
    // 带着文件的，「发送中」气泡里先列出文件名（服务器写进记录的是完整路径，记录到了就换成那份）
    const shown = [text, ...files.map(f => `📎 ${f.name}`)].filter(Boolean).join('\n');
    const texts = [...(withStaged ? tab.staged.map(s => s.text.trim()) : []), shown].filter(Boolean);
    // 从会话末尾往后找自己这句（apply 里的 pending）。往上翻远了、手上这批没到末尾（tabs.js 的 cut）时末尾是 total，
    // 用手上这批的结尾的话，回到底部换成末尾那段以后会从更早的地方找，前面有一样的话（比如「现在呢」）就把气泡提前撤了
    const at = tab.cut ? tab.total : tab.offset + tab.items.length, mid = !!tab.running, base = Date.now() + Math.random();
    const ids = (texts.length ? texts : ['']).map((t, k) => {
      tab.pending.push({ id: base + k, text: t, at, sent: false, mid,
                         shots: k === Math.max(texts.length, 1) - 1 ? images.map(im => im.url) : [] });
      return base + k;
    });
    tab.running = true;   // 状态那一块和气泡同时出现，不会晚一拍再把气泡顶上去
    if (!mid) tab.phase = null;   // 新的一轮：接口回复以前是「正在发送至查看器」；插话时这一轮原来的阶段照旧显示
    tab.failed = null;
    stickToBottom();
    try {
      if (tab.fresh) {   // 新会话标签页：这句话就是第一句，建好会话后这个标签页会换成真正的那个
        await createFrom(tab, text, images);   // 建成才返回，没建成抛出原因（推送来的，见 tabs.js 的 onBirth）
      } else {
        // 服务器在后台核对额度、拦下时推一条带着这个编号的 rejected，那时再把这句话退回来（见 tabs.js 的 takeRejected）
        const nonce = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        tab.inflight[nonce] = { text, images, files, ids };
        const body = { text, nonce, ...(withStaged ? { staged: true } : {}),
                       ...(images.length ? { images: images.map(im => ({ media_type: im.media_type, data: im.data })) } : {}),
                       ...(files.length ? { files: files.map(f => f.path) } : {}) };
        let r;
        const asked = Date.now();
        try {
          r = await withTab(tab, () => api(`/api/tabs/${tab.id}/say`, { method: 'POST', body }));
        } catch (e) { delete tab.inflight[nonce]; throw e; }
        // 推送可能比这个回复先到（apply 收到推送会记 phaseAt）：那时推来的是更新的状态，服务器认为已经推过、不会再推，
        // 拿回复里的旧状态（多半是「正在核对额度」，待发的话也还没清）盖掉它，界面就停在那里直到状态再变
        if (!(tab.phaseAt > asked)) {
          Object.assign(tab, { running: r.running, chatError: r.chatError, staged: r.staged || [],
                               phase: r.phase, phaseAt: Date.now() });
        }
        for (const p of tab.pending) if (ids.includes(p.id)) p.sent = true;
        takeRejected(tab, r.rejected);
      }
    } catch (e) {
      // 没发出去：撤掉气泡，把这句话连同图片留着，旁边给个重试按钮（待发的话还在服务器上，重试时照样一起带上）
      tab.pending = tab.pending.filter(x => !ids.includes(x.id));
      tab.failed = { text, images, files, error: e.message };
      if (tab.fresh) tab.running = false;   // 还没有会话，没有推送来把状态那一块收掉
    }
    tab.sending = false;
  }
  // 引用回复：把这段话逐行加上 > 放进草稿，光标停在后面接着写
  function quote(text) {
    const tab = current.value;
    if (!tab) return;
    const q = text.trim().split('\n').map(l => `> ${l}`).join('\n');
    tab.draft = (tab.draft.trim() ? `${tab.draft.replace(/\s*$/, '')}\n\n` : '') + `${q}\n\n`;
    nextTick(() => {
      autoGrow();
      composerInput.value?.focus();
    });
  }

  // 被钩子拦下、没发出去的那句话放回输入框（「这句话没发出去」那一行的按钮）：接在已经写了的后面，不覆盖
  function refill(text) {
    const tab = current.value;
    if (!tab) return;
    tab.draft = (tab.draft.trim() ? `${tab.draft.replace(/\s*$/, '')}\n\n` : '') + text;
    nextTick(() => {
      autoGrow();
      composerInput.value?.focus();
    });
  }

  // 打断：点下去按钮就变成「正在打断」，等这一轮真的停下（推送来 running=false）再撤；没打断成就说出来
  async function stopChat() {
    const tab = current.value;
    if (!tab || tab.stopping) return;
    tab.stopping = true;
    tab.stopAt = tab.results || 0;   // 结果消息数比这多了，就是被打断的那一轮停了（见 tabs.js 的 apply）
    tab.stopT = Date.now();          // 「已请求打断」的秒数从点下这一刻数起（见 workLine）
    try {
      await withTab(tab, () => api(`/api/tabs/${tab.id}/interrupt`, { method: 'POST' }));
    } catch (e) {
      tab.stopping = false;
      fail('中断', e);
    }
  }

  // 上下文用量：CLI 连着时用它给的真实数值（带上限和百分比），否则退回从会话记录里估的数字。
  // warn 是看板上那个数字的提醒色：20 万起黄、30 万起橙、40 万起红、50 万起红色还呼吸
  const ctxWarn = n => (n >= 500000 ? 'warn-r warn-pulse' : n >= 400000 ? 'warn-r' : n >= 300000 ? 'warn-o' : n >= 200000 ? 'warn-y' : '');
  const ctx = computed(() => {
    const t = current.value;
    if (t?.ctx?.max) return { total: t.ctx.total, max: t.ctx.max, percent: t.ctx.percent ?? 0, warn: ctxWarn(t.ctx.total) };
    return t?.context ? { total: t.context, max: 0, percent: 0, warn: ctxWarn(t.context) } : null;
  });

  // 请求大小提醒：每次请求都把整个上下文（连读过的图片）重新上传，大到上游的单条上限会被拒收。
  // 服务器给最大的一次请求多大、上限多大、到上限的几成才提醒（chat.Driver.req_size）；没到就不显示（提醒不要一直显示）
  const reqWarn = computed(() => {
    const t = current.value, r = t?.reqSize;
    if (!r || r.bytes < r.limit * r.warnAt) return null;
    const mb = n => (n / 1e6).toFixed(1);
    const up = { gpt: 'ChatGPT', deepseek: 'DeepSeek', glm: '智谱' }[familyOf(t)] || 'Claude';
    return {
      text: `请求 ${mb(r.bytes)} / ${mb(r.limit)} MB`,
      over: r.bytes >= r.limit,
      tip: `每次请求都会上传完整上下文（含已读图片），最大一次约 ${mb(r.bytes)} MB，${up} 单次上限约 ${mb(r.limit)} MB，`
        + '超出将被拒绝。建议使用 /compact 压缩上下文或新建会话。',
    };
  });

  // 会话信息面板
  const info = reactive({ open: false, loading: false, error: '', data: null, tab: '' });
  // 模型下拉只给这三个，不列 CLI 报上来的那份，所以新会话标签页也不用等取列表。
  // 后两个性价比低，名字后面写明不推荐。GPT 号只能用 GPT 的模型，换成下面那几个（服务器的 instance.GPT_MODELS，第一个是默认）
  const claudeModels = [
    { value: 'claude-opus-5-5', displayName: 'Opus 5.5' },
    { value: 'claude-sonnet-5-5', displayName: 'Sonnet 5.5（性价比低，不推荐）' },
    { value: 'claude-haiku-5-5', displayName: 'Haiku 5.5（性价比低，不推荐）' },
  ];
  const gptModels = [
    { value: 'gpt-6-sol', displayName: 'GPT-6 Sol' },
    { value: 'gpt-6.1-sol', displayName: 'GPT-6.1 Sol' },
    { value: 'gpt-6-astra', displayName: 'GPT-6 Astra' },
    { value: 'gpt-6-luna', displayName: 'GPT-6 Luna' },
    // 快速档：思考强度不变、出字更快，价格是普通档的 2.5 倍（暂定价）
    { value: 'gpt-6-sol-fast', displayName: 'GPT-6 Sol 快速（2.5 倍价）' },
    { value: 'gpt-6.1-sol-fast', displayName: 'GPT-6.1 Sol 快速（2.5 倍价）' },
    { value: 'gpt-6-astra-fast', displayName: 'GPT-6 Astra 快速（2.5 倍价）' },
    { value: 'gpt-6-luna-fast', displayName: 'GPT-6 Luna 快速（2.5 倍价）' },
  ];
  // DeepSeek 号（API 号）只接了 Flash（服务器 instance.API_MODELS）；[1m] 让 Claude Code 按 1M 上下文算
  const deepseekModels = [
    { value: 'deepseek-flash[1m]', displayName: 'DeepSeek Flash' },
  ];
  // 智谱号（API 号）只接了 GLM-5.3（服务器 instance.API_MODELS）
  const glmModels = [
    { value: 'glm-5.3[1m]', displayName: 'GLM-5.3' },
  ];
  const lists = { claude: claudeModels, gpt: gptModels, deepseek: deepseekModels, glm: glmModels };
  // 中转站号的下拉只列它接了的那几个：配置里写了名字就用，没写就照订阅号同一个模型的叫法
  const relayList = models => models.map(m => ({ value: m.value,
    displayName: m.label || claudeModels.find(c => c.value === m.value)?.displayName || m.value }));
  const modelList = computed(() => {
    const t = current.value, relay = t && relayOf(t);
    return relay ? relayList(relay) : t ? lists[familyOf(t)] || claudeModels : claudeModels;
  });
  // GPT 号、DeepSeek 号上 auto 权限模式用不了（要 Claude 模型审核每个动作），选着 auto 时实际按 dontAsk 跑（服务器 run_mode，快照里的 runMode）
  const runModeOf = tab => tab.runMode || (tab.webMode === 'auto' && familyOf(tab) !== 'claude' ? 'dontAsk' : tab.webMode);
  // 会话正在用的模型是不是这一项：两边都可能带着上下文长度之类的后缀（比如 [1m]，DeepSeek 下拉里的那项带、记录里回的不带），去掉再比
  const bare = m => (m || '').replace(/\[.*\]$/, '');
  const isModel = (value, model) => !!model && bare(model) === bare(value);
  const modelBusy = ref(false), modelError = ref('');
  function applyInfo(tab, data) {
    Object.assign(info, { data, tab: tab.id });
    const u = data.usage || {};
    if (u.maxTokens) tab.ctx = { total: u.totalTokens, max: u.maxTokens, percent: u.percentage };
  }
  async function loadInfo(force = false, connect = true) {
    const tab = current.value;
    // 只读模式不连 Claude：面板只显示会话记录里有的东西，这里一次都不问
    // 新会话标签页还没有会话，没什么可问的
    if (readonly.value || !tab || tab.fresh || (info.data && info.tab === tab.id && !force)) return;
    if (!connect) {   // 没连着服务器回 409，悬停时不算错误，不用提示
      try { applyInfo(tab, await api(`/api/tabs/${tab.id}/info?connect=0`)); } catch {}
      return;
    }
    Object.assign(info, { loading: true, error: '', tab: tab.id });
    try {
      applyInfo(tab, await api(`/api/tabs/${tab.id}/info`));
    } catch (e) { info.error = e.message; }
    info.loading = false;
  }
  function openInfo() {
    info.open = true;
    loadInfo(true);
  }
  async function setModel(value) {
    const tab = current.value;
    if (!tab || modelBusy.value) return;
    if (tab.fresh) return (tab.webModel = value);   // 新会话标签页：先记着，建会话时一起交给服务器
    modelBusy.value = true;
    try {
      Object.assign(tab, await api(`/api/tabs/${tab.id}/model`, { method: 'POST', body: { model: value } }));
      if (info.data) info.data.model = value;
      modelError.value = '';
    } catch (e) {
      modelError.value = e.message;   // 面板里留着，面板收起了也有底部的提示条
      fail('切换模型', e);
    }
    modelBusy.value = false;
  }

  // 换思考强度（null = 不指定，按 Claude Code 自己的设置）。CLI 连着时服务器替用户发一句 /effort，Claude 正在回复也能换，
  // 排到这一轮结束才执行；没连着的下次发话带着新强度续接。用上之前面板上灰着、写明什么时候生效（effortOf 的 pending）
  const effortError = ref(''), effortBusy = ref(false);
  async function setEffort(effort) {
    const tab = current.value;
    if (!tab || effortBusy.value) return;
    if (tab.fresh) return (tab.webEffort = effort);   // 新会话标签页：先记着，建会话时一起交给服务器
    if ((tab.effort || null) === effort) return;
    effortBusy.value = true;
    try {
      Object.assign(tab, await withTab(tab, () => api(`/api/tabs/${tab.id}/effort`, { method: 'POST', body: { effort } })));
      effortError.value = '';
    } catch (e) {
      effortError.value = e.message;
      fail('切换思考强度', e);
    }
    effortBusy.value = false;
  }

  // 切换中看板上的小标签直接写「切换中」；出错除了面板里那行，底部也说一声（悬停面板一收起就看不见了）
  const modeError = ref(''), modeBusy = ref(false);
  async function setMode(mode) {
    const tab = current.value;
    if (!tab || mode === tab.webMode || modeBusy.value) return;
    if (mode === 'bypassPermissions' && !confirm('全部放行后，Claude 可不经确认执行任何命令、修改任何文件。确定开启吗？')) return;
    if (tab.fresh) return (tab.webMode = mode);   // 新会话标签页：先记着，建会话时一起交给服务器
    modeBusy.value = true;
    try {
      Object.assign(tab, await withTab(tab, () => api(`/api/tabs/${tab.id}/mode`, { method: 'POST', body: { mode } })));
      modeError.value = '';
    } catch (e) {
      modeError.value = e.message;
      fail('切换权限模式', e);
    }
    modeBusy.value = false;
  }

  // 换账号：服务器会收掉连着的 CLI，下次发话用新号续接；信息面板里的账号、模型列表都跟着作废
  const accountError = ref(''), accountBusy = ref(false);
  async function setAccount(account) {
    const tab = current.value;
    if (!tab || account === tab.account || accountBusy.value) return;
    if (tab.fresh) return (tab.account = account);   // 新会话标签页：空的就是按设置走，建会话时一起交给服务器
    accountBusy.value = true;
    try {
      Object.assign(tab, await withTab(tab, () => api(`/api/tabs/${tab.id}/account`, { method: 'POST', body: { account } })));
      accountError.value = '';
      if (info.tab === tab.id) Object.assign(info, { data: null, tab: '' });
    } catch (e) {
      accountError.value = e.message;
      fail('切换账号', e);
    }
    accountBusy.value = false;
  }

  // 状态那一行：卡在链路的哪一段、在等什么、等了多久（写法见 phase.js，每条只说一件事）。
  // 服务器只在阶段变了时才推，秒数由前端每秒往上数
  const tick = ref(Date.now());
  setInterval(() => (tick.value = Date.now()), 1000);
  // 上游多久没下发数据不拿来报慢、报卡：第一次握手本来就可能久，不写正文和思考直接写工具调用时，
  // 参数又不是流式下发的，长时间没新数据都是常态
  const secs = s => (s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`);
  // 这个会话里回话的是谁：挂了人设卡是卡名（新会话标签页是下拉里选中的那张），没挂时 GPT 号是 GPT、DeepSeek 号是 DeepSeek，都不是就是 fallback
  const aiName = (tab, fallback = 'Claude') => personaName(tab) || ({ gpt: 'GPT', deepseek: 'DeepSeek', glm: 'GLM' }[familyOf(tab)] || fallback);
  // 简洁模式新会话页输入框里的提示：「向助手提问」「向 GPT 提问」，名字是拉丁字母开头的和「向」之间空一格
  const askHint = tab => { const n = aiName(tab); return `向${/^[\x00-\x7f]/.test(n) ? ' ' : ''}${n}${/^[\x00-\x7f]/.test(n) ? ' ' : ''}提问`; };
  // 新会话页的问候：按此刻的时段问好，后面接用户名；游客（没有名字）和单用户实例默认的「用户」只问好、不写对象
  function greet(name) {
    const h = new Date(tick.value).getHours();
    const part = h >= 5 && h < 11 ? '早上好' : h >= 11 && h < 13 ? '中午好' : h >= 13 && h < 18 ? '下午好' : '晚上好';
    return (name && name !== '用户' && name !== '游客' ? `${part}，${name}` : part) + '~';
  }
  function workLine(tab) {
    const w = workState(tab);
    // 换句时上下滚动（index.html 的 work-roll）认的是段名和那句话；秒数、token 数、重试第几次这些数字变了不算换句，不然每秒滚一次
    w.roll = `${w.src}|${w.text.replace(/\d+/g, '#')}`;
    return w;
  }
  // 状态那句话放不下时末尾是省略号：有鼠标的设备悬停弹出全文卡片，点一下固定住、再点收起（这一轮结束时 tabs.js 收掉）。
  // 放得下就不弹，免得同一句话再摆一遍；手机不认悬停（它会把悬停粘在点过的地方），只靠点
  const clipped = el => { const t = el.querySelector('.work-say:last-child .work-text'); return !!t && t.scrollWidth > t.clientWidth; };
  function workTipHover(tab, e, on) {
    if (!matchMedia('(hover: hover)').matches) return;
    tab.workHover = on && clipped(e.currentTarget);
  }
  function workTipPin(tab, e) {
    if (tab.workPin) Object.assign(tab, { workPin: false, workHover: false });
    else if (clipped(e.currentTarget)) tab.workPin = true;
  }
  function workState(tab) {
    const now = tick.value, silent = Math.round(pushQuiet() / 1000);
    const p = tab.stopping ? { kind: 'stopping', age: 0 } : tab.phase;
    // 秒数：阶段从收到推送那刻接着数；「已请求打断」从点打断那刻数，不接着算原来那一步已经过了多久
    const gone = Math.max(0, (now - ((tab.stopping && tab.stopT) || tab.phaseAt || now)) / 1000);
    // 主语：挂了人设卡写卡名，没挂按号写，GPT 号写 GPT（和气泡上的标签一致）
    const w = phaseLine(p, aiName(tab, ''));
    // 进度条按真实阶段画：点了打断也停在原来那格
    const step = phaseStep(tab.phase);
    const out = { ...w, cls: SRC_CLASS[w.src] || '', sec: p ? secs(Math.round(p.age + gone)) : '',
                  tip: p?.why || p?.error || '', step };
    // 推送断了：这段时间真正卡在哪一步看不到了，写明最后知道的是哪一步，不盖掉它；进度条停在最后所知那格、标红
    if (silent >= 20) {
      return { src: '网络', cls: SRC_CLASS.网络, warn: true, sec: '', tip: out.tip, step: { ...step, lost: true },
               text: `${silent} 秒未收到推送，正在重新连接；最后状态：${w.src} · ${w.text}` };
    }
    // 两个心跳间隔都没收到任何消息：查看器卡住了或者网络不好，原来那句的秒数已经不可信了，标黄写明（旧后端不说间隔就不标）
    const every = pushEvery();
    if (every && silent >= 2 * every) {
      return { src: '网络', cls: SRC_CLASS.网络, warn: true, sec: '', tip: out.tip, step: { ...step, warn: true },
               text: `${silent} 秒未收到查看器消息；最后状态：${w.src} · ${w.text}` };
    }
    return out;
  }

  // 缓存失效提醒：缓存一小时没用就失效，下一次请求要把整段上下文重新写入缓存，比接着缓存贵得多。
  // 离这个会话上一次请求（主会话的，不算子代理）超过一小时，输入框里最上面一行常驻的小字（淡红药丸），写明重新写入大约要花几分：
  // 当前上下文 × 这个模型的缓存写入单价（收钱用的，和气泡上的分一样），这一轮要输出多少事先不知道，没算进去。
  // 正在跑的不提醒（请求刚发出去）；时间到了跟着每秒的 tick 自己冒出来。预测拦截那边同样按一小时算（reserve.next_cost）
  // DeepSeek 号不提醒（它的缓存存活时间很长）：官方写缓存不再使用后「一般为几个小时到几天」才清空，
  // 没有一个说得准的时限，一小时就说失效是错的；预测拦截那边同样不按一小时算它（reserve.next_cost）
  // 选中的号和上一次回复的号不一样，也同样提醒：缓存跟着号走，换了号下一次请求照样整段重新写入，DeepSeek 号也一样。
  // 上一次回复是哪个号发的查不到（没有记录）就不说换了号，「取不到」不当成「不一样」。
  // 写成三段（lead、mark、tail）再接金额：输入框里显示时 mark 和金额用计费的红色（index.html 的 .cache-hint）
  const CACHE_TTL = 3600 * 1000;
  function cacheNote(tab) {
    if (tab.fresh || tab.running) return null;
    let last = null;
    for (let i = (tab.items?.length || 0) - 1; i >= 0; i--) {
      const it = tab.items[i];
      if (it.use && it.ts && !it.sidechain) { last = it; break; }
    }
    if (!last) return null;
    const was = accountOf(last.mid), moved = !!(was && tab.account && was !== tab.account);
    const ttl = CACHE_TTL;
    const expired = familyOf(tab) !== 'deepseek' && tick.value - Date.parse(last.ts) >= ttl;
    if (!moved && !expired) return null;
    const total = tab.ctx?.total || tab.context || 0;
    if (!total) return null;
    const p = callPoints({ in: 0, cw: total, cr: 0, out: 0,
                           cw1h: relayOf(tab) ? total : 0 }, tab.webModel || last.model, tab.account);   // 中转站缓存默认一小时
    const words = moved ? { lead: '已更换账号，缓存', mark: '不可复用', tail: '，重新写入约需' }
      : { lead: '缓存', mark: '已过期', tail: `（${ttl === CACHE_TTL ? '1 小时' : '5 分钟'}），重新写入约需` };
    return { ...words, cost: p == null ? '' : fen(p) };
  }

  // 输入框里打「/」开头、还在打指令名（没打空格）时，输入框上方长出一张灰色卡片，列出以它开头的可用指令。
  // 清单问服务器（/api/commands：这个会话的 CLI 连着就用它的，否则用最近一次见到的），每个标签页取一次；点一项、或者按 Tab 填第一项，
  // 填成「/指令 」接着打参数。取不到（比如后端还没更新到有这个接口的版本）就在卡片里写明原因
  async function loadCmds(tab) {
    if (tab.cmds || tab.cmdsBusy) return;
    tab.cmdsBusy = true;
    try {
      tab.cmds = await api(`/api/commands${tab.id && !tab.fresh ? `?tab=${encodeURIComponent(tab.id)}` : ''}`);
    } catch (e) {
      tab.cmds = { commands: [], why: `指令列表加载失败：${e.message || e}` };
    }
    tab.cmdsBusy = false;
  }
  watch(() => current.value?.draft, d => { if (d?.startsWith('/') && current.value) loadCmds(current.value); });
  function cmdMenu(tab) {
    const m = /^\/(\S*)$/.exec(tab?.draft || '');
    if (!m) return null;
    if (!tab.cmds) return { items: [], why: '正在加载指令…' };
    const q = m[1].toLowerCase(), all = tab.cmds.commands || [];
    const items = all.filter(c => c.name.toLowerCase().startsWith(q)).sort((a, b) => (b.name.toLowerCase() === q) - (a.name.toLowerCase() === q));
    return { items, why: items.length ? '' : tab.cmds.why || `没有以「/${m[1]}」开头的指令` };
  }
  function pickCmd(tab, c) {
    tab.draft = `/${c.name} `;
    nextTick(() => { composerInput.value?.focus(); autoGrow(); });
  }
  // 卡片长高的动画：每次画出来、内容变了（清单取回来、打字筛掉几项）都量一下实际高度，从上次的高度（刚画出来时是输入框半高，
  // 整张藏在输入框后面）动画过去。以前用 CSS 把 max-height 从半高放到限高，卡片实际矮得多，几十毫秒就放过了它的高度，看不出在长
  // （等于没有动画）。挂在 @vue:mounted / @vue:updated 上，根组件每秒重画一次也会来，所以内容（字）没变就什么都不做；
  // 动画半路内容又变了，从眼下的高度接着过去（先停掉旧动画才量得到新的实际高度）。
  // 藏起来的那一帧（cardHidden）连上下内边距一起收掉：卡片最矮只能矮到上下内边距之和（比输入框半高高出十几像素），
  // 只收高度的话开头一下冒出、结尾一下没了一截（量过）
  const cardHidden = el => {
    const half = (parseFloat(getComputedStyle(el).getPropertyValue('--composer-h')) || 56) / 2;
    return { boxSizing: 'border-box', height: `${half}px`, paddingTop: '0px', paddingBottom: `${half}px` };
  };
  const cardNow = el => {
    const cs = getComputedStyle(el);
    return { boxSizing: 'border-box', height: `${el.offsetHeight}px`, paddingTop: cs.paddingTop, paddingBottom: cs.paddingBottom };
  };
  function cardGrow(vnode) {
    const el = vnode.el;
    if (!el?.isConnected || el.textContent === el._sig) return;
    el._sig = el.textContent;
    const from = el._grow?.playState === 'running' ? cardNow(el) : el._grown ?? cardHidden(el);
    el._grow?.cancel();
    el.style.overflowY = '';
    const to = el._grown = cardNow(el);
    if (from.height === to.height || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    // 长的途中卡片比内容矮，overflow-y: auto 会冒出几百毫秒的滚动条，动画期间先不让它滚
    el.style.overflowY = 'hidden';
    el._grow = el.animate([from, to], { duration: 260, easing: 'cubic-bezier(0.2, 0.9, 0.3, 1)' });
    el._grow.onfinish = () => { el.style.overflowY = ''; };
  }
  // 收起：打掉「/」、选中一项时卡片要消失，v-if 直接删掉就是一下没了，没有收起动画。
  // 挂在 <transition> 的 leave 上：从眼下的样子（长到一半也接得上）缩回藏起来的那一帧、整张躲回输入框后面，缩完才删；缩的途中不接点击
  function cardShrink(el, done) {
    const from = cardNow(el);
    el._grow?.cancel();
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return done();
    el.style.overflowY = 'hidden';
    el.style.pointerEvents = 'none';
    el.animate([from, cardHidden(el)], { duration: 260, easing: 'cubic-bezier(0.2, 0.9, 0.3, 1)', fill: 'forwards' }).onfinish = done;
  }
  function onCmdTab(e) {
    const tab = current.value, first = tab && cmdMenu(tab)?.items[0];
    if (!first) return;
    e.preventDefault();
    pickCmd(tab, first);
  }

  // 这一轮结束了、后台还挂着任务：一直显示着，免得以为什么都没在跑。任务结束时 Claude 会自己开一轮接着说
  function bgLine(tab) {
    const gone = Math.max(0, (tick.value - (tab.bgAt || tick.value)) / 1000);
    return (tab.bg || []).map(t => `${t.desc || '后台任务'} · 已运行 ${secs(Math.round(t.age + gone))}`).join('；');
  }

  // Claude 约好了过一会儿自己接着干（ScheduleWakeup、定时任务）：写明几点、还有多久，在那之前 CLI 不会被空闲回收
  function wakeLine(tab) {
    const left = Math.max(0, Math.round(tab.wake - tick.value / 1000));
    return `Claude 将于 ${clock(tab.wake * 1000)} 继续（剩余 ${secs(left)}）`;
  }

  // 额度恢复后自动继续（服务器的 auto_go_state）：因为额度停着、或者开关开着时，对话末尾一行带开关，写明打算几点发、为什么还没发；关着时只有开关
  function autoGoLine(tab) {
    const a = tab.autoGo;
    if (!a.on) return '';
    if (!a.stop) return '已开启：会话因额度暂停时，额度恢复后将自动发送「继续」';
    if (a.sent) return `额度已恢复，已于 ${clock(a.sent * 1000)} 自动发送「继续」`;
    if (a.why) return a.why;
    return a.due ? `额度预计 ${clock(a.due * 1000)} 恢复，恢复一分钟后将自动发送「继续」` : '正在查询额度恢复时间';
  }
  async function setAutoGo(tab, on) {
    if (tab.autoGoBusy) return;
    tab.autoGoBusy = true;
    try {
      Object.assign(tab, await withTab(tab, () => api(`/api/tabs/${tab.id}/autogo`, { method: 'POST', body: { on } })));
    } catch (e) { fail(on ? '开启额度恢复后自动继续' : '关闭额度恢复后自动继续', e); }
    tab.autoGoBusy = false;
  }

  return {
    workLine, workTipHover, workTipPin, askHint, greet, bgLine, wakeLine, autoGoLine, setAutoGo, SRC_TIPS, STEPS, cacheNote, cmdMenu, pickCmd, onCmdTab, cardGrow, cardShrink,
    composerInput, autoGrow, onEnter, sendChat, stageChat, unstage, retrySend, restoreFailed, stopChat, quote, refill, onPaste, onDropFiles, onPickFiles, onPickAny, pickUpload, dropFile, fileSize,
    ctx, reqWarn,
    info, openInfo, loadInfo, modelList, claudeModels, isModel, modelBusy, modelError, setModel, effortBusy, effortError, setEffort, runModeOf,
    MODE_TIPS, modeError, modeBusy, setMode, accountError, accountBusy, setAccount,
    composerHint: touch ? '输入消息…' : 'Enter 发送，Shift+Enter 换行，/ 输入指令',
  };
}
