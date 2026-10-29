// 刻度尺（移植自 TurnCoder 的 minimap）和跟它共用一套动画的滚动控制
// 用户说的话一道长刻度，Claude 的正文一道短刻度，间距均匀；小三角对准屏幕上最上面那条；
// 点刻度平滑跳过去，悬停立刻预览。几何位置在重画时算好缓存，滚动时只移动三角
const { ref, reactive, watch, nextTick } = Vue;
import { clock, frameOnce, who } from './util.js';

// minPitch 是密度上限：刻度再多也不挤得比这更密，放不下的部分滑出窗口、在两端淡出
const MM = { pad: 7, gap: 8, tickH: 3, minH: 64, cursorH: 10, minPitch: 5 };
// 两种滚动的速度：时长在 min～max 毫秒之间，按距离每像素 perPx 毫秒；时长越短，中间匀速段越快
const SPEED = { bottom: { min: 110, max: 260, perPx: 0.09 }, tick: { min: 130, max: 320, perPx: 0.11 } };
// 两头缓加速减速、中间匀速，时长随距离在 110～260 毫秒之间，长距离跳转也不拖沓
const mmEase = t => {
  const r = 0.16, v = 1 / (1 - r);
  return t < r ? v * t * t / (2 * r) : t > 1 - r ? 1 - v * (1 - t) ** 2 / (2 * r) : v * (r / 2 + t - r);
};
// 用户说的话按字数定刻度长度：字数取对数再过 sigmoid，几个字到几千字都落在 min～max 之间，
// 特别长的不会伸出去，很短的也不会被挤得看不见；mid 字时正好一半长，s 越小越陡。
// max 改了要跟着改 style.css 里 .mm 的宽度和 .mm-view 的宽度
const USER_W = { min: 12, max: 28, mid: 50, s: 1.2 };
const userTickW = text => {
  // 上传文件的附注不算用户说的话
  const n = (text || '').replace(/\n*（通过网页上传了 \d+ 个文件）[\s\S]*$/, '').trim().length;
  const x = (Math.log(Math.max(1, n)) - Math.log(USER_W.mid)) / USER_W.s;
  return Math.round(USER_W.min + (USER_W.max - USER_W.min) / (1 + Math.exp(-x)));
};

// mine：折叠刻度盘，只给用户说的话画刻度，Claude 的那些不画，整条短很多
export function useMinimap({ scroller, rows, current, follow, loadEarlier, loadLater, loadHead, loadTail, trim, mine }) {
  const mm = reactive({ ticks: [], height: 0, total: 0, offset: 0, pitch: 0, tickH: 3, hit: 4,
                        cursor: null, view: null, preview: null });
  const farFromBottom = ref(false);
  const farFromTop = ref(false);
  let mmEls = [], mmTops = [], mmBots = [], mmAnim = 0;
  let atBottom = true;

  // 窄屏（手机）上刻度尺是隐藏的，那就干脆不生成、不逐帧更新，省下滚动时的开销
  const narrowQuery = matchMedia('(max-width: 768px)');
  const narrow = ref(narrowQuery.matches);
  narrowQuery.addEventListener('change', e => { narrow.value = e.matches; mmSoon(); });

  function renderMinimap() {
    const log = scroller.value;
    if (!log) return;
    fillUp(log);
    if (narrow.value) {
      if (mm.ticks.length) mm.ticks = [];
      return;
    }
    mmEls = [...log.querySelectorAll(mine?.value ? '.row.k-user' : '.row.k-user, .row.k-text')];
    const n = mmEls.length, base = log.getBoundingClientRect().top - log.scrollTop;
    const maxH = Math.max(MM.minH, Math.round(log.clientHeight * 0.7));
    const fitH = Math.min(maxH, Math.max(MM.minH, MM.pad * 2 + MM.tickH + Math.max(0, n - 1) * MM.gap));
    const pitch = n > 1 ? Math.max(MM.minPitch, (fitH - MM.pad * 2 - MM.tickH) / (n - 1)) : 0;
    // total 是所有刻度排开的总长，超过窗口高度时窗口只露出一段
    const total = MM.pad * 2 + MM.tickH + Math.max(0, n - 1) * pitch;
    const height = Math.min(maxH, Math.max(MM.minH, total));
    // 太密时刻度变细、点击区收窄：刻度之间总留一道缝，相邻的点击区也不重叠
    const tickH = n > 1 ? Math.max(2, Math.min(MM.tickH, Math.floor(pitch) - 1)) : MM.tickH;
    const rects = mmEls.map(el => el.getBoundingClientRect());
    mmTops = rects.map(r => r.top - base);
    mmBots = rects.map(r => r.bottom - base);
    const ticks = mmEls.map((el, i) => {
      const item = rows.value[+el.dataset.idx];
      return { item, y: n > 1 ? MM.pad + i * pitch : (height - tickH) / 2,
               w: item.kind === 'user' ? userTickW(item.text) : null,
               cls: [item.kind === 'user' ? 'mm-user' : 'mm-ai', item.sidechain && 'mm-side'] };
    });
    Object.assign(mm, { ticks, height, total, pitch, tickH, hit: n > 1 ? Math.max(0.5, (pitch - tickH) / 2) : 4, preview: null });
    mmCursor();
  }
  function mmCursor() {
    const log = scroller.value;
    if (narrow.value) return;
    if (!log || !mm.ticks.length) return (mm.cursor = null);
    const top = log.scrollTop, bottom = top + log.clientHeight;
    let i = mmTops.findIndex((t, k) => mmBots[k] > top && t < bottom);
    // 折叠时一屏里常常只有 Claude 的回复：对准这一轮开头用户说的那句（视口上方最近的一条）
    if (i < 0 && mine?.value) i = mmTops.findLastIndex(t => t < top);
    // 视口停在末尾空白处、没有对话可对准时，按滚动比例放
    mm.cursor = i >= 0 ? mm.ticks[i].y + mm.tickH / 2 - MM.cursorH / 2
      : MM.pad + top / Math.max(1, log.scrollHeight - log.clientHeight) * (mm.total - MM.pad * 2) - MM.cursorH / 2;
    // 这一屏盖住了哪几条：在刻度尺上画一段淡淡的区间，一眼看出自己站在整段对话的哪一截
    if (i >= 0) {
      let last = i;
      while (last + 1 < mmTops.length && mmTops[last + 1] < bottom) last++;
      mm.view = { y: mm.ticks[i].y, h: Math.max(mm.tickH, mm.ticks[last].y + mm.tickH - mm.ticks[i].y) };
    } else {
      mm.view = null;
    }
    // 放不下时窗口跟着三角滑动，让三角尽量停在窗口正中；到了两头就贴边
    mm.offset = Math.max(0, Math.min(mm.total - mm.height, mm.cursor + MM.cursorH / 2 - mm.height / 2));
  }
  const mmSoon = () => frameOnce(renderMinimap);
  watch(() => [current.value?.id, current.value?.gen, rows.value.length, mine?.value], () => nextTick(renderMinimap));
  window.addEventListener('resize', mmSoon);

  // 滚到 to；toEnd 时最后一帧重新量一次底部，滚动途中内容变长也能停在真正的底部
  function scrollAnim(to, toEnd = false, speed = SPEED.tick) {
    const log = scroller.value;
    if (!log) return;
    const from = log.scrollTop, dist = to - from;
    const finish = () => (log.scrollTop = toEnd ? log.scrollHeight : to);
    cancelAnimationFrame(mmAnim);
    if (Math.abs(dist) < 2 || matchMedia('(prefers-reduced-motion: reduce)').matches) return finish();
    const dur = Math.max(speed.min, Math.min(speed.max, Math.abs(dist) * speed.perPx)), t0 = performance.now();
    const step = now => {
      const t = (now - t0) / dur;
      if (t >= 1) return finish();
      log.scrollTop = from + dist * mmEase(t);
      mmAnim = requestAnimationFrame(step);
    };
    mmAnim = requestAnimationFrame(step);
  }
  function mmJump(i) {
    const log = scroller.value, el = mmEls[i];
    if (!log || !el) return;
    const target = () => Math.max(0, Math.min(log.scrollHeight - log.clientHeight,
      log.scrollTop + el.getBoundingClientRect().top - log.getBoundingClientRect().top));
    scrollAnim(target());
    // 屏幕外的行先按估算高度占位，滚过去之后真实高度才出来，所以再校正几次
    let n = 0;
    const fix = () => {
      if (n++ > 6) return;
      const to = target();
      if (Math.abs(to - log.scrollTop) < 2) return;
      log.scrollTop = to;
      requestAnimationFrame(fix);
    };
    setTimeout(() => requestAnimationFrame(fix), 280);
  }
  // 点在刻度上就跳到那条；点在两端空白处，按间距换算成最近的那一条（算上窗口滑动的距离）
  function mmClick(e) {
    const tick = e.target.closest('.mm-tick');
    if (tick) return mmJump(+tick.dataset.i);
    const y = e.clientY - e.currentTarget.getBoundingClientRect().top + mm.offset;
    const i = mm.pitch ? Math.round((y - MM.pad - mm.tickH / 2) / mm.pitch) : 0;
    mmJump(Math.max(0, Math.min(mm.ticks.length - 1, i)));
  }
  function mmHover(e) {
    const tick = e.target.closest('.mm-tick');
    if (!tick) return (mm.preview = null);
    const { item, y } = mm.ticks[+tick.dataset.i];
    const text = (item.text || '').trim() || (item.image ? '（图片）' : '');
    mm.preview = { me: item.kind === 'user', y: y - mm.offset + mm.tickH / 2, text: text.length > 400 ? text.slice(0, 400) + '…' : text,
                   head: `${item.kind === 'user' ? who.me : 'Claude'} · ${clock(item.ts)}` };
  }

  // 自动滚动：停在底部时跟随新内容；离底部远了就浮出「回到底部」按钮
  function measure(el) {
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
    atBottom = gap < 80;
    // 手上这批底下卸掉过（tabs.js 的 cut）：贴着这批的底也还没到会话末尾，「回到底部」照样浮出来
    farFromBottom.value = gap > 400 || !!current.value?.cut;
    farFromTop.value = el.scrollTop > 400 || !!current.value?.offset;
    // 离开了最顶上：窄屏顶上那几个圆按钮淡入一圈细边（style.css 的 html.log-away-top），换了标签页也跟着这一份重算
    document.documentElement.classList.toggle('log-away-top', el.scrollTop > 2);
  }
  // 刚才是不是在往上翻：手机上「回到顶部」「回到底部」只在往上翻时浮出来，往下滚或停下 2.5 秒就收起（样式在窄屏那一段）。
  // 页面高度变了的那一下不算（往前接了内容会把视口扶下去、底下来了新内容），只认用户自己滚的。
  // 离底部的距离也要跟着变才算：手机上输入框落回、对话区变高时，浏览器把滚动位置往回收一截，顶上近了、离底部却没变，
  // 以前当成往上翻，「回到顶部」平白浮出来
  const scrollUp = ref(false);
  let lastEl = null, lastTop = 0, lastH = 0, lastGap = 0, upTimer = 0;
  function trackDirection(el) {
    const top = el.scrollTop, h = el.scrollHeight, gap = h - top - el.clientHeight;
    if (el === lastEl && h === lastH) {
      if (top < lastTop - 2 && gap > lastGap + 2) {
        scrollUp.value = true;
        clearTimeout(upTimer);
        upTimer = setTimeout(() => (scrollUp.value = false), 2500);
      } else if (top > lastTop + 2 && gap < lastGap - 2) {
        scrollUp.value = false;
        clearTimeout(upTimer);
      }
    }
    lastEl = el; lastTop = top; lastH = h; lastGap = gap;
  }
  const onScroll = e => {
    measure(e.target);
    trackDirection(e.target);
    if (current.value) current.value.lastTop = e.target.scrollTop;   // 切走后内容被释放了，回来时按它还原位置
    frameOnce(mmCursor);
    const el = e.target;
    if (el.scrollTop < 600) fetchEarlier();   // 快翻到顶了，先把更早的取回来
    else if (el.scrollHeight - el.scrollTop - el.clientHeight < 600) fetchLater();   // 快翻到这批的底了，底下卸掉过就接上
  };
  // 往前面接内容会把后面的内容整个推下去，所以补完要按「离底部多远」把视口扶回原处。
  // 新接上的行高度是慢慢定下来的（图片、代码上色），多扶几次才稳
  async function fetchEarlier() {
    const tab = current.value, el = scroller.value;
    if (!tab || !el || !tab.offset || tab.earlier) return;
    const gap = el.scrollHeight - el.scrollTop;
    if (!(await loadEarlier(tab))) return;
    for (let i = 0; i < 4; i++) {
      await nextTick();
      el.scrollTop = el.scrollHeight - gap;
      await new Promise(r => requestAnimationFrame(r));
    }
    // 手上这批长过上限了：卸掉底下离视口够远的那截（tabs.js 的 trim、下面的 farSeq）。卸底下不挪上面的内容，视口不用扶
    if (trim(tab, 'bottom', farSeq(el, 'bottom'))) {
      await nextTick();
      measure(el);
    }
    renderMinimap();   // 里面会再看一眼，还没补满就接着补
    // 新接上的这段折叠后可能很矮，扶完视口还贴在顶上、甚至一点没动（没动就没有滚动事件），那就接着补
    if (el.scrollTop < 600) fetchEarlier();
  }
  // 视口正中（找不到就是视口下半截第一行）是哪一行：卸掉顶上的内容前记下它在哪，卸完照它把视口扶回去
  function anchorRow(el) {
    const box = el.getBoundingClientRect(), mid = box.top + box.height / 2;
    return [...el.querySelectorAll(':scope > .log-body > .row')].find(r => r.getBoundingClientRect().bottom > mid) || null;
  }
  const nextFrame = () => new Promise(r => requestAnimationFrame(r));
  // 卸到哪里为止（交给 tabs.js 的 trim）：只卸离视口 FAR 像素以外的。卸底下给第一行顶边超出视口底边 FAR 的那行的序号（它起往后都卸），
  // 卸顶上给第一行底边还在视口顶边上方 FAR 以内的那行的序号（它之前的都卸）；没有够远的行是 null，不卸。
  // 卸完两头都还剩至少 FAR 像素，比触发取段的 600 像素远，卸完不会马上又去取刚卸掉的那头
  const FAR = 1500;
  function farSeq(el, end) {
    const box = el.getBoundingClientRect();
    const rows = [...el.querySelectorAll(':scope > .log-body > .row[data-seq]')];
    if (end === 'bottom') {
      const r = rows.find(r => r.getBoundingClientRect().top > box.bottom + FAR);
      return r ? +r.dataset.seq : null;
    }
    const keep = rows.find(r => r.getBoundingClientRect().bottom > box.top - FAR);
    return keep && keep !== rows[0] ? +keep.dataset.seq : null;
  }
  // 往下翻到手上这批的底了，而底下卸掉过（cut）：接上后面一段，再卸掉顶上多出来的。卸顶上会把下面的内容整个往上提，
  // 所以卸之前记下视口里的一行在哪，卸完把滚动位置反着补回去（行高是慢慢定下来的，多扶两次）。
  // 换的时候挂着 shift：这些不是新消息，下面两个 watch 不跟到底、不做淡入
  async function fetchLater() {
    const tab = current.value, el = scroller.value;
    if (!tab || !el || !tab.cut || tab.later) return;
    tab.shift = true;
    try {
      if (!(await loadLater(tab))) return;
      await nextTick();
      const row = anchorRow(el), top = row?.getBoundingClientRect().top;
      if (trim(tab, 'top', farSeq(el, 'top')) && row) {
        for (let i = 0; i < 3; i++) {
          await nextTick();
          const d = row.isConnected ? row.getBoundingClientRect().top - top : 0;
          if (Math.abs(d) >= 1) el.scrollTop += d;
          await nextFrame();
        }
      }
    } finally {
      await nextFrame();
      tab.shift = false;
    }
    measure(el);
    renderMinimap();
    // 新接上的这段折叠后可能很矮，视口还贴着底，接着接
    if (tab.cut && el.scrollHeight - el.scrollTop - el.clientHeight < 600) fetchLater();
  }
  // 末尾这一截撑不满一屏（或者只比一屏高一点）时页面滚不起来，就不会有滚动事件去触发往上补：
  // 内容一变就看一眼，能滚的余量不到 600 像素就接着补，补到能滚过这条线或者上面没有了为止。
  // 底下卸掉过的，离这批的底不到 600 像素也一样去接
  function fillUp(el) {
    if (current.value?.offset && el.scrollHeight - el.clientHeight < 600) fetchEarlier();
    else if (current.value?.cut && el.scrollHeight - el.scrollTop - el.clientHeight < 600) fetchLater();
  }
  // 保险：页面滚不动时滚轮也不会带来滚动事件，在顶上往上滚就直接去补（比如上一次补失败了）；在底下往下滚同样去接
  const onWheel = e => {
    const el = e.currentTarget;
    if (e.deltaY < 0 && el.scrollTop < 600) fetchEarlier();
    else if (e.deltaY > 0 && el.scrollHeight - el.scrollTop - el.clientHeight < 600) fetchLater();
  };
  function scrollToEnd() {
    nextTick(() => scroller.value && (scroller.value.scrollTop = scroller.value.scrollHeight));
  }
  // 用户自己发了话：不管刚才滚到哪儿，都跟到底部去（手上这批不在末尾就先换成末尾那段）
  function stickToBottom() {
    atBottom = true;
    if (current.value?.cut) return backToBottom();
    scrollToEnd();
  }
  // 回到底部：手上这批底下卸掉过（cut），先换成会话末尾那一段，换好直接停在最底下（中间隔着几千条，滚过去没有意义）
  async function backToBottom() {
    const tab = current.value, el = scroller.value;
    if (!tab || !el) return;
    atBottom = true;
    if (tab.cut) {
      tab.shift = true;
      try {
        if (!(await loadTail(tab))) return;
        await nextTick();
        el.scrollTop = el.scrollHeight;
      } finally {
        await nextFrame();
        tab.shift = false;
      }
      measure(el);
      renderMinimap();
      return;
    }
    scrollAnim(el.scrollHeight - el.clientHeight, true, SPEED.bottom);
  }
  // 回到顶部：上面还有没取回来的，换成会话开头那一段（tabs.js 的 loadHead，以前是一次全取回来），再停在最顶上
  async function backToTop() {
    const tab = current.value, el = scroller.value;
    if (!tab || !el) return;
    atBottom = false;
    if (tab.offset) {
      tab.shift = true;
      try {
        if (await loadHead(tab)) {
          await nextTick();
          el.scrollTop = 0;
          measure(el);
          renderMinimap();
          return;
        }
      } finally {
        await nextFrame();
        tab.shift = false;
      }
    }
    scrollAnim(0, false, SPEED.bottom);
  }
  // 换了一份内容：刻度尺和「回到底部」跟着这一份重新算
  function recalibrate() {
    const el = scroller.value;
    if (!el) return;
    measure(el);
    renderMinimap();
  }
  // 只在同一个会话里来了新内容时才跟。换了会话不算：atBottom 那时还是上一个会话的，拿它来判断会把刚切过来的会话
  // 硬拉到底，丢掉用户在那里看到的位置
  // 手上这批底下卸掉过（cut）时贴着的只是这批的底，不跟；刚接上、卸掉、换掉一段（shift）也不是新内容，不跟
  watch(() => [current.value?.id, current.value?.items.length, current.value?.running], ([id], [was]) => {
    if (id === was && follow.value && atBottom && !current.value?.cut && !current.value?.shift) scrollToEnd();
  });
  // 不在底部时扶住眼前的内容（手机 Firefox 上往上翻着看时，底下来了新的工具调用，眼前的内容会被往下扯一段，
  // 在 Chromium、桌面 Firefox 里都没复现出来是哪一步扯的）：更新前记下对话区正中间是哪一行、在哪，更新完它要是挪了，
  // 就把滚动位置反着补回去，在这一帧画出来之前做完。浏览器自己的滚动锚定已经扶好的话补的量是 0。
  // 取正中间那一点：顶上压着标题栏、底下压着输入框。更新后过几帧才变高的（图片晚到）管不到
  // 同一处顺带给新冒出来的行一个淡入、往上浮一点的动画：更新前记下对话里已有的行，
  // 更新后排在第一个旧行后面、又不是旧行的就是新来的（排在它前面的是往上补的更早内容，不动）。
  // 用户自己的话不动：发出前那颗「发送中」气泡已经浮上来过了，记录到了再淡入一次会闪
  watch(() => [current.value?.id, current.value?.items.length], ([id, n], [was, m]) => {
    const el = scroller.value, body = el?.querySelector(':scope > .log-body');
    // 按需接上、卸掉、换掉一段（shift）由 fetchLater 这些自己扶视口，接上来的是旧内容，也不做新消息的淡入
    if (id !== was || !el || !body || current.value?.shift) return;
    const old = n > m && !matchMedia('(prefers-reduced-motion: reduce)').matches ? new Set(body.children) : null;
    // 停在底部、要跟到底的：记下原来最后一行在哪，跟到底以后旧内容不是一下子跳上去，而是带一点回弹滑上去
    // （和权限栏弹出卡片同一条曲线，免得旧内容瞬移、只有新内容淡入）
    const last = old && atBottom && follow.value ? body.lastElementChild : null, lastTop = last?.getBoundingClientRect().top;
    let row = null, top = 0;
    if (!atBottom) {
      const box = el.getBoundingClientRect();
      row = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)?.closest('.log-body > .row');
      if (row && el.contains(row)) top = row.getBoundingClientRect().top;
      else row = null;
    }
    nextTick(() => {
      if (row?.isConnected && !atBottom) {
        const d = row.getBoundingClientRect().top - top;
        if (Math.abs(d) >= 1) el.scrollTop += d;
      }
      if (!old) return;
      // 这时「跟到底」（上面那个 watch 排的 nextTick）已经先做完了
      const lift = last?.isConnected ? last.getBoundingClientRect().top - lastTop : 0;
      if (lift < -1) body.animate([{ transform: `translateY(${-lift}px)` }, { transform: 'none' }],
                                  { duration: 420, easing: 'cubic-bezier(0.3, 1.45, 0.55, 1)' });
      let seen = false;
      for (const c of body.children) {
        if (old.has(c)) { seen = true; continue; }
        if (seen && c.classList.contains('row') && !c.classList.contains('k-user')) {
          c.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 220, easing: 'ease-out' });
        }
      }
    });
  }, { flush: 'pre' });

  return { mm, mmSoon, mmClick, mmHover, onScroll, onWheel, farFromBottom, farFromTop, scrollUp, backToTop, backToBottom, scrollToEnd,
           stickToBottom, recalibrate, fetchEarlier, fetchLater };
}
