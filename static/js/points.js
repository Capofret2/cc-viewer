// 花费：单价、账本、余额在服务器上都按「点」存，界面上一律换成人民币写（FEN 换成分，yuan 再写成元）。气泡头部写这次请求花了几分，
// 各处会话标题前写这个会话一共花了几分；写的都是余额实际扣掉的钱，免费、折扣免掉的和预定付掉的在悬停的分项卡片里（见下面的 callCharge、vSplit）。
// 不写成 5 小时额度的百分之几：关掉计费标记时，同样的 token 只扣一半 5 小时额度，周额度却照扣，
// 点数和 5 小时额度对不上（见 FITTING.md）。
// 单价是服务器 points_rates.json 里的（取配置时拿到）：各模型每百万 token 算几个点，输入、缓存写入、缓存命中、
// 输出各一个。表里没有的模型（还没拟合过）不算花费
const { reactive, ref, watch } = Vue;
import { me } from './util.js';

// 1 点 = 5 分：1000 点兑 50 元。换汇率只改这里
export const FEN = 5;
// 界面上的花费、余额一律写成元（只在前端换算）：有效数字和原来写分时一样，
// 原来分留一位小数，元就留三位（1.6 分写成 0.016 元）。名字照旧叫 fen，各处的门槛（不到 0.05 分不写）也照旧按分算
// d 是小数位：侧边栏的会话花费、工作过程标题栏的总价只留到分（d = 2）
export const yuan = (fenAmount, d = 3) => `${(fenAmount / 100).toFixed(d)}元`;
export const fen = (points, d = 3) => yuan(points * FEN, d);
// 对话里的回复（气泡头、收成一行的一步、工作过程标题栏）把「元」写成符号放在数后面：0.03¥，别处照旧写元
export const fenSign = (points, d = 3) => `${(points * FEN / 100).toFixed(d)}¥`;

const rates = reactive({});
// 单价换了就加一：对话行有 v-memo，不带上它的话单价到了也不重画
export const pointsVer = ref(0);
export function setRates(r) {
  for (const k of Object.keys(rates)) delete rates[k];
  Object.assign(rates, r || {});
  pointsVer.value++;
}

// 模型名后面可能带日期或 [1m]：取最长的前缀匹配。号自己有一套价的（中转号，服务器 rates.billing_model）先查「模型@account=号」。
// 返回用的是单价表里哪一条，价目表要靠它认出号自己的价、把同一条价的几个号并成一行
export function rateKey(model, account) {
  const tagged = `${(model || '').split('[')[0]}@account=${account}`;
  if (account && rates[tagged]) return tagged;
  return Object.keys(rates).filter(k => (model || '').startsWith(k)).sort((a, b) => b.length - a.length)[0] || null;
}
function rateOf(model, account) {
  const k = rateKey(model, account);
  return k ? rates[k] : null;
}

// 价目表（账号卡片底栏的「价目表」，app.js 的 priceRows）：
// 「一次请求」把真实 Opus 5.5 调用的平均构成套到各家单价上比较（约 8 万次调用，
// 平均缓存写入 7600、缓存命中 28 万、输出 1232 token，输入少到可以不计）
export const AVG_CALL = { in: 0, cw: 7600, cr: 280000, out: 1232 };
// 每百万 token 合几元（{in, cw, cr, out}）：和气泡上算花费的是同一份单价（已乘收钱的比例）；没有单价就是 null
export function yuanRate(model, account) {
  const r = rateOf(model, account);
  return r ? Object.fromEntries(['in', 'cw', 'cr', 'out'].map(k => [k, r[k] * FEN / 100])) : null;
}

function missingHourRate(use, model, account, r) {
  const tagged = (model || '').includes('@account=') || (account && rates[`${(model || '').split('[')[0]}@account=${account}`]);
  return tagged && use?.cw1h && r?.cw1h == null && !r?.estimate_only;
}

// 一次请求花了几点：use 是 {in, cw, cr, out}；这个模型没有单价就是 null
export function callPoints(use, model, account, mid) {
  const r = rateOf(model, account);
  if (!use || !r) return null;
  if ('charge_yuan' in use && r.yuan) return use.charge_yuan * r.yuan;
  const actual = callPrices.get(mid);
  if (r.estimate_only && actual && actual.account === account && !actual.estimated && actual.yuan != null) return actual.yuan * (100 / FEN);
  if (missingHourRate(use, model, account, r)) return null;
  return (use.in * r.in + use.cw * r.cw + use.cr * r.cr + use.out * r.out
    + (use.cw1h || 0) * (r.cw1h || 0)) / 1e6;
}

const callPrices = reactive(new Map());
export function setCallPrices(prices) {
  let changed = false;
  for (const [mid, value] of Object.entries(prices || {})) {
    const old = callPrices.get(mid);
    if (!old || old.account !== value.account || old.estimated !== value.estimated || old.yuan !== value.yuan) {
      callPrices.set(mid, value); changed = true;
    }
  }
  if (changed) pointsVer.value++;
}
export function callEstimated(use, model, account, mid) {
  if (!rateOf(model, account)?.estimate_only) return false;
  if (use && 'estimated_calls' in use) return use.estimated_calls > 0;
  const value = callPrices.get(mid);
  return !value || value.account !== account || value.estimated || value.yuan == null;
}
const spentEstimated = spent => Object.values(spent || {}).some(use => use.estimated_calls > 0);

// 气泡头部：几分
export const callText = (use, model) => {
  const p = callPoints(use, model);
  return p == null ? '' : fen(p);
};

// 悬停显示的几项里，某一项单独花了几分：part 是这一项含的 {in / cw / cr / out: token 数}，各按各的单价算再相加
// （界面上的「缓存写入」含输入，见 item-row.js 的 useParts）；这个模型没有单价就是空的。限时特惠不改单价，这里不打折
export const partPoints = (part, model, account) => {
  const r = rateOf(model, account);
  // 平台只给总扣费，没有各项金额；不把估算拆分伪装成平台实扣。
  if (r?.estimate_only) return null;
  if (missingHourRate(part, model, account, r)) return null;
  return r ? Object.entries(part).reduce((s, [k, n]) => s + (n || 0) * (r[k] || 0), 0) / 1e6 : null;
};
export const partText = (part, model) => {
  const p = partPoints(part, model);
  return p == null ? '' : fen(p);
};

// 会话标题前：spent 是服务器给的 {模型: {in, cw, cr, out}}（主会话加子代理），只加有单价的模型
export function spentPoints(spent) {
  let sum = 0, any = false;
  for (const [model, use] of Object.entries(spent || {})) {
    const p = callPoints(use, model);
    if (p != null) { sum += p; any = true; }
  }
  return any ? sum : null;
}
export function spentText(spent) {
  const p = spentPoints(spent);
  return p == null ? '' : fen(p) + (spentEstimated(spent) ? ' 预估' : '');
}

// 每条回复是哪个号发的（回复编号 → 号）：各标签页的快照带来，服务器按 api_calls.jsonl 认，
// 网页对话还没记进去的按驱动它的 CLI 认。回复编号全局唯一，放一处就行。
// 气泡详情里写出号名；限量号（见 users.py，名单在 me.paid）发的，点数用对位色写，免费号的用主题色
const callAccounts = reactive(new Map());
export function setCallAccounts(calls) {
  let changed = false;
  for (const [mid, acc] of Object.entries(calls || {})) {
    if (callAccounts.get(mid) !== acc) { callAccounts.set(mid, acc); changed = true; }
  }
  if (changed) pointsVer.value++;   // 对话行有 v-memo，加一才会重画
}
export const accountOf = mid => (mid && callAccounts.get(mid)) || '';
// ts 是这次调用的时间：免费号改成限量号（me.paidSince，号 → unix 秒）以前发的，照旧算免费号的，和账本一致
export const isPaid = (mid, ts) => {
  const a = accountOf(mid);
  if (!a || !me.paid.includes(a)) return false;
  const since = me.paidSince?.[a];
  return !since || !ts || Date.parse(ts) / 1000 >= since;
};
watch(() => [me.paid, me.paidSince, me.promo, me.autoPromo], () => pointsVer.value++);   // 限量号名单、特惠取到得晚，到了要重画

// 限时特惠（服务器 instance.PROMO，me.promo 是 号 → [{from, until, factor, label}]，一个号可以有好几段；旧服务器给的是单独一条）：
// 特惠时段里在这个号上的花费照单价算，只有 factor 那几成进账本（红的，有预定就由预定付），其余免掉（绿的），和账本一致。
// t 是 unix 秒；不知道时刻的按原价。几段重叠取低的
// 自动特惠（me.autoPromo 是 号 → {factor, label, windows: [[开始, 结束]]}，服务器 reserve.auto_promo_view）也算，两个都在取低的
const fixedAt = (acc, t) => [].concat(me.promo?.[acc] || []).filter(p => p.from <= t && t < p.until)
  .reduce((best, p) => (!best || p.factor < best.factor ? p : best), null);
export function promoFactor(acc, t) {
  if (!acc || t == null) return 1;
  const p = fixedAt(acc, t), a = me.autoPromo?.[acc];
  const f = p ? p.factor : 1;
  return a?.windows.some(([x, y]) => x <= t && t < y) ? Math.min(f, a.factor) : f;
}
// 这次调用按原价的几成收（ts 是会话记录里的时间字符串）
export const promoOf = (mid, ts) => promoFactor(accountOf(mid), ts ? Date.parse(ts) / 1000 : null);
// 这个号此刻在特惠里就给标签上写的字和截止时刻（选号的地方挂「特惠 1折」「特惠半价」），不在就是 null；
// 手写的和自动的同时在，按折扣低的那个写
export function promoTag(acc) {
  const now = Date.now() / 1000, f = promoFactor(acc, now);
  if (f === 1) return null;
  const p = fixedAt(acc, now), fixed = p && p.factor === f, a = me.autoPromo?.[acc];
  const label = fixed ? p.label : a.label, until = fixed ? p.until : a.windows.find(([x, y]) => x <= now && now < y)[1];
  const d = new Date(until * 1000), pad = n => String(n).padStart(2, '0');
  // badge：简洁模式账号表里号名右上角的角标，按折扣写「50%off」（小写、和百分比不空格）
  return { label, badge: `${Math.round((1 - f) * 100)}%off`, title: `限时特惠：该账号按原价的 ${f * 10} 成收费，${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())} 截止` };
}

// 限量号的调用里由预定付了的各有几成（回复编号 → 0～1，服务器 reserve.frac 给的，标签页快照带来）。
// 这一份不再扣钱，界面上写成蓝的（和额度血条上自己预定的蓝同一个意思）。
// 调用记下后几秒服务器才把它分给预定，所以刚发出的那次先是红的，过一会儿变蓝
const callCovered = reactive(new Map());
export function setCallCovered(covered) {
  let changed = false;
  for (const [mid, f] of Object.entries(covered || {})) {
    if (callCovered.get(mid) !== f) { callCovered.set(mid, f); changed = true; }
  }
  if (changed) pointsVer.value++;
}
export const coveredOf = mid => (mid && callCovered.get(mid)) || 0;

// 特惠结算时记下的免了几成（回复编号 → 0～1，服务器 reserve.recorded_free 给的）：绿条不够时免得比折扣少，用完了就不免。
// 没记的（会话主人没有预定、不用结算的，只能花绿条，绿条总是够）按折扣算；旧后端不给这张表，也是按折扣算
const callFree = reactive(new Map());
export function setCallFree(free) {
  let changed = false;
  for (const [mid, f] of Object.entries(free || {})) {
    if (callFree.get(mid) !== f) { callFree.set(mid, f); changed = true; }
  }
  if (changed) pointsVer.value++;
}
export const freeOf = (mid, ts) => (mid && callFree.has(mid) ? callFree.get(mid) : 1 - promoOf(mid, ts));

// 界面上直接写出来的花费只能是余额实际扣掉的钱：限量号照常扣钱的和由预定付掉的合成一个数
// （预定只对号主开放、用来给自己的号留额度，下单时已经从余额扣过），免费号、特惠和号主折扣免掉的不算进去；
// 这几项各是多少放进悬停（手机上点一下）立刻浮出的分项卡片（vSplit）。账本照旧由后端分三段算，这里只改怎么显示。
// 一次调用：p0 是照单价的全价（点），返回 {pay 实扣, red 照常扣钱, blue 由预定付掉, free 免掉的, why 免掉的是什么}
export function callCharge(p0, mid, ts) {
  if (!isPaid(mid, ts)) return { pay: 0, red: 0, blue: 0, free: p0, why: accountOf(mid) ? '免费号，不扣钱' : '没有账号记录，不扣钱' };
  const p = p0 * (1 - freeOf(mid, ts)), f = coveredOf(mid);
  return { pay: p, red: p * (1 - f), blue: p * f, free: p0 - p, why: '特惠或折扣免掉' };
}
// 分项卡片里的字：一项一行，不到 0.05 分的不写；有免掉的就在最前面写全价，几项加起来对得上。
// full 给 null 就不写全价（余额看板那种按花钱算的，下单付的预定不是调用的全价）；blueLabel 换掉「由预定付掉」的说法
export function chargeSplit(c, fmt = fen, blueLabel = '由预定付掉') {
  const rows = [['照常扣钱', c.red], [blueLabel, c.blue], [c.why, c.free]].filter(([, p]) => p * FEN >= 0.05);
  const full = c.full === undefined ? c.pay + c.free : c.full;
  if (full != null && c.free * FEN >= 0.05) rows.unshift(['按单价全价', full]);
  return rows.map(([label, p]) => `${label} ${fmt(p)}`).join('\n');
}

// 分项卡片：v-split="字"，有鼠标的设备悬停立刻浮出，手机上点一下浮出、再点或点别处收起（点到的这下不再交给外面，
// 免得侧边栏这类一点就打开会话的行抢走）。不用 title（停一会儿才冒，界面上不用）。
// 卡片只有一张，由 index.html 最底下的 .split-pop 画、position: fixed，不会被侧边栏、表格这些会裁剪的容器挡住
export const splitPop = reactive({ text: '', x: 0, top: 0, bottom: 0 });
const canHover = matchMedia('(hover: hover)').matches;
let popEl = null;
function popAt(el) {
  const r = el.getBoundingClientRect();
  // 卡片最宽 300px、以数字为中心，靠屏幕边时往里挪，不顶出去
  Object.assign(splitPop, { text: el._split, x: Math.min(Math.max(r.left + r.width / 2, 158), innerWidth - 158), top: r.top, bottom: r.bottom });
  popEl = el;
}
function popHide() { splitPop.text = ''; popEl = null; }
document.addEventListener('click', e => { if (popEl && !popEl.contains(e.target)) popHide(); }, true);
addEventListener('scroll', () => popEl && popHide(), true);   // 卡片是按数字当时的位置放的，一滚就对不上了
export const vSplit = {
  mounted(el, { value }) {
    el._split = value;
    el.addEventListener('mouseenter', () => { if (canHover && el._split) popAt(el); });
    el.addEventListener('mouseleave', () => { if (canHover && popEl === el) popHide(); });
    el.addEventListener('click', e => {
      if (canHover || !el._split) return;
      e.stopPropagation();
      if (popEl === el) popHide(); else popAt(el);
    });
  },
  updated(el, { value }) {
    el._split = value;
    if (popEl === el) value ? (splitPop.text = value) : popHide();
  },
  beforeUnmount(el) { if (popEl === el) popHide(); },
};
// 按花钱算的总数（余额看板、用户管理按最近用量排序用）：r 是 {all, paid, blue, bought}，和上面 bought 那种写法加起来一样
export function spentMoney(r) {
  return Math.max(0, (spentPoints(r?.all) || 0) - (spentPoints(r?.blue) || 0)) + (r?.bought || 0);
}
// 各处会话标题前的总消耗：<spent-pts :spent="s.spent" :paid="s.spentPaid" :blue="s.spentBlue">；
// spent 是全价，paid 是其中进账本的（限量号花的、已去掉特惠和折扣免掉的），blue 是 paid 里由预定付掉的（格式都和 spent 一样）。
// 只写一个数：实扣 = paid（照常扣钱的 + 预定付掉的），三段各多少在分项卡片里（见 callCharge 上面的说明）。
// bought（点数）给了就是按花钱算（余额看板、用户管理里每个人最近 N 分钟）：预定下了单钱就花掉了，
// 这时写的是照常扣钱的 + 这段时间下单付的预定，预定付掉的那些调用不再算（钱下单时已经算过）
// stack：最近会话、标签下拉、全部会话的卡片上，数固定在标题右上角（.pts-stack 是右浮动，写在标题文字前面，标题绕着它排）
// bare：不写「元」、只留到分，只写数（简洁模式侧边栏的会话底下那行，接在几分钟前后面，省地方）
export const SpentPts = {
  props: { spent: Object, paid: Object, blue: Object, bought: { type: Number, default: null }, stack: Boolean, bare: Boolean },
  directives: { split: vSplit },
  computed: {
    t() {
      pointsVer.value;
      const all = spentPoints(this.spent), p = spentPoints(this.paid) || 0, b = Math.min(spentPoints(this.blue) || 0, p);
      const money = this.bought != null, pay = money ? p - b + this.bought : p;
      if (all == null && !pay) return null;
      let text = fen(pay, this.bare ? 2 : 3);
      if (this.bare) text = text.replace(/元$/, '');
      if (spentEstimated(this.spent)) text += ' 预估';
      const split = chargeSplit({ red: p - b, blue: money ? this.bought : b, free: Math.max(0, (all || 0) - p), why: '免费或折扣免掉',
                                  full: money ? null : all || 0 }, fen, money ? '这段时间下单付的预定' : '由预定付掉');
      return { text, split };
    },
  },
  template: `<span v-if="stack" v-show="t" class="pts-stack"><b v-if="t" class="pts" v-split="t.split">{{ t.text }}</b></span><b
    v-else-if="t" class="pts" v-split="t.split">{{ t.text }}</b>`,
};
