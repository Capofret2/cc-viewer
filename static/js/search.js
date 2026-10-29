// 找话：在当前会话里精确找到第几条并跳过去，或者扫一遍全部会话看哪些里面有。
// 结果能按来源筛：用户说的、Claude 的回复、思考、工具、其他，筛选在服务器那边做，名额不会被别的类占掉
const { reactive, nextTick } = Vue;
import { api, clock, persistRef, who } from './util.js';
import { unfold } from './item-row.js';

export const SEARCH_GROUPS = { get user() { return who.me; }, text: 'Claude', thinking: '思考', tool: '工具', other: '其他' };

export function useSearch({ current, openTab, ensureLoaded }) {
  const search = reactive({
    // ran：这批结果是拿哪个词搜的（跳过去时描这个词；框里的字可能已经又改过了）
    open: false, q: '', ran: '', scope: 'tab', loading: false, error: '',
    hits: [], sessions: [], more: false, done: false, input: null, counts: {},
  });
  // 勾着哪几类，记在这台设备上
  const kinds = persistRef('searchKinds', Object.keys(SEARCH_GROUPS));
  function toggleKind(k) {
    const on = kinds.value.includes(k);
    if (on && kinds.value.length === 1) return;   // 至少留一类
    kinds.value = on ? kinds.value.filter(x => x !== k) : [...kinds.value, k];
    if (search.done) runSearch();
  }

  function openSearch(scope) {
    search.open = true;
    if (scope) search.scope = scope;
    nextTick(() => search.input?.focus());
  }

  async function runSearch() {
    const q = search.q.trim();
    Object.assign(search, { error: '', hits: [], sessions: [], more: false, done: false, counts: {} });
    if (!q || (q.length < 2 && /^[\x00-\x7f]$/.test(q))) {   // 和服务器一样：中文一个字也有得找，英文单个字母命中太多
      search.error = q ? '英文至少两个字母，中文至少一个字' : '';
      return;
    }
    if (search.scope === 'tab' && (!current.value || current.value.fresh)) {
      search.error = '请先打开一个会话';
      return;
    }
    search.loading = true;
    try {
      const tid = search.scope === 'tab' ? current.value.id : '';
      const r = await api(`/api/search?q=${encodeURIComponent(q)}${tid ? `&tid=${tid}` : ''}&kinds=${kinds.value.join(',')}`);
      Object.assign(search, { hits: r.hits || [], sessions: r.sessions || [], more: !!r.more, done: true,
                              counts: r.counts || {}, ran: q });
    } catch (e) {
      search.error = e.message;
    }
    search.loading = false;
  }

  // 跳到命中的那一条：不在手上这批里就换成它附近那一段（tabs.js 的 ensureLoaded），再滚过去闪一下
  async function jumpTo(hit) {
    const tab = current.value;
    if (!tab) return;
    search.open = false;
    search.input?.blur();   // 手机上先把键盘收了，页面高度先变好，后面滚到中间才不会被键盘收起顶歪
    // 跳不过去时把搜索框重新打开，写明原因（框已经收起来了，不说就像点了没反应）
    let ok = false;
    try { ok = await ensureLoaded(tab, hit.seq); } catch (e) {
      Object.assign(search, { open: true, error: `无法获取该条附近的内容：${e.message}` });
      return;
    }
    if (!ok) {
      Object.assign(search, { open: true, error: '会话内容已整体更新（例如经过压缩），请重新搜索后再跳转' });
      return;
    }
    await nextTick();
    await new Promise(r => requestAnimationFrame(r));
    // 折叠时那一条被收进了卡片里、没有自己的位置：跳到它所在的那张（序号不比它大的最后一个）
    const find = () => tab.el?.querySelector(`[data-seq="${hit.seq}"]`) ||
      [...(tab.el?.querySelectorAll('[data-seq]') || [])].filter(e => +e.dataset.seq <= hit.seq).pop();
    let el = find();
    // 落在被用户收起来的气泡上：先展开，再找一次里面那一段
    const box = el?.querySelector(':scope > .bubble:is(.folded, .thead.shut)') || el?.closest('.bubble:is(.folded, .thead.shut)');
    if (box) {
      unfold(box);
      await nextTick();
      el = find() || el;
    }
    if (!el) {
      search.open = true;
      search.error = '该条已被显示设置隐藏，请开启对应开关后重试';
      return;
    }
    // 能在这一条里找到搜的那几个字，就只描那几个字、把第一处滚到中间（不整个气泡闪）；
    // 找不到（字落在收着的工具参数里、被渲染改了样等）才退回整条闪一下
    const hits = textRanges(el, search.ran);
    if (hits.length) {
      hits[0].startContainer.parentElement.scrollIntoView({ block: 'center' });
      markHits(hits);
      return;
    }
    el.scrollIntoView({ block: 'center' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1800);
  }

  // 把 el 里所有看得见的文字连起来找 q（区分大小写，和服务器一样），一处一个 Range；
  // 连起来找是因为渲染出来的加粗、代码等会把一句话拆成好几个文字节点
  function textRanges(el, q) {
    if (!q || !window.CSS?.highlights || typeof Highlight !== 'function') return [];
    const nodes = [], walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let all = '';
    for (let n; (n = walk.nextNode());) {
      if (!n.parentElement?.getClientRects().length) continue;   // 藏着的（display: none、收起的）不算
      nodes.push([n, all.length]);
      all += n.data;
    }
    const at = pos => {   // 连起来的第 pos 个字落在哪个节点的第几个
      let k = nodes.length - 1;
      while (k > 0 && nodes[k][1] > pos) k--;
      return [nodes[k][0], pos - nodes[k][1]];
    };
    const out = [];
    for (let i = all.indexOf(q); i >= 0; i = all.indexOf(q, i + q.length)) {
      const r = new Range();
      r.setStart(...at(i));
      r.setEnd(...at(i + q.length - 1)); r.setEnd(r.endContainer, r.endOffset + 1);
      out.push(r);
    }
    return out;
  }

  // 描出来留一会儿再擦掉（::highlight 不能做动画，样式在 style.css 的 ::highlight(find-hit)）
  let unmark = 0;
  function markHits(ranges) {
    clearTimeout(unmark);
    CSS.highlights.set('find-hit', new Highlight(...ranges));
    unmark = setTimeout(() => CSS.highlights.delete('find-hit'), 2600);
  }

  // 全部会话的结果：打开那个会话，接着在里面搜同一个词
  async function openFound(s) {
    try {
      await openTab(s.path);
      search.scope = 'tab';
      await runSearch();
    } catch (e) {
      search.error = e.message;
    }
  }

  const hitLabel = h => `${{ user: who.me, command: '命令', text: 'Claude', thinking: '思考', tool: '工具',
                            result: '工具结果', system: '系统', attachment: '附件', task: '后台任务' }[h.kind] || h.kind}`
    + (h.ts ? ` · ${clock(h.ts)}` : '');

  return { search, openSearch, runSearch, jumpTo, openFound, hitLabel, SEARCH_GROUPS, searchKinds: kinds, toggleKind };
}
