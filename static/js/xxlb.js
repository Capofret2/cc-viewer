// 新会话页上的看板娘小小蓝白：Live2D 模型，出自 TeamMajdata 的 MajdataPlay，对方同意在设置里注明出处后使用，
// 出处由下面的 XxlbCredit 写在设置里。开着二次元风格界面时替换原来的颜文字；平时放待机动作，点她害羞，说出第一句话时鼓励。
// 加载失败就退回颜文字，并在提示条里说是哪一步没成（这次打开页面里不再试，刷新再来）。
// 运行库都在 vendor/ 里，第一次要画她时才加载、只加载一次：Live2D 官方的 Cubism Core（Live2D 专有软件许可里的可再分发代码，
// 只能原样再分发、不能改）、pixi.js 6.5.10 和 pixi-live2d-display 0.4.0 的 cubism4 版（都是 MIT）。
// 模型在 live2d/xxlb/：贴图从 4096 缩到 2048（她只有一百多像素高，原图两张在显卡里要占 128MB），只带用得到的三段动作。
// 头发、裙子这些蓝色的地方跟着主题色换颜色（见下面的 RECOLOR）
import { KaoFace } from './kao.js';
import { fail } from './util.js';
const { ref, watch, onMounted, onUnmounted } = Vue;

const MODEL = 'live2d/xxlb/xxlb.model3.json';
// 模型画布边长（模型坐标）。她几乎占满画布，害羞时头顶的小卷线会冒出上沿十几个单位，所以框四周各让 4 个 CSS 像素
// （三段动作逐帧量过）
const SIZE = 800, MARGIN = 4;

// 三个脚本一个一个按顺序加载：pixi-live2d-display 一加载就要找全局的 PIXI 和 Live2DCubismCore。
// 每个脚本只加载一次；某一个没下来就记下失败，刷新页面再试
const loaded = {};
const loadScript = src => loaded[src] ||= new Promise((ok, no) => {
  const s = Object.assign(document.createElement('script'), { src });
  s.onload = ok;
  s.onerror = () => { s.remove(); no(new Error(`${src} 没加载下来`)); };
  document.head.append(s);
});
async function loadLibs() {
  for (const src of ['vendor/live2dcubismcore.min.js', 'vendor/pixi.min.js', 'vendor/pixi-live2d-display-cubism4.min.js']) {
    await loadScript(src);
  }
}

// 这次打开页面时加载失败过：所有新会话页都退回颜文字。
// 开源版不带小小蓝白的模型和 Live2D 运行库（模型是对方许可给原作者用的，Live2D 运行库是专有许可），所以一开始就当成加载不了：
// 不去请求这些文件，不弹提示条，设置里也不写出处。自己放好 live2d/xxlb/ 和上面三个运行库以后，在 instance.json 写 "xxlb": true，
// 配置到了（/api/config 的 xxlb，sessions.js 交过来）才打开。加载失败过（failed）就不再打开
const broken = ref(true);
let failed = false;
export const setXxlb = on => { broken.value = !on || failed; };
// 配置可能比新会话页晚到：挂载时还关着，就等开关打开了再开始（画布要等 v-if 渲染出来，所以隔一拍）
const whenOn = start => {
  if (!broken.value) { start(); return null; }
  const stop = Vue.watch(broken, v => { if (!v) { stop(); Vue.nextTick(start); } });
  return stop;
};

// 跟着主题换颜色：她原本偏蓝的像素（色相 170～270 度，两头渐变）把色相转到这个主题的色相上，
// 饱和度乘一个数、再往白色提亮一些，成马卡龙色；太灰、太暗的少提或不提，所以黑色蝴蝶结、描边仍然是深色，金发夹、皮肤、眼睛不在这个色相里、不变。
// 色相只看是不是蓝、几乎没颜色的才不转，深蓝的描边也整个转过去（按明暗渐变着转的话描边停在半路，橙色主题下发绿）；转也走近的那一边。
// 在显卡上做（pixi 滤镜）：贴图不用重算，切主题当场换，贴图照旧和别的新会话页共用
const RECOLOR_BASE = 213;   // 她原本发色的色相
// 每个主题一套，在竖版 demo 里逐个调的：hue 转到哪个色相（度），sat 饱和度乘几（可以大于 1），lift 往白色提亮多少。
// 所有主题的 sat、lift 都和蓝色那套一样，只换色相（不然蓝色以外的发色太白、太淡）。
// 深色模式用同一套。表里没有的主题（以后加的）按主题色 --me-strong 的色相，sat、lift 也和蓝色一样
const RECOLOR_FALLBACK = { sat: 1.05, lift: 0.4 };
const RECOLOR = {
  green: { hue: 144, ...RECOLOR_FALLBACK },
  blue: { hue: 213, ...RECOLOR_FALLBACK },
  purple: { hue: 259, ...RECOLOR_FALLBACK },
  teal: { hue: 176, ...RECOLOR_FALLBACK },
  pink: { hue: 331, ...RECOLOR_FALLBACK },
  amber: { hue: 17, ...RECOLOR_FALLBACK },
};
const RECOLOR_FRAG = `
varying vec2 vTextureCoord;
uniform sampler2D uSampler;
uniform float uShift;   // 色相转多少圈（目标色相 − 原本色相，取近的一边）/ 360，在 −0.5～0.5
uniform float uSat;     // 饱和度乘几
uniform float uLift;    // 往白色提亮多少
vec3 rgb2hsl(vec3 c) {
  float mx = max(c.r, max(c.g, c.b)), mn = min(c.r, min(c.g, c.b)), d = mx - mn, l = (mx + mn) * 0.5, h = 0.0, s = 0.0;
  if (d > 0.0) {
    s = l > 0.5 ? d / (2.0 - mx - mn) : d / (mx + mn);
    if (mx == c.r) h = (c.g - c.b) / d + (c.g < c.b ? 6.0 : 0.0);
    else if (mx == c.g) h = (c.b - c.r) / d + 2.0;
    else h = (c.r - c.g) / d + 4.0;
    h /= 6.0;
  }
  return vec3(h, s, l);
}
float hue2rgb(float p, float q, float t) {
  t = fract(t);
  if (t < 1.0 / 6.0) return p + (q - p) * 6.0 * t;
  if (t < 0.5) return q;
  if (t < 2.0 / 3.0) return p + (q - p) * (2.0 / 3.0 - t) * 6.0;
  return p;
}
vec3 hsl2rgb(vec3 c) {
  if (c.y <= 0.0) return vec3(c.z);
  float q = c.z < 0.5 ? c.z * (1.0 + c.y) : c.z + c.y - c.z * c.y, p = 2.0 * c.z - q;
  return vec3(hue2rgb(p, q, c.x + 1.0 / 3.0), hue2rgb(p, q, c.x), hue2rgb(p, q, c.x - 1.0 / 3.0));
}
void main() {
  vec4 c = texture2D(uSampler, vTextureCoord);
  if (c.a <= 0.0) { gl_FragColor = c; return; }
  vec3 hsl = rgb2hsl(c.rgb / c.a);   // 进来的是预乘过透明度的颜色，先还原
  float deg = hsl.x * 360.0;
  float wh = smoothstep(170.0, 190.0, deg) * (1.0 - smoothstep(250.0, 270.0, deg)) * smoothstep(0.03, 0.08, hsl.y);
  if (wh <= 0.0) { gl_FragColor = c; return; }
  float w = wh * smoothstep(0.08, 0.2, hsl.y) * smoothstep(0.1, 0.25, hsl.z);
  hsl.x = fract(hsl.x + uShift * wh);
  hsl.y = min(1.0, hsl.y * (1.0 - (1.0 - uSat) * w));   // 乘的数大于 1 时可能超过 1
  hsl.z += (1.0 - hsl.z) * uLift * w;
  gl_FragColor = vec4(hsl2rgb(hsl) * c.a, c.a);
}`;

// 当前主题用哪套换色参数：按根元素的 data-theme（app.js 写的，没写就是默认的绿色）查表；表里没有就读 --me-strong 的色相配通用值，
// 换主题、系统换深浅色时重读。读不出来是 null，这时不换色、保持原样
const themeRecolor = ref(null);
function readThemeRecolor() {
  const name = document.documentElement.dataset.theme || 'green';
  if (RECOLOR[name]) {
    themeRecolor.value = RECOLOR[name];
    return;
  }
  const v = getComputedStyle(document.documentElement).getPropertyValue('--me-strong').trim();
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.fillStyle = '#010203';
  ctx.fillStyle = v;   // 交给浏览器解析，读回来统一是 #rrggbb；解析不了就还是上面那个
  const hex = ctx.fillStyle;
  if (!v || hex === '#010203' || !/^#[0-9a-f]{6}$/i.test(hex)) {
    themeRecolor.value = null;
    return;
  }
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
  const mx = Math.max(r, g, b), d = mx - Math.min(r, g, b);
  let h = 0;
  if (d) {
    h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  }
  themeRecolor.value = { hue: h * 60, ...RECOLOR_FALLBACK };
}
let themeWatched = false;
function watchTheme() {
  if (themeWatched) return;
  themeWatched = true;
  readThemeRecolor();
  new MutationObserver(readThemeRecolor).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', readThemeRecolor);
}
// 按这套参数设滤镜；没有参数（读不出主题色）就关掉，保持原样
function applyRecolor(filter, p) {
  filter.enabled = !!p;
  if (!p) return;
  const turn = ((p.hue - RECOLOR_BASE) % 360 + 540) % 360 - 180;   // 取近的一边：−180～180 度
  Object.assign(filter.uniforms, { uShift: turn / 360, uSat: p.sat, uLift: p.lift });
}
const newRecolor = () => {
  const f = new PIXI.Filter(undefined, RECOLOR_FRAG, { uShift: 0, uSat: 1, uLift: 0 });
  f.resolution = devicePixelRatio || 1;   // 跟共用的渲染器一样，不然高分屏上她会糊
  return f;
};

// 所有小小蓝白共用一个离屏的 pixi 渲染器（只占一个 WebGL 上下文）：页面上的每个她都是普通的 2D 画布，各自的模型先画在共用这块的左上角，再拷过去。
// 不能每个画布各开一个 WebGL：pixi-live2d-display 里 Cubism 的着色器全局只有一份、只认最后用过它的那个上下文，
// 两个上下文轮流画，另一个就「没有可用的着色器程序」、一片空白（设置里的主题色那排一出来就会碰上；开着几个新会话标签页来回切也会）
let shared = null;
function sharedRenderer(w, h) {
  if (!shared) {
    // 留着绘图缓冲区：画完紧接着拷，不留的话浏览器合成以后就取不到了
    shared = new PIXI.Renderer({ width: w, height: h, resolution: devicePixelRatio || 1, backgroundAlpha: 0, antialias: true,
                                 preserveDrawingBuffer: true });
  } else if (shared.screen.width < w || shared.screen.height < h) {
    // 只往大里长：大小不一的几个轮流画时不用每帧重新分配
    shared.resize(Math.max(w, shared.screen.width), Math.max(h, shared.screen.height));
  }
  return shared;
}
// 把 stage 画在共用渲染器左上角 w×h（CSS 像素）那块，再拷进 ctx 的画布（它的绘图缓冲区是 w×h 乘设备像素比）
function drawTo(ctx, stage, w, h) {
  const r = sharedRenderer(w, h), k = r.resolution;
  r.render(stage, { clear: true });
  ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
  ctx.drawImage(r.view, 0, 0, Math.round(w * k), Math.round(h * k), 0, 0, ctx.canvas.width, ctx.canvas.height);
}
// 把模型缩放、摆到 w×h（绘图坐标）的正中，四周让 margin
function place(model, w, h, margin) {
  const s = (Math.min(w, h) - margin * 2) / SIZE;
  model.scale.set(s);
  model.position.set((w - SIZE * s) / 2, (h - SIZE * s) / 2);
}

// 设置里主题色那排的小人（XxlbThemes）多大、四周让多少：和 style.css 的 .swatch.xxlb 一致
const SWATCH = 44, SWATCH_MARGIN = 1;

export const XxlbLive2d = {
  components: { KaoFace },
  // kao：退回颜文字时用哪个；active：所在的标签页在前台（后台的不画也不动，省显卡）；cheer：说出了第一句话；
  // pose：page 是新会话页上那个（待机、点她害羞），swatch 是设置里主题色那排选中的那个（一直循环鼓励、举点赞牌，点击交给外面的按钮）
  props: { kao: { type: [String, Array], required: true }, active: Boolean, cheer: Boolean, pose: { type: String, default: 'page' } },
  setup(props) {
    const canvas = ref(null), ready = ref(false), seen = ref(true);
    const swatch = props.pose === 'swatch';
    // 系统设了减少动画：停住不动（新会话页停在待机第一帧，主题色那排停在举着点赞牌的那一刻），点了也不动
    const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
    let ctx = null, stage = null, model = null, ticker = null, ro = null, io = null, recolor = null, size = null, gone = false;

    const draw = () => {
      if (size && stage) drawTo(ctx, stage, size[0], size[1]);
    };
    // 画布的显示大小由样式定（宽屏、手机不一样），这里只跟着它改绘图缓冲区和模型的缩放；藏起来（display: none）时量到 0，不动
    const fit = () => {
      const w = canvas.value?.clientWidth, h = canvas.value?.clientHeight;
      if (!w || !h || !model) return;
      const k = devicePixelRatio || 1;
      canvas.value.width = Math.round(w * k);
      canvas.value.height = Math.round(h * k);
      size = [w, h];
      place(model, w, h, swatch ? SWATCH_MARGIN : MARGIN);
      draw();
    };
    // 在动的条件：所在标签页在前台，而且确实在屏幕上（设置页收起来、侧边栏滑走了就停）
    const run = () => {
      if (!ticker) return;
      if (props.active && seen.value && !still) ticker.start();
      else ticker.stop();
    };
    const play = group => {
      if (model && !still) model.motion(group, 0, PIXI.live2d.MotionPriority.FORCE);
    };
    const cleanup = () => {
      ro?.disconnect();
      io?.disconnect();
      ticker?.destroy();
      // 只拆这一份：贴图在 pixi 的缓存里和别的小小蓝白共用，不能跟着销毁；共用的渲染器也留着
      stage?.destroy({ children: true });
      ctx = stage = model = ticker = ro = io = recolor = size = null;
    };

    const start = async () => {
      if (gone) return;
      try {
        await loadLibs();
        if (gone) return;
        ctx = canvas.value.getContext('2d');
        // 不挂 pixi 的全局时钟、不让它自己接管点击：动不动由这一个自己的时钟管，点击只认整块画布。
        // 主题色那排把没动作时自动放的那组换成鼓励，就一直循环点赞
        const m = await PIXI.live2d.Live2DModel.from(MODEL, { autoUpdate: false, autoInteract: false,
                                                             ...(swatch ? { idleMotionGroup: 'Cheer' } : {}) });
        if (gone) { m.destroy(); return; }
        model = m;
        watchTheme();
        recolor = newRecolor();
        applyRecolor(recolor, themeRecolor.value);
        model.filters = [recolor];
        stage = new PIXI.Container();
        stage.addChild(model);
        ticker = new PIXI.Ticker();
        ticker.add(() => {
          model.update(ticker.deltaMS);
          draw();
        });
        fit();
        ro = new ResizeObserver(fit);
        ro.observe(canvas.value);
        io = new IntersectionObserver(([e]) => { seen.value = e.isIntersecting; run(); });
        io.observe(canvas.value);
        if (still) {
          // 动作文件是异步读进来的，要等它开始放再画，不然先画出没有动作的默认姿势，之后哪次重画（比如换主题）又变成动作里的样子。
          // 新会话页停在待机第一帧；主题色那排往后走 2.5 秒，停在举着点赞牌的那一刻（一步步走，一下跳 2.5 秒头发的物理会乱）
          await model.motion(swatch ? 'Cheer' : 'Idle', 0, PIXI.live2d.MotionPriority.IDLE);
          if (gone) return;
          for (let t = 0; t < (swatch ? 2500 : 1); t += 100) {
            model.update(swatch ? 100 : 0);
            draw();
          }
        }
        ready.value = true;
        run();
      } catch (e) {
        cleanup();
        if (!gone && !broken.value) {
          failed = broken.value = true;
          fail('加载看板娘小小蓝白', e);
        }
      }
    };
    let stopWait = null;
    onMounted(() => { stopWait = whenOn(start); });
    onUnmounted(() => {
      stopWait?.();
      gone = true;
      cleanup();
    });
    watch(() => props.active, run);
    watch(() => props.cheer, on => on && play('Cheer'));
    // 换了主题：时钟停着的（后台标签页、减少动画）也当场重画一帧，切回来就是新颜色
    watch(themeRecolor, p => {
      if (!recolor) return;
      applyRecolor(recolor, p);
      if (!ticker.started) draw();
    });

    return { canvas, ready, broken, swatch, tap: () => swatch || play('Touch') };
  },
  template: `<canvas v-if="!broken" ref="canvas" class="xxlb-live2d" :class="{ ready }" :role="swatch ? null : 'img'"
                     :aria-label="swatch ? null : '看板娘小小蓝白'" @click="tap"></canvas><kao-face v-else :kao="kao"></kao-face>`,
};

// 主题色那排没选中的几个用的静止图：在共用的渲染器上按每个主题的参数换色、停在待机第一帧，各拍一张（data: 地址），拍完把这一份模型拆掉。
// 整个页面拍一次、大家共用；没拍成下次打开设置再试
let shotsLoad = null;
function takeShots() {
  return shotsLoad ||= (async () => {
    await loadLibs();
    const model = await PIXI.live2d.Live2DModel.from(MODEL, { autoUpdate: false, autoInteract: false });
    const stage = new PIXI.Container();
    try {
      const f = newRecolor();
      model.filters = [f];
      stage.addChild(model);
      place(model, SWATCH, SWATCH, SWATCH_MARGIN);
      await model.motion('Idle', 0, PIXI.live2d.MotionPriority.IDLE);
      model.update(0);
      const c = document.createElement('canvas');
      c.width = c.height = Math.round(SWATCH * (devicePixelRatio || 1));
      const ctx = c.getContext('2d');
      const out = {};
      for (const [key, p] of Object.entries(RECOLOR)) {
        applyRecolor(f, p);
        drawTo(ctx, stage, SWATCH, SWATCH);
        out[key] = c.toDataURL();
      }
      return out;
    } finally {
      stage.destroy({ children: true });
    }
  })().catch(e => {
    shotsLoad = null;
    throw e;
  });
}

// 设置里的主题色：开着二次元风格界面时，圆点换成按各自主题换好色的小小蓝白；
// 选中的垫一个这个主题浅色的圆底，是会动的 Live2D、一直循环鼓励（举点赞牌），没选中的是 takeShots 拍的静止待机图。
// 只让选中的那个动：会动的每帧都要在共用的渲染器上画一遍再拷过来，六个一起动不值。
// 静止图还没拍好、Live2D 加载失败、或者某个主题不在换色表里（以后新加的），那一个照旧是原来的圆点
export const XxlbThemes = {
  components: { XxlbLive2d },
  props: { themes: { type: Object, required: true }, theme: { type: String, required: true } },
  emits: ['pick'],
  setup() {
    const shots = ref(null);
    let stopWait = null;
    onMounted(() => { stopWait = whenOn(() => takeShots().then(s => (shots.value = s), e => fail('给主题色那排拍小小蓝白', e))); });
    Vue.onUnmounted(() => stopWait?.());
    return { shots, broken };
  },
  // 选中的那个：会动的叠在静止图上面，加载好（ready）了才把静止图藏起来，换选中时不会空一下
  template: `<div class="swatches"><button v-for="(label, key) in themes" :key="key" type="button" class="swatch"
      :class="{ on: theme === key, xxlb: !broken && shots?.[key] }" :data-theme="key" :aria-label="label" @click="$emit('pick', key)">
      <template v-if="!broken && shots?.[key]"><xxlb-live2d v-if="theme === key" kao="" pose="swatch" :active="true"></xxlb-live2d><img
        :src="shots[key]" alt=""></template></button></div>`,
};

// 出处（对方要求写在设置里）：有二次元风格界面开关的人写在开关下面，看不到开关的人（游客、单用户实例、旧后端）写在设置页最后
export const XxlbCredit = {
  setup: () => ({ broken }),
  // 中文接着链接时不能断行：模板里的换行会变成一个空格
  template: `<p v-if="!broken" class="global-note xxlb-credit">新会话页的看板娘小小蓝白出自 TeamMajdata 的 <a
    href="https://github.com/TeamMajdata/MajdataPlay" target="_blank" rel="noopener">MajdataPlay</a>，Live2D 运行库按 <a
    href="https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html"
    target="_blank" rel="noopener">Live2D 专有软件许可</a>使用</p>`,
};
