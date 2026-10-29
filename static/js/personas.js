// 「人设卡」页（后端见 personas.py）：新建会话时选一张，卡上的内容接在系统提示词最后。
// 列表里自己的卡在前、别人公开的在后，点一张展开全文和操作：自己的能改、公开或撤回、删除；谁的都能设成自己默认选的、复制成自己的。
// 新会话标签页上的「人设卡」下拉也用这里的列表；没选过就是自己设的默认（没设是不用），和账号一样，界面上直接当成选中了它。
// 会话里的 Claude 用 ccpersona 管的是同一份卡。
// 卡上还能挂钩子（写法同 Claude Code 的 settings.json，外加文字钩子）、脚本和补充资料。编辑框里钩子一行一个，
// 存的时候按时机和匹配条件拼回 settings.json 的样子。资料可能有几十 MB，编辑时原来的只交指纹不回传全文。
// 带命令、脚本，或者正文、文字钩子超过提醒线的卡，别人第一次用时先弹窗给人看、同意了才建会话（confirmPersona），同意过这一版就不再问。
// 网页文件两个实例现读：后端还没换新的时没有 hooks、scripts、consentHash 这些字段，这里都按没有处理，老流程照常走
const { reactive, computed, watch } = Vue;
import { api, me, say, store } from './util.js';

export const HOOK_EVENTS = { SessionStart: '会话开始', UserPromptSubmit: '提交消息', PreToolUse: '工具调用前', PostToolUse: '工具调用后',
  PostToolUseFailure: '工具调用出错后', PermissionRequest: '请求权限时', Notification: '通知', Stop: '回复结束时',
  SubagentStart: '子代理开始', SubagentStop: '子代理结束', PreCompact: '压缩前', SessionEnd: '会话结束' };

// settings.json 那样的 {时机: [{matcher, hooks}]} 和编辑框里一行一个的互相换
function rowsOf(hooks) {
  const rows = [];
  for (const [event, groups] of Object.entries(hooks || {}))
    for (const g of groups)
      for (const h of g.hooks)
        rows.push({ key: Math.random(), event, matcher: g.matcher || '', type: h.type, text: h.text || '',
                    choices: (h.choices || []).join('\n'), command: h.command || '', timeout: h.timeout || '' });
  return rows;
}
function hooksOf(rows) {
  const out = {};
  for (const r of rows) {
    const choices = r.choices.split('\n').map(s => s.trim()).filter(Boolean);
    const h = r.type === 'text' ? { type: 'text', text: r.text, ...(choices.length ? { choices } : {}) }
      : { type: 'command', command: r.command, ...(r.timeout ? { timeout: +r.timeout } : {}) };
    const groups = (out[r.event] ||= []);
    let g = groups.find(x => x.matcher === r.matcher.trim());
    if (!g) groups.push(g = { matcher: r.matcher.trim(), hooks: [] });
    g.hooks.push(h);
  }
  return out;
}

export function usePersonas() {
  const pc = reactive({
    open: false, loading: false, loaded: false, error: '', cards: [], default: null, defaultGone: '',
    max: { name: 40, text: 100000, softText: 20000, hookText: 20000, filesMB: 50, scripts: 5, script: 20000, timeout: 60, hooks: 20,
           textEvents: { SessionStart: 5000, UserPromptSubmit: 2000, PreToolUse: 2000, PostToolUse: 2000 } },
    expanded: '', busy: '', msg: {}, edit: null, confirm: '',
    ask: null,   // 第一次用一张要先问的卡时的弹窗：{ card（全文）, resolve }
  });

  async function loadPersonas() {
    pc.loading = true;
    try {
      Object.assign(pc, await api('/api/personas'), { error: '', loaded: true });
    } catch (e) { pc.error = `无法获取人设卡：${e.message}`; }
    pc.loading = false;
  }
  // 知道是谁以后取一次，登录、退出、换人以后重新取（各人能选的卡不一样）
  watch(() => me.loaded && me.personas && [me.name], v => v && loadPersonas(), { immediate: true });

  function openPersonas() {
    Object.assign(pc, { open: true, edit: null, confirm: '', msg: {} });
    loadPersonas();
  }
  function closePersonas() { pc.open = false; }
  function togglePersona(c) {
    pc.expanded = pc.expanded === c.id ? '' : c.id;
    pc.confirm = '';
  }

  // 编辑框：新建（id 为空）、改自己的、把别人公开的复制成自己的（也是新建）。卡上有脚本、资料时先取全文（脚本要能改，资料要拿到指纹），
  // 取不到就不打开编辑框
  async function startPersona(c = null, copy = false) {
    if (pc.busy) return;
    let full = c;
    if (c && ((c.files || []).length || (c.scripts || []).length)) {
      pc.busy = `open:${c.id}`;
      try {
        full = await api(`/api/personas/${c.id}`);
      } catch (e) {
        pc.msg = { [c.id]: { ok: false, text: `无法获取卡上的脚本和资料，编辑框无法打开：${e.message}` } };
        return;
      } finally { pc.busy = ''; }
    }
    const files = (full?.files || []).map(f => ('sha' in f ? { name: f.name, sha: f.sha, size: f.size } : { name: f.name, text: f.text, size: f.text.length }));
    const scripts = (full?.scripts || []).map(s => ({ name: s.name, text: s.text }));
    const base = { text: c ? c.text : '', files, scripts, hooks: rowsOf(full?.hooks), error: '' };
    pc.edit = c && !copy ? { ...base, id: c.id, name: c.name, public: c.public }
      : { ...base, id: '', name: c ? `${c.name}（副本）`.slice(0, pc.max.name) : '', public: false };
  }
  // 从本机挑文本文件当补充资料或脚本，文件名就是名字，同名的换掉。只收 UTF-8 文本：读出来有替换字符就当成不是文本
  async function pickTexts(ev, what, take) {
    const e = pc.edit;
    const picked = [...ev.target.files];
    ev.target.value = '';
    if (!e) return;
    e.error = '';
    for (const f of picked) {
      const text = await f.text();
      if (text.includes('�')) { e.error = `「${f.name}」不是 UTF-8 文本，${what}仅支持文本文件`; continue; }
      take(f.name, text);
    }
  }
  const addPersonaFiles = ev => pickTexts(ev, '补充资料', (name, text) => {
    pc.edit.files = [...pc.edit.files.filter(x => x.name !== name), { name, text, size: text.length }];
  });
  const addPersonaScripts = ev => pickTexts(ev, '脚本', (name, text) => {
    pc.edit.scripts = [...pc.edit.scripts.filter(x => x.name !== name), { name, text }];
  });
  const dropPersonaFile = name => pc.edit && (pc.edit.files = pc.edit.files.filter(f => f.name !== name));
  const dropPersonaScript = name => pc.edit && (pc.edit.scripts = pc.edit.scripts.filter(f => f.name !== name));
  const addHookRow = () => pc.edit.hooks.push({ key: Math.random(), event: 'UserPromptSubmit', matcher: '', type: 'text', text: '',
                                                  choices: '', command: '', timeout: '' });
  const dropHookRow = r => (pc.edit.hooks = pc.edit.hooks.filter(x => x !== r));
  const hookSize = r => r.text.trim().length + Math.max(0, ...r.choices.split('\n').map(s => s.trim().length));
  // 文字钩子超过这个时机的提醒线：照样能存，但每次都多带这么多字，旁边标黄
  const textEvents = () => pc.max.textEvents || {};
  // 后端还是老的（max 里没有钩子的上限）就不显示钩子、脚本两块：老后端不认，填了存不进去
  const hooksSupported = computed(() => !!pc.max.textEvents);
  const hookHeavy = r => r.type === 'text' && hookSize(r) > (textEvents()[r.event] ?? Infinity);
  const textEvent = ev => ev in textEvents();

  async function savePersona() {
    const e = pc.edit;
    if (!e || pc.busy) return;
    if (!e.name.trim()) return (e.error = '请填写名字');
    pc.busy = 'save';
    try {
      const c = await api(e.id ? `/api/personas/${e.id}` : '/api/personas', { method: e.id ? 'PUT' : 'POST',
        body: { name: e.name, text: e.text, public: e.public, hooks: hooksOf(e.hooks), scripts: e.scripts,
                files: e.files.map(f => ('text' in f ? { name: f.name, text: f.text } : { name: f.name, sha: f.sha })) } });
      pc.edit = null;
      await loadPersonas();
      pc.expanded = c.id;
      pc.msg = { [c.id]: { ok: true, text: e.id ? '已保存，修改对之后新建的会话生效' : '已创建，新建会话时可选择它' } };
    } catch (err) { e.error = err.message; }
    pc.busy = '';
  }
  async function act(c, what, fn, ok) {
    if (pc.busy) return;
    pc.busy = `${what}:${c.id}`;
    try {
      await fn();
      await loadPersonas();
      pc.msg = { [c.id]: { ok: true, text: ok } };
    } catch (e) { pc.msg = { [c.id]: { ok: false, text: `${what}失败：${e.message}` } }; }
    pc.busy = '';
  }
  const publishPersona = (c, on) => act(c, on ? '公开' : '撤回公开',
    () => api(`/api/personas/${c.id}`, { method: 'PUT', body: { public: on } }),
    on ? '已公开：所有人新建会话时均可选择' : '已撤回公开：他人无法再选择，已有会话不受影响');
  const defaultPersona = (c, on) => act(c, on ? '设为默认' : '取消默认',
    () => api('/api/personas/default', { method: 'POST', body: { id: on ? c.id : null } }),
    on ? '已设为默认，新建会话将默认使用此卡' : '已取消默认，新建会话将不使用人设卡');
  async function deletePersona(c) {
    if (pc.busy) return;
    pc.busy = `del:${c.id}`;
    try {
      await api(`/api/personas/${c.id}`, { method: 'DELETE' });
      pc.confirm = pc.expanded = '';
      await loadPersonas();
      say(`已删除人设卡「${c.name}」`);
    } catch (e) { pc.msg = { [c.id]: { ok: false, text: `删除失败：${e.message}` } }; }
    pc.busy = '';
  }
  const ownerText = c => (c.mine ? (c.public ? '我的 · 已公开' : '我的 · 仅自己可用') : `由 ${c.owner} 公开`);
  const hookCount = c => Object.values(c.hooks || {}).reduce((n, gs) => n + gs.reduce((m, g) => m + g.hooks.length, 0), 0);
  const hookLine = (event, g, h) => `${HOOK_EVENTS[event] || event}${g.matcher ? `（匹配 ${g.matcher}）` : ''}：`
    + (h.type === 'text' ? `附文字 ${h.text}${h.choices?.length ? `\n  随机句子：${h.choices.join(' / ')}` : ''}`
      : `执行 ${h.command}${h.timeout ? `（最多 ${h.timeout} 秒）` : ''}`);
  const hookLines = c => Object.entries(c.hooks || {}).flatMap(([ev, gs]) => gs.flatMap(g => g.hooks.map(h => hookLine(ev, g, h))));

  // 新建会话前（sessions.js 的 createFromTab）：选的卡要先问时弹窗，同意了返回这一版的指纹交给服务器，取消了抛出原因（这句话退回输入框）。
  // 不用问的返回 null。游客记不住，同意过的指纹存在这个浏览器里；带命令、脚本的卡游客不能用，直接说明
  async function confirmPersona(id) {
    const c = id ? pc.cards.find(x => x.id === id) : null;
    if (!c || !c.consentHash || c.consented) return null;
    if (!me.name && c.runsCode) throw new Error(`人设卡「${c.name}」包含命令或脚本，游客无法使用，请登录后选择`);
    if (!me.name && store.get(`personaAck:${id}`, '') === c.consentHash) return c.consentHash;
    let full;
    try {
      full = await api(`/api/personas/${id}`);
    } catch (e) { throw new Error(`无法获取人设卡「${c.name}」的详情，无法确认：${e.message}`); }
    const ok = await new Promise(resolve => (pc.ask = { card: full, resolve }));
    pc.ask = null;
    if (!ok) throw new Error(`未同意使用人设卡「${c.name}」，消息未发送；可更换人设卡后重新发送`);
    if (me.name) c.consented = true;
    else store.set(`personaAck:${id}`, full.consentHash);
    return full.consentHash;
  }
  const answerAsk = ok => pc.ask?.resolve(ok);

  // 新会话标签页：选的是哪张（没选过就是默认的那张），下拉里的选项。列表没取到时不知道默认是哪张，
  // 交 null 让服务器按这个人设的默认来，不能当成不用
  // 选的存在 pickedPersona 上：persona 是建好的会话用的卡名（服务器的简报），标题栏按它显示
  // 新会话标签页发话时用了哪张，就成了下次默认选的：有名字的人由服务器顺手改成默认（server.new_session），
  // 和人设卡页上「设成默认」是同一个值，这里只是不用等重新取列表；游客记在这个浏览器里，卡没了就是不用
  const guestPick = () => { const id = store.get('freshPersona', ''); return pc.cards.some(c => c.id === id) ? id : ''; };
  const personaOf = tab => (tab.pickedPersona ?? (pc.loaded ? (me.name ? pc.default || '' : guestPick()) : null));
  function rememberPersona(id) {
    if (id === null) return;   // 列表没取到时交的是 null，服务器按默认来，默认没变
    if (me.name) Object.assign(pc, { default: id || null, defaultGone: '' });
    else store.set('freshPersona', id);
  }
  const personaOptions = computed(() => [{ value: '', label: '不使用人设卡' },
    ...pc.cards.map(c => ({ value: c.id, label: c.mine ? c.name : `${c.name}（${c.owner}）` }))]);

  return { pc, HOOK_EVENTS, loadPersonas, openPersonas, closePersonas, togglePersona, startPersona, addPersonaFiles, addPersonaScripts,
           dropPersonaFile, dropPersonaScript, addHookRow, dropHookRow, hookSize, hookHeavy, textEvent, hooksSupported, savePersona,
           publishPersona, defaultPersona, deletePersona, ownerText, hookCount, hookLines, confirmPersona, answerAsk,
           personaOf, rememberPersona, personaOptions };
}
