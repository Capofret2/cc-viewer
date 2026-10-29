// 工具卡片：请求气泡里一行灰字细条，点那一行或眼睛就在细条底下展开一张卡片（命令、对比、结果），再点收起
import { vMd, whyNot } from './markdown.js';

// 工具卡片头上写的那句（导出给 item-row.js：工作过程里收成一行的一步，没有文字时拿第一个工具的这句）
export const TOOL_HEAD = {
  Bash: i => i.description || i.command,
  Read: i => i.file_path, Write: i => i.file_path, Edit: i => i.file_path,
  NotebookEdit: i => i.notebook_path,
  Grep: i => `${i.pattern}  ${i.path || ''}`, Glob: i => i.pattern,
  WebFetch: i => i.url, WebSearch: i => i.query, ToolSearch: i => i.query,
  Agent: i => i.description, Task: i => i.description, Skill: i => i.skill,
};

const DiffView = {
  props: ['before', 'after'],
  computed: {
    lines() {
      return Diff.diffLines(this.before || '', this.after || '').flatMap(part => {
        const sign = part.added ? '+' : part.removed ? '-' : ' ';
        let rows = part.value.replace(/\n$/, '').split('\n');
        if (sign === ' ' && rows.length > 6) rows = [...rows.slice(0, 3), '⋯', ...rows.slice(-3)];
        return rows.map(text => ({ sign, text }));
      });
    },
  },
  template: `<pre class="diff"><div v-for="(l, i) in lines" :key="i" :class="'d' + l.sign">{{ l.sign }} {{ l.text }}</div></pre>`,
};

// 工具名左边那个小图标：都是 16×16 的线条图，跟站里其他图标一路画法，不用表情符号（也导出给收成一行的一步，写成「图标×次数」）
export const TOOL_ICON = {
  Bash: 'M3.5 5L6 7.5 3.5 10M7.5 11.5h5',                                  // 终端提示符
  Read: 'M4 2.5h5l3 3v8H4zM9 2.5v3h3',                                     // 文件
  Write: 'M3.5 12.5l.8-2.6 6.4-6.4 1.8 1.8-6.4 6.4zM9.5 4.5l2 2',          // 笔
  Edit: 'M3.5 12.5l.8-2.6 6.4-6.4 1.8 1.8-6.4 6.4zM9.5 4.5l2 2',
  MultiEdit: 'M2.5 11.5l.7-2.2 5.6-5.6 1.5 1.5-5.6 5.6zM11 9.5l2.5 2.5M11 12l2.5-2.5',
  NotebookEdit: 'M4 2.5h8v11H4zM6.5 2.5v11M8.5 6h2M8.5 9h2',               // 笔记本
  Grep: 'M7 10.5a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM9.6 9.6l3 3',            // 放大镜
  Glob: 'M8 3v10M3.7 5.5l8.6 5M12.3 5.5l-8.6 5',                           // 通配星号
  WebFetch: 'M8 2.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM2.5 8h11M8 2.5c1.6 1.8 1.6 9.2 0 11M8 2.5c-1.6 1.8-1.6 9.2 0 11',
  WebSearch: 'M7 10.5a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM9.6 9.6l3 3M3.5 7h7',
  ToolSearch: 'M7 10.5a3.5 3.5 0 100-7 3.5 3.5 0 000 7zM9.6 9.6l3 3M3.5 7h7',
  Agent: 'M4 4h3.2v3.2H4zM8.8 4H12v3.2H8.8zM4 8.8h3.2V12H4zM8.8 8.8H12V12H8.8',   // 分头去做
  Task: 'M4 4h3.2v3.2H4zM8.8 4H12v3.2H8.8zM4 8.8h3.2V12H4zM8.8 8.8H12V12H8.8',
  TodoWrite: 'M3 5l1.4 1.4L7 3.8M3 11.2l1.4 1.4L7 10M9.5 5.2h3.5M9.5 11.4h3.5',   // 清单打勾
  Skill: 'M9 2.5L4.5 9H7.5l-.5 4.5L11.5 7H8.5z',                           // 闪电
};
export const ICON_OTHER = 'M8 2.5l5 2.8v5.4L8 13.5 3 10.7V5.3z';   // 认不出的工具：一个六边形

// 一段时长写成人话：56 毫秒 / 3.3 秒 / 2 分 5 秒
// Bash 跑得快（不到 10 秒）就不写用时，把地方让给中文描述
const SPENT_MIN = 10000;
function spentText(ms) {
  if (!(ms >= 0)) return '';
  if (ms < 1000) return `${ms} 毫秒`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒`;
  const m = Math.floor(ms / 60000);
  return `${m} 分 ${Math.round((ms - m * 60000) / 1000)} 秒`;
}

const PEEK_EDGE = 8;   // 点开的卡片离对话区底边至少留这么多

// Edit / MultiEdit 加几减几，Write 加几；和展开后的对比用同一套逐行比对。
// 逐行比对不便宜，算过的按条目记住：条目是冻结不变的，弱引用表会跟着条目一起释放
const deltaCache = new WeakMap();
function deltaOf(item) {
  if (deltaCache.has(item)) return deltaCache.get(item);
  const value = countDelta(item, item.input || {});
  deltaCache.set(item, value);
  return value;
}
function countDelta(item, input) {
  const count = s => (s ? s.replace(/\n$/, '').split('\n').length : 0);
  if (item.name === 'Write') return { add: count(input.content) };
  const edits = item.name === 'Edit' ? [input] : item.name === 'MultiEdit' ? input.edits || [] : [];
  if (!edits.length) return null;
  let add = 0, del = 0;
  for (const e of edits) {
    for (const part of Diff.diffLines(e.old_string || '', e.new_string || '')) {
      if (part.added) add += part.count;
      else if (part.removed) del += part.count;
    }
  }
  return { add, del };
}

// 结果区的标题：只有图没有字的（Read 读图片）不写「1 行」
const resultHead = r => {
  const head = [r.error ? '出错了' : '结果'];
  if (r.text || !r.images) head.push(`${r.text.split('\n').length} 行`);
  if (r.images) head.push(`${r.images.length} 张图`);
  return head.join(' · ');
};

const kb = n => n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)}MB` : `${Math.max(1, Math.round(n / 1024))}KB`;

// 工具结果里的图：一张一行折叠条，写着格式、尺寸、大小。用户点开那一张才生成图片去取，没点开一个字节都不传；
// 取过的由浏览器缓存（接口给了长期缓存），再收起展开不会重传
const ResultShots = {
  props: ['images'],
  data: () => ({ shown: {}, why: {} }),
  methods: {
    label(im) {
      return ['图片', (im.type || '').replace('image/', '').toUpperCase(),
              im.w && `${im.w}×${im.h}`, im.bytes && kb(im.bytes)].filter(Boolean).join(' · ');
    },
    onToggle(i, e) { if (e.target.open) this.shown[i] = true; },
    async onError(i, im) { this.why[i] = await whyNot(im.url); },
  },
  template: `
    <div class="shots">
      <template v-for="(im, i) in images" :key="i">
        <small v-if="!im.url" class="shot-err">图片无法显示：{{ im.why }}</small>
        <details v-else class="shot" @toggle="onToggle(i, $event)">
          <summary>{{ label(im) }}</summary>
          <small v-if="why[i]" class="shot-err">图片无法显示：{{ why[i] }}</small>
          <img v-else-if="shown[i]" :src="im.url" @error="onError(i, im)">
        </details>
      </template>
    </div>`,
};

export const ToolCard = {
  props: ['item', 'result'],
  components: { DiffView, ResultShots },
  directives: { md: vMd },
  inject: { menu: { default: null } },
  // open：卡片点开着；sinking：正在收起，细条那一行先挪回原位、写全的描述缩回去，外层才量得到收起的高度。
  // resultOpen：结果区被用户点着收起 / 展开过；没点过时出错就展开（点开卡片时一律展开）
  data: () => ({ open: false, resultOpen: null, sinking: false }),
  methods: {
    // 点开的卡片要是有一截掉在屏幕外，就把对话区往下带一点；
    // 但顶部不能带出去，宁可露不全也要让卡片的开头还看得见
    reveal(card) {
      const log = this.$el.closest('.log');
      if (!card || !log) return;
      const r = card.getBoundingClientRect(), box = log.getBoundingClientRect();
      const over = r.bottom - box.bottom + PEEK_EDGE;      // 下面超出了多少
      const room = r.top - box.top - PEEK_EDGE;            // 上面还能让出多少
      const by = Math.min(over, Math.max(0, room));
      if (by > 2) log.scrollBy({ top: by, behavior: 'smooth' });
    },
    // 点细条那一行：不让 details 自己展开，和点眼睛一样（点一下就展开，不用先选中）
    onSummary(e) {
      e.preventDefault();
      this.toggle();
    },
    // 点开：卡片就地出现，外层从细条高平滑长到「细条 + 卡片」，把后面的内容推开；
    // 收起：外层缩回细条高，后面的内容跟上来，缩完卡片才拿掉
    toggle() {
      clearTimeout(this.pinTimer);
      const wrap = this.$el;
      if (this.open) {
        wrap.style.height = `${wrap.getBoundingClientRect().height}px`;
        wrap.style.overflow = 'hidden';
        this.sinking = true;
        // 等描述缩回去再量细条，量到的才是收起后的高度，不然缩完还会再跳一下
        this.$nextTick(() => {
          const to = this.$refs.card.getBoundingClientRect().height;
          void wrap.offsetHeight;   // 先坐实起点，不然和下一步同帧，动画会被跳过
          wrap.style.transition = 'height 0.2s ease';
          wrap.style.height = `${to}px`;
          this.pinTimer = setTimeout(() => {
            this.open = this.sinking = false;
            wrap.style.height = wrap.style.transition = wrap.style.overflow = '';
          }, 220);
        });
        return;
      }
      const strip = this.$refs.card.getBoundingClientRect().height;
      this.resultOpen = true;   // 点开就连结果一起看
      this.open = true;
      this.$nextTick(() => {
        const full = wrap.getBoundingClientRect().height;
        wrap.style.height = `${strip}px`;
        wrap.style.overflow = 'hidden';
        void wrap.offsetHeight;
        wrap.style.transition = 'height var(--lift-ms) var(--lift-ease)';
        wrap.style.height = `${full}px`;
        this.pinTimer = setTimeout(() => {
          wrap.style.height = wrap.style.transition = wrap.style.overflow = '';
          this.reveal(this.$refs.slimCard);
        }, 260);
      });
    },
    onResultToggle(e) { this.resultOpen = e.target.open; },
    resultHead,
    // 工具卡片上的菜单：有什么就给什么，最后总兜底一个完整输入参数
    onMenu(e) {
      const i = this.input, items = [];
      const path = i.file_path || i.notebook_path || i.path;
      if (i.command) items.push({ label: '复制命令', run: () => this.menu.copy(i.command, '命令') });
      if (path) items.push({ label: '复制文件路径', run: () => this.menu.copy(path, '路径') });
      if (this.result?.text) {
        items.push({ label: this.result.error ? '复制错误信息' : '复制结果',
                     hint: `${this.result.text.split('\n').length} 行`,
                     run: () => this.menu.copy(this.result.text, '结果') });
      }
      items.push({ label: '复制输入参数', hint: 'JSON', run: () => this.menu.copy(this.json, '参数') });
      this.menu.open(e, items);
    },
  },
  computed: {
    // 电脑上右键；手机上安卓长按、iPhone 和 iPad 双击（menu.js 的 on）
    menuEvents() { return this.menu ? this.menu.on(this.onMenu) : {}; },
    showResult() { return this.resultOpen ?? !!this.result?.error; },
    input() { return this.item.input || {}; },
    icon() { return TOOL_ICON[this.item.name] || ICON_OTHER; },
    head() { return (TOOL_HEAD[this.item.name] || (() => ''))(this.input); },
    status() { return !this.result ? 'wait' : this.result.error ? 'err' : 'ok'; },
    json() { return JSON.stringify(this.input, null, 2); },
    // Read 读了几行，不用展开就能看到；只读了一部分时带上文件总行数
    lines() {
      const l = this.result?.lines;
      if (!l) return '';
      return l.total && l.total !== l.num ? `${l.num} 行 / 共 ${l.total}` : `${l.num} 行`;
    },
    // Bash 右边写这条命令跑了多久：工具调用的时间到结果写回记录的时间，不到 10 秒不写
    spent() {
      if (this.item.name !== 'Bash' || !this.item.ts || !this.result?.ts) return '';
      const ms = new Date(this.result.ts) - new Date(this.item.ts);
      return ms >= SPENT_MIN ? spentText(ms) : '';
    },
    // Edit / MultiEdit 加几减几，Write 加几；算过的按条目记住
    delta() { return deltaOf(this.item); },
  },
  // 细条用 details 只是为了 summary 的读屏和键盘语义，展开由 onSummary 接管，details 自己永远不开；
  // 眼睛在最右边，和点那一行一样
  template: `
    <div class="tool-wrap" :class="[status, { open, sinking, bash: item.name === 'Bash' }]" v-on="menuEvents">
      <button type="button" class="peek-btn" aria-label="展开" @click.prevent.stop="toggle">
        <svg viewBox="0 0 16 16"><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>
      </button>
      <details ref="card" class="tool" :class="status">
        <summary @click="onSummary"><small v-if="lines" class="lines">{{ lines }}</small><small v-else-if="delta" class="lines"><span class="plus">+{{ delta.add }}</span><span v-if="'del' in delta" class="minus"> −{{ delta.del }}</span></small><small v-else-if="spent" class="lines">{{ spent }}</small><svg class="ticon" viewBox="2 2 12 12"><path :d="icon"/></svg><span class="tline"><b>{{ item.name }}</b><span class="head">{{ head }}</span></span>
          <i v-if="status === 'wait'">运行中…</i></summary>
      </details>
      <!-- 点开后细条下面的圆角卡片，落在页面里占位 -->
      <div v-if="open" ref="slimCard" class="slim-card" :class="status">
        <pre v-if="item.name === 'Bash'"><code>{{ input.command }}</code></pre>
        <diff-view v-else-if="item.name === 'Edit'" :before="input.old_string" :after="input.new_string"></diff-view>
        <diff-view v-else-if="item.name === 'Write'" before="" :after="input.content"></diff-view>
        <div v-else-if="input.prompt && !input.url" class="md" v-md="input.prompt"></div>
        <ul v-else-if="item.name === 'TodoWrite'" class="todos">
          <li v-for="t in input.todos" :class="t.status">{{ t.content }}</li>
        </ul>
        <pre v-else-if="Object.keys(input).length > 1"><code>{{ json }}</code></pre>
        <details v-if="result" class="result" :open="showResult" @toggle="onResultToggle">
          <summary>{{ resultHead(result) }}</summary>
          <!-- 左边那道状态色竖条画在外面这层上：结果长了 pre 自己会滚，画在 pre 里会跟着内容滚走 -->
          <div v-if="result.text || !result.images" class="res-text"><pre>{{ result.text }}</pre></div>
          <result-shots v-if="result.images" :images="result.images"></result-shots>
        </details>
      </div>
    </div>`,
};
