# ADR-021: 前端样式实现偏离「TailwindCSS」建议方案

状态：已接受（记录现状；改回 Tailwind 需新 ADR 与排期）

## 背景

设计事实源的建议方案把前端写成「React + Vite + TailwindCSS」，`AGENTS.md` 技术栈一栏
沿用同一表述。实际交付的前端是 **React + Vite + 手写 CSS**：`frontend/package.json`
里没有 tailwind 依赖（依赖仅 react/react-dom/lucide/morphicons），样式落在
`styles.css`（4249 行）、`config.css`（1834 行）、`workspace.css`（346 行）、
`records.css`（329 行）、`page-tabs.css`（84 行），合计约 6800 行，由配置页、工作台、
记录页三个视图分区各自持有（ADR-018）。

## 决策

1. **接受现状，不在本期迁移 Tailwind**：约 6800 行既有样式在三个视图分区内已经稳定，
   迁移是一次纯样式重写，收益（构建体积、开发效率）低于风险（回归、演示前返工），
   且与 M5 已闭环的状态冲突。
2. **门禁维持现状并按现状记录**：前端没有单元测试框架，门禁是
   `npm --prefix frontend run build`（`tsc --noEmit && vite build`）+
   `frontend/rendercheck/` 的 jsdom 渲染冒烟 + 人工浏览器核对清单
   （`doc/testing.md` §3.3、`doc/deployment.md`「演示与验收」）。
3. **口径同步范围**：`AGENTS.md` 技术栈一栏改为与实现一致（React + Vite + 手写 CSS，
   指向本 ADR）；`doc/15 ...平台.md` 的「建议方案」属事实源，修改需人类同意，
   本次未改——若同意，按「React + Vite（样式手写 CSS，见 ADR-021）」修订。

## 备选与未采纳

- **现在迁移 Tailwind**：需重写约 6800 行样式并重跑三页渲染冒烟与人工核对，
  在本期交付窗口内属高风险项，未采纳。
- **引入 Tailwind 只为新页面**：会出现两套样式体系并存，可维护性更差，未采纳。
- **把 CSS 拆成组件级样式**：属重构，需独立任务与归属人确认，未纳入本次。

## 影响

- 报告与答辩口径需与实现一致：说明前端样式为手写 CSS（按视图分区组织），
  而非 TailwindCSS；差距已在 ADR 中留痕，不构成「文档漂移」。
- 若后续要做 Tailwind 迁移，需新 ADR + 视觉回归方案（至少覆盖三个视图分区的渲染冒烟）。
