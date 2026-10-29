// 返回键（手机的返回手势、浏览器的后退）先收起页面上最上面那一层，不离开页面（在手机 PWA 的设置页下意识按返回时，
// 不能直接退回浏览器的空白页）。做法：开着几层就在浏览器历史里多记几步（每步的 state 记着第几层），按返回落到下面一步时收起最上面那层；
// 用界面上的按钮收起的，把多记的那几步退掉，不然以后按返回要白按几下。
// 手机上什么都没开时也垫一步：按返回先提示一句，紧接着再按一次才真的离开（垫的这一步要等用户再点一下页面才补回去，见下）。
// 浏览器只认用户刚点过、按过键时记下的步：没有这个的会被返回键直接跳过（Chrome 防止网页拦返回键的规矩），
// 所以只在用户刚动过（navigator.userActivation.isActive）时补记，否则等下一次点按再补
const { computed, watch } = Vue;
import { touch, say } from './util.js';

const KEY = 'ccBack';

// layers：从最上面往下排的 [{ open: () => 开着没有, close: () => 收起 }]
export function useBack(layers) {
  const depth = computed(() => layers.filter(l => l.open()).length);
  let pushed = 0;          // 当前这一步在基底上面第几步
  let guard = touch;       // 手机上底下垫一步；按掉以后等用户再动一下才补
  let settling = false;    // 自己退掉多记的步，等它的 popstate 回来
  // 基底这一步打上记号：分享链接改 # 生出来的步、别的脚本 replaceState(null) 过的步，都不是这里记的
  history.replaceState({ ...(history.state || {}), [KEY]: 0 }, '');

  const active = () => !navigator.userActivation || navigator.userActivation.isActive;
  function sync() {
    if (settling) return;
    const want = (guard ? 1 : 0) + depth.value;
    if (want < pushed) {
      settling = true;
      history.go(want - pushed);
      pushed = want;
    } else if (want > pushed && active()) {
      while (pushed < want) history.pushState({ [KEY]: ++pushed }, '');
    }
  }

  window.addEventListener('popstate', e => {
    const at = e.state?.[KEY];
    if (settling) {
      settling = false;
      if (typeof at === 'number') pushed = at;
      return sync();
    }
    // 不是这里记的步（页面里点了改 # 的链接生出来的）：不当成返回，把它当成新的基底重新数
    if (typeof at !== 'number') {
      history.replaceState({ ...(history.state || {}), [KEY]: 0 }, '');
      pushed = 0;
      return;
    }
    let back = pushed - at;
    pushed = at;
    for (; back > 0; back--) {
      const top = layers.find(l => l.open());
      if (top) top.close();
      else {
        guard = false;
        say('再按一次返回键退出');
      }
    }
  });
  // 点按、按键时补记还差的步（这时浏览器认为是用户刚动过）；垫底的那一步也是这时补回去
  const poke = () => {
    if (touch && !guard && !settling && pushed === depth.value) guard = true;
    sync();
  };
  window.addEventListener('pointerdown', poke, true);
  window.addEventListener('keydown', poke, true);
  // 开关一层多半就在点按的处理里，这时补记最及时；收起了就把多的步退掉
  watch(depth, () => sync(), { flush: 'post' });
  // 不经过历史、由别处判定成「按了返回」时用（全屏时的返回键先被浏览器拿去退出全屏，见 app.js）：收起最上面那层，
  // 多记的那一步由上面的 watch 退掉。返回有没有收起东西
  function closeTop() {
    const top = layers.find(l => l.open());
    if (top) top.close();
    return !!top;
  }
  return { closeTop };
}
