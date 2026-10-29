// 新会话标签页上的颜文字：每开一个随机挑一个。有几个会动（眨眼、心跳、闪光、打呼），每帧只换一两个字符
const { ref, onMounted, onUnmounted } = Vue;

// 不动的就是一个字符串；会动的写成 [[这一帧, 停多少毫秒], …]
export const KAOMOJI = [
  'ꉂ(˵˃ ᗜ ˂˵)', '٩(ˊᗜˋ*)و', 'ฅ(＾・ω・＾ฅ)', "(ฅ'ω'ฅ)", '=^..^=', '(=｀ω´=)', 'ヾ(≧▽≦*)o',
  '(*´▽｀*)', "(〃'▽'〃)", '(｀・ω・´)', '(・∀・)', 'o(*￣▽￣*)ブ', '(ﾉ>ω<)ﾉ', '( ´ ▽ ｀ )ﾉ',
  '(っ´ω｀c)', '(^._.^)ﾉ', '/ᐠ˵- ⩊ -˵マ', 'ฅ/ᐠ. ̫ .ᐟ\\ฅ', 'ᗜ⩊ᗜ',
  [['(=^･ω･^=)', 2600], ['(=^ｰωｰ^=)', 150]],          // 眨眼：闭眼用半角的长音符，和半角的点一样宽
  [['₍^. .^₎⟆', 2600], ['₍^- -^₎⟆', 150]],
  [['≽^•⩊•^≼', 2600], ['≽^-⩊-^≼', 150]],
  [['(｡･ω･｡)ﾉ♡', 700], ['(｡･ω･｡)ﾉ♥', 350]],          // 心跳
  [['( •̀ ω •́ )✧', 600], ['( •̀ ω •́ )✦', 600]],
  [['✧٩(ˊωˋ*)و✦', 600], ['✦٩(ˊωˋ*)و✧', 600]],        // 两头轮流闪
  [['⋆˚🐾˖°', 600], ['˚⋆🐾°˖', 600]],                  // 星星和小点换位置，像在闪
  [['/ᐠ_ ꞈ _ᐟ\\', 1200], ['/ᐠ_ ꞈ _ᐟ\\ɴʏᴀ', 400], ['/ᐠ_ ꞈ _ᐟ\\ɴʏᴀ~', 1800]],   // 一个字一个字喵出来
  [['( =ω=)..z', 700], ['( =ω=)..zz', 700], ['( =ω=)..zzZ', 900]],               // 打呼
  [['≽^-⩊-^≼ ᶻ', 700], ['≽^-⩊-^≼ ᶻz', 700], ['≽^-⩊-^≼ ᶻzZ', 900]],
];
export const randomKao = () => KAOMOJI[Math.floor(Math.random() * KAOMOJI.length)];

// 各帧叠在同一格里只亮当前这帧：格子按最宽的那帧撑开，字符宽窄不同时脸也不会左右晃。
// 系统设了「减少动态效果」就停在第一帧
export const KaoFace = {
  props: { kao: { type: [String, Array], required: true } },
  setup(props) {
    const frames = Array.isArray(props.kao) ? props.kao : [[props.kao, 0]];
    const at = ref(0);
    let timer = 0;
    const step = () => (timer = setTimeout(() => { at.value = (at.value + 1) % frames.length; step(); },
                                           frames[at.value][1]));
    onMounted(() => { if (frames.length > 1 && !matchMedia('(prefers-reduced-motion: reduce)').matches) step(); });
    onUnmounted(() => clearTimeout(timer));
    return { frames, at };
  },
  template: `<span class="kao-frames"><span v-for="(f, i) in frames" :key="i" :class="{ on: i === at }"
                                            :aria-hidden="i !== at">{{ f[0] }}</span></span>`,
};
