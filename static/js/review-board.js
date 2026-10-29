// 开源版不含审稿看板（配合另一套自动审稿流程用的整页面板）。这里只留其余代码要用的名字，一律按「没开」走：
// reviewable 永远是假，入口不出现；组件只是占位，不会被打开。
const { reactive } = Vue;

export const REVIEW_PERSONA = '';
export const reviewable = () => false;

export function useReviewBoard() {
  const rb = reactive({ open: false });
  return { rb, reviewable, openReview: () => {}, closeReview: () => (rb.open = false) };
}

export const ReviewBoard = { props: { tab: Object }, emits: ['close'], template: '<div></div>' };
