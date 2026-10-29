// 选标签：新建会话里是一个下拉栏，「编辑标签」弹窗里直接摊开。点一下选上或取消，也能当场输入新标签；
// 每个标签带着自己的颜色，右键能换颜色
const { ref, computed, onMounted, onUnmounted, inject } = Vue;
import { tagStyle } from './tag-color.js';

export const TagPicker = {
  props: {
    modelValue: { type: Array, default: () => [] },
    options: { type: Array, default: () => [] },   // 已有的全部标签
    inline: Boolean,                                // 直接摊开，不做成下拉
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const open = ref(false);
    const typed = ref('');
    // 已有的标签，再加上这次刚新建、列表里还没有的
    const all = computed(() => [...new Set([...props.options, ...props.modelValue])]);
    const has = t => props.modelValue.includes(t);
    const toggle = t => emit('update:modelValue',
      has(t) ? props.modelValue.filter(x => x !== t) : [...props.modelValue, t]);
    function add() {
      const t = typed.value.trim().slice(0, 24);
      typed.value = '';
      if (t && !has(t)) emit('update:modelValue', [...props.modelValue, t]);
    }
    // 下拉栏点别处就收起（面板自己吃掉点击）
    const shut = () => (open.value = false);
    onMounted(() => window.addEventListener('click', shut));
    onUnmounted(() => window.removeEventListener('click', shut));
    const colorMenu = inject('tagColorMenu', () => {});
    return { open, typed, all, has, toggle, add, tagStyle, colorMenu };
  },
  template: `
    <div class="tag-picker" :class="{ inline, open }">
      <button v-if="!inline" type="button" class="tag-trigger" @click.stop="open = !open">
        <span v-for="t in modelValue" :key="t" class="tag" :style="tagStyle(t)">{{ t }}</span>
        <span v-if="!modelValue.length" class="placeholder">不加标签</span>
        <svg viewBox="0 0 16 16"><path d="M4 6l4 4 4-4"/></svg>
      </button>
      <div v-if="inline || open" class="tag-panel" @click.stop>
        <!-- 一项一行，圆点一直在，选中的右边打勾（和侧边栏的漏斗、标签菜单一样） -->
        <div v-if="all.length" class="tag-opts">
          <button v-for="t in all" :key="t" type="button" class="tag-opt" :class="{ on: has(t) }" :style="tagStyle(t)"
                  @click="toggle(t)" @contextmenu.prevent="colorMenu($event, t)">
            <i class="menu-dot"></i><span class="tag-opt-name">{{ t }}</span><svg v-if="has(t)" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></svg>
          </button>
        </div>
        <p v-else class="empty">还没有标签，在下面输入一个</p>
        <input v-model="typed" maxlength="24" placeholder="新建标签，回车添加" @keydown.enter.prevent="add">
      </div>
    </div>`,
};
