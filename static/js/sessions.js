// 找会话、开会话：文件选择器、全部会话看板、最近会话菜单、新建会话，以及给会话打标签
const { ref, reactive, computed, watch } = Vue;
import { api, fileName, touch, store, say, fail, who, me, persistRef, persistReactive, shortSpan } from './util.js';
import { setXxlb } from './xxlb.js';
import { setTagColors, tagColors } from './tag-color.js';
import { setRates, spentPoints } from './points.js';
import { siteNames } from './acct-tag.js';
import { withTab } from './tabs.js';

export function useSessions({ tabs, current, select, openTab, readonly, newFresh, promote, personaOf, rememberPersona, confirmPersona }) {
  // 添加会话 / 浏览文件夹
  const picker = reactive({ open: false, input: '', error: '', path: '', parent: '', entries: [], root: '' });
  // 本组账号（不能用的号带 banned 和原因）和服务器启动时定的默认账号，只读模式下都是空的
  const accounts = ref([]);
  // sharedHome：共用环境运行 Claude 的那个用户的家目录（服务器 /api/environments 给，旧后端没有）
  const environments = reactive({ enabled: false, ready: false, environment: null, sharedHome: '', accounts: [], busy: false, error: '' });
  async function loadEnvironments() {
    try { Object.assign(environments, await api('/api/environments'), { error: '' }); }
    catch (e) { environments.error = e.message; }
  }
  async function createEnvironment() {
    if (environments.busy) return;
    environments.busy = true;
    environments.error = '';
    try {
      Object.assign(environments, await api('/api/environments', { method: 'POST' }));
      say(me.newUser ? '您的工作环境已就绪' : '独立环境已创建，旧会话不变');
    } catch (e) { environments.error = e.message; }
    finally { environments.busy = false; }
  }
  // 新用户（服务器 users.is_new；旧后端不给 newUser，一切照旧）只有独立环境，界面上不提共用环境。
  // 注册时服务器就建好了；那时没建成，打开页面时这里再建一次（一次页面只自动建一回，再不成由用户点「重试」），
  // 还没建好时不让发话、不让选文件夹新建，不会改用共用环境
  const needEnvironment = () => !!me.newUser && environments.enabled && !environments.environment;
  let autoTried = false;
  watch(() => needEnvironment() && environments.ready && !environments.error && !environments.busy, go => {
    if (go && !autoTried) { autoTried = true; createEnvironment(); }
  });
  // 只能在独立环境里开会话：新用户，或者在设置里关了共用环境（feature，在下面）的人，都得已经有了独立环境
  const onlyPrivate = () => !!environments.environment && (!!me.newUser || !feature('shared'));
  const envNotReady = () => (environments.busy ? '您的工作环境正在准备，请稍候'
    : `您的工作环境尚未就绪${environments.error ? `：${environments.error}` : ''}`);
  // 这个环境的家目录（绝对路径），新会话页工作区那颗药丸照它填；取不到（旧后端、环境信息还没到）是空的，照旧用 ~
  const homeOf = kind => (kind === 'private' ? environments.environment?.home : environments.sharedHome) || '';
  function changeEnvironment(tab, kind) {
    if (tab.busy) return;
    tab.environment = kind;
    // 换环境时工作区一律回到新环境的家目录：两边的家目录互相进不去，
    // 一个环境里填的路径换过去多半打不开
    tab.cwd = homeOf(kind) || '~';
    if (kind === 'private') tab.visible = [];
  }
  // 新会话页上选环境：还没建独立环境的，选「独立」就当场创建，建好再换过去；没建成原因写在工作区那一行下面（environments.error）
  async function pickEnvironment(tab, kind) {
    if (tab.busy) return;
    if (kind === 'private' && !environments.environment) {
      tab.envCreating = true;
      try { await createEnvironment(); } finally { tab.envCreating = false; }
      if (!environments.environment) return;
    }
    changeEnvironment(tab, kind);
  }
  // 环境信息比新会话标签页晚到时，标签页先填了 ~：还没动过的换成家目录的绝对路径
  watch(() => [environments.sharedHome, environments.environment?.home], () => {
    for (const t of tabs.value) if (t.fresh && !t.busy && t.cwd === '~' && homeOf(t.environment)) t.cwd = homeOf(t.environment);
  });
  const defaultAccount = ref('');
  // 权限模式的选项和默认值：新会话标签页还没有会话，看板上的权限下拉从这里拿
  const modeList = ref([]), defaultMode = ref('');
  // 思考强度的几档（模型下拉里选；不选就按 Claude Code 自己的设置）
  const effortList = ref([]);
  // 没选过、Claude Code 设置里也没给这个模型写强度时，服务器拉起 CLI 带的那档（server.launch_effort）；旧后端不给是 null
  const defaultEffort = ref(null);
  // Claude 号没选过模型、设置里也没写默认模型时，服务器拉起 CLI 带的那个（server.launch_model）；旧后端不给是 null
  const defaultModel = ref(null);
  // 服务器的配置：浏览的起点，以及是不是只读模式启动的（只读就没有输入框、不连 Claude）。
  // 没取到时不能当成只读：说清楚，看板上挂着「点这里重试」，「只读」标签也不显示
  const configError = ref('');
  // 分享链接由后端签发随机凭证；旧后端没有这项能力时明说，不复制一个打不开的旧式链接。
  const publicShare = ref(false);
  const sidePages = ref(false);   // 后端认得 /api/side（侧边栏一页页取）；旧后端不给，照旧一次取全部
  const sideStatus = ref(false);  // 后端认得 /api/side/status（侧边栏的状态点）；旧后端不给，只有这台设备上开着的会话有点
  const sideFilterOk = ref(false);  // 后端认得侧边栏的筛选（/api/side 的 state、tag 和 /api/side/tags）
  const sidePins = ref(false);   // 后端认得置顶（/api/side 第一页带 pinned、/api/side/pin）；旧后端不给，不出置顶
  const firstMessageImages = ref(false);   // 旧后端没有这个能力标记：带图首句要明确拦下，不能只发文字
  async function shareLink(s) {
    if (!publicShare.value) throw new Error('当前服务器还没有启用分享链接；请等服务更新后重试');
    // 看板/侧边栏的卡片未必开过；旧的打开接口照常能用，签发仍只由有权的人完成。
    if (!tabs.value.some(t => t.id === s.id)) await api('/api/tabs', { method: 'POST', body: { path: s.path, focus: true } });
    const { url } = await api(`/api/tabs/${s.id}/public-share`, { method: 'POST' });
    return url;
  }
  async function revokeShare(s) {
    if (!publicShare.value) throw new Error('当前服务器还没有启用分享链接；请等服务更新后重试');
    if (!tabs.value.some(t => t.id === s.id)) await api('/api/tabs', { method: 'POST', body: { path: s.path, focus: true } });
    return api(`/api/tabs/${s.id}/public-share`, { method: 'DELETE' });
  }
  // 侧边栏的「最近」要先知道后端认不认分页（sidePages），所以取会话列表之前先等配置（rootDone，loadRootNow 自己接住错误、不会抛）
  let rootDone = null;
  const loadRoot = () => (rootDone = loadRootNow());
  const loadRootNow = async () => {
    try {
      const c = await api('/api/config');
      picker.root = c.root;
      readonly.value = c.readonly !== false;
      accounts.value = c.accounts || [];
      for (const a of accounts.value) if (a.relay && a.api) siteNames[a.name] = a.api;   // 中转站号的标签写站名（acct-tag.js）
      defaultAccount.value = c.account || '';
      modeList.value = c.modes || [];
      defaultMode.value = c.defaultMode || '';
      effortList.value = c.efforts || [];
      defaultEffort.value = c.defaultEffort || null;
      defaultModel.value = c.defaultModel || null;
      publicShare.value = !!c.publicShare;
      sidePages.value = !!c.sidePages;
      sideStatus.value = !!c.sideStatus;
      sideFilterOk.value = !!c.sideFilter;
      sidePins.value = !!c.sidePins;
      pwaCookie.value = c.pwaCookie || '';
      readPwa();
      firstMessageImages.value = c.firstMessageImages === true;
      if (c.call) who.me = c.call;
      if (c.host) who.host = c.host;
      who.pcSetupUrl = c.pcSetupUrl || '';
      setXxlb(c.xxlb === true);   // 部署者放好了看板娘的模型才画（xxlb.js）
      setRates(c.rates);   // 换算点数用的单价
      configError.value = '';
      await loadEnvironments();
    } catch (e) {
      firstMessageImages.value = false;
      configError.value = e.message;
      fail('加载服务器配置', e);
    }
  };
  // 新用户（和关了共用环境的人）在自己的独立环境里浏览、新建（pickerEnv）；别人照旧是共用环境
  const pickerEnv = () => (onlyPrivate() ? 'private' : '');
  async function browse(path) {
    if (needEnvironment()) return (picker.error = envNotReady());
    const env = pickerEnv();
    try {
      Object.assign(picker, await api(`/api/browse?path=${encodeURIComponent(path || '')}${env ? `&environment=${env}` : ''}`), { error: '' });
    } catch (e) { picker.error = e.message; }
  }
  function openPicker() {
    picker.open = true;
    browse(picker.path || picker.root);
  }
  async function addTab(path) {
    try {
      await openTab(path);
      Object.assign(picker, { open: false, input: '', error: '' });
    } catch (e) { picker.error = e.message; }
  }
  // 回车：.jsonl 就打开；留空就在当前文件夹新建会话；输入文件夹（只写名字就按当前文件夹算）就在那里新建
  // 只读模式不能新建会话，输入文件夹就是跳过去找
  async function submitPath() {
    const input = picker.input.trim();
    if (input.endsWith('.jsonl')) return addTab(input);
    const target = !input ? picker.path : /^[\/~]/.test(input) ? input : `${picker.path.replace(/\/$/, '')}/${input}`;
    if (needEnvironment()) return (picker.error = envNotReady());
    const env = pickerEnv();
    try {
      // 顺便确认文件夹存在、把 ~ 展开
      const { path } = await api(`/api/browse?path=${encodeURIComponent(target)}${env ? `&environment=${env}` : ''}`);
      if (readonly.value) { picker.input = ''; return browse(path); }
      Object.assign(picker, { open: false, input: '', error: '' });
      openNewChat(path);
    } catch (e) { picker.error = e.message; }
  }

  // 标签：一个会话可以打好几个，只记在服务器的小文件里，不碰会话记录。
  // allTags 是全部标签（打得多的在前），新建会话和编辑标签时拿来挑
  // 颜色也一起取：页面一打开就要用到（看板标题后面的标签）
  const allTags = ref([]);
  const loadTags = () => api('/api/tags').then(r => { allTags.value = r.tags; setTagColors(r.colors); },
                                              e => fail('加载标签', e));
  loadTags();
  // 给标签挑颜色，color 为 null 就回到按名字自动配色
  async function setTagColor(tag, color) {
    try {
      setTagColors((await api('/api/tags/color', { method: 'POST', body: { tag, color } })).colors);
    } catch (e) { fail(`修改「${tag}」的颜色`, e); }
  }

  // 全部会话总览：分组的卡片墙，左边一栏标签，选中哪个就只看带这个标签的会话（tag 为空是全部）。
  // 服务器给的 groups 总是按工作区分好的；换别的分组方式、按用量排都在这里重排，不用重新请求
  // deleted 是在查看器里删掉、这个人能恢复的会话（服务器只给会话所属用户和查看器账号），排在卡片墙最下面
  // testWorkspace：服务器给了才出「显示测试会话」开关（旧后端不在组上标 test，开不开都一样）
  const overview = reactive({ open: false, loading: false, error: '', groups: [], deleted: [], tag: '', recentOf: 0, testWorkspace: '' });
  // 测试会话：工作目录在测试工作区里的（服务器在组上标 test），全部会话和最近会话里默认不显示，开关打开才显示。
  // 登录了存在服务器上、各设备共用（一选就存，同默认账号），游客、单用户实例和取不到设置时记在这个浏览器里
  // 开关用 v-model 绑它：存失败时值没变，正在存那一下的重画会把勾选框拨回去，不会显示成已经打开
  const localTests = persistRef('showTests', false);
  const showTests = computed({ get: () => globalSettings.showTests ?? localTests.value, set: on => setShowTests(on) });
  const testsBusy = ref(false);
  async function setShowTests(on) {
    if (typeof globalSettings.showTests !== 'boolean') { localTests.value = on; return; }
    if (testsBusy.value) return;
    testsBusy.value = true;
    try {
      globalSettings.showTests = (await api('/api/settings/show-tests', { method: 'POST', body: { on } })).showTests;
    } catch (e) { fail(on ? '开启「显示测试会话」' : '关闭「显示测试会话」', e); }
    testsBusy.value = false;
  }
  const testFree = computed(() => (showTests.value ? overview.groups : overview.groups.filter(g => !g.test)));
  // 侧边栏的「最近」只列自己的会话（见 recentShown）：别人的（公开的、无主的，查看器账号还有别人的私有会话）
  // 只收在全部会话那一页最下面默认折叠的「其他人的会话」里。只改显示，看得到哪些照旧由服务器定。
  // 全部会话那一页（从账号页的「管理会话」进）：查看器账号看到所有人的会话（查看器账号在那里就是要管所有人的），
  // 别人照旧只看自己的；新用户（me.newUser）是查看器账号也只看自己的
  const ovOwn = computed(() => !!me.newUser || !me.admin);
  // 是不是自己的会话：单用户实例里服务器不发所属用户（owner 一律是 null），会话都是这一个人的
  const isMine = owner => !me.multi || owner === me.name;
  const mineOnly = (groups, mine) => groups.map(g => ({ ...g, sessions: g.sessions.filter(s => isMine(s.owner) === mine) }))
    .filter(g => g.sessions.length);
  const visibleGroups = computed(() => (ovOwn.value ? mineOnly(testFree.value, true) : testFree.value));
  const othersOpen = ref(false);
  const othersGroup = computed(() => {
    if (!ovOwn.value) return { key: 'others', sessions: [] };
    const all = mineOnly(testFree.value, false).flatMap(g => g.sessions.map(s => ({ ...s, ws: g.name })));
    return { key: 'others', sessions: (overview.tag ? all.filter(s => s.tags?.includes(overview.tag)) : all).sort((a, b) => b.mtime - a.mtime) };
  });
  // 看法存在浏览器里（和主题、显示开关一样是本地偏好）：group 按目录 dir / 按所属用户 owner / 不分组 none；
  // sort 是最近更新 mtime，或最近 mins 分钟的用量 recent（谁都能选，只算自己看得到的会话）。
  // 用户管理、余额看板里的「最近多少分钟」也用这个 mins，几处填一样的数才对得上
  // adminSort 是用户管理的排序：注册先后 join，或最近用量 recent；boardSort 是余额看板的：余额 balance、join、recent。
  // 一开始全部会话不分组，三处都按最近 300 分钟的用量排。v 是这套默认值的版本：
  // 以前存下的看法是旧默认值时期的，整份作废一次，让每个人都从新的初始状态开始
  const ovView = persistReactive('overviewView',
    { v: 3, group: 'none', sort: 'recent', mins: 300, adminSort: 'recent', boardSort: 'recent' },
    saved => (saved.v === 3 ? saved : {}));
  // 实际生效的：按所属用户分组只在多用户实例里有（单用户实例的会话都是同一个人的）
  const ovGroup = computed(() => (ovView.group === 'owner' && !me.multi ? 'dir' : ovView.group));
  const byRecent = computed(() => ovView.sort === 'recent');
  const okMins = () => Number.isFinite(ovView.mins) && ovView.mins > 0 && ovView.mins <= 43200;
  const openedIds = computed(() => new Set(tabs.value.map(t => t.id)));
  async function openOverview() {
    Object.assign(overview, { open: true, loading: true, error: '' });
    const recent = byRecent.value;
    if (recent && !okMins()) {
      Object.assign(overview, { loading: false, error: '分钟数须大于 0 且不超过 43200' });
      return;
    }
    try {
      const { groups, deleted, testWorkspace } = await api(recent ? `/api/overview?mins=${ovView.mins}` : '/api/overview');
      overview.groups = groups;
      overview.deleted = deleted || [];
      overview.testWorkspace = testWorkspace || '';
      // 记下这批卡片上的用量是最近几分钟的，标题上照它写；没带用量就是 0。
      // 旧版服务器不认 mins、卡片上不带 recent：那是取不到，不能写成「没有用量」
      const stale = recent && groups.some(g => g.sessions.some(s => !s.recent));
      overview.recentOf = recent && !stale ? ovView.mins : 0;
      if (stale) overview.error = '服务器版本过旧，暂不支持最近用量，已按最近更新排序';
    } catch (e) { overview.error = e.message; }
    overview.loading = false;
  }
  // 卡片上这段时间的用量合计成点数（免费加限量），没用过、或者模型没有单价就是 0
  const recentPts = s => spentPoints(s.recent?.spent) || 0;
  // 按模型的 token 数（{模型: {in, cw, cr, out, n}}）加进 into
  function addModels(into, add) {
    for (const [model, u] of Object.entries(add || {})) {
      const t = into[model] ||= {};
      for (const [k, v] of Object.entries(u)) t[k] = (t[k] || 0) + v;
    }
  }
  // 左栏：看板里出现的标签和各自的会话数，多的在前（藏起来的测试会话不算）
  const tagCounts = computed(() => {
    const n = {};
    for (const g of visibleGroups.value) for (const s of g.sessions) for (const t of s.tags || []) n[t] = (n[t] || 0) + 1;
    return Object.entries(n).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh'))
      .map(([name, count]) => ({ name, count }));
  });
  // 按选的分组方式重新分组、排序，再按标签筛，筛空了的组整组不显示。
  // 按用量排时组内用量多的在前，这段时间没用过的排在后面、按最近更新；组和组之间按组内合计，一样（都是 0）再按最近更新
  const shownGroups = computed(() => {
    const all = visibleGroups.value.flatMap(g => g.sessions.map(s => ({ ...s, ws: g.name })));
    const kept = overview.tag ? all.filter(s => s.tags?.includes(overview.tag)) : all;
    const parts = new Map();
    for (const s of kept) {
      const key = ovGroup.value === 'dir' ? s.ws : ovGroup.value === 'owner' ? (s.owner || '') : '';
      if (!parts.has(key)) parts.set(key, []);
      parts.get(key).push(s);
    }
    const recent = overview.recentOf > 0;
    const byTime = (a, b) => b.mtime - a.mtime;
    const groups = [...parts].map(([key, sessions]) => {
      sessions.sort(recent ? (a, b) => recentPts(b) - recentPts(a) || byTime(a, b) : byTime);
      // 组标题上的合计：各会话按模型的 token 数加起来，免费、限量、预定付掉的照样分颜色写
      const use = { spent: {}, paid: {}, blue: {} };
      if (recent) {
        for (const s of sessions) {
          addModels(use.spent, s.recent?.spent); addModels(use.paid, s.recent?.spentPaid); addModels(use.blue, s.recent?.spentBlue);
        }
      }
      return { key: `${ovGroup.value}:${key}`, name: ovGroup.value === 'dir' ? key : ovGroup.value === 'owner' ? (key || '无主') : '全部会话',
               dir: ovGroup.value === 'dir', sessions, newest: Math.max(...sessions.map(s => s.mtime)),
               use, recent: recent ? spentPoints(use.spent) || 0 : 0 };
    });
    return groups.sort((a, b) => b.recent - a.recent || b.newest - a.newest);
  });
  // 电脑上悬停就筛，移开不取消（不然挪去点卡片的路上筛选就没了）；手机上点一下
  const hoverTag = t => { if (!touch) overview.tag = t; };

  // 会话多的工作区默认只露前三行卡片，其余收起来。一行几张按卡片墙的宽度算（和样式里
  // 「每张至少 200px、间距 8px、自动排满」一致；窄屏固定两列，和样式里 760px 那一档一致），
  // 展开过的工作区在页面开着时记住
  const FOLD_ROWS = 3;
  const perRow = ref(4);
  const openGroups = reactive(new Set());
  const narrow = matchMedia('(max-width: 760px)');
  const groupsWatcher = new ResizeObserver(([e]) => {
    perRow.value = narrow.matches ? 2 : Math.max(1, Math.floor((e.target.clientWidth - 4 + 8) / (200 + 8)));
  });
  function groupsRef(el) {
    groupsWatcher.disconnect();
    if (el) groupsWatcher.observe(el);
  }
  // 手机上卡片是两列，三行就是六张
  const foldAt = computed(() => perRow.value * FOLD_ROWS);
  const cardsOf = g => (openGroups.has(g.key) ? g.sessions : g.sessions.slice(0, foldAt.value));
  const toggleGroup = name => (openGroups.has(name) ? openGroups.delete(name) : openGroups.add(name));
  // 删掉的会话（见服务器 users.py「删掉的会话」）：照样按标签筛，和工作区一样只露前三行
  const trashGroup = computed(() => ({ key: 'trash',
    sessions: overview.tag ? overview.deleted.filter(s => s.tags?.includes(overview.tag)) : overview.deleted }));
  // 刚删掉一个会话：卡片墙和最近会话里先拿掉，最近会话下次悬停重新取
  function forget(id) {
    for (const g of overview.groups) g.sessions = g.sessions.filter(s => s.id !== id);
    overview.groups = overview.groups.filter(g => g.sessions.length);
    recentAll.value = recentAll.value.filter(s => s.id !== id);
    pinnedAll.value = pinnedAll.value.filter(s => s.id !== id);
    rowMap.delete(id);
    recentAt = 0;
  }
  // 恢复：卡片上的按钮写「正在恢复…」防连点；好了整个卡片墙重新取一遍，它回到原来的工作区里
  async function restore(s) {
    if (s.restoring) return;
    s.restoring = true;
    const name = s.title || fileName(s.path);
    try {
      await api(`/api/sessions/${s.id}/deleted`, { method: 'POST', body: { deleted: false } });
      say(`已恢复「${name}」`);
      recentAt = 0;
      await openOverview();
    } catch (e) { fail(`恢复「${name}」`, e); }
    s.restoring = false;
  }
  // 选了卡片就关弹窗：一次只看一个会话
  async function pickCard(s) {
    try {
      await openTab(s.path);
      overview.error = '';
      overview.open = false;
    } catch (e) { overview.error = e.message; }
  }

  // 没有悬停的设备上，菜单改成点一下展开；点别处收起
  const openDd = ref('');
  function toggleDd(name) {
    openDd.value = openDd.value === name ? '' : name;
  }
  window.addEventListener('click', e => { if (!e.target.closest('.dd')) openDd.value = ''; });

  // 侧边栏的「最近」：打开侧边栏时取；5 秒内重复打开不重新拉
  const recentAll = ref([]);
  // 置顶的会话：服务器从分页里拿出来、第一页一次全给（/api/side 的 pinned），排在「最近」最上面，不进 recentAll
  const pinnedAll = ref([]);
  // recentState：'' 还没取过，loading 正在取，ok 取到了，否则是出错的原因（面板里写出来，下次悬停再试）
  const recentState = ref('');
  let recentAt = 0;
  // 侧边栏一页页取、看到哪儿画到哪儿（一次取回名下八百多个会话、全画出来的话，
  // 开侧边栏、换会话都要重画几百行，很卡）。recentKind 是 recentAll 里装的是哪种：side 侧边栏分页取的（带着测试会话开没开），
  // all 一次取全部的（不认分页的旧后端）；换了种类整份重来。
  // 行按会话编号留着同一个对象（rowMap），重新取到只改变了的字段，没变的行不用重画
  const SIDE_PAGE = 50;
  const recentKind = ref('');
  const rowMap = new Map();
  const sideShown = ref(SIDE_PAGE);   // 侧边栏画到第几行，每次打开侧边栏从头算
  const sideMore = ref(false);   // 服务器那边还有更早的没取
  const sideMoreState = ref('');   // 取更早的那一页：'' 闲着，loading 正在取，否则是出错的原因
  let sideGen = 0;   // 换了种类就加一，还在路上的翻页回来对不上就扔掉
  function patchRows(cards) {
    return cards.map(c => {
      const row = rowMap.get(c.id);
      if (!row) {
        const r = reactive(c);
        rowMap.set(c.id, r);
        return r;
      }
      for (const [k, v] of Object.entries(c)) if (JSON.stringify(row[k]) !== JSON.stringify(v)) row[k] = v;
      return row;
    });
  }
  const sideCard = c => ({ ...c, ws: fileName(c.ws) || c.ws });
  // 第二级缓存：第一页存在这台设备上（各实例端口不同，本来就分开；再按人、按开没开测试会话分开），
  // 页面一打开先画出上次的，取到新的马上换上；没取到新的，列表顶上写明下面是上次的。退出登录时清掉（users.js 的 logout）
  const sideKey = kind => `sideRows:${me.name || ''}:${kind}`;
  // 「最近」右边漏斗（state：unread 有新消息、attention 需要处理、run 进行中，空是全部）和标签（tag，空是全部）两个筛选，
  // 记在这台设备上。服务器那边筛好再分页（/api/side 的 state、tag）；前端在取到的里面照样再筛一遍（recentShown）：
  // 状态一变（进行中的干完了）这一行马上按新的点走，旧后端不认这两个参数时也只是少了没取到的那几页
  const sideFilter = persistReactive('sideFilter', { state: '', tag: '' });
  const SIDE_STATES = { unread: ['unread'], attention: ['limit', 'error'], run: ['run'] };
  const SIDE_STATE_NAMES = { unread: '有新消息', attention: '需要处理', run: '进行中' };
  const sideFilterLabel = computed(() => [SIDE_STATE_NAMES[sideFilter.state], sideFilter.tag].filter(Boolean).join(' · ') || '最近');
  // 种类里带着筛选：换了筛选整份重来，存在设备上的第一页也按筛选分开
  const sideKind = () => `side${showTests.value ? 1 : 0}:${sideFilter.state}:${sideFilter.tag}`;
  function prefillSide() {
    if (recentAll.value.length) return;
    const kind = sideKind();
    const rows = store.get(sideKey(kind), null), pinned = store.get(`${sideKey(kind)}:pin`, []);
    if (!Array.isArray(rows) || !rows.length) return;
    recentKind.value = kind;
    rowMap.clear();
    recentAll.value = patchRows(rows);
    pinnedAll.value = Array.isArray(pinned) ? patchRows(pinned) : [];
  }
  const sideUrl = more => `/api/side?limit=${SIDE_PAGE}&tests=${showTests.value ? 1 : 0}`
    + `${sideFilter.state ? `&state=${sideFilter.state}` : ''}${sideFilter.tag ? `&tag=${encodeURIComponent(sideFilter.tag)}` : ''}${more}`;
  // 排在 last 后面的（更早更新的；一样时按路径，同服务器）
  const olderThan = (s, last) => s.mtime < last.mtime || (s.mtime === last.mtime && s.path > last.path);
  async function loadRecent() {
    await rootDone;
    const kind = sidePages.value ? sideKind() : 'all';
    if (kind === recentKind.value && Date.now() - recentAt < 5000) return;
    recentAt = Date.now();
    if (kind !== recentKind.value) {
      recentKind.value = kind;
      rowMap.clear();
      recentAll.value = [];
      pinnedAll.value = [];
      sideMore.value = false;
      sideMoreState.value = '';
      sideGen++;
      recentState.value = 'loading';
    } else if (recentState.value !== 'ok') recentState.value = 'loading';
    const gen = sideGen;
    try {
      if (kind !== 'all') {
        // 只重新取第一页，和手里已经取到的接起来：后面那些没更新过（更新过的会挪进第一页），原样留着；
        // 第一页最后一行之前、却不在第一页里的，是删掉了或者不归自己了，扔掉
        const r = await api(sideUrl(''));
        if (gen !== sideGen) return;
        // 置顶的先换上，手里后面那些里有它们（别的设备上刚钉住的）就拿掉；反过来刚取消的会出现在第一页或者以后的页里
        const pinned = patchRows((r.pinned || []).map(sideCard)), pinIds = new Set(pinned.map(s => s.id));
        const top = patchRows(r.items.map(sideCard));
        if (!r.more) {
          recentAll.value = top;
          sideMore.value = false;
        } else {
          const ids = new Set(top.map(s => s.id)), last = top[top.length - 1];
          const rest = recentAll.value.filter(s => !ids.has(s.id) && !pinIds.has(s.id) && olderThan(s, last));
          for (const s of recentAll.value) if (!ids.has(s.id) && !pinIds.has(s.id) && !olderThan(s, last)) rowMap.delete(s.id);
          recentAll.value = top.concat(rest);
          if (!rest.length) sideMore.value = true;
        }
        for (const s of pinnedAll.value) if (!pinIds.has(s.id) && !recentAll.value.includes(s)) rowMap.delete(s.id);
        pinnedAll.value = pinned;
        store.set(sideKey(kind), recentAll.value.slice(0, SIDE_PAGE));
        store.set(`${sideKey(kind)}:pin`, pinned);
      } else {
        // 走到这里是不认分页的旧后端：mine=1 让它只算自己名下的，不认就照旧全给，下面照样筛
        const { groups } = await api('/api/overview?mine=1');
        if (gen !== sideGen) return;
        recentAll.value = patchRows(groups.flatMap(g => g.sessions.map(s => ({ ...s, ws: fileName(g.name) || g.name, test: !!g.test })))
          .sort((a, b) => b.mtime - a.mtime));
      }
      recentState.value = 'ok';
    } catch (e) {
      if (gen !== sideGen) return;
      recentAt = 0;
      recentState.value = `加载失败：${e.message}`;
    }
  }
  // 侧边栏滚到底：手里还有没画的就多画一页，都画完了、服务器那边还有，就取下一页。接上了返回 true
  async function moreSide() {
    if (sideMoreState.value === 'loading') return false;
    if (recentShown.value.length > sideShown.value) {
      sideShown.value += SIDE_PAGE;
      return true;
    }
    const last = recentAll.value[recentAll.value.length - 1];
    if (recentKind.value === 'all' || !sideMore.value || !last) return false;
    sideMoreState.value = 'loading';
    const gen = sideGen;
    try {
      const r = await api(sideUrl(`&before=${encodeURIComponent(`${last.mtime}:${last.path}`)}`));
      if (gen !== sideGen) return false;
      const have = new Set(recentAll.value.map(s => s.id));
      recentAll.value = recentAll.value.concat(patchRows(r.items.map(sideCard).filter(c => !have.has(c.id))));
      sideMore.value = r.more;
      sideShown.value += SIDE_PAGE;
      sideMoreState.value = '';
      return true;
    } catch (e) {
      if (gen === sideGen) sideMoreState.value = `更早的会话加载失败：${e.message}`;
      return false;
    } finally {
      if (gen !== sideGen) sideMoreState.value = '';
    }
  }
  // 列表底下垫一行（sideMoreRef），滚到离它 300px 就接下一页。接上以后它要是还在眼前（列表短），观察者不会再报一次，
  // 所以重新挂一遍让它再看一眼；没接上（出错、到底了）不重挂，免得出错时一直重试，出错由那一行写出来、点它重试
  let moreEl = null;
  const moreIo = new IntersectionObserver(es => {
    if (es.some(e => e.isIntersecting)) moreSide().then(ok => ok && Vue.nextTick(rewatch));
  }, { rootMargin: '300px 0px' });
  function rewatch() {
    if (!moreEl) return;
    moreIo.unobserve(moreEl);
    moreIo.observe(moreEl);
  }
  const sideMoreRef = el => {
    if (el === moreEl) return;
    if (moreEl) moreIo.unobserve(moreEl);
    moreEl = el;
    if (el) moreIo.observe(el);
  };
  const sideHasMore = computed(() => recentShown.value.length > sideShown.value || (recentKind.value !== 'all' && sideMore.value));
  function retryMore() {
    if (sideMoreState.value === 'loading') return;
    sideMoreState.value = '';
    moreSide().then(ok => ok && Vue.nextTick(rewatch));
  }
  // 测试会话照全部会话那个开关藏起来，只列自己的，再按右边漏斗和标签筛，下面几样都从这份拿
  // 置顶的排在最前面，筛法一样
  const recentShown = computed(() => pinnedAll.value.concat(recentAll.value).filter(s => (showTests.value || !s.test) && isMine(s.owner)
    && (!sideFilter.tag || s.tags?.includes(sideFilter.tag))
    && (!sideFilter.state || SIDE_STATES[sideFilter.state].includes(sideDot(s)))));
  // 置顶、取消置顶（手机上往右划一行，电脑上会话菜单里）：当场挪过去，再告诉服务器；没存上就挪回去、说清楚。
  // 置顶的照最近更新排在最上面；取消的插回「最近」里它该在的位置，比手里最后一行还早、后面还有没取的，就等翻到那一页再出现
  const byNewest = (a, b) => b.mtime - a.mtime || (a.path < b.path ? -1 : 1);
  function placePin(s, on) {
    s.pinned = on;
    if (on) {
      recentAll.value = recentAll.value.filter(x => x !== s);
      pinnedAll.value = [...pinnedAll.value.filter(x => x !== s), s].sort(byNewest);
    } else {
      pinnedAll.value = pinnedAll.value.filter(x => x !== s);
      const last = recentAll.value[recentAll.value.length - 1];
      if (!sideMore.value || !last || !olderThan(s, last)) recentAll.value = [...recentAll.value, s].sort(byNewest);
    }
  }
  // 挪的时候各行从原来的位置滑到新位置：挪之前量一遍各行，挪完再量，差多少就从那里动画回原位（FLIP），
  // 节奏同侧边栏别处那条带回弹的曲线；新露出来的行淡入。取消置顶的要去的位置还没取到（会从列表里消失）就先原地淡出，再让别的行补上来
  const FLIP_MS = 300, FLIP_EASE = 'cubic-bezier(0.3, 1.18, 0.5, 1)';
  const sideRowEls = () => [...document.querySelectorAll('.side .side-sess[data-id]')];
  async function movePin(s, on) {
    const calm = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const last = recentAll.value[recentAll.value.length - 1];
    const gone = !on && sideMore.value && last && olderThan(s, last);
    const self = sideRowEls().find(el => el.dataset.id === s.id);
    if (gone && self && !calm) await self.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 160, fill: 'forwards' }).finished.catch(() => {});
    const before = new Map(sideRowEls().map(el => [el.dataset.id, el.getBoundingClientRect().top]));
    placePin(s, on);
    await Vue.nextTick();
    if (calm) return;
    for (const el of sideRowEls()) {
      const y0 = before.get(el.dataset.id);
      if (y0 == null) {
        el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: FLIP_MS, easing: 'ease-out' });
        continue;
      }
      const dy = y0 - el.getBoundingClientRect().top;
      if (Math.abs(dy) >= 1) el.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], { duration: FLIP_MS, easing: FLIP_EASE });
    }
  }
  async function togglePin(s) {
    const on = !s.pinned, name = s.title || fileName(s.path);
    await movePin(s, on);
    try {
      await api('/api/side/pin', { method: 'POST', body: { id: s.id, pinned: on } });
      store.set(`${sideKey(recentKind.value)}:pin`, pinnedAll.value);
    } catch (e) {
      movePin(s, !on);
      fail(on ? `置顶「${name}」` : `取消置顶「${name}」`, e);
    }
  }
  // 「最近」列出自己的全部会话（按最近更新排，列表自己滚），但只画到 sideShown（滚到底再接，见 moreSide）
  const recent = computed(() => recentShown.value.slice(0, sideShown.value));
  // 「最近」最上面再列出还不在列表里的自己的标签页：新会话标签页（正在创建的写「正在创建…」），
  // 和刚建成（bornAt，tabs.js 的 promote）、列表还没重新取到的会话；重新取到以后它就在下面的列表里了，这里自然不再列。
  // 列表是一页页取的，没取到的老会话不算。开着垂直标签页时它们都在上面的标签页里，这里不再重复列
  const recentTabs = computed(() => {
    if (sideTabs.value) return [];
    const ids = new Set(recentAll.value.concat(pinnedAll.value).map(s => s.id));
    return tabs.value.filter(t => t.fresh || (t.bornAt && isMine(t.owner) && !ids.has(t.id))).reverse();
  });
  // 侧边栏每行最左边的状态点：run 正在干活（绿点一闪一闪，同标签栏的 .dot.run），
  // limit 停在额度上（橙），error 上一轮出错、会话文件不在了（红，标签栏的 .dot.gone 也是红），
  // unread 干完了、还有没看过的内容（不闪的绿点，服务器按 turn_done 和 seen_at 算）；闲着不画、照样占着位置，名字对齐。
  // 这台设备上开着的会话看标签页自己的状态（推送一到就变），没看过的内容看服务器的，或者标签页自己数的未读（底部标签页开着时）；
  // 正在看的那个永远不算未读。没开着的全看服务器给的（sideStates，判断同服务器 side_state）
  const sidOf = path => path.split('/').pop().replace(/\.jsonl$/, '');
  function sideDot(s) {
    const t = tabs.value.find(x => x.id === s.id);
    if (t?.fresh) return t.busy || t.running ? 'run' : 'idle';
    if (t) {
      const st = t.running ? 'run' : t.missing ? 'error' : t.autoGo?.stop ? 'limit' : t.chatError ? 'error' : '';
      if (st) return st;
      return t.id !== current.value?.id && (t.unread || sideStates.value[sidOf(t.path)] === 'unread') ? 'unread' : 'idle';
    }
    return sideStates.value[sidOf(s.path)] || 'idle';
  }
  const sideStates = ref({});
  const sideStatesErr = ref('');   // 没取到：写在列表顶上，不能把取不到画成闲着
  let polling = false;
  const openRecent = s => openTab(s.path).catch(e => fail('打开会话', e));

  // 全局设置：存在服务器上、所有设备共用（设置面板里「全局设置」那组），别的设备改了会推过来。
  // default 是按设置此刻会用哪个号、为什么（auto 时随额度变，所以开新会话前都重新取一次）。
  // 多用户实例里登录的人还能设自己的默认账号（mine）；全局那份只有查看器账号能改（canGlobal），给游客和没设自己的人用
  // default.account：新会话此刻会用的号（auto 时是服务器挑好的，挑不出来是空）；private：独立环境的新会话会用的号。
  // default.why 是服务器为什么这么挑，只给开发者看，界面上不写
  // fresh：上次在新会话标签页上发话时选的运行环境、工作区、标签（登录了才有，见 openFresh）
  // showTests：「显示测试会话」存在服务器上的那份，登录了才是 true / false，否则是 null（用浏览器里记的）
  // autoSwitch：当前的号被预测拦截拦下时自动切号（users.auto_switch），登录了才是 true / false，游客和旧后端是 null（不出这个开关）
  // features：新会话页上人设卡（persona）、共用环境（shared）、标签（tags）开没开，服务器给了才有，否则是 null（全都显示）
  // voice / voiceServices：讯飞语音输入的配置和能选的识别服务（voice.js、服务器 voice.py）
  const globalSettings = reactive({ settings: {}, mine: null, canGlobal: false, default: { account: '', why: '' }, private: '',
                                    fresh: null, showTests: null, features: null, autoSwitch: null, bottomTabs: null,
                                    voice: null, voiceServices: [], fallback: '', busy: false, error: '' });
  function applySettings(r) {
    Object.assign(globalSettings, { settings: r.settings || {}, mine: r.mine || null,
                                    canGlobal: !!r.canGlobal, default: r.default || {},
                                    private: r.private || '', fresh: r.fresh || null,
                                    showTests: typeof r.showTests === 'boolean' ? r.showTests : null,
                                    features: r.features || null,
                                    autoSwitch: typeof r.autoSwitch === 'boolean' ? r.autoSwitch : null,
                                    bottomTabs: typeof r.bottomTabs === 'boolean' ? r.bottomTabs : null,
                                    // voice：讯飞语音输入的配置（voice.js），{} 是没配、null 是游客或旧后端（不出这项设置和语音按钮）
                                    voice: r.voice && typeof r.voice === 'object' ? r.voice : null, voiceServices: r.voiceServices || [],
                                    fallback: r.fallback || '' });
  }
  async function loadSettings() {
    if (readonly.value) return;
    try {
      applySettings(await api('/api/settings'));
      globalSettings.error = '';
    } catch (e) {
      globalSettings.error = e.message;
      fail('加载全局设置', e);
    }
  }

  // 新会话页只放用户开了的几项：人设卡、共用环境、标签各一个开关，跟着账号存（users.features），
  // 没切过的人设卡、共用环境按用没用过定（二次元另外累计花掉超过 5 毛也开），标签一律关着。persona 那个键在设置里叫「二次元风格UI」，
  // 同时管新会话页的颜文字；它只管外观，关着时人设卡照样用（最近选的那张，人设卡页上改）。别的关着的那项在新会话页上不出现，也不偷偷生效：
  // 标签关着就不打，共用环境关着就只在自己的独立环境里开会话。游客、旧后端（不给这个键）照旧全有
  const feature = k => globalSettings.features?.[k] !== false;
  const featureBusy = ref('');
  async function setFeature(key, on) {
    if (featureBusy.value) return;
    featureBusy.value = key;
    try {
      globalSettings.features = (await api('/api/settings/feature', { method: 'POST', body: { key, on } })).features;
    } catch (e) {
      fail('保存设置', e);
      globalSettings.features = { ...globalSettings.features };   // 开关弹回存着的样子（勾选框自己已经变了）
    }
    featureBusy.value = '';
  }
  // 自动切号的开关，一选就存
  const switchBusy = ref(false);
  async function setAutoSwitch(el) {
    const on = el.checked;
    if (switchBusy.value) return void (el.checked = !on);
    switchBusy.value = true;
    try {
      globalSettings.autoSwitch = (await api('/api/settings/auto-switch', { method: 'POST', body: { on } })).autoSwitch;
    } catch (e) {
      fail(on ? '开启自动切换账号' : '关闭自动切换账号', e);
      el.checked = !on;   // 没存上：勾选框弹回存着的样子（存着的值没变，Vue 不会替它重画）
    }
    switchBusy.value = false;
  }
  // 底部标签页（users.bottom_tabs，默认关）：开着时也有页面底下的标签页栏，换会话不收掉旧的；
  // 控制区那一排照旧浮在输入框下面，盖住标签页栏也不管（统一放下面）。游客和旧后端（null）没有
  // 设置取到之前用身份里给的（/api/me 的 bottomTabs）：恢复标签页时就要知道留几个（tabs.js 的 load）
  const keepTabs = computed(() => (globalSettings.bottomTabs ?? me.bottomTabs) === true);
  const tabsBusy = ref(false);
  async function setBottomTabs(el) {
    const on = el.checked;
    if (tabsBusy.value) return void (el.checked = !on);
    tabsBusy.value = true;
    try {
      globalSettings.bottomTabs = (await api('/api/settings/bottom-tabs', { method: 'POST', body: { on } })).bottomTabs;
    } catch (e) {
      fail(on ? '开启底部标签页' : '关闭底部标签页', e);
      el.checked = !on;
    }
    tabsBusy.value = false;
  }
  // 垂直标签页（设备级，默认关）：开着底部标签页时才能开，标签页不再排在页面底下，
  // 改成竖着列在侧边栏「最近」上面（index.html 的 .side-tabs）。底部标签页关了它就不算数，开关也变灰，存着的选择不动
  const vertTabs = persistRef('vertTabs', false);
  const sideTabs = computed(() => keepTabs.value && !!vertTabs.value);
  // 桌面应用全屏打开（设备级）：记在这台设备浏览器的 cookie 里（名字服务器给，两个实例分开），
  // 浏览器取桌面应用清单（/manifest.json）时带上它，服务器按它给。装好的应用什么时候重新取清单由浏览器定，所以写好以后说明怎么让它换过来
  const pwaCookie = ref('');   // 后端认得设备级的这个开关时给 cookie 名；旧后端不给，不出这个开关
  const pwaFull = ref(false);
  const readPwa = () => (pwaFull.value = !!pwaCookie.value && document.cookie.split('; ').includes(`${pwaCookie.value}=1`));
  function setPwaFull(el) {
    const on = el.checked;
    document.cookie = `${pwaCookie.value}=${on ? 1 : ''}; path=/; max-age=${on ? 10 * 365 * 86400 : 0}; samesite=lax`;
    readPwa();
    if (pwaFull.value !== on) {
      el.checked = pwaFull.value;
      return fail(on ? '开启桌面应用全屏' : '关闭桌面应用全屏', '浏览器未能保存此设置（可能已禁用 Cookie）');
    }
    say(`已${on ? '开启' : '关闭'}，重新打开桌面应用后生效；如未生效，请卸载后重新安装`, { ms: 6000 });
  }
  // 新会话页交给服务器的人设卡：二次元只管外观，关着时下拉不显示，照样用自己最近选的那张（人设卡页上的默认）
  const freshPersona = tab => personaOf(tab);
  // 变成只能用独立环境了（页面刚打开时环境、设置晚取到，刚建好环境，或者刚关了共用环境）：还没发话、没动过的新会话标签页换过去。
  // 要等 feature 声明了才能挂（这个监听一挂上就会算一遍 onlyPrivate）
  watch(onlyPrivate, ok => {
    if (ok) for (const t of tabs.value) if (t.fresh && !t.busy && t.environment !== 'private') changeEnvironment(t, 'private');
  });
  // 侧边栏：最近的会话、新会话、搜索，齿轮进账号页和设置页（sideView）。两种用法按窗口宽窄分：
  // 宽屏（sideWide）常驻在左边、把页面挤到右边，挑会话、开新会话、搜索都不收，只有点两条横线才收，
  // 开没开着记在这台设备上（sideDocked），下次打开页面照旧；窄屏（手机、拉窄的窗口）是盖在页面上的抽屉，用完就收、按 Esc 也收。
  // 每次打开都从会话那一页看起。
  // 常驻不许挤正文（和 ChatGPT 这些网页版一样，全屏或比较宽的窗口才常驻）：常驻以后正文那栏还得有最宽的 920px
  // （style.css 的 .log 两边是 max(16px, 50% - 460px)）和两边各 16px 的留白。侧边栏 264（style.css 的 --side-w）
  // + 920 + 32，再给 Windows 的滚动条留 17，就是 1234px
  const wideMq = matchMedia('(min-width: 1234px)');
  const sideWide = ref(wideMq.matches);
  const dockOpen = persistRef('sideDocked', true);
  const sideOpen = ref(sideWide.value && !!dockOpen.value);
  const sideView = ref('main');   // main 会话那一页，me 账号页，set 设置页
  wideMq.addEventListener('change', e => {
    sideWide.value = e.matches;
    sideOpen.value = e.matches && !!dockOpen.value;   // 拉宽了按上次常驻的样子，拉窄了先收起来，不盖住页面
  });
  watch(sideOpen, v => {
    if (sideWide.value) dockOpen.value = v;
    if (v) {
      sideShown.value = SIDE_PAGE;   // 打开时从最上面看起，只画第一页，取到过的留着，滚下去马上接上
      loadRecent();
    }
  });
  // 侧边栏开着时开关了测试会话、换了筛选：要的那份变了，马上重新取
  watch(() => [showTests.value, sideFilter.state, sideFilter.tag], () => sideOpen.value && loadRecent());
  // 标签图标点开的单子：服务器列侧边栏列得出来的会话上的全部标签和个数（/api/side/tags）；旧后端没有，就数手里取到的那些
  async function loadSideTags() {
    if (!sideFilterOk.value) {
      const n = {};
      for (const s of recentAll.value) for (const t of s.tags || []) n[t] = (n[t] || 0) + 1;
      return Object.entries(n).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh')).map(([name, count]) => ({ name, count }));
    }
    return (await api(`/api/side/tags?tests=${showTests.value ? 1 : 0}`)).tags;
  }
  // 状态点：侧边栏开着、页面在前台时每 3 秒问一次服务器（只看内存，很轻）；打开侧边栏、切回页面时马上问。
  // 推送连接跟着开着的标签页走，一个都没有时不连，所以这里不靠推送
  async function pollStates() {
    if (polling || !sideStatus.value || !sideOpen.value || document.hidden) return;
    polling = true;
    try {
      const { states } = await api('/api/side/status');
      if (JSON.stringify(states) !== JSON.stringify(sideStates.value)) sideStates.value = states;
      sideStatesErr.value = '';
    } catch (e) {
      sideStatesErr.value = `会话状态加载失败：${e.message}`;
    }
    polling = false;
  }
  setInterval(pollStates, 3000);
  // 配置、侧边栏开着这两样哪个最后到位都马上问一次（宽屏上侧边栏一打开页面就开着，sideOpen 自己不会变）
  watch(() => sideStatus.value && sideOpen.value, v => v && pollStates());
  // 看过了（侧边栏的未读点，服务器 side_seen）：正看着哪个会话、页面在前台时报一次——换到它、切回页面、它这一轮干完的时候。
  // 在哪台设备上看过都算。手机在后台放久了，服务器会把会话放掉，切回来时这一报会先撞上 404，所以套 withTab 重开再报。
  // 这是后台自己报的，用户什么都没做，报不上只写控制台不上界面：最多那颗点多亮一会儿，下次换到它、它干完时还会再报
  async function markSeen() {
    const t = current.value;
    if (!sideStatus.value || !t || t.fresh || document.hidden) return;
    try {
      await withTab(t, () => api('/api/side/seen', { method: 'POST', body: { id: t.id } }));   // 编号要现取，重开后可能变了
    } catch (e) {
      console.warn(`未能将「${t.title || fileName(t.path)}」标记为已读：${e.message}`);
    }
  }
  watch(() => [sideStatus.value, current.value?.id], markSeen);
  watch(() => current.value?.running, (now, was) => was && !now && markSeen());
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    markSeen();
    pollStates();
  });
  // 两条横线：宽屏上开着再点就收（常驻只能这样手动收），其余时候打开
  function openSide() {
    if (sideWide.value && sideOpen.value) return void (sideOpen.value = false);
    sideView.value = 'main';
    sideOpen.value = true;
    loadRecent();
  }
  // 新会话页右上角的头像：直接打开侧边栏的账号页（照 Gemini）
  function openMe() {
    sideView.value = 'me';
    sideOpen.value = true;
  }
  // 在侧边栏里挑了会话、开了新会话、去搜索：窄屏上用完就收，宽屏上常驻不收
  function sideDone() {
    if (!sideWide.value) sideOpen.value = false;
  }
  // 会话那一页的列表滚动区：滚下去了，顶上新会话药丸下沿露出一条淡出；下面还有，账号那一行上沿露出一条淡出（照 Gemini）
  const sideScrolled = ref(false), sideMoreBelow = ref(false);
  function onSideScroll(e) {
    const el = e?.target || document.querySelector('.side-scroll');
    if (!el) return;
    sideScrolled.value = el.scrollTop > 0;
    sideMoreBelow.value = el.scrollTop + el.clientHeight < el.scrollHeight - 1;
  }
  // 列表变长变短（取到了、接了一页）也要重新看一眼下面还有没有
  watch(() => recent.value.length + recentTabs.value.length, () => Vue.nextTick(onSideScroll));
  // 侧边栏的时间只写最大的一个单位（util.js 的 shortSpan）
  const shortAgo = t => shortSpan(Date.now() / 1000 - t);
  // 简洁模式手机上新会话页账号卡片的查询时间：同上只写最大的一个单位，后面跟 ago，不到一分钟写 just now
  const agoEn = t => { const s = shortAgo(t); return s === '<1m' ? 'just now' : `${s} ago`; };
  // scope：global 全局那份，mine 自己的
  async function saveSetting(key, value, scope = 'global') {
    if (globalSettings.busy) return;
    globalSettings.busy = true;
    try {
      applySettings(await api('/api/settings', { method: 'POST', body: { [key]: value, scope } }));
      globalSettings.error = '';
      say(scope === 'mine' ? '您的默认账号已保存，所有设备生效' : '全局设置已保存，所有设备生效');
    } catch (e) {
      globalSettings.error = e.message;
      fail(scope === 'mine' ? '保存默认账号' : '保存全局设置', e);
    }
    globalSettings.busy = false;
  }
  // 新会话没点过号时用的号：服务器按设置挑好的（auto 是此刻最宽裕的），界面上直接当成选中了它，
  // 不写「自动」「默认」这些机制（内部怎么挑的只给开发者看）；挑不出来是空，就哪个都不选
  const pickedAccount = environment => (environment === 'private' ? globalSettings.private
    : globalSettings.default.account || defaultAccount.value) || '';

  // 新建会话：在某个工作目录里说出第一句话，会话文件建出来就打开它
  // 账号一打开就选好服务器挑的那个号（取到设置以后），想换就在下拉里换；标签默认用上次选的，在全部会话里正筛着某个标签时再把它也带上
  const newChat = reactive({ open: false, cwd: '', text: '', account: '', tags: [], error: '', busy: false });
  function openNewChat(cwd = '') {
    if (readonly.value) return;
    const tags = store.get('newChatTags', []);
    if (overview.open && overview.tag && !tags.includes(overview.tag)) tags.push(overview.tag);
    Object.assign(newChat, { open: true, cwd, text: '', account: pickedAccount(pickerEnv()), tags, error: '' });
    loadTags();
    loadSettings().then(() => newChat.account || (newChat.account = pickedAccount(pickerEnv())));
  }
  async function createChat() {
    if (readonly.value || newChat.busy || !newChat.text.trim()) return;
    Object.assign(newChat, { busy: true, error: '' });
    try {
      if (needEnvironment()) throw new Error(envNotReady());
      store.set('newChatTags', newChat.tags);
      // 这里不选人设卡，服务器按这个人设的默认来；默认那张要先问的话一样弹窗
      const persona = freshPersona({});
      const personaConsent = await confirmPersona(persona);
      const env = pickerEnv();
      const brief = await api('/api/sessions', { method: 'POST',
        body: { cwd: newChat.cwd, text: newChat.text, account: newChat.account || null, tags: newChat.tags,
                ...(env ? { environment: env } : {}), ...(persona === '' ? { persona } : {}),
                ...(personaConsent ? { personaConsent } : {}) } });
      await openTab(brief.path);
      newChat.open = overview.open = false;
      loadTags();
    } catch (e) { newChat.error = e.message; }
    newChat.busy = false;
  }

  // 标签页栏的加号：左键开一个空白的新会话标签页（家目录、不带标签、账号按全局设置），
  // 说出第一句话才真正建会话；右键走选文件夹的交互式新建。只读模式不能新建，左右键都是打开会话。
  // 权限、模型、账号在看板的小标签上选（和已有的会话一样），先记在这个标签页上，建会话时一起交给服务器；
  // 模型不选就按 Claude Code 自己的设置。多用户实例里还有会话所属用户和可见范围：登录了就是自己的、默认只有自己看得到（可改），
  // 游客建的是无主会话
  // 运行环境、工作区、标签照上次发第一句话时选的填：登录了存在服务器上、各设备共用，
  // 游客和单用户实例记在这个浏览器里。上次选的独立环境现在没有了就是共用环境。人设卡另有自己的默认，见 personas.js
  const lastFresh = () => (me.multi && me.name ? globalSettings.fresh : store.get('freshTab', null)) || {};
  // 新会话标签页上一次选的标签按设备记（createFromTab 发话时存）；这台设备上没记过的，照旧用账号上记的（lastFresh）
  const freshTagsKey = () => `freshTags:${me.name || ''}`;
  function freshPrefs(owner) {
    const p = lastFresh();
    // 新用户（和关了共用环境的人）只有独立环境；还没建好时先填共用，建好了由上面的 watch 换过去，没建好发不了话
    const environment = (onlyPrivate() || p.environment === 'private') && environments.environment ? 'private' : 'shared';
    const here = store.get(freshTagsKey(), null);
    // 上次的工作区只在环境和上次一样时填回去（另一个环境的路径多半进不去）；上次就是家目录（~）的写成绝对路径
    const last = p.environment === environment && p.cwd && p.cwd !== '~' ? p.cwd : '';
    return { environment, cwd: last || homeOf(environment) || '~', tags: [...(Array.isArray(here) ? here : p.tags || [])], visible: owner ? [] : null };
  }
  // blank：要往输入框里写话（设备页「让 Claude 帮我看看」），只复用草稿是空的，免得盖掉人写了一半的话
  function openFresh({ blank = false } = {}) {
    if (readonly.value) return openPicker();
    // 已经有一个还没发出第一句话的新会话标签页就切过去，写了一半的草稿留着，不再多开一个
    // （不然连点几次新会话，侧边栏里多出好几颗「新会话」）。发出了、正在建的不算
    const idle = tabs.value.find(t => t.fresh && !t.busy && !t.birth && !(blank && t.draft?.trim()));
    if (idle) {
      select(idle);
      return idle;
    }
    const owner = me.multi && me.name ? me.name : null;
    const start = freshPrefs(owner);
    const tab = newFresh({ ...start, account: '', modes: modeList.value, webMode: defaultMode.value, webModel: null, webEffort: null, owner });
    loadTags();
    // 页面刚打开时设置可能还没取到：取到以后，这几项还没动过就换成上次选的
    loadSettings().then(() => {
      // 工作区还停在开头填的、或者只是被换成了家目录的绝对路径（上面 environments 的 watch），都算没动过
      if (tab.fresh && !tab.busy && tab.environment === start.environment && [start.cwd, homeOf(start.environment)].includes(tab.cwd)
          && tab.tags.join('\n') === start.tags.join('\n')) Object.assign(tab, freshPrefs(owner));
    });
    return tab;   // 设备页「请 Claude 排查」要往它的输入框里写话
  }
  // 说出第一句话：服务器做完马上能做完的检查就回一个编号（见 server.Birth），这个标签页订阅它的推送，
  // 核对额度、启动 Claude Code、出网……走到哪一步和已有会话一样显示；建成了才返回，没建成抛出原因（连同最后停在哪一步，见 tabs.js 的 onBirth）
  async function createFromTab(tab, text, images = []) {
    if (images.length && !firstMessageImages.value) throw new Error('当前服务器还不能用图片开新会话，图片已保留；请等服务更新后重试');
    if (needEnvironment()) throw new Error(envNotReady());
    tab.busy = true;
    try {
      // 标签那一项关着（设置里的开关，默认关）就不打标签，也不改这台设备记着的上次选择
      const persona = freshPersona(tab), tags = feature('tags') ? tab.tags : [];
      // 这次选的标签记在这台设备上，下次开新会话标签页照着预先选好（见 freshPrefs）
      if (feature('tags')) store.set(freshTagsKey(), [...tags]);
      const environment = onlyPrivate() ? 'private' : tab.environment || 'shared';
      // 选的卡要先问（带命令、脚本，或者很长）：弹窗同意了才往下走，取消了这句话退回输入框（personas.js 的 confirmPersona）
      const personaConsent = await confirmPersona(persona);
      const r = await api('/api/sessions', { method: 'POST',
        // 没点过号就用界面上选中的那个（服务器挑好的），不让服务器再挑一次换成别的
        body: { cwd: tab.cwd, text, ...(images.length ? { images: images.map(im => ({ media_type: im.media_type, data: im.data })) } : {}),
                account: tab.account || pickedAccount(tab.environment) || null, tags,
                mode: tab.webMode || null, model: tab.webModel || null, effort: tab.webEffort || null,
                visible: tab.owner ? tab.visible : null, environment,
                // 人设卡同样交界面上选中的那张（没选过就是自己设的默认），空字符串是不用
                persona, ...(personaConsent ? { personaConsent } : {}), remember: true,
                // 取消勾选的工作设备（devices.js 的 toggleWork），没取消过不交：登记了的都能用
                ...(tab.workOff?.length ? { devicesOff: tab.workOff } : {}),
                // 从待办页勾选发来的那几条（todos.js 的 sendPicked）：服务器记上从它们开出、替这个会话认领。旧后端不认这个字段，照常建会话
                ...(tab.todoIds?.length ? { todos: tab.todoIds } : {}) } });
      // 服务器收下了就记住这次选的（登录了服务器已经存好，这里只是不用等下次取设置），下个新会话标签页照着填
      const fresh = { environment, cwd: tab.cwd.trim() || '~', tags: [...tags] };
      if (me.multi && me.name) globalSettings.fresh = fresh;
      else store.set('freshTab', fresh);
      rememberPersona(persona);
      const born = new Promise((resolve, reject) => (tab.birthWait = { resolve, reject }));
      Object.assign(tab, { birth: r.id, running: true, phase: r.phase, phaseAt: Date.now() });
      await born;
      // 建成了才清：没建成话退回输入框，说明卡片还挂着，再发一次照样算从这几条待办开出
      Object.assign(tab, { todoIds: null, todoNote: null });
      loadTags();
    } finally { tab.busy = false; }
  }

  // 改了名（app.js 的 saveRename）：侧边栏那一行和全部会话里的卡片跟着换，不用等重新取。
  // 会话开着时改名改的是标签页，它们是另外的对象，以前要等下次取列表才变（宽屏常驻的侧边栏只在换会话、重新打开时取）
  function renamed(id, title) {
    if (rowMap.has(id)) rowMap.get(id).title = title;
    for (const g of overview.groups) for (const s of g.sessions) if (s.id === id) s.title = title;
  }

  // 编辑标签：标签页和全部会话里的卡片右键都能打开，按会话编号改，会话不用先打开
  const tagEdit = reactive({ open: false, id: '', title: '', tags: [], error: '', busy: false });
  function openTagEdit(s) {
    Object.assign(tagEdit, { open: true, id: s.id, title: s.title || fileName(s.path), tags: [...(s.tags || [])], error: '' });
    loadTags();
  }
  async function saveTags() {
    if (tagEdit.busy) return;
    tagEdit.busy = true;
    try {
      const { id, tags } = await api(`/api/sessions/${tagEdit.id}/tags`, { method: 'POST', body: { tags: tagEdit.tags } });
      // 开着的标签页和看板上的卡片都跟着换，不用重新拉
      for (const t of tabs.value) if (t.id === id) t.tags = tags;
      for (const g of overview.groups) for (const s of g.sessions) if (s.id === id) s.tags = tags;
      if (rowMap.has(id)) rowMap.get(id).tags = tags;
      tagEdit.open = false;
      loadTags();
    } catch (e) { tagEdit.error = e.message; }
    tagEdit.busy = false;
  }

  return {
    environments, loadEnvironments, createEnvironment, changeEnvironment, pickEnvironment, homeOf,
    picker, loadRoot, configError, shareLink, revokeShare, browse, globalSettings, loadSettings, saveSetting, applySettings, pickedAccount, effortList, defaultEffort, defaultModel, openPicker, addTab, submitPath, accounts, defaultAccount,
    feature, featureBusy, setFeature, switchBusy, setAutoSwitch, keepTabs, tabsBusy, setBottomTabs, vertTabs, sideTabs,pwaCookie, pwaFull, setPwaFull, freshPersona, onlyPrivate, sideOpen, sideView, sideWide, openSide, openMe, sideDone,
    overview, ovView, ovGroup, byRecent, openedIds, openOverview, pickCard, tagCounts, shownGroups, hoverTag,
    showTests, testsBusy, visibleGroups, othersGroup, othersOpen, ovOwn,
    groupsRef, foldAt, cardsOf, openGroups, toggleGroup, trashGroup, forget, restore,
    openDd, toggleDd, recent, recentTabs, loadRecent, openRecent, recentState, sidePins, togglePin,
    sideMoreRef, sideHasMore, sideMoreState, retryMore, prefillSide, sideDot, sideStatesErr,
    sideScrolled, sideMoreBelow, onSideScroll, shortAgo, agoEn, sideFilter, sideFilterLabel, loadSideTags,
    newChat, openNewChat, createChat, openFresh, createFromTab,
    allTags, tagEdit, openTagEdit, saveTags, tagColors, setTagColor, renamed,
  };
}
