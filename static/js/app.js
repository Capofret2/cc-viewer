// 主程序：显示设置 + 把各块接起来。具体逻辑都在各自的模块里
const { createApp, ref, computed, watch, nextTick, provide } = Vue;
const { reactive } = Vue;
import { api, persistRef, persistReactive, touch, fileName, clock, kilo, kiloInt, ago, shortModel, modelName, modelFamily, say, fail, who, OFFPEAK } from './util.js';
import { ItemRow } from './item-row.js';
import { fitGlyphs } from './glyph.js';
import { useTabs, openOnServer, stays } from './tabs.js';
import { useMinimap } from './minimap.js';
import { useChat, MODE_TIPS } from './chat.js';
import { useSessions } from './sessions.js';
import { useMenu, ContextMenu } from './menu.js';
import { exportMarkdown } from './export.js';
import { useSearch } from './search.js';
import { TagPicker } from './tag-picker.js';
import { UiSelect } from './ui-select.js';
import { XxlbLive2d, XxlbCredit, XxlbThemes } from './xxlb.js';
import { AcctTag, acctNumWidth } from './acct-tag.js';
import { PALETTE, autoColor, tagStyle, tagHue } from './tag-color.js';
import { useQuota, QuotaBar, QuotaSegs } from './quota.js';
import { useReserve, longPress } from './reserve.js';
import { spentText, pointsVer, SpentPts, rateKey, yuanRate, callPoints, AVG_CALL, FEN, splitPop } from './points.js';
import { useUsers } from './users.js';
import { GrantPlan } from './plan.js';
import { useStats, LineChart, BarChart } from './stats.js';
import { useDevices } from './devices.js';
import { usePersonas } from './personas.js';
import { useTodos } from './todos.js';
import { useReviewBoard, ReviewBoard } from './review-board.js';
import { useVoice } from './voice.js';
import { useAvatar } from './avatar.js';
import { CtxViews, useCtxMap } from './ctx-ring.js';
import { useBack } from './back.js';

const THEMES = { green: '绿', blue: '蓝', purple: '紫', teal: '青', pink: '粉', amber: '橙' };
const filterLabels = { tool: '工具', system: '系统消息', attachment: '附件' };

// 侧边栏会话行往右划：露出主题色的底，左边写「置顶」或「取消置顶」（行上的 data-pin），划过 PIN_AT 松手就切换。
// 只认先横着往右走的：越过 10px 时竖着走得多、或者往左，就交给列表滚动和抽屉的往左划收起。横着拖起来以后给滚动区临时写
// overflow-y: hidden 挡住上下滚（同跟手翻页，不靠 preventDefault，监听都是被动的）；长按菜单的计时碰到 touchmove 自己取消。
// 行里的字和盖在上面的药丸由样式按 --pull 挪（style.css「往右划置顶」），松手放过渡弹回；拖过的那一下松手不算点开会话。
// 绑定的值是切换函数，后端不认置顶时给 null，不起手
const PIN_AT = 72;
// 侧边栏正在跟手拖或松手后正在滑（app.js 里 sd 不空）：这时按下去是接住侧边栏，往右拖也是拖侧边栏，行不起手置顶。setup 里换成真的
let sideBusy = () => false;
const pinSwipe = {
  mounted(el, binding) {
    el._pin = binding.value;
    let x0 = 0, y0 = 0, mode = '', armed = false, scroller = null, quiet = 0;
    const pull = v => el.style.setProperty('--pull', `${v}px`);
    el.addEventListener('touchstart', e => {
      mode = el._pin && e.touches.length === 1 && !sideBusy() ? 'wait' : '';
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
    }, { passive: true });
    el.addEventListener('touchmove', e => {
      if (!mode) return;
      const t = e.touches[0], mx = t.clientX - x0, my = t.clientY - y0;
      if (mode === 'wait') {
        if (Math.abs(mx) < 10 && Math.abs(my) < 10) return;
        if (mx <= 0 || Math.abs(my) > mx * 0.7) { mode = ''; return; }
        mode = 'drag';
        x0 = t.clientX;   // 从越过门槛那一刻起算，行不会一开始就跳一下
        scroller = el.closest('.side-scroll');
        if (scroller) scroller.style.overflowY = 'hidden';
        el.classList.remove('pin-back');
        el.classList.add('pulling');
      }
      const raw = Math.max(0, t.clientX - x0);
      armed = raw >= PIN_AT;
      el.classList.toggle('pin-armed', armed);
      pull(armed ? PIN_AT + (raw - PIN_AT) * 0.35 : raw);   // 过了门槛带阻尼
    }, { passive: true });
    const end = done => {
      if (mode !== 'drag') { mode = ''; return; }
      mode = '';
      quiet = Date.now();
      if (scroller) scroller.style.overflowY = '';
      el.classList.remove('pin-armed');
      el.classList.add('pin-back');
      pull(0);
      setTimeout(() => { if (!mode) el.classList.remove('pulling', 'pin-back'); }, 260);
      if (done && armed) el._pin?.();
      armed = false;
    };
    el.addEventListener('touchend', () => end(true), { passive: true });
    el.addEventListener('touchcancel', () => end(false), { passive: true });
    el.addEventListener('click', e => {
      if (Date.now() - quiet < 400) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, true);
  },
  updated(el, binding) { el._pin = binding.value; },
};

createApp({
  components: { ItemRow, ContextMenu, TagPicker, UiSelect, XxlbLive2d, XxlbCredit, XxlbThemes, AcctTag, SpentPts, QuotaBar, QuotaSegs, CtxViews, GrantPlan, LineChart, BarChart, ReviewBoard },
  setup() {
    // 设置全都记在这台设备的浏览器里，改了自动写回去
    // 思考默认不显示：CC 不把真正的思考写进记录，这些块基本是空的
    const show = persistReactive('show', { tool: true, system: true, attachment: false });
    const follow = persistRef('follow', true);
    // 主题色：只换强调色，工具调用的状态色不受影响
    const theme = persistRef('theme', 'green');
    watch(theme, v => { document.documentElement.dataset.theme = v; }, { immediate: true });
    // 性能：常驻 = 切换标签不销毁已经渲染好的内容（切换快，内存多）
    // 「屏幕外不跳过渲染」默认开：两个平台向上惯性滚动都会抽搐，所以默认不省这一下
    const perf = persistReactive('perf', { keepAlive: true, keepMax: 3, noSkip: true }, saved => ({
      keepAlive: saved.keepAlive,
      keepMax: saved.keepMax,
      // 兼容旧设置：以前存的是反过来的「跳过」
      noSkip: saved.noSkip ?? (saved.skipOffscreen === undefined ? undefined : !saved.skipOffscreen),
    }));
    const KEEP_CHOICES = { 1: '只留当前', 2: '2 个', 3: '3 个', 5: '5 个', 99: '不限' };
    // 常驻几个（含正在看的）：关了常驻就只留当前。没有标签页栏时（没开底部标签页）也按它在后台留着最近打开的几个会话
    const keepN = () => (perf.keepAlive ? perf.keepMax || 3 : 1);

    const { tabs, active, current, scroller, rows, setLog, load, connect, select, close, openTab, openSid,
            loadEarlier, loadLater, loadHead, loadTail, trim, stepTab, ensureLoaded, newFresh, promote, pushQuiet, pushEvery, pushLost,
            retrySync } = useTabs({ show, onSettings: () => sessions.loadSettings(),
                                    // 一次只开一个会话：没开底部标签页（sessions.keepTabs）。恢复标签页、换会话时用
                                    single: () => !sessions.keepTabs.value, keep: () => keepN() });

    // 常驻：最近看过的几个标签留着已经画好的内容，切过去很快；更早的切走就释放，省内存。
    // 留几个由用户在设置里定
    const seen = ref([]);
    watch(active, id => {
      if (id) seen.value = [id, ...seen.value.filter(x => x !== id)];
    }, { immediate: true });
    // 切标签的翻页动画（像手机桌面翻页）：往右边的标签切，新页面从右边整页滑进来、旧页面往左滑出去，反过来同理。
    // 旧页面要还留着（常驻）才有滑出；关掉标签后跳到邻居、系统要求减少动效时不做。时长和样式里的动画一致
    const SLIDE_MS = 280;
    const slide = reactive({ from: '', to: '', dir: 0 });
    let slideTimer = 0;
    let skipSlide = false;
    // 跟手翻页时标签页栏的高亮先跟着页面走（哪一页占了一大半就亮哪个），不等停稳 select（不然高亮很迟滞）；空着就是 active
    const pagerOn = ref('');
    watch(active, (id, old) => {
      const order = tabs.value.map(t => t.id), a = order.indexOf(old), b = order.indexOf(id);
      clearTimeout(slideTimer);
      // 跟手翻页已经把新页面拖到位了，不再放一遍翻页动画（见下面跟手翻页的 finish）；看板和控件那排（bar-in 只看 dir）照样按方向滑进来，
      // 和点标签页切换一样有这段动画（这里不能把 dir 也清了）
      if (skipSlide) {
        Object.assign(slide, { from: '', to: '', dir: b > a ? 1 : -1 });
        slideTimer = setTimeout(() => Object.assign(slide, { from: '', to: '', dir: 0 }), SLIDE_MS);
        return;
      }
      // 没有标签页栏时不做（没开底部标签页，sessions.keepTabs；换会话是从侧边栏挑，不是左右翻页）；
      // 开着底部标签页照旧做
      if (!old || !id || a < 0 || b < 0 || !sessions.keepTabs.value || matchMedia('(prefers-reduced-motion: reduce)').matches) {
        return Object.assign(slide, { from: '', to: '', dir: 0 });
      }
      Object.assign(slide, { from: old, to: id, dir: b > a ? 1 : -1 });
      slideTimer = setTimeout(() => Object.assign(slide, { from: '', to: '', dir: 0 }), SLIDE_MS);
    });
    // 正在看的标签页一换（点、滑、新开），标签页栏就横滚到把它整个露出来、两边留一点空；本来就看得见的不动
    // （不然手机上滑到右边的标签页时，它的标签已经不在可见区域了）。自己算着滚 .tabs，不用 scrollIntoView，免得把页面也带着滚
    watch(() => pagerOn.value || active.value, () => {
      const el = document.querySelector('.tabbar .tab.on'), box = el?.parentElement;
      if (!box) return;
      const r = el.getBoundingClientRect(), b = box.getBoundingClientRect(), pad = 16;
      const by = r.left < b.left + pad ? r.left - b.left - pad : r.right > b.right - pad ? r.right - b.right + pad : 0;
      const smooth = !matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (by) box.scrollBy({ left: by, behavior: smooth ? 'smooth' : 'auto' });
    }, { flush: 'post' });
    const keepShown = tab => {
      if (tab.id === slide.from) return true;   // 正在滑出去的那页，滑完再藏
      if (tab.id === active.value) return true;
      if (!perf.keepAlive) return false;
      const at = seen.value.indexOf(tab.id);
      return at >= 0 && at < keepN();
    };

    // 窄屏（和样式里的 max-width: 760px 同一条线）：可见范围那一格要不要搬到上看板，转屏、拖窗口时跟着变
    const narrowMq = matchMedia('(max-width: 760px)');
    const narrow = ref(narrowMq.matches);
    narrowMq.addEventListener('change', e => (narrow.value = e.matches));

    // 手机上在弹出的卡片（找话、改名、选文件夹……都在 .mask 里）里打字时，输入法把整页压矮，底下的对话、输入框、
    // 标签页栏也跟着往上挤。这时把主界面钉在键盘弹出前的高度（html.kb-freeze、--app-h），
    // 只让卡片随键盘上移；钉在屏幕底边的标签页栏按 --kb（键盘挡掉的高度）往下补回去。
    // 键盘收完、焦点也不在卡片里了才松开，焦点刚离开时键盘还在收，马上松开主界面会先被压一下再弹回来
    {
      const root = document.documentElement;
      let h0 = 0, w0 = 0;
      const inCard = () => !!document.activeElement?.closest?.('.mask');
      const release = () => {
        h0 = 0;
        root.classList.remove('kb-freeze');
        root.style.removeProperty('--app-h');
        root.style.removeProperty('--kb');
      };
      addEventListener('focusin', e => {
        if (h0 || !narrow.value || !e.target.closest?.('.mask') || !e.target.matches('input, textarea, [contenteditable]')) return;
        h0 = innerHeight; w0 = innerWidth;   // focusin 先于键盘弹出，这时量的还是完整高度
        root.style.setProperty('--app-h', `${h0}px`);
        root.classList.add('kb-freeze');
      });
      const settle = () => {
        if (!h0) return;
        if (innerWidth !== w0) return release();   // 转屏了，原来量的高度不再作数
        const kb = Math.max(0, h0 - innerHeight);
        root.style.setProperty('--kb', `${kb}px`);
        if (kb < 2 && !inCard()) release();
      };
      addEventListener('resize', settle);
      // 焦点离开卡片：键盘本来就没弹（外接键盘）或者已经收了，就不会再有 resize，在这里收尾
      addEventListener('focusout', () => setTimeout(settle));
    }

    // 手机上点输入框：输入法弹出时页面要等浏览器改了视口才跟着变矮，比输入法慢一拍、还是一下子跳上去的。
    // 改成一获得焦点就把输入框（连同底下那排小标签、回到顶部 / 底部两个按钮，停在最底下时连整段对话，最后几条跟着上移、不被盖住）
    // 用 transform 往上推，推多少按这台设备上次量到的输入法高度（localStorage，横竖屏分开记）；浏览器真的改了视口以后按实际高度校准
    // （还在动就从眼下的位置接着过去），推到位了再一次性把主界面的高度（--app-h，html.kb-lift）收矮、去掉 transform，画面不变。
    // 收起时反过来：先把高度放回去，再让这些东西从原来的高处落回；按返回键收起输入法（焦点还在）就等视口变高了再落。
    // 以前直接动画主界面的高度，每一帧整页重排，手机上不丝滑：现在动画期间只动 transform，开头、结尾各排一次，
    // 节奏和权限栏弹出卡片一样带一点回弹。第一次用还没记过高度，照旧等视口变了再动画过去。
    // 记下的高度只管起头：切成只有悬浮窗的语音输入时视口根本不变矮，推上去等不到 resize 就一直悬着。
    // 所以先推以后过 WAIT 视口还没变矮，就落回原处、把高度记成没量过；输入法开着时切到语音、再切回键盘，也都按视口实际变化跟着走
    {
      // 底部标签页栏不跟着上移：它钉在视口底边，视口变矮时用 --kb（视口实际矮了多少）往下补回去、躲在输入法后面；
      // 输入框底下给它让出的那截（--tabbar-h）也一起沉到输入法后面：往上推的量少推这么多（th），主界面多出来的被 body 按视口裁掉
      const root = document.documentElement, KEY = 'cc-kb-h', DUR = 420, WAIT = 1000, EASE = 'cubic-bezier(0.3, 1.18, 0.5, 1)';
      let h0 = 0, w0 = 0, th = 0, stick = false, shutting = false, lifted = 0, aim = 0, moving = false, anims = [], els = [], token = 0, waitT = 0;
      let fh = innerHeight, fw = innerWidth;   // 没在打字时最近一次的完整视口，焦点不动、输入法自己弹出来（从语音切回键盘）时拿它当 h0
      const known = () => +(JSON.parse(localStorage.getItem(KEY) || '{}')[innerWidth > innerHeight ? 'land' : 'port'] || 0);
      const remember = kb => {
        const all = JSON.parse(localStorage.getItem(KEY) || '{}');
        all[w0 > h0 ? 'land' : 'port'] = Math.round(kb);
        localStorage.setItem(KEY, JSON.stringify(all));
      };
      const inComposer = el => !!el?.closest?.('form.composer') && el.matches('textarea');
      const atEnd = () => { const el = scroller.value; return !!el && el.scrollHeight - el.scrollTop - el.clientHeight < 80; };
      // 要跟着动的几样：输入框、底下那排小标签、回到顶部 / 底部，停在最底下时还有整段对话
      const parts = () => {
        const log = scroller.value, wrap = log?.closest('.logwrap');
        if (!log) return [];
        return [...log.querySelectorAll(':scope > form.composer, :scope > .tools-slot'),
                ...(wrap ? wrap.querySelectorAll(':scope > .to-top, :scope > .to-bottom') : []),
                ...(stick ? [log.querySelector(':scope > .log-body')] : [])].filter(Boolean);
      };
      // 新会话页上不能整段推的几样（不然问候先跟着上去、键盘到位又往下瞬移，云等键盘到位才瞬移上去）：
      // 颜文字和问候在输入框上面那块空里上下居中，云按对话区的百分比贴着底摆，终点都跟着主界面的高度变，和输入框推的不是一个量
      const floaters = () => {
        const log = scroller.value, wrap = log?.closest('.logwrap');
        if (!log?.querySelector(':scope > .log-body > .fresh')) return [];
        return [...log.querySelectorAll(':scope > .log-body > .fresh :is(.kao, .fresh-hi)'),
                ...(wrap ? wrap.querySelectorAll(':scope > .fresh-clouds > i') : [])];
      };
      const offsetOf = e => new DOMMatrixReadOnly(getComputedStyle(e).transform).m42;
      const stop = () => { anims.forEach(a => a.cancel()); anims = []; moving = false; };
      // 从眼下的位置（各自此刻的 transform，或者统一给的 from）动画挪到比排版位置高 to 像素，挪完调 then
      const slide = (to, then, from) => {
        const t = ++token, start = els.map(e => from ?? offsetOf(e));
        stop();
        aim = to; moving = true;
        const dur = matchMedia('(prefers-reduced-motion: reduce)').matches ? 1 : DUR;
        anims = els.map((e, i) => e.animate([{ transform: `translateY(${start[i]}px)` }, { transform: `translateY(${-to}px)` }],
                                            { duration: dur, easing: EASE, fill: 'forwards' }));
        anims.push(...floatAnims(to, start, dur));
        Promise.all(anims.map(a => a.finished)).then(() => { if (t === token) { moving = false; then?.(); } }, () => {});
      };
      // 新会话页那几样跟着一起挪：同一帧里把主界面临时改成起点、终点那两个高度（输入框此刻推高了多少、要推到多少），量它们的自然位置再改回来，
      // 只排版不画，屏幕上看不出来；然后从起点位置挪到终点位置，曲线、时长和输入框一样。主界面真的改成终点高度（commit、落完交还）时它们正好在自然位置上。
      // 在 .log-body 里的（颜文字、问候）停在最底下时还跟着整段一起推，这一截要扣掉；云要缩放（宽高按百分比变），以左上角为原点
      function floatAnims(to, start, dur) {
        const fl = floaters();
        if (!fl.length) return [];
        const ci = els.findIndex(e => e.matches('form.composer')), lb = els.findIndex(e => e.classList.contains('log-body'));
        const was = ci < 0 ? 0 : -start[ci];
        const keep = root.style.getPropertyValue('--app-h');
        const base = fl.map(e => e.getBoundingClientRect());
        const at = h => { root.style.setProperty('--app-h', `${h}px`); return fl.map(e => e.getBoundingClientRect()); };
        const a = at(h0 - was), b = at(h0 - to);
        root.style.setProperty('--app-h', keep);
        return fl.map((e, i) => {
          const o = base[i], inLog = lb >= 0 && els[lb].contains(e);
          const key = (r, logOff) => (e.tagName === 'I'
            ? { transform: `translate(${r.left - o.left}px, ${r.top - o.top}px) scale(${r.width / o.width}, ${r.height / o.height})`, transformOrigin: '0 0' }
            : { transform: `translateY(${r.top - o.top - (inLog ? logOff : 0)}px)` });
          return e.animate([key(a[i], lb >= 0 ? start[lb] : 0), key(b[i], -to)], { duration: dur, easing: EASE, fill: 'forwards' });
        });
      }
      const release = () => {
        stop();
        clearTimeout(waitT);
        h0 = 0; shutting = false; lifted = 0; aim = 0; els = [];
        root.classList.remove('kb-lift');
        root.style.removeProperty('--app-h');
        root.style.removeProperty('--kb');
      };
      // 推到位、浏览器也改好了视口：主界面收矮这么多、去掉 transform、停在底部的滚到底，同一帧里做完，画面不变。
      // 视口还没变（输入法还没真弹出来）就先推着，等 resize
      const commit = () => {
        if (shutting || h0 - innerHeight < 80) return;
        lifted = aim;
        root.style.setProperty('--app-h', `${h0 - lifted}px`);
        stop();
        if (stick && scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
      };
      // 落回：已经收矮了就先把高度放回去（停在底部的滚到底），再让这几样从原来的高处落下来；还在往上推的半路就从眼下的位置落
      const finish = () => { if (innerHeight >= h0 - 2) release(); };   // 输入法也收完了才交还；还没收完等 resize
      const down = () => {
        if (!h0 || shutting) return;
        shutting = true;
        clearTimeout(waitT);
        const was = lifted;
        stick = atEnd();
        els = parts();
        if (!was) return slide(0, finish);
        root.style.setProperty('--app-h', `${h0}px`);
        lifted = 0;
        stop();
        if (stick && scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
        slide(0, finish, -was);
      };
      const begin = (h, w) => {
        h0 = h; w0 = w;
        th = parseFloat(getComputedStyle(root).getPropertyValue('--tabbar-h')) || 0;
        root.style.setProperty('--app-h', `${h0}px`);
        root.classList.add('kb-lift');
      };
      const lift = e => {
        if (!narrow.value || !inComposer(e.target) || root.classList.contains('kb-freeze')) return;
        if (h0 && !shutting) return;   // 已经推上去了
        if (!h0) begin(innerHeight, innerWidth);   // focusin 先于输入法弹出，这时量的还是完整高度
        shutting = false;
        stick = atEnd();
        els = parts();
        const kb = known();
        if (!kb) return;
        slide(Math.max(0, kb - th), commit);
        // 过了 WAIT 视口还没变矮：这回的输入法不占地方（悬浮的语音输入之类），落回去，记成没量过，下次点输入框不先推、等视口真变了再动画
        clearTimeout(waitT);
        waitT = setTimeout(() => {
          if (!h0 || shutting || lifted || h0 - innerHeight >= 80) return;
          remember(0);
          down();
        }, WAIT);
      };
      addEventListener('focusin', lift);
      // 按返回键收起输入法时焦点还留在输入框里，再点它不会有 focusin，输入法照样弹出来：这时按点完的那一下（click）算。
      // 不能按按下（pointerdown）算：手机上按下以后才补发 mousedown，这时界面已经开始往上收，mousedown 落到了输入框外面，
      // 焦点被拿走、又往下放，要点两下才能打字，界面还上下抽一下
      addEventListener('click', e => { if (!h0 && e.target === document.activeElement) lift(e); });
      addEventListener('focusout', () => setTimeout(() => {
        if (h0 && !inComposer(document.activeElement)) down();   // 焦点又回到输入框（点了发送之类）就不落
      }));
      addEventListener('resize', () => {
        if (!h0) {
          // 焦点一直在输入框里、输入法自己弹了出来（开着时切到语音，前面已经落回交还了，现在又切回键盘）：没有 focusin，按视口变矮接上
          if (narrow.value && inComposer(document.activeElement) && !root.classList.contains('kb-freeze')
              && innerWidth === fw && fh - innerHeight > 80) {
            begin(fh, fw);
            stick = atEnd();
            els = parts();
          } else {
            fh = innerHeight; fw = innerWidth;
            return;
          }
        }
        if (innerWidth !== w0) return release();   // 转屏了，原来量的高度不再作数
        const kb = h0 - innerHeight;
        root.style.setProperty('--kb', `${Math.max(0, kb)}px`);
        if (kb > 80 && !shutting) {   // 输入法弹出来了：记下高度，按实际校准
          clearTimeout(waitT);
          remember(kb);
          const L = Math.max(0, kb - th);
          if (lifted) {   // 已经收矮过、高度又变了（换了输入法）：直接按新的来
            if (Math.abs(L - lifted) >= 1) { lifted = aim = L; root.style.setProperty('--app-h', `${h0 - L}px`); }
          } else if (Math.abs(L - aim) >= 1) slide(L, commit);
          else if (!moving) commit();   // 猜的高度刚好对、早就推到位了，只差视口
          return;
        }
        if (kb < 2) shutting ? !moving && release() : down();   // 输入法收完了；焦点还在（按了返回键）就从这里开始落
      });
    }

    // 手机上往右划打开侧边栏（手不用去够左上角的两条横线）。
    // 默认一次只开一个会话，没有标签页可切；开了底部标签页的按起手位置分开，见下面。
    // 宽松一点：哪里起手都行、不用贴着屏幕边，方向偏水平（竖着走的不到横着走的七成）。窄屏上是跟手拉出来的（startSideDrag，见下面）；
    // 宽屏常驻的收着时划过 50px、0.8 秒内就算（swipeDir）。
    // 在输入框、弹出的卡片和下拉、本身能左右滚的地方（代码块、宽表格）上划不算，那些地方的手指要留给它们自己。
    // 开了底部标签页时往右划两头都要，按起手的位置分开：输入框顶边往下（输入框、底下那排小标签）
    // 左右划切标签页，上面和中间往右划打开侧边栏、往左划不管。标签页栏本身不算（它不在对话区里，触摸到不了这里），留给它自己横滚。
    // 这时输入框没在打字（没有焦点）也能起手，在打字时照旧留给移光标、选字
    let swipe = null;
    const NO_SWIPE = 'pre, table, .tool-wrap, textarea, input, .dd-panel, .ui-select-list, .plus-menu';
    const lowTabs = () => sessions.keepTabs.value;
    // 手指底下到 stop 为止，有没有能左右滚的
    function hScrolls(el, stop) {
      for (; el && el !== stop; el = el.parentElement) {
        if (el.scrollWidth > el.clientWidth + 1 && /auto|scroll/.test(getComputedStyle(el).overflowX)) return true;
      }
      return false;
    }
    const swipeStart = e => {
      const ta = e.target.closest('form.composer textarea');
      const free = !e.target.closest(NO_SWIPE) || (ta && ta !== document.activeElement && lowTabs());
      const y = e.touches[0]?.clientY;
      swipe = e.touches.length === 1 && free && !hScrolls(e.target, e.currentTarget)
        ? { x: e.touches[0].clientX, y, t: Date.now(), low: lowTabs() && lowZone(e.currentTarget, y) } : null;
    };
    // 起手在不在输入框顶边往下。量位置会逼浏览器先排版，所以只在开着底部标签页时量，别的时候每次摸屏幕都不多花这一下
    function lowZone(main, y) {
      const box = main.querySelector?.('form.composer');
      return !!box && y >= box.getBoundingClientRect().top;
    }
    // 返回这一下算不算宽松的横划：1 往右、-1 往左、0 不算
    function swipeDir(e) {
      if (!swipe) return 0;
      const t = e.changedTouches[0];
      const dx = t.clientX - swipe.x, dy = t.clientY - swipe.y;
      return Date.now() - swipe.t < 800 && Math.abs(dx) > 50 && Math.abs(dy) < Math.abs(dx) * 0.7 ? Math.sign(dx) : 0;
    }
    // 跟手翻页的弹簧还没停稳时手指按下：当场接住（见下面 grab）。pager 只在有标签页栏、正在翻页时才有，别的时候只多这一下判空
    const onTouchStart = e => {
      swipeStart(e);
      if (pager && !pager.drag && swipe && canDragTabs()) grab(e);
    };
    // 侧边栏（抽屉的时候）里往左拖收起，和往右拖拉出一样跟手（见下面 startSideDrag）：侧边栏跟着手指往回缩，
    // 松手按还露着多少定，过一半留着、不到一半收起，甩一下按甩的方向。在右边压暗的那条缝上起手也算；往右拖留给会话行的置顶（pinSwipe）。
    // 宽屏常驻的只有点两条横线才收。松手后还在滑的时候手指一按就当场接住（grabSide），往左往右都能接着拖
    const onSideTouchStart = e => {
      swipeStart(e);
      if (sd?.anim && swipe && e.touches.length === 1) grabSide(e);
    };
    // 接住那一下松手以后浏览器还会补一个点按，别让它落到压暗处（收起）或者会话行（打开会话）上
    let sideQuiet = 0;
    const onSideClickCapture = e => {
      if (Date.now() - sideQuiet < 400) { e.preventDefault(); e.stopPropagation(); }
    };
    const onSideTouchMove = e => {
      if (!swipe || swipe.noDrag || e.touches.length !== 1) return;
      const t = e.touches[0];
      if (sd?.drag) return sideMove(t.clientX);
      const dx = t.clientX - swipe.x, dy = t.clientY - swipe.y;
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
      if (dx >= 0 || Math.abs(dy) >= Math.abs(dx) * 0.7 || sd || sessions.sideWide.value || !sessions.sideOpen.value
          || !startSideDrag(e.currentTarget.querySelector('.side-scroll'), t.clientX, true)) swipe.noDrag = true;
    };
    const onSideTouchEnd = e => {
      if (sd?.drag) sideRelease(e.changedTouches[0].clientX);
      swipe = null;
    };
    const onSideTouchCancel = () => {
      if (sd?.drag) sideRelease(0, true);
      swipe = null;
    };
    // 跟手翻页（像手机桌面一样能中途接住、能连着快速翻）。
    // 能切标签页的地方（输入框往下那块）一横着拖，页面就跟着手指走，两边的标签页跟着露出来
    // （已经收掉的先拿「正在打开」那一页顶上）；第一个、最后一个再往外拉带阻尼。
    // 做法是一个小翻页器 pager：所有页面的位置由一个连续的数 p 定（起手时正在看的那页是 0，单位是一整页，往左拖变大），
    // 第 k 页（标签页序号 i0 + k）摆在 (k − p) 页宽的地方。松手后按甩的速度定停在哪一页（release），用临界阻尼的弹簧一帧帧推过去（settle）；
    // 弹簧没停稳时手指一按就当场接住、接着拖（grab），连着甩几下就一页页往后翻。只有停稳的那一页才真正 select 过去
    // （中途路过的只露脸，不切、不加载），翻完撤掉手写的样式（finish）。
    // 不卡的几样做法：
    // - 触摸移动、取消的监听是被动的，只在有标签页栏或者窄屏（要跟手拉侧边栏）时挂上（index.html 里 v-on="keepTabs || !sideWide ? dragOn : {}"），
    //   宽屏又没开底部标签页时一点不多花；
    //   拖起来以后挡住上下滚动靠给当前页临时写 overflow-y: hidden，不靠 preventDefault
    // - 露着的页面标成 will-change: transform，各自一层由合成器挪，不重绘；拖的时候位置一帧只写一次（requestAnimationFrame）
    // - 从越过 10px 门槛那一刻起算位移，页面不会一开始就跳一下
    const peeks = ref([]);   // 要露脸却没有画好内容的标签页编号，各拿一页「正在打开」顶上
    let pager = null;
    const SPRING = 0.02;     // 弹簧的角频率（每毫秒）：临界阻尼，两三百毫秒停稳
    const canDragTabs = () => sessions.keepTabs.value && !sessions.sideOpen.value && swipe.low;
    // 往右划拉出侧边栏是跟手的：横着往右拖起来以后，侧边栏从左边跟着手指露出来，拖多远露多少，压暗的那层跟着加深；
    // 松手按露出来的多少定：过了一半就打开，不到一半收回去；轻轻一甩（过 0.3px/ms）按甩的方向算，原来那样短短一划照样能打开。
    // 只在窄屏的抽屉上这样，宽屏常驻的往右划照旧直接打开。拖的时候靠 sideDrag 让侧边栏先露出来，松手定了打开才置上 sideOpen，
    // 收回去的那一下不算打开过，返回键的历史不多记一步。位置写在侧边栏那几层的行内样式上，盖过 side-in 的进场样式，一帧只写一次；
    // 拖的时候同跟手翻页一样给当前页临时写 overflow-y: hidden，页面不跟着上下滚。
    // 慢慢拖不发抖：侧边栏、压暗、阴影都标上 will-change 各占一层，由合成器挪、调透明度，
    // 不每帧重画（不标时拖 1.2 秒重画 144 次，tests/scratchpad/trace_side_drag.py 量的）；拖的时候不去取最近的会话，
    // 免得结果半路回来、重画列表卡几帧，松手打开时 openSide 再取；对话区是 touch-action: pan-y（style.css），横着起手浏览器不当成滚动接走。
    // 阴影是单独一层 .side-glow，透明度跟着露出的比例走：收着时侧边栏挪出屏幕，阴影不会在屏幕边上多留一条再一下子没了
    const sideDrag = ref(false);
    // 正在拖（drag）或松手后正在滑到位（anim）的这一下。滑的时候手指按在侧边栏、压暗处上就接住（不然收回的半路没法往右拉）；
    // 收起的滑到屏幕外就清空、整层放开触摸（pointer-events: none），手指马上落回对话区能再拉出来，不等 side-in 的退场放完
    let sd = null;
    let hiding = null, hideTimer = 0;   // 收起滑完、等退场放完再撤行内样式的那一下
    sideBusy = () => !!sd;
    const SIDE_MS = 260;   // 和 style.css 里 side-in 的时长一样
    const canDragSide = dx => dx > 0 && !sd && !sessions.sideWide.value && !sessions.sideOpen.value && !(lowTabs() && swipe.low);
    function sideReset(g) {
      if (g.side) Object.assign(g.side.style, { transition: '', transform: '', willChange: '' });
      for (const el of [g.shade, g.glow]) if (el) Object.assign(el.style, { transition: '', opacity: '', willChange: '' });
      if (g.wrap) g.wrap.style.pointerEvents = '';
    }
    function stopHiding() {
      clearTimeout(hideTimer);
      hideTimer = 0;
      hiding = null;
    }
    // 退场还没放完又点两条横线打开了：马上撤掉收着时的样式，照常放进场。
    // sessions 在下面才建好，watch 会当场读一次 getter，所以等 setup 跑完再挂（直接挂会让整个页面起不来）
    nextTick(() => watch(() => sessions.sideOpen.value, v => {
      if (v && hiding && !sd) { sideReset(hiding); stopHiding(); }
    }));
    // closing：在开着的侧边栏上往左拖收起。它本来就显示着，当场找到、量好宽度，从整张露着（off 取宽度）起算；
    // 拖的时候同样置上 sideDrag，松手定了收起、sideOpen 先落下，侧边栏也照样留着滑完再藏。返回假就是没找到侧边栏，这一下不拖
    function startSideDrag(page, x, closing) {
      if (!closing) sessions.sideView.value = 'main';
      stopHiding();   // 上一下的退场还没放完：样式归这一下管，不再撤
      sideDrag.value = true;
      // 起手越过门槛的这一点也记进速度历史：短短一甩可能只再来一两个触摸点，不记它就算不出速度、甩不动
      sd = { drag: true, closing, x0: x, page, off: closing ? Infinity : 0, w: 0, hist: [{ x, t: performance.now() }], frame: 0, side: null, shade: null, glow: null };
      if (closing) {
        sideRender();
        if (!sd.side) { sd = null; sideDrag.value = false; return false; }
        sd.off = sd.w;
      }
      if (page) page.style.overflowY = 'hidden';
      return true;
    }
    // 拉出来时侧边栏是 sideDrag 置上以后才显示的，第一帧再找它、量宽度
    function sideRender() {
      const g = sd;
      g.frame = 0;
      if (!g.side) {
        g.side = document.querySelector('.side-wrap > .side');
        g.shade = document.querySelector('.side-wrap > .side-shade');
        g.glow = g.side?.querySelector(':scope > .side-glow');
        if (!g.side || !g.shade || !g.glow) return void (g.side = null);
        g.wrap = g.side.parentElement;
        g.wrap.style.pointerEvents = '';
        g.w = g.side.offsetWidth || 1;
        Object.assign(g.side.style, { transition: 'none', willChange: 'transform' });
        for (const el of [g.shade, g.glow]) Object.assign(el.style, { transition: 'none', willChange: 'opacity' });
      }
      const off = Math.min(g.off, g.w);
      g.side.style.transform = `translateX(${off - g.w}px)`;
      g.shade.style.opacity = g.glow.style.opacity = off / g.w;
    }
    // 滑到半路被接住：停在这一帧的位置（读过渡算到哪儿了），撤掉过渡，这一下直接算拖，往哪边都跟（free），不用再过 10px 门槛
    function grabSide(e) {
      const g = sd, x = e.touches[0].clientX;
      clearTimeout(g.timer);
      g.anim = false;
      g.off = Math.max(0, Math.min(g.w, g.w + new DOMMatrix(getComputedStyle(g.side).transform).m41));
      for (const el of [g.side, g.shade, g.glow]) el.style.transition = 'none';
      sideRender();
      Object.assign(g, { drag: true, free: true, grabbed: true, x0: x, off0: g.off, hist: [{ x, t: performance.now() }],
                         page: e.currentTarget.querySelector('.side-scroll') });
      if (g.page) g.page.style.overflowY = 'hidden';
      sideDrag.value = true;
    }
    function sideMove(x) {
      const g = sd, now = performance.now();
      g.off = g.free ? Math.max(0, Math.min(g.w, g.off0 + x - g.x0))
        : g.closing ? Math.max(0, g.w + Math.min(0, x - g.x0)) : Math.max(0, x - g.x0);
      g.hist.push({ x, t: now });
      while (g.hist.length > 2 && now - g.hist[0].t > 100) g.hist.shift();
      if (!g.frame) g.frame = requestAnimationFrame(sideRender);
    }
    // 松手：定下打开还是收回，从手指停下的地方滑过去；cancel 是被系统收走的触摸，回到起手时的样子（拉出来的收回，收起的留着，接住的停到近的那头）
    function sideRelease(x, cancel) {
      const g = sd, now = performance.now();
      g.drag = false;
      if (g.page) g.page.style.overflowY = '';
      if (g.frame) cancelAnimationFrame(g.frame);
      sideRender();
      if (g.grabbed) sideQuiet = Date.now();
      const h = g.hist.find(q => now - q.t <= 80) || g.hist[g.hist.length - 1];
      const vx = h && now - h.t > 0 ? (x - h.x) / (now - h.t) : 0;
      const open = cancel && !g.free ? !!g.closing : !cancel && Math.abs(vx) > 0.3 ? vx > 0 : g.off > g.w / 2;
      // 从收着拉出来的才走 openSide（从会话那一页看起）；收到半路又拉回来的直接置回去，还停在原来那页
      if (open && !sessions.sideOpen.value) {
        if (g.free || g.closing) sessions.sideOpen.value = true;
        else sessions.openSide();
      }
      if (!open && sessions.sideOpen.value) sessions.sideOpen.value = false;
      if (!g.side) { sideDrag.value = false; sd = null; return sideReset(g); }
      const ms = matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : SIDE_MS;
      void g.side.offsetWidth;   // 先让手指停下的那个位置生效，下面换上过渡才从这里滑过去
      Object.assign(g.side.style, { transition: `transform ${ms}ms cubic-bezier(0.2, 0.7, 0.2, 1)`, transform: open ? 'translateX(0)' : 'translateX(-100%)' });
      for (const el of [g.shade, g.glow]) Object.assign(el.style, { transition: `opacity ${ms}ms ease`, opacity: open ? 1 : 0 });
      g.anim = true;
      g.timer = setTimeout(() => {
        g.anim = false;
        sideDrag.value = false;
        if (sd === g) sd = null;
        if (open) return sideReset(g);   // sideOpen 已经接上，直接撤样式
        // 收起的：滑到屏幕外就放开触摸；v-show 还要放一遍 side-in 的退场，放完、藏起来以后才撤样式，不然撤掉的那一帧会闪出整张侧边栏
        g.wrap.style.pointerEvents = 'none';
        hiding = g;
        hideTimer = setTimeout(() => { sideReset(g); stopHiding(); }, SIDE_MS + 40);
      }, ms);
    }
    function pageEl(k) {
      const tab = tabs.value[pager.i0 + k];
      if (!tab) return null;
      const el = pager.wrap.querySelector(`main.log[data-tab="${tab.id}"]`) || pager.wrap.querySelector(`.log-peek[data-peek="${tab.id}"]`);
      if (!el && !peeks.value.includes(tab.id)) peeks.value = [...peeks.value, tab.id];   // 下一帧才画出来，这一帧先空着
      return el;
    }
    // 按 p 摆好露着的一两页；离开视野的撤掉手写样式（起手那页一直是当前页，不撤，摆在屏幕外）。
    // 露着的邻居临时能接触摸（.off 本来不接），弹簧没停时按在它上面也接得住
    function render() {
      pager.frame = 0;
      const { p, w } = pager, show = new Map();
      for (let k = Math.floor(p); k <= Math.ceil(p); k++) {
        const el = k === 0 ? pager.cur : pageEl(k);
        if (el) show.set(el, k);
      }
      for (const el of pager.shown) {
        if (show.has(el)) continue;
        if (el === pager.cur) el.style.transform = `translateX(${-p * w}px)`;
        else Object.assign(el.style, { transform: '', visibility: '', willChange: '', pointerEvents: '' });
      }
      for (const [el, k] of show) {
        if (!pager.shown.has(el) && el !== pager.cur) Object.assign(el.style, { visibility: 'visible', willChange: 'transform', pointerEvents: 'auto' });
        el.style.transform = `translateX(${(k - p) * w}px)`;
        pager.touched.add(el);
      }
      pager.shown = new Set(show.keys());
      // 高亮跟着占了一大半屏幕的那页走，只在换页时写一次
      const near = tabs.value[pager.i0 + Math.round(p)];
      if (near && pagerOn.value !== near.id) pagerOn.value = near.id;
    }
    function startPager(cur) {
      const wrap = cur.closest('.logwrap');
      pager = { cur, wrap, w: wrap.clientWidth, i0: tabs.value.findIndex(t => t.id === active.value), p: 0,
                shown: new Set(), touched: new Set([cur]), frame: 0, anim: 0, drag: null };
      wrap.classList.add('dragging');
      Object.assign(cur.style, { willChange: 'transform', overflowY: 'hidden' });   // 拖着的时候页面不跟着上下滚
    }
    // 弹簧没停稳时手指按下：停在这一帧的位置，这一下直接算拖，不用再过 10px 门槛
    function grab(e) {
      cancelAnimationFrame(pager.anim);
      pager.anim = 0;
      pager.drag = { x0: e.touches[0].clientX, p0: pager.p, hist: [], base: pager.target };   // base：接住前正要停的那页
    }
    function dragTo(x) {
      const g = pager.drag, lo = -pager.i0, hi = tabs.value.length - 1 - pager.i0;
      let p = g.p0 - (x - g.x0) / pager.w;
      if (p < lo) p = lo - (lo - p) * 0.3;
      else if (p > hi) p = hi + (p - hi) * 0.3;
      pager.p = p;
      // 记下最近 100 毫秒的手指位置，松手时按这段算速度
      const now = performance.now();
      g.hist.push({ x, t: now });
      while (g.hist.length > 2 && now - g.hist[0].t > 100) g.hist.shift();
      if (!pager.frame) pager.frame = requestAnimationFrame(render);
    }
    const onTouchMove = e => {
      if (!swipe || swipe.noDrag || e.touches.length !== 1) return;
      const t = e.touches[0];
      if (sd?.drag) return sideMove(t.clientX);
      if (!pager?.drag) {
        const dx = t.clientX - swipe.x, dy = t.clientY - swipe.y;
        if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
        if (Math.abs(dy) >= Math.abs(dx) * 0.7) { swipe.noDrag = true; return; }
        // 窄屏上往右拖、这里不切标签页：拉出侧边栏，从越过门槛那一刻起算
        if (canDragSide(dx)) { startSideDrag(e.currentTarget, t.clientX); return; }
        // 竖着走的（上面）、这里不能切标签页的（还在翻却没接住的也是）：这一下不拖（松手时照旧看是不是往下拉）
        if (pager || !canDragTabs()) { swipe.noDrag = true; return; }
        startPager(e.currentTarget);
        pager.drag = { x0: t.clientX, p0: 0, hist: [] };
      }
      dragTo(t.clientX);
    };
    // 松手：按松手前约 80 毫秒的速度定停在哪一页（不然要划太长、常常划短了页面左右抽一下）。
    // 甩过 0.3px/ms（轻轻一甩）、这一下拖过 15px：停到甩的方向上的下一页；慢慢拖着走的：朝走的方向越过六分之一页就停到前面那页，否则退回；
    // 松手时往回拉（反方向过 0.1px/ms）的同样按拉的方向算，所以拖得再远也能反悔；手指停着不动的按这一下拖的方向算
    function release(x) {
      const g = pager.drag, now = performance.now();
      pager.drag = null;
      if (pager.frame) { cancelAnimationFrame(pager.frame); pager.frame = 0; }
      const h = g.hist.find(q => now - q.t <= 80) || g.hist[g.hist.length - 1];
      const vx = h && now - h.t > 0 ? (x - h.x) / (now - h.t) : 0;   // 手指往右为正（px/ms）
      const p = pager.p, f = Math.floor(p), c = Math.ceil(p);
      const fwd = Math.abs(vx) > 0.1 ? vx < 0 : p - g.p0 > 0 ? true : p - g.p0 < 0 ? false : null;
      let k;
      // 接住正在翻的再甩：从正要停的那页再往前一页（连着甩两下就是翻两页），不然停在半路上甩一下还落回刚才那页
      if (Math.abs(vx) > 0.3 && Math.abs(x - g.x0) > 15) {
        k = vx < 0 ? f + 1 : c - 1;
        if (g.base != null) k = vx < 0 ? Math.max(k, g.base + 1) : Math.min(k, g.base - 1);
      }
      else if (fwd === true) k = p - f > 1 / 6 ? f + 1 : f;
      else if (fwd === false) k = c - p > 1 / 6 ? c - 1 : c;
      else k = Math.round(p);
      k = Math.max(-pager.i0, Math.min(tabs.value.length - 1 - pager.i0, k));
      settle(k, -vx / pager.w);
    }
    // 临界阻尼弹簧：x(t) = (x0 + (v0 + ω·x0)·t)·e^(−ωt)，接着手指的速度推过去，不过冲来回晃
    function settle(k, v0) {
      const g = pager, x0 = g.p - k, t0 = performance.now(), w = SPRING;
      g.target = k;
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) return finish(k);
      const step = now => {
        const t = now - t0, e = Math.exp(-w * t);
        const x = (x0 + (v0 + w * x0) * t) * e, v = (v0 - w * (v0 + w * x0) * t) * e;
        if (t > 800 || (Math.abs(x) * g.w < 0.5 && Math.abs(v) * g.w < 0.05)) return finish(k);
        g.p = k + x;
        render();
        g.anim = requestAnimationFrame(step);
      };
      g.anim = requestAnimationFrame(step);
    }
    async function finish(k) {
      const g = pager;
      g.anim = 0;
      g.p = k;
      render();
      const tab = tabs.value[g.i0 + k];
      if (k && tab) { skipSlide = true; select(tab); }
      pagerOn.value = '';
      await nextTick();
      skipSlide = false;
      for (const el of g.touched) Object.assign(el.style, { transform: '', visibility: '', willChange: '', overflowY: '', pointerEvents: '' });
      peeks.value = [];
      g.wrap.classList.remove('dragging');
      if (pager === g) pager = null;
    }
    const onTouchCancel = () => {
      if (sd?.drag) sideRelease(0, true);
      if (pager?.drag) {
        pager.drag.hist = [];   // 被系统收走的触摸不算甩
        release(pager.drag.x0);
      }
      swipe = null;
    };
    const onTouchEnd = e => {
      if (!swipe) return;
      if (sd?.drag) {
        swipe = null;
        return sideRelease(e.changedTouches[0].clientX);
      }
      if (pager?.drag) {
        swipe = null;
        return release(e.changedTouches[0].clientX);
      }
      // 宽屏常驻的收着时往右划直接打开；窄屏的抽屉由上面跟手拉出来，这里不管
      const tabZone = lowTabs() && swipe.low;   // 开了底部标签页、在输入框往下起的手：只切标签页
      if (sessions.sideWide.value && !tabZone && !sessions.sideOpen.value && swipeDir(e) > 0) {
        swipe = null;
        return sessions.openSide();
      }
      const t = e.changedTouches[0];
      const dx = t.clientX - swipe.x, dy = t.clientY - swipe.y;
      swipe = null;
      // 切标签页全由上面的跟手翻页管（没有标签页栏时后台留着的几个看不见，canDragTabs 不让拖）；
      // 在顶上往下拉：页面滚不动时没有滚动事件，靠这一下去补更早的；手上这批底下卸掉过时，在底下往上推同样去接后面的
      const el = e.currentTarget;
      if (el.classList.contains('todo-page')) return;   // 待办页借这套手势只为往右划拉出侧边栏，上下拉不去补对话
      if (dy > 60 && Math.abs(dx) < dy && el.scrollTop < 600) minimap.fetchEarlier();
      else if (dy < -60 && Math.abs(dx) < -dy && el.scrollHeight - el.scrollTop - el.clientHeight < 600) minimap.fetchLater();
    };
    // 折叠刻度盘：只留用户说的话那几格刻度，默认开着
    const mmMine = persistRef('mmMine', true);
    // 用户说的话要不要按 Markdown 和公式渲染：默认不渲染。语音输入常带出半角波浪号这类符号，
    // 两个 ~ 一配对中间整段就被划掉了；关着时按原样显示纯文字
    const mdUser = persistRef('mdUser', false);
    provide('mdUser', mdUser);
    // 全屏：手机上收起顶上的时间电量栏和底下的导航栏，网页占满整个屏幕。
    // 开关记在这台设备上，开一次就一直算数：浏览器只认用户亲手点的那一下，没法一打开页面就全屏，
    // 所以刷新以后、按返回键退出以后，下一次点页面任何地方就顺手回到全屏，不用再进设置。关掉开关才真的不全屏。
    // 苹果手机的网页没有这个接口（fullscreenEnabled 为假），不出这一项
    // 装到桌面的应用本身就是全屏打开的（「桌面应用全屏打开」，server.py 的 /manifest.json）：不再用网页的全屏接口、也不出这一项，
    // 不然又弹提示气泡、返回键又先被拿去退出全屏
    const canFull = !!document.fullscreenEnabled && !matchMedia('(display-mode: fullscreen)').matches;
    const full = persistRef('fullscreen', false);
    const enterFull = () => document.documentElement.requestFullscreen({ navigationUI: 'hide' })
      .catch(e => say(`进入全屏失败：${e.message || e}`, { err: true }));
    const setFull = on => {
      full.value = on;
      if (on && !document.fullscreenElement) enterFull();
      else if (!on && document.fullscreenElement) {
        document.exitFullscreen().catch(e => say(`退出全屏失败：${e.message || e}`, { err: true }));
      }
    };
    // 捕获阶段先接住每一次点击，点的东西照常响应；点开关本身不算（交给 setFull，免得关的那一下又把全屏打开）。
    // 在会话区按返回退出的也一样，点一下就回去
    if (canFull) document.addEventListener('click', e => {
      if (full.value && !document.fullscreenElement && !e.target.closest?.('.full-switch')) enterFull();
    }, true);
    const minimap = useMinimap({ scroller, rows, current, follow, loadEarlier, loadLater, loadHead, loadTail, trim, mine: mmMine });
    // 只读模式（服务器默认）：没有输入框，也不去问 Claude 要任何东西。
    // 拿到服务器配置之前先按只读算，宁可输入框晚一点出现，也不先去连 Claude
    const readonly = ref(true);
    const chat = useChat({ current, stickToBottom: minimap.stickToBottom, readonly, pushQuiet, pushEvery,
                           createFrom: (tab, text, images) => sessions.createFromTab(tab, text, images), gptTab: tab => isGpt(acctOf(tab)),
                           familyTab: tab => familyOf(acctOf(tab)), relayTab: tab => relayOf(acctOf(tab)),
                           // 状态那行的主语：建好的会话用服务器简报里的卡名，新会话标签页还在建时用下拉里选中的那张
                           personaName: tab => (tab.fresh
                             ? personas.pc.cards.find(c => c.id === sessions.freshPersona(tab))?.name : tab.persona) || '' });
    // 人设卡（personas.js）：新建会话时交给服务器的是界面上选中的那张
    const personas = usePersonas();
    const sessions = useSessions({ tabs, current, select, openTab, readonly, newFresh, promote, personaOf: personas.personaOf,
                                   rememberPersona: personas.rememberPersona, confirmPersona: personas.confirmPersona });
    // 新会话标签页没点过号时选中的号：服务器按设置挑好的（独立环境另挑一个），挑不出来是空
    const fallbackAccount = (tab = current.value) => sessions.pickedAccount(tab?.environment);
    // 标签页用的号；是不是 GPT 号（经本机翻译层用 ChatGPT 订阅，模型只能选 GPT 的，见服务器 instance.py）
    const acctOf = tab => tab.account || (tab.fresh ? fallbackAccount(tab) : '');
    const isGpt = name => !!sessions.accounts.value.find(a => a.name === name)?.gpt;
    // 号是哪一家的（claude / gpt / deepseek，服务器 /api/config 的 family）；旧后端不给就按 gpt 分两家
    const familyOf = name => { const a = sessions.accounts.value.find(x => x.name === name); return a?.family || (a?.gpt ? 'gpt' : 'claude'); };
    // 中转站号（转 Claude 模型的平台，服务器 instance.RELAY）能选的模型：[{value, label}]，不是中转站是 null。
    const relayOf = name => sessions.accounts.value.find(x => x.name === name)?.relay?.models || null;
    // 这个标签页此刻用哪档思考强度：自己选过的（set），没选过就是所用账号在 Claude Code 设置里写的那档
    // （服务器随配置给，见 server.settings_effort）：先按会话实际用的模型（没有就是设置里的默认模型，去掉 [1m] 这类后缀）
    // 查按模型写的那档，没有再用不分模型的；都没写是 null，界面上照实说，不写含糊的「默认」
    // 模型也一样照实写：新会话标签页没选模型时，CLI 用的是所用账号 Claude Code 设置里的默认模型，直接写它，不写「默认模型」
    // 模型分 Claude 和 GPT 两家，号只能用自家的：选的（或会话最近用的）和号不是一家的，这个号上按它自己的默认模型跑
    // （服务器 run_model），这里也照实写那个默认模型
    // CLI 读的是哪份 Claude Code 设置：独立环境的 CLI 用那个环境自己的配置目录，不是号的（服务器 settings_effort），
    // 已有的会话服务器随快照给（settingsEffort），新会话标签页用 /api/environments 给的；旧后端不给这两个字段时照旧用号的
    const settingsOf = tab => {
      const name = acctOf(tab), acct = sessions.accounts.value.find(a => a.name === name)?.effort || {};
      const own = tab.fresh ? (tab.environment === 'private' ? sessions.environments.effort : null) : tab.settingsEffort;
      if (!own) return acct;
      return familyOf(name) !== 'claude' ? { ...own, model: acct.model } : own;   // GPT 号、DeepSeek 号的默认模型是启动时定的，压过设置里写的
    };
    // 设置里写的别名（比如 opus[1m]）换成下拉里那一项的全名、留着后缀：小标签写「Opus 5.5」而不是「Opus」，面板也勾得上那一项
    // （CLI 把别名解析成最新的那个：设置写 opus[1m] 的会话回复上记的都是 claude-opus-5-5）
    const fullModel = m => {
      const a = (m || '').match(/^([a-z]+)(\[.*\])?$/), hit = a && chat.claudeModels.find(x => x.value.includes(`-${a[1]}-`));
      return hit ? hit.value + (a[2] || '') : m;
    };
    // 设置里也没写默认模型的（独立环境没有设置文件），Claude 号上服务器拉起 CLI 带实例的默认模型（defaultModel，server.launch_model），
    // 照它写（dflt），不写「模型未设」，也不说「来自 CC 默认设置」；旧后端不给就照旧
    const modelOf = tab => {
      const name = acctOf(tab), fam = familyOf(name), relay = relayOf(name), same = m => !!m &&
        (relay ? relay.some(x => x.value === m) : modelFamily(m) === fam);
      if (same(tab.webModel)) return { model: tab.webModel, set: true };
      if (!tab.fresh && same(tab.model)) return { model: tab.model, set: false };
      const own = settingsOf(tab).model, dflt = fam === 'claude' && sessions.defaultModel.value;
      if (own || !dflt) return { model: fullModel(own) || '', set: false };
      return { model: dflt, set: false, dflt: true };
    };
    // pending：定下的这档还没有请求用上过（服务器比对最近一条回复记的实际强度，见 server.Tab.sync_effort），
    // 界面上灰着，并写明这一轮还按哪档（used）、什么时候改用新的。
    // 没选过的，有回复就照最近一条回复记的实际强度写（actual），设置是推测、回复上记的才是 CLI 真按的那档；还没有回复才按设置推。
    // 设置里的默认模型常写成别名（opus），按模型写的键是全名（claude-opus-5-5），别名去对全名里的那一段（同服务器 settings_level）。
    // 设置里也没给这个模型写的，服务器拉起 CLI 时带实例的默认档（defaultEffort，server.launch_effort），照它写（dflt），不说「来自 CC 默认设置」
    const effortOf = tab => {
      const chosen = tab.fresh ? tab.webEffort : tab.effort;
      if (chosen) return { level: chosen, set: true, pending: !tab.fresh && !!tab.effortPending, used: tab.usedEffort };
      if (!tab.fresh && tab.usedEffort) return { level: tab.usedEffort, set: false, actual: true };
      const s = settingsOf(tab), by = s.byModel || {};
      const model = (tab.webModel || (!tab.fresh && tab.model) || s.model || '').replace(/\[.*\]$/, '');
      const hit = by[model] || (/^[a-z]+$/.test(model) && Object.entries(by).find(([k]) => `-${k}-`.includes(`-${model}-`))?.[1]);
      const level = hit || s.level || null, dflt = sessions.defaultEffort.value;
      return level || !dflt ? { level, set: false } : { level: dflt, set: false, dflt: true };
    };
    const quota = useQuota({ current, readonly, fallback: fallbackAccount });
    const people = useUsers({ current, openTab, view: sessions.ovView, promoTag: quota.promoTag });   // 身份、登录注册、账本、可见范围
    // 账号页上的余额曲线、我的号、用量小图和用量统计页（stats.js）
    const stats = useStats({ accounts: sessions.accounts, quota, sideWide: sessions.sideWide });
    // 每次打开账号页取一次（各自一分钟或半分钟内不重取）；额度要现取，我的号的血条照它画
    watch(() => sessions.sideOpen.value && sessions.sideView.value === 'me' && !!people.me.name, v => {
      if (!v) return;
      stats.loadBalance();
      stats.loadOwned();
      stats.loadMini();
      quota.loadQuota();
    });
    // 预定额度的面板（账号表里右键、手机上长按某个号）
    const reserve = useReserve({ loadQuota: quota.loadQuota, loadMe: people.loadMe });
    // 设备页（devices.js）：哪里解决不了就开个新对话，把问题写进输入框，用户看一眼再发
    function askClaude(text) {
      if (readonly.value) return say('只读模式不能和 Claude 对话', { err: true });
      const tab = sessions.openFresh({ blank: true });
      if (!tab) return;
      tab.draft = text;
      tab.todoIds = null;   // 这个标签页以前从待办页写过话又清空了：这次不是从待办开出
      nextTick(chat.autoGrow);
      say('问题写进新对话的输入框了，看一眼没问题就发送', { ms: 4000 });
    }
    const devices = useDevices({ askClaude, meReady: people.meReady });
    // 待办页（todos.js）：盖住对话区；换到别的会话（侧边栏里挑、搜索、从待办页点会话、发到新会话）就收起
    const todos = useTodos({ openTab, scroller, openFresh: o => sessions.openFresh(o), afterDraft: () => nextTick(chat.autoGrow) });
    watch(active, () => todos.closeTodos());
    // 审稿看板（review-board.js）：自动审稿的会话才有入口，看的是正在看的会话，换到别的会话就收起
    const review = useReviewBoard();
    watch(active, () => review.closeReview());
    // 讯飞语音输入（voice.js）：配过的人输入框里多一个波形按钮，识别出的字写进开口时那个标签页的草稿
    const voice = useVoice({ current, composerInput: chat.composerInput, autoGrow: chat.autoGrow, globalSettings: sessions.globalSettings });
    // 侧边栏的账号页顶上直接列出自己的设备：每次打开账号页取一次设备列表
    watch(() => sessions.sideOpen.value && sessions.sideView.value === 'me' && people.me.devices,
          v => { if (v) devices.loadDevices(); });
    // 页面打开就取一次：新会话标签页的 Remotes 小标签要按它数、按它勾，一台设备都没有时的小红点（devDot）也要靠它
    people.meReady.then(() => people.me.devices && devices.loadDevices());
    // 下看板权限小标签地方紧：bypassPermissions 只写 bypass，面板里照旧写全
    // 小标签那一行的写法：权限、思考强度首字母大写、驼峰拆成词（bypass → Bypass，dontAsk → Don't Ask），
    // 模型写正式名字（opus-5-5 → Opus 5.5，同气泡头部的 modelName）；只改这一行，点开的卡片里照旧
    const MODE_LABEL = { bypassPermissions: 'Bypass', auto: 'Auto', dontAsk: "Don't Ask", acceptEdits: 'Accept Edits', plan: 'Plan', default: 'Default' };
    const modeShort = m => MODE_LABEL[m] || m;
    const EFFORT_LABEL = { low: 'Low', medium: 'Medium', high: 'High', xhigh: 'XHigh', max: 'Max' };
    const effortLabel = e => EFFORT_LABEL[e] || e;
    // 设置里写的别名（opus、sonnet）modelName 认不出、原样返回，这里补个首字母大写；别的认不出的编号照原样写
    const chipModel = m => { const n = modelName(m); return /^[a-z]+$/.test(n) ? n[0].toUpperCase() + n.slice(1) : n; };
    // 所有前端账号列表共用：显示本周订完的整组放最后，按界面上写的重置时刻排；不知道时刻的放这组末尾。
    // 其余号仍按自动挑号的规矩排，不改服务器挑号，也不改用户已选的号。
    const sortedAccounts = computed(() => {
      const keys = Object.fromEntries(sessions.accounts.value.map(a => {
        const q = quota.quotaCells(a.name);
        return [a.name, q.weekOut ? [1, q.week?.at || Infinity]
          : [0, ...quota.rankKey(a.name, !!people.paidBlock(a, current.value, quota.hasBlue(a.name)), !!a.api)]];
      }));
      return sessions.accounts.value.filter(a => !a.hidden).sort((x, y) => quota.byRank(keys[x.name], keys[y.name]));
    });
    // 账号面板先列此刻能选的号；被封、已搬走或当前身份不能用的限量号只在横线下说明原因。
    // 余额透支了，但在这个号上还有预定的蓝，照样能用（用蓝不扣钱）。
    // 留给独立环境的集中登录号（后端的 private_only）共用环境的面板里不列（只改显示）；
    // 标了 shared 的集中登录号两边都列。旧后端不给 private_only，就照旧把独立环境支持表里除 GPT 以外的号都当专用
    const accountGroups = computed(() => {
      const available = [], unavailable = [];
      const envs = sessions.environments;
      const privateOnly = a => envs.private_only ? envs.private_only.includes(a.name) : !a.gpt && envs.accounts.includes(a.name);
      for (const a of sortedAccounts.value) {
        if (current.value?.environment === 'private' ? !sessions.environments.accounts.includes(a.name) : privateOnly(a)) continue;
        // 被封、搬走的号选不了；此刻付不起的限量号（余额是 0、游客）照样能选（选号不花钱，
        // 发话时服务器 check_paid 才拦），payWhy 写为什么现在用不了，选中它时表底下一行红字说出来
        if (a.banned) unavailable.push({ ...a, why: a.note || '不可用' });
        else available.push({ ...a, why: '', payWhy: people.paidBlock(a, current.value, quota.hasBlue(a.name)) || '' });
      }
      return { available, unavailable };
    });
    // 设置页「新会话默认账号」能选的号：被封、订阅到期的不列；共用的 Claude 订阅号（不是 API 号）只有号主能固定，
    // 别人固定它没有意义，「自动」会挑此刻最宽裕的。已经设了的照旧列着（选择是用户的数据，不悄悄换掉），服务器 set_settings 同一个规矩
    const defaultChoices = computed(() => sortedAccounts.value.filter(a => {
      if (a.name === sessions.globalSettings.mine) return true;
      const exp = quota.quota.expires[a.name];
      if (a.banned || (exp && exp * 1000 < Date.now())) return false;
      return familyOf(a.name) !== 'claude' || !!a.api || (people.me.owns || []).includes(a.name);
    }));
    // 用不了的号（被封、搬走的墓碑）默认收成一行「另有 N 个号用不了」，点了才展开（多的时候十几个，
    // 原来一行行全列着，账号卡片一限高，被挤得只剩一行的反而是能选的号）。看板账号卡片和新会话账号表共用这一个开关
    const deadOpen = ref(false);
    // 本周用完、等重置的号（weekOut，sortedAccounts 排在最后）只露靠上三个，其余收成一行「另有 N 个号待重置」，点了才展开。
    // 选着的号照样露着，不收进去。返回收起来的号名
    const waitOpen = ref(false);
    const waitFolded = (rows, keep) => rows.filter(a => quota.quotaCells(a.name).weekOut).slice(3).map(a => a.name).filter(n => n !== keep);
    // 价目表（账号卡片底栏「刷新额度」左边）：只列此刻能选的号用得上的模型，每家只列默认模型（同 util.modelTier 认的推荐模型）。
    // 号自己有一套价的（ccapi1 这类中转号）单独一行、名字后面挂号；按时段计价的（DeepSeek、智谱）高峰、非高峰各一行，此刻适用的那行标「现在」。
    // 数全从前端拿到的单价现算（和气泡、账本同一份），单价重新拟合后跟着变；「是 Opus 的几倍」以订阅号的 Opus 5.5 为准，这里没有 Claude 号也照样比。
    // 行的先后固定按家排，不跟着账号表的额度排序跳
    const PRICE_MODEL = { claude: 'claude-opus-5-5', gpt: 'gpt-6-astra', deepseek: 'deepseek-flash', glm: 'glm-5.3' };
    const OFF_LABEL = { deepseek: '空闲', glm: '空闲' };   // 智谱官方叫非高峰，表里和 DeepSeek 统一写空闲
    const PRICE_ORDER = ['claude', 'own', 'gpt', 'deepseek', 'glm'];
    const priceList = reactive({ open: false });
    const priceRows = computed(() => {
      const yuanOf = (model, account) => { const p = callPoints(AVG_CALL, model, account); return p == null ? null : p * FEN / 100; };
      const base = yuanOf(PRICE_MODEL.claude);
      const rows = [], seen = new Set();
      // 中转站一个号可以接好几个模型（某个中转站的 Opus 5.5 和 Gemini 4 Argon），每个模型各一行
      for (const a of accountGroups.value.available) for (const relayModel of relayOf(a.name)?.map(m => m.value) || [null]) {
        const fam = familyOf(a.name), model = relayModel || PRICE_MODEL[fam] || PRICE_MODEL.claude;
        const key = rateKey(model, a.name);
        if (!key || seen.has(key)) continue;   // 没有单价的号本来就用不了；同一条价的几个号并成一行
        seen.add(key);
        const own = key.includes('@account=') ? a.name : '';
        const pk = quota.quotaCells(a.name).peak;
        const split = OFF_LABEL[fam] && rateKey(model + OFFPEAK) === model + OFFPEAK;
        for (const [m, peak] of split ? [[model, '高峰'], [model + OFFPEAK, OFF_LABEL[fam]]] : [[model, '']]) {
          const call = yuanOf(m, own || undefined);
          rows.push({ key: key + peak, name: modelName(m) + (peak ? ` ${peak}` : ''), account: own,
                      rank: PRICE_ORDER.indexOf(own ? 'own' : fam), fam, r: yuanRate(m, own || undefined), call,
                      times: base && call != null ? call / base : null, now: !!(peak && pk && (peak !== '高峰') === pk.off) });
        }
      }
      return rows.sort((x, y) => (x.rank < 0 ? 99 : x.rank) - (y.rank < 0 ? 99 : y.rank));
    });
    // 表里一律写两位小数，悬停（手机上点一下）那一格立刻弹出完整的数：
    // 完整的数取 6 位有效数字，单价乘出来的浮点尾巴（1.1602800000000001）不算精度
    const priceNum = v => v.toFixed(2);
    const priceFull = v => String(Number(v.toPrecision(6)));
    // 收进去、放出来的那几行：挂在每行的 <transition> 上，高度连上下内边距一起从 0 长出来、缩回 0（只收高度最矮只能矮到内边距之和，
    // 同 chat.js 的 cardHidden），字跟着淡入淡出。半路又点了，从眼下的样子接着过去
    const ROW_GONE = { boxSizing: 'border-box', height: '0px', paddingTop: '0px', paddingBottom: '0px', opacity: 0 };
    const rowNow = el => {
      const cs = getComputedStyle(el);
      return { boxSizing: 'border-box', height: `${el.offsetHeight}px`, paddingTop: cs.paddingTop, paddingBottom: cs.paddingBottom, opacity: cs.opacity };
    };
    function rowFold(el, done, open) {
      const from = el._fold?.playState === 'running' || !open ? rowNow(el) : ROW_GONE;
      el._fold?.cancel();
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) return done();
      const to = open ? rowNow(el) : ROW_GONE;
      el.style.overflow = 'hidden';   // 矮下去时里面的字不漏到别的行上
      el._fold = el.animate([from, to], { duration: 220, easing: 'cubic-bezier(0.2, 0.9, 0.3, 1)', fill: open ? 'none' : 'forwards' });
      el._fold.onfinish = () => { el.style.overflow = ''; done(); };
    }
    const rowIn = (el, done) => rowFold(el, done, true);
    const rowOut = (el, done) => rowFold(el, done, false);
    // 新会话标签页上直接点账号卡片选号。一开始选中的是服务器挑好的号（fallbackAccount），点哪张就换成哪个
    function pickFreshAccount(tab, name) {
      if (tab.account !== name) chat.setAccount(name);
    }
    // 新会话页的账号表收着时露几个：以前只露选中的那一个。
    // 现在按排好的顺序从上往下露，能放几个放几个，前提是对话区不多出能滚的余量；最多 4 个、至少 1 个，剩下的收进「其他 N 个号」。
    // 选中的号不在前几个里（用户点过排在后面的号）就顶掉最后一个，照样露着
    const acctFit = ref(3);
    function acctShown(rows, picked) {
      const top = rows.slice(0, acctFit.value).map(a => a.name);
      if (picked && !top.includes(picked)) top.splice(Math.max(0, top.length - 1), 1, picked);
      return top;
    }
    // 量还空着多少，够放几行就多露几行，不够就少露；改了以后下一帧再量一次（「其他 N 个号」那一行会出现、消失），几次以内就定下来。
    // 宽屏（输入框居中，见下面 freshPage）：输入框下面那块选项区（.fresh-opts，自己能滚）的可视高度里，内容（.fresh-opts-in）之外还空多少，前提是这块不用滚；
    // 手机（照旧整页一起滚）：输入框的顶边到新会话页这一段的底边（加上它底下留的那段）之间还空多少。
    // 正在输入框里打字时不量：手机上键盘弹起来对话区变矮，账号一行行收起来反而晃眼
    function fitAccts(depth = 0) {
      const tab = current.value;
      if (!tab?.fresh || tab.acctOpen || depth > 4) return;
      const log = document.querySelector('.log:not(.off)'), sec = log?.querySelector(':scope > .log-body > section.fresh');
      const opts = sec?.querySelector(':scope > .fresh-opts'), inner = opts?.firstElementChild, box = log?.querySelector('form.composer');
      const row = [...(sec?.querySelectorAll('.fresh-acct') || [])].find(e => e.offsetHeight);
      // 工作区、人设卡、环境这些是后取到的，内容会后变高；窗口变了选项区也跟着变：都盯着，变了再量
      // （section 在宽屏上是 display: contents，盯它不会报大小变化，所以三样一起盯）
      if (inner && inner !== freshSeen) {
        freshWatcher.disconnect();
        for (const e of [sec, opts, inner]) freshWatcher.observe(e);
        freshSeen = inner;
      }
      if (!row || !box || box.contains(document.activeElement)) return;
      const gap = parseFloat(getComputedStyle(row.parentElement).rowGap) || 0;
      let free;
      if (narrow.value) {
        free = box.getBoundingClientRect().top - sec.getBoundingClientRect().bottom - log.scrollTop - parseFloat(getComputedStyle(sec).marginBottom);
      } else {
        const cs = getComputedStyle(opts);
        free = opts.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - inner.offsetHeight;
      }
      const n = Math.max(1, Math.min(4, acctFit.value + Math.floor(free / (row.offsetHeight + gap))));
      if (n !== acctFit.value) { acctFit.value = n; nextTick(() => fitAccts(depth + 1)); }
    }
    // 新会话页：宽屏上输入框停在正中间，颜文字在上面，别的选项在下面自己滚。
    // 说出第一句话（发送中、正在建会话）就换回普通会话的排法，输入框落到最底下；没建成、话退回来时又回到正中间。
    // 手机上不这样：颜文字、选项、输入框整页一起排，发出第一句话时新会话页也不藏
    const freshPage = t => !!(!narrow.value && t?.fresh && !t.busy && !t.pending.length);
    // 宽屏上说出第一句话以后、会话建成之前：新会话页藏起来，输入框不再带新会话页那种带主题色的边
    const freshGone = t => !!(!narrow.value && t?.fresh && (t.busy || t.pending.length));
    // 两种排法一个是网格里的一行、一个是贴底悬浮，CSS 过渡接不上：换之前（watch 默认在页面更新前跑）记下输入框和底下那排小标签的位置，
    // 换好以后从旧位置动画回新位置（先算位移再放动画），落下时末尾稍微冲过一点再收回，像落地。
    // 只在同一个标签页里、窗口没跨过手机那条线时动：切标签页、转屏拖窗口换了排法不算
    watch(() => [current.value?.id, freshPage(current.value), narrow.value], ([id, now, thin], [was, then, wasThin]) => {
      if (id !== was || now === then || thin !== wasThin || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      const log = document.querySelector(`.log[data-tab="${id}"]`);
      const els = [log?.querySelector(':scope > form.composer'), log?.querySelector(':scope > .tools-slot')].filter(Boolean);
      const tops = els.map(e => e.getBoundingClientRect().top);
      nextTick(() => els.forEach((e, k) => {
        const dy = tops[k] - e.getBoundingClientRect().top;
        if (e.isConnected && Math.abs(dy) > 1) e.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }],
                                                          { duration: 460, easing: 'cubic-bezier(0.3, 1.18, 0.5, 1)' });
      }));
    });
    const fitSoon = () => nextTick(() => requestAnimationFrame(() => fitAccts()));
    const freshWatcher = new ResizeObserver(fitSoon);
    let freshSeen = null;
    watch(() => [current.value?.id, current.value?.fresh, current.value?.acctOpen, accountGroups.value.available.length,
                 narrow.value], fitSoon);
    addEventListener('resize', fitSoon);
    // 标签页的悬停卡片：悬停立刻出来、往上冒，写花费、用户最近说的一句话（不写路径）。
    // 最近一句从前端常驻的条目里倒着找，还没加载过内容的标签页就不写这一项
    // 垂直标签页（侧边栏里那一列，side）的卡片弹在标签页右边、和它上下居中，从左边长出来
    const tabTip = reactive({ tab: null, x: 0, y: 0, ox: 0, side: false });
    function showTabTip(e, t, side = false) {
      // 卡片上一项都写不出来（新会话标签页就是这样）就不弹，不然是一张空卡片
      if (touch || !(spentText(t.spent) || lastSaid(t))) return;
      const r = e.currentTarget.getBoundingClientRect();
      if (side) {
        // 横着从侧边栏外沿再空 8px（按药丸的右端会压在侧边栏的分隔线上）；
        // 竖直方向只按中线摆，离窗口上下边至少留一截，三行以内的卡片不会出窗口
        const right = e.currentTarget.closest('.side')?.getBoundingClientRect().right ?? r.right;
        const y = Math.max(60, Math.min(r.top + r.height / 2, innerHeight - 60));
        return void Object.assign(tabTip, { tab: t, x: Math.min(right + 8, innerWidth - 368), y, ox: 0, side });
      }
      const x = Math.max(8, Math.min(r.left, innerWidth - 368));
      // ox：标签页的中点离卡片左边多远，卡片从那里长出来
      Object.assign(tabTip, { tab: t, x, y: r.top - 6, ox: r.left + r.width / 2 - x, side });
    }
    function lastSaid(t) {
      const items = t.items || [];
      for (let i = items.length - 1; i >= 0; i--) {
        const it = items[i];
        if (it.kind === 'user' && !it.sidechain && it.text?.trim()) return it.text.trim();
      }
      return '';
    }
    // 没有输入框时「回到底部」这些按钮不用再给它让位置
    watch(readonly, v => v ? document.documentElement.style.setProperty('--composer-h', '0px')
                           : nextTick(chat.autoGrow), { immediate: true });
    const finder = useSearch({ current, openTab, ensureLoaded });
    // 上下文 / 花费的环形图（上下文药丸的悬停卡片、会话信息弹窗最上面）：点到具体某一条就收起卡片或弹窗、
    // 跳到那个气泡（和搜索结果同一个跳法）。弹窗打开时也取一次
    const { ctxMap, ctxView, loadCtxMap } = useCtxMap({ current });
    watch(() => chat.info.open, open => open && loadCtxMap());
    function jumpCtx(seq) {
      sessions.openDd.value = '';
      chat.info.open = false;
      document.activeElement?.blur?.();   // 键盘切进来展开的，不失焦卡片就一直开着
      finder.jumpTo({ seq });
    }
    // Ctrl/Cmd+F 不接管，留给浏览器自带的查找（浏览器只找得到已经画出来的那几百行，找全部要用页面上的找话）。
    // 找话从侧边栏顶上、收起时笔底下的搜索按钮进
    window.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        finder.search.open = false;
        if (!sessions.sideWide.value) sessions.sideOpen.value = false;   // 宽屏上常驻的侧边栏只有点两条横线才收
      }
    });
    // 页面根上的 simple 写死在 index.html 的 <html> 上：样式里各处还按它选（弃用经典模式前，界面在两种之间切）。
    // 权限、模型、账号、上下文这一排（下看板）放在输入框正下方，不在任何容器里，就是几个灰字（照 Claude 网页版：
    // 权限在左边，模型、账号、上下文在右边）。它要和输入框在同一个滚动区里：触摸板滚到底再往上拉时内容会回弹，
    // 输入框跟着一起动，这一排也得跟着动（做成 position: fixed 摆在输入框里的话，回弹时输入框走了、它留在原地）。
    // 所以模板里把下看板整个 teleport 进正在看的那个会话输入框底下的槽（index.html 的 .tools-slot，id 是 tools-会话编号）。
    // 槽是和输入框一起画出来的，要等这一轮画完才找得到：先挂空、下一拍再指过去（toolsTo 空着就是不搬，留在下看板原处）。
    // 同一个会话的输入框重画了（槽换了一个元素、编号没变），Vue 不会自己重新找目标，所以先撤回原处再指过去
    const toolsTo = ref('');
    let toolsAt = null;
    function aimTools() {
      const id = active.value ? `tools-${active.value}` : '';
      const el = id ? document.getElementById(id) : null;
      if (el === toolsAt) return;
      toolsAt = el;
      toolsTo.value = '';
      if (el) nextTick(() => { if (toolsAt === el) toolsTo.value = '#' + id; });
    }
    watch([active, () => chat.composerInput.value], () => nextTick(aimTools), { immediate: true });
    // 默认没有标签页栏：在侧边栏、全部会话里换到别的会话时，后台只留最近打开的几个（设置里「标签页常驻」留几个，keepN，含正在看的；
    // 不然点回刚看过的会话每次都要「正在打开」），更早的从这台设备上收掉（同关标签页，会话本身不受影响），
    // 不然看不见的标签页越攒越多、个个都订着推送。还没说第一句话的新会话标签页里写了东西、或者正在建会话的，不算数也不收（tabs.js 的 stays）。
    // 设置里开了「底部标签页」（sessions.keepTabs）就有标签页栏，不收。刷新页面时只恢复正在看的那个（tabs.js 的 load）
    function trimTabs() {
      if (sessions.keepTabs.value) return;
      const keep = new Set(seen.value.filter(id => tabs.value.some(t => t.id === id && !stays(t))).slice(0, keepN()));
      if (active.value) keep.add(active.value);
      for (const t of [...tabs.value]) if (!keep.has(t.id) && !stays(t)) close(t);
    }
    watch(active, (id, old) => { if (old && old !== id) trimTabs(); });
    // 关掉「底部标签页」、改了留几个：马上按规矩收
    watch([() => sessions.keepTabs.value, () => perf.keepAlive, () => perf.keepMax], trimTabs);
    // 看板上的弹出卡片挂在看板朝着对话区的那条边上（样式见 style.css「看板上的弹出卡片」）：悬停、点开、键盘切进来时现算位置交给样式——
    // 上看板的往下挂，顶边对齐上看板底线；下看板的往上挂，底边对齐下看板顶线；都往里多盖 --hang-over 把线盖住，
    // 在这一格正对着的地方居中，碰到窗口边往里收。看板高度、药丸位置都会变，所以每次现算。
    // 已经有一张卡片开着时换到另一格，给看板挂一下 swap，新的直接出现、旧的直接消失，像同一张卡片换了内容
    let swapTimer = 0;
    // 卡片开着时自己变了大小（最近会话悬停时才取标签，取到后多出一栏、变宽），按新尺寸再摆一次，不然会伸出窗口
    const panelWatcher = new ResizeObserver(es => {
      for (const e of es) if (getComputedStyle(e.target).visibility === 'visible') place(e.target.parentElement);
    });
    // 卡片的位置是相对这一格算的偏移：同一排的小标签变宽变窄（换号后账号小标签从「本周额度用完了」变成一个百分比、切换中…），
    // 这一格跟着挪，卡片也被带着平移出去（这一排靠右排，手机上换号时卡片会横移出屏幕）。
    // 所以同一排每一格的大小都盯着，变了就把开着的卡片按新位置重摆
    const chipWatcher = new ResizeObserver(es => {
      const rows = new Set(es.map(e => e.target.parentElement));
      for (const row of rows) for (const dd of row?.querySelectorAll(':scope > .dd') || []) {
        const panel = dd.querySelector(':scope > .dd-panel');
        if (panel && getComputedStyle(panel).visibility === 'visible') place(dd);
      }
    });
    function place(dd) {
      // 看板外壳是 display: contents，量不出位置，要量这一格所在的那半块
      const half = dd.closest('.bar-head, .bar-tools'), panel = dd.querySelector(':scope > .dd-panel');
      if (!half || !panel) return;
      const b = half.getBoundingClientRect(), d = dd.getBoundingClientRect(), w = panel.offsetWidth;
      let left = Math.max(8, Math.min(d.left + d.width / 2 - w / 2, innerWidth - w - 8));
      // 小标签在输入框底下，卡片是单独一张、浮在小标签上面，左边和小标签对齐（右边的小标签卡片往左收），左右都不出输入框
      // （不然权限卡片会飞出输入框、压到侧边栏上）；卡片比输入框还宽就只管不出窗口。--hang-origin 是小标签的中点，卡片从那里长出来
      if (half.classList.contains('bar-tools')) {
        const c = document.querySelector('.log:not(.off) form.composer')?.getBoundingClientRect();
        const lo = Math.max(8, c && c.width >= w ? c.left : 0), hi = Math.min(innerWidth - 8, c && c.width >= w ? c.right : innerWidth);
        left = Math.max(lo, Math.min(d.left - 4, hi - w));
        dd.style.setProperty('--hang-origin', `${d.left + d.width / 2 - left}px`);
      }
      const over = parseFloat(getComputedStyle(half).getPropertyValue('--hang-over')) || 0;
      if (half.classList.contains('bar-tools')) {
        dd.style.setProperty('--hang-bottom', `${d.bottom - b.top - over}px`);
        dd.style.setProperty('--hang-gap', `${Math.max(0, d.top - b.top)}px`);
      } else {
        dd.style.setProperty('--hang-top', `${b.bottom - d.top - over}px`);
        dd.style.setProperty('--hang-gap', `${Math.max(0, b.bottom - d.bottom)}px`);
      }
      dd.style.setProperty('--hang-left', `${left - d.left}px`);
      // 卡片不许冲出窗口上下沿（号多了，矮屏幕上账号卡片会往上出界）：量出卡片这一头到窗口边还剩多高（留 8px）交给样式当最大高度。
      // 放不下的挂 tall，卡片整张滚；账号卡片例外，只有表头下面的表身（能选的号连同收起来的墓碑，.acct-body）自己缩、自己滚（见 style.css 最后一段）。
      // 量的时候先摘掉 tall，按内容原本多高来比，不然挂上以后变矮、下一次又摘掉，来回跳
      // 往上弹的卡片还要让开顶上浮着的圆按钮（手机上会盖住那三颗）：卡片横向跨过哪颗，上沿就停在哪颗底下 8px，按钮的位置当场量
      let ceil = 8;
      if (half.classList.contains('bar-tools')) {
        for (const btn of document.querySelectorAll('.bar-head > *')) {
          const r = btn.getBoundingClientRect();
          if (r.height && r.right > left && r.left < left + w) ceil = Math.max(ceil, r.bottom + 8);
        }
      }
      const room = Math.floor(half.classList.contains('bar-tools') ? d.top - 10 - ceil
        : innerHeight - (innerWidth > 760 ? b.bottom - over : b.bottom + 4) - 8);
      dd.style.setProperty('--hang-max', `${Math.max(120, room)}px`);
      panel.classList.remove('tall');
      panel.classList.toggle('tall', panel.scrollHeight > Math.max(120, room));
    }
    // 窗口变高变矮（转屏、拖窗口、手机键盘弹出）时开着的卡片按新的空间重摆
    addEventListener('resize', () => {
      for (const p of document.querySelectorAll('.bar .dd-panel, .bar-tools .dd-panel')) {
        if (getComputedStyle(p).visibility === 'visible') place(p.parentElement);
      }
    });
    function hang(dd) {
      // 下看板被搬进了输入框底下（toolsTo），不在 .bar 里
      const bar = dd.closest('.bar') || dd.closest('.bar-tools'), panel = dd.querySelector(':scope > .dd-panel');
      if (!bar || !panel) return;
      place(dd);
      panelWatcher.observe(panel);
      for (const x of dd.parentElement?.children || []) chipWatcher.observe(x);
      const other = [...bar.querySelectorAll('.dd-panel')].some(x => x !== panel && getComputedStyle(x).visibility === 'visible');
      if (other) {
        bar.classList.add('swap');
        clearTimeout(swapTimer);
        swapTimer = setTimeout(() => bar.classList.remove('swap'), 250);
      }
    }
    for (const type of ['pointerover', 'focusin']) {
      document.addEventListener(type, e => {
        const dd = e.target.closest?.('.bar .dd, .bar-tools .dd');
        if (dd && !(e.relatedTarget && dd.contains(e.relatedTarget))) hang(dd);   // 在同一格里移动不重算
      });
    }

    // 右键菜单（安卓长按、iPhone 和 iPad 双击，见 menu.js 的 on）：气泡和工具卡片都从这里拿，所以往下提供给所有子组件
    const menu = useMenu({ quote: chat.quote, refill: chat.refill });
    // 菜单不带灰色说明小字（重命名、删除会话后面那些，多余）；挑标签颜色那份的「当前」照留，那是在标哪个选着
    const openMenu = menu.open;
    menu.open = (e, items) => openMenu(e, items.map(it => (it.dot != null ? it : { ...it, hint: '' })));
    provide('menu', menu);
    provide('canQuote', () => !readonly.value);
    // 头像（avatar.js）：账号页「头像」那一行换、恢复；各处头像有图画图、没图画名字头一个字
    const avatar = useAvatar({ menu });   // 只读模式没有输入框，右键菜单里就不给「引用回复」

    // 标签页上点右键（手机上长按，v-longpress 自己计时，苹果设备上也能用；双击不弹）：重命名、编辑标签、复制路径、导出、关掉、删除
    const rename = reactive({ open: false, tab: null, name: '', error: '', busy: false });
    const openRename = tab => Object.assign(rename, { open: true, tab, name: tab.title || '', error: '', busy: false });
    async function saveRename() {
      const tab = rename.tab;
      if (!tab || rename.busy) return;
      rename.busy = true;   // 完整模式下要等 Claude 空闲、收掉连着的 CLI，可能要一会儿
      try {
        // 侧边栏里没开着的会话（sideMenu 交来的是会话摘要）：先在服务器上打开它（不切过去），改好只把名字写回摘要。
        // 用户正等着改名的结果，照正在看的打开（focus），不排在后台恢复的队伍后面
        const own = tabs.value.includes(tab);
        if (!own) await openOnServer(tab.path);
        const r = await api(`/api/tabs/${tab.id}/title`, { method: 'POST', body: { title: rename.name } });
        own ? Object.assign(tab, r) : (tab.title = r.title);
        sessions.renamed(tab.id, r.title);
        rename.open = false;
      } catch (e) {
        rename.error = e.message;
      }
      rename.busy = false;
    }
    async function copyPublicShare(tab) {
      if (people.shareBusy.value) return;
      people.shareBusy.value = true;
      say('正在创建分享链接…', { ms: 0 });
      try {
        const url = await sessions.shareLink(tab);
        if (!await menu.copy(url, '分享链接')) {
          window.prompt('分享链接已经创建；请从这里手动复制：', url);
        }
      } catch (e) { fail('创建分享链接', e); }
      finally { people.shareBusy.value = false; }
    }
    async function revokePublicShare(tab) {
      if (people.shareBusy.value) return;
      people.shareBusy.value = true;
      say('正在撤销分享链接…', { ms: 0 });
      try {
        const { revoked } = await sessions.revokeShare(tab);
        say(revoked ? `已撤销 ${revoked} 条分享链接` : '这个会话没有待撤销的分享链接');
      } catch (e) { fail('撤销分享链接', e); }
      finally { people.shareBusy.value = false; }
    }
    // 关闭其他标签页（橙字）：先切到这个，再把别的一个个关掉，和点它们的 × 一样只是从这台设备的标签栏拿掉。
    // 先切过去，免得每关一个、正在看的就跳到旁边那个上去。只剩它一个时不给这一项
    const closeOthersItem = tab => (tabs.value.length > 1 ? [{ label: '关闭其他标签页', tone: 'warn', run: () => {
      menu.close();
      select(tab);
      tabs.value.filter(t => t !== tab).forEach(close);
    } }] : []);
    function tabMenu(e, tab, head = []) {
      if (tab.source === 'api') return menu.open(e, [...head, { label: '查看只读会话', run: () => openTab(tab.path) }]);
      if (tab.fresh) {   // 还没建出会话，能做的只有关掉
        return menu.open(e, [{ label: '关闭标签页', run: () => { menu.close(); close(tab); } }, ...closeOthersItem(tab)]);
      }
      menu.open(e, [
        ...head,
        // 自动审稿的会话才有（跑过 review.py 或选了审稿人设卡）：侧边栏里没开着的先打开、切过去再开看板
        ...(review.reviewable(tab) ? [{ label: '审稿看板', run: async () => {
          menu.close();
          try { tabs.value.includes(tab) ? select(tab) : await openTab(tab.path); } catch (e) { return fail('打开会话', e); }
          nextTick(review.openReview);
        } }] : []),
        { label: '重命名', hint: readonly.value ? '仅修改显示名称' : '同步写入会话记录',
          run: () => { menu.close(); openRename(tab); } },
        { label: '编辑标签…', run: () => { menu.close(); sessions.openTagEdit(tab); } },
        // 自己的会话：打开下看板工作设备那一格的卡片（devices.js、服务器 workdev.py）。侧边栏里没开着的先打开、切过去
        ...(people.me.devices && devices.dev.work && devices.workMine(tab) ? [{ label: '工作设备…',
          hint: 'workOff' in tab ? devices.workText(tab) : '',
          run: async () => {
            menu.close();
            try { tabs.value.includes(tab) ? select(tab) : await openTab(tab.path); } catch (e) { return fail('打开会话', e); }
            devices.loadDevices();
            setTimeout(() => (sessions.openDd.value = 'work'));   // 等这次点击传完再展开，理由同下面的可见范围
          } }] : []),
        { label: '复制会话路径', run: () => menu.copy(tab.path, '路径') },
        ...(people.canPublicShare(tab) ? [
          { label: '复制分享链接', hint: '对方登录后仅可查看', run: () => { menu.close(); copyPublicShare(tab); } },
          { label: '撤销分享链接', run: () => { menu.close(); revokePublicShare(tab); } },
        ] : []),
        // 导出的是正在看的那个：侧边栏里没开着的会话先打开、切过去再导出
        { label: '导出 Markdown', run: async () => {
          menu.close();
          try { tabs.value.includes(tab) ? select(tab) : await openTab(tab.path); } catch (e) { return fail('打开会话', e); }
          nextTick(exportMd);
        } },
        // 没开「底部标签页」时没有标签页栏，也就没有「关闭标签页」；侧边栏里没开着的会话也没有
        ...(!sessions.keepTabs.value || !tabs.value.includes(tab) ? []
          : [{ label: '关闭标签页', hint: '仅本设备', run: () => { menu.close(); close(tab); } }, ...closeOthersItem(tab)]),
        { label: '删除会话…', hint: deleteWhy(tab) || '对所有人隐藏，可恢复', tone: 'danger',
          run: () => { menu.close(); deleteWhy(tab) ? say(deleteWhy(tab), { err: true }) : openDelete(tab); } },
      ]);
    }
    // 侧边栏「最近」右边的漏斗和标签（漏斗的几项照 Gemini）：选中的那项打勾（菜单的 check），前面画状态点（stateDot）或标签色点
    function sideStateMenu(e) {
      const f = sessions.sideFilter, pick = state => () => { menu.close(); f.state = state; };
      menu.open(e, [
        { label: '全部', check: !f.state, run: pick('') },
        { label: '有新消息', stateDot: 'unread', check: f.state === 'unread', run: pick('unread') },
        { label: '需要处理', stateDot: 'attention', check: f.state === 'attention', run: pick('attention') },
        { label: '进行中', stateDot: 'run', check: f.state === 'run', run: pick('run') },
      ]);
    }
    async function sideTagMenu(e) {
      const f = sessions.sideFilter, pick = tag => () => { menu.close(); f.tag = tag; };
      let list;
      try { list = await sessions.loadSideTags(); } catch (err) { return fail('加载标签', err); }
      // 正筛着的标签这会儿没有会话了（标签被删光）也列出来，不然点不掉
      if (f.tag && !list.some(t => t.name === f.tag)) list = [{ name: f.tag, count: 0 }, ...list];
      menu.open(e, [
        { label: list.length ? '全部标签' : '暂无带标签的会话', check: !f.tag, run: pick('') },
        ...list.map(t => ({ label: t.name, dot: tagHue(t.name), hint: String(t.count), check: f.tag === t.name, run: pick(t.name) })),
      ]);
    }
    // 侧边栏里的会话：第二行的「⋯」、右键（手机上长按）弹出和右上角「⋯」同一份菜单。
    // 开着的就用那个标签页；没开着的直接用会话摘要（编号、路径、名字、标签、所属用户都有），不为了弹菜单切过去
    // 置顶放在最前面：手机上往右划这一行也行（pinSwipe），电脑上没有划的手势，靠这一项
    const sideMenu = (e, s) => tabMenu(e, tabs.value.find(t => t.id === s.id) || s,
      sessions.sidePins.value ? [{ label: s.pinned ? '取消置顶' : '置顶', run: () => { menu.close(); sessions.togglePin(s); } }] : []);

    // 删除会话：只在查看器里删，会话文件不动、能恢复（见服务器 users.py「删掉的会话」）。
    // 能不能删和服务器 users.delete_why 一个规矩：所属用户和查看器账号，无主会话只有查看器账号，单用户实例里打开的人就是查看器账号。
    // 先弹窗确认；删好了这台设备上开着它的标签页关掉，别的设备、同一会话经别的路径开着的标签页由推送关
    const deleteWhy = tab => (!people.me.multi || people.me.admin ? ''
      : !tab.owner ? '无主会话仅查看器账号可删除'
      : tab.owner === people.me.name ? '' : `这是 ${tab.owner} 的会话，仅 ${tab.owner} 和查看器账号可删除`);
    const trash = reactive({ open: false, tab: null, error: '', busy: false });
    const openDelete = tab => Object.assign(trash, { open: true, tab, error: '', busy: false });
    async function confirmDelete() {
      const tab = trash.tab;
      if (!tab || trash.busy) return;
      trash.busy = true;
      try {
        await api(`/api/sessions/${tab.id}/deleted`, { method: 'POST', body: { deleted: true } });
        trash.open = false;
        close(tab);
        sessions.forget(tab.id);
        say(`已删除「${tab.title || fileName(tab.path)}」，可在全部会话底部的「已删除」中恢复`);
      } catch (e) {
        trash.error = e.message;
      }
      trash.busy = false;
    }
    // 标签上点右键：挑颜色。「自动多彩」是按名字自动配的那种，前面的色点就是它会用的颜色
    function tagColorMenu(e, name) {
      const cur = sessions.tagColors[name];
      const pick = color => { menu.close(); sessions.setTagColor(name, color); };
      menu.open(e, [
        { label: '自动多彩', dot: PALETTE[autoColor(name)].h, hint: cur == null ? '当前' : '', run: () => pick(null) },
        ...PALETTE.map((p, i) => ({ label: p.name, dot: p.h, hint: cur === i ? '当前' : '', run: () => pick(i) })),
      ]);
    }
    provide('tagColorMenu', tagColorMenu);

    // 全部会话里的卡片上点右键（安卓长按、iPhone 和 iPad 双击）：编辑标签、复制路径
    function cardMenu(e, s) {
      if (s.source === 'api') return tabMenu(e, s);
      menu.open(e, [
        { label: '编辑标签…', run: () => { menu.close(); sessions.openTagEdit(s); } },
        { label: '复制会话路径', run: () => menu.copy(s.path, '路径') },
        ...(people.canPublicShare(s) ? [
          { label: '复制分享链接', run: () => { menu.close(); copyPublicShare(s); } },
          { label: '撤销分享链接', run: () => { menu.close(); revokePublicShare(s); } },
        ] : []),
      ]);
    }

    // 分享链接（地址后面带 #s=<会话编号>，或者服务器把 /share/<凭证> 转来的 #share=<凭证>，凭它换会话编号，见 server.open_share）：
    // 打开那个会话并切过去，打开了就把这截从地址栏去掉，刷新不会再开一次。游客被拒时链接留着：登录后整页刷新，会再开一次
    async function openShared() {
      const hash = new URLSearchParams(location.hash.slice(1)), token = hash.get('share');
      if (!hash.get('s') && !token) return;
      // 带着原来的 state：返回键（back.js）靠它认这一步
      const drop = () => history.replaceState(history.state, '', location.pathname + location.search);
      try {
        await openSid(token ? (await api(`/api/share/${encodeURIComponent(token)}`, { method: 'POST' })).sid : hash.get('s'));
        drop();
      } catch (e) {
        if (e.status === 403 && people.me.multi && !people.me.name) {
          say(`${e.message}（登录后将自动打开此会话）`, { err: true, ms: 0 });
        } else {
          drop();
          fail('打开分享的会话', e);
        }
      }
    }

    // 导出成 Markdown 存到本地（整段对话，不只是已经画出来的那一截）
    const exporting = ref(false);
    async function exportMd() {
      const tab = current.value;
      if (!tab || exporting.value) return;
      exporting.value = true;
      try {
        const r = await exportMarkdown(tab);
        menu.say(`已导出 ${r.items} 条`);
      } catch (e) {
        fail('导出', e);
      }
      exporting.value = false;
    }

    // 换标签：输入框换了一个、刻度尺换了一份内容，都要重新量一次
    watch(active, () => nextTick(() => {
      const tab = current.value, el = scroller.value;
      if (!el) return;
      chat.autoGrow();
      if (!tab.shown) {   // 第一次显示这个标签，停在最下面；新会话标签页停在顶上，从头看起
        tab.shown = true;
        el.scrollTop = tab.fresh ? 0 : el.scrollHeight;
      }
      minimap.recalibrate();
    }));

    // 多用户实例没有游客（只改前端交互，服务器那边照旧）：没登录、登录失效、退出以后，
    // 整页只有登录 / 注册那一个界面（login），别的什么都不画，也不去恢复标签页、连推送。
    // wait：还没确认是谁，先空着，免得游客先闪一下主界面；确认是谁失败了照常画主界面，那里写着出错原因和重试
    const authGate = computed(() => {
      const me = people.me;
      if (!me.loaded && !me.error) return 'wait';
      return me.loaded && me.multi && !me.name ? 'login' : 'app';
    });
    // 宽屏上侧边栏常驻（sessions.js 的 sideWide）：页面根上挂 side-docked，样式把页面挤到侧边栏右边、去掉压暗的那层。
    // 只在主界面挂：登录页、还没确认是谁时根本没画侧边栏，挂上了登录卡片会偏到右边去
    watch(() => authGate.value === 'app' && sessions.sideOpen.value && sessions.sideWide.value, v => {
      document.documentElement.classList.toggle('side-docked', v);
    }, { immediate: true });
    // 开了底部标签页：标签页栏固定贴在窗口最底下，量出它的高度交给样式（--tabbar-h），底下那排小标签连同输入框往上让开这么高，
    // 不被它盖住（不然开了以后被控制区挡住）；没有标签页栏时是 0。栏的高度会变（手机上标签页高一点），所以一直盯着
    const tabbarWatcher = new ResizeObserver(([e]) =>
      document.documentElement.style.setProperty('--tabbar-h', `${e.target.offsetHeight}px`));
    // 垂直标签页（sessions.sideTabs）时标签页在侧边栏里，底下没有栏，也是 0
    watch([() => sessions.keepTabs.value, () => sessions.sideTabs.value, authGate], () => nextTick(() => {
      tabbarWatcher.disconnect();
      const bar = document.querySelector('#app .tabbar');
      if (bar) tabbarWatcher.observe(bar);
      else document.documentElement.style.setProperty('--tabbar-h', '0px');
    }), { immediate: true });
    const rootReady = sessions.loadRoot();
    rootReady.then(sessions.loadSettings);   // 失败时它自己会报、看板上挂着重试
    // 宽屏上侧边栏一打开页面就常驻着：登录确认好了就先画这台设备上存的上次那一页、再取新的（打开侧边栏那一下才取的话，这里没人点）。
    // 不等标签页恢复完（load）：等它的话，页面打开要 1.5 秒左右才有列表
    people.meReady.then(() => {
      if (authGate.value !== 'login' && sessions.sideOpen.value) {
        sessions.prefillSide();
        sessions.loadRecent();
      }
    });
    people.meReady.then(() => authGate.value !== 'login' && load(people.meReady).then(async () => {
      if (!current.value?.fresh) minimap.scrollToEnd();
      connect();
      // 常驻着时换了会话（新建的会话也算）就顺手刷新列表，里面的选中和顺序跟着变
      watch(active, () => sessions.sideOpen.value && sessions.sideWide.value && sessions.loadRecent());
      await openShared();
      // 页面开着时又粘贴了一个分享链接：只换了 # 后面那截，页面不会刷新
      window.addEventListener('hashchange', openShared);
      // 新设备（或者标签页都关了）打开页面，一个标签页都没有：直接开一个新会话标签页，不用先去找加号。
      // 只读模式不能新建，得等服务器配置到了才知道是不是只读；配置没取到就不开，看板上挂着重试
      await rootReady;
      const freshIfEmpty = () => {
        if (!tabs.value.length && !readonly.value && !sessions.configError.value) sessions.openFresh();
      };
      freshIfEmpty();
      // 之后标签页全没了（关掉最后一个，不管它是不是新会话；或者看不到了被撤掉）也一样补一个新会话标签页，
      // 和浏览器关掉最后一个标签页就开新标签页一个道理：空页面没有意义
      watch(() => tabs.value.length, freshIfEmpty);
    }));

    // 底下那排小标签放不下时按先后让地方：先把 Remotes 缩成图标加台数（.bar-tools.fit-dev），
    // 还不够再把花费并回上下文那颗（fit-merge：花费那颗藏起来，上下文的卡片里带回两种视角的切换，toolsMerged），再挤才轮到权限、模型截成省略号。
    // 量法：每次从不让开始一档一档加，看小标签里的字有没有被截、三段有没有出界；在同一帧里改 class、量，画出来的只有最后那一档。
    // 宽度变了（ResizeObserver）、小标签里的字变了（MutationObserver：换会话、换模型、台数和花费变了）都重量，一帧最多量一次
    const toolsMerged = ref(false);
    {
      let bar = null, queued = false;
      const ro = new ResizeObserver(() => soon()), mo = new MutationObserver(() => soon());
      const attach = () => {
        if (bar?.isConnected) return true;
        ro.disconnect(); mo.disconnect();
        bar = document.querySelector('.bar-tools');
        if (!bar) return false;
        ro.observe(bar);
        mo.observe(bar, { childList: true, subtree: true, characterData: true });
        return true;
      };
      const tight = () => {
        const chips = bar.querySelector(':scope > .chips');
        if (!chips) return false;
        const box = chips.getBoundingClientRect();
        // 一颗颗看有没有出界（手机上三段拆开排，分段本身没有框）；藏起来的那颗量出来是 0 宽，跳过
        for (const el of chips.querySelectorAll(':scope > .chip-grp > *')) {
          const r = el.getBoundingClientRect();
          if (r.width && (r.left < box.left - 1 || r.right > box.right + 1)) return true;
        }
        for (const t of chips.querySelectorAll('.chip > .chip-text')) if (t.scrollWidth > t.clientWidth + 1) return true;
        return false;
      };
      function fit() {
        queued = false;
        if (!attach()) return;
        bar.classList.remove('fit-dev', 'fit-merge');
        if (tight()) {
          bar.classList.add('fit-dev');
          if (tight()) bar.classList.add('fit-merge');
        }
        toolsMerged.value = bar.classList.contains('fit-merge');
      }
      function soon() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(fit);
      }
      watch(() => authGate.value === 'app', on => on && nextTick(soon), { immediate: true });
    }

    const exposed = {
      authGate, tabs, active, current, setLog, select, close, keepShown, slide, narrow, onTouchStart, onTouchEnd, onSideTouchStart, onSideTouchMove, onSideTouchEnd, onSideTouchCancel, onSideClickCapture, pushLost, retrySync,
      dragOn: { touchmovePassive: onTouchMove, touchcancelPassive: onTouchCancel }, peeks, pagerOn, sideDrag,
      show, filterLabels, follow, mmMine, mdUser, canFull, full, setFull, perf, KEEP_CHOICES, THEMES, theme,
      ...minimap, ...chat, ...sessions, ...finder, ...quota, ...people, ...reserve, ...devices, ...personas, ...avatar, ...todos, ...stats, ...voice, ...review,
      // 侧边栏里挑会话、开新会话、去搜索都先调它：待办页开着就收起（挑的正是眼下这个会话时 active 不变，上面那个 watch 接不到）
      sideDone: () => { todos.closeTodos(); sessions.sideDone(); }, ctxMap, ctxView, loadCtxMap, jumpCtx, sortedAccounts, accountGroups, defaultChoices, deadOpen, waitOpen, waitFolded, priceList, priceRows, priceNum, priceFull, splitPop, rowIn, rowOut, fallbackAccount, effortOf, modelOf, pickFreshAccount, MODE_TIPS, menu, exportMd, exporting,
      acctFit, acctShown, toolsMerged, freshPage, freshGone, toolsTo, modeShort, effortLabel, chipModel, tabMenu, sideMenu, sideStateMenu, sideTagMenu, cardMenu, tagColorMenu, tagStyle, rename, saveRename, trash, confirmDelete, readonly, tabTip, showTabTip, lastSaid,
      copyPublicShare, revokePublicShare,
      acctFamily: familyOf,   // 号名写成图标 + 编号（acct-tag.js）时认是哪一家
      acctNumWidth,
      touch, fileName, clock, kilo, kiloInt, ago, shortModel, spentText, pointsVer, promoTag: quota.promoTag,
      say,   // 模板里直接弹说明（请求大小提醒，手机上没有悬停）
    };
    // 返回键先收起最上面那一层（back.js）。从上往下排：菜单、小标签的卡片（只算手机上点开的，电脑上悬停开关不记步）、
    // 弹窗（.over 那两个压在最上面，其余照 index.html 里的先后、后写的盖在先写的上面）、侧边栏的设置页和账号页、窄屏上的侧边栏抽屉
    const ex = exposed, mask = (o, close = () => (o.open = false)) => ({ open: () => !!o.open, close });
    const layers = [
      { open: () => !!menu.menu.items.length, close: menu.close },
      { open: () => touch && !!sessions.openDd.value, close: () => (sessions.openDd.value = '') },
      { open: () => !!(ex.rv.open && ex.rv.confirm), close: () => ex.rv.busy || (ex.rv.confirm = false) },
      mask(ex.ledger), mask(ex.priceList), mask(ex.traffic), mask(ex.admin), mask(ex.board), mask(ex.us), mask(ex.dev, ex.closeDevices),
      { open: () => !!ex.pc.ask, close: () => ex.answerAsk(false) }, mask(ex.pc, ex.closePersonas),
      mask(ex.rb), mask(ex.rv), mask(ex.pw), mask(ex.newChat), mask(ex.search), mask(ex.tagEdit), mask(ex.trash), mask(ex.rename), mask(ex.info),
      mask(ex.overview), mask(ex.picker),
      { open: () => sessions.sideOpen.value && sessions.sideView.value === 'set', close: () => (sessions.sideView.value = 'me') },
      { open: () => sessions.sideOpen.value && sessions.sideView.value !== 'main', close: () => (sessions.sideView.value = 'main') },
      { open: () => sessions.sideOpen.value && !sessions.sideWide.value, close: () => (sessions.sideOpen.value = false) },
      { open: () => todos.td.open, close: todos.closeTodos },   // 待办页压在侧边栏抽屉底下、对话区上面
    ];
    const back = useBack(layers);
    // 底下那一截跟着手指走：输入框、小标签那一排、底部标签页栏和回到顶部 / 底部，就像接在屏幕底部内容再往下的东西，
    // 竖着拖的时候顺着手指一起动，一帧跟一帧：手指往下（往上翻以前的内容）就跟着往下走出屏幕，手指往上（往回看最新的）就跟着冒出来，
    // 冒出来比手指快一倍，往回滚容易出来；松手时停在半路的，照手指最后的走向落定（Web Animations，0.22 秒），之后不自己再动。
    // 点一下屏幕切换：露着就收起，收着就回来（点到链接、按钮、能点开的气泡这些不算）。顶上那几颗圆按钮不动。只给触屏。
    // 往下最多走整截的高度，也不超过离对话末尾还有多远：输入框和小标签那一排在对话区这个滚动区里，挪出末尾的那截会撑大可滚动的高度
    // （停在最底下时会变成「没到底」，跟着新消息往下走就断了）。所以藏着时快到末尾就被末尾顶上来、跟着内容露出来，到了最底下就是完整的样子
    // （点回到底部、跟着新消息走都一样）；在末尾附近点一下收起，能挪多少挪多少，剩下的原地淡掉，往下拖到整截都出了屏幕再撤掉淡掉。
    // 新会话页、开着菜单弹窗侧边栏这些层、输入框报错、正在打字时不跟手，已经挪走的落回原处；焦点进了输入框也落回，换标签页直接回原处。
    // 不做停几秒自己收、按滚动方向整块收放：出现和消失会拽走注意力，逻辑也复杂
    if (touch) {
      // REVEAL：往上冒出来时按手指位移的几倍走（一比一往回滚很难出来）；往下藏照旧一比一，看着像接在内容底下。TAP_MS：按多久以内算点一下
      const root = document.documentElement, SLOP = 8, GAP = 8, REVEAL = 2, TAP_MS = 400, EASE = 'cubic-bezier(0.2, 0.7, 0.2, 1)';
      const reduce = matchMedia('(prefers-reduced-motion: reduce)');
      // T：眼下往下挪了多少；F：原地淡掉了（末尾附近点了收起、挪不出去的时候）；hid：落定在藏起来那头（之后跟着离末尾的距离走）；
      // full：整截有多高；els：跟着动的几样和各自最多挪多少；coast：手指滚动（连同松手后的惯性）最后动的时刻，按住正在惯性滚的页面让它停下不算点一下
      let T = 0, F = false, hid = false, full = 0, els = [], g = null, coast = -1e9;
      const typing = () => root.classList.contains('kb-lift') || root.classList.contains('kb-freeze')
        || (!narrow.value && !!document.activeElement?.closest?.('.composer'));
      const stay = () => authGate.value !== 'app' || !current.value || !!current.value.fresh || !!current.value.failed
        || !!current.value.chatError || layers.some(l => l.open());
      // 正在看的会话的输入框（或只能看时那行说明）、小标签那一排、标签页栏一起走整截；回到顶部 / 底部只走到离窗口底边 16px
      const measure = () => {
        const log = scroller.value, wrap = log?.closest('.logwrap');
        const c = log?.querySelector(':scope > .composer'), s = log?.querySelector(':scope > .tools-slot');
        full = (c?.offsetHeight || 0) + (s?.offsetHeight || 0) + GAP;   // 多走 GAP，输入框的阴影也出去
        const cap = Math.max(0, full - GAP - 4);
        els = [c, s, document.querySelector('.tabbar')].filter(Boolean).map(el => ({ el, cap: Infinity }))
          .concat([...(wrap ? wrap.querySelectorAll(':scope > .to-top, :scope > .to-bottom') : [])].map(el => ({ el, cap })));
      };
      const room = () => {
        const log = scroller.value;
        return log ? Math.max(0, Math.min(full, Math.floor(log.scrollHeight - log.scrollTop - log.clientHeight))) : 0;
      };
      // 挪到 v、淡不淡（f）；anim 时用 Web Animations 从眼下的样子过去。原地淡掉只给输入框、小标签那一排和标签页栏，
      // 回到顶部 / 底部自己按滚动淡入淡出，不碰它们的透明度
      const put = (v, anim = false, f = F) => {
        const was = T, wasF = F, go = anim && !reduce.matches;
        T = v; F = f;
        for (const { el, cap } of els) {
          const a0 = Math.min(was, cap), a1 = Math.min(v, cap);
          for (const a of el.getAnimations()) if (a.id === 'bar-y' || (a.id === 'bar-o' && wasF !== f)) a.cancel();
          el.style.translate = a1 ? `0 ${a1}px` : '';
          if (go && a0 !== a1) el.animate({ translate: [`0 ${a0}px`, `0 ${a1}px`] }, { duration: 220, easing: EASE, id: 'bar-y' });
          if (cap !== Infinity || wasF === f) continue;
          el.style.opacity = f ? '0' : '';
          el.style.pointerEvents = f ? 'none' : '';
          if (go) el.animate({ opacity: [wasF ? 0 : 1, f ? 0 : 1] }, { duration: 220, easing: 'ease', id: 'bar-o' });
        }
      };
      const home = () => { hid = false; if (T || F) put(0, true, false); };
      // 点一下切换：露着就收起，收着就回来。离末尾不够整截高时挪到末尾为止，剩下的原地淡掉
      const toggle = () => {
        if (F || (full && T >= full)) return home();
        measure();
        const max = room();
        hid = true;
        put(max, true, max < full);
      };
      // 点到能点的东西（链接、按钮、输入框、图片，能点开的气泡、工具卡片这些样式上是手形指针）就只做它自己的事，不切换
      const tappable = el => !!el.closest?.('a, button, input, textarea, select, label, summary, video, audio, img, [role="button"], [contenteditable], .composer, .tools-slot')
        || getComputedStyle(el).cursor === 'pointer';
      const opt = { capture: true, passive: true };
      addEventListener('touchstart', e => {
        g = null;
        const log = scroller.value;
        if (e.touches.length !== 1 || !log?.contains(e.target) || stay() || typing()) return;
        // 落定到一半又按下：从眼下的位置接着跟手
        const el = els[0]?.el;
        if (el?.getAnimations().some(a => a.id === 'bar-y')) put(parseFloat(getComputedStyle(el).translate.split(' ')[1]) || 0);
        g = { x: e.touches[0].clientX, y: e.touches[0].clientY, last: 0, y1: 0, on: false, track: [],
              t0: e.timeStamp, stop: e.timeStamp - coast < 100, target: e.target };
      }, opt);
      addEventListener('touchmove', e => {
        if (!g || e.touches.length !== 1) return;
        const t = e.touches[0];
        if (!g.on) {
          const dx = t.clientX - g.x, dy = t.clientY - g.y;
          if (Math.abs(dx) < SLOP && Math.abs(dy) < SLOP) return;
          if (Math.abs(dy) <= Math.abs(dx)) { g = null; return; }   // 横着划（切标签页、开侧边栏、滚代码块）不管
          g.on = true; g.last = g.y1 = t.clientY;   // 从越过门槛那一刻起算
          measure();
          return;
        }
        const d = t.clientY - g.last;   // 手指往下为正：往上翻以前的内容，底下这截跟着往下走
        g.last = t.clientY;
        g.track.push([e.timeStamp, t.clientY]);   // 最近 0.1 秒的手指位置，松手时看往哪走
        while (g.track.length > 2 && e.timeStamp - g.track[0][0] > 100) g.track.shift();
        if (!d) return;
        if (F && d < 0) put(T, true, false);   // 淡掉的时候往上拖：先淡回来，再跟着手指冒
        const v = Math.max(0, Math.min(room(), T + (d < 0 ? d * REVEAL : d)));
        if (v !== T || (F && v >= full)) put(v, false, F && v < full);   // 淡掉的往下拖到整截都出了屏幕，撤掉淡掉
      }, opt);
      const lift = e => {
        if (!g || e.touches.length) return;
        const { on, track, y1, t0, stop, target } = g;
        g = null;
        if (!on) {
          if (e.type === 'touchend' && !stop && e.timeStamp - t0 < TAP_MS && !tappable(target)) toggle();   // 点一下切换
          return;
        }
        coast = e.timeStamp;
        const max = room();
        if (T <= 0 && !F) { hid = false; return; }
        if (T >= max) { hid = true; return; }   // 走到头了（或者被末尾顶着），就停在这儿
        // 照手指最后 0.1 秒往哪走落定（只看最后一下，抬手时手指的小回弹会带偏）；最后没动就看这一下整体往哪走，再不行落到近的那头
        const end = track.length ? track[track.length - 1][1] : y1;
        const recent = track.length > 1 ? end - track[0][1] : 0, dir = Math.abs(recent) > 2 ? Math.sign(recent) : Math.sign(end - y1);
        hid = dir > 0 || (!dir && T > max / 2);
        put(hid ? max : 0, true);
      };
      addEventListener('touchend', lift, opt);
      addEventListener('touchcancel', lift, opt);
      // 手指滚动和松手后接着的惯性记进 coast（跟着新消息往下走这类程序滚的不算）；藏着时惯性还在滚、程序滚（回到底部、跟着新消息走）：
      // 快到末尾被末尾顶上来，离开末尾再跟着回到藏着的位置
      addEventListener('scroll', e => {
        if (e.target !== scroller.value) return;
        if (g?.on || e.timeStamp - coast < 150) coast = e.timeStamp;
        if (!hid || g?.on) return;
        const v = room();
        if (v !== T || (F && v >= full)) put(v, false, F && v < full);
      }, opt);
      document.addEventListener('focusin', e => { if (e.target.closest?.('.composer, .tools-slot')) home(); });
      watch(stay, v => { if (v) home(); });
      watch(active, () => { hid = false; put(0, false, false); els = []; full = 0; });
    }
    // 全屏时按返回键，浏览器先拿去退出全屏，网页收不到这次返回（没有 popstate），也拦不住（实测）。
    // 所以把开着全屏开关时的退出当成一次返回：开着弹窗、侧边栏这些就收起最上面那层，下一次点页面回到全屏
    // （浏览器只认用户亲手点的那一下，没法当场回去，中间会露一下状态栏）；什么都没开（会话区）就只是退出全屏，点一下页面照样回去。
    // 关开关时 setFull 先把 full 置成关，不会走到这里；切到别的应用时浏览器也可能退出全屏，页面在后台时的退出不算返回
    if (canFull) document.addEventListener('fullscreenchange', () => {
      if (document.fullscreenElement || !full.value || document.hidden) return;
      back.closeTop();
    });
    return exposed;
  },
}).use({ install: app => (app.config.globalProperties.$who = who) })   // 所有模板里都能直接写 $who.me
  .directive('longpress', longPress)   // 账号行上右键、手机上长按：打开预定面板
  .directive('pin-swipe', pinSwipe)   // 侧边栏会话行往右划：置顶、取消置顶
  .mount('#app');
// 小标签、标签页里的字按字形中心居中（不认识 text-box 的浏览器才用得上）：等字体加载完再量，不然量的是后备字体
document.fonts.ready.then(() => fitGlyphs());
