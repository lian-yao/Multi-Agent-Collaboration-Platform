# ADR-040：外观跟随系统，靠一层生成的颜色令牌实现

日期：2026-09-23 ｜ 状态：**已落地** ｜ 关联：ADR-021（手写 CSS，不引框架）、ADR-031（对话流渲染），`doc/testing.md` §4.26

## 背景

用户提出：「增加一个暗色模式，默认与系统保持一致，可切换，切换按钮替换放在
`class="lucide lucide-settings2"`」（页脚那个不通向任何设置的图标）。

动手前先量了一次成本。6 个样式表里的颜色是散落字面量，共 **826 处、459 个不同的
（角色, 色值）组合**：

| 文件 | 颜色字面量 |
| --- | --- |
| `src/styles.css` | 519 |
| `src/config/config.css` | 180 |
| `src/workspace/workspace.css` | 66 |
| `src/records/records.css` | 44 |
| `src/components/inline-confirm.css` / `page-tabs.css` | 9 / 8 |

由此要否掉一条默认直觉：**「再写一份暗色样式表」**。那等于让人的注意力去兜完备性 ——
826 处各判一次，漏一处就是一块死色，而且**没有任何可跑的判据**能说「暗色覆盖完了」。

## 决策

### 1. 颜色先收敛成令牌，再让令牌随主题变

新增 `frontend/src/theme.css`（**由脚本生成，不手改**），声明 459 个 `--t-{角色}-{色值}`：

```css
:root { --t-bg-ffffff:#ffffff; /* … 459 条 */ }
:root[data-theme="dark"] { --t-bg-ffffff:#2a2f32; /* … 同一批名字，另一批取值 */ }
```

6 个样式表里的 826 处字面量全部换成 `var(--t-…)`。

**浅色块是恒等映射**：`--t-bg-ffffff` 的浅色取值就是 `#ffffff`。令牌名自带原值，于是
「浅色渲染一个像素没变」从一句承诺变成**可机检的命题**，不需要留一份改动前的样式表当基线。

角色按「同一个属性上的同一个色值」聚类，四类：`bg`（背景）、`ink`（前景/描边/填充）、
`bd`（边框/分隔线）、`shadow`。换值的规则是**背景保序、前景反序**：浅色里越浅的底色，
在暗色里仍是靠亮的那几档；而浅色里越深的文字，在暗色里越亮。暗色取值不改色相
（保留浅色那点青灰），只重排明度与饱和度。

### 2. 由脚本生成，不手写

`frontend/rendercheck/theme-tokens.py` 四个子命令：

- `preview`：只报告会改哪些、改成什么，不落盘；
- `apply`：一次性把字面量换成令牌（跑过一次后再跑会拒绝，因为已经没有字面量可换）；
- `regenerate`：浅色值在 `theme.css` 里被调过之后，按同一套映射重算暗色块；
- `verify`：三项校验 —— 令牌闭合（每个 `var(--t-*)` 在**两个**块里都有声明）、
  浅色恒等（浅色取值 == 令牌名里那串）、暗色对比度（浅色合格的前景/背景组合在暗色下仍达标）。

生成物的头部写了「本文件由 `theme-tokens.py` 生成」，免得下一个人直接手改。

### 3. 三态偏好，默认跟随系统

- **用户态** `system | light | dark`，存 `localStorage["macp-theme"]`；
  **`system` 就是不写这个键**（不是写 `"system"`）—— 存储里没有键 = 跟随系统，这一条在首屏脚本、
  `theme.ts`、验证脚本三处是同一个口径。
- **生效态**写 `<html data-theme="light|dark">`，CSS 只认这个属性。
- 切换循环 `system → dark → light → system`。
- 跟随系统期间订阅 `matchMedia("(prefers-color-scheme: dark)")` 的 change，系统外观变了**实时**跟。
- 一旦显式选了 light/dark，就不再受系统影响。

### 4. 首屏不能闪：`index.html` 里的一段同步脚本

放在样式表**之前**、同步执行，先读 `localStorage` + `matchMedia` 落好
`data-theme` / `color-scheme` / `meta[theme-color]`；水合之后交给 `src/theme/theme.ts` 的
`useTheme()` 接管与监听。两处口径必须一致，所以都写了指向对方的注释。

### 5. 切换按钮不能把页脚撑高

页脚原本是 `v0.1.0` + 一个 **15px** 的 `Settings2` 图标。第一版按钮带 `padding:2px 6px`，
页脚从 15px 长到 20px；侧栏是纵向 flex、底部锚定，于是「运行时」卡被整体顶上去 5px ——
那是**与外观无关的位移**。现在按钮是 `height:15px; padding:0 6px`，与它替换掉的那个图标等高。

### 6. 颜色不能落到 UA 的 `buttontext`

`<button>` 没有被作者样式指定 `color` 时，浏览器给的 `buttontext` 会随 `color-scheme` 变
（浅色黑、暗色白）。那个颜色**不受令牌管辖**：暗色下它自己变白，看着"对"，但主题其实没管到它，
「全站颜色都由令牌解释」的口径就漏了。凡此类按钮显式写 `color:inherit`
（本轮：`.suggestion` / `.cfg-provider-item` / `.cfg-switch`）。

## 备选方案

- **再写一份暗色样式表 / 在 `@media (prefers-color-scheme: dark)` 里覆写几十条规则。**
  否决：826 处要人工各判一次，完备性没有判据；而且「跟随系统」与「手动切换」会分裂成
  媒体查询与 class 两套真相，两边都得维护。
- **只在 `<html>` 上加 class、把颜色写两遍。** 否决：同上，只是把重复换了个位置。
- **靠 `color-scheme` 让浏览器自己反色。** 否决：它只管表单控件与滚动条那一层，
  手写的背景、边框、文字一概不理 —— 结果是"一半变了"，比不做更难解释。
- **引第三方主题框架或 CSS 变量库。** 否决：ADR-021 已定不引 CSS 框架；本方案多出来的
  只有自定义属性这一个原语，没有多一层依赖。

## 代价

- 样式表里多了一层间接：读到一个 `var(--t-bg-eaf0f3)` 得去 `theme.css` 才知道具体值。
  缓解：令牌名末尾就是浅色原值，`verify` 会强制这条恒等。
- 改浅色值不能再直接改样式表 —— 要么同时改 `theme.css` 的两处（浅色 + `regenerate`），
  要么加回字面量再跑 `apply`（那会按新值重排全部令牌名）。这件事写在 `theme.css` 头部与本节。
- 品牌色（`src/config/providerIcons.gen.ts` 里各家模型的标识色）**故意不进令牌**：
  换主题不该改品牌色。验证脚本里它们是一份白名单。

## 影响

- 新增：`frontend/src/theme.css`（生成）、`frontend/src/theme/theme.ts`、
  `frontend/rendercheck/theme-tokens.py`。
- `frontend/index.html`：首屏脚本 + `meta[name="theme-color"]`。
- `frontend/src/main.tsx`：引入 `theme.css`。
- `frontend/src/App.tsx`：页脚 `Settings2` → `.theme-toggle`。
- `frontend/src/styles.css`（含 `.theme-toggle` 新增 5 条规则）、`workspace/workspace.css`、
  `config/config.css`、`records/records.css`、`components/inline-confirm.css`、
  `components/page-tabs.css`：826 处字面量 → `var(--t-…)`。
- `doc/testing.md` §4.26。

## 教训

- 「再写一份暗色样式表」是这类需求的默认直觉，但它把**完备性**交给了人的注意力。
  把颜色先收敛成一层有名字的令牌，才让「暗色覆盖到了没有」变成一条能跑的命令。
- 页面里最隐蔽的漏网颜色不是写死的十六进制，而是**浏览器给的默认值**。
  找它们只能靠扫计算样式，`grep` 字面量一条都抓不到。
