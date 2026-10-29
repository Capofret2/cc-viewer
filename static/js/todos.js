// 「待办」页（后端见 todos.py）：侧边栏「新会话」底下那颗药丸进来，是对话区里的一种页面，
// 和新会话页、会话同级（不是能叉掉的弹窗）：盖住各对话区，顶上浮着的圆按钮、往右划拉出侧边栏照旧；侧边栏里换到别的会话就换走。
// 紧凑列表：层级 1 两行（标题、现状），下级缩进列在上级底下、只占一行；重要性缩成右边一个「高 / 中 / 低」小方块，时限只写还剩几天，
// 推断的（src 不是 user）画虚线，和用户定了的分开。顶上一颗输入框打字回车就新建一条，新建的那条当场展开，点选重要性、时限、分组，不点也行。
// 点一行展开：现状、描述、进展、不清楚的事，可以改的几样，以及挂着的会话（正在做、从这里开出、记录过、查过），点会话就打开它。
// 状态、重要性、时限各是一根滑动药丸（和思考强度的 .effort-seg 同一种），标题点铅笔就地改（别堆一排排小药丸、别把标题再抄一行输入框）。
// 正在改的字（标题、分组）放在 td.edit 里、不直接绑待办：整页模板任何一处一变就重画，绑 :value 的输入框会被冲回原来的字，15 秒的刷新更是一定冲掉
// 左边圆圈多选，底下浮出「发送到新会话」：把选中的几条写进新会话的输入框，发出去时服务器记上从它们开出、替它认领（server.Birth）。
// 会话里的 Claude 用 cctodo 改的是同一份；页面开着时每 15 秒重新取一次，Claude 写的进展、会话的状态点跟着变。
// 网页文件两个实例现读：后端还没有待办时 /api/me 不给 todos，入口不出现，别的流程不受影响
// 清单入口的做法（todos.py 开头）：条目是用户工作区里的文件，后端照旧给同一个形状（一份清单当成一个分组，没有层级）；
// 页面上的改动后端攒成操作、标「待同步」，AI 下次读清单时写进文件。lists 是登记的几份清单，读不出来、格式不对的写着原因，给一颗「让 AI 修好」。
// 旧后端不给 lists（listsKnown 是 false），页面照旧
const { reactive, computed, watch, nextTick } = Vue;
import { api, me, say } from './util.js';

// 状态都写三个字、重要性五档两个字，和 todos.py 一致；旧后端只有 high、mid、low 三档，照样认
export const TODO_STATUS = { todo: '未开始', doing: '工作中', waiting: '等待中', done: '已完成', dropped: '暂搁置' };
export const TODO_IMP = { critical: '关键', high: '重要', mid: '一般', low: '次要', minor: '可选' };
export const TODO_EFFORT = { hours: '几小时', days: '几天', weeks: '几周' };
const OPEN = ['todo', 'doing', 'waiting'];
const REFRESH_MS = 15000;

const pad = n => String(n).padStart(2, '0');
const isoDay = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const addDays = n => { const d = new Date(); d.setDate(d.getDate() + n); return isoDay(d); };
// 快捷时限：本周是到这周日（周日当天就是今天）
const DUE_CHOICES = () => [['本周', addDays((7 - new Date().getDay()) % 7)], ['两周内', addDays(14)], ['一个月内', addDays(30)]];

export function useTodos({ openTab, scroller, openFresh, afterDraft }) {
  const td = reactive({
    open: false, loading: false, loaded: false, error: '', groups: [], items: [], sessions: {}, max: { title: 80, group: 30 },
    lists: [], listsKnown: false,   // 登记的清单（新后端才给）
    filter: 'all', picked: [], expanded: '', fresh: '', draft: '', busy: '', shut: { '': false, __done: true },
    edit: { id: '', key: '', text: '', orig: '' },   // 正在改哪条的哪一样（title、group）、改成了什么、原来是什么
    sub: { id: '', text: '' },   // 正在给哪条加子项、打了什么
    ppick: '',   // 摊开了「上级」候选的那条
    cal: { id: '', y: 0, m: 0 },   // 摊开了月历的那条、翻到哪年哪月
    unfold: [],    // 展开了子项的上级
    bodyOpen: [],  // 描述展开看全文的那几条（默认只露几行）
  });
  let timer = 0;

  async function loadTodos() {
    if (td.loading) return;
    td.loading = true;
    try {
      const r = await api('/api/todos');
      Object.assign(td, { groups: r.groups, items: r.items, sessions: r.sessions, max: r.max || td.max, error: '', loaded: true,
                          lists: r.lists || [], listsKnown: Array.isArray(r.lists) });
      // 已经没有了、做完了的不再算选中
      td.picked = td.picked.filter(id => r.items.some(it => it.id === id && OPEN.includes(it.status)));
    } catch (e) { td.error = `无法获取待办：${e.message}`; }
    td.loading = false;
  }
  // 知道是谁以后取一次（侧边栏药丸上写着几条在做），登录、退出、换人以后重新取
  watch(() => me.loaded && me.todos && [me.name], v => v && loadTodos(), { immediate: true });

  // 顶上浮着的圆按钮离开最顶上才有细边（html.log-away-top，平时由 minimap.js 按对话区挂）：待办页是自己的一页，
  // 开着时按待办页自己滚到哪算（onTodoScroll），刚打开在最顶上；收起时按底下那个对话区重算（不然细边还取决于之前的会话）
  const awayTop = on => document.documentElement.classList.toggle('log-away-top', on);
  const onTodoScroll = e => awayTop(e.target.scrollTop > 2);
  function openTodos() {
    if (!td.open) awayTop(false);
    td.open = true;
    loadTodos();
    clearInterval(timer);
    timer = setInterval(() => td.open && !document.hidden && loadTodos(), REFRESH_MS);
  }
  function closeTodos() {
    if (!td.open) return;
    td.open = false;
    clearInterval(timer);
    nextTick(() => awayTop((scroller.value?.scrollTop || 0) > 2));
  }

  const daysLeft = r => (r ? Math.round((new Date(`${r.v}T00:00`) - new Date(`${isoDay(new Date())}T00:00`)) / 864e5) : null);
  const dueText = r => { const n = daysLeft(r); return n === null ? '' : n > 0 ? `${n}天` : n === 0 ? '今天' : `过${-n}天`; };
  const pass = it => td.filter === 'all' || (td.filter === 'doing' ? it.status === 'doing' : !!it.deadline);
  // 子项默认折着：上级那行写有几个子项，td.unfold 里有它才列出下面的；筛「工作中」「有时限」时不折，符合的都列
  const kidsOf = computed(() => {
    const open = td.items.filter(it => OPEN.includes(it.status));
    const ids = new Set(open.map(it => it.id)), kids = new Map();
    for (const it of open) {
      const p = ids.has(it.parent) ? it.parent : null;
      if (!kids.has(p)) kids.set(p, []);
      kids.get(p).push(it);
    }
    return kids;
  });
  // 排序（综合时限和重要性；系数是试用的经验值）：
  // 一条的分 = 重要性权重 ×（1 + 2 × 时间压力）× 层级折扣。权重关键 5 到可选 1，没定的 2.5（导进来的一半没定，不能全沉底）；
  // 时间压力 7 /（剩几天 + 7），今天到期（过期也算今天）乘 3、一周乘 2、一个月乘 1.38，没时限乘 1；层级 2 打六折、3 打三折，子项盖不过主线。
  // 同一层里按「排名分」从高到低：自己的分和子项里最高排名分的八成取大的，子项里有急事上级也跟着靠前；一样高的先建的在前。
  // 分组按组里最高的一条加上其余各条之和的 0.15 倍排（一件急事就能把整组顶上去，事多的组也靠前一点），未分组一样参与；已完成永远在最底下
  const IMP_W = { critical: 5, high: 4, mid: 3, low: 2, minor: 1 }, LEVEL_K = { 1: 1, 2: 0.6, 3: 0.3 };
  const score = it => {
    const d = daysLeft(it.deadline), p = d === null ? 0 : 7 / (Math.max(d, 0) + 7);
    return (IMP_W[it.importance?.v] ?? 2.5) * (1 + 2 * p) * (LEVEL_K[it.level] ?? 1);
  };
  const rankOf = computed(() => {
    const kids = kidsOf.value, memo = new Map();
    const rank = it => {
      if (!memo.has(it.id)) memo.set(it.id, Math.max(score(it), ...(kids.get(it.id) || []).map(k => 0.8 * rank(k))));
      return memo.get(it.id);
    };
    for (const it of td.items) if (OPEN.includes(it.status)) rank(it);
    return memo;
  });
  const cmp = (a, b) => (rankOf.value.get(b.id) ?? score(b)) - (rankOf.value.get(a.id) ?? score(a)) || a.created - b.created;
  // 分组里按上下级排好：[{ name, rows: [{ it, depth, kids }] }]；做完的另成最底下一组，按完成时间新的在前
  const sections = computed(() => {
    const kids = kidsOf.value, fold = td.filter === 'all';
    const order = ['', ...td.groups], out = new Map(order.map(g => [g, []])), all = new Map(order.map(g => [g, []]));
    const walk = (it, depth, g) => {
      const ks = kids.get(it.id) || [];
      all.get(g).push(score(it));
      if (pass(it)) out.get(g).push({ it, depth, kids: ks.length });
      // 折着的子项不列，分照样算进组里
      const shown = !fold || td.unfold.includes(it.id);
      for (const k of [...ks].sort(cmp)) shown ? walk(k, depth + 1, g) : tally(k, g);
    };
    const tally = (it, g) => { all.get(g).push(score(it)); for (const k of kids.get(it.id) || []) tally(k, g); };
    for (const it of (kids.get(null) || []).sort(cmp)) {
      if (!out.has(it.group)) { out.set(it.group, []); all.set(it.group, []); order.push(it.group); }
      walk(it, 0, it.group);
    }
    const groupScore = s => (s.length ? Math.max(...s) + 0.15 * (s.reduce((a, b) => a + b, 0) - Math.max(...s)) : 0);
    const list = [...out].filter(([, rows]) => rows.length)
      .map(([name, rows]) => ({ name, label: name || '未分组', rows, s: groupScore(all.get(name)) }))
      .sort((a, b) => b.s - a.s || order.indexOf(a.name) - order.indexOf(b.name));
    const done = td.items.filter(it => !OPEN.includes(it.status) && td.filter === 'all').sort((a, b) => (b.done || 0) - (a.done || 0));
    if (done.length) list.push({ name: '__done', label: '已完成', rows: done.map(it => ({ it, depth: 0 })) });
    return list;
  });
  const counts = computed(() => {
    const open = td.items.filter(it => OPEN.includes(it.status));
    return { all: open.length, doing: open.filter(it => it.status === 'doing').length, due: open.filter(it => it.deadline).length };
  });
  const toggleSection = name => (td.shut[name] = !td.shut[name]);
  // 点一行：有折着的子项先把子项展开，再点一次才是展开这条来改；收起子项点右边那个「几个子项」
  const toggleKids = it => (td.unfold = td.unfold.includes(it.id) ? td.unfold.filter(x => x !== it.id) : [...td.unfold, it.id]);
  function clickRow(r) {
    if (r.kids && td.filter === 'all' && !td.unfold.includes(r.it.id) && td.expanded !== r.it.id) return toggleKids(r.it);
    toggleTodo(r.it);
  }
  const toggleTodo = it => {
    td.expanded = td.expanded === it.id ? '' : it.id;
    if (td.expanded !== td.fresh) td.fresh = '';   // 刚新建的那条只在它还展开着时标出来
    // 换了展开的那条，摊开的候选、月历、打到一半的子项都收起
    Object.assign(td, { ppick: '', cal: { id: '', y: 0, m: 0 }, sub: { id: '', text: '' } });
  };
  const togglePick = it => {
    if (!OPEN.includes(it.status)) return;
    td.picked = td.picked.includes(it.id) ? td.picked.filter(x => x !== it.id) : [...td.picked, it.id];
  };

  function replace(it) {
    const i = td.items.findIndex(x => x.id === it.id);
    if (i >= 0) td.items[i] = it;
    else td.items.push(it);
    if (it.group && !td.groups.includes(it.group)) td.groups.push(it.group);
  }
  async function quickAdd() {
    const title = td.draft.trim();
    if (!title || td.busy) return;
    td.busy = 'add';
    try {
      const it = await api('/api/todos', { method: 'POST', body: { title } });
      replace(it);
      Object.assign(td, { draft: '', expanded: it.id, fresh: it.id, filter: 'all' });
      td.shut[''] = false;
    } catch (e) { say(`新建待办失败：${e.message}`, { err: true }); }
    td.busy = '';
  }
  // 改几样：网页上点的算用户定的（src: user）。再点一次已经选中的那项是清掉
  async function setTodo(it, patch) {
    if (td.busy) return;
    td.busy = `set:${it.id}`;
    try {
      replace(await api(`/api/todos/${it.id}`, { method: 'PUT', body: patch }));
      if (patch.status && !OPEN.includes(patch.status)) td.picked = td.picked.filter(x => x !== it.id);
    } catch (e) { say(`修改「${it.title}」失败：${e.message}`, { err: true }); }
    td.busy = '';
  }
  const rate = (it, field, v) => setTodo(it, { [field]: it[field]?.v === v && it[field]?.src === 'user' ? null : { v, src: 'user' } });

  // 就地改标题、分组：开始改时把原来的字抄进 td.edit，回车、点开别处存，Esc 不存
  const editing = (it, key) => td.edit.id === it.id && td.edit.key === key;
  function startEdit(it, key) {
    if (editing(it, key)) return;
    td.edit = { id: it.id, key, text: it[key] || '', orig: it[key] || '' };
    if (key === 'title') nextTick(() => document.querySelector('.todo-title-in')?.focus());
  }
  function endEdit(save = true) {
    const { id, key, text } = td.edit;
    if (!id) return;
    td.edit = { id: '', key: '', text: '', orig: '' };
    const it = td.items.find(x => x.id === id), v = text.trim();
    if (!save || !it || v === (it[key] || '') || (key === 'title' && !v)) return;
    setTodo(it, { [key]: v });
  }
  // 分组的候选：未分组和已有的分组，动手打了字才按打的字筛（刚点开时框里是原来的分组名，照样列全）；打的是没有的名字，最上面一项就是新建它。
  // 点一项（按下时不让输入框失焦，免得先存了打到一半的字）就写进去、存掉
  function groupChoices() {
    const q = td.edit.text === td.edit.orig ? '' : td.edit.text.trim(), all = td.groups.filter(g => !q || g.includes(q));
    const out = (!q ? [{ name: '', label: '未分组' }] : []).concat(all.map(g => ({ name: g, label: g })));
    if (q && !td.groups.includes(q)) out.unshift({ name: q, label: `新建分组「${q}」` });
    return out;
  }
  function pickGroup(name) {
    td.edit.text = name;
    endEdit();
    document.activeElement?.blur();
  }
  // 加子项（网页上也能加）：卡片底下「＋ 添加子项」换成一颗输入框，回车就在这条下面建一条，
  // 分组跟着这条、层级低一级（最低到 3），建好当场展开
  function openSub(it) {
    td.sub = { id: it.id, text: '' };
    nextTick(() => document.querySelector('.todo-sub-in')?.focus());
  }
  async function addSub(it) {
    const title = td.sub.text.trim();
    if (!title || td.busy) return;
    td.busy = 'sub';
    try {
      const kid = await api('/api/todos', { method: 'POST', body: { title, parent: it.id, group: it.group, level: Math.min(3, it.level + 1) } });
      replace(kid);
      Object.assign(td, { sub: { id: '', text: '' }, expanded: kid.id, fresh: kid.id, filter: 'all' });
      if (!td.unfold.includes(it.id)) td.unfold.push(it.id);   // 新的子项要看得见
    } catch (e) { say(`给「${it.title}」添加子项失败：${e.message}`, { err: true }); }
    td.busy = '';
  }
  // 改上级：候选是「无」和同组还没做完的待办，去掉自己和自己的下级（挂过去会成环）；挂到哪条底下，分组跟着那条
  const parentTitle = it => td.items.find(x => x.id === it.parent)?.title || '';
  function parentChoices(it) {
    const under = x => { for (let at = x; at; at = td.items.find(y => y.id === at.parent)) if (at.id === it.id) return true; return false; };
    return [{ id: '', label: '无' }].concat(td.items.filter(x => x.group === it.group && OPEN.includes(x.status) && !under(x))
      .sort(cmp).map(x => ({ id: x.id, label: x.title })));
  }
  const toggleParentPick = it => (td.ppick = td.ppick === it.id ? '' : it.id);
  // 浮出来的月历、父项候选：点它们所在那一行以外的地方就收起（那几行自己拦下了点击）
  const closePops = () => { if (td.ppick || td.cal.id) Object.assign(td, { ppick: '', cal: { id: '', y: 0, m: 0 } }); };
  // 描述短（三行、一百来字以内）就整段摊开，不折、不写「展开」
  const bodyLong = it => { const b = (it.body || '').trim(); return b.split('\n').length > 3 || b.length > 120; };
  const toggleBody = it => (td.bodyOpen = td.bodyOpen.includes(it.id) ? td.bodyOpen.filter(x => x !== it.id) : [...td.bodyOpen, it.id]);
  function pickParent(it, id) {
    td.ppick = '';
    if (id === (it.parent || '')) return;
    const p = td.items.find(x => x.id === id);
    setTodo(it, p ? { parent: id, ...(p.group !== it.group ? { group: p.group } : {}) } : { parent: null });
  }
  // 时限最后一段「日期」：在那一行底下摊开自己画的月历（浏览器自带的日期面板太复古）。
  // 一周从周一起；选中的那天主题色圆底，今天一圈细边；底下「清除」「今天」两颗药丸；‹ › 翻月
  function openCal(it) {
    if (td.cal.id === it.id) return (td.cal = { id: '', y: 0, m: 0 });
    const d = it.deadline ? new Date(`${it.deadline.v}T00:00`) : new Date();
    td.cal = { id: it.id, y: d.getFullYear(), m: d.getMonth() };
  }
  function calMove(n) {
    const d = new Date(td.cal.y, td.cal.m + n, 1);
    Object.assign(td.cal, { y: d.getFullYear(), m: d.getMonth() });
  }
  // 这个月的格子：前后补上邻月的几天（灰着、也能点），凑满整周
  function calDays() {
    const first = new Date(td.cal.y, td.cal.m, 1), lead = (first.getDay() + 6) % 7, out = [];
    const last = new Date(td.cal.y, td.cal.m + 1, 0).getDate(), n = Math.ceil((lead + last) / 7) * 7;
    for (let i = 0; i < n; i++) {
      const d = new Date(td.cal.y, td.cal.m, 1 - lead + i);
      out.push({ iso: isoDay(d), d: d.getDate(), out: d.getMonth() !== td.cal.m });
    }
    return out;
  }
  const todayIso = () => isoDay(new Date());
  function pickDay(it, iso) {
    td.cal = { id: '', y: 0, m: 0 };
    if (iso === it.deadline?.v && it.deadline?.src === 'user') return;
    setTodo(it, { deadline: iso ? { v: iso, src: 'user' } : null });
  }
  // 时限药丸停在哪一段：快捷的三段之一，别的日期停在「选日期」那段
  function dueIndex(it) {
    if (!it.deadline) return -1;
    const i = DUE_CHOICES().findIndex(c => c[1] === it.deadline.v);
    return i >= 0 ? i : 3;
  }

  // 挂着的会话：正在做、从这里开出、记录过、查过；同一个会话只放在最前面那一组
  function linkGroups(it) {
    const seen = new Set(), out = [];
    for (const [k, label] of [['working', '正在做'], ['born', '从这里开出'], ['wrote', '记录过'], ['read', '查过']]) {
      const list = (it.links?.[k] || []).filter(x => !seen.has(x.sid) && seen.add(x.sid))
        .map(x => ({ ...x, s: td.sessions[x.sid] || { sid: x.sid, missing: true } }));
      if (list.length) out.push({ k, label, list });
    }
    return out;
  }
  function openLinked(s) {
    if (s.elsewhere) return say(`该会话在 ${s.elsewhere} 上，请到那边打开`, { err: true });   // 共用的待办里另一个实例的会话
    if (s.missing) return say('找不到该会话（可能已删除或无权查看）', { err: true });
    // 点的就是底下正开着的那个会话时，换不了标签页、不会触发「换走就收起」，这里自己收（不然点了不跳回去）
    openTab(s.path).then(closeTodos, e => say(`打开会话失败：${e.message}`, { err: true }));
  }
  const workingOf = it => (it.links?.working || []).map(x => td.sessions[x.sid]).filter(s => s && !s.missing);

  // 发到新会话：写进新会话标签页的输入框，人看一眼再发；发出去时连同这几条的编号交给服务器（sessions.js 的 createFromTab）
  function sendPicked() {
    const its = td.picked.map(id => td.items.find(x => x.id === id)).filter(Boolean);
    if (!its.length) return;
    const tab = openFresh({ blank: true });
    if (!tab) return;
    // 这段说明不放进输入框：和「/」的指令卡片一样，画成垫在输入框后面往上长出来的灰卡片（index.html 的 .todo-note），
    // 输入框空着给用户写要说的话；发出去时拼在前面，用户写了话才加一道 --- 隔开（chat.js 的 deliver）
    const how = td.listsKnown ? '先运行 cctodo 读完整份清单' : '已替你认领，先用 cctodo mine 看全文';
    tab.todoNote = { text: `从待办页开出：接着做下面${its.length > 1 ? '这几条' : '这条'}待办（${how}）\n`
      + its.map(it => `- ${it.title}`).join('\n') };
    tab.todoIds = its.map(it => it.id);
    td.picked = [];
    closeTodos();
    afterDraft();
    nextTick(() => document.querySelector('.composer textarea')?.focus());
  }
  // 「让 AI 修好」「让 AI 新建一份」：和发到新会话一样开一个新会话，说明画成输入框后面的灰卡片，用户可以再补一句（页面上的按钮都做成开会话交给 AI）
  function fixList(l) {
    const tab = openFresh({ blank: true });
    if (!tab) return;
    tab.todoNote = { text: l
      ? `待办清单「${l.name}」（${l.where}）的文件不符合格式，网页上显示不了：${l.error}。先运行 cctodo guide 看「条目文件」的格式要求，再把这份清单的文件改好，改完运行 cctodo read ${l.name} 核对。\n`
      : '帮我新建一份待办清单：先运行 cctodo guide 看怎么组织，再问我放在哪台设备的哪个目录，建好后用 cctodo add 登记。\n' };
    tab.todoIds = null;
    closeTodos();
    afterDraft();
    nextTick(() => document.querySelector('.composer textarea')?.focus());
  }
  // 卡片右上角的 ×：这次不从待办开出，说明和认领都不要了
  const dropTodoNote = tab => Object.assign(tab, { todoNote: null, todoIds: null });

  return { td, TODO_STATUS, TODO_IMP, TODO_EFFORT, DUE_CHOICES, loadTodos, openTodos, closeTodos, onTodoScroll, sections, counts, toggleSection,
           toggleTodo, toggleKids, clickRow, toggleBody, bodyLong, closePops, togglePick, quickAdd, setTodo, rate, editing, startEdit, endEdit, groupChoices, pickGroup, openSub, addSub,
           parentTitle, parentChoices, toggleParentPick, pickParent, openCal, calMove, calDays, todayIso, pickDay, dueIndex, linkGroups, openLinked,
           workingOf, sendPicked, fixList, dropTodoNote, dueText, daysLeft };
}
