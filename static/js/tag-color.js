// 标签的颜色：默认按名字自动挑一种（同一个名字在哪台设备上都一样），也能自己挑。
// 色板只定色相，深浅和饱和度由样式统一压低，所以都是清新的淡色，深色模式下也不刺眼
const { reactive } = Vue;

export const PALETTE = [
  { name: '薄荷', h: 155 }, { name: '天青', h: 195 }, { name: '雾蓝', h: 222 }, { name: '薰衣草', h: 262 },
  { name: '藕粉', h: 330 }, { name: '蜜桃', h: 18 }, { name: '杏黄', h: 42 }, { name: '青柠', h: 88 },
];

// 标签名 → 用户挑的色号，从服务器取（/api/tags 的 colors）
export const tagColors = reactive({});
export function setTagColors(colors) {
  for (const k of Object.keys(tagColors)) delete tagColors[k];
  Object.assign(tagColors, colors || {});
}

// 按名字算一个稳定的色号：同一个名字永远是同一种颜色
export function autoColor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return h % PALETTE.length;
}
export const tagHue = name => PALETTE[tagColors[name] ?? autoColor(name)]?.h ?? PALETTE[0].h;
export const tagStyle = name => ({ '--h': tagHue(name) });
