// 账号页上的几张图（后端 stats.py）：余额最近一个月的曲线、号主看自己名下的号（我的号）、
// 用量速度的小图（最近 3 天每小时花了多少），点用量进去是用量统计页（模板在 index.html 的「用量统计」弹窗）。
// 画法：一根纵轴，自己用主题色、别人和所有人用灰，线 2px；折线、柱子悬停（手机上按住拖）出十字准线，读数写在图上方那一行，
// 不另弹悬停提示（界面上不用悬停提示，README「铁律和约定」）。没悬停时那一行写这张图是什么，两条线时带图例。
// 旧后端没有这些接口（404）：余额照旧是一行按钮，我的号、用量都不出。点数只在接口里，界面上一律写元
const { reactive, ref, computed, watch } = Vue;
import { api, me, kilo, modelName } from './util.js';
import { fen } from './points.js';
import { dayText } from './plan.js';

export const money = (p, d = 2) => fen(p || 0, d);
export const tokText = n => (n == null ? '–' : kilo(Math.round(n)));
const pad = n => String(n).padStart(2, '0');
const dayOf = s => { const d = new Date(s * 1000); return `${d.getMonth() + 1}月${d.getDate()}日`; };
const hourOf = s => { const d = new Date(s * 1000); return `${dayOf(s)} ${pad(d.getHours())}:00`; };
const pct = v => `${(v || 0).toFixed(1)}%`;
const share = a => { const s = a.reduce((x, y) => x + y, 0); return a.map(v => (s ? v / s * 100 : 0)); };

// 折线（可带面积）：series 每条 {key, label, values, cls: 'me' | 'pool', area}，横轴按 t0 起每 step 秒一个点（times 给了就按它读时刻）。
// 纵轴最底下是 0、最高处是最大值（有负数时底下是最小值，写出来，0 画一道虚线）。画布按 1000×100 的格子画、拉伸铺满，线宽不跟着拉。
// 排在前面的线画在最上面：「所有人」常常和「我」差不多高，灰线不能把主题色那条盖住
export const LineChart = {
  props: { series: Array, t0: Number, step: Number, times: Array, fmt: { type: Function, default: v => String(v) },
           height: { type: Number, default: 110 }, idle: { type: String, default: '' } },
  data: () => ({ hover: -1 }),
  computed: {
    n() { return Math.max(0, ...this.series.map(s => s.values.length)); },
    range() {
      let lo = 0, hi = 0;
      for (const s of this.series) for (const v of s.values) { if (v < lo) lo = v; if (v > hi) hi = v; }
      return [lo, hi > lo ? hi : lo + 1];
    },
    paths() {
      return this.series.map(s => {
        const line = s.values.map((v, i) => `${i ? 'L' : 'M'}${this.x(i).toFixed(1)} ${this.y(v).toFixed(2)}`).join('');
        const base = this.y(0).toFixed(2), last = this.x(s.values.length - 1).toFixed(1);
        return { ...s, line, fill: s.area && s.values.length ? `${line}L${last} ${base}L${this.x(0).toFixed(1)} ${base}Z` : '' };
      });
    },
    legend() { return this.series.length > 1 ? this.series : []; },
    read() {
      const i = this.hover;
      if (i < 0) return null;
      return { t: hourOf(this.tAt(i)), vals: this.series.map(s => ({ key: s.key, label: s.label, cls: s.cls, v: this.fmt(s.values[i] ?? 0) })) };
    },
    // 横轴刻度：跨一天半以上按日期（最多五六个），否则每 6 小时一个；贴着两头的不写，免得和边上挤在一起
    ticks() {
      const n = this.n;
      if (n < 2) return [];
      const a = this.tAt(0), b = this.tAt(n - 1), span = b - a, out = [];
      if (span > 36 * 3600) {
        const stride = Math.max(1, Math.ceil(span / 86400 / 5)), d = new Date(a * 1000);
        d.setHours(24, 0, 0, 0);
        for (let t = d.getTime() / 1000, k = 0; t < b; t += 86400, k++) if (k % stride === 0) out.push({ x: (t - a) / span * 100, label: dayOf(t) });
      } else {
        const d = new Date(a * 1000);
        d.setMinutes(0, 0, 0);
        d.setHours(Math.floor(d.getHours() / 6) * 6 + 6);
        for (let t = d.getTime() / 1000; t < b; t += 6 * 3600) {
          const h = new Date(t * 1000).getHours();
          out.push({ x: (t - a) / span * 100, label: h ? `${pad(h)}:00` : dayOf(t) });
        }
      }
      return out.filter(t => t.x > 5 && t.x < 95);
    },
  },
  methods: {
    tAt(i) { return this.times ? this.times[i] : this.t0 + i * this.step; },
    x(i) { return this.n > 1 ? i / (this.n - 1) * 1000 : 500; },
    y(v) { const [lo, hi] = this.range; return 100 - (v - lo) / (hi - lo) * 100; },
    move(e) {
      const r = this.$refs.plot.getBoundingClientRect();
      if (!r.width || !this.n) return;
      this.hover = Math.round(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (this.n - 1));
    },
  },
  template: `<div class="lc">
    <div class="lc-read"><template v-if="read"><b>{{ read.t }}</b><span v-for="v in read.vals" :key="v.key"><i class="lc-key"
      :class="v.cls"></i>{{ v.label }} {{ v.v }}</span></template><template v-else><span class="lc-idle">{{ idle }}</span><span
      v-for="s in legend" :key="s.key"><i class="lc-key" :class="s.cls"></i>{{ s.label }}</span></template></div>
    <div ref="plot" class="lc-plot" :style="{ height: height + 'px' }" @pointermove="move" @pointerdown="move"
         @pointerleave="hover = -1" @pointercancel="hover = -1">
      <svg class="lc-svg" viewBox="0 0 1000 100" preserveAspectRatio="none" aria-hidden="true">
        <line v-for="g in [25, 50, 75]" :key="g" class="lc-grid" x1="0" x2="1000" :y1="g" :y2="g" vector-effect="non-scaling-stroke"/>
        <line v-if="range[0] < 0" class="lc-zero" x1="0" x2="1000" :y1="y(0)" :y2="y(0)" vector-effect="non-scaling-stroke"/>
        <template v-for="p in [...paths].reverse()" :key="p.key"><path v-if="p.fill" :d="p.fill" class="lc-area" :class="p.cls"/><path :d="p.line"
          class="lc-line" :class="p.cls" vector-effect="non-scaling-stroke"/></template>
        <line v-if="hover >= 0" class="lc-cross" :x1="x(hover)" :x2="x(hover)" y1="0" y2="100" vector-effect="non-scaling-stroke"/>
      </svg>
      <template v-if="hover >= 0"><i v-for="s in series" :key="s.key" class="lc-dot" :class="s.cls"
        :style="{ left: x(hover) / 10 + '%', top: y(s.values[hover] ?? 0) + '%' }"></i></template>
      <span class="lc-max">{{ fmt(range[1]) }}</span><span v-if="range[0] < 0" class="lc-min">{{ fmt(range[0]) }}</span>
    </div>
    <div class="lc-x"><span v-for="t in ticks" :key="t.x" :style="{ left: t.x + '%' }">{{ t.label }}</span></div>
  </div>`,
};

// 柱子：labels 是每一格的名字，series 同 LineChart（一格里几条并排），every 是横轴每隔几格写一个名字
export const BarChart = {
  props: { labels: Array, series: Array, fmt: { type: Function, default: v => String(v) }, height: { type: Number, default: 90 },
           idle: { type: String, default: '' }, every: { type: Number, default: 1 } },
  data: () => ({ hover: -1 }),
  computed: {
    max() { let m = 0; for (const s of this.series) for (const v of s.values) if (v > m) m = v; return m || 1; },
    legend() { return this.series.length > 1 ? this.series : []; },
  },
  template: `<div class="lc bc">
    <div class="lc-read"><template v-if="hover >= 0"><b>{{ labels[hover] }}</b><span v-for="s in series" :key="s.key"><i class="lc-key"
      :class="s.cls"></i>{{ s.label }} {{ fmt(s.values[hover] || 0) }}</span></template><template v-else><span class="lc-idle">{{ idle }}</span><span
      v-for="s in legend" :key="s.key"><i class="lc-key" :class="s.cls"></i>{{ s.label }}</span></template></div>
    <div class="bc-plot" :style="{ height: height + 'px' }" @pointerleave="hover = -1">
      <span v-for="(l, i) in labels" :key="i" class="bc-col" :class="{ on: hover === i }" @pointerenter="hover = i" @pointerdown="hover = i"><i
        v-for="s in series" :key="s.key" class="bc-bar" :class="s.cls" :style="{ height: (s.values[i] || 0) / max * 100 + '%' }"></i></span>
    </div>
    <div class="bc-x"><span v-for="(l, i) in labels" :key="i">{{ i % every ? '' : l }}</span></div>
  </div>`,
};

const TOK_LABEL = { in: '未缓存输入', cw: '缓存写入', cr: '缓存命中', out: '输出' };
const METRICS = [{ value: 'full', label: '花费' }, { value: 'n', label: '请求次数' }, { value: 'cw', label: '缓存写入' },
                 { value: 'cr', label: '缓存命中' }, { value: 'out', label: '输出' }];
const RANGES = [{ value: 1, label: '1 天' }, { value: 3, label: '3 天' }, { value: 7, label: '7 天' }, { value: 14, label: '14 天' }, { value: 30, label: '30 天' }];
const monthBefore = s => { const d = new Date(s * 1000); d.setMonth(d.getMonth() - 1); return d.getTime() / 1000; };

// accounts：号列表（sessions.accounts）；quota：useQuota 交出来的那一包（到期时间、血条格子）；sideWide：宽屏常驻侧边栏
export function useStats({ accounts, quota, sideWide }) {
  // ---- 余额曲线（最近 30 天，每小时一个点）----
  const bal = reactive({ data: null, error: '', unsupported: false, at: 0 });
  async function loadBalance() {
    if (!me.name || bal.unsupported || Date.now() - bal.at < 60000) return;
    bal.at = Date.now();
    try {
      const r = await api('/api/balance-history?days=30');
      bal.data = { t0: r.points[0]?.[0] || 0, step: r.step, times: r.points.map(p => p[0]), values: r.points.map(p => p[1]) };
      bal.error = '';
    } catch (e) {
      if (e.status === 404) bal.unsupported = true;
      else bal.error = `余额曲线加载失败：${e.message}`;
    }
  }

  // ---- 我的号（自己是号主的号）----
  const owned = reactive({ list: null, error: '', at: 0 });
  async function loadOwned() {
    if (!me.name || Date.now() - owned.at < 30000) return;
    owned.at = Date.now();
    try {
      owned.list = (await api('/api/owned')).accounts;
      owned.error = '';
    } catch (e) {
      owned.error = e.status === 404 ? '' : `我的号加载失败：${e.message}`;
    }
  }
  // 开通时间按到期往前推一个自然月（instance.json 的 expires 就是开通时间往后一个自然月填的，README「号的到期时间」）
  const ownedRows = computed(() => {
    const now = Date.now() / 1000;
    return (owned.list || []).map(o => {
      const a = accounts.value.find(x => x.name === o.account) || {};
      const exp = quota.quota.expires[o.account] || null, start = exp ? monthBefore(exp) : null;
      const status = a.banned ? (a.note || '暂时不可用') : exp && exp < now ? '订阅已到期' : '';
      const q = quota.quotaCells(o.account);
      const tierOf = c => (!c ? '' : c.rest > 30 ? 'ok' : c.rest > 10 ? 'warn' : 'low');
      return { ...o, name: o.account, api: a.api || null, exp, start, status, q, fiveTier: tierOf(q.five), weekTier: tierOf(q.week),
               life: exp ? Math.max(0, Math.min(100, (now - start) / (exp - start) * 100)) : 0,
               left: exp ? Math.max(0, Math.ceil((exp - now) / 86400)) : null };
    }).sort((x, y) => (!!x.status - !!y.status) || (y.last || 0) - (x.last || 0));
  });
  // 收着时露几个：手机上两个、宽屏三个，多出来的点「展开全部」才出来
  const ownedOpen = ref(false);
  const ownedMax = computed(() => (sideWide.value ? 3 : 2));
  const ownedShown = computed(() => (ownedOpen.value ? ownedRows.value : ownedRows.value.slice(0, ownedMax.value)));

  // ---- 用量（账号页的小图和统计页共用一份缓存，一分钟内不重取）----
  const cache = new Map();
  async function fetchUsage(days, who) {
    const k = `${days}|${who}`, hit = cache.get(k);
    if (hit && Date.now() - hit.at < 60000) return hit.data;
    const data = await api(`/api/usage-stats?days=${days}${who ? `&user=${encodeURIComponent(who)}` : ''}`);
    cache.set(k, { at: Date.now(), data });
    return data;
  }
  const mini = reactive({ data: null, error: '', unsupported: false, loading: false });
  async function loadMini() {
    if (!me.name || mini.unsupported || mini.loading) return;
    mini.loading = true;
    try {
      mini.data = await fetchUsage(3, '');
      mini.error = '';
    } catch (e) {
      if (e.status === 404) mini.unsupported = true;
      else mini.error = `用量加载失败：${e.message}`;
    }
    mini.loading = false;
  }
  // who：'' 是自己，'all' 是所有人，查看器账号还能挑某个人
  const us = reactive({ open: false, loading: false, error: '', days: 3, who: '', data: null, metric: 'full', tok: 'cr' });
  let seq = 0;
  async function loadUsage() {
    const my = ++seq;
    us.loading = true;
    us.error = '';
    try {
      const d = await fetchUsage(us.days, us.who);
      if (my === seq) us.data = d;
    } catch (e) {
      if (my === seq) us.error = `用量统计加载失败：${e.message}`;
    }
    if (my === seq) us.loading = false;
  }
  // 「刷新」：扔掉这一份缓存现取（服务器那边也只沿用一分钟）
  function refreshUsage() {
    cache.delete(`${us.days}|${us.who}`);
    loadUsage();
  }
  function openUsage() {
    us.open = true;
    loadUsage();
  }
  watch(() => [us.days, us.who], () => us.open && loadUsage());

  // 统计页上画的东西都从这里算好给模板
  const uv = computed(() => {
    const d = us.data;
    if (!d) return null;
    const all = d.who === 'all', mine = d.profile, oth = d.others, hours = d.days * 24;
    // 这段时间别人一次也没用过，「其他人」那一列、那一组柱子都不出（不是 0，是没有可比的）
    const meLabel = all ? '所有人' : d.who === me.name ? '我' : d.who, othLabel = all || !oth.n ? '' : '其他人';
    const fmtOf = m => (m === 'full' ? v => money(v) : m === 'n' ? v => `${Math.round(v)} 次` : v => tokText(v));
    const line = [{ key: 'me', label: meLabel, values: d.series[us.metric], cls: 'me', area: true }];
    if (!all) line.push({ key: 'pool', label: '所有人', values: d.pool[us.metric], cls: 'pool' });
    const typ = ['cw', 'cr', 'out', 'in'].map(k => {
      const a = mine.typical?.[k], b = oth.n ? oth.typical?.[k] : null, top = Math.max(a?.[1] || 0, b?.[1] || 0) || 1;
      return { k, label: TOK_LABEL[k], a, b, wa: (a?.[1] || 0) / top * 100, wb: (b?.[1] || 0) / top * 100 };
    });
    // 三种 token 的分布：一次请求落在哪一档（每档 ×1.78），各占这群请求的百分之几；两头都是 0 的档不画
    const h1 = share(mine.hist?.[us.tok] || []), h2 = oth.n ? share(oth.hist?.[us.tok] || []) : [];
    let lo = 0, hi = d.bins.length - 1;
    while (lo < hi && !h1[lo] && !h2[lo]) lo++;
    while (hi > lo && !h1[hi] && !h2[hi]) hi--;
    const hist = { labels: d.bins.slice(lo, hi + 1).map(b => `${tokText(b)} 起`), every: Math.max(1, Math.ceil((hi - lo + 1) / 7)),
                   series: [{ key: 'me', label: meLabel, values: h1.slice(lo, hi + 1), cls: 'me' },
                            ...(h2.length ? [{ key: 'pool', label: othLabel, values: h2.slice(lo, hi + 1), cls: 'pool' }] : [])] };
    const clock = { labels: Array.from({ length: 24 }, (_, h) => `${pad(h)}:00`),
                    series: [{ key: 'me', label: meLabel, values: share(d.clock), cls: 'me' },
                             ...(all ? [] : [{ key: 'pool', label: '所有人', values: share(d.clockAll), cls: 'pool' }])] };
    const topModel = mine.models?.[0]?.full || 1, topAcct = d.accounts[0]?.full || 1, topUser = d.users?.[0]?.full || 1;
    const inp = mine.tokens ? mine.tokens.in + mine.tokens.cw + mine.tokens.cr : 0;
    return {
      all, meLabel, othLabel, line, lineFmt: fmtOf(us.metric), typ, hist, clock,
      kpis: [{ k: '花费（按单价）', v: money(mine.full || 0) }, { k: '实际扣费', v: money(mine.paid || 0) },
             { k: '请求次数', v: `${mine.n || 0}` }, { k: '会话数', v: `${d.sessions.count}` },
             { k: '平均每小时', v: money((mine.full || 0) / hours) }, { k: '最忙的一小时', v: money(Math.max(0, ...d.series.full)) },
             { k: '缓存命中占输入', v: mine.hit == null ? '–' : pct(mine.hit * 100) },
             { k: '每次请求花费（中位）', v: mine.typical ? money(mine.typical.cost[1], 3) : '–' }],
      mix: inp ? ['cr', 'cw', 'in'].map(k => ({ k, label: TOK_LABEL[k], w: mine.tokens[k] / inp * 100 })) : [],
      out: mine.tokens?.out || 0,
      models: (mine.models || []).map(m => ({ ...m, label: modelName(m.model), w: m.full / topModel * 100 })),
      accounts: d.accounts.slice(0, 8).map(a => ({ ...a, w: a.full / topAcct * 100 })),
      users: (d.users || []).map(u => ({ ...u, w: u.full / topUser * 100 })),
      sess: d.sessions, othSess: d.othersSessions,
      whoOptions: [{ value: '', label: '我' }, { value: 'all', label: '所有人' },
                   ...(d.names || []).filter(n => n !== me.name).map(n => ({ value: n, label: n }))],
    };
  });

  return { bal, loadBalance, owned, ownedRows, ownedShown, ownedMax, ownedOpen, loadOwned, mini, loadMini,
           us, openUsage, loadUsage, refreshUsage, uv, METRICS, RANGES, TOK_LABEL, money, tokText, dayText };
}
