// 小工具：网络、设置存取、格式化。谁都能用，不依赖别的模块
const { ref, reactive, watch } = Vue;

// 界面上怎么称呼用这个查看器的人（气泡、刻度预览、找话、导出里的那个名字）。
// 每个实例在服务器配置里自己定（/api/config 的 call），默认是「用户」；取到配置前也先用它。
// host 是查看器所在的机器在界面上的叫法（host），pcSetupUrl 是装卸电脑脚本对外的地址（pcSetupUrl，空就用当时打开页面的地址），都见 instance.py
export const who = reactive({ me: '用户', host: '服务器', pcSetupUrl: '' });

// 现在是谁在用（见 users.py、users.js）：name 为 null 是游客；单用户实例（multi 为 false）里一直是那一个人。
// loaded 之前、或者没取到（error）时不知道是谁，不能当成游客
export const me = reactive({ loaded: false, error: '', name: null, admin: false, multi: false, stale: '',
                             balance: null, granted: 0, unrated: 0, paid: [], paidSince: {}, promo: {}, autoPromo: {}, guestName: '游客',
                             owns: [],   // 自己是号主的号（旧服务器不给，就当没有）
                             devices: false, personas: false });

// 出错时的说法要让人看得懂：连不上、回的不是 JSON（比如代理回了个错误网页）都翻成中文，带上状态码
export const api = async (url, { method = 'GET', body } = {}) => {
  let res;
  try {
    res = await fetch(url, {
      method, body: body && JSON.stringify(body), headers: { 'Content-Type': 'application/json' },
    });
  } catch {
    throw Object.assign(new Error('无法连接服务器'), { status: 0 });
  }
  let data;
  try { data = await res.json(); } catch {
    throw Object.assign(new Error(`服务器返回异常（${res.status} ${res.statusText}）`), { status: res.status });
  }
  if (!res.ok) throw Object.assign(new Error(data.detail || `${res.status} ${res.statusText}`), { status: res.status });
  return data;
};

// 页面底部的小提示条：复制、导出的结果，以及各种动作失败的原因都从这里说，谁都能用。
// 出错的停久一点、标成红色；ms 为 0 就一直挂着，直到下一句或 say('') 把它换掉
export const toast = reactive({ text: '', err: false });
let toastTimer = 0;
export function say(text, { err = false, ms } = {}) {
  Object.assign(toast, { text, err });
  clearTimeout(toastTimer);
  const hold = ms ?? (err ? 6000 : 1600);
  if (text && hold) toastTimer = setTimeout(() => (toast.text = ''), hold);
}
// 某个动作失败了：「什么失败了：为什么」
export const fail = (what, e) => say(`${what}失败：${e?.message || e}`, { err: true });

// 每台设备开了哪些标签、各种开关只记在自己的浏览器里；隐私模式等拿不到存储时就不记
export const store = {
  get(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
  },
};

// 存取统一走这两个：读出来的值直接就是响应式的，之后改了自动写回去
export function persistRef(key, fallback) {
  const r = ref(store.get(key, fallback));
  watch(r, v => store.set(key, v));
  return r;
}
// migrate 用来把旧格式的存档改写成现在的样子（读一次，之后就按新格式存）；
// 存档里没有的项（值是 undefined）不覆盖默认值
export function persistReactive(key, defaults, migrate) {
  const saved = store.get(key, {}) || {};
  const o = reactive({ ...defaults });
  for (const [k, v] of Object.entries(migrate ? migrate(saved) : saved)) {
    if (v !== undefined) o[k] = v;
  }
  watch(o, v => store.set(key, { ...v }));
  return o;
}

// 手机、平板这类没有悬停的设备：菜单和工具预览改成点一下展开
export const touch = matchMedia('(hover: none), (pointer: coarse)').matches;
// iPhone、iPad：Safari 长按不发右键事件（长按归系统选字），手机上弹菜单只能靠双击；
// 安卓长按会发右键事件，双击留给浏览器选字（见 menu.js 的 on）。新 iPad 的 Safari 报成 Mac，靠多点触控认出来
export const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

// 复制到剪贴板。局域网是 http，不是安全上下文，navigator.clipboard 根本拿不到，
// 所以一定要留着 execCommand 这条老路（它不挑上下文）
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* 没权限或不是安全上下文，走下面 */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, ta.value.length);   // iOS 上不加这句选不中
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

export const fileName = p => p.split('/').pop();
// 文件大小写给人看（和 server.size_text 一样的写法）
export const fileSize = n => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} 字节`);
// 看板地方小，模型名只留简称：claude-opus-5 → opus-5，末尾的日期版本号也去掉（完整名字在悬停说明里）
export const shortModel = m => (m || '').replace(/^claude-/, '').replace(/-\d{8}$/, '');
// 模型编号写成给人看的名字（气泡头部写是哪个模型回的）：claude-opus-5-5 → Opus 5.5，gpt-6.1-sol-fast → GPT-6.1 Sol 快速，
// 和模型下拉里的写法一致；[1m] 这类后缀、末尾的日期去掉。认不出的照原样写编号，不猜；
// 没有模型（CLI 自己写的 <synthetic> 提示）照旧写 Claude
// DeepSeek 空闲时段的调用，服务器给的模型名后面接着 @offpeak（按时段计价，见服务器 peak.py）。模型名里不写峰谷，
// 气泡上的「峰」「谷」写在花费左边、悬停才显示（item-row.js 的 peakMark）
export const OFFPEAK = '@offpeak';
export function modelName(m) {
  const id = (m || '').replace(OFFPEAK, '').replace(/\[.*\]$/, '');
  if (!id || id === '<synthetic>') return 'Claude';
  const cap = s => s[0].toUpperCase() + s.slice(1);
  let x = id.match(/^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/);
  if (x) return `${cap(x[1])} ${x[2]}${x[3] ? '.' + x[3] : ''}`;
  x = id.match(/^gpt-(\d+(?:\.\d+)?)-([a-z]+)(-fast)?$/);
  if (x) return `GPT-${x[1]} ${cap(x[2])}${x[3] ? ' 快速' : ''}`;
  x = id.match(/^deepseek-([a-z0-9.-]+)$/);
  if (x) return `DeepSeek ${x[1].split('-').map(cap).join(' ')}`;
  x = id.match(/^glm-([0-9.]+)(?:-([a-z]+))?$/);   // 智谱：glm-5.3 → GLM-5.3，glm-5.3-flash → GLM-5.3 Flash
  if (x) return `GLM-${x[1]}${x[2] ? ' ' + cap(x[2]) : ''}`;
  x = id.match(/^gemini-([0-9.]+)-([a-z-]+)$/);   // Gemini（经某个中转站接入）：gemini-4-argon → Gemini 4 Argon
  if (x) return `Gemini ${x[1]} ${x[2].split('-').map(cap).join(' ')}`;
  return id;
}
// 模型是哪一家的：gpt、deepseek、glm（智谱）、gemini，其余都算 claude（同服务器 instance.model_family）。号只能用自家的模型
export const modelFamily = m => (/^gpt-/.test(m || '') ? 'gpt' : /^deepseek-/.test(m || '') ? 'deepseek' : /^glm-/.test(m || '') ? 'glm'
  : /^gemini-/.test(m || '') ? 'gemini' : 'claude');
// 气泡头部的模型名用什么样式：每家各只有一个推荐模型，就是各自的默认模型（chat.js 模型下拉的第一项、instance.GPT_MODELS 的第一个）。
// Opus 5.5、DeepSeek Flash（这家只接了它）、Gemini 4 Argon（用户要用的三个模型之一）照常写；GPT-6 Astra 加粗；
// 其余的（含认不出的编号）都是不推荐的，黄色加粗；CLI 自己写的提示不是模型请求，不标
export function modelTier(m) {
  const id = (m || '').replace(OFFPEAK, '').replace(/\[.*\]$/, '');
  if (!id || id === '<synthetic>' || ['claude-opus-5-5', 'deepseek-flash', 'glm-5.3', 'gemini-4-argon'].includes(id)) return '';
  return id === 'gpt-6-astra' ? 'model-gpt' : 'model-warn';
}
// 记录里的时间：当天的只写时分秒，不是当天的前面写上日期（不是今年的再加年份），不然旧会话看着像今天发生的
const sameDay = (a, b) => a.toDateString() === b.toDateString();
const dateOf = d => `${d.getFullYear() === new Date().getFullYear() ? '' : d.getFullYear() + '年'}${d.getMonth() + 1}月${d.getDate()}日`;
const timeOf = d => d.toLocaleTimeString('zh-CN', { hour12: false });
export const clock = t => { const d = new Date(t); return sameDay(d, new Date()) ? timeOf(d) : `${dateOf(d)} ${timeOf(d)}`; };
// 一段起止时间：止和起是同一天就只写止的时分秒
export const clockSpan = (from, to) => {
  const a = new Date(from), b = new Date(to);
  return `${clock(from)}–${sameDay(a, b) ? timeOf(b) : clock(to)}`;
};
export const kilo = n => (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n));
// 不留小数的 k（底下那排的上下文小标签用）：一千以上一律写多少 k，一百万写 1000k
export const kiloInt = n => (n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n));
export const ago = s => {
  const m = Math.round((Date.now() / 1000 - s) / 60);
  return m < 1 ? '刚刚' : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.round(m / 60)} 小时前` : `${Math.round(m / 1440)} 天前`;
};
// 一段时长（秒）只写最大的一个单位（侧边栏的时间）：1y、2mo、3d、5h、12m，不到一分钟写 <1m；
// 一年按 365 天、一个月按 30 天，零头舍掉。侧边栏的「多久以前」和手机账号卡片的「多久后重置」共用
const SHORT_UNITS = [[31536000, 'y'], [2592000, 'mo'], [86400, 'd'], [3600, 'h'], [60, 'm']];
export const shortSpan = s => {
  for (const [n, u] of SHORT_UNITS) if (s >= n) return `${Math.floor(s / n)}${u}`;
  return '<1m';
};


// 同一件事一帧只做一次（滚动事件一帧能来好几个）
const frames = new Map();
export const frameOnce = fn =>
  frames.get(fn) || frames.set(fn, requestAnimationFrame(() => { frames.delete(fn); fn(); }));
