// 对话里的一行：气泡、思考、工具卡片、系统消息，按条目种类分派
const { ref, reactive, watch } = Vue;
import { clock, clockSpan, kilo, frameOnce, shortModel, modelName, modelTier, modelFamily, OFFPEAK } from './util.js';
// 这个文件里的 fen 是写成「0.03¥」的那个（points.js 的 fenSign）：这里画的都是对话里的回复
import { callPoints, callEstimated, partPoints, accountOf, fenSign as fen, callCharge, chargeSplit, vSplit } from './points.js';
import { vMd } from './markdown.js';
import { ToolCard, TOOL_HEAD, TOOL_ICON, ICON_OTHER } from './tool-card.js';

// 标题栏上的用时：紧凑写法，到秒为止，单字母单位、两段之间空一格：42s、8m 9s、1h 8m
const shortSpan = ms => {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60 ? ` ${s % 60}s` : ''}`;
  const m = Math.floor(s % 3600 / 60);
  return `${Math.floor(s / 3600)}h${m ? ` ${m}m` : ''}`;
};

// 手机上没有悬停：点到哪个 Claude 气泡（点开里面的工具也算），就摊开它头部的四个数，同一时间只摊一个，
// 点到气泡外面收起。用点击事件来摊，点击照常交给工具卡片，不像 :hover 那样第一下被浏览器拿去当悬停。
// 电脑上也这样：气泡头上那行整行平时藏着，点了才出来（用户的气泡也是）
const tappedUse = ref(0);
let tapSeq = 0;
document.addEventListener('click', e => { if (!e.target.closest?.('.bubble')) tappedUse.value = 0; });
// 细项藏回去也要淡出：细项平时是 display: none，Firefox 不会等透明度过渡播完再切 display，
// 一藏就没了。所以藏的时候先留着显示、透明度降到 0（样式里的 .fading / .hov-out），播完才真藏掉。
// 点开的那个换走了：刚收起的那个记在 fadedUse 里一小会儿；鼠标移出气泡：给气泡挂一小会儿 hov-out。
// 点开时头上那行是折行摊开的（.ai-head.shown），淡出期间也得照样摊着：一收回一行，后面的细项立刻被截到行尾外面，看着还是一下没了
const FADE_MS = 200;
const fadedUse = ref(0);
let fadeTimer = 0;
watch(tappedUse, (_, old) => {
  if (!old) return;
  fadedUse.value = old;
  clearTimeout(fadeTimer);
  fadeTimer = setTimeout(() => { fadedUse.value = 0; }, FADE_MS);
});
document.addEventListener('mouseout', e => {
  const b = e.target.closest?.('.bubble.ai');
  if (!b || b.contains(e.relatedTarget)) return;
  b.classList.add('hov-out');
  clearTimeout(b._hovOut);
  b._hovOut = setTimeout(() => b.classList.remove('hov-out'), FADE_MS);
});

// 收起和展开（要的是不用学就会用，界面上只说「收起」「展开」）：
// 1. 用户一句话之后 Claude 连着的一段白色气泡是一轮回复，看起来是一个整体：最后一个是汇报（这一轮的产出），前面的是工作过程。
//    最后一个调了工具的不算汇报，整段都是过程，收起来只剩标题栏。
//    只有绿色气泡和开新一轮的后台通知（Claude 空闲时到的那种）把一段隔开，中间的系统消息、附件、正忙时插进来的通知这些不算。
// 2. 每一轮都收进工作过程块（只有一个不调工具的汇报的除外），顶上一行标题栏（kind: 'thead'）：箭头、工作过程、用时、几次请求、
//    几次工具调用，右边整轮的总价，点它收起 / 展开中间的过程；展开着时过程末尾、汇报前面还有一行「收起工作过程」（kind: 'tfoot'），
//    收起后汇报留在原处。这一轮停下来以后过程自动收起，只剩标题栏和汇报连成一张卡片，
//    背后往上露出错开的纸边；汇报自己的价格和时间不再写（总价在标题栏）。
// 3. 收起只露四行：用户的话、汇报、单独一个气泡都一样，照原样排、往下渐隐，点哪里都展开。
//    正文估出来比一屏还高的默认就收着（用户的话和 Claude 的气泡都是），点开、收起时气泡的高度有动画。
//    正在写的那一轮不自动收：看着它一行行写出来，写完突然收成四行会跳
// 4. 右键 / 长按菜单里的「收起」不分点在哪：一轮回复先收过程，过程收着了才收汇报；「展开」先放出过程。
// 状态按条目自己的编号记在这里（Claude 的按回复编号，用户的话按时间和开头几个字），不挂在组件上：标签页内容被释放后重画、
// 往上补内容时组件会重建，收起的不能跟着散开；外层有 v-memo，也不从外面传。只记在这一页里，刷新以后按行数重新来
const folded = reactive(new Set());       // 收起只露四行的气泡
// 默认收着的（正文比一屏高的，以及不是用户说的、却画成绿色气泡的：后端标了 auto，CLI 自己排进来的通知、压缩摘要），这里记被点开的
const opened = reactive(new Set());
// 在这一页里看着写出来的气泡（正在回复的那一轮里出现过的），不按长短自动收。按 foldKey 记，不用响应式：写完以后照旧展开着
const seenLive = new Set();
const prefixOpen = reactive(new Set());   // 被展开了的过程（停下来以后本该自动收起）
const prefixShut = reactive(new Set());   // 被收起来的过程
const openPrefix = k => { prefixShut.delete(k); prefixOpen.add(k); };
const shutPrefix = k => { prefixOpen.delete(k); prefixShut.add(k); };
// 每次排好对话行时记下（foldPrefixes）：过程里的气泡、汇报各属于哪一轮（轮的编号就是标题栏的编号），各轮的过程此刻收没收着。
// 几个标签页共用，所以只增改、不清空（编号按回复编号，各会话不会撞）
// cardReport：那一轮有没有汇报（最后一个气泡调了工具就没有，整段都是过程）
const prefixOf = new Map(), tailOf = new Map(), cardShut = new Map(), cardReport = new Map();
// 调没调工具：请求气泡看它包着的全部条目（藏起来的工具也算），别的看画出来的那几段
const usesTool = r => (r.all || r.parts || []).some(p => p.kind === 'tool');
// 工作过程里特别长的正文是在跟用户说话：像汇报一样露在过程外面，把这一轮从它后面断开、前后各收各的过程。
// 阈值看了多用户实例 30 天的数据：只数正文块时过程正文 95% 不到 170 字、98% 不到 275 字，
// 300 字以上的抽出来看都是在讲原因、讲方案、认错；带字的思考块（session.py 标了 thought：过渡话、英文的内心独白）不算字数。
// 旧后端不标 thought，就连它一起数
const TALK_CHARS = 300;
const talkLen = r => (r.kind === 'call' ? r.all || [] : [r])
  .reduce((n, p) => n + (p.kind === 'text' && !p.thought ? (p.text || '').trim().length : 0), 0);
// 一个请求气泡里画出来的工具调用
const toolsOf = r => (r.parts || []).filter(p => p.kind === 'tool');
// 正在回复的那一轮，标题栏上的用时每秒往上走：只有还在回复的标题栏读它，别的不跟着重画
const nowTick = ref(Date.now());
setInterval(() => { nowTick.value = Date.now(); }, 1000);
// 有标题栏、此刻展开着的过程里的行（连同夹在中间的系统消息这些）→ 那一轮的编号：给标题栏到「收起工作过程」这一整段
// 垫一块底，看得出过程是一个整体、有多大（style.css 的 .row.proc）。按行对象记，过程收起时行不在了，再展开时重新挂上去画
const procOf = new WeakMap();
// 三级折叠的中间那级：展开着的工作过程，每一步（一次请求的气泡）默认收成一行，
// 点开才是原样的气泡，里面的工具照旧各自收着；汇报不收。这里记被点开的那几步（按 foldKey）
const stepOpen = reactive(new Set());
// 点开了思考药丸的请求气泡（按 foldKey）：明文思考（API 号）平时收着，点头部的「思考 3.2k」才摊在气泡最上面
const thinkOpen = reactive(new Set());
const thinkClosing = reactive(new Set());   // 正在收起的：小三角先转回去，原文等缩完才拿掉
// 收成一行时写的那句：去掉 Markdown 记号、并成一行
const flat = s => (s || '').replace(/[`*_#>|]/g, '').replace(/\s+/g, ' ').trim();
// 只调了工具、没写字的一步，按工具补上动词，对象一个个写出来（只写「读 3 个文件」太简陋）：
// 「读 a.py 第 10–60 行、b.js」「读 style.css 第 10–20、80–100 行」「改 a.py 3 处、b.js」「运行 说明一；说明二」。
// 对象是 TOOL_HEAD 那句，文件只写文件名；同一种工具同一个对象并在一起。没列的工具写名字
const STEP_VERB = {
  Read: '读', Write: '写', Edit: '改', MultiEdit: '改', NotebookEdit: '改', Bash: '运行', Grep: '搜', Glob: '找文件',
  WebFetch: '打开', WebSearch: '搜索', ToolSearch: '查找工具', Agent: '派子代理', Task: '派子代理', Skill: '用技能', TodoWrite: '更新待办',
};
const FILE_TOOLS = /^(Read|Write|Edit|MultiEdit|NotebookEdit)$/;
// Read 读了哪几行：offset 是从第几行起、limit 是读几行（都没写就是从头读，写「全文」）
const readSpan = i => {
  const off = Number(i.offset) || 0, lim = Number(i.limit) || 0;
  return lim ? `${off || 1}–${(off || 1) + lim - 1}` : off ? `${off} 起` : '';
};
// 收成一行的一步里工具图标按结果上色，和工具卡片一个规矩：没结果是还在跑，带 error 是失败，其余成功
const STEP_STATE = { ok: '', err: '，失败', wait: '，还在跑' };
// 系统消息、附件收起时那一行写什么：直接写记录里的英文类型（「system · api_error」）用户看不懂，也杂乱。
// 认得出的换成一句中文，认不出的统一写「系统消息」「附带的内容」，点开照旧是原文
const SYS_SUB = {
  local_command: '命令输出', informational: '提示', model_refusal_no_fallback: '模型拒绝回答', stop_hook_summary: '停止时的钩子',
};
const ATTACH_SUB = {
  environment: '环境信息', date: '日期', file: '附带的文件', edited_text_file: '改过的文件', nested_memory: '记忆文件',
  instructions: '项目说明', hook_additional_context: '钩子附加的内容', hook_success: '钩子运行完', total_tokens_reminder: '上下文用量提醒',
  skill_listing: '可用技能', deferred_tools_record: '可用工具', deferred_tools_delta: '可用工具变动', mcp_instructions_delta: 'MCP 说明变动',
  read_truncation_notice: '读文件被截断', compact_file_reference: '压缩前读过的文件', thinking_drop: '丢掉了思考',
};
function sysLabel(it) {
  if (it.kind === 'error') return it.limit ? '到达限额' : '出错了';
  if (it.kind === 'meta') return 'CLI 附带的消息';
  if (it.kind === 'attachment') return ATTACH_SUB[it.subtype] || '附带的内容';
  if (it.subtype === 'api_error') return /第 \d+ 次重试/.test(it.text || '') ? '接口出错，正在重试' : '接口出错';
  return SYS_SUB[it.subtype] || '系统消息';
}
const foldKey = it => (it.kind === 'user' ? `u:${it.ts}:${(it.text || '').slice(0, 40)}:${it.image || ''}`
  : `${it.kind}:${it.mid || it.all?.[0]?.mid || it.ts}:${it.cont ? it.seq : ''}`);
const AI_KINDS = new Set(['call', 'text']);
const FOLD_LINES = 4;     // 收起后露几行，和样式里 .bubble.folded > .fold-body 的高度一致

// 一行大约能写几个汉字：对话区最宽 920px、两边各留 16px；电脑上 Claude 的气泡最宽 85%，手机上占满；气泡左右内边距加边框 26px，正文 14px
const textCols = ref(40);
// 一屏能放几行正文（14px、行高 1.6）：窗口高度减去上面的标签栏、底下的输入框和小标签那一排，大约 200px。
// 手机上只在宽度变了（转屏）时重算：弹出输入法时窗口变矮，跟着算的话气泡会一会儿收一会儿开
const screenLines = ref(30);
let screenW = 0;
function measureCols() {
  const w = Math.min(innerWidth - 32, 920), narrow = matchMedia('(max-width: 760px)').matches;
  textCols.value = Math.max(10, Math.floor(((narrow ? w : w * 0.85) - 26) / 14));
  if (!narrow || innerWidth !== screenW) {
    screenW = innerWidth;
    screenLines.value = Math.max(12, Math.floor((innerHeight - 200) / (14 * 1.6)));
  }
}
measureCols();
addEventListener('resize', () => frameOnce(measureCols));

// 估一段话、一个请求气泡画出来有几行：正文按每行字数折行（汉字占一格，其余算半格多），空行算半行，代码块不折行（横着滚）；
// 气泡还有头部一行，工具细条、收着的思考各算一行半（连同间隔）。只求个大概，用来判断长不长
const lineCache = new WeakMap();
function textLines(text, cols) {
  let n = 0, code = false;
  for (const line of text.split('\n')) {
    if (/^\s*(```|~~~)/.test(line)) { code = !code; n += 1; continue; }
    if (code) { n += 0.9; continue; }
    if (!line.trim()) { n += 0.5; continue; }
    let w = 0;
    for (const c of line) w += c.charCodeAt(0) > 0x2e80 ? 1 : 0.55;
    n += Math.max(1, Math.ceil(w / cols));
  }
  return n;
}
function rowLines(row, cols) {
  const hit = lineCache.get(row);
  if (hit?.cols === cols) return hit.n;
  let n = 1;
  if (row.kind === 'call') {
    for (const p of row.parts || []) n += p.kind === 'text' ? textLines(p.text || '', cols) : 1.5;
  } else n += textLines(row.text || '', cols);
  lineCache.set(row, { cols, n });
  return n;
}

// 认出每一轮回复，给过程长的（或动过的）加标题栏，过程收着的那几轮只留标题栏和汇报（tabs.js 排好对话行后调）。
// 排出来的标题栏、收着时的汇报沿用上次的对象（没变就不重画，对话行有 v-memo）
const heads = new Map(), tails = new Map(), feet = new Map();
// cuts：开新一轮的后台通知、重启提示、loop 和定时任务到点那句的序号（CLI 拿它开了新的一轮，见 session.py 里各处的 turn），从小到大。
// 它和用户说话一样把前后分成两轮；按序号认，不看它此刻显不显示：关掉「系统消息」时它藏起来了，分轮也不能跟着变
export function foldPrefixes(rows, running, cuts = []) {
  const runs = [];
  let run = [], c = 0, prev = -Infinity;
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    if (r.kind === 'user') { if (run.length) runs.push(run); run = []; }
    else if (AI_KINDS.has(r.kind)) {
      for (; c < cuts.length && cuts[c] < r.seq; c++) {
        if (cuts[c] > prev && run.length) { runs.push(run); run = []; }
      }
      run.push(i);
      prev = r.seq;
    }
  }
  const live = running && run.length ? run : null;   // 后面还没有绿色气泡、Claude 还在回复的那段：停下来以前不自动收
  if (live) for (const i of live) seenLive.add(foldKey(rows[i]));   // 也不按长短自动收成四行，写完以后照旧展开着
  if (run.length) runs.push(run);
  const plan = new Map(), before = new Map(), after = new Map();
  // 过程里够长的正文（TALK_CHARS）把一轮断成几段：每段到这样一个气泡为止，它当这一段的汇报（调没调工具都算）
  const segs = [];
  for (const idx of runs) {
    let from = 0;
    for (let k = 0; k < idx.length - 1; k++) {
      if (talkLen(rows[idx[k]]) >= TALK_CHARS) { segs.push({ idx: idx.slice(from, k + 1), talk: true, run: idx }); from = k + 1; }
    }
    segs.push({ idx: idx.slice(from), talk: false, run: idx });
  }
  // 一个规矩：每一轮都收进工作过程块，不分长短，块外面最多只留最后一个不调工具的汇报，
  // 不再有散落的气泡；正在回复时块展开（里面每一步收成一行，见 isStep），这一轮停下来就整块收起，用户点过的照点的来。
  // 断开的几段各是一块；正在回复的那一轮里几段都展开着（open），用时只在最后一段上往上走（isLive）
  for (const { idx, talk, run: whole } of segs) {
    const open = whole === live, isLive = open && !talk;
    if (idx.length < 2 && (talk || !usesTool(rows[idx[0]]))) {   // 只有一个汇报（或一段长话）：没有过程
      const k = foldKey(rows[idx[0]]);
      prefixOf.delete(k);
      tailOf.delete(k);
      continue;
    }
    // 最后一个气泡没调工具才算汇报；调了工具（说完话还在干活、被打断、正在跑）就整段都是过程，没有汇报。
    // 断开那一段的长话调了工具也算汇报
    const lastAt = idx[idx.length - 1], last = rows[lastAt], report = talk || !usesTool(last);
    const pre = (report ? idx.slice(0, -1) : idx).map(i => rows[i]), key = 'p:' + foldKey(pre[0]);
    for (const it of pre) { const k = foldKey(it); prefixOf.set(k, key); tailOf.delete(k); }
    if (report) {
      const lk = foldKey(last);
      tailOf.set(lk, key);
      prefixOf.delete(lk);
    }
    const shut = prefixShut.has(key) || (!open && !prefixOpen.has(key));
    cardShut.set(key, shut);
    cardReport.set(key, report);
    const end = report ? lastAt : lastAt + 1;   // 过程到这一行为止（不含）
    for (let j = idx[0]; j < end; j++) shut ? procOf.delete(rows[j]) : procOf.set(rows[j], key);
    const calls = report ? [...pre, last] : pre;
    let head = heads.get(key);
    if (!head || head.shut !== shut || head.live !== isLive || head.calls.length !== calls.length || calls.some((r, i) => head.calls[i] !== r)) {
      // 序号比过程第一个气泡小半个：和它不撞（列表的键、找话跳转都按序号），收着时找话落在被收起的气泡上会先找到这一行。
      // live：这一轮还在回复，标题栏上的用时每秒往上走（ItemRow 的 head）
      head = Object.freeze({ kind: 'thead', key, shut, seq: pre[0].seq - 0.5, ts: pre[0].ts, calls, hidden: pre.length, report, live: isLive });
      heads.set(key, head);
    }
    let tail = null;
    if (shut && report) {
      tail = tails.get(key);
      if (!tail || tail.src !== last) {
        tail = Object.freeze({ ...last, src: last, tail: key });
        tails.set(key, tail);
      }
    }
    plan.set(idx[0], { head, tail, shut, end: lastAt });
    if (!shut) {
      // 展开着时过程末尾再插一行「收起工作过程」，和标题栏一头一尾，不用翻回顶上才能收：有汇报插在汇报前面，
      // 序号比汇报小半个；没有汇报接在最后一个气泡后面，序号比它大半个，和别的行都不撞
      const seq = report ? last.seq - 0.5 : last.seq + 0.5, reportSeq = report ? last.seq : null;
      let foot = feet.get(key);
      if (!foot || foot.seq !== seq || foot.reportSeq !== reportSeq) {
        foot = Object.freeze({ kind: 'tfoot', key, seq, reportSeq });
        feet.set(key, foot);
      }
      (report ? before : after).set(lastAt, foot);
    }
  }
  if (!plan.size) return rows;
  const out = [];
  for (let i = 0; i < rows.length; i++) {
    const p = plan.get(i);
    if (p) {
      out.push(p.head);
      if (p.shut) {   // 过程连同夹在中间的系统消息这些都收起来，只留汇报（没有汇报就只剩标题栏）
        if (p.tail) out.push(p.tail);
        i = p.end;
        continue;
      }
    }
    if (before.has(i)) out.push(before.get(i));
    out.push(rows[i]);
    if (after.has(i)) out.push(after.get(i));
  }
  return out;
}

// 找话跳进收起来的地方：标题栏就展开过程，别的气泡展开它自己（search.js 用，el 是带 data-fold 的气泡）
export const unfold = el => {
  const k = el.dataset.fold;
  if (k.startsWith('p:')) return openPrefix(k);
  opened.add(k);
  folded.delete(k);
  stepOpen.add(k);
};

// 收起只露四行时底下的「展开全文」（点气泡哪里都展开，它只是告诉人能点）
const FOLD_BTNS = `<button v-if="isFolded" type="button" class="fold-more">展开全文<template v-if="foldInfo"> · {{ foldInfo }}</template></button>`;
// 气泡头上这次请求的花费：靠右放在气泡右上角
// （.pts-right 右浮动，所以写在头的最前面），工作过程里外一个样子，
// 和收成一行时最右边那列花费、标题栏右边的总价对齐
// 只写余额实际扣掉的一个数，免费、折扣免掉的和预定付掉的在悬停（手机上点一下）的分项卡片里（points.js 的 vSplit）
const PTS_BODY = `<b v-for="x in pts" :key="x" class="pts" v-split="split">{{ x }}</b><small v-if="priceEstimated"> · 预估</small>`;
// 限量号照常扣钱的那份不另写「非预定的消耗」，红色的花费已经说明了。
// DeepSeek 这次调用按峰还是谷计价（peakMark），写在花费左边，悬停才显示
const PTS_PEAK = `<span v-if="peakMark" class="pts-note pts-peak" :class="{ shown: showUse, fading: fadeUse }">{{ peakMark }}</span>`;
const PTS_HEAD = `<span v-if="pts.length" class="pts-right">${PTS_PEAK}${PTS_BODY}</span>`;
export const ItemRow = {
  name: 'ItemRow',
  props: ['item', 'result', 'results'],
  emits: ['open'],
  components: { ToolCard },
  directives: { md: vMd, split: vSplit },
  inject: { menu: { default: null }, canQuote: { default: () => () => true }, mdUser: { default: null } },
  created() { this.tapId = ++tapSeq; },
  watch: {
    // 头上那行点开时折行摊开、收起时回到一行，高度平滑过渡（一下跳过去的话，下面的正文跟着蹦）。
    // 侦听器在页面更新前跑：先量旧高度，更新完再量新高度，从旧的动画过去；动画半路又变了，从眼下的高度接着走
    headOpen() {
      const head = this.$el?.querySelector?.(':scope > .bubble.ai > small.ai-head');
      if (!head) return;
      const h0 = head.offsetHeight;
      this.$nextTick(() => {
        head._resize?.cancel();
        head.style.overflow = '';
        const h1 = head.offsetHeight;
        if (Math.abs(h1 - h0) < 1 || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        head.style.overflow = 'hidden';   // 长的途中比字矮，不藏住的话折出来的那几行压在正文上
        head._resize = head.animate([{ height: `${h0}px` }, { height: `${h1}px` }], { duration: FADE_MS, easing: 'cubic-bezier(0.2, 0.7, 0.2, 1)' });
        head._resize.onfinish = () => { head.style.overflow = ''; };
      });
    },
  },
  computed: {
    // 手机上最近点过的就是这个气泡：头部的四个数摊开
    showUse() { return tappedUse.value === this.tapId; },
    fadeUse() { return fadedUse.value === this.tapId; },
    headOpen() { return this.showUse || this.fadeUse; },
    // 气泡头部悬停时的三个数，后面各跟这一项单独占几分。输入并进缓存写入、不单写：GPT 没有缓存写入，
    // 没命中的前缀全记在输入里，可它会被自动缓存、下次命中，按 Claude Code 的说法就是缓存写入；Claude 的输入只有零星几个 token，一起并了。
    // 花费按输入、缓存写入各自的单价算再相加，三项加起来正好是头部的总数
    useParts() {
      const u = this.item.use;
      if (!u) return [];
      return [['写入', { in: u.in, cw: u.cw, cw1h: u.cw1h }], ['命中', { cr: u.cr }], ['输出', { out: u.out }]].map(([label, part]) =>
        ({ label, n: kilo(Object.entries(part).reduce((s, [k, n]) => s + (k === 'cw1h' ? 0 : n || 0), 0)),
           p: (x => (x == null ? '' : fen(x)))(partPoints(part, this.item.model, accountOf(this.item.mid))) }));
    },
    // DeepSeek 的调用按峰还是谷计价：服务器给谷时段的模型名接着 @offpeak（peak.py），别家的模型没有
    peakMark() {
      const m = this.item.model || '';
      return ['deepseek', 'glm'].includes(modelFamily(m)) ? (m.endsWith(OFFPEAK) ? '谷' : '峰') : '';
    },
    // 气泡头部写是哪个模型回的（Opus 5.5、GPT-6 Astra 这样，util.modelName）
    speaker() {
      return modelName(this.item.model);
    },
    // 模型名的样式：不推荐的黄色加粗，GPT 的推荐模型加粗（util.modelTier）
    speakerCls() {
      return modelTier(this.item.model);
    },
    // 气泡头部最前面的花费：这次请求余额实际扣了多少（照常扣钱的 + 由预定付掉的，points.js 的 callCharge）；被打断后接着的那截不再重复写。
    // 免费号、特惠和折扣免掉的（freeOf：绿条不够时免得少）不算进这个数，和账本一致，各项在分项卡片里（split）。
    // 悬停摊开的写入、命中、输出后面跟的是照单价的全价，是分项，不是实扣
    priceEstimated() { return callEstimated(this.item.use, this.item.model, accountOf(this.item.mid), this.item.mid); },
    charge() {
      const p0 = this.item.cont ? null : callPoints(this.item.use, this.item.model, accountOf(this.item.mid), this.item.mid);
      return p0 == null ? null : callCharge(p0, this.item.mid, this.item.ts);
    },
    pts() { return this.charge ? [fen(this.charge.pay)] : []; },
    split() { return this.charge ? chargeSplit(this.charge, fen) : ''; },
    // 这次请求是哪个号发的，写在四个数后面；查不到就说没有记录，不拿现在选的号顶替
    account() { return accountOf(this.item.mid) || '没有记录'; },
    // 用户说的话要不要按 Markdown 和公式渲染（设置里的开关，默认不渲染）
    userMd() { return !!(this.mdUser?.value ?? this.mdUser); },
    // 电脑上右键；手机上安卓长按、iPhone 和 iPad 双击（menu.js 的 on）
    menuEvents() { return this.menu ? this.menu.on(this.onMenu) : {}; },
    fkey() { return foldKey(this.item); },
    // 有没有明文思考（tabs.js 已经去掉了空的思考块），有才画能点的药丸；订阅号照旧只写跳数
    thinkText() { return (this.item.parts || []).some(p => p.kind === 'thinking'); },
    thinkShown() { return thinkOpen.has(this.fkey); },
    thinkOn() { return this.thinkShown && !thinkClosing.has(this.fkey); },   // 药丸的样子：收起一开始小三角就转回去
    // 药丸上的数：DeepSeek 用官方分词器数的 token；数不了的（智谱、a1、a2）只写「思考」，不拿估的数顶上
    thinkLabel() { return this.item.thinkTok ? ' ' + kilo(this.item.thinkTok) : ''; },
    shownParts() {
      const ps = this.item.parts || [];
      return this.thinkShown ? ps : ps.filter(p => p.kind !== 'thinking');
    },
    // 够不够长、值不值得收起（估出来超过四行）：不够长的收起了也看不出变化，菜单里就不给「收起」
    foldable() {
      const it = this.item, cols = textCols.value;
      if (it.kind === 'user') return textLines(it.text || '', cols) + (it.image ? 5 : 0) > FOLD_LINES + 0.5;
      return AI_KINDS.has(it.kind) && rowLines(it, cols) - 1 > FOLD_LINES + 0.5;
    },
    // 正文估出来比一屏还高（行数的估法同 foldable）
    tall() {
      const it = this.item, cols = textCols.value;
      const n = it.kind === 'user' ? textLines(it.text || '', cols) + (it.image ? 5 : 0) : AI_KINDS.has(it.kind) ? rowLines(it, cols) : 0;
      return n > screenLines.value;
    },
    // 默认收着、点开了才展开的：CLI 自己排进来的绿色气泡，和比一屏还高的（在这一页里看着写出来的除外）
    autoFold() {
      const it = this.item;
      return (it.kind === 'user' && it.auto) || (this.tall && !seenLive.has(this.fkey));
    },
    isFolded() {
      if (!this.foldable) return false;
      if (this.autoFold) return !opened.has(this.fkey);
      return folded.has(this.fkey);
    },
    // 收成一行的一步（isStep）：调了哪些工具，按第一次出现的顺序，同一种、同一个结果的合成一个图标和次数；
    // 成功的和失败的分开，不叠在一起（绿的「✎×2」、红的「✎」）
    stepTools() {
      const n = new Map();
      for (const t of toolsOf(this.item)) {
        const r = this.results?.[t.id], st = !r ? 'wait' : r.error ? 'err' : 'ok', k = t.name + ':' + st;
        const g = n.get(k) || { name: t.name, st, n: 0 };
        g.n++;
        n.set(k, g);
      }
      return [...n.entries()].map(([key, g]) => ({ key, name: g.name, n: g.n, d: TOOL_ICON[g.name] || ICON_OTHER, cls: 't-' + g.st,
                                                   tip: `${g.name} ${g.n} 次${STEP_STATE[g.st]}` }));
    },
    // 收成一行的一步写的那句：思考里的进度说明（只有部分回复有）> 正文第一句 > 只调了工具的，按工具补上动词、对象逐个写出来
    // （STEP_VERB：「读 a.py 第 10–60 行、b.js，改 style.css 2 处，运行 跑测试」），都没有就写模型和时间
    stepLine() {
      const parts = this.item.parts || [];
      // 不按标点、不按段落切，整段并成一行，样式里最多排两行、截成省略号；只取开头一截，够排两行就行
      const think = parts.find(p => p.kind === 'thinking' && p.text?.trim());
      if (think) return flat(think.text.slice(0, 400));
      const text = (parts.find(p => p.kind === 'text' && p.text?.trim())?.text || this.item.text || '').trim();
      if (text) return flat(text.slice(0, 400));
      const tools = toolsOf(this.item);
      if (!tools.length) return `${this.speaker} · ${clock(this.item.ts)}`;
      // 对象：文件按完整路径分组、只写文件名；命令写说明，没说明写命令本身
      const target = t => {
        const i = t.input || {};
        if (t.name === 'Bash' && i.description) return flat(i.description);
        return flat(String((TOOL_HEAD[t.name] || (() => ''))(i) || ''));
      };
      const groups = new Map();   // 工具名 → 对象 → 这几次调用
      for (const t of tools) {
        const g = groups.get(t.name) || new Map(), f = target(t);
        g.set(f, [...(g.get(f) || []), t]);
        groups.set(t.name, g);
      }
      return [...groups].map(([name, byT]) => {
        const items = [...byT].map(([f, ts]) => {
          const n = ts.length, shown = FILE_TOOLS.test(name) ? f.split('/').pop() : f;
          if (name === 'Read') {   // 读了哪几段：「第 10–60 行」「第 10–20、80–100 行」，有从头读的就写「全文」
            const spans = ts.map(t => readSpan(t.input || {}));
            if (spans.every(Boolean)) return `${shown} 第 ${spans.join('、')} 行`;
            if (spans.some(Boolean)) return `${shown} ${spans.map(s => (s ? `第 ${s} 行` : '全文')).join('、')}`;
            return n > 1 ? `${shown} ${n} 次` : shown;
          }
          const times = `${n} ${/^(Edit|MultiEdit|NotebookEdit)$/.test(name) ? '处' : '次'}`;   // 同一个文件改了几处、同一件事做了几次
          return n === 1 ? shown : shown ? `${shown} ${times}` : times;
        }).filter(Boolean);
        const v = STEP_VERB[name] || name, all = [...byT.values()].flat();
        // 带说明的命令只有一条：说明本身就是一句话，不再加「运行」；几条之间用分号，说明里常带顿号
        if (name === 'Bash') return all.length === 1 && all[0].input?.description ? items[0] : `${v} ${items.join('；')}`;
        return items.length ? `${v} ${items.join('、')}` : v;
      }).join('，');
    },
    // 收起后「展开全文」后面写的：一共多少字、几个工具
    foldInfo() {
      const it = this.item, text = (it.text || '').trim();
      const tools = toolsOf(it).length;
      return [text && `${text.length} 字`, tools && `${tools} 个工具`, it.image && '图片'].filter(Boolean).join(' · ');
    },
    // 标题栏上的数，算的是整轮（过程加汇报）：用时（第一次请求到汇报那次）、几次请求、几次工具调用、总价
    // （只写余额实际扣掉的一个数，免掉的、预定付掉的在分项卡片里，同请求气泡）。模型、三种 token、每种工具几次放在悬停说明里
    head() {
      const it = this.item;
      if (it.kind !== 'thead') return null;
      let reqs = 0, tools = 0, missing = 0, estimated = false;
      const money = { pay: 0, red: 0, blue: 0, free: 0, why: '免费或折扣免掉' }, models = new Set(), names = {};
      const parts = [['缓存写入', ['in', 'cw']], ['缓存命中', ['cr']], ['输出', ['out']]].map(([label, keys]) => ({ label, keys, n: 0, p: 0 }));
      for (const c of it.calls) {
        // 汇报那次（有汇报时是 calls 的最后一个，见 foldPrefixes）的请求和花费不算进标题栏，写在汇报自己的头上；
        // 用时照旧算到汇报那次
        if (it.report && c === it.calls[it.calls.length - 1]) continue;
        for (const t of toolsOf(c)) {
          tools++;
          names[t.name] = (names[t.name] || 0) + 1;
        }
        if (c.cont || !c.use) continue;   // 被打断后接着的那截和前面是同一次请求
        reqs++;
        if (c.model) models.add(shortModel(c.model));
        const all = callPoints(c.use, c.model, accountOf(c.mid), c.mid);
        estimated ||= callEstimated(c.use, c.model, accountOf(c.mid), c.mid);
        for (const x of parts) {
          x.n += x.keys.reduce((s, k) => s + (c.use[k] || 0), 0);
          const part = Object.fromEntries(x.keys.map(k => [k, c.use[k]]));
          if (x.keys.includes('cw')) part.cw1h = c.use.cw1h;
          const amount = partPoints(part, c.model, accountOf(c.mid));
          if (amount == null) x.unknownPrice = true;
          else x.p += amount;
        }
        if (all == null) { missing++; continue; }
        const ch = callCharge(all, c.mid, c.ts);   // 同请求气泡（见 charge）
        for (const k of ['pay', 'red', 'blue', 'free']) money[k] += ch[k];
      }
      // 还在回复的（it.live）用时算到此刻，每秒更新；次数、总价随着新的气泡进来重算
      const last = it.calls[it.calls.length - 1], ms = (it.live ? nowTick.value : Date.parse(last.ts)) - Date.parse(it.ts);
      const tip = [
        `${[...models].join('、') || '模型不明'} · ${clock(last.ts) !== clock(it.ts) ? clockSpan(it.ts, last.ts) : clock(it.ts)}`,
        parts.map(x => `${x.label} ${kilo(x.n)}${x.unknownPrice ? '' : ' ' + fen(x.p)}`).join(' · '),
        Object.entries(names).sort((a, b) => b[1] - a[1]).map(([n, c]) => `${n}×${c}`).join(' '),
        missing ? `另有 ${missing} 次请求的模型暂无单价，未计入总价` : '',
        it.shut ? `展开工作过程（${it.hidden} 条）` : '收起工作过程',
      ].filter(Boolean).join('\n');
      // 左边那段一句写完：模型和强度、用时、「11 Tool Calls / 7 API Calls」（请求次数和工具次数写成除法），
      // 用「 · 」隔开，同一个灰、不加粗；没调工具的只写请求次数
      const calls = [tools && `${tools} Tool Call${tools > 1 ? 's' : ''}`, `${reqs} API Call${reqs > 1 ? 's' : ''}`].filter(Boolean).join(' / ');
      // 一行写完：用时（th-rest）、次数（th-calls，一直完整），右边总价。不写模型和强度（统一不显示，也不换成两行）
      const dur = ms > 0 ? shortSpan(ms) : '';
      return {
        reqs, tip, calls, rest: dur, total: reqs > missing ? fen(money.pay, 2) + (estimated ? ' 预估' : '') : '',   // 整轮的总价只留到分，分项卡片里照旧三位
        split: chargeSplit(money, fen),
        layers: it.hidden >= 8 ? 2 : 1,   // 背后露几层纸边（样式里最多两层）
      };
    },
  },
  methods: {
    clock,
    kilo,
    cmdLine: it => [it.tags['command-name'] && `${it.tags['command-name']} ${it.tags['command-args'] || ''}`.trim(),
                    it.tags['bash-input'] && `! ${it.tags['bash-input']}`].filter(Boolean).join(' '),
    cmdOut: it => Object.entries(it.tags).filter(([k, v]) => /std(out|err)/.test(k) && v).map(([, v]) => v).join('\n'),
    sysLabel,
    // 头上那行的思考强度常驻、首字母大写（和小标签那一行的写法一样）
    effortCap: e => ({ low: 'Low', medium: 'Medium', high: 'High', xhigh: 'XHigh', max: 'Max' }[e] || e),
    // 在捕获阶段记下，工具卡片里拦住冒泡的按钮也算点到了这个气泡
    // 在展开着的过程里（见 procOf）：每次画都现查，不做成 computed（procOf 不是响应式的，computed 会一直记着第一次的结果）
    isProc() { return procOf.has(this.item); },
    procKey() { return procOf.get(this.item); },   // 这一行属于哪一轮的过程（行上的 data-proc，收起前淡出时按它找）
    // 这一步此刻收成一行：在展开着的过程里、是一次请求的气泡（汇报不在过程里），没被点开过
    isStep() {
      const it = this.item;
      return (it.kind === 'call' || it.kind === 'text') && !it.tail && this.isProc() && !stepOpen.has(this.fkey);
    },
    // 点开了的一步：头上那一整行（最前面朝下的箭头）、左边那道竖线都能点，点了收回一行（只有小箭头太难点中）
    stepOpened() { return this.isProc() && stepOpen.has(this.fkey); },
    // 点开、收回都有高度过渡，做法同 toggleFold
    openStep() {
      const h0 = this.bubbleH();
      stepOpen.add(this.fkey);
      this.growBubble(h0, false);
    },
    shutStep() {
      const h0 = this.bubbleH();
      stepOpen.delete(this.fkey);
      if (tappedUse.value === this.tapId) tappedUse.value = 0;   // 点头上那行时气泡先记成了点选，收起来就不算了
      this.growBubble(h0, true);
    },
    // 拖着选字松手那一下也会来一个点击，不算（不然选着字头上突然多出一行，字往下跳）
    tapUse() { if (!getSelection()?.toString()) tappedUse.value = this.tapId; },
    // 收着的气泡点哪里都是展开，不交给里面的链接、工具卡片；展开着的照旧（点 Claude 气泡摊开四个数，
    // 点用户的气泡浮出顶上那行称呼和时间）
    onBubbleClick(e) {
      if (this.isFolded) {
        e.stopPropagation();
        e.preventDefault();
        return this.toggleFold();
      }
      if (!e.target.closest?.('.think-btn')) this.tapUse();   // 点思考药丸只展开思考，不顺带摊开细项
    },
    // 思考原文那几段自己从 0 高长出来、淡入，下面的回复跟着往下挪；收起倒过来，缩完才拿掉。
    // 不能像 toggleFold 那样只动整个气泡的高度：Claude 的回复没有底色和边框，气泡边缘长高看不见，原文还是一下子冒出来。
    // 气泡是竖排的 flex，段与段之间有间距（6px），段刚出来时用负的下外边距把它后面那道间距也收掉。上外边距不碰：第一段本来就有
    // -4px 的上边距贴近头部（style.css「small + .fold-body > :first-child」），动画里写死成别的值，第一行会先往下走、停下时再跳回去
    // 也不能靠上边距收间距，那样第一行会跟着往下挪。flex 子项的最小高度要写 0，不然缩不下去
    toggleThink() {
      const k = this.fkey;
      if (thinkClosing.has(k)) return;
      const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
      const box = { boxSizing: 'border-box', overflow: 'clip', minHeight: '0px' }, ease = 'cubic-bezier(0.2, 0.8, 0.2, 1)';
      // 一段收着、摊开时的样子：高度和下边距按它此刻的实际值算（下边距自然值减掉段间距）
      const frames = el => {
        const h = el.getBoundingClientRect().height, mb = parseFloat(getComputedStyle(el).marginBottom) || 0;
        const gap = parseFloat(getComputedStyle(el.parentElement.closest('.bubble')).rowGap) || 0;
        return { h, shut: { ...box, height: '0px', marginBottom: `${mb - gap}px`, opacity: 0 },
                 full: { ...box, height: `${h}px`, marginBottom: `${mb}px`, opacity: 1 } };
      };
      const dur = h => Math.round(Math.min(420, 220 + h * 0.1));
      const parts = () => [...(this.$el.querySelectorAll?.(':scope > .bubble .call-part.p-thinking') || [])];
      if (!thinkOpen.has(k)) {
        thinkOpen.add(k);
        if (!reduce) this.$nextTick(() => parts().forEach(el => {
          const { h, shut, full } = frames(el);
          el.animate([shut, full], { duration: dur(h), easing: ease });
        }));
        return;
      }
      const els = reduce ? [] : parts();
      if (!els.length) return thinkOpen.delete(k);
      thinkClosing.add(k);
      const anims = els.map(el => {
        const { h, shut, full } = frames(el);
        return el.animate([full, shut], { duration: dur(h), easing: ease, fill: 'forwards' });
      });
      Promise.allSettled(anims.map(a => a.finished)).then(() => { thinkOpen.delete(k); thinkClosing.delete(k); });
    },
    // 收起只露四行、展开全文
    toggleFold() {
      const k = this.fkey, h0 = this.bubbleH();
      if (this.autoFold) opened.has(k) ? opened.delete(k) : opened.add(k);
      else this.isFolded ? folded.delete(k) : folded.add(k);
      this.growBubble(h0, this.isFolded);
    },
    bubbleH() { return this.$el.querySelector?.(':scope > .bubble')?.getBoundingClientRect().height; },
    // 收起、展开时气泡的高度从原来的过渡到新的：换状态之前量一下（h0），换了、画好以后再量，
    // 期间把气泡定高、裁掉多出来的，后面的行跟着它一起挪。收起时气泡顶上已经翻出屏幕的，把它的顶拉回屏幕里，不然收完人停在下面不知道收到哪了。
    // 收成一行的一步和点开后的气泡是两个元素，所以画好以后重新找气泡，从换之前的高度长到它自己的高度
    growBubble(h0, shut) {
      if (h0 == null || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      this.$nextTick(() => {
        const b = this.$el.querySelector?.(':scope > .bubble');
        if (!b) return;
        const h1 = b.getBoundingClientRect().height, log = b.closest('.log');
        if (Math.abs(h1 - h0) < 2) return;
        if (shut && log) {
          const over = log.getBoundingClientRect().top - b.getBoundingClientRect().top;
          if (over > 0) log.scrollTop -= over + 8;
        }
        const box = { boxSizing: 'border-box', overflow: 'clip' };
        b.animate([{ ...box, height: `${h0}px` }, { ...box, height: `${h1}px` }],
                  { duration: Math.round(Math.min(420, 220 + Math.abs(h1 - h0) * 0.1)), easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
      });
    },
    // 标题栏：点一下收起 / 展开这一轮的过程。展开前记下标题栏后面那一行（汇报，或者更后面的）在哪，展开后从那里挪下去
    toggleCard() {
      const k = this.item.key;
      if (!this.item.shut) return this.growProc(k, false, () => shutPrefix(k));
      const next = this.$el.nextElementSibling, was = next?.getBoundingClientRect().top;
      openPrefix(k);
      this.$nextTick(() => this.growProc(k, true, null, next, was));
    },
    // 展开、收起时灰底整块长出来、缩回去：过程里的行（连同末尾「收起工作过程」）从上往下
    // 像一道帘子按 ease-out 拉开（各行按自己在整块里的位置用 clip-path 依次揭开），后面的行（汇报和更后面的）同一个节奏挪下去；
    // 收起倒过来，帘子往上收、后面的行跟着上来，收完才真的把行拿掉（done），再把估的位移和实际差的那一点补平。
    // 行在屏幕外会跳过渲染，没法包一层容器整体量高、整体动，只能逐行算。设了减少动画就直接收、直接展开
    growProc(k, open, done, next, was) {
      const fin = () => done && done();
      const body = this.$el.closest('.log-body'), log = this.$el.closest('.log');
      if (!body || matchMedia('(prefers-reduced-motion: reduce)').matches) return fin();
      const rows = [...body.querySelectorAll(`:scope > .row[data-proc="${CSS.escape(k)}"]`)];
      if (!rows.length) return fin();
      // 要量的位置（过程里各行、后面和上面露着的行、滚动位置）全部先量完，再一起开动画：
      // 量一下、开一个动画、再量下一行，浏览器每量一次都要把样式重算一遍，过程长的时候点一下要算上百次，手机上卡
      const boxes = rows.map(r => r.getBoundingClientRect());
      const top = boxes[0].top, H = boxes[boxes.length - 1].bottom - top;
      if (H < 1) return fin();
      const D = Math.round(Math.min(480, 200 + H * 0.3));
      // 后面的行：展开时按展开前后量出来的位移，收起时按灰底的高度估；只动眼下看得见的（再往下的挪了也看不见）
      const dy = open ? (was != null && next?.isConnected ? was - next.getBoundingClientRect().top : -H) : -H;
      const view = log?.getBoundingClientRect() ?? { top: 0, bottom: innerHeight };
      const limit = view.bottom + Math.abs(dy);
      const tail = [];
      for (let n = rows[rows.length - 1].nextElementSibling; n && n.getBoundingClientRect().top < limit; n = n.nextElementSibling) tail.push(n);
      // 收起：滚到底附近时，拿掉这一截以后滚动条到不了原来的位置，浏览器会把整页往下推（正文收上去后又往下瞬移）。
      // 先估出会被推下多少（shift），动画里就让上面的行（标题栏和更早的）一起往下挪这么多、后面的行少往上挪这么多，结束时正好落在收完的位置；
      // 动画期间关掉滚动锚定，免得浏览器自己再挪一次。收完把剩下的一点误差用很短的动画补平
      const shift = !open && log ? Math.max(0, log.scrollTop - Math.max(0, log.scrollHeight - H - log.clientHeight)) : 0;
      const head = [];
      if (!open) for (let n = rows[0].previousElementSibling; n && n.getBoundingClientRect().bottom > view.top - shift; n = n.previousElementSibling) head.push(n);
      // 帘子和后面的行用同一条 ease-out 三次曲线、按同样的采样点逐帧写死（各段之间直线插值）：以前各行按自己的起止时刻直线揭开、
      // 后面的行另用 cubic-bezier，两条曲线对不齐，收到最后几帧会有一两行字露在灰底外面、或者被汇报压住
      const N = 24, steps = Array.from({ length: N + 1 }, (_, i) => 1 - (1 - i / N) ** 3);
      const curtain = p => (open ? p : 1 - p) * H;   // 灰底下沿离整块顶多远：展开从 0 拉到 H，收起从 H 收回 0
      const anims = rows.map((r, i) => {
        const y0 = boxes[i].top - top, h = boxes[i].height;
        // 这一行底下盖掉多少：帘子在这一行上面就整行盖住，在它下面就全露出来
        return r.animate(steps.map(p => ({ clipPath: `inset(0 0 ${Math.min(h, Math.max(0, y0 + h - curtain(p)))}px 0)` })),
                         { duration: D, easing: 'linear', fill: open ? 'none' : 'forwards' });
      });
      const slide = (els, to) => els.map(e => e.animate(steps.map(p => ({ transform: `translateY(${(open ? 1 - p : p) * to}px)` })),
                                                        { duration: D, easing: 'linear', fill: open ? 'none' : 'forwards' }));
      if (open) { slide(tail, dy); return fin(); }
      if (log) log.style.overflowAnchor = 'none';
      const moves = [...slide(tail, dy + shift), ...(shift ? [...slide(head, shift), ...slide(rows, shift)] : [])];
      Promise.all([...anims, ...moves].map(x => x.finished)).catch(() => {}).then(() => {
        const refs = [tail[0], head[0]].filter(Boolean), vis = refs.map(e => e.getBoundingClientRect().top);   // 收完前它们眼下（挪过以后）在哪
        fin();
        this.$nextTick(() => {
          moves.forEach(x => x.cancel());
          refs.forEach((ref, i) => {
            const off = ref.isConnected ? vis[i] - ref.getBoundingClientRect().top : 0;
            if (Math.abs(off) > 0.5) (ref === tail[0] ? tail : head).forEach(e => e.isConnected
              && e.animate([{ transform: `translateY(${off}px)` }, { transform: 'none' }], { duration: 120, easing: 'ease-out' }));
          });
          if (log) log.style.overflowAnchor = '';
        });
      });
    },
    // 从「收起工作过程」收、而标题栏已经滚出屏幕上沿时用：过程里的行淡出再收起，汇报留在原处（见 shutFromFoot）。
    // 这时候整块缩回去，汇报会跟着飞出屏幕，看不出是怎么收的
    fadeProc(k, done) {
      const body = this.$el.closest('.log-body');
      const els = body && !matchMedia('(prefers-reduced-motion: reduce)').matches
        ? [...body.querySelectorAll(`:scope > .row[data-proc="${CSS.escape(k)}"]`)] : [];
      if (!els.length) return done();
      Promise.all(els.map(e => e.animate([{ opacity: 1 }, { opacity: 0, transform: 'translateY(-4px)' }],
                                         { duration: 140, easing: 'ease-in', fill: 'forwards' }).finished))
        .catch(() => {}).then(done);
    },
    // 过程末尾那行「收起工作过程」：收起后汇报留在屏幕上原来的位置，看着像过程卷回了标题栏。
    // 这一行收起后就没了（组件跟着卸掉），所以先把要用的取出来
    shutFromFoot() {
      if (this.item.reportSeq == null) return this.shutCard(this.item.key);   // 没有汇报：同从过程里收起，把标题栏拉进视野
      const log = this.$el.closest('.log'), seq = this.item.reportSeq, k = this.item.key;
      const top = () => log?.querySelector(`.log-body > .row[data-seq="${seq}"]`)?.getBoundingClientRect().top;
      // 标题栏还在屏幕里：和点标题栏一样整块缩回去；滚出去了才淡出、把汇报留在原处
      const head = log?.querySelector(`.bubble.thead[data-fold="${CSS.escape(k)}"]`)?.getBoundingClientRect();
      if (head && head.top >= log.getBoundingClientRect().top) return this.growProc(k, false, () => shutPrefix(k));
      this.fadeProc(k, () => {
        const was = top();
        shutPrefix(k);
        this.$nextTick(() => {
          const now = top();
          if (log && was != null && now != null) log.scrollTop += now - was;
        });
      });
    },
    // 从过程里某个气泡的菜单收起：按着的那个气泡跟着消失，把标题栏拉进视野，不然像是跳到了别处
    shutCard(k) {
      const log = this.$el.closest('.log');
      this.growProc(k, false, () => {
        shutPrefix(k);
        this.$nextTick(() => log?.querySelector(`.bubble.thead[data-fold="${CSS.escape(k)}"]`)?.scrollIntoView({ block: 'nearest' }));
      });
    },
    // 菜单：第一项是收起 / 展开（不分点在哪：一轮回复先收过程、再收汇报，展开先放出过程），然后是正文、引用，点在代码块上时还能只复制那一段
    onMenu(e) {
      const it = this.item, close = fn => () => { this.menu.close(); fn(); };
      if (it.kind === 'thead') {
        return this.menu.open(e, [it.shut ? { label: '展开工作过程', hint: `${it.hidden} 个气泡`, run: close(this.toggleCard) }
          : { label: '收起工作过程', hint: it.report ? '只留汇报' : '', run: close(this.toggleCard) }]);
      }
      if (it.kind === 'tfoot') {
        return this.menu.open(e, [{ label: '收起工作过程', hint: it.reportSeq != null ? '只留汇报' : '', run: close(this.shutFromFoot) }]);
      }
      const text = (it.text || '').trim(), k = this.fkey, card = prefixOf.get(k) || tailOf.get(k), isTail = tailOf.has(k);
      const items = [];
      if (this.isFolded) items.push({ label: '展开全文', run: close(this.toggleFold) });
      if (card && !cardShut.get(card)) {
        items.push({ label: '收起工作过程', hint: cardReport.get(card) ? '只留汇报' : '', run: close(() => this.shutCard(card)) });
      }
      else {
        if (!this.isFolded && this.foldable && (!card || isTail)) {
          items.push({ label: isTail ? '收起汇报' : '收起', hint: `只留${FOLD_LINES}行`, run: close(this.toggleFold) });
        }
        if (card) items.push({ label: '展开工作过程', run: close(() => openPrefix(card)) });
      }
      if (text) {
        items.push({ label: '复制正文', run: () => this.menu.copy(text, '正文') });
        if (this.canQuote()) items.push({ label: '引用回复', hint: '填进输入框', run: () => this.menu.quote(text) });
      }
      const here = e.target.closest('pre');
      const all = [...this.$el.querySelectorAll('pre')];
      if (here) items.push({ label: '复制这段代码', run: () => this.menu.copy(here.innerText, '代码') });
      if (all.length > (here ? 1 : 0)) {
        items.push({ label: `复制全部代码`, hint: `${all.length} 段`,
                     run: () => this.menu.copy(all.map(p => p.innerText).join('\n\n'), '代码') });
      }
      if (items.length) this.menu.open(e, items);
    },
  },
  template: `
    <div class="row" :class="['k-' + item.kind, { side: item.sidechain, proc: isProc(), open: item.kind === 'thead' && !item.shut }]"
         :data-proc="isProc() ? procKey() : item.kind === 'tfoot' ? item.key : null">
      <template v-if="item.kind === 'user'">
        <div class="bubble me" :class="{ folded: isFolded }" :data-fold="fkey" v-on="menuEvents" @click.capture="onBubbleClick">
          <small class="me-head" :class="{ shown: showUse }">{{ item.queued ? '插话' : $who.me }} · {{ clock(item.ts) }}</small>
          <!-- 简洁模式：气泡外面左边、和最后一行字对齐的灰色时间（最新一条一直在，别的悬停才出来，见 style.css 的 .me-time） -->
          <small class="me-time" :class="{ queued: item.queued, shown: showUse }"><span v-if="item.queued" class="mt-q">插话</span><span
            v-for="s in clock(item.ts).split(' ')" :key="s">{{ s }}</span></small>
          <div class="fold-body">
            <img v-if="item.image" :src="item.image" loading="lazy">
            <div v-if="userMd" class="md" v-md="item"></div>
            <div v-else-if="item.text" class="plain">{{ item.text }}</div>
          </div>${FOLD_BTNS}
        </div>
      </template>
      <!-- 一轮回复的标题栏（foldPrefixes）：箭头、工作过程、「8m 9s · 11 Tool Calls / 7 API Calls」（th-rest、th-calls 两格，一行写完），右边只有总价；
           汇报那次的请求和花费不算进来，写在汇报自己的头上，「0.03¥」靠右
           （英文和符号，1 次写单数）
           （整轮的总价接在请求次数后面连着读，和过程里每一步最右边那列花费对齐）；点它收起 / 展开中间的过程。
           收着时背后往上露出错开的纸边（过程越长层数越多），像汇报后面压着一摞没摊开的气泡 -->
      <div v-else-if="item.kind === 'thead'" class="bubble ai thead" :class="item.shut ? ['shut', 'stack' + head.layers] : []"
           :data-fold="item.key" v-on="menuEvents" @click="toggleCard">
        <svg class="th-chev" viewBox="0 0 16 16"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg><b class="th-title">工作过程</b><span
          class="th-meta th-line"><span v-if="head.rest" class="th-rest">{{ head.rest }}</span></span><span
          class="th-meta th-calls" :class="{ sep: head.who || head.rest }">{{ head.calls }}</span><span v-if="head.total" class="th-meta th-cost"><b
          class="pts th-price" v-split="head.split">{{ head.total }}</b></span>
      </div>
      <!-- 过程展开时，过程末尾、汇报前面的一行「收起工作过程」：和标题栏一头一尾，箭头朝上 -->
      <div v-else-if="item.kind === 'tfoot'" class="bubble ai tfoot" :data-fold="item.key"
           v-on="menuEvents" @click="shutFromFoot" role="button" aria-label="收起工作过程">
        <!-- 只留一个居中的朝上箭头，不写字；读屏照旧念「收起工作过程」 -->
        <svg class="th-chev" viewBox="0 0 16 16"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg>
      </div>
      <!--一次 API 请求一个气泡。头部最前面是这次请求的点数（常驻），四个数电脑上悬停才显示，
           手机上点到气泡里任何地方就摊开；思考跳数（只有网页对话记得到）一串一颗药丸，接在头部时间后面，不另占一行。
           下面依次是正文和工具，每段带着自己的序号（找话跳转能定位进来）。
           标题栏收着过程时的汇报（item.tail）不写头部：价格在标题栏的总价里 -->
      <!-- 展开着的工作过程里收成一行的一步（isStep）：箭头、一句话（stepLine）、各工具的图标×次数、这一步的花费；点一下原样展开 -->
      <!-- 排成一段：箭头、工具图标（次数是图标右上角的角标，不占宽度）、那段话依次接着写，
           最多两行、截成省略号，第二行从最左边顶格起。花费靠右浮动在第一行的右上角（几步的花费上下对齐成一列，和点开后右上角的花费、
           标题栏右边的总价对齐），第二行从它底下一直写到最右边再打省略号。浮动要包在里面那层普通的块里（.step-flow）：
           直接放进做截断的 -webkit-box 里会变成一个盒子项、浮不起来 -->
      <div v-else-if="isStep()" class="bubble ai step" :data-fold="fkey" v-on="menuEvents" @click="openStep">
        <div class="step-body"><div class="step-flow"><span v-if="pts.length" class="step-pts">${PTS_BODY}</span><svg class="th-chev"
          viewBox="0 0 16 16"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg><span v-if="stepTools.length" class="step-tools"><span
          v-for="t in stepTools" :key="t.key" class="step-tool" :class="t.cls"><svg
          class="ticon" viewBox="2 2 12 12"><path :d="t.d"/></svg><sup v-if="t.n > 1" class="step-n">{{ t.n }}</sup></span></span>{{ stepLine }}</div></div>
      </div>
      <div v-else-if="item.kind === 'call'" class="bubble ai call" :class="{ folded: isFolded, tail: item.tail, picked: showUse, 'step-open': stepOpened() }" :data-fold="fkey"
           v-on="menuEvents" @click.capture="onBubbleClick">
        <span v-if="stepOpened()" class="step-rail" aria-label="收成一行" @click.stop="shutStep"></span><small class="ai-head"
          :class="{ shown: headOpen, 'step-head': stepOpened() }" @click="stepOpened() && shutStep()"><svg
          v-if="stepOpened()" class="th-chev step-less" viewBox="0 0 16 16"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg>${PTS_HEAD}<span :class="speakerCls || null">{{ speaker }}</span><span v-if="item.effort && !item.cont"
          class="effort"> · {{ effortCap(item.effort) }}</span> · {{ clock(item.ts) }}<span v-if="thinkText" class="think-run think-btn" :class="{ open: thinkOn }" role="button" :aria-expanded="thinkOn"
          @click.stop="toggleThink">思考{{ thinkLabel }}<svg class="think-tri" viewBox="0 0 8 8"><path d="M2 1 6.5 4 2 7z"/></svg></span><span v-else v-for="(n, k) in item.think"
          :key="'think' + k" class="think-run">思考 {{ n }} 跳</span><em v-if="item.sidechain"> · 子代理</em><span v-if="useParts.length && !item.cont" class="use" :class="{ shown: showUse, fading: fadeUse }"> · <template
          v-for="(u, k) in useParts" :key="u.label">{{ k ? ' · ' : '' }}{{ u.label }} {{ u.n }}<b v-if="u.p" class="pts">{{ u.p }}</b></template> · 账号 {{ account }}</span></small>
        <div class="fold-body">
        <div v-for="p in shownParts" :key="p.seq" class="call-part"
             :class="['p-' + p.kind, { side: p.sidechain }]"
             :data-seq="p.seq">
          <div v-if="p.kind === 'text'" class="md" v-md="p"></div>
          <div v-else-if="p.kind === 'thinking'" class="md think-body" v-md="p"></div>
          <tool-card v-else-if="p.kind === 'tool'" :item="p" :result="results?.[p.id]"></tool-card>
        </div>
        </div>${FOLD_BTNS}
      </div>
      <div v-else-if="item.kind === 'text'" class="bubble ai" :class="{ folded: isFolded, tail: item.tail, picked: showUse, 'step-open': stepOpened() }" :data-fold="fkey"
           v-on="menuEvents" @click.capture="onBubbleClick">
        <span v-if="stepOpened()" class="step-rail" aria-label="收成一行" @click.stop="shutStep"></span><small class="ai-head"
          :class="{ shown: headOpen, 'step-head': stepOpened() }" @click="stepOpened() && shutStep()"><svg
          v-if="stepOpened()" class="th-chev step-less" viewBox="0 0 16 16"><path d="m6 3.5 4.5 4.5L6 12.5"/></svg>${PTS_HEAD}<span :class="speakerCls || null">{{ speaker }}</span><span v-if="item.effort"
          class="effort"> · {{ effortCap(item.effort) }}</span> · {{ clock(item.ts) }}<em v-if="item.sidechain"> · 子代理</em><span v-if="useParts.length" class="use" :class="{ shown: showUse, fading: fadeUse }"> · <template
          v-for="(u, k) in useParts" :key="u.label">{{ k ? ' · ' : '' }}{{ u.label }} {{ u.n }}<b v-if="u.p" class="pts">{{ u.p }}</b></template> · 账号 {{ account }}</span></small>
        <div class="fold-body"><div class="md" v-md="item"></div></div>${FOLD_BTNS}
      </div>
      <details v-else-if="item.kind === 'thinking'" class="thinking">
        <summary>思考</summary>
        <div class="md" v-md="item"></div>
      </details>
      <!-- 没有回复编号、进不了请求气泡的工具调用单独一行，画法一样 -->
      <tool-card v-else-if="item.kind === 'tool'" :item="item" :result="result"></tool-card>
      <!-- 系统小条一律居中一行灰字：有正文的（命令输出、系统消息原文）收成一行，点开在下面摊开灰底框 -->
      <details v-else-if="item.kind === 'command' && cmdOut(item)" class="sys">
        <summary><code>{{ cmdLine(item) }}</code></summary>
        <pre>{{ cmdOut(item) }}</pre>
      </details>
      <div v-else-if="item.kind === 'command'" class="line"><code>{{ cmdLine(item) }}</code></div>
      <div v-else-if="item.kind === 'duration'" class="line">本轮用时 {{ (item.ms / 1000).toFixed(1) }} 秒</div>
      <div v-else-if="item.kind === 'notice'" class="line">{{ item.text }}</div>
      <!-- 到达限额：查看器的预测拦截让 CLI 停在这里（仿照官方的写法，CLI 自己记进记录的，见 chat.Driver._after_tool） -->
      <div v-else-if="item.kind === 'limit'" class="line limit-line">{{ item.text }}</div>
      <!-- 提交前的钩子把一句话拦下了，记录里没有这句话（见 session.on_prompt_blocked）：一直看得到，原话可以放回输入框重发 -->
      <div v-else-if="item.kind === 'blocked'" class="line limit-line">
        {{ item.text }}
        <button v-if="item.prompt && menu && canQuote()" type="button" @click="menu.refill(item.prompt)">放回输入框</button>
      </div>
      <!-- 后台任务的通知：CLI 自动塞进来的，不是用户说的话（不管 Claude 当时忙不忙，都是这一行灰字，见 session.on_task）。
           监视器的通知不代表结束，写「监视到」和事件内容 -->
      <div v-else-if="item.kind === 'task'" class="line">
        <template v-if="item.event">监视到：{{ item.text }} · {{ item.event }}</template>
        <template v-else>后台任务{{ { completed: '完成', failed: '失败', killed: '被终止' }[item.status] || item.status || '结束' }}：{{ item.text }}</template>
      </div>
      <div v-else-if="item.subtype === 'away_summary'" class="line">{{ item.text }}</div>
      <!-- 认得出的 API 报错（session.api_error_hint）：一行中文说明，悬停看原文；不归在系统消息里，一直显示 -->
      <div v-else-if="item.kind === 'apierr'" class="line limit-line">{{ item.text }}</div>
      <div v-else-if="item.kind === 'continued'" class="line">
        会话在新文件中继续 <button @click="$emit('open', item.path)">打开</button>
      </div>
      <details v-else-if="item.text" class="sys" :class="item.level">
        <summary>{{ sysLabel(item) }}</summary>
        <pre>{{ item.text }}</pre>
      </details>
      <div v-else class="line" :class="item.level">{{ sysLabel(item) }}</div>
    </div>`,
};
