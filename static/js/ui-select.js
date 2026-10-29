// 自己画的下拉框，页面上的下拉一律用它，不用原生 <select>（有的浏览器点一次原生下拉，
// 会接连弹出好几个选单，网页这边收到的事件一切正常，查不出也管不了，干脆不让浏览器弹原生选单）。
// options 是 [{ value, label, disabled, short }]（short 是按钮上的短写法，地方小时用，选单里照旧写 label）；v-model 绑值；选了和原来不一样的值才发 change（带新值），对应原生的 @change。
// 当前值不在选项里（比如还没选）时按钮上写 placeholder。class、aria-label 这些属性落在按钮上，各处原来给下拉写的样式照样用。
// 选单挂在 body 上（Teleport），按按钮的位置用 fixed 摆，下面放不下就往上开：放在弹窗、看板下拉面板里也不会被裁掉。
// 选单里的点击不往外冒：看板下拉、标签选择这些「点别处就收起」的监听不会把它当成点了别处，把外面的面板一起收掉
const { ref, computed, nextTick, onMounted, onUnmounted } = Vue;

let closeOpen = null;   // 同一时刻只开一个选单：开新的先收掉旧的

export const UiSelect = {
  inheritAttrs: false,
  props: {
    modelValue: null,
    options: { type: Array, default: () => [] },
    placeholder: { type: String, default: '' },
    disabled: Boolean,
  },
  emits: ['update:modelValue', 'change'],
  setup(props, { emit }) {
    // up：往上开（简洁模式的弹出动画据此从按钮那头长出来，见 style.css 的 ui-pop）
    const open = ref(false), btn = ref(null), list = ref(null), pos = ref({}), hi = ref(-1), up = ref(false);
    const current = computed(() => props.options.find(o => o.value === props.modelValue));
    // 按钮下面放不下（不到 200 像素）又比上面窄，就往上开；左右不出屏幕
    function place() {
      const r = btn.value.getBoundingClientRect(), below = innerHeight - r.bottom - 8, above = r.top - 8;
      up.value = below < 200 && above > below;
      pos.value = { left: `${Math.max(4, Math.min(r.left, innerWidth - 4 - Math.max(r.width, 160)))}px`, minWidth: `${r.width}px`,
                    maxHeight: `${Math.max(120, up.value ? above : below)}px`,
                    ...(up.value ? { bottom: `${innerHeight - r.top + 4}px` } : { top: `${r.bottom + 4}px` }) };
    }
    function close() {
      open.value = false;
      if (closeOpen === close) closeOpen = null;
    }
    function toggle() {
      if (props.disabled) return;
      if (open.value) return close();
      closeOpen?.();
      place();
      open.value = true;
      closeOpen = close;
      hi.value = props.options.findIndex(o => o.value === props.modelValue);
      nextTick(() => list.value?.querySelector('.on')?.scrollIntoView({ block: 'nearest' }));
    }
    function pick(o) {
      if (!o || o.disabled) return;
      close();
      btn.value?.focus();
      if (o.value === props.modelValue) return;
      emit('update:modelValue', o.value);
      emit('change', o.value);
    }
    // 键盘：上下键挪高亮（跳过不能选的），回车、空格选中或打开，Esc 收起
    function onKey(e) {
      if (props.disabled) return;
      if (e.key === 'Escape' && open.value) { e.preventDefault(); return close(); }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!open.value) return toggle();
        const n = props.options.length, step = e.key === 'ArrowDown' ? 1 : -1;
        for (let i = 1; i <= n; i++) {
          const j = (hi.value + step * i + n) % n;
          if (!props.options[j].disabled) { hi.value = j; break; }
        }
        nextTick(() => list.value?.querySelector('.hi')?.scrollIntoView({ block: 'nearest' }));
      } else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        open.value ? pick(props.options[hi.value]) : toggle();
      }
    }
    // 在按钮和选单以外按下就收起；页面滚动、窗口变大小时选单会和按钮错开，也收起（选单自己里面滚不算）
    const outside = e => { if (open.value && !btn.value?.contains(e.target) && !list.value?.contains(e.target)) close(); };
    const onScroll = e => { if (open.value && !list.value?.contains(e.target)) close(); };
    onMounted(() => {
      document.addEventListener('pointerdown', outside, true);
      addEventListener('scroll', onScroll, true);
      addEventListener('resize', close);
    });
    onUnmounted(() => {
      document.removeEventListener('pointerdown', outside, true);
      removeEventListener('scroll', onScroll, true);
      removeEventListener('resize', close);
      close();
    });
    return { open, btn, list, pos, hi, up, current, toggle, pick, onKey };
  },
  template: `
    <button ref="btn" type="button" v-bind="$attrs" class="ui-select" :class="{ open, empty: !current }" :disabled="disabled"
            aria-haspopup="listbox" :aria-expanded="open" @click="toggle" @keydown="onKey">
      <span class="ui-select-text">{{ current ? current.short || current.label : placeholder }}</span>
      <svg viewBox="0 0 16 16"><path d="M4 6l4 4 4-4"/></svg>
    </button>
    <!-- 进出的动画（样式里 html.simple 的 ui-pop） -->
    <Teleport to="body"><Transition name="ui-pop">
      <ul v-if="open" ref="list" class="ui-select-list" :class="{ up }" role="listbox" :style="pos" @click.stop>
        <li v-for="(o, i) in options" :key="String(o.value)" role="option" :aria-selected="o.value === modelValue"
            :aria-disabled="!!o.disabled" :class="{ on: o.value === modelValue, hi: i === hi, off: o.disabled }"
            @click="pick(o)" @pointerenter="hi = i">{{ o.label }}</li>
      </ul>
    </Transition></Teleport>`,
};
