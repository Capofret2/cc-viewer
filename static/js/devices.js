// 「设备」页（后端见 devices.py）：自己的设备列表，看得见、管得了，不用非得跟 AI 说。
// 列表里每台设备写着在不在线、Claude 最近在上面执行了什么，能测试连接、暂停访问、改名、删除；「添加设备」分电脑和服务器两种：
// 电脑是在那台电脑上运行一行命令，窗口里实时显示装到哪一步（就是 Claude 在对话里看的那份进度），出错就用大白话说怎么办；
// 哪里解决不了，点「请 Claude 排查」开一个新对话，把链接和出错的原话写进输入框交给 Claude。
// Claude 在对话里给的 #dev=<链接名字> 链接点开就是同一个添加窗口。只在开了 ssh 门卫的实例、登录以后有（me.devices）
const { reactive, computed } = Vue;
import { api, me, say, copyText, ago, who } from './util.js';

export const OS_LABEL = { windows: 'Windows', mac: 'Mac', linux: 'Linux' };
const STEPS = ['运行命令', '安装 SSH 服务', '允许 Claude 登录', '登记这台电脑', '建立连接'];
const POLL_MS = 2000;

// 页面多半就是在要连的那台电脑上打开的：按浏览器猜系统，猜不到（手机、平板）就让用户自己选
function guessOs() {
  const ua = navigator.userAgent || '', p = navigator.userAgentData?.platform || navigator.platform || '';
  if (/Android|iPhone|iPad/i.test(ua)) return '';
  if (/Win/i.test(p) || /Windows/i.test(ua)) return 'windows';
  if (/Mac/i.test(p) || /Mac OS X/i.test(ua)) return 'mac';
  if (/Linux/i.test(p)) return 'linux';
  return '';
}

// 各系统怎么打开命令窗口、怎么粘贴：照着做就行，不用懂命令行
export const HOWTO = {
  windows: ['按 Windows 键，输入 PowerShell 并按回车打开（普通窗口即可，无需管理员权限）。',
            '点击下方「复制」，在 PowerShell 窗口中单击鼠标右键粘贴，然后按回车。',
            'Windows 询问「你要允许此应用对你的设备进行更改吗？」时点击「是」，随后查看新弹出的窗口，安装完成后显示 Done。'],
  mac: ['按 Command + 空格，输入「终端」（或 Terminal）并按回车打开。',
        '点击下方「复制」，在终端中按 Command + V 粘贴，然后按回车。',
        '提示输入密码时，输入这台 Mac 的开机密码（输入时不显示）并按回车。安装完成后显示 Done。'],
  linux: ['打开终端（多数桌面环境可按 Ctrl + Alt + T）。',
          '点击下方「复制」，在终端中按 Ctrl + Shift + V 粘贴，然后按回车。',
          '提示输入密码时，输入这台电脑的开机密码（输入时不显示）并按回车。安装完成后显示 Done。'],
};

export function useDevices({ askClaude, meReady }) {
  // loaded：取到过一次列表（小红点要等取到了才知道有没有设备）；work：后端认得会话的工作设备，旧后端没有
  const dev = reactive({
    open: false, view: 'list', loading: false, loaded: false, error: '', devices: [], pending: [], pubkey: '', work: false,
    workBusy: '', workError: '',
    expanded: '', busy: '', msg: {}, hist: {}, edit: { name: '', to: '', note: '' }, confirm: '', cleanup: true, removal: null,
    link: null, linkError: '', os: guessOs() || 'windows',
    server: { name: '', login: '', host: '', port: 22, via: '', busy: false, error: '', result: null },
  });
  let timer = 0, polling = '', adoptOs = false;

  async function loadDevices() {
    dev.loading = true;
    try {
      Object.assign(dev, await api('/api/devices'), { error: '', loaded: true });
    } catch (e) { dev.error = `无法获取设备列表：${e.message}`; }
    dev.loading = false;
  }
  function openDevices() {
    Object.assign(dev, { open: true, view: 'list', removal: null, confirm: '' });
    loadDevices();
  }
  function stopPoll() {
    clearTimeout(timer);
    polling = '';
  }
  function closeDevices() {
    stopPoll();
    dev.open = false;
  }
  function devBack() {
    stopPoll();
    Object.assign(dev, { view: 'list', link: null, linkError: '' });
    loadDevices();
  }

  // ---------- 添加电脑：生成链接、看进度 ----------
  async function pollLink(tok) {
    if (polling !== tok) return;
    try {
      dev.link = await api(`/api/devices/links/${tok}`);
      dev.linkError = '';
      // 重新打开的链接：命令先显示正在装的那种系统（只知道下载的是 Mac/Linux 那份、浏览器又猜不出时当 Linux）
      if (adoptOs && dev.link.os) dev.os = dev.link.os !== 'unix' ? dev.link.os : ['mac', 'linux'].includes(dev.os) ? dev.os : 'linux';
      adoptOs = false;
    } catch (e) { dev.linkError = `无法获取进度：${e.message}`; }
    if (polling === tok) timer = setTimeout(() => pollLink(tok), POLL_MS);
  }
  function watchLink(tok) {
    stopPoll();
    polling = tok;
    pollLink(tok);
  }
  async function addPc() {
    Object.assign(dev, { view: 'pc', link: null, linkError: '' });
    adoptOs = false;
    try {
      const st = await api('/api/devices/links', { method: 'POST' });
      dev.link = st;
      watchLink(st.tok);
    } catch (e) { dev.linkError = `无法生成安装命令：${e.message}`; }
  }
  // 列表里「正在添加」的那几行、对话里 Claude 给的 #dev= 链接：打开同一个窗口接着看
  function openLink(tok) {
    Object.assign(dev, { open: true, view: 'pc', link: null, linkError: '' });
    adoptOs = true;
    watchLink(tok);
  }
  const linkCmd = computed(() => (!dev.link ? '' : dev.os === 'windows' ? dev.link.win : dev.link.unix));
  const linkLeft = computed(() => (dev.link ? Math.round((dev.link.expires * 1000 - Date.now()) / 60000) : 0));
  const linkOk = computed(() => dev.link?.machines.find(m => m.ok) || null);
  const linkBad = computed(() => dev.link?.machines.find(m => m.ok === false) || null);
  // 第几步出的错：电脑报的是「error step 2: ...」
  const errStep = computed(() => {
    const m = /error step (\d)/.exec(dev.link?.error || '');
    return m ? +m[1] : null;
  });
  // 进度那几步各是什么状态：done 做完了、now 正在做（或在等用户运行）、bad 出错、todo 还没到
  // （服务器给的 cur：0 等用户运行命令，1～3 电脑报的第几步，4 报到好了、在等连通，5 全好了）
  const linkSteps = computed(() => {
    const st = dev.link;
    if (!st) return [];
    const cur = Math.min(st.cur, 5), bad = st.error ? (errStep.value ?? cur) : linkBad.value ? 4 : -1;
    return STEPS.map((name, i) => {
      const state = i === bad ? 'bad' : i < cur ? 'done' : i === cur ? 'now' : 'todo';
      const note = i === 1 && cur === 1 && st.elevate && !st.error ? '等待您在弹出的窗口中点击「是」'
        : i === 0 && cur === 0 ? '请在要连接的电脑上运行上方命令' : '';
      return { name, state, note };
    });
  });
  async function copyCmd() {
    if (await copyText(linkCmd.value)) say('已复制，请在命令窗口中粘贴');
    else say('复制失败：浏览器未允许，请手动选中命令复制', { err: true });
  }

  // ---------- 列表里的每台设备 ----------
  const devState = d => {
    if (d.paused) return { cls: 'paused', text: '已暂停访问' };
    if (d.kind === 'pc') {
      return d.online ? { cls: 'on', text: '在线' }
        : { cls: 'off', text: d.seen ? `离线 · 最后在线于${ago(d.seen)}` : '离线' };
    }
    if (!d.last) return { cls: '', text: '尚未连接' };
    return d.last.exit === 0 ? { cls: 'on', text: `${ago(d.last.ts)}连接成功` } : { cls: 'bad', text: `${ago(d.last.ts)}连接失败` };
  };
  const devDesc = d => (d.kind === 'pc'
    ? `${OS_LABEL[d.os] ? `${OS_LABEL[d.os]} ` : ''}电脑${d.hostname ? ` ${d.hostname}` : ''} · 用户 ${d.login}`
    : `服务器 ${d.login}@${d.host}${d.port !== 22 ? `:${d.port}` : ''}${d.via ? ` · 经 ${d.via}` : ''}`);
  // 门卫记账时会话那一栏写的是谁：查看器自己连的写「来源·动作」（设备页·测试连接、自动登记·登记时试连），
  // 其余是 Claude 在某个对话里（写的是会话编号）
  const byViewer = who => (who || '').includes('·');
  const histWho = h => (byViewer(h.who) ? `${h.who.replace('·', '（')}）` : 'Claude');
  const shortCmd = c => (c.length > 60 ? `${c.slice(0, 60)}…` : c);
  const lastText = d => (byViewer(d.last.who)
    ? `上次连接：${d.last.who.split('·')[1]}${d.last.exit === 0 ? '' : '，连接失败'}（${ago(d.last.ts)}）`
    : `Claude 上次执行：${shortCmd(d.last.cmd)}（${ago(d.last.ts)}）`);
  const addedText = d => ({ web: '，通过设备页添加', chat: '，由 Claude 在对话中添加' })[d.added] || '';
  // 列表里「正在添加」那几行：图标和一句话（cur 的意思见 linkSteps）
  const pendingIcon = p => (p.error ? 'bad' : p.stuck ? 'wait' : 'now');
  const pendingText = p => (p.error ? `出错：${p.tip || p.error}`
    : p.stuck ? `已 ${Math.round(p.stuck / 60)} 分钟无进展，请点击查看`
    : p.elevate && p.cur === 1 ? '等待您在电脑上弹出的窗口中点击「是」' : `正在「${STEPS[Math.min(p.cur, 4)]}」`);

  async function toggleDev(d) {
    Object.assign(dev, { expanded: dev.expanded === d.name ? '' : d.name, confirm: '', removal: null });
    dev.edit.name = '';
    if (dev.expanded !== d.name) return;
    try {
      dev.hist[d.name] = (await api(`/api/devices/${encodeURIComponent(d.name)}/history`)).rows;
    } catch (e) { dev.msg[d.name] = { ok: false, text: `无法获取执行记录：${e.message}` }; }
  }
  async function busyDo(key, fn) {
    if (dev.busy) return;
    dev.busy = key;
    try { await fn(); } finally { dev.busy = ''; }
  }
  const testDev = d => busyDo(`test:${d.name}`, async () => {
    dev.msg[d.name] = { ok: true, text: '正在连接…' };
    try {
      dev.msg[d.name] = await api(`/api/devices/${encodeURIComponent(d.name)}/test`, { method: 'POST' });
    } catch (e) { dev.msg[d.name] = { ok: false, text: e.message }; }
    loadDevices();
  });
  const pauseDev = (d, paused) => busyDo(`pause:${d.name}`, async () => {
    try {
      await api(`/api/devices/${encodeURIComponent(d.name)}`, { method: 'POST', body: { paused } });
      dev.msg[d.name] = { ok: true, text: paused ? '已暂停：Claude 当前无法连接该设备，点击「恢复访问」可重新开放' : '已恢复：Claude 可以重新连接该设备' };
      await loadDevices();
    } catch (e) { dev.msg[d.name] = { ok: false, text: e.message }; }
  });
  function startEdit(d) {
    Object.assign(dev.edit, { name: d.name, to: d.name, note: d.note || '' });
    dev.confirm = '';
  }
  const saveEdit = d => busyDo(`edit:${d.name}`, async () => {
    try {
      const r = await api(`/api/devices/${encodeURIComponent(d.name)}`, { method: 'POST', body: { to: dev.edit.to, note: dev.edit.note } });
      dev.edit.name = '';
      if (dev.edit.to !== d.name) dev.expanded = dev.edit.to;
      say(r.text || '已保存');
      await loadDevices();
    } catch (e) { dev.msg[d.name] = { ok: false, text: e.message }; }
  });
  // 电脑默认连电脑上的东西一起卸；服务器默认不去删公钥：和它共用家目录的服务器会一起连不上
  function askDelete(d) {
    Object.assign(dev, { confirm: d.name, cleanup: d.kind === 'pc', removal: null });
    dev.edit.name = '';
  }
  const doDelete = d => busyDo(`del:${d.name}`, async () => {
    try {
      const r = await api(`/api/devices/${encodeURIComponent(d.name)}?cleanup=${dev.cleanup ? 1 : 0}`, { method: 'DELETE' });
      Object.assign(dev, { removal: { name: d.name, kind: d.kind, os: d.os, steps: r.steps }, confirm: '', expanded: '' });
      await loadDevices();
    } catch (e) { dev.msg[d.name] = { ok: false, text: `删除失败：${e.message}` }; }
  });
  // 电脑离线、没卸掉时，用户自己在那台电脑上卸用的命令（和安装命令一个样子）：卸载脚本由查看器自己发（/pc-setup/uninstall.*），
  // 地址用实例配置的对外地址（instance.json 的 pc_setup_url），没配就用浏览器当时打开查看器的地址，那台电脑要访问得到它
  const base = () => who.pcSetupUrl || location.origin;
  const UNINSTALL = {
    get windows() { return `[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm ${base()}/pc-setup/uninstall.ps1 | iex`; },
    get unix() { return `bash -c "$(curl -fsSL ${base()}/pc-setup/uninstall.sh || wget -qO- ${base()}/pc-setup/uninstall.sh)"`; },
  };
  async function copyUninstall(os) {
    if (await copyText(UNINSTALL[os])) say('已复制，请在该电脑的命令窗口中粘贴运行（Windows 需以管理员身份打开 PowerShell）');
    else say('复制失败，请手动选中命令复制', { err: true });
  }

  // ---------- 添加服务器 ----------
  function addServer() {
    Object.assign(dev.server, { name: '', login: '', host: '', port: 22, via: '', busy: false, error: '', result: null });
    dev.view = 'server';
  }
  // 往服务器上加公钥的那行命令：authorized_keys 最后一行没换行时先补一个（不补会把公钥粘到别人那行上，两把都坏）
  const keyCmd = computed(() => (!dev.pubkey ? '' : 'mkdir -p ~/.ssh && f=~/.ssh/authorized_keys && touch $f && '
    + `{ [ -z "$(tail -c 1 $f)" ] || echo >> $f; } && { grep -qxF '${dev.pubkey}' $f || echo '${dev.pubkey}' >> $f; } && chmod 700 ~/.ssh && chmod 600 $f`));
  async function copyKeyCmd() {
    if (await copyText(keyCmd.value)) say('已复制，请登录该服务器后粘贴运行');
    else say('复制失败，请手动选中命令复制', { err: true });
  }
  async function saveServer() {
    const s = dev.server;
    if (s.busy) return;
    if (!s.name.trim() || !s.login.trim() || !s.host.trim()) return (s.error = '请填写名字、用户名和地址');
    Object.assign(s, { busy: true, error: '', result: null });
    try {
      s.result = await api('/api/devices/servers', { method: 'POST',
        body: { name: s.name, login: s.login, host: s.host, port: +s.port || 22, via: s.via || null } });
      loadDevices();
    } catch (e) { s.error = e.message; }
    s.busy = false;
  }

  // ---------- 交给 Claude ----------
  function askLink() {
    const st = dev.link;
    if (!st) return;
    const step = STEPS[Math.min(st.cur, 4)];
    const what = st.error ? `页面上显示出错了：${st.error}`
      : linkBad.value ? `电脑接上了，但从${who.host}登录它没成功：${linkBad.value.why}`
      : st.stuck ? `已经 ${Math.round(st.stuck / 60)} 分钟停在「${step}」没有动静了` : `现在停在「${step}」`;
    closeDevices();
    askClaude(`我在网页的「设备」页添加电脑（安装链接 ${st.tok}），${what}。请帮我看看是怎么回事、该怎么办。`);
  }
  function askClaudeDev(d) {
    const m = dev.msg[d.name];
    closeDevices();
    askClaude(`我的设备「${d.name}」（${devDesc(d)}）现在显示「${devState(d).text}」`
      + (m && !m.ok ? `，测试连接的结果是：${m.text}` : '') + '。请帮我看看怎么回事。');
  }

  // Claude 在对话里给的链接：地址后面带 #dev=<链接名字>，打开添加窗口接着看，打开了就把这截从地址栏去掉
  function fromHash() {
    const tok = new URLSearchParams(location.hash.slice(1)).get('dev');
    if (!tok) return;
    history.replaceState(history.state, '', location.pathname + location.search);   // 带着 state：返回键（back.js）靠它认这一步
    if (!me.devices) return say(me.name ? '当前实例未提供设备页' : '请登录后查看安装进度', { err: true, ms: 0 });
    openLink(tok);
  }
  meReady.then(fromHash);
  window.addEventListener('hashchange', fromHash);

  const devTitle = computed(() => ({ list: '我的设备', choose: '添加设备', pc: '添加我的电脑', server: '添加服务器' })[dev.view]);

  // 侧边栏账号页顶上那张设备卡片（设备要显眼，不用先点进去）：点某一台打开设备面板、直接展开它；
  // 点「添加」直接进挑电脑还是服务器那一页
  async function openDevice(d) {
    openDevices();
    if (dev.expanded !== d.name) await toggleDev(d);
  }
  function addDevice() {
    openDevices();
    dev.view = 'choose';
  }

  // ---------- 会话的工作设备（服务器 workdev.py）：Claude 在哪几台上干活，取消了勾选的门卫不放行 ----------
  // 默认全勾上：登记了的都能用，后来加的也马上能用，除非用户在这个会话里主动取消，
  // 所以记的是取消了哪些（workOff）。Local 就是 Claude 所在的那台机器（服务器和系统提示词里叫「云电脑」），一直在、取消不了；
  // 界面上写 Local。
  // 新会话标签页先记在标签页上（tab.workOff），发第一句话时交给服务器；已有的会话一改就交给服务器（POST /api/tabs/{id}/devices）
  const CLOUD = 'Local';
  // 有没有工作设备这一项：开了门卫、登录了、后端认得（/api/devices 给了 work）；已有的会话还要是自己的、快照里有 workOff。
  // 单用户实例里会话不记所属用户（owner 是 null），都是这一个人的
  const workMine = tab => tab.owner === me.name || (!me.multi && !!me.name);
  const workShown = tab => !!(tab && me.devices && dev.work && (tab.fresh || ('workOff' in tab && workMine(tab))));
  const workOn = (tab, name) => !(tab.workOff || []).includes(name);
  // 小标签上写「Remotes · 勾上的远程设备几台」，不算一直在的 Local
  // 底下那排放不下时只写图标加台数（workCount，app.js 的 fitTools）
  const workCount = tab => dev.devices.filter(d => workOn(tab, d.name)).length;
  function workText(tab) {
    return `Remotes · ${workCount(tab)}`;
  }
  async function toggleWork(tab, d) {
    if (dev.workBusy) return;
    const off = tab.workOff || [];
    const next = off.includes(d.name) ? off.filter(n => n !== d.name) : [...off, d.name];
    dev.workError = '';
    if (tab.fresh) return void (tab.workOff = next);
    dev.workBusy = tab.id;
    try {
      Object.assign(tab, await api(`/api/tabs/${tab.id}/devices`, { method: 'POST', body: { off: next } }));
    } catch (e) { dev.workError = `工作设备修改失败：${e.message}`; }
    dev.workBusy = '';
  }
  // 小红点：一台自己的设备都没有（只有云电脑）时，从「＋ 添加设备」一路往外点到侧边栏的按钮上，加了第一台就一起消失
  const devDot = computed(() => !!(me.devices && dev.loaded && !dev.devices.length));

  return {
    workMine, workShown, workOn, workText, workCount, toggleWork, devDot, CLOUD,
    dev, loadDevices, openDevices, openDevice, addDevice, closeDevices, devBack, addPc, openLink, linkCmd, linkLeft, linkOk, linkBad, linkSteps, copyCmd,
    devState, devDesc, histWho, lastText, addedText, pendingIcon, pendingText, toggleDev, testDev, pauseDev, startEdit, saveEdit, askDelete, doDelete,
    copyUninstall, addServer, keyCmd, copyKeyCmd, saveServer, askLink, askClaudeDev, devTitle, HOWTO, OS_LABEL, UNINSTALL,
  };
}
