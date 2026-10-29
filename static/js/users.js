// 用户（后端见 users.py）：设置卡片最上面那行身份、注册登录、改密码、自己的账本、查看器账号的用户管理，
// 以及会话的主人和可见范围、限量号能不能用。
// 登录、注册、退出以后整页刷新：标签页、推送连接、各处列表都按新身份重来一遍，不用一样样去换
const { reactive, ref, computed } = Vue;
import { api, me, say, fail } from './util.js';
import { withTab } from './tabs.js';
import { spentMoney, spentPoints, FEN, yuan, fen } from './points.js';
import { PLAN_PARTS, PLAN_DAYS, dayText } from './plan.js';

// 日期框（<input type="date">）的值：本机时区的 YYYY-MM-DD
const dateStr = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

// 账本、余额：服务器给的是点，这里换成人民币元写（见 points.js 的 FEN、yuan），和别处一样留三位小数
export const fmtPts = n => `${n < 0 ? '−' : ''}${yuan(Math.abs(n || 0) * FEN)}`;

// view 是全部会话那边存在浏览器里的看法（sessions.js 的 ovView），用户管理借它的「最近多少分钟」和自己的排序
export function useUsers({ current, openTab, view, promoTag }) {
  async function loadMe() {
    try {
      // owns 先清空：旧服务器不给它，退出登录后也不能留着上一个人的号
      Object.assign(me, { owns: [] }, await api('/api/me'), { loaded: true, error: '' });
    } catch (e) {
      me.error = e.message;
      fail('验证身份', e);
    }
  }
  // 标签页按身份分开记，恢复前要先知道是谁（tabs.js 的 load 等它）
  const meReady = loadMe();
  // 余额：多用户实例里登录了就显示；单用户实例只在配了限量号、或者收到过额度时才有意义
  const showBalance = computed(() => !!me.name && me.balance != null && (me.multi || me.paid.length > 0 || !!me.granted));

  // 登录和注册共用一个小窗口，mode 切换
  const auth = reactive({ open: false, mode: 'login', name: '', password: '', again: '', error: '', busy: false });
  const openAuth = (mode = 'login') => Object.assign(auth, { open: true, mode, password: '', again: '', error: '' });
  async function submitAuth() {
    if (auth.busy) return;
    if (auth.mode === 'register' && auth.password !== auth.again) return (auth.error = '两次输入的密码不一致');
    Object.assign(auth, { busy: true, error: '' });
    try {
      await api(`/api/${auth.mode}`, { method: 'POST', body: { name: auth.name, password: auth.password } });
      say(auth.mode === 'register' ? '注册成功，正在刷新页面…' : '登录成功，正在刷新页面…', { ms: 0 });
      location.reload();
    } catch (e) {
      Object.assign(auth, { error: e.message, busy: false });
    }
  }
  const loggingOut = ref(false);
  async function logout() {
    if (loggingOut.value) return;
    loggingOut.value = true;
    try {
      await api('/api/logout', { method: 'POST' });
      // 侧边栏存在这台设备上的会话列表（sessions.js 的 sideKey）跟着清掉，别人再用这台设备时不留会话标题；
      // 清不掉就说出来，晚几秒再刷新，好让人看到
      let wait = 0;
      try {
        for (const k of Object.keys(localStorage)) {
          if (k.startsWith(`sideRows:${me.name || ''}:`) || k === `freshTags:${me.name || ''}`) localStorage.removeItem(k);
        }
      } catch (e) {
        fail('清除本设备的会话列表', e);
        wait = 4000;
      }
      say('已退出，正在刷新页面…', { ms: 0 });
      setTimeout(() => location.reload(), wait);
    } catch (e) {
      loggingOut.value = false;
      fail('退出登录', e);
    }
  }

  // 改自己的密码：别的设备上的登录随之失效，这台照常登录着
  const pw = reactive({ open: false, old: '', fresh: '', again: '', error: '', busy: false });
  const openPassword = () => Object.assign(pw, { open: true, old: '', fresh: '', again: '', error: '' });
  async function submitPassword() {
    if (pw.busy) return;
    if (pw.fresh !== pw.again) return (pw.error = '两次输入的新密码不一致');
    Object.assign(pw, { busy: true, error: '' });
    try {
      await api('/api/password', { method: 'POST', body: { old: pw.old, new: pw.fresh } });
      pw.open = false;
      say('密码已修改，其他设备需重新登录');
    } catch (e) { pw.error = e.message; }
    pw.busy = false;
  }

  // 账本：自己的，或者查看器账号在用户管理里点开某个人的
  const ledger = reactive({ open: false, loading: false, error: '', data: null, name: '' });
  async function openLedger(name = '') {
    Object.assign(ledger, { open: true, loading: true, error: '', data: null, name });
    try {
      ledger.data = await api(name ? `/api/admin/ledger?name=${encodeURIComponent(name)}` : '/api/ledger');
    } catch (e) { ledger.error = e.message; }
    ledger.loading = false;
  }
  const openFromLedger = s => openTab(s.path).then(() => (ledger.open = admin.open = false), e => fail('打开会话', e));

  // 余额看板：谁都能看，除查看器账号以外每个人的余额，还有最近 N 分钟的用量（N 同用户管理，view.mins）。
  // 服务器按余额从多到少排好；view.boardSort 换成注册先后 join、最近用量 recent 时在这里重排。
  // 最近用量和会话那边一样按实际用掉的算：蓝的是由预定付掉的调用，不是这段时间下单付的预定，排序按三段的和（就是 all）
  const board = reactive({ open: false, loading: false, error: '', users: [], overdraft: null, recentOf: 0 });
  async function openBoard() {
    Object.assign(board, { open: true, loading: true, error: '' });
    try {
      const mins = view.mins;
      if (!Number.isFinite(mins) || mins <= 0 || mins > 43200) throw new Error('分钟数须大于 0 且不超过 43200');
      const r = await api(`/api/board?mins=${mins}`);
      // 旧版服务器不认 mins、名单上不带 recent：那是取不到，不能写成「没有用量」
      const stale = r.users.some(u => !u.recent);
      Object.assign(board, { users: r.users, overdraft: r.overdraft, recentOf: stale ? 0 : mins });
      if (stale) board.error = '服务器版本过旧，暂不支持最近用量，已按余额排序';
    } catch (e) { board.error = e.message; }
    board.loading = false;
  }
  const boardUsers = computed(() => {
    const s = view.boardSort;
    const used = u => spentPoints(u.recent?.all) || 0;
    if (s === 'recent' && board.recentOf) return [...board.users].sort((a, b) => used(b) - used(a));
    if (s === 'join') return [...board.users].sort((a, b) => (a.ts || 0) - (b.ts || 0));
    return board.users;
  });

  // 用户管理（查看器账号）：名单上每人的账和最近 N 分钟的用量（免费、限量分开写），点一个人进他自己的那页（sel）：
  // 发额度（按月计划或一次发放，一次发放填负数就是扣）、他的额度计划（能撤销）、重设密码、运行环境。
  // N 和全部会话那边共用（view.mins）；排序 view.adminSort：注册先后 join，或最近用量 recent（按花钱算：免费、照常扣钱、下单付的预定的和）。
  // orphan 是无主会话最近的用量（单用户实例没有），不是一个人，不参与排序，放在名单最后
  const admin = reactive({ open: false, loading: false, error: '', users: [], orphan: null, recentOf: 0, busy: '', sel: '', revoking: '',
                           grant: { mode: 'plan', amount: '', note: '' }, reset: { password: '' } });
  async function loadAdmin() {
    const mins = view.mins;
    if (!Number.isFinite(mins) || mins <= 0 || mins > 43200) throw new Error('分钟数须大于 0 且不超过 43200');
    const r = await api(`/api/admin/users?mins=${mins}`);
    // 旧版服务器不认 mins、名单上不带 recent：那是取不到，不能写成「没有用量」
    const stale = r.users.some(u => !u.recent);
    Object.assign(admin, { users: r.users, orphan: r.orphan ?? null, recentOf: stale ? 0 : mins });
    if (stale) throw new Error('服务器版本过旧，暂不支持最近用量');
  }
  async function openAdmin() {
    Object.assign(admin, { open: true, loading: true, error: '' });
    try {
      await loadAdmin();
    } catch (e) { admin.error = e.message; }
    admin.loading = false;
  }
  // 点进某个人、回到名单：上一个人填了一半的金额、备注、开始日期、新密码不带过去
  function openUser(name) {
    Object.assign(admin, { sel: name, error: '', revoking: '' });
    Object.assign(admin.grant, { amount: '', note: '', start: dateStr(new Date()) });
    admin.reset.password = '';
  }
  const adminUser = computed(() => admin.users.find(u => u.name === admin.sel) || null);
  // 还有没发完的计划：名单上标「计划中」
  const planning = u => (u.plans || []).some(p => !p.revoked && p.items.some(it => !it.done));
  // 发放那一栏底下的一行说明：按月计划写清哪天发多少，一次发放写清是马上进余额
  // 开始日期（按月计划，可以从以前的某一天算起）：选今天就从此刻算（和不传一样），别的日子从那天 0 点（本机时区）算
  const startOf = s => (s && s !== dateStr(new Date()) ? new Date(`${s}T00:00:00`).getTime() / 1000 : null);
  const grantHint = computed(() => {
    const g = admin.grant, pts = Number(g.amount) * 100 / FEN;
    if (g.mode !== 'plan') return '立即计入对方余额；填负数为扣除';
    if (!(pts > 0)) return `填写总额，分 ${PLAN_PARTS} 次发放，从开始日期起每 ${PLAN_DAYS} 天发放 1/${PLAN_PARTS}`;
    const now = Date.now() / 1000, t0 = startOf(g.start) ?? now;
    const dues = Array.from({ length: PLAN_PARTS }, (_, i) => t0 + i * PLAN_DAYS * 86400);
    const got = dues.filter(t => t <= now), rest = dues.filter(t => t > now);
    return [got.length && `建好即到账 ${fen(pts * got.length / PLAN_PARTS, 2)}（${got.map(dayText).join('、')}${got.length > 1 ? ` 共 ${got.length} 期` : ''}）`,
            rest.length && `${rest.map(dayText).join('、')} 各发放 ${fen(pts / PLAN_PARTS, 2)}`].filter(Boolean).join('，');
  });
  // 按花钱算：免费号的、限量号照常扣钱的，加上这段时间下单付的预定（预定付掉的调用不再算，见 points.spentMoney）
  const recentPts = u => spentMoney(u?.recent);
  const adminUsers = computed(() => view.adminSort !== 'recent' ? admin.users
    : [...admin.users].sort((a, b) => recentPts(b) - recentPts(a)));
  async function grantPoints() {
    // 查看器账号填的是元（界面上都写元），服务器收的是点
    const g = admin.grant, user = admin.sel, amount = Number(g.amount) * 100 / FEN, plan = g.mode === 'plan';
    if (admin.busy || !user) return;
    if (plan && !(amount > 0)) return (admin.error = '请输入大于零的总额（元）');
    if (!g.amount || !Number.isFinite(amount) || !amount) return (admin.error = '请输入非零金额（元）');
    Object.assign(admin, { busy: 'grant', error: '' });
    try {
      if (plan) {
        const start = startOf(g.start);
        const p = await api('/api/admin/grant-plan', { method: 'POST', body: { user, amount, note: g.note, ...(start == null ? {} : { start }) } }).catch(e => {
          // 网页文件是新的、服务器还没换上新版时没有这个接口
          throw e.status === 404 || e.status === 405 ? new Error('服务器尚未更新，暂不支持按月计划，请改用一次发放') : e;
        });
        say(`已为 ${user} 建立额度计划，共 ${fmtPts(amount)}，已到账 ${fmtPts(p.given)}`);
      } else {
        await api('/api/admin/grant', { method: 'POST', body: { user, amount, note: g.note } });
        say(amount > 0 ? `已向 ${user} 发放 ${fmtPts(amount)}` : `已扣除 ${user} 的 ${fmtPts(-amount)}`);
      }
      Object.assign(g, { amount: '', note: '' });
      if (user === me.name) loadMe();
    } catch (e) { admin.error = e.message; }
    // 发出去了才重取名单；名单没取到要说出来，不然看着像没发上
    if (!admin.error) {
      try { await loadAdmin(); } catch (e) { admin.error = `额度已发放，但用户列表刷新失败：${e.message}`; }
    }
    admin.busy = '';
  }
  // 撤销计划：当作没发生过，已发的几期一起收回（users.revoke_plan）。先在计划底下展开确认（revoking），点确认才发
  async function revokePlan(p) {
    if (admin.busy) return;
    Object.assign(admin, { busy: 'revoke', error: '' });
    try {
      await api('/api/admin/grant-plan/revoke', { method: 'POST', body: { id: p.id } });
      say(`已撤销 ${p.user} 的额度计划，已发放的 ${fmtPts(p.given)} 一并收回`);
      admin.revoking = '';
      if (p.user === me.name) loadMe();
    } catch (e) { admin.error = e.message; }
    if (!admin.error) {
      try { await loadAdmin(); } catch (e) { admin.error = `计划已撤销，但用户列表刷新失败：${e.message}`; }
    }
    admin.busy = '';
  }
  async function resetPassword() {
    const r = admin.reset, user = admin.sel;
    if (admin.busy || !user) return;
    if (!r.password) return (admin.error = '请填写新密码');
    Object.assign(admin, { busy: 'reset', error: '' });
    try {
      await api('/api/admin/password', { method: 'POST', body: { name: user, password: r.password } });
      say(`${user} 的密码已重设，需在所有设备上重新登录`);
      r.password = '';
    } catch (e) { admin.error = e.message; }
    admin.busy = '';
  }

  // 只用独立环境还是也能选共用环境（服务器 users.is_new / set_new，字段名沿用 newUser）：只改这个人以后在界面上看到什么，
  // 已有的会话、建好的独立环境都不动
  async function setNewUser(u, on) {
    if (admin.busy) return;
    Object.assign(admin, { busy: 'new:' + u.name, error: '' });
    try {
      await api('/api/admin/newuser', { method: 'POST', body: { name: u.name, on } });
      say(`${u.name} ${on ? '已改为只用独立环境' : '已可选择共用环境'}，刷新页面后生效`);
      if (u.name === me.name) loadMe();
      await loadAdmin();
    } catch (e) { admin.error = e.message; }
    admin.busy = '';
  }

  // 流量（查看器账号）：网络闸门记下的每条连接加起来（server.admin_traffic），按会话、按号；
  // 某个小时传得特别多的会话标红（alert）。最近多少小时由查看器账号填，取不到要说出来，不能当成没有流量
  const traffic = reactive({ open: false, loading: false, error: '', hours: 24, data: null });
  async function openTraffic() {
    const h = Number(traffic.hours);
    Object.assign(traffic, { open: true, error: '' });
    if (!Number.isFinite(h) || h <= 0 || h > 720) return (traffic.error = '小时数须大于 0 且不超过 720');
    traffic.loading = true;
    try {
      traffic.data = await api(`/api/admin/traffic?hours=${h}`);
    } catch (e) { traffic.error = `无法获取流量：${e.message}`; }
    traffic.loading = false;
  }
  const mbText = n => (n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${(n / 1e6).toFixed(1)} MB`);

  // 可见范围：看板上那个小标签的面板。用户名单打开面板时取，半分钟内不重取
  const userNames = ref([]), namesError = ref('');
  let namesAt = 0;
  async function loadNames() {
    if (!me.name || Date.now() - namesAt < 30000) return;
    namesAt = Date.now();
    try {
      userNames.value = (await api('/api/users')).users;
      namesError.value = '';
    } catch (e) {
      namesAt = 0;
      namesError.value = `用户列表没取到：${e.message}`;
    }
  }
  const canShare = tab => !!tab?.owner && (me.admin || me.name === tab.owner);
  const canPublicShare = tab => !!tab && !tab.fresh && !!me.name && (me.admin || (!!tab.owner && me.name === tab.owner));
  // 会话主人自己也算一个人
  const visText = s => (!s?.owner ? '无主' : s.visible === 'public' ? 'public'
    : s.visible?.length ? `visible to ${s.visible.length + 1}` : 'private');
  // 查看器账号才看得到的别人的私有会话：卡片上标出来
  const lockedForOthers = s => !!s?.owner && s.visible !== 'public' && me.name !== s.owner && !(s.visible || []).includes(me.name);
  // 能不能往这个会话里发话：别人的公开会话、靠分享链接看到的会话只能看（和 users.use_why 一个规矩，后端也拦）；空字符串就是能。
  // 说明写得客气、不提谁能发话：输入框不在了一看就明白（「只有 X 能往里发话，你只能看」这种提示味太冲）
  const useBlock = tab => (me.multi && !tab?.fresh && tab?.owner && !me.admin && me.name !== tab.owner
    && (tab.visible === 'public' || !(tab.visible || []).includes(me.name))
    ? `这是 ${tab.owner} ${tab.visible === 'public' ? '公开的' : '分享给你的'}会话，欢迎随意阅读` : '');
  const shareBusy = ref(false), shareError = ref('');
  async function changeShare(url, body, what) {
    const tab = current.value;
    if (!tab || shareBusy.value) return;
    // 新会话标签页还没有会话：先记在标签页上，建会话时一起交给服务器（会话主人在那里改不了，只有可见范围）
    if (tab.fresh) {
      if (url === 'share') tab.visible = body.visible;
      return;
    }
    shareBusy.value = true;
    try {
      Object.assign(tab, await withTab(tab, () => api(`/api/tabs/${tab.id}/${url}`, { method: 'POST', body })));
      shareError.value = '';
    } catch (e) {
      shareError.value = e.message;
      fail(what, e);
    }
    shareBusy.value = false;
  }
  const setShare = visible => changeShare('share', { visible }, '改可见范围');
  function toggleViewer(name) {
    const v = Array.isArray(current.value?.visible) ? current.value.visible : [];
    setShare(v.includes(name) ? v.filter(n => n !== name) : [...v, name]);
  }
  const setOwner = owner => changeShare('owner', { owner: owner || null }, '改会话的主人');

  // 限量号能不能用、为什么不能（空字符串就是能用）：游客不能；已有的会话还得有主人（新会话建出来就是自己的）；
  // 花费记在自己头上时，自己的余额透支到 me.overdraft 就不能用。别人名下的会话这里不知道主人的余额，靠服务器拦。
  // 单用户实例没有游客，也没有无主会话（都算主人的）。服务器那边同样会拦，这里只是先说清楚。
  // blue：自己在这个号上还有预定的蓝，用蓝不扣钱，余额透支也照样能用（服务器 users.paid_why 同样放行）
  // 自己是这个号的号主（me.owns）也不看余额：花的钱又流回自己
  function paidBlock(a, tab, blue = false) {
    if (!a?.paid) return '';
    if (me.multi && !me.name) return '请登录后使用';
    if (me.multi && tab && !tab.fresh && !tab.owner) return '无主会话无法使用';
    const mine = !tab || tab.fresh || tab.owner === me.name;
    if (me.multi && mine && !blue && !(me.owns || []).includes(a.name) && me.balance != null && me.overdraft != null && me.balance <= me.overdraft) return `余额已降至 ${fmtPts(me.overdraft)}`;
    return '';
  }
  // 下拉里的一项：被封的写原因，限量号标出来、不能用的写为什么；限时特惠里的号写上特惠（promoTag）
  function acctLabel(a, tab) {
    if (a.banned) return `${a.name}（${a.note || '不可用'}）`;
    const why = paidBlock(a, tab), promo = promoTag(a.name);
    return (a.paid ? `${a.name}（限量号${why ? `，${why}` : ''}）` : a.name) + (promo ? ` ${promo.label}` : '');
  }

  return {
    me, loadMe, meReady, showBalance, fmtPts, auth, openAuth, submitAuth, loggingOut, logout, pw, openPassword, submitPassword,
    ledger, openLedger, openFromLedger, board, openBoard, boardUsers, admin, openAdmin, adminUsers, grantPoints, resetPassword, setNewUser,
    openUser, adminUser, planning, grantHint, revokePlan,
    traffic, openTraffic, mbText,
    userNames, namesError, loadNames, canShare, canPublicShare, visText, lockedForOthers, useBlock, shareBusy, shareError, setShare, toggleViewer, setOwner,
    paidBlock, acctLabel,
  };
}
