// 右键菜单（安卓上长按、iPhone 和 iPad 上双击）：气泡和工具卡片的快捷操作都挂在这里
const { reactive, nextTick } = Vue;
import { copyText, touch, ios, say, toast } from './util.js';

export function useMenu({ quote, refill }) {
  // items 为空就是没打开；x/y 是菜单左上角，打开后再按视口边界修正。底部提示条是公共的（util.js 的 say）
  const menu = reactive({ items: [], x: 0, y: 0 });
  function close() { menu.items = []; }

  function open(e, items) {
    if (!items.length) return;
    e.preventDefault();
    Object.assign(menu, { items, x: e.clientX, y: e.clientY });
    // 贴着边打开时会伸出屏幕，量出真实尺寸再掰回来
    nextTick(() => {
      const el = document.querySelector('.ctx-menu');
      if (!el) return;
      const r = el.getBoundingClientRect();
      if (r.right > innerWidth - 8) menu.x = Math.max(8, innerWidth - r.width - 8);
      if (r.bottom > innerHeight - 8) menu.y = Math.max(8, innerHeight - r.height - 8);
    });
  }

  // 安卓上长按自己计时（浏览器自己的长按要按半秒以上才发右键事件，太慢）：按住 LONG_MS 不动就弹，
  // 手指挪开 10px 以上算在滚动，不弹。浏览器随后照样会发一次右键事件，那次只拦下默认动作（不然它去选字）、不再弹第二次；
  // 早于浏览器长按就松手的话，后面会跟一次点击，也吞掉，不然菜单一出来就被它收掉、气泡还当成点了一下。
  // 在手机上试过，200ms 跟手
  const LONG_MS = 200;
  let press = null, firedAt = 0, swallow = false;
  const unpress = () => { if (press) clearTimeout(press.timer); press = null; };
  window.addEventListener('click', e => {
    if (!swallow) return;
    swallow = false;
    e.stopPropagation();
    e.preventDefault();
  }, true);
  // 触摸的几个监听写成被动的（键名后缀 Passive，Vue 认这个写法）：满屏气泡都挂着，不被动的话浏览器每次滚动前都要等它们
  function longTouch(handler) {
    return {
      contextmenu: e => (Date.now() - firedAt < 2000 ? e.preventDefault() : handler(e)),
      touchstartPassive: e => {
        // 里外两层都挂着菜单时，手指底下最里面那层说了算（事件往外冒，里层先到）
        if (e._menuPress) return;
        e._menuPress = true;
        unpress();
        if (e.touches.length !== 1) return;
        const { clientX, clientY } = e.touches[0], target = e.target, currentTarget = e.currentTarget;
        press = { x: clientX, y: clientY, timer: setTimeout(() => {
          press = null;
          firedAt = Date.now();
          swallow = true;
          handler({ target, currentTarget, clientX, clientY, preventDefault() {}, stopPropagation() {} });
        }, LONG_MS) };
      },
      touchmovePassive: e => {
        const t = e.touches[0];
        if (press && Math.hypot(t.clientX - press.x, t.clientY - press.y) > 10) unpress();
      },
      touchendPassive: () => { unpress(); if (swallow) setTimeout(() => (swallow = false), 400); },
      touchcancelPassive: unpress,
    };
  }

  // 点别处、滚动、按 Esc 都收起（菜单自己吃掉点击，不会自己关掉自己）
  window.addEventListener('click', close);
  window.addEventListener('scroll', close, true);
  window.addEventListener('keydown', e => e.key === 'Escape' && close());

  const api = {
    menu, open, close, say,
    async copy(text, what = '') {
      close();
      const ok = await copyText(text);
      if (ok) say(`已复制${what}`);
      else say('复制失败：浏览器禁止写入剪贴板', { err: true });
      return ok;
    },
    quote(text) {
      close();
      quote(text);
      say('已引用到输入框');
    },
    refill(text) {
      close();
      refill(text);
      say('已放回输入框');
    },
    // 电脑上右键；手机上按系统只留一种（安卓上双击是选字复制，菜单弹出来会挡着）：
    // 安卓长按（浏览器发右键事件），双击留给浏览器选字；iPhone、iPad 长按不发右键事件、归系统选字，只能双击
    // （双击还要靠样式挡住浏览器自己的放大）
    on(handler) {
      if (!touch) return { contextmenu: handler };
      return ios ? { contextmenu: handler, dblclick: handler } : longTouch(handler);
    },
  };
  return api;
}

export const ContextMenu = {
  props: ['api'],
  computed: { menu() { return this.api.menu; }, toast() { return toast; } },
  methods: {
    pick(item) { item.run(); },
  },
  template: `
    <teleport to="body">
      <div v-if="menu.items.length" class="ctx-menu dd-panel" :style="{ left: menu.x + 'px', top: menu.y + 'px' }"
           @click.stop @contextmenu.prevent>
        <!-- dot 是标签色点的色相；stateDot 是侧边栏的状态点（unread、attention、run，样子同侧边栏会话行左边那颗）；
             check 是选中的那项，右边打勾（侧边栏的漏斗、标签筛选）；
             tone 给要小心的项上色：warn 橙（关闭其他标签页）、danger 红（删除会话） -->
        <button v-for="(it, i) in menu.items" :key="i" class="dd-item" :class="[{ checked: it.check }, it.tone && 'tone-' + it.tone]" @click="pick(it)">
          <span><i v-if="it.stateDot" class="menu-state" :class="it.stateDot"></i><i v-else-if="it.dot != null" class="menu-dot"
                :style="{ '--h': it.dot }"></i>{{ it.label }}</span>
          <small v-if="it.hint">{{ it.hint }}</small>
          <svg v-if="it.check" class="menu-check" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></svg>
        </button>
      </div>
      <div v-if="toast.text" class="ctx-tip" :class="{ err: toast.err }" role="status">{{ toast.text }}</div>
    </teleport>`,
};
