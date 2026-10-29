// 渲染层：Markdown、公式、代码上色、示意图、引用的服务器图片。对外只给一个 v-md 指令
import { copyText, say, fail, fileSize } from './util.js';

//
// 公式：在 Markdown 解析阶段就认出来交给 KaTeX，免得 _ 和 * 先被当成斜体加粗
const tex = (src, displayMode) =>
  katex.renderToString(src, { displayMode, output: 'html', throwOnError: false, strict: false });
// 行内 $...$：$ 要紧贴内容，结尾的 $ 后面不能跟字母数字，避开 $PID、$5 和 $10、$HOME/$USER 这类写法
const INLINE_MATH = [
  [/^\$\$((?:\\.|[^\\$])+?)\$\$/, true],
  [/^\\\[([\s\S]+?)\\\]/, true],
  [/^\\\(([\s\S]+?)\\\)/, false],
  [/^\$(?![\s$])((?:\\.|[^\\$\n])*?(?:\\.|[^\s\\$]))\$(?![A-Za-z0-9])/, false],
];
marked.use({
  extensions: [
    {
      name: 'mathBlock', level: 'block',
      start: src => src.match(/^ {0,3}(\$\$|\\\[)/m)?.index,
      tokenizer(src) {
        const m = src.match(/^ {0,3}\$\$([\s\S]+?)\$\$ *(?:\n|$)/) || src.match(/^ {0,3}\\\[([\s\S]+?)\\\] *(?:\n|$)/);
        if (m) return { type: 'mathBlock', raw: m[0], text: m[1].trim() };
      },
      renderer: t => `<div class="math">${tex(t.text, true)}</div>`,
    },
    {
      name: 'math', level: 'inline',
      start: src => src.match(/\$|\\[([]/)?.index,
      tokenizer(src) {
        for (const [re, display] of INLINE_MATH) {
          const m = src.match(re);
          if (m) return { type: 'math', raw: m[0], text: m[1].trim(), display };
        }
      },
      renderer: t => tex(t.text, t.display),
    },
  ],
});

// 加粗斜体的星号：CommonMark 判断星号能不能开、能不能关的规则是照着词间有空格的英文定的，
// 「**注意：**这里」「是**「重点」**内容」这种星号内侧是标点、外侧紧挨汉字的写法会配不上对，星号原样露出来。
// 这里照抄 marked 15 的 emStrong，只多一条：星号外侧挨着中日韩文字时，和挨着标点一样看待。
// 内侧的汉字照旧算普通文字，不然「**中文**API」这种原本好好的写法反而配不上了；下划线不动，照旧交给 marked
const CJK = '[\\p{sc=Han}\\p{sc=Hiragana}\\p{sc=Katakana}\\p{sc=Hangul}]';
const CJK_CHAR = new RegExp(CJK, 'u');
// 和 marked 开着 GFM 时一样，~ 不算标点
const P = '(?!~)[\\p{P}\\p{S}]', PS = '(?!~)[\\s\\p{P}\\p{S}]', NPS = '(?:[^\\s\\p{P}\\p{S}]|~)';
const AST_END = new RegExp('^[^_*]*?__[^_*]*?\\*[^_*]*?(?=__)|[^*]+(?=[^*])'
  + `|${CJK}(\\*+)(?!\\*)(?=${P})`          // 1 汉字**标点：可开可关（新加的）
  + `|(?!\\*)${P}(\\*+)(?=${CJK})`          // 2 标点**汉字：可开可关（新加的）
  + `|(?!\\*)${P}(\\*+)(?=[\\s]|$)`         // 3 只能关
  + `|${NPS}(\\*+)(?!\\*)(?=${PS}|$)`       // 4 只能关
  + `|(?!\\*)${PS}(\\*+)(?=${NPS})`         // 5 只能开
  + `|[\\s](\\*+)(?!\\*)(?=${P})`           // 6 只能开
  + `|(?!\\*)${P}(\\*+)(?!\\*)(?=${P})`     // 7 可开可关
  + `|${NPS}(\\*+)(?=${NPS})`, 'gu');       // 8 可开可关
marked.use({
  tokenizer: {
    emStrong(src, maskedSrc, prevChar = '') {
      if (src[0] !== '*') return false;
      let match = this.rules.inline.emStrongLDelim.exec(src);
      if (!match) return;
      // 开头的星号后面是标点时，前面得是空格、标点或者（新加的）中日韩文字
      if (match[1] && prevChar && !this.rules.inline.punctuation.exec(prevChar) && !CJK_CHAR.test(prevChar)) return;
      const lLength = [...match[0]].length - 1;
      let delimTotal = lLength, midDelimTotal = 0;
      AST_END.lastIndex = 0;
      maskedSrc = maskedSrc.slice(-1 * src.length + lLength);
      while ((match = AST_END.exec(maskedSrc)) != null) {
        const [, either1, either2, right1, right2, left1, left2, either3, either4] = match;
        const rDelim = either1 || either2 || right1 || right2 || left1 || left2 || either3 || either4;
        if (!rDelim) continue;
        let rLength = [...rDelim].length;
        if (left1 || left2) {
          delimTotal += rLength;
          continue;
        }
        // CommonMark 规则 9、10：可开可关的星号和开头的星号数加起来是 3 的倍数时不配对
        if ((either1 || either2 || either3 || either4) && lLength % 3 && !((lLength + rLength) % 3)) {
          midDelimTotal += rLength;
          continue;
        }
        delimTotal -= rLength;
        if (delimTotal > 0) continue;
        rLength = Math.min(rLength, rLength + delimTotal + midDelimTotal);
        const raw = src.slice(0, lLength + match.index + [...match[0]][0].length + rLength);
        const n = Math.min(lLength, rLength) % 2 ? 1 : 2;
        const text = raw.slice(n, -n);
        return { type: n === 1 ? 'em' : 'strong', raw, text, tokens: this.lexer.inlineTokens(text) };
      }
    },
  },
});

// 示意图：语言后面跟着「示意图」的代码块（```svg 示意图）画成图放在原处，右上角能切去看源码。
// 不带标记的照旧是代码：用户要的是代码时 Claude 不加它，旧会话里的代码块也不会突然变成图（提示词在 chat.py）
const VIZ_MARK = '示意图';
const VIZ_HL = { svg: 'xml', html: 'xml', mermaid: 'plaintext' };   // 切到源码时按什么语言上色
// 能引用的服务器图片，和后端 /api/tabs/{id}/file 认的格式一致
const IMAGE_FILE = /\.(png|jpe?g|gif|webp|svg)$/i;
const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
// 不带协议头的地址就是服务器上的路径（相对路径由后端按会话的工作目录算），file:// 也当路径
const localPath = href => href.startsWith('file://') ? decodeURI(href.slice(7))
  : /^([a-z][\w+.-]*:|\/\/|#)/i.test(href) ? null : href;
marked.use({
  renderer: {
    code({ text, lang }) {
      const [kind, mark] = (lang || '').trim().split(/\s+/);
      if (mark !== VIZ_MARK || !VIZ_HL[kind]) return false;
      return `<div class="viz" data-viz="${kind}"><pre><code class="language-${VIZ_HL[kind]}">${esc(text)}</code></pre></div>`;
    },
    // 浏览器拿不到服务器上的文件：这里只记下路径，滚到跟前再换成取图的地址（见 drawWatcher）
    image({ href, text }) {
      const path = localPath(href);
      return path ? `<img data-path="${esc(path)}" alt="${esc(text)}">` : false;
    },
    // 指向服务器上文件的链接是下载按钮（Claude 只在用户要下载时才给，提示词在 chat.py）；点了先问服务器能不能下，见下面的 click
    link({ href, tokens }) {
      const path = localPath(href);
      return path ? `<a class="file-dl" href="#" data-path="${esc(path)}" title="下载 ${esc(path)}">${this.parser.parseInline(tokens)}</a>` : false;
    },
  },
});

// 点下载链接：先让服务器查一遍（有没有、给不给下），不行就当场说为什么；行了再交给浏览器下载，大文件也不经过页面内存
document.addEventListener('click', async e => {
  const a = e.target.closest('a.file-dl');
  if (!a) return;
  e.preventDefault();
  if (a.classList.contains('busy')) return;
  const path = a.dataset.path, tid = a.closest('[data-tab]')?.dataset.tab;
  if (!tid) return fail(`下载「${path}」`, '无法确定所属会话');
  a.classList.add('busy');
  try {
    const url = `${fileUrl(tid, path)}&download=1`;
    let res;
    try { res = await fetch(`${url}&check=1`); } catch { throw new Error('无法连接服务器'); }
    if (!res.ok) throw new Error(await reason(res));
    const { name, size } = await res.json();
    const t = Object.assign(document.createElement('a'), { href: url, download: name });
    document.body.append(t);
    t.click();
    t.remove();
    say(`开始下载「${name}」（${fileSize(size)}）`);
  } catch (err) {
    fail(`下载「${path}」`, err);
  }
  a.classList.remove('busy');
});

// 代码上色很贵，一个长会话有几百个代码块。改成滚到跟前（上下各留 300px）才上色，每块只上一次
const hlSeen = new WeakSet();
const hlWatcher = new IntersectionObserver(entries => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    hlWatcher.unobserve(e.target);
    if (hlSeen.has(e.target)) continue;
    hlSeen.add(e.target);
    hljs.highlightElement(e.target);
  }
}, { rootMargin: '300px' });

// 渲染结果：条目对象当键存在弱引用表里，条目被丢弃时自动释放；
// 直接传字符串的地方（比如工具的 prompt）用一张小表兜底
const mdByItem = new WeakMap();
const mdByText = new Map();
// 同一段文字只渲染一次；组件每次更新都会调到这里，没变就直接跳过
const renderMd = (el, { value }) => {
  const obj = value && typeof value === 'object';
  const text = obj
    ? value.text || (value.kind === 'thinking' ? '（思考内容未记录）' : '')
    : value || '';
  if (el._md === text) return;
  el._md = text;
  let html = obj ? mdByItem.get(value) : mdByText.get(text);
  if (html === undefined) {
    html = DOMPurify.sanitize(marked.parse(text));
    if (obj) mdByItem.set(value, html);
    else {
      if (mdByText.size > 500) mdByText.delete(mdByText.keys().next().value);
      mdByText.set(text, html);
    }
  }
  el.innerHTML = html;
  el.querySelectorAll('pre code').forEach(c => hlWatcher.observe(c));
  addCopyButtons(el);
  prepareDrawings(el);
};

// 每个代码块右上角浮一个复制按钮。按钮要放在 pre 外面套一层：
// pre 自己会滚动，按钮搁里面会跟着内容滚走
function addCopyButtons(el) {
  for (const pre of el.querySelectorAll('pre')) {
    if (pre.parentElement.classList.contains('pre-wrap')) continue;
    const wrap = document.createElement('div');
    wrap.className = 'pre-wrap';
    pre.parentNode.insertBefore(wrap, pre);
    wrap.appendChild(pre);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'code-copy';
    btn.textContent = '复制';
    wrap.appendChild(btn);
  }
}

// 点哪个复制哪个：全站只挂一次，省得每个代码块各绑一份
document.addEventListener('click', async e => {
  const btn = e.target.closest('.code-copy');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const pre = btn.parentElement.querySelector('pre');
  const ok = await copyText((pre.querySelector('code') || pre).innerText);
  btn.textContent = ok ? '已复制' : '复制失败';
  btn.classList.add('done');
  clearTimeout(btn._t);
  btn._t = setTimeout(() => {
    btn.textContent = '复制';
    btn.classList.remove('done');
  }, 1400);
});

// 示意图和引用的图片也等滚到跟前才画、才取：一个长会话里可能有很多张
const drawWatcher = new IntersectionObserver(entries => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    drawWatcher.unobserve(e.target);
    // 条目不知道自己在哪个会话里，从所在的标签页取；取图都要带着它，后端按它认权限、算相对路径
    const tid = e.target.closest('[data-tab]')?.dataset.tab;
    (e.target.tagName === 'IMG' ? showFile : drawViz)(e.target, tid);
  }
}, { rootMargin: '300px' });

// 示意图：上面是图，源码（连同复制按钮）先藏着，右上角一个按钮来回切
function prepareDrawings(el) {
  for (const box of el.querySelectorAll('.viz')) {
    box.prepend(Object.assign(document.createElement('div'), { className: 'viz-view' }));
    box.append(Object.assign(document.createElement('button'), { type: 'button', className: 'viz-flip', textContent: '源码' }));
    drawWatcher.observe(box);
  }
  el.querySelectorAll('img[data-path]').forEach(img => drawWatcher.observe(img));
}

const fileUrl = (tid, path) => `/api/tabs/${tid}/file?path=${encodeURIComponent(path)}`;
const reason = async res => (await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`;

function showFile(img, tid) {
  if (!tid) return brokenImage(img, '无法确定所属会话');
  img.onerror = async () => brokenImage(img, await whyNot(img.src));
  img.src = fileUrl(tid, img.dataset.path);
}
// 图片加载失败时浏览器不给原因，再请求一次把服务器说的原因读出来
export async function whyNot(url) {
  try {
    const res = await fetch(url);
    return res.ok ? '浏览器无法显示此文件' : await reason(res);
  } catch {
    return '无法连接服务器';
  }
}
function brokenImage(img, why) {
  img.replaceWith(Object.assign(document.createElement('span'),
    { className: 'img-err', textContent: `图片无法显示：${img.dataset.path}（${why}）` }));
}

async function drawViz(box, tid) {
  const view = box.querySelector('.viz-view');
  try {
    await DRAW[box.dataset.viz](view, box.querySelector('pre code').textContent, tid);
  } catch (e) {
    box.classList.add('bad');   // 画不出来就写明原因，源码直接露出来
    view.textContent = `示意图渲染失败：${e.message || e}`;
  }
}

let vizSeq = 0;
const DRAW = {
  // 当成图片显示：SVG 自带的样式和脚本都关在图片里，漏不到页面上
  async svg(view, src, tid) {
    const svg = withNamespaces(await inlineImages(src, tid));
    const img = new Image();
    img.alt = '示意图';
    // 根元素没写宽度时图片默认只有 300×150：让它铺满，高度按 viewBox 的比例来
    if (!/\swidth=/.test(svg.match(/<svg\b[^>]*>/i)?.[0] || '')) img.style.width = '100%';
    await new Promise((ok, no) => {
      img.onload = ok;
      img.onerror = () => no(new Error('SVG 解析失败（可能有未闭合的标签或不支持的字符实体）'));
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    });
    view.replaceChildren(img);
  },
  async mermaid(view, src) {
    view.innerHTML = (await (await loadMermaid()).render(`viz-${++vizSeq}`, src)).svg;
  },
  // 放进只许跑脚本的小框：Claude 写的整页样式和全局变量不会把查看器搞乱
  async html(view, src, tid) {
    const frame = Object.assign(document.createElement('iframe'), { className: 'viz-frame' });
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.srcdoc = await inlineImages(src, tid) + REPORT_HEIGHT;
    view.replaceChildren(frame);
  },
};

// 当成图片显示的 SVG 必须是合规的 XML，Claude 常漏写命名空间，补上
const withNamespaces = svg => svg.replace(/<svg\b([^>]*)>/i, (tag, attrs) => '<svg'
  + (/\sxmlns=/.test(attrs) ? '' : ' xmlns="http://www.w3.org/2000/svg"')
  + (/xlink:/.test(svg) && !/\sxmlns:xlink=/.test(attrs) ? ' xmlns:xlink="http://www.w3.org/1999/xlink"' : '')
  + attrs + '>');

// 示意图里用本地路径引用的图片：图片形式的 SVG 和小框里都取不到服务器的文件（小框里也带不上看的人的登录），
// 所以画之前用看的人自己的身份把图取回来，换成内嵌数据
const REF = /\b((?:xlink:)?href|src)=(["'])(.*?)\2/g;
async function inlineImages(src, tid) {
  const paths = new Set([...src.matchAll(REF)].map(m => localPath(m[3])).filter(p => p && IMAGE_FILE.test(p)));
  if (!paths.size) return src;
  if (!tid) throw new Error('无法确定所属会话，引用的图片无法加载');
  const data = new Map(await Promise.all([...paths].map(async p => [p, await fetchDataUrl(fileUrl(tid, p), p)])));
  return src.replace(REF, (all, attr, q, ref) => (data.has(localPath(ref)) ? `${attr}=${q}${data.get(localPath(ref))}${q}` : all));
}

async function fetchDataUrl(url, path) {
  let res;
  try {
    res = await fetch(url);
  } catch {
    throw new Error(`无法加载 ${path}：无法连接服务器`);
  }
  if (!res.ok) throw new Error(`无法加载 ${path}：${await reason(res)}`);
  const blob = await res.blob();
  return new Promise((ok, no) => {
    const r = new FileReader();
    r.onload = () => ok(r.result);
    r.onerror = () => no(new Error(`读不出 ${path}`));
    r.readAsDataURL(blob);
  });
}

// 流程图的库有 5MB 多，会话里真出现流程图时才加载，只加载一次
let mermaidLoad = null;
function loadMermaid() {
  return mermaidLoad ||= new Promise((ok, no) => {
    const s = Object.assign(document.createElement('script'), { src: 'vendor/mermaid.min.js' });
    s.onload = () => {
      // 出错时不让它往页面上画自带的报错图，原因由 drawViz 写在示意图的位置。
      // 默认的样子往页面上靠：新版默认的 neo 外观给节点加投影，换成平的 classic；
      // 连线上的字垫白底，不用它按主色推出来的米黄。图里自己写了 look、edgeLabelBackground 的照它写的来
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true,
                           look: 'classic', themeVariables: { edgeLabelBackground: '#ffffff' } });
      ok(mermaid);
    };
    s.onerror = () => {
      mermaidLoad = null;   // 下一张流程图再试一次
      s.remove();
      no(new Error('流程图的库 vendor/mermaid.min.js 没加载下来'));
    };
    document.head.append(s);
  });
}

// 小框里是无主来源，外面量不到它多高，只能由框里的页面自己量了报出来
const REPORT_HEIGHT = '<script>new ResizeObserver(() => parent.postMessage({ vizHeight: '
  + 'document.documentElement.getBoundingClientRect().height }, "*")).observe(document.documentElement);</script>';
// 框的高度跟着内容走，最高到屏幕高度的 85%，再长就在框里滚：按屏幕高度写的页面（100vh）会随框一起长，不封顶就停不下来
addEventListener('message', e => {
  const h = e.data?.vizHeight;
  if (typeof h !== 'number') return;
  const frame = [...document.querySelectorAll('.viz-frame')].find(f => f.contentWindow === e.source);
  if (frame) frame.style.height = `${Math.min(Math.ceil(h), Math.round(innerHeight * 0.85))}px`;
});

// 示意图右上角的按钮：在图和源码之间来回切
document.addEventListener('click', e => {
  const btn = e.target.closest('.viz-flip');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  btn.textContent = btn.parentElement.classList.toggle('src') ? '图' : '源码';
});

export const vMd = { mounted: renderMd, updated: renderMd };
