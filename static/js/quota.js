// 额度：各号的 5 小时和本周额度。服务器从状态栏脚本存的快照和网页对话收到的限额事件里汇总，
// 这里只管取回来、画到看板的账号标签上（后面一根当前号的 5 小时血条），以及账号面板、新会话标签页里每个号的额度。
// 界面上一律写剩下多少（0% 是用满了），服务器给的是用了多少，在这里倒过来。血条都是直的
const { reactive, computed, watch } = Vue;
import { api, ago, shortSpan } from './util.js';
import { promoTag as accountPromoTag, promoFactor } from './points.js';

// 5 小时额度的血条：总长是 100%，左边绿的是还能用的（固定绿色；百分比按剩多少分绿、黄、红三档），
// 接着淡红、和绿的一样高、呼吸闪烁的是最近 30 分钟用掉的（不会回来，不是还能用的血），再往右空着（更早用掉的）。本周额度快用完时，这个 5 小时窗口剩得再多也用不满，
// 血条右端按本周剩的折算砍掉一截（clip），还能用的那段和百分比都只算砍剩的。
// 预定额度（见 reserve.py）：别人预定的部分服务器已经算成用掉了（hold，画在右边的空槽里，不画红色），
// 自己的预定画成蓝的：周预定深蓝，5 小时预定浅一点、会呼吸。各段从左到右是保底的周预定、保底的 5 小时预定、绿的、
// 先用的周预定、先用的 5 小时预定——消耗从右往左吃，先用的在最右边（QuotaSegs，看板小标签里的小血条也用它）。
// q 是 quotaCells 给的一个号的格子。看板账号面板和新会话标签页的账号表都用它：外层不占位，血条和百分比各是表格的一格，
// 百分比后面跟着「in 2h13m」。本周额度剩 1% 以内的号不用它：那一行只有号名和一句「本周用完了，几时重置」（真用完和被别人订掉一样写），
// 排在能用的号后面（index.html 的两张账号表、app.js 的 accountGroups；官方读数真到 100% 也这么写）。
// 权威读数的百分比带 q-auth（垫一层淡底），按拟合比例算出来的不带
export const AUTH_TIP = '权威读数：来自 /usage、状态栏或限额事件';
export const QuotaSegs = {
  props: { f: Object },
  template: `<i v-for="s in f.bars" :key="s.key" class="q-seg" :class="[s.cls, { end: s.end }]" :style="{ width: s.w + '%' }"></i><i
    class="q-recent" :style="{ width: (f.recent || 0) + '%' }"></i>`,
};
export const QuotaBar = {
  props: { q: Object },
  components: { QuotaSegs },
  data: () => ({ authTip: AUTH_TIP }),
  template: `<div class="q-bar-row">
    <span class="q-bar" :style="{ clipPath: q.five?.clip || null }"><quota-segs
      v-if="q.five" :f="q.five"></quota-segs></span>
    <b class="q-bar-pct" :class="[q.five?.cls, { 'q-auth': q.five?.auth, 'q-reset': q.five?.isReset }]">{{ q.five ? q.five.value : '–' }}<small
      v-if="q.five?.mineShort" class="q-mine">{{ q.five.mineShort }}</small></b>
    <!-- in 单独一格（没有也占着），上下各行的 in 才左边对齐；「in 」单独包一层，简洁模式手机上的新会话账号卡片藏掉它。
         q-in-short 是照侧边栏只写一个单位的写法（「2h」），只有手机上的账号卡片用它，省列宽 -->
    <small class="q-in"><template v-if="q.five?.inText"><span class="q-in-long"><span class="q-in-pre">in </span>{{ q.five.inText }}</span><span
      class="q-in-short">{{ q.five.inShort }}</span></template></small>
  </div>`,
};

// fallback：新会话标签页没指定账号时，按设置此刻会用哪个号（在 app.js 里接上）
export function useQuota({ current, readonly, fallback }) {
  // busy：正在手动刷新；errors：上次刷新时各号的出错原因；loadError：后台取最新额度失败的原因
  // （每分钟自动取一次，失败了不弹提示条免得刷屏，账号面板里写一行，数字旁本来就标着是几分钟前查的）
  // fiveInWeek：5 小时窗口用满约合本周额度的多少（服务器拟合的，见 server.FIVE_IN_WEEK）；旧版服务器不给是 null。
  // GPT 号（ChatGPT 的订阅）各有各的，在 fiveInWeekOf 里。weekOnly：没有 5 小时限额、只有本周额度的号，血条画本周的
  // expires：号 → 到期时刻（秒）；旧版服务器不给就是空的，倒计时照旧只看重置时刻
  // balances：API 号（DeepSeek，按 token 付费、没有额度窗口）最近一次查到的平台余额，号 → {total 元, available, ts}；
  // peak：这些号此刻按高峰还是空闲价算、到几点换（号 → {peak, until}）。旧后端不给就是空的
  const quota = reactive({ accounts: {}, fiveInWeek: null, fiveInWeekOf: {}, weekOnly: [], expires: {}, balances: {}, peak: {},
                           busy: false, errors: {}, loadError: '' });
  let at = 0;
  // 读的是服务器上的小文件，很便宜；悬停面板时 10 秒内重复悬停不再取
  async function loadQuota(force = false) {
    if (readonly.value || (!force && Date.now() - at < 10000)) return;
    at = Date.now();
    try {
      const r = await api('/api/quota?limit=0');
      Object.assign(quota, { accounts: r.accounts || {}, fiveInWeek: r.fiveInWeek ?? null, fiveInWeekOf: r.fiveInWeekOf || {},
                             weekOnly: r.weekOnly || [], expires: r.expires || {}, balances: r.balances || {}, peak: r.peak || {} });
      quota.loadError = '';
    } catch (e) {
      at = 0;
      quota.loadError = e.message;
    }
  }
  watch(readonly, v => v || loadQuota(true), { immediate: true });
  // 手动刷新：服务器给每个号开一次交互界面跑 /usage，大约十秒
  async function refreshQuota() {
    if (readonly.value || quota.busy) return;
    quota.busy = true;
    try {
      const r = await api('/api/quota/refresh', { method: 'POST' });
      Object.assign(quota, { accounts: r.accounts || {}, fiveInWeek: r.fiveInWeek ?? null, fiveInWeekOf: r.fiveInWeekOf || {},
                             weekOnly: r.weekOnly || [], expires: r.expires || {}, balances: r.balances || {}, peak: r.peak || {},
                             errors: r.errors || {} });
      at = Date.now();
    } catch (e) {
      quota.errors = { 刷新: e.message };
    }
    quota.busy = false;
  }
  setInterval(() => loadQuota(true), 60000);

  // 某个号某个窗口的用量；窗口已经过了重置时间，旧数字就不算了
  function windowOf(name, key) {
    const w = quota.accounts[name]?.[key];
    if (!w || w.used_percentage == null) return null;
    return w.resets_at && w.resets_at * 1000 < Date.now() ? { ...w, reset: true } : w;
  }
  // 剩下多少（百分点）：已过重置时间的窗口是满的；别人预定的部分（hold，服务器按看的人算好）当成已经用掉
  const restOf = w => Math.max(0, (w.reset ? 100 : Math.min(100, Math.max(0, 100 - w.used_percentage))) - (w.hold || 0));
  // 这个人在这个号上手里还有没有预定的蓝（余额透支时，有蓝的限量号照样能用，见 users.paid_why）
  const hasBlue = name => { const m = quota.accounts[name]?.mine; return !!m && m.ff + m.fb + m.wf + m.wb > 0.05; };
  const pct = w => `${Math.round(restOf(w))}%`;

  // 看板上账号标签显示的号：会话用的号；新会话标签页没指定时，是按设置此刻会用的那个
  const acctName = computed(() => current.value?.account || (current.value?.fresh ? fallback?.() || '' : ''));
  // 看板上的账号小标签：名字、5 小时还剩多少，后面一根直的小血条（和账号面板里的同一份数：按本周砍短、分三档上色）。
  // 不写本周。没查到、或已过重置时间还没查到新数的写「–」、不画血条，不拿旧数顶着；
  // 本周额度用完了，5 小时剩多少没意义，只写一句灰字。
  // warn 是号名字的提醒色，按标签上写的那个百分比分档：不到 40% 黄、不到 30% 橙、不到 20% 红、
  // 不到 10%（含本周用完了）红色还呼吸；没有读数的不上色
  const acctChip = computed(() => {
    const q = quotaCells(acctName.value);
    const pk = q.peak ? ` · ${q.peak.label}` : '';
    if (q.balance) return { text: q.balance.text + pk, bar: null, warn: q.balance.low ? 'warn-r' : '' };
    if (q.weekOut) return { weekOut: true, warn: 'warn-r warn-pulse' };
    if (!q.five || q.five.passed) return { text: '–' + pk, bar: null, warn: '' };
    const r = Math.round(q.five.rest);
    return { text: q.five.value + pk, bar: q.five,
             warn: r < 10 ? 'warn-r warn-pulse' : r < 20 ? 'warn-r' : r < 30 ? 'warn-o' : r < 40 ? 'warn-y' : '' };
  });

  // 剩余额度按颜色分档：一半以上绿、剩两成以上黄、再少就红（血条还能用的那段和百分比都用它）
  const tier = rest => (rest <= 20 ? 'q-low' : rest <= 50 ? 'q-mid' : 'q-ok');

  // 一个号的额度格子。本周那格在账号面板的表里（定语在表头上，这里只给数）。
  // 5 小时那格画成血条（QuotaBar）：rest 还能用的、recent 最近 30 分钟用掉的（服务器从额度历史里减出来的，不知道是 null）。
  // 还能用的 = 5 小时剩的和本周剩的折成 5 小时百分点（÷ fiveInWeek）里较小的那个；本周不够时 clip 把血条右端砍掉多出来的那截，
  // capText 说明为什么。已过重置时间的 5 小时窗口按满的算（passed），本周不够照样砍。
  // 重置：5 小时窗口写还有多久，本周的写日期和钟点；inText 是跟在百分比后面的紧凑写法（5 小时「2h13m」、本周「2d5h」）。
  // 查询时间取两项里较新的那个：ts 是「几分钟前」的写法，tsAt 是原始时刻（秒，没有是 0），给简洁模式的悬停卡片按侧栏的写法另写。
  // 没查到的窗口给 null，界面上写「–」。
  // auth：这个百分比是权威读数（/usage、状态栏、限额事件，或由它们直接算出来的，比如已重置）；按拟合比例砍过的不算。
  // out：估计用不了了，5 小时写着 0% 或本周订完了（没过重置时间）；weekOut：本周那个窗口写着 1% 以内。week.at 是本周重置的时间戳（秒，没有是 0），给账号表排序
  // 5 小时还剩 F、本周还剩 W（都已扣掉别人的预定；W 为 null 是不按本周砍）、自己的预定 mine：此刻能用的部分按 5 小时百分点
  // 拆成几段，按消耗先后：ff 先用的 5 小时预定、wf 先用的周预定、g 绿的、fb 保底的 5 小时预定、wb 保底的周预定。
  // 和服务器 reserve.split 是同一套算法（预测拦截用那边的），改一处要改两处
  function segments(F, W, mine, k) {
    const m = mine || { ff: 0, fb: 0, wf: 0, wb: 0, lock: 0 };
    const ff = Math.min(m.ff, F), fb = Math.min(m.fb, F - ff), room = F - ff - fb;
    const free = W == null ? Infinity : Math.max(0, W / k - m.lock / k);
    const wf = Math.min(m.wf / k, room), g = Math.min(free, room - wf), wb = Math.min(m.wb / k, room - wf - g);
    return { ff, wf, g, fb, wb, total: ff + wf + g + fb + wb };
  }
  // 没有 5 小时限额的号（weekOnly）：5 小时那格照样画血条，画的是本周额度（本周百分点，k 当 1，不用再按本周砍）
  // 智谱（订阅制的 API 号）：服务器存在余额那处的是 5 小时和本周用了百分之几、几时重置（api_relay.quota），
  // 换成和 Claude 号一样的窗口，账号表里照样画血条、写百分比和重置倒数，和别的号对齐。
  // 用满 5 小时约合本周多少没有拟合过，不按本周砍血条；没有近 30 分钟用量
  function subWindow(b, used, at) {
    const w = { used_percentage: used, resets_at: at || 0, ts: b.ts };
    return at && at * 1000 < Date.now() ? { ...w, reset: true } : w;
  }
  function quotaCells(name) {
    const b = quota.balances[name], pk = quota.peak[name], sub = !!b && b.five != null;
    // 按高峰还是空闲价算，挂在号名右上角，只写一个字，不写到几点
    const peak = pk ? { label: pk.peak ? '峰' : '谷', off: !pk.peak } : null;
    const week = sub ? subWindow(b, b.week, b.week_reset) : windowOf(name, 'seven_day'), weekOnly = quota.weekOnly.includes(name);
    const five = sub ? subWindow(b, b.five, b.five_reset) : weekOnly ? week : windowOf(name, 'five_hour'), mine = quota.accounts[name]?.mine || null;
    // 号在窗口重置之前就到期（订阅到头），「in 2d5h」改按到期时刻倒数，悬停写明原因；reset 那句「几时重置」照旧写重置时刻
    const exp = quota.expires[name];
    const inOf = s => (exp && exp < s ? short(exp) : short(s));
    const inShortOf = s => shortSpan(Math.max(0, (exp && exp < s ? exp : s) - Date.now() / 1000));
    const inTip = s => (exp && exp < s ? `该账号于 ${when(exp)} 到期，早于本窗口重置时间（${when(s)}），按到期时间倒计时` : '');
    const wk = !week ? null : week.reset ? { value: '已重置', cls: 'q-ok', reset: '', inText: '', rest: 100, isReset: true, auth: true }
      : { value: pct(week), reset: week.resets_at ? when(week.resets_at) : '', inText: week.resets_at ? inOf(week.resets_at) : '',
          inShort: week.resets_at ? inShortOf(week.resets_at) : '',
          inTip: week.resets_at ? inTip(week.resets_at) : '', at: week.resets_at || 0,
          rest: restOf(week), cls: tier(restOf(week)), auth: !week.hold };
    // 本周剩的里面有多少是自己订的（周预定加上 5 小时预定占的周额度），只给自己看
    if (wk && mine?.lock > 0.05) Object.assign(wk, { mine: `其中您已预定 ${mine.lock.toFixed(1)}%（含周预定及 5 小时预定占用的周额度）`,
                                                     mineShort: `订${mine.lock.toFixed(1)}` });
    let f = null;
    if (five) {
      const k = sub ? null : weekOnly ? 1 : quota.fiveInWeekOf[name] ?? quota.fiveInWeek, rest = restOf(five);
      const sp = segments(rest, wk && k ? wk.rest : null, mine, k || 1);
      const usable = sp.total;
      const capped = usable < rest - 0.5;   // 差不到半个点就不砍，免得取整后数字一样、血条却短了一丝
      const shown = capped ? usable : rest;
      const blue5 = sp.ff + sp.fb, blueW = sp.wf + sp.wb;
      // 从左到右：保底的周、保底的 5 小时、绿的、先用的周、先用的 5 小时；最右边那段带圆头
      const bars = [['wb', 'q-blue-w', sp.wb], ['fb', 'q-blue-5', sp.fb], ['g', 'q-rest', capped ? sp.g : sp.g + rest - usable],
                    ['wf', 'q-blue-w', sp.wf], ['ff', 'q-blue-5', sp.ff]]
        .filter(b => b[2] > 0.05).map(([key, cls, w]) => ({ key, cls, w }));
      if (bars.length) bars[bars.length - 1].end = true;
      f = { rest: shown, green: sp.g, bars, cls: tier(shown), passed: !!five.reset, isReset: !!five.reset && !capped && !five.hold,
            value: five.reset && !capped && !five.hold ? '已重置' : `${Math.round(shown)}%`,
            recent: five.reset ? 0 : five.recent == null ? null : Math.min(five.recent, 100 - rest),
            reset: !five.reset && five.resets_at ? left(five.resets_at) : '',
            inText: !five.reset && five.resets_at ? inOf(five.resets_at) : '',
            inShort: !five.reset && five.resets_at ? inShortOf(five.resets_at) : '', inTip: !five.reset && five.resets_at ? inTip(five.resets_at) : '',
            auth: !capped && !five.hold,
            clip: capped ? `inset(0 ${rest - usable}% 0 0 round 99px)` : '',
            capText: [weekOnly ? '该账号无 5 小时限额，额度条显示本周额度' : '', capped ? `本周剩余额度不足以用满本窗口（本周剩余 ${Math.round(wk.rest)}%，用满 5 小时额度约占本周 ${(k * 100).toFixed(1)}%${mine?.lock ? '，其中含您的预定' : ''}），本窗口最多还可使用 ${Math.round(usable)}%`
              : wk && k == null && !sub ? '服务器版本过旧，额度条未按本周额度折算' : '',
              blue5 + blueW > 0.05 ? `蓝色为您的预定：5 小时预定 ${blue5.toFixed(1)}%（浅蓝，本窗口结束后作废）、周预定 ${blueW.toFixed(1)}%（深蓝，本周结束后作废），使用预定额度不另扣费` : '']
              .filter(Boolean).join('；') };
      if (!five.reset && !weekOnly && !sub) f.recentText = f.recent == null ? '暂无近 30 分钟用量数据' : `近 30 分钟已用 ${Math.round(f.recent)}%`;
      // 当前这个窗口里自己订下、还没用掉的 5 小时预定，跟在百分比后面写「订x」（和本周那格一样，只给自己看）
      const held5 = (mine?.ff || 0) + (mine?.fb || 0);
      if (held5 > 0.05) Object.assign(f, { mineShort: `订${held5.toFixed(1)}`,
                                           mine: `其中您在本窗口预定且尚未使用的 ${held5.toFixed(1)}%` });
    }
    // API 号（DeepSeek）：没有额度窗口，账号表里那几格换成一格余额（index.html 的 acct-balance），后面灰字写「峰到 18:00」；
    // 平台说没钱了（available 为假）也照样能选，发话时上游自己会拒
    const balance = sub || (!b && !pk) ? null : {
      text: b?.unavailable || (b && b.total != null ? `余额 ${b.total.toFixed(2)} 元` : '无法查询余额'), low: !!b && b.available === false,
      until: peak && pk.until ? `${peak.label}到 ${when(pk.until)}` : '' };
    const ts = Math.max(five?.ts || 0, week?.ts || 0, b?.ts || 0);
    if (balance) return { five: null, week: null, balance, peak, ts: ts ? ago(ts) : '', tsAt: ts, out: false, weekOut: false };
    // 按界面上写的那个整数百分比判断，不按原始小数：别人的预定常把一个号吃到只剩 0.004% 这种零头，
    // 写出来是 0%，按小数判断却算还有余量，就留在能用的号里了。本周剩 1% 以内（写出来是 0% 或 1%）就算订完
    // （自己订下的那 1% 也一样）
    const spent = (c, n) => !!c && !c.isReset && Math.round(c.rest) <= n;
    const weekOut = spent(wk, 1);
    return { five: f, week: wk, peak, ts: ts ? ago(ts) : '', tsAt: ts, out: spent(f, 0) || weekOut, weekOut };
  }
  // 离重置还有多久：几小时几分（不到一小时就只写几分）
  function left(s) {
    const m = Math.max(0, Math.round((s * 1000 - Date.now()) / 60000));
    return m >= 60 ? `${Math.floor(m / 60)}小时${m % 60 ? `${m % 60}分` : ''}` : `${m}分`;
  }

  // 离重置还有多久的紧凑写法，跟在百分比后面：一天以上「2d5h」，一小时以上「2h13m」，再短「13m」
  function short(s) {
    const m = Math.max(0, Math.round((s * 1000 - Date.now()) / 60000));
    const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
    return d ? `${d}d${h}h` : h ? `${h}h${m % 60}m` : `${m}m`;
  }

  // 本周的重置时间：当天的只写钟点，不是当天的带上月/日
  function when(s) {
    const d = new Date(s * 1000), now = new Date();
    const hm = d.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
    return d.toDateString() === now.toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  }

  // 账号表的基础排法（和自动挑号对齐）：和 server.auto_ranked 同一套规矩，改一处要改两处。
  // 显示本周订完的号由 app.js 的 sortedAccounts 单独置后、按重置时间排，不走这套基础排法。
  // 自己有先用的预定、血条右边此刻画着蓝条的整组在前；此刻付不起的限量号（cant）整组在后；
  // 组里只看预测绿条：血条上 5 小时剩的（本周不够时已经砍短了）减掉一半红的（最近 30 分钟用掉的）。
  // 它超过 ROOMY 的都算足够充裕、不再比多少，其中本周最快重置的在前，然后是不够充裕的、它多的在前，没读数的最后；一样时保持账号表的顺序（sort 是稳定的）。返回能逐项比大小的数组
  // API 号（api 为真）不进服务器的自动挑号，只在账号表里照这套规矩排（它们也该一起往前排，至少排在用不了的号前面）：
  // 智谱这类订阅制的（余额那处存着 5 小时读数）照血条排，本周重置按它自己的 week_reset；
  // DeepSeek、中转站这类按 token 付费、没有额度窗口的不会用满，算作充裕、排在充裕的号里最后（它没有到期作废的额度，先用快重置的订阅号）；
  // 平台说没钱了的（balance.low）算作用光
  const ROOMY = 50;
  function rankKey(name, cant, api = false) {
    const bal = quota.balances[name], q = quotaCells(name), f = q.five;
    const blue = f ? f.bars.filter(b => b.key === 'ff' || b.key === 'wf').reduce((s, b) => s + b.w, 0) : 0;
    const green = f ? f.rest - (f.recent || 0) / 2 : 0;
    let key;
    if (!f) key = !api || (bal && 'five' in bal) ? [2, 0] : q.balance?.low ? [1, 0] : [0, Infinity];
    else if (green > ROOMY) {
      // 已经过了的重置时刻（新的一周还没读到）按 7 天一轮往后推；不知道的排在充裕的里面最后
      // 订阅在那之前就到期的按到期时刻排，已经到期的照旧（同 instance.week_end）
      const now = Date.now() / 1000, exp = quota.expires[name];
      let at = (bal ? bal.week_reset : quota.accounts[name]?.seven_day?.resets_at) || Infinity;
      while (at <= now) at += 7 * 86400;
      if (exp > now && exp < at) at = exp;
      key = [0, at];
    } else key = [1, -green];
    return [blue > 0.05 ? 0 : 1, cant ? 1 : 0, ...key];
  }
  const byRank = (x, y) => { for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1; return 0; };

  // 半价要有公共绿条才有意义：已订完或绿条不足 1% 就不挂标签，自己的蓝条不算；没读数时不当成已经用完。
  function promoTag(name) {
    const tag = accountPromoTag(name);
    if (!tag || promoFactor(name, Date.now() / 1000) !== 0.5) return tag;
    const q = quotaCells(name);
    return q.weekOut || (q.five && q.five.green < 1) ? null : tag;
  }

  return { quota, loadQuota, refreshQuota, acctName, acctChip, quotaCells, hasBlue, promoTag, rankKey, byRank };
}
