// 预定额度的面板（规矩和算法见 reserve.py）：账号表表头「5小时」右边的「预定」打开（先选现在用的号，标题里的下拉换号），
// 或者右键（手机上长按）某个号直接打开那个号的。
// 列出从当前窗口起、本周重置之前的每个 5 小时窗口（以后的按「每次重置立刻开下一个」排，开窗服务 keeper.py 保证准时开），
// 每个窗口拖进度条订百分之几（0～100），另外可以订本周额度的百分之几，还能提前订以后几周的（连本周最多 5 周，服务器给的 weeks，平时收着，点一下才展开；
// 旧服务器不给 weeks 就只有本周）；每一批自己选先用预定的还是当保底。一次付清，用不完也不退。
// 上限都是服务器按此刻算好给的，这里只把填的数和上限比一比、算好价钱，下单时服务器再核一遍
const { reactive, computed } = Vue;
import { api, say, ago, me } from './util.js';
import { FEN, yuan } from './points.js';

const pad = n => String(n).padStart(2, '0');
const hm = s => { const d = new Date(s * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const md = s => { const d = new Date(s * 1000); return `${d.getMonth() + 1}/${d.getDate()}`; };
// 一个窗口：「10/11 14:00–19:00」，跨天的结束时刻也带日期
export const winText = w => `${md(w.start)} ${hm(w.start)}–${md(w.end) === md(w.start) ? '' : md(w.end) + ' '}${hm(w.end)}`;
const num = v => { const x = parseFloat(v); return Number.isFinite(x) && x > 0 ? x : 0; };

export function useReserve({ loadQuota, loadMe }) {
  // confirm：点了「付款预定」以后弹出的确认窗口开着没有（价格、不退款都在那里写明，在那里点确定才真的付）
  // future：以后的窗口展开没有。每次打开面板都先收着，只露当前窗口，点一下才展开
  // weeks：以后各周填的数（按那一周的开始时刻）；moreWeeks：以后的几周展开没有
  // promoDraft：号主正在拖的折扣进度条（收原价的百分之几），松手存好以后清掉、回到服务器给的 ownerPromo
  const rv = reactive({ open: false, account: '', loading: false, busy: false, error: '', data: null, five: {}, week: '', weeks: {},
                        front: true, confirm: false, future: false, moreWeeks: false, promoDraft: null });
  async function openReserve(account) {
    Object.assign(rv, { open: true, account, loading: true, error: '', data: null, five: {}, week: '', weeks: {}, front: true,
                        confirm: false, future: false, moreWeeks: false, promoDraft: null });
    // 设置还没取到时说不出现在用哪个号，写明让人在标题的下拉里选，不拿空号去请求
    if (!account) { rv.loading = false; rv.error = '尚未获取当前账号，请在上方下拉框中选择账号'; return; }
    try {
      rv.data = await api(`/api/reserve/${encodeURIComponent(account)}`);
    } catch (e) {
      // 403 是号主关了别人的预定（server.reserve_ctx）：只写服务器那句「暂时不支持预定」，不当成打开失败
      rv.error = e.status === 403 ? e.message : `无法打开 ${account} 的预定：${e.message}`;
    }
    rv.loading = false;
  }
  // 号主开关别人在这个号上下新的预定（服务器只回开关状态）：关了不影响自己订，也不动已有的预定
  async function setReserveOpen(on) {
    if (rv.busy || !rv.data) return;
    rv.busy = true;
    rv.error = '';
    const account = rv.account;
    try {
      const r = await api(`/api/reserve/${encodeURIComponent(account)}/open`, { method: 'POST', body: { open: on } });
      if (rv.account === account && rv.data) rv.data.closed = r.closed;
      say(r.closed ? `${account} 已停止接受他人预定，已有预定不受影响` : `${account} 已恢复接受他人预定`);
    } catch (e) { rv.error = `无法${on ? '开启' : '关闭'}他人预定：${e.message}`; }
    rv.busy = false;
  }
  // 号主的折扣进度条现在指着的数（收原价的百分之几）：拖着时是拖到的，平时是服务器存着的
  const rvPromoPct = () => rv.promoDraft ?? Math.round((rv.data?.ownerPromo ?? 1) * 100);
  const promoText = pct => (pct >= 100 ? '原价' : pct <= 0 ? '免费' : pct === 50 ? '半价' : `${pct / 10}折`);
  // 号主松开折扣进度条：存下这个号从现在起收原价的几成，只管以后的调用（服务器 reserve.set_owner_promo）
  async function setOwnerPromo(pct) {
    if (rv.busy || !rv.data) return;
    rv.busy = true;
    rv.error = '';
    const account = rv.account;
    try {
      const r = await api(`/api/reserve/${encodeURIComponent(account)}/promo`, { method: 'POST', body: { factor: pct / 100 } });
      if (rv.account === account && rv.data) rv.data.ownerPromo = r.ownerPromo;
      say(`${account} 已改为${promoText(Math.round(r.ownerPromo * 100))}`);
      loadMe();   // 账号表上的特惠标签跟着变
    } catch (e) { rv.error = `无法修改 ${account} 的折扣：${e.message}`; }
    rv.promoDraft = null;   // 没存成就弹回原来的
    rv.busy = false;
  }
  // 这一批：填了哪些窗口、订本周多少、折合本周多少、要付多少点；哪里超了上限
  const rvOrder = computed(() => {
    const d = rv.data;
    if (!d || d.noReserve) return { five: [], week: 0, weeks: [], problems: [], empty: true };   // 不开放预定的号号主只能改折扣
    const five = d.windows.map(w => ({ start: w.start, pct: num(rv.five[w.start]) })).filter(x => x.pct > 0);
    const later = d.weeks || [];
    const weeks = later.map(w => ({ start: w.start, end: w.end, pct: num(rv.weeks[w.start]) })).filter(x => x.pct > 0);
    const week = num(rv.week), pct5 = five.reduce((s, x) => s + x.pct, 0), pctLater = weeks.reduce((s, x) => s + x.pct, 0);
    const needWeek = pct5 * d.k + week;
    const problems = d.windows.filter(w => num(rv.five[w.start]) > w.max + 1e-6)
      .map(w => `${winText(w)} 最多还可预定 ${w.max.toFixed(1)}%`);
    if (needWeek > d.weekMax + 1e-6) problems.push(`本次预定合计占本周 ${needWeek.toFixed(2)}%，本周仅剩 ${d.weekMax.toFixed(2)}% 可预定`);
    problems.push(...later.filter(rvOverWeek).map(w => `${winText(w)} 当周最多还可预定 ${w.max.toFixed(2)}%`));
    // 订多少就收正常用掉这么多要花的钱：按单价折成点数，再乘收钱的比例（服务器给的 price，和账本一致）；不受限时特惠影响
    const points = d.charge ? (pct5 + (week + pctLater) / d.k) * d.ptsPerFive * (d.price ?? 1) : 0;
    // 价格：大字写的分数（免费号写「免费」）
    const price = d.charge ? yuan(points * FEN) : '免费';
    return { five, week, weeks, pct5, pctLater, needWeek, points, price, problems, empty: !five.length && !week && !weeks.length };
  });
  const rvOver = w => num(rv.five[w.start]) > w.max + 1e-6;
  const rvOverWeek = w => num(rv.weeks[w.start]) > w.max + 1e-6;
  // 「付款预定」：先核一遍，弹出确认窗口。号主订自己的号钱又流回自己、随时能撤，不弹窗口直接下单
  function submitReserve() {
    const o = rvOrder.value;
    if (o.empty || o.problems.length || rv.busy) return;
    if (rv.data?.owner) confirmReserve();
    else rv.confirm = true;
  }
  // 确认窗口里点「确定付」：这才真的下单
  async function confirmReserve() {
    const o = rvOrder.value;
    if (o.empty || o.problems.length || rv.busy) return;
    rv.busy = true;
    rv.error = '';
    try {
      const r = await api('/api/reserve', { method: 'POST', body: { account: rv.account, front: rv.front, five: o.five, week: o.week,
                                                                   weeks: o.weeks.map(({ start, pct }) => ({ start, pct })) } });
      Object.assign(rv, { data: r.plan, five: {}, week: '', weeks: {}, confirm: false });
      say(`预定成功：${rv.account}${r.batch.points && !r.plan.owner ? `，已付 ${yuan(r.batch.points * (r.plan.price ?? 1) * FEN)}` : ''}`);
      loadQuota(true);   // 自己的蓝条马上画出来
      loadMe();          // 余额变了
    } catch (e) {
      rv.error = `预定失败：${e.message}`;
      rv.confirm = false;   // 错误写在面板上，确认窗口收起来才看得到
    }
    rv.busy = false;
  }
  // 撤销一批预定：没用掉的那几成退钱，这一批作废，额度还给大家。查看器账号能撤别人的；
  // 号主撤自己在自己号上的（「我的预定」里的按钮，rv.data.owner），走另一个接口
  async function cancelBatch(b) {
    if (rv.busy) return;
    const own = b.user === me.name;
    if (!confirm(own ? `确定撤销您于${ago(b.ts)}在 ${rv.account} 上的这批预定吗？未使用的部分按下单价格退款，撤销后无法恢复。`
      : `确定撤销 ${b.user} 于${ago(b.ts)}在 ${rv.account} 上的这批预定吗？未使用的部分按下单价格退还给 ${b.user}，撤销后无法恢复。`)) return;
    rv.busy = true;
    rv.error = '';
    try {
      const r = await api(own ? '/api/reserve/cancel' : '/api/admin/reserve/cancel', { method: 'POST', body: { id: b.id } });
      rv.data = r.plan;
      say(own ? `已撤销，退回 ${yuan(r.refund * FEN)}` : `已撤销，已向 ${b.user} 退回 ${yuan(r.refund * FEN)}`);
      loadQuota(true);   // 占着的额度还回去了，血条跟着变
      loadMe();          // 撤的是自己的单时余额变了
    } catch (e) { rv.error = `撤销失败：${e.message}`; }
    rv.busy = false;
  }
  // 标题后面的红字：号 7 天内月订阅到期才写，不满一天写「今天」（天数往下取整，不把剩一天半说成两天）
  const rvExpiry = exp => {
    if (!exp) return '';
    const left = exp - Date.now() / 1000;
    if (left <= 0) return '月订阅已到期';
    if (left >= 7 * 86400) return '';
    const d = Math.floor(left / 86400);
    return d ? `月订阅 ${d} 天后到期` : '月订阅今天到期';
  };
  return { rv, openReserve, rvOrder, rvOver, rvOverWeek, submitReserve, confirmReserve, cancelBatch, setReserveOpen, winText, rvExpiry,
           rvPromoPct, promoText, setOwnerPromo };
}

// 右键（手机上长按）：账号行上打开预定面板，标签页上弹出菜单。长按弹出东西后，松手那一下的点击不再当成选号、切标签；
// 安卓长按时还会再来一次右键菜单事件，同一下只开一次。回调拿到的是右键事件，长按时是一个带手指位置的同样形状的对象，
// 弹菜单的地方照样按 clientX / clientY 摆。只有手指按着时弹出的才吞掉随后那一下点击：
// 电脑上右键完马上左键点（比如右键看了一眼菜单又直接点标签页切过去）不能被吃掉
export const longPress = {
  mounted(el, binding) {
    el._lp = binding.value;
    let timer = 0, fired = 0, swallow = 0, touching = false;
    const fire = e => { fired = Date.now(); if (touching) swallow = fired; el._lp?.(e); };
    const cancel = () => clearTimeout(timer);
    el.addEventListener('touchstart', e => {
      cancel();
      touching = true;
      const { clientX, clientY } = e.touches[0];
      timer = setTimeout(() => fire({ clientX, clientY, preventDefault() {} }), 550);
    }, { passive: true });
    el.addEventListener('touchmove', cancel, { passive: true });
    for (const t of ['touchend', 'touchcancel']) el.addEventListener(t, () => { cancel(); touching = false; }, { passive: true });
    el.addEventListener('contextmenu', e => {
      e.preventDefault();
      cancel();
      if (Date.now() - fired > 1000) fire(e);
    });
    el.addEventListener('click', e => {
      if (Date.now() - swallow < 1000) { e.preventDefault(); e.stopImmediatePropagation(); swallow = 0; }
    }, true);
  },
  updated(el, binding) { el._lp = binding.value; },
};
