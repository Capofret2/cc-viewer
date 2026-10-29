// 标签与推送：一个标签就是一个会话文件，自己带着条目、草稿、滚动容器和渲染缓存
const { ref, reactive, computed, watch, nextTick } = Vue;
import { api, store, say, fail, toast, me, fileName } from './util.js';
import { randomKao } from './kao.js';
import { setCallAccounts, setCallCovered, setCallFree, setCallPrices } from './points.js';
import { foldPrefixes } from './item-row.js';
import { phaseLine } from './phase.js';

// focus：有没有人正等着它（用户正在看、正在改名这些）。服务器先解析这种，后台恢复的排队；旧服务器不认这个字段，照常打开。
// 只有恢复队伍里排着的后台标签页交 false，和切不切过去没有关系
export const openOnServer = (path, focus = true) => api('/api/tabs', { method: 'POST', body: { path, focus } });

// 一次只开一个会话时（没开底部标签页），换走或刷新页面后这个标签页还留不留：
// 还没说第一句话的新会话标签页里写了东西、或者正在建会话的留着，别的收掉（app.js 换会话时、load 恢复时）
export const stays = tab => !!(tab.fresh && (tab.busy || tab.draft?.trim() || tab.attach?.length || tab.files?.length));

// 碰到 404 时怎么重新打开：useTabs 起来以后换成它的恢复队伍（同一个标签页只开一次、按先后排，见 reopen），
// 编号跟着服务器给的改（刷新页面时还没开好的标签页先用的是占位编号）
let reopenVia = tab => openOnServer(tab.path).then(brief => Object.assign(tab, brief));

// 服务器在后台核对额度时把这句话拦下了（推送或发话的回复里带着 rejected，nonce 是发话时起的编号）：
// 撤掉它的「发送中」气泡，把这句话连同图片、文件留着，旁边给个重试按钮。同一次退回只认一回
export function takeRejected(tab, rejected) {
  const sent = rejected && tab.inflight?.[rejected.nonce];
  if (!sent) return;
  delete tab.inflight[rejected.nonce];
  tab.pending = tab.pending.filter(x => !sent.ids.includes(x.id));
  tab.failed = { text: sent.text, images: sent.images, files: sent.files, error: rejected.why };
}

// 当前的号被预测拦截拦下、服务器自动换了个号接着发（server.hand_over、Birth 的 switched）：提示一行，同一次只提示一回。
// 上下文压缩后的第一轮重新挑了号（kind 是 compact，server.compact_pick）也走这里，换个说法。
// 刷新页面后快照里还带着上一次的，过了两分钟的就不再提
export function noteSwitch(tab, sw) {
  if (!sw || sw.n === tab.switchSeen) return;
  tab.switchSeen = sw.n;
  // 压缩后重新挑号和额度不够时切号，对用户都是自动切换了账号，不分开说
  if (Date.now() / 1000 - sw.t < 120) say(`已自动切换账号：${sw.from} → ${sw.to}`);
}

// 服务器重启或太久没人看时会释放会话，碰到 404 就重新打开一次再试（fn 里的地址要现拼，重新打开后编号可能变了）
export async function withTab(tab, fn) {
  try { return await fn(); } catch (e) {
    if (e.status !== 404) throw e;
    await reopenVia(tab);
    return fn();
  }
}

// 思考不归显示开关管：有明文的（API 号）在气泡头部的「思考」药丸里点开，订阅号的思考没有内容，只写跳数
const HIDDEN = { tool: ['tool'], system: ['system', 'duration', 'error', 'task'], attachment: ['attachment', 'meta'] };

// 一次 API 请求画成一个气泡。同一个回复编号挨着的正文、工具调用收在一起，头部写这次请求的
// 点数和四个数，前面是思考跳数（只有网页对话记得到，见 server.record_think）。工具结果和被显示开关藏起来的条目不打断，
// 用户说的话、系统消息这些显示出来的条目会打断。空的思考块（CC 不写思考内容）不画，但只有空思考的请求也照样有个
// 只带头部的气泡，不然它的点数就看不到了。同一个编号被打断后又出现（少见），后面那个气泡标成 cont，不再重复写点数。
// 没变的请求沿用上次的气泡对象：不然每来一条新内容，所有气泡都会被当成新的整个重画一遍（工具结果到了也算变）
const callCaches = new WeakMap();
const CALL_KINDS = new Set(['text', 'thinking', 'tool']);
function calls(tab, visible, hiddenKey) {
  const old = callCaches.get(tab) || new Map(), next = new Map(), out = [], seen = new Set();
  let cur = null;
  const flush = () => {
    if (!cur) return;
    const all = cur, first = all[0], last = all[all.length - 1];
    const parts = all.filter(i => visible(i) && !(i.kind === 'thinking' && !i.text?.trim()));
    const done = parts.filter(i => i.kind === 'tool' && tab.results[i.id]).length;
    // 每次推送带来的都是新数组，按内容比
    const think = tab.think?.[first.mid] || null, thinkKey = think ? think.join() : '';
    const cont = seen.has(first.mid);
    seen.add(first.mid);
    const hit = old.get(first.seq);
    const row = hit && hit.all.length === all.length && hit.all[all.length - 1] === last && hit.done === done
      && hit.hiddenKey === hiddenKey && hit.thinkKey === thinkKey && hit.cont === cont ? hit : Object.freeze({
        kind: 'call', seq: first.seq, ts: first.ts, mid: first.mid, model: first.model, effort: first.effort, sidechain: first.sidechain,
        // 同一次调用的几个条目各带一份用量，靠前的可能是流式写到一半的中间数：各项取最大
        use: all.reduce((u, i) => (i.use ? Object.fromEntries(Object.keys(u).map(k => [k, Math.max(u[k], i.use[k] || 0)])) : u),
                        { in: 0, cw: 0, cr: 0, out: 0, cw1h: 0 }),
        parts, think, thinkKey, cont, all, done, hiddenKey,
        // DeepSeek 的思考是明文，服务器给每块估了多少 token（session.py），气泡头部写「思考 3.2k」代替思考跳数
        thinkTok: all.reduce((s, i) => s + (i.kind === 'thinking' && i.tokens || 0), 0),
        text: all.filter(i => i.kind === 'text').map(i => i.text).join('\n\n'),   // 复制正文、刻度尺预览用
      });
    next.set(row.seq, row);
    out.push(row);
    cur = null;
  };
  for (const it of tab.items) {
    // 重复追加的条目（dup）整个跳过：请求气泡不看 visible，只靠它挡不住，会多出一串只有头部的空气泡
    if (it.kind === 'result' || it.dup) continue;
    if (CALL_KINDS.has(it.kind) && it.mid) {
      if (cur && cur[0].mid === it.mid) cur.push(it);
      else { flush(); cur = [it]; }
    } else if (visible(it)) {
      flush();
      out.push(it);
    }
  }
  flush();
  callCaches.set(tab, next);
  return out;
}

const TAIL = 300;   // 第一次打开只画末尾这些条：几千条的长会话首屏才不会卡着等
const STEP = 200;   // 往上翻到头时，每次再补这些条
// 手上最多留多少条（取回来的只增不减的话，往上翻到头就是整个会话都画着，手机上几千行、九万多个节点滚不动）：
// 往上补、往下接以后超过 KEEP + STEP 条，就把离视口远的那一头卸到 KEEP 条（见 trim），要看时再按需取回来
const KEEP = 500;

// 给条目编上全局序号：往前面补内容时，后面这些条的 key 不会跟着错位，
// 不然 Vue 会认为整个列表都换了人，几百上千行白白重画一遍
function stamp(items, start, tid) {
  return items.map((it, k) => {
    it.seq = start + k;
    // 图片在服务器那边放着，条目里只有个编号，这里补成地址，要看到了才去取
    if (it.image && !String(it.image).startsWith('data:')) it.image = `/api/tabs/${tid}/image/${it.image}`;
    // 工具结果里的图同理，地址只是备着：用户点开那一张才去取（tool-card.js 的 ResultShots）
    if (it.images) it.images = it.images.map(im => ({ ...im, url: im.key ? `/api/tabs/${tid}/image/${im.key}` : null }));
    return Object.freeze(it);
  });
}

// onSettings：服务器推来全局设置（别的设备改了），交给会话模块（在 app.js 里接上）。
// single：此刻是不是一次只开一个会话（没开底部标签页）
// keep：一次只开一个会话时（single）后台留着最近打开的几个（含正在看的，app.js 的 keepN），超出的由 app.js 收掉
export function useTabs({ show, onSettings, single = () => false, keep = () => 1 }) {
  const tabs = ref([]);
  const active = ref('');   // 停在哪个标签页也按身份记，知道是谁以后在 load 里取
  const current = computed(() => tabs.value.find(t => t.id === active.value));
  // 当前标签的滚动容器：元素挂在标签自己身上，切换标签自然就跟着换
  const scroller = computed(() => current.value?.el || null);
  const rows = computed(() => current.value?.rows || []);

  // 一个标签自带的全部状态：
  // items/gen/unread 是会话内容，pending 是刚发出还没写进记录的话，
  // draft 是这个标签的草稿，el 是它的滚动容器，shown 记着有没有显示过（决定要不要先跳到底），
  // rows/results 是只跟着自己的内容和显示开关重算的渲染缓存 —— 别的标签有新内容不会连累它。
  // 全挂在标签上，关标签时跟着一起没了，不用逐张表去清
  function makeTab(brief) {
    const tab = reactive({
      ...brief,
      items: [], gen: '', unread: 0, pending: [],
      // offset：手上这批条目之前还有多少条没取（0 就是从头都在）；earlier：正在往上补。
      // cut：手上这批的底下卸掉过、没到会话末尾（往上翻远了），这时 total 才是末尾在哪；later：正在往下接。
      // shift：刚换过、卸过手上这批（不是新消息），刻度尺那边别把它当新内容去跟到底、做淡入（minimap.js）
      offset: 0, earlier: false, cut: false, later: false, shift: false,
      failed: null,   // 上一句没发出去的话，旁边会给个重试按钮
      inflight: {},   // 已经交给查看器、还可能在后台被退回的话：编号 → { text, images, files, ids 气泡 }（见 takeRejected）
      loadError: '',  // 内容没取到（或推送来的没接上、补拉又失败）的原因，对话区底部显示并给个重试
      loading: !!brief.loading,   // 在恢复队伍里排着或正在开（见 reopen），对话区转圈写「正在加载」
      sending: false, staging: false, stopping: false,   // 正在发话 / 正在存待发的话 / 已经请求打断、等它停下
      attach: [],     // 待发出去的图片（粘贴或拖进来的）
      files: [],      // 回形针传的文件：{ name, size, loaded, path（传好了才有）, error, xhr }，发话时告诉 Claude 放在哪
      draft: '', el: null, shown: false,
      staged: brief.staged || [],   // 待发的话（存在服务器上，点发送时一起发）
      // 对话行：一次 API 请求一个气泡，再认出每一轮回复、收起工作过程
      rows: computed(() => {
        // restart：实例重启后查看器发给 AI 的提示，用户看不到，只当分界（见下面的 cuts）
        const hiddenKinds = ['result', 'restart', ...Object.keys(HIDDEN).filter(k => !show[k]).flatMap(k => HIDDEN[k])];
        const hidden = new Set(hiddenKinds);
        // dup：CLI 把旧记录原样又追加了一遍生成的条目（session.py 的 Session.dup），原件已经画过，不再画一遍
        const visible = i => !hidden.has(i.kind) && !i.dup;
        // 排好以后认出每一轮回复，过程长的加标题栏、停下来的收起过程（item-row.js）。
        // 开新一轮的后台通知（Claude 空闲时到的）、loop 和定时任务到点时 CLI 塞进来的那句（meta，见 session.on_user）也是一轮的分界，不管它显不显示都要算上
        const cuts = tab.items.filter(i => (i.kind === 'task' || i.kind === 'restart' || i.kind === 'meta') && i.turn && !i.dup).map(i => i.seq);
        // 发话那一刻 running 就置上了，这句话却要等记录里出现才成为分界：挂着「发送中」气泡（不是插话）时，
        // 末尾那段是已经结束的上一轮，照样自动收起，不然气泡浅着的那一会儿上一轮的过程会摊开、发出去又收回，一闪一闪
        // 手上这批没到末尾（cut）时，最后一段不是正在写的那一轮，照常收起
        const live = tab.running && !tab.cut && !tab.pending.some(p => !p.mid);
        return foldPrefixes(calls(tab, visible, hiddenKinds.join()), live, cuts);
      }),
      results: computed(() =>
        Object.fromEntries(tab.items.filter(i => i.kind === 'result').map(i => [i.id, i]))),
    });
    return tab;
  }
  // 内容区挂上来了：要是之前看过、切走后被释放（超出常驻个数）又重画的，回到上次看到的位置
  // （高度要等几帧才定下来，多扶两次）
  const setLog = (tab, el) => {
    const fresh = el && el !== tab.el;
    tab.el = el || null;
    if (fresh && tab.shown && tab.lastTop != null) {
      const top = tab.lastTop;
      nextTick(() => requestAnimationFrame(() => {
        el.scrollTop = top;
        requestAnimationFrame(() => (el.scrollTop = top));
      }));
    }
  };

  // 按起始位置对齐接上，拉取和推送同时到达时也不会重复。
  // 位置都是「全局第几条」，手上这批从 tab.offset 开始
  function apply(tab, { items, start, tablesDelta, ...meta }) {
    const had = tab.offset + tab.items.length, was = tipOf(tab);
    // 推送里那几张表只带了变了的条目（订阅时说了 delta=1，服务器 publish 才这么发）：思考跳数表并进已有的，不能整份换掉；
    // 回复对照表、预定付费表、特惠免掉表本来就是并进全局的（setCallAccounts / setCallCovered / setCallFree），不用另管。整段拉取给的是整份，照旧换
    if (tablesDelta && meta.think) meta.think = { ...(tab.think || {}), ...meta.think };
    const regen = 'gen' in meta && meta.gen !== tab.gen;
    if (tab.cut && start >= had && !regen) {
      // 手上这批没到末尾（往上翻远了，底下卸掉了）：新来的离视口远着，不接，只记总数和状态（下面 meta 里的 total），回到底部时再取末尾
    } else if (start > had || start < tab.offset || regen) {
      // 接不上（换代了，或者服务器只给了末尾）：整批换掉。换了代的条目编号是另一套，就算位置落在手上这批里面也不能接在旧的后面
      tab.offset = start;
      tab.items = stamp(items, start, tab.id);
      tab.cut = false;
    } else {
      tab.items = tab.items.slice(0, start - tab.offset).concat(stamp(items, start, tab.id));
      tab.cut = false;
    }
    Object.assign(tab, meta);
    takeRejected(tab, meta.rejected);
    noteSwitch(tab, meta.switched);
    setCallAccounts(meta.calls);   // 每条回复是哪个号发的：详情里写号名，限量号的点数换成对位色
    setCallPrices(meta.callPrices); // 预估随后由平台实扣替换，包含真实零扣费。
    setCallCovered(meta.covered);  // 由预定付了的回复各有几成：那一份点数写成蓝的
    setCallFree(meta.free);        // 特惠结算时记下的免了几成：那一份写成主题色
    if ('phase' in meta) tab.phaseAt = Date.now();   // 阶段里的秒数是服务器发出时的，从收到这刻起前端接着数
    if ('bg' in meta) tab.bgAt = Date.now();          // 后台任务跑了多久，同样从收到这刻起接着数
    // 打断生效了（或者这一轮自己结束了）。打断后 CLI 会马上拿排着的插话开新的一轮，「不在跑」只有几十毫秒、推送赶不上，
    // 所以结果消息数（results）比点打断时多了，也算被打断的那一轮已经停了
    if (!tab.running || (tab.stopping && tab.results > tab.stopAt)) tab.stopping = false;
    // 状态那句话的全文卡片固定住了（chat.js 的 workTipPin）：这一轮结束就收掉，下一轮不带着
    if (!tab.running) tab.workPin = tab.workHover = false;
    // 记录里出现了同样的话，或者这一轮已经结束了，发送中的气泡就撤掉
    if (tab.pending.length) {
      tab.pending = tab.pending.filter(p => (tab.running || !p.sent) &&
        !tab.items.slice(Math.max(0, p.at - tab.offset)).some(
          i => i.kind === 'user' && !i.dup && i.text.trim() === p.text));
    }
    const added = tipOf(tab) - was;
    if (added > 0 && tab.id !== active.value) tab.unread += added;
  }
  // 会话末尾在第几条：手上这批到了末尾就是它的结尾，没到（cut）就是服务器说的总数。推送接着它往后推，发话也从它往后找自己那句
  function tipOf(tab) {
    return tab.cut ? tab.total : tab.offset + tab.items.length;
  }
  // 失败时把原因记在标签页上（对话区底部显示、带重试），再往外抛；只想补拉不想处理错误的调用方 catch 掉就行，不会静默。
  // 同一个标签页正在补拉时又要补拉（重连、推送接不上、重新打开几处同时来）：不并发再发，等这次完了再补一次，
  // 几个人一起等的也只补这一次（以前服务器重启后同一个网页对同一个标签页要取四五遍）
  const syncs = new WeakMap();   // tab → { p 正在补拉的这次, next 完了以后再补的那次 }
  function sync(tab) {
    if (tab.fresh) return Promise.resolve();   // 新会话标签页还没有会话可取
    // 还在恢复队伍里（刷新页面时的占位编号服务器不认识，取了也是 404）：等它开好再取
    const job = jobs.get(tab);
    if (job) return job.p.then(() => sync(tab));
    const cur = syncs.get(tab);
    if (cur) return (cur.next ||= cur.p.catch(() => {}).then(() => sync(tab)));
    const p = (async () => {
      try {
        // 手上这批没到末尾（cut）时只问末尾之后有没有新的，取回来也不接（apply），不然会把中间几千条一下全取回来
        const r = await withTab(tab, () =>
          api(`/api/tabs/${tab.id}/items?since=${tipOf(tab)}&gen=${tab.gen}&tail=${TAIL}`));
        // 这份是服务器推第 r.v 次以前取的，等它回来的时候更新的推送已经到了、接上了：
        // 拿它覆盖会把推来的新条目截掉、把状态改回去，服务器认为都推过了不会再推，要刷新才看得到。同一代里比它旧就不用
        if (r.gen === tab.gen && r.v != null && tab.v != null && r.v < tab.v) {
          gapNote(tab, { kind: 'stale', have: tab.v, want: r.v });
        } else {
          apply(tab, r);
        }
        tab.loadError = '';
        tab.okEpoch = epoch;
      } catch (e) {
        tab.loadError = e.message;
        throw e;
      }
    })().finally(() => syncs.delete(tab));
    syncs.set(tab, { p, next: null });
    return p;
  }
  // 重新打开以后取一次内容：正在补拉的那次（多半就是碰到 404 等着重新打开的那次）开好以后会自己重取，不用再排一次
  const ensureSynced = tab => syncs.get(tab)?.p ?? sync(tab);
  const retrySync = tab => sync(tab).catch(() => {});   // 错误已经记在 tab.loadError 上了
  // 往上翻到头了：再补一段更早的接在前面。补到了返回 true，让调用方把滚动位置扶正
  async function loadEarlier(tab) {
    if (!tab || tab.earlier || !tab.offset) return false;
    tab.earlier = true;
    try {
      const at = tab.offset, from = Math.max(0, at - STEP);
      const r = await withTab(tab, () =>
        api(`/api/tabs/${tab.id}/items?start=${from}&end=${at}&gen=${tab.gen}`));
      // 换代了，等推送来重新对齐；等的时候手上这批整个换过了（回到底部、跳到某条），这段接不上了
      if (r.gen !== tab.gen || !r.items.length || tab.offset !== at) return false;
      tab.items = stamp(r.items, from, tab.id).concat(tab.items);
      tab.offset = from;
      return true;
    } catch (e) {
      fail('取更早的内容', e);   // 往上滚会再试
      return false;
    } finally {
      tab.earlier = false;
    }
  }
  // 往下翻到手上这批的底了、底下卸掉过（cut）：再接一段后面的。接到末尾就不再 cut，推送照常往后接。
  // 等的时候来了新推送只记了总数（apply），所以用两边总数里大的那个判断接没接到末尾
  async function loadLater(tab) {
    if (!tab || !tab.cut || tab.later) return false;
    tab.later = true;
    try {
      const at = tab.offset + tab.items.length;
      const r = await withTab(tab, () =>
        api(`/api/tabs/${tab.id}/items?start=${at}&end=${at + STEP}&gen=${tab.gen}`));
      if (r.gen !== tab.gen || !tab.cut || tab.offset + tab.items.length !== at) return false;
      tab.items = tab.items.concat(stamp(r.items, at, tab.id));
      tab.total = Math.max(tab.total, r.total);
      if (at + r.items.length >= tab.total) tab.cut = false;
      return r.items.length > 0;
    } catch (e) {
      fail('取后面的内容', e);   // 往下滚会再试
      return false;
    } finally {
      tab.later = false;
    }
  }
  // 手上这批太长了：卸掉离视口远的那一头，至少留 KEEP 条，卸了返回 true。往上翻时卸底下（end 是 'bottom'），卸完标 cut；
  // 往下翻时卸顶上，卸完 offset 跟着往后挪，顶上那行提示照样写还有几条。
  // seq 是刻度尺量好的边界（minimap.js 的 farSeq）：卸底下时这一条起往后都离视口够远，卸顶上时这一条之前的都够远；
  // 量不出（内容太矮、没有够远的）就是 null，不卸。只按条数卸的话，折叠后很矮的会话里往上补完卸底下、往下接完卸顶上，
  // 两头一直来回取（长会话上碰到过，回到顶部以后连第 0 条都被卸掉了）。
  // 卸下来不到一段（STEP）时不卸，免得在边上来回取、来回卸。还有发送中的话时不卸底下：那句话要在末尾等记录里出现才撤（apply 里的 pending）
  function trim(tab, end, seq) {
    if (!tab || seq == null || (end === 'bottom' && tab.pending.length)) return false;
    const at = tab.items.findIndex(i => i.seq >= seq);   // 边界上（或之后）的第一条
    if (end === 'bottom') {
      const keep = Math.max(KEEP, at);
      if (at < 0 || tab.items.length - keep < STEP) return false;
      tab.items = tab.items.slice(0, keep);
      tab.cut = true;
    } else {
      const drop = Math.min(at < 0 ? 0 : at, tab.items.length - KEEP);
      if (drop < STEP) return false;
      tab.items = tab.items.slice(drop);
      tab.offset += drop;
    }
    return true;
  }
  // 整批换成取回来的这一段（回到顶部、跳到某条）：没到末尾就标 cut
  function place(tab, r) {
    tab.offset = r.start;
    tab.items = stamp(r.items, r.start, tab.id);
    tab.total = Math.max(tab.total, r.total);
    tab.cut = r.start + r.items.length < tab.total;
  }

  // 回到顶部：换成会话开头那一段（以前是把上面没取的一次全取回来接在前面，几千条的会话一下全画出来）
  async function loadHead(tab) {
    if (!tab || tab.earlier || !tab.offset) return false;
    tab.earlier = true;
    try {
      const r = await withTab(tab, () =>
        api(`/api/tabs/${tab.id}/items?start=0&end=${TAIL}&gen=${tab.gen}`));
      if (r.gen !== tab.gen) return false;   // 换代了，等推送来重新对齐
      place(tab, r);
      return true;
    } catch (e) {
      fail('取开头的内容', e);
      return false;
    } finally {
      tab.earlier = false;
    }
  }
  // 回到底部、自己发话时手上这批不在末尾（cut）：换成末尾那一段，和第一次打开一样只取 TAIL 条（gen 不给，服务器就只给末尾），
  // 之后推送照常往后接。换成了返回 true
  async function loadTail(tab) {
    if (!tab?.cut || tab.later) return false;
    tab.later = true;
    try {
      const r = await withTab(tab, () => api(`/api/tabs/${tab.id}/items?since=0&gen=&tail=${TAIL}`));
      if (!tab.cut) return false;   // 等的时候已经接到末尾了
      tab.cut = false;
      apply(tab, r);
      return true;
    } catch (e) {
      fail('取最新的内容', e);
      return false;
    } finally {
      tab.later = false;
    }
  }

  // 跳到第几条（找话）：那条不在手上这批里，就从它前面一点开始换成一段（TAIL 条），不再一直取到末尾
  async function ensureLoaded(tab, seq) {
    if (!tab || (seq >= tab.offset && seq < tab.offset + tab.items.length)) return true;
    const from = Math.max(0, seq - 40);
    const r = await withTab(tab, () =>
      api(`/api/tabs/${tab.id}/items?start=${from}&end=${from + TAIL}&gen=${tab.gen}`));
    if (r.gen !== tab.gen) return false;
    place(tab, r);
    return true;
  }

  // 新会话标签页：还没有会话文件，发出第一句话前都能改工作区、标签和账号；刷新页面就没了，不记进浏览器。
  // 说出第一句话以后，服务器回一个编号（birth，见 server.Birth），这个标签页订阅它的推送：走到哪一步照常显示，
  // 建成了推来正式标签页的快照（promote），没建成推来原因；birthWait 是 sessions.js 的 createFromTab 在等的那个结果
  function newFresh(fields) {
    // envCreating：在新会话页上选「独立」、正在当场创建独立环境（sessions.js 的 pickEnvironment）
    const tab = makeTab({ id: `new-${Date.now()}`, path: '', title: '新会话', fresh: true, busy: false, envCreating: false,
                          birth: '', birthWait: null, kao: randomKao(), ...fields });
    tabs.value.push(tab);
    select(tab);
    return tab;
  }
  // 第一句话建出了会话：在原位置换成真正的标签页，发送中的气泡跟着带过去，等记录里出现这句话再撤
  async function promote(fresh, brief) {
    const i = tabs.value.indexOf(fresh);
    if (i < 0) return;   // 等的时候用户把它关了
    const tab = makeTab(brief);
    // 侧边栏的「最近」是一页页取的，没取到的不一定是新的：只有刚建成的才在列表还没重新取到时排到最上面（sessions.js 的 recentTabs）
    tab.bornAt = Date.now();
    tab.pending = fresh.pending.map(p => ({ ...p, at: 0, sent: true }));
    tabs.value.splice(i, 1, tab);
    if (active.value === fresh.id) select(tab);
    await sync(tab).catch(() => {});
  }
  // 新会话标签页订阅到的推送（见 newFresh）
  async function onBirth(fresh, msg) {
    const wait = fresh.birthWait;
    noteSwitch(fresh, msg.switched);
    if (msg.born) {
      fresh.birthWait = null;
      await promote(fresh, msg.born);
      return wait?.resolve();
    }
    if (msg.error || msg.gone) {
      fresh.birthWait = null;
      fresh.birth = '';   // 不再订阅它；重试时会建一个新的
      // 没建成：连同最后停在哪一段一起说，用户看得出是哪里坏了
      const last = msg.last ? phaseLine(msg.last) : null;
      const why = msg.gone ? '服务器已重启，无法确认新会话是否创建成功；请先在「最近会话」中查找，未找到再重新发送'
        : `${msg.error}${last ? `（最后状态：${last.src} · ${last.text}）` : ''}`;
      return wait?.reject(new Error(why));
    }
    Object.assign(fresh, { running: true, phase: msg.phase, phaseAt: Date.now() });
  }

  // 恢复队伍：刷新页面、服务器重启以后要重新打开的标签页都排在这里，一次只开一个：正在看的先开，
  // 其余按标签栏的顺序一个接一个开完。三条路都汇到这里——刷新页面恢复（load）、取内容碰到 404（withTab）、
  // 推送说服务器不认识了（msg.reopen）——排着或正在开的标签页不会再开第二遍。切到还排着的标签页，它马上单独开、
  // 告诉服务器这是正在看的（服务器那边正在看的直接解析，后台的排队，见 server.open_tab_async），不用等前面那个。
  // 推送不再每开好一个就重连：正在看的开好重连一次，队伍走空再重连一次（以前每开好一个都重连，重连又把所有标签页补拉一遍）
  const jobs = new Map();   // tab → { p, resolve, reject, started }
  let laneBusy = false, sinceConnect = 0, focusLive = 0;
  // 推送每连上一次（服务器可能重启过）、页面每加载一次算一轮；标签页在这一轮里取到过内容或重新打开过，就记下这一轮（okEpoch）。
  // 服务器重启后各标签页的 404、「请重新打开」是陆续到的，后台的可能先到：正在看的那个这一轮还没确认过，就先把它当正在看的开了，
  // 后台的等它开好再走
  let epoch = 0;
  function reopen(tab) {
    const hit = jobs.get(tab);
    if (hit) return hit.p;
    // 已经关掉的（比如开好才认出和别的标签页是同一个会话）不再开，不然它永远排不到、等它的一直等着
    if (!tabs.value.includes(tab)) return Promise.reject(Object.assign(new Error('标签页已经关掉了'), { closed: true }));
    const job = { started: false };
    job.p = new Promise((resolve, reject) => Object.assign(job, { resolve, reject }));
    jobs.set(tab, job);
    tab.loading = true;
    // 后台的等这一轮同步代码都排进来再开始走（load、syncAll 是按标签栏顺序一个个排的）：
    // 不然排在前面的后台标签页会抢在正在看的那个之前发出去
    if (tab.id === active.value) startJob(tab, true);
    else queueMicrotask(pump);
    return job.p;
  }
  async function startJob(tab, focus) {
    const job = jobs.get(tab);
    if (!job || job.started) return;
    job.started = true;
    if (focus) focusLive++;
    try {
      adopt(tab, await openOnServer(tab.path, focus));
      tab.okEpoch = epoch;
      job.resolve();
    } catch (e) {
      tab.loadError = `打开失败：${e.message}`;
      job.reject(e);
    } finally {
      jobs.delete(tab);
      tab.loading = false;
      sinceConnect++;
      if (focus) focusLive--;
      // 推送还没连过（刷新页面时，app.js 等 load 返回才第一次连）就不用重连，那一次会把开好的都订阅上
      if (focus && (source || next)) connectSoon();
      if (focus) pump();
    }
  }
  async function pump() {
    if (laneBusy) return;
    const waiting = tabs.value.some(t => jobs.get(t)?.started === false);
    const cur = current.value;
    // 每轮只替它开一次：开失败了（服务器还没起来）不能一圈圈接着重试，原因已经写在它的对话区底部
    if (waiting && cur && !cur.fresh && !jobs.has(cur) && cur.okEpoch !== epoch && cur.triedEpoch !== epoch) {
      cur.triedEpoch = epoch;
      reopen(cur).catch(() => {});   // 正在看的先开（它在服务器那边还在的话很快就回来）
      return;
    }
    if (focusLive) return;   // 正在看的还在路上：后台的等它开好（startJob 收尾时再叫 pump）
    const queued = tabs.value.find(t => jobs.get(t) && !jobs.get(t).started);
    if (!queued) {
      if (sinceConnect && (source || next)) connectSoon();
      return;
    }
    laneBusy = true;
    await startJob(queued, false);
    laneBusy = false;
    pump();
  }
  // 服务器给的简况接到标签页上；刷新页面时还没开好的标签页用的可能是占位编号，换成服务器给的。
  // 同一个会话已经有别的标签页开着了（占位时认不出来），留那一个
  function adopt(tab, brief) {
    const old = tab.id;
    const twin = tabs.value.find(t => t !== tab && t.id === brief.id);
    if (twin) {
      const wasActive = active.value === old;
      close(tab);
      if (wasActive) select(twin);
      return;
    }
    // 简况里的 gen 不抄：它是服务器那份内容的代号，标签页手上的条目没跟着换，gen 只由 apply 跟着条目一起改。
    // 以前一起抄过来，刷新页面恢复的标签页手上一条都没有、代号却对上了，第一次取内容就从第 0 条全取（since=0），
    // 首屏只取末尾 TAIL 条落了空（5615 条的会话一打开就是 1466 行、九万多个节点，手机上滚动卡顿）；
    // 服务器重启后重新打开的，内容换了代也会拿新代号去接旧条目
    const { gen, ...rest } = brief;
    Object.assign(tab, rest);
    if (active.value === old && old !== brief.id) active.value = brief.id;
  }
  reopenVia = reopen;

  let loaded = false;
  // 开着哪些标签页、停在哪个，多用户实例里按身份分开记（游客也算一个身份）：换个身份登录只恢复这个身份自己开过的，
  // 别人的私有会话不拿去试，也就不会每次刷新都弹「没有权限」；换回原来的身份，原来那些照样回来。单用户实例照旧用原来的键。
  // 不知道现在是谁（/api/me 没取到）时是 null：不恢复也不保存，免得记到别人名下。
  // 每个标签页连同编号和标题一起记：刷新页面时先用它们摆出占位标签页，认得出停在哪个、先开哪个；
  // 还没开好的占位编号（load-…）不记
  let keys = null;
  watch(() => tabs.value.filter(t => !t.fresh).map(t => ({
    path: t.path, ...(t.id.startsWith('load-') ? {} : { id: t.id }), ...(t.title ? { title: t.title } : {}),
  })), v => loaded && keys && store.set(keys.tabs, v));
  watch(active, id => loaded && keys && store.set(keys.active, id || ''));

  // ready：身份取到了（users.js 的 meReady）。正在看的那个开好、取到内容就返回（后面要滚到底、连推送），其余的在后台按顺序开
  async function load(ready) {
    await ready;
    if (!me.loaded) {
      loaded = true;
      say(`无法获取当前身份（${me.error || '身份获取失败'}），本次未恢复标签页，新打开的标签页也不会保存；请刷新页面重试`, { err: true, ms: 12000 });
      return;
    }
    const at = me.multi ? `@${me.name || ''}` : '';
    keys = { tabs: `tabs${at}`, active: `active${at}` };
    // 刚改成按身份分开记时这个身份还没有自己的记录，先接着用原来不分身份的那份（没权限的会在下面关掉）
    let saved = store.get(keys.tabs, null) ?? store.get('tabs', []);
    active.value = store.get(keys.active, null) ?? store.get('active', '');
    // 一次只开一个会话（没开底部标签页）：只恢复停着的那个。以前留下来的其余标签页看不见，
    // 却每次刷新、服务器重启都在后台重开一遍，一直订着推送，服务器那边也就一直不释放；这台设备上收掉，会话本身照旧在「最近」里。
    // 停着的是新会话标签页（不记）的话一个都不恢复，app.js 会开一个新会话标签页，和上次看到的一样。
    // 以前记的没有编号、认不出停在哪个：照旧全开，都开好、认出停在哪个以后再把别的收掉（见下面）
    const lone = single();
    if (lone && saved.every(s => s.id)) saved = saved.filter(s => s.id === active.value);
    // 先摆出占位标签页。以前记的没有编号，占位编号认不出停在哪个：那就都按顺序在后台开，停着的那个开好了自然就显示了
    let n = 0;
    tabs.value = saved.map(s => makeTab({ id: s.id || `load-${n++}`, path: s.path, title: s.title || fileName(s.path), loading: true }));
    if (!current.value && tabs.value.length && saved.every(s => s.id)) active.value = tabs.value[0].id;
    // 服务器说文件不在了（400/404）才不再恢复。没有权限看的（403）：按身份分开记以后，只有会话的主人收回了可见范围才会碰到，
    // 留着也只会每次再被拒一遍，所以直接关掉。在查看器里删掉了的（410）也关掉。几种都要告诉用户。
    // 别的错（多半是正赶上服务器重启，代理回了 502）标签页留着，对话区底部写原因、给重试
    let gone = 0, hidden = 0, dead = 0;
    const done = tabs.value.map(t => reopen(t).then(
      () => tabs.value.includes(t) && ensureSynced(t).then(() => (t.unread = 0), () => {}),
      e => {
        const s = e?.status;
        if ([400, 404, 403, 410].includes(s)) {
          if (s === 403) hidden++; else if (s === 410) dead++; else gone++;
          close(t);
        }
      }));
    loaded = true;
    const focus = tabs.value.indexOf(current.value);
    if (focus >= 0) await done[focus];
    Promise.all(done).then(() => {
      if (!current.value && tabs.value.length) active.value = tabs.value[0].id;
      if (lone) for (const t of [...tabs.value]) if (t !== current.value && !stays(t)) close(t);
      if (hidden) {
        say(`已关闭 ${hidden} 个无权查看的标签页（会话主人已收回可见范围）`, { err: true, ms: 12000 });
      } else if (dead) {
        say(`已关闭 ${dead} 个会话已删除的标签页（会话主人和查看器账号可在全部会话底部的「已删除」中恢复）`, { err: true, ms: 12000 });
      } else if (gone) {
        say(`${gone} 个标签页的会话文件已不存在，未恢复`, { err: true });
      }
    });
  }

  // 切到还在恢复队伍里排着的标签页：马上单独开它（见 reopen）
  function select(tab) {
    active.value = tab.id;
    tab.unread = 0;
    if (jobs.get(tab)?.started === false) startJob(tab, true);
  }
  // 手机上左右滑：-1 是上一个标签，1 是下一个
  function stepTab(dir) {
    const i = tabs.value.findIndex(t => t.id === active.value);
    const next = tabs.value[i + dir];
    if (i >= 0 && next) select(next);
  }
  // 关闭只是从这台设备的标签栏里拿掉，会话文件和别人的标签都不受影响。还在恢复队伍里排着的不再开
  function close(tab) {
    const job = jobs.get(tab);
    if (job && !job.started) {
      jobs.delete(tab);
      job.reject(Object.assign(new Error('标签页已经关掉了'), { closed: true }));
    }
    const i = tabs.value.indexOf(tab);
    tabs.value = tabs.value.filter(t => t !== tab);
    if (active.value === tab.id) active.value = (tabs.value[i] || tabs.value[i - 1])?.id;
  }
  // 打开会话：已经开着的就直接切过去
  // 大会话要几秒才打开：超过一小会儿还没好，底部就挂一句「正在打开」，好了收掉；失败由调用方说原因
  // 已经开着的（包括没有标签页栏时后台留着的那几个）直接切过去，不再问服务器：问一趟慢了也会挂「正在打开」。
  // 以前问了也只是拿回编号找到它，不更新别的
  const openTab = path => {
    if (/^api-session\/api-[a-f0-9]+\.jsonl$/.test(path)) {
      // 侧栏收起会回退它自己的 history 步；新窗口不会被那次回退取消导航。
      const page = window.open(`/api-session.html?session=${encodeURIComponent(fileName(path).replace(/\.jsonl$/, ''))}`, '_blank');
      if (!page) return Promise.reject(new Error('只读会话窗口被浏览器拦住了，请允许本站打开新窗口'));
      page.opener = null;
      return Promise.resolve();
    }
    const open = tabs.value.find(t => t.path === path && !t.fresh);
    if (open) return Promise.resolve(select(open));
    return openWith(() => openOnServer(path));
  };
  // 分享链接只带会话编号：服务器去找文件，照常查这个人能不能看
  const openSid = sid => openWith(() => api('/api/tabs', { method: 'POST', body: { sid } }));
  // 新开的会话先订阅推送、订阅上了再取内容：先取后订阅的话，中间推来的东西会漏，只好连上以后再全部补拉一遍。
  // 取好内容才摆进标签页、切过去，换之前照旧看着原来那个。一次只开一个会话、后台也不留（single 且 keep 是 1）时，正在看的那个切过去以后就收掉（app.js），
  // 这次订阅就不带它（leaving），收掉时订阅不变，不用再连一次；后台留着几个时它还留着，照旧订着
  async function openWith(request) {
    const OPENING = '正在打开会话…';
    const hint = setTimeout(() => say(OPENING, { ms: 0 }), 300);
    let leaving = null, tab = null, mine = false;
    try {
      const brief = await request();
      tab = tabs.value.find(t => t.id === brief.id);
      if (!tab) {
        tab = makeTab(brief);
        mine = true;
        const cur = current.value;
        if (single() && keep() <= 1 && cur && !stays(cur)) (leaving = cur).leaving = true;
        await subscribe(tab);
        await sync(tab);
        tabs.value.push(tab);
      }
      select(tab);
    } finally {
      if (mine) opening.delete(tab);
      // 没换过去（打开或取内容出错了）：原来那个照旧订着
      if (leaving && tabs.value.includes(leaving) && active.value === leaving.id) {
        leaving.leaving = false;
        connectSoon();
      }
      clearTimeout(hint);
      if (toast.text === OPENING) say('');
    }
  }
  // 等推送订阅上这个正在打开的标签页（见 connect 的 onopen）。推送连不上（服务器出问题、页面还没开始连推送）就不干等，
  // 最多等 3 秒直接去取，取完照样摆出来，订阅等推送连上时补上
  const opening = new Map();   // 正在打开、还没摆进标签页的 → 订阅上了叫一声
  function subscribe(tab) {
    if (!loaded) return Promise.resolve();
    return new Promise(resolve => {
      const timer = setTimeout(resolve, 3000);
      opening.set(tab, () => { clearTimeout(timer); resolve(); });
      connectSoon();
    });
  }

  // 服务器推送：订阅所有打开的标签，有新条目时服务器直接推过来。
  // 订阅变了（开了、收了标签页）要换一条连接：先连新的、连上了再关旧的，中间不断档，所以只有新加进来的要补拉；
  // 以前是先关再连，断开那一会儿谁都可能漏，连上以后把所有标签页补拉一遍，大会话每次补拉都带着整份附表。
  // 推送真断过（浏览器报错、心跳停了）才全部补拉
  let source = null;     // 正在用的连接
  let next = null;       // 换订阅时新开、还没连上的那条
  let live = new Set();  // source 订阅着的编号
  let dropped = false;   // 推送断过、还没重新连上（见 onopen）
  let lastSeen = 0;   // 最后一次从推送连接收到东西（含心跳：新后端 3 秒一次，旧后端 15 秒一次）的时间
  // 换连接那一会儿旧连接收到的推送原文：新连接从服务器登记它那一刻起，会把同一条再发一遍，认出来丢掉（不然接不上、又要补拉）
  const echo = new Set();
  let echoTimer = 0, connectTimer = 0;
  // 推送断了：浏览器报连接出错，或者 20 秒什么都没收到（正常 15 秒一次心跳）。看板上挂个红标签，重新收到东西就撤
  const pushLost = ref(false);
  const syncAll = () => tabs.value.forEach(t => sync(t).catch(() => {}));
  // 要订阅的：开着的标签页（一次只开一个会话时马上要收掉的那个除外，见 openWith），加上正在打开、等订阅上再取内容的。
  // 新会话标签页说出第一句话以后，订阅的是服务器给的那个编号（birth）
  const wanted = () => [...new Set([...tabs.value.filter(t => !t.leaving), ...opening.keys()]
    .map(t => (t.fresh ? t.birth : t.id)).filter(Boolean))];
  // 同一轮里订阅变了好几次（开一个、收一个、恢复队伍开好一个）只连一次
  function connectSoon() {
    clearTimeout(connectTimer);
    connectTimer = setTimeout(connect, 0);
  }
  function connect() {
    clearTimeout(connectTimer);
    sinceConnect = 0;   // 恢复队伍开好的标签页这一连都订阅上了（见 pump）
    const ids = wanted();
    next?.close();
    next = null;
    // 真断过：旧连接没用了，直接关掉，连上以后全部补拉
    if (dropped || !ids.length) { source?.close(); source = null; live = new Set(); }
    if (!ids.length) return (pushLost.value = false);
    // 订阅没变、连接好好的：不用换
    if (source && ids.length === live.size && ids.every(id => live.has(id))) return;
    if (!source) lastSeen = Date.now();
    // delta=1：几张附带的表只收变了的条目（见 apply）。没重启的旧服务器不认这个参数，照旧发整份，apply 也照旧处理
    const es = next = new EventSource(`/api/stream?ids=${ids.join(',')}&delta=1`);
    es.onopen = () => {
      lastSeen = Date.now();
      pushLost.value = false;
      es.every = 0;   // 心跳间隔等这次连上以后的第一个心跳再认：重连上的可能是重启成旧版本的服务器（不带间隔），不能照旧标黄
      // 同一条连接断了以后浏览器自己重连上的：这段时间漏掉的全部补拉。真断过再连上才算新的一轮（见 epoch）
      if (es === source) {
        if (dropped) { epoch++; dropped = false; }
        return syncAll();
      }
      if (es !== next) return es.close();   // 还没连上就又换了一条
      const prev = source, had = live;
      source = es;
      next = null;
      live = new Set(ids);
      prev?.close();
      clearTimeout(echoTimer);
      echoTimer = setTimeout(() => echo.clear(), 10000);
      // 正在打开的等着自己取内容，这里叫它一声
      for (const [t, ready] of opening) if (live.has(t.id)) ready();
      // 之前没连着（刚打开页面、断过）：全部补拉。恢复队伍为了订阅新开好的标签页换连接不算新的一轮，不然正在看的那个每换一次都要再开一遍
      if (!prev || dropped) {
        if (dropped) { epoch++; dropped = false; }
        return syncAll();
      }
      for (const t of tabs.value) if (!had.has(t.fresh ? t.birth : t.id) && live.has(t.fresh ? t.birth : t.id)) sync(t).catch(() => {});
    };
    es.onerror = () => { if (es === source) { pushLost.value = true; dropped = true; } };   // 浏览器会自己重连，连上了 onopen 再撤
    es.onmessage = e => onPush(es, e);
  }
  function onPush(es, e) {
    lastSeen = Date.now();
    pushLost.value = false;
    const msg = JSON.parse(e.data);
    if (msg.ping) {
      es.every = msg.every || 0;   // 新后端的心跳带着间隔（秒），见 pushEvery
      if (msg.v && es === source) reconcile(msg.v);
      return;
    }
    // 换连接那一会儿：旧连接收到的带内容的推送记下，新连接重放的同一条丢掉（只认带 start 的，补拉、删除这些照常处理）
    if ('start' in msg) {
      if (es === source && next) echo.add(e.data);
      else if (echo.delete(e.data)) return;
    }
    if (msg.all) return syncAll();
    if (msg.settings) return onSettings?.();   // 全局设置改了：重新取（每人看到的「此刻会用哪个号」不一样）
    if (msg.birth) {
      const fresh = tabs.value.find(t => t.fresh && t.birth === msg.id);
      return fresh && onBirth(fresh, msg);
    }
    const tab = tabs.value.find(t => t.id === msg.id);
    if (!tab) return;
    // 看不到这个会话了（会话主人收回了可见范围）：已经显示的内容也撤掉，写明原因；推送也不再给它发
    if (msg.denied) return Object.assign(tab, { items: [], offset: 0, cut: false, loadError: msg.denied, running: false });
    // 会话被删掉了（这台设备之外删的，或者同一会话经别的路径开着的标签页）：关掉，说是谁什么时候删的、去哪里恢复
    if (msg.deleted) {
      close(tab);
      return say(msg.deleted, { err: true, ms: 12000 });
    }
    // 服务器重启过、不认识这个会话了：排进恢复队伍重新打开（出错原因 startJob 记在标签页上），
    // 开好以后取一次内容；重新订阅等正在看的开好、或者队伍走空时一起做。服务器这次没登记它，
    // 从「订阅着的」里拿掉，编号没变也要换一条连接
    if (msg.reopen) {
      if (es === source) live.delete(tab.id);
      return reopen(tab).then(() => ensureSynced(tab).catch(() => {}), () => {});
    }
    // 推来的这一截接不上（换代了或中间漏了），就老老实实拉一遍
    if (msg.resync || msg.gen !== tab.gen || msg.start !== tipOf(tab)) {
      return sync(tab).catch(() => {});
    }
    apply(tab, msg);
  }
  // 对账：心跳里带着这条连接订阅的各会话服务器推到了第几次（v，server.Tab.push_n），
  // 排在心跳前面的推送都已经到了，所以手上的编号比它旧就是漏了（推送丢了、被别的回复盖掉了），补拉一次，不用等刷新。
  // 正在补拉、还在恢复队伍里的不比（它们马上会拿到新的）；旧后端的心跳不带 v，什么都不做
  function reconcile(v) {
    for (const [id, want] of Object.entries(v)) {
      const tab = tabs.value.find(t => t.id === id && !t.fresh);
      if (!tab || tab.v == null || tab.v >= want || syncs.has(tab) || jobs.has(tab)) continue;
      gapNote(tab, { kind: 'behind', have: tab.v, want });
      sync(tab).catch(() => {});   // 出错原因 sync 记在 tab.loadError 上了
    }
  }
  // 对不上时告诉服务器记一笔日志（查漏推用，同一处只报一次）；送不到只在控制台说，不打扰用户
  function gapNote(tab, { kind, have, want }) {
    const key = `${kind}:${have}:${want}`;
    if (tab.gapNoted === key) return;
    tab.gapNoted = key;
    api('/api/push_gap', { method: 'POST', body: { id: tab.id, kind, have, want, n: tipOf(tab) } })
      .catch(e => console.warn('推送对账的记录没送到服务器', e));
  }
  // 打开、关闭标签时换一条订阅
  watch(() => tabs.value.map(t => (t.fresh ? t.birth : t.id)).filter(Boolean).join(','), () => loaded && connectSoon());
  // 连接「半死」时浏览器既不报错也不重连（手机切后台、锁屏、隧道断过一下），发出去的话就一直挂着「发送中」：
  // 服务器每 15 秒有一次心跳，超过 45 秒什么都没收到就当它断了，重连并补拉；切回前台、网络恢复时也立刻重连
  // 这几种重连都可能是真断过：连上以后算新的一轮
  const reconnectLost = () => { dropped = true; connect(); };
  setInterval(() => {
    if (!loaded || !(source || next)) return;
    const quiet = Date.now() - lastSeen;
    if (quiet > 20000) pushLost.value = true;
    if (quiet > 45000) reconnectLost();
  }, 5000);
  document.addEventListener('visibilitychange', () => { if (loaded && document.visibilityState === 'visible') reconnectLost(); });
  window.addEventListener('online', () => loaded && reconnectLost());

  // 推送连接多少毫秒没收到任何东西了（正常最多一个心跳间隔）：状态那行用它判断是不是网页和服务器之间断了
  const pushQuiet = () => (source || next ? Date.now() - lastSeen : 0);
  // 服务器说的心跳间隔（秒）：旧后端不说就是 0，状态那行不据此标黄（它 15 秒才一次心跳，会误报）
  const pushEvery = () => source?.every || 0;

  return { tabs, active, current, scroller, rows, setLog, load, connect, select, close, openTab, openSid,
           loadEarlier, loadLater, loadHead, loadTail, trim, tipOf, stepTab, ensureLoaded, newFresh, promote,
           pushQuiet, pushEvery, pushLost, retrySync };
}
