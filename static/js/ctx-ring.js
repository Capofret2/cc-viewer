// 上下文药丸的悬停卡片：画成 GNOME 磁盘用量分析那样的环形图，里圈粗、外圈细，外圈的扇形都落在它所属那块的角度范围里。
// 两种视角，卡片顶上切换（kind）：
// - 上下文（ctx，数据和拆法见 ctxmap.py）：此刻上下文里装着什么。第一圈是系统提示词、工具、记忆文件、技能和消息的各类
//   （用户的话、工具结果……，后端树里「消息」那一层在这里摊平了，见 liftMessages），往外依次按工具、文件或命令、每一条分。
// - 花费（spend，见 spendmap.py）：这个会话一共花了多少分、花在哪。第一圈是缓存写入（含输入）、缓存命中、输出，
//   往外是哪一轮、哪次请求、具体花在什么上。
// 旁边一张表列出这一层的各块（兼作图例，颜色不单独表意）。
// 悬停一块：下面一行写它是哪条路径上的、多少、占几成、是准数还是估的；点有下一层的块就钻进去（它到圆心、整圈都是它），
// 点圆心或顶上的路径退回去；点到具体某一条（有序号的）就跳到对话里那个气泡。手机上点一下先看说明，再点一下才跳
const { reactive, computed, ref, watch } = Vue;
import { api, kilo, touch, persistRef } from './util.js';
import { fen, SpentPts } from './points.js';

const SIZE = 260, C = SIZE / 2, R = 126, HOLE = 42, LEVELS = 4;

// 颜色跟着类别走，不跟着大小排名走：换个会话、哪块变大变小，同一类还是同一个颜色。
// 消息的各类用分类色，两两之间都要一眼分得开（挑色时算过色差）；
// 系统那几块用灰阶（固定的那部分，不是看的重点，不用互相区分）。
// 回复蓝、工具调用浅紫、思考偏红的深紫、用户的话深绿、工具结果橙；附加信息原来的琥珀色和橙色太像，换成黄
// 再往外一圈圈继承所属那块的颜色、逐圈变浅
const HUES = { '工具结果': '#f0861a', '思考': '#8a2272', '工具调用': '#a07cf2', 'Claude 的回复': '#2f6fd6',
               '附加信息': '#e3c234', '图片': '#00b5d8', '压缩摘要': '#d8322f' };
const TALK = '#1c6b34';   // 用户的话（名字跟着称呼变，按「的话」结尾认）
const SYS = { '系统提示词': '#7b828c', '系统工具': '#959ba4', 'MCP 工具': '#a9aeb6', 'MCP 说明': '#a9aeb6',
              '自定义子代理': '#bcc0c7', '记忆文件': '#8c8780', '技能': '#a7a199' };
const OTHER = '#9aa0a8';
// 花费视角第一圈的计费项
const BILL = { '缓存写入': '#eb6834', '缓存命中': '#2a78d6', '输出': '#6250d6' };

function lighten(hex, t) {
  if (!hex.startsWith('#')) return hex;
  const n = parseInt(hex.slice(1), 16), mix = c => Math.round(c + (255 - c) * t);
  return `rgb(${mix(n >> 16)} ${mix((n >> 8) & 255)} ${mix(n & 255)})`;
}

// 给整棵树上色（只做一次，存在 WeakMap 里，不碰响应式数据）
function paint(root) {
  const colors = new WeakMap();
  (function walk(node, depth, base, from) {
    for (const c of node.c || []) {
      let own = null;
      if (depth === 1) own = HUES[c.n] || (c.n.endsWith('的话') ? TALK : SYS[c.n] || BILL[c.n] || OTHER);
      const b = own || base, f = own ? depth : from;
      colors.set(c, own || lighten(base, Math.min(0.62, (depth - f) * 0.22)));
      walk(c, depth + 1, b, f);
    }
  })(root, 1, OTHER, 1);
  return colors;
}

// 上下文视角不单列「消息」这一块：它底下的各类直接和系统那几块并排放在第一圈，按大小排，「其余 n 项」垫底。
// 只在画图这边摊平，后端的树照旧按 CLI 的分法把消息算一块
function liftMessages(t) {
  const msg = t.c?.find(c => c.n === '消息');
  if (!msg?.c) return t;
  const top = [...t.c.filter(c => c !== msg), ...msg.c].sort((a, b) => (a.rest || 0) - (b.rest || 0) || b.t - a.t);
  return { ...t, c: top };
}

function arcPath(a0, a1, r0, r1) {
  const full = a1 - a0 > Math.PI * 2 - 1e-6;
  if (full) a1 = a0 + Math.PI * 2 - 1e-4;
  const p = (a, r) => `${(C + r * Math.sin(a)).toFixed(2)} ${(C - r * Math.cos(a)).toFixed(2)}`;
  const big = a1 - a0 > Math.PI ? 1 : 0;
  return `M${p(a0, r1)}A${r1} ${r1} 0 ${big} 1 ${p(a1, r1)}L${p(a1, r0)}A${r0} ${r0} 0 ${big} 0 ${p(a0, r0)}Z`;
}

const depthOf = n => (n.c?.length ? 1 + Math.max(...n.c.map(depthOf)) : 0);
const pct = (t, of) => (of ? (t / of * 100 >= 10 ? (t / of * 100).toFixed(0) : (t / of * 100).toFixed(1)) : '0') + '%';

export const CtxRing = {
  props: { tree: Object, max: Number, loading: Boolean, error: String, kind: { type: String, default: 'ctx' } },
  emits: ['jump'],
  setup(props, { emit }) {
    const spend = computed(() => props.kind === 'spend');
    const num = t => (spend.value ? fen(t) : kilo(t));   // 花费视角的 t 是点数，写成分
    const stack = ref([]);      // 从根到当前圆心那一块
    const hover = ref(null);
    const colors = ref(new WeakMap());
    // 新取到一棵树（有了新内容）：照着原来钻到的那几层名字再钻一遍，鼠标还在卡片里时不会被弹回最外层
    watch(() => props.tree, (t, old) => {
      if (!t) return (stack.value = []);
      if (props.kind === 'ctx') t = liftMessages(t);
      colors.value = paint(t);
      const path = [t];
      for (const n of stack.value.slice(1)) {
        const hit = path.at(-1).c?.find(c => c.n === n.n && c.c);
        if (!hit) break;
        path.push(hit);
      }
      stack.value = path;
      hover.value = null;
    }, { immediate: true });
    const focus = computed(() => stack.value.at(-1));
    const colorOf = n => colors.value.get(n) || OTHER;

    // 当前圆心往外最多画四圈，圈宽一样（像磁盘分析那样），层数少时每圈宽一些
    const arcs = computed(() => {
      const f = focus.value;
      if (!f?.t) return [];
      const levels = Math.max(1, Math.min(LEVELS, depthOf(f))), w = (R - HOLE) / levels, out = [];
      (function walk(node, a0, a1, d, path) {
        let a = a0;
        for (const c of node.c || []) {
          const span = (a1 - a0) * c.t / node.t, p = [...path, c];
          if (span > 0.004) {   // 不到四分之一度的画出来只是一根线，跳过（它的上一层照样盖着这片角度）
            out.push({ node: c, path: p, d: arcPath(a, a + span, HOLE + (d - 1) * w + 1, HOLE + d * w), color: colorOf(c) });
            if (d < levels) walk(c, a, a + span, d + 1, p);
          }
          a += span;
        }
      })(f, 0, Math.PI * 2, 1, []);
      return out;
    });
    // 悬停时它所在的那一支（它的上层、它自己、它底下的）照常，别的变淡
    const lit = a => !hover.value || a.node === hover.value || a.path.includes(hover.value)
      || hoverPath.value.includes(a.node);
    const hoverPath = computed(() => arcs.value.find(a => a.node === hover.value)?.path || []);

    // 悬停说明：路径、多少（花费视角是几分、合多少 token）、占整个的和这一层的各几成、准数还是估的
    const line = computed(() => {
      const root = stack.value[0], f = focus.value, h = hover.value;
      if (!root || !f) return null;
      const n = h || f;
      const trail = [...stack.value.slice(1), ...(h ? hoverPath.value : [])].map(x => x.n);
      return {
        trail: trail.join(' › ') || (spend.value ? '这个会话的全部花费' : '整个上下文'),
        num: num(n.t), share: `${spend.value ? '占总花费' : '占上下文'} ${pct(n.t, root.t)}`,
        local: f !== root && h ? pct(n.t, f.t) : '',
        count: spend.value ? (n.tok != null ? `${kilo(n.tok)} token` : '') : n.k > 1 ? `${n.k} 条` : '',
        how: n.rest ? '太碎的合在一起' : n.e ? (n.c ? '含按字数分摊的' : '按字数分摊') : 'API 准数',
        jump: n.s != null ? (touch ? '再点一下跳到这条' : '点一下跳到这条') : n.c ? (h ? '点一下展开这一块' : '') : '',
      };
    });

    let lastPointer = 'mouse';
    function pick(n) {
      if (n.c?.length) {   // 点的是外圈的块：中间隔着的几层一起进栈，退回时一层层退
        stack.value = [...stack.value, ...(arcs.value.find(a => a.node === n)?.path || [n])];
        hover.value = null;
      } else if (n.s != null) {
        if (lastPointer !== 'mouse' && hover.value !== n) return (hover.value = n);
        emit('jump', n.s);
      } else {
        hover.value = n;
      }
    }
    const up = () => stack.value.length > 1 && (stack.value = stack.value.slice(0, -1), hover.value = null);
    const zoomTo = i => { stack.value = stack.value.slice(0, i + 1); hover.value = null; };
    const rows = computed(() => focus.value?.c || []);

    return { SIZE, C, HOLE, stack, focus, hover, arcs, lit, line, pick, up, zoomTo, rows, colorOf, kilo, pct, spend, num,
             onDown: e => (lastPointer = e.pointerType || 'mouse'),
             enter: (e, n) => e.pointerType === 'mouse' && (hover.value = n) };
  },
  template: `<div class="ctx-map">
    <p v-if="error" class="error">{{ spend ? '花费' : '上下文' }}没拆成：{{ error }}</p>
    <p v-else-if="!tree" class="empty">{{ loading ? '正在从会话记录里拆…' : '还没有内容' }}</p>
    <template v-else>
      <div class="ctx-crumbs">
        <template v-for="(n, i) in stack" :key="i"><span v-if="i" class="sep">›</span><button
          type="button" :disabled="i === stack.length - 1" @click.stop="zoomTo(i)">{{ i ? n.n : spend ? '全部花费' : '上下文' }}</button></template>
        <small v-if="loading">正在更新…</small>
      </div>
      <!-- 摆法交给样式的网格（见 style.css「上下文组成卡片」）：电脑上图在左、表在右，下面是悬停说明和灰字；
           手机上图、悬停说明固定，中间的表和灰字（ctx-scroll）单独滚 -->
        <svg class="ctx-ring" :viewBox="'0 0 ' + SIZE + ' ' + SIZE" @pointerdown="onDown" @pointerleave="hover = null">
          <path v-for="(a, i) in arcs" :key="i" :d="a.d" :style="{ fill: a.color }" :class="{ dim: !lit(a), rest: a.node.rest }"
                @pointerenter="enter($event, a.node)" @click.stop="pick(a.node)"></path>
          <circle :cx="C" :cy="C" :r="HOLE - 3" class="ctx-hole" :class="{ up: stack.length > 1 }" @click.stop="up()"></circle>
          <text :x="C" :y="C - 2" class="ctx-hole-num">{{ num(focus.t) }}</text>
          <text :x="C" :y="C + 15" class="ctx-hole-sub">{{ stack.length > 1 ? '点这里退回'
            : spend ? tree.calls + ' 次请求' : max ? '占上限 ' + pct(tree.t, max) : 'token' }}</text>
        </svg>
      <div class="ctx-scroll">
        <div class="ctx-list">
          <button v-for="(c, i) in rows" :key="i" type="button" class="ctx-row" :class="{ on: hover === c }"
                  @pointerenter="enter($event, c)" @pointerdown="onDown" @click.stop="pick(c)">
            <i :style="{ background: colorOf(c) }"></i>
            <span class="ctx-name">{{ c.n }}</span>
            <small class="ctx-count">{{ spend ? (c.tok != null ? kilo(c.tok) : '') : c.k > 1 ? c.k + ' 条' : '' }}</small>
            <b :class="{ guess: c.e }">{{ num(c.t) }}</b>
            <small class="ctx-pct">{{ pct(c.t, focus.t) }}</small>
          </button>
        </div>
      <p v-if="spend" class="ctx-note">
        每次请求的输入、缓存写入、缓存命中、输出是 API 准数，按单价换成元；只算记在这个会话名下的调用，和标签页上的总花费是同一份。
        往外是哪一轮（{{ $who.me }}的哪句话）、哪次请求（子代理的归到发起它的那一轮）。输出里看得见的字按字数算、剩下的算思考；
        缓存写入里新进上下文的各条按两次请求的上下文之差分摊，比新内容多写的是缓存没命中、把已有历史重新写了一遍。数字带虚线的含按字数分摊的部分；表里分数前面是合多少 token。
        <template v-if="tree.unrated">另有 {{ tree.unrated }} 次调用的模型还没有单价，没算进去。</template>
      </p>
      <p v-else class="ctx-note">
        <template v-if="!tree.cli">CLI 没连着，系统部分（系统提示词、工具、记忆文件、技能）拆不开，按第一次请求估成一块。</template>
        <template v-else-if="tree.stale">系统部分是 CLI 上次连着时给的明细。</template>
        消息逐条按两次请求之间上下文涨了多少（API 准数）分摊，同一组里有好几样时按字数分；数字带虚线的含按字数分摊的部分。
        <template v-if="tree.cliTotal && Math.abs(tree.cliTotal - tree.t) > tree.t * 0.005">CLI 算的总数是 {{ kilo(tree.cliTotal) }}，
          这里逐条加起来是 {{ kilo(tree.t) }}（最后一次请求之后进来的只能按字数估）。</template>
      </p>
      </div>
      <div v-if="line" class="ctx-line">
        <span class="ctx-trail">{{ line.trail }}</span>
        <span><b>{{ line.num }}</b> · {{ line.share }}<template v-if="line.local">（这一块里 {{ line.local }}）</template><template
          v-if="line.count"> · {{ line.count }}</template> · {{ line.how }}<template v-if="line.jump"> · {{ line.jump }}</template></span>
      </div>
    </template>
  </div>`,
};

// 两种视角一整套：切换的滑动药丸（和思考强度那个一样）、此刻视角的总数、叠在同一格里的两张环形图。
// 两张叠着放、不在看的那张只是藏起来照样占地方：高度总按高的那张算，切换时不伸不缩。
// 上下文药丸的悬停卡片（foot：切换那一行放在最底下、挨着药丸，卡片往上开、底边不动，按钮不会跟着内容跳）
// 和会话信息弹窗的最上面（切换那一行在图上面）共用；extra 插槽放那一行最右边的按钮
export const CtxViews = {
  components: { CtxRing, SpentPts },
  props: { maps: Object, view: String, tabId: String, ctx: Object, spent: Object, spentPaid: Object, spentBlue: Object, foot: Boolean },
  emits: ['update:view', 'jump'],
  setup(props) {
    const kind = computed(() => (props.view === 'spend' ? 'spend' : 'ctx'));
    return { kind, kilo };
  },
  template: `<div class="ctx-views-box" :class="{ foot }">
    <div class="dd-title ctx-switch">
      <div class="effort-seg ctx-views" :style="{ '--n': 2, '--i': kind === 'spend' ? 1 : 0 }">
        <i class="effort-thumb"></i>
        <button type="button" :class="{ on: kind === 'ctx' }" @click.stop="$emit('update:view', 'ctx')">上下文</button>
        <button type="button" :class="{ on: kind === 'spend' }" @click.stop="$emit('update:view', 'spend')">花费</button>
      </div>
      <small v-if="kind === 'spend'"><spent-pts :spent="spent" :paid="spentPaid" :blue="spentBlue"></spent-pts></small>
      <!-- 不写上限（大家都知道是 1M），百分比是占上限的几成 -->
      <small v-else-if="ctx">{{ kilo(ctx.total) + (ctx.max ? '（' + ctx.percent + '%）' : '') }}</small>
      <slot name="extra"></slot>
    </div>
    <div class="ctx-stack">
      <ctx-ring v-for="v in ['ctx', 'spend']" :key="v" :kind="v" :class="{ off: kind !== v }"
                :tree="maps[v].tab === tabId ? maps[v].data : null" :max="ctx?.max || 0"
                :loading="maps[v].loading" :error="maps[v].tab === tabId ? maps[v].error : ''" @jump="s => $emit('jump', s)"></ctx-ring>
    </div>
  </div>`,
};

// 取数：悬停药丸时取，同一个标签页内容没变、半分钟内不重取；取的时候旧图照常显示，只在顶上写「正在更新」。
// 看哪个视角记在这台设备的浏览器里（ctxView）；从哪里点进去先看哪张：上下文药丸是上下文，标题前的花费是花费（index.html 里设）
export function useCtxMap({ current }) {
  const ctxView = persistRef('ctxView', 'ctx');
  const blank = () => ({ tab: '', key: '', at: 0, data: null, loading: false, error: '' });
  const ctxMap = reactive({ ctx: blank(), spend: blank() });
  // 两种视角一起取（都只读会话记录，很快）：切过去时图已经在了，卡片不会先缩成一行「正在拆」、把鼠标甩到卡片外面去
  const loadCtxMap = (force = false) => Promise.all(['ctx', 'spend'].map(v => loadView(v, force)));
  async function loadView(view, force) {
    const t = current.value, m = ctxMap[view];
    if (!t || t.fresh || m.loading) return;
    const key = view === 'ctx' ? `${t.id}|${t.total}|${t.ctx?.total}` : `${t.id}|${t.total}`;
    if (!force && m.key === key && Date.now() - m.at < 30000) return;
    if (m.tab !== t.id) Object.assign(m, { tab: t.id, data: null });
    Object.assign(m, { loading: true, error: '' });
    try {
      const data = await api(`/api/tabs/${t.id}/${view === 'ctx' ? 'context' : 'spend'}`);
      if (m.tab === t.id) Object.assign(m, { data, key, at: Date.now() });
    } catch (e) {
      m.error = e.message;
    }
    m.loading = false;
  }
  return { ctxMap, ctxView, loadCtxMap };
}
