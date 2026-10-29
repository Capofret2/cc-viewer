// 额度计划（后端 users.grant_plan）：一笔总额分几期发，默认 4 期、每 7 天一期。
// 账号页给本人看还在发的计划（me.plans），用户管理里给查看器账号看某人的全部计划（含撤销了、发完了的）。
// 一条进度条：每期一个圆点，发了的实心打勾、下一期描主题色边；轨道上走过的时间填主题色，走到此刻。圆点下面写这一期的日期和金额。
// 撤销了就当没发生过（users.revoke_plan）：每期都画成虚线、划掉，轨道不填
import { fen } from './points.js';

export const PLAN_PARTS = 4, PLAN_DAYS = 7;   // 和后端 users.PLAN_PARTS、PLAN_DAYS 一致，界面上只填总额
export const dayText = s => { const d = new Date(s * 1000); return `${d.getMonth() + 1}月${d.getDate()}日`; };
const money = p => fen(p, 2);

export const GrantPlan = {
  props: { plan: Object },
  computed: {
    items() { return this.plan.items || []; },
    span() { const it = this.items; return it.length > 1 ? it[it.length - 1].due - it[0].due : 0; },
    // 页面开着的时候不跟着走（一分钟挪不到一个像素），刷新、重取时按那时的时刻画
    fill() {
      const it = this.items;
      if (this.plan.revoked) return 0;
      if (!this.span) return it[0]?.done ? 100 : 0;
      return Math.max(0, Math.min(100, (Date.now() / 1000 - it[0].due) / this.span * 100));
    },
    nextI() { return this.items.find(it => !it.done && !it.dropped)?.i; },
    title() { return this.plan.note || `${dayText(this.plan.start)}起的额度计划`; },
  },
  methods: {
    pos(it) { return this.span ? (it.due - this.items[0].due) / this.span * 100 : 0; },
    money, dayText,
  },
  template: `<div class="gp" :class="{ revoked: plan.revoked }">
    <div class="gp-head"><b class="gp-title">{{ title }}</b><b v-if="plan.revoked" class="paid-mark">已撤销</b>
      <small v-if="plan.revoked">原计划 {{ money(plan.total) }}，已全部收回</small>
      <small v-else>已发 <em>{{ money(plan.given) }}</em> / {{ money(plan.total) }}</small></div>
    <div class="gp-track">
      <div class="gp-rail"><i class="gp-fill" :style="{ width: fill + '%' }"></i></div>
      <span v-for="it in items" :key="it.i" class="gp-dot" :class="{ done: it.done, next: it.i === nextI, dropped: it.dropped }"
            :style="{ left: pos(it) + '%' }"><svg v-if="it.done" viewBox="0 0 12 12"><path d="M2.6 6.3 5 8.6l4.4-4.9"/></svg></span>
    </div>
    <div class="gp-labels">
      <span v-for="(it, k) in items" :key="it.i" class="gp-label"
            :class="{ first: k === 0, last: k === items.length - 1 && k > 0, done: it.done, dropped: it.dropped }"
            :style="{ left: pos(it) + '%' }"><b>{{ dayText(it.due) }}</b><span>{{ money(it.amount) }}</span></span>
    </div>
  </div>`,
};
