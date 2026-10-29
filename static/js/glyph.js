// 看板上的小标签、标签和标签页名字里的字，要按字形中心在外形里上下居中，不按行框：同一行高里，
// 字形落在偏上还是偏下的位置随字体变（有的电脑上偏下，测试浏览器里偏上）。
// 认识 CSS text-box 的浏览器由样式按字体度量裁掉行框上下的空白（见 style.css「按字形居中」）；不认识的走这里：
// 在页面里放一个看不见的探针，用和那处一样的字体、字号、行高排一行字，读出基线落在行框里的哪儿、大写字母多高，
// 算出大写字母的中心比行框中心低多少像素，交给样式把行高减掉两倍偏差、空出来的补成一侧的内边距，
// 字整体挪回中心，外形高度不变。
// 不用画布量：画布不认 system-ui 这类字体关键字，量到的是别的字体。也不能用大字号量完按比例缩：
// 浏览器排小字号时把字体的上伸、下伸取成整数像素，基线位置和按比例算的差出半个多像素，所以按实际字号、行高各量各的。
// 量的是大写字母和数字的中心；汉字的中心比它高一点点（约 0.02 个字高），肉眼看不出来

// 要居中的几处：变量名前缀、字号、行高（和 style.css 里那几处一致：小标签 12px、标签 12px 配 18px 行高、标签页 14px，
// 没写行高的继承正文的行高系数 --lh）
const SPOTS = [['chip', 12, null], ['tag', 12, 18], ['tab', 14, null]];

// 按给定的字号、行高排一行（fix 是已经打算挪的像素：行高减掉两倍、补成一侧的内边距，和样式里的做法一样），
// 返回大写字母中心比整块中心低多少像素
function lowBy(fontSize, lineHeight, fix = 0) {
  const line = lineHeight || fontSize * parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--lh'));
  const probe = document.createElement('div');
  probe.setAttribute('aria-hidden', 'true');
  Object.assign(probe.style, { position: 'fixed', left: '-9999px', top: '0', visibility: 'hidden', whiteSpace: 'nowrap',
                               fontSize: `${fontSize}px`, lineHeight: `${line - 2 * Math.abs(fix)}px`,
                               paddingTop: `${Math.max(0, -2 * fix)}px`, paddingBottom: `${Math.max(0, 2 * fix)}px` });
  // 基线标记：高 0 的行内块，底边就落在基线上；大写字母高：1cap 高的块（不认 cap 单位的浏览器高度是 0，按 0.7 个字高估）
  probe.innerHTML = 'H<span style="display:inline-block;width:1px;height:0"></span>'
                  + '<span style="display:inline-block;width:1px;height:1cap;vertical-align:top"></span>';
  document.body.append(probe);
  const box = probe.getBoundingClientRect();
  const [mark, cap] = probe.querySelectorAll('span');
  const baseline = mark.getBoundingClientRect().bottom - box.top;
  const capH = cap.getBoundingClientRect().height || 0.7 * fontSize;
  probe.remove();
  return baseline - capH / 2 - box.height / 2;
}

export function fitGlyphs(force = false) {
  if (!force && CSS.supports('text-box', 'trim-both cap alphabetic')) return;
  const root = document.documentElement;
  for (const [name, fontSize, lineHeight] of SPOTS) {
    // 缩了行高以后，浏览器把行距的一半取整的结果会变，按挪过的样子再排一次、把剩下的偏差补上
    let low = lowBy(fontSize, lineHeight);
    low += lowBy(fontSize, lineHeight, low);
    if (!Number.isFinite(low)) continue;
    root.style.setProperty(`--glyph-${name}-cut`, `${2 * Math.abs(low)}px`);
    root.style.setProperty(`--glyph-${name}-pt`, `${Math.max(0, -2 * low)}px`);
    root.style.setProperty(`--glyph-${name}-pb`, `${Math.max(0, 2 * low)}px`);
  }
  root.classList.add('fix-glyph');
}
