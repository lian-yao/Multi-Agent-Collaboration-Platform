/**
 * AgentGlyph.tsx — Agent 角色图标（`role` → 图形），配置页与协作画布共用这一处解析。
 *
 * ## 为什么要有这个模块
 *
 * 两处原先各画各的：配置页的角色卡片把**显示名的第一个字**当 monogram 塞进 34px 方块
 * （「信」「数」「报」），协作画布的节点一律画机器人。于是同一个 Agent 在两个页面长得
 * 不一样；而单字中文方块既认不出是谁，又和旁边的 Provider 品牌标不同形，看着像还没
 * 写完的占位符。
 *
 * 现在只有一处判断，两边都调 `AgentGlyph`，所以**同一个角色在两处必然同形** —— 这是
 * 结构性保证，而不是靠两边各自记得改。
 *
 * ## 三条口径
 *
 * - **先按 `role`，再按显示名，最后回退机器人。** `role` 是角色目录里的稳定语义键
 *   （`collector` / `analyst` / `reporter`，自定义角色为自由文本键，见 `doc/api.md` §5.7）。
 *   先查 `role` 是为了让「把『信息收集 Agent』改叫别的名字」不至于把图标一起换掉。
 * - **回退值是机器人。** 协作画布的 Agent 节点本来就画机器人；拿它当兜底，含义是
 *   「没有更贴切的语义时就画通用 Agent」，而不是再退回一个字母。
 * - **`AGENT_ICON_RULES` 的顺序即优先级**，靠前的先匹配，所以更具体的关键词必须排在
 *   更宽泛的前面（`报告` 要排在 `信息` 前面，否则「信息报告 Agent」会被判成收集）。
 *
 * ## 边界
 *
 * 图标只表达**角色语义**，不表达状态。执行中 / 失败 / 暂停由视图自己覆盖（见
 * `workspace/GraphCanvas.tsx` 的 `NodeGlyph`）——状态是人要立刻看见的信号，不能被
 * 角色图标盖掉。反过来，配置页没有状态诉求，直接用角色图标。
 *
 * 加一张图标：往 `AGENT_ICON_RULES` 加一行即可，键名同时是 `AGENT_ICON_GLYPHS` 的键。
 */

import {
  Bot,
  Calculator,
  ChartLine,
  ClipboardList,
  Code,
  Database,
  FileSearch,
  FileText,
  Image,
  Languages,
  ListChecks,
  MessageSquare,
  Microscope,
  PenLine,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";

/**
 * 图标键。`bot` 是**回退键**，其余都对应一类角色语义；
 * 键名同时用作 `AGENT_ICON_RULES` 的优先级标识，取语义名而不是图标名，
 * 这样换图标（比如 `chart` 从折线换成柱状）不必动匹配规则。
 */
export type AgentIconKey =
  | "report"
  | "summary"
  | "write"
  | "translate"
  | "review"
  | "plan"
  | "code"
  | "chart"
  | "compute"
  | "search"
  | "research"
  | "knowledge"
  | "converse"
  | "media"
  | "bot";

/** 角色关键词 → 图标键。**顺序即优先级**，具体在前、宽泛在后。 */
const AGENT_ICON_RULES: readonly (readonly [AgentIconKey, readonly string[]])[] = [
  // 交付物明确的排最前：「报告」是这条流水线的终点产物，不该被上游语义抢走
  ["report", ["report", "报告", "汇报"]],
  ["summary", ["summar", "总结", "归纳", "综述", "摘要"]],
  ["write", ["write", "writer", "author", "draft", "撰写", "写作", "文案", "起草"]],
  ["translate", ["translat", "翻译", "多语言"]],
  ["review", ["review", "critic", "audit", "verify", "审核", "评审", "校验", "质检", "复核"]],
  [
    "plan",
    ["plan", "orchestrat", "coordinat", "schedul", "workflow", "规划", "编排", "调度", "协调", "工作流"],
  ],
  ["code", ["code", "coder", "program", "develop", "engineer", "编码", "开发", "工程"]],
  ["chart", ["analyst", "analys", "analyz", "analysis", "分析", "数据", "指标", "metric", "statistic"]],
  ["compute", ["math", "calcul", "compute", "计算", "数值"]],
  ["search", ["collect", "gather", "retriev", "search", "crawl", "fetch", "收集", "检索", "采集", "抓取", "信息"]],
  ["research", ["research", "investigat", "调研", "研究", "探究"]],
  ["knowledge", ["knowledge", "memory", "rag", "vector", "embedding", "知识", "记忆"]],
  ["converse", ["chat", "dialog", "conversat", "问答", "对话", "客服"]],
  ["media", ["image", "vision", "video", "audio", "speech", "multimodal", "图像", "视觉", "视频", "语音", "多模态"]],
];

/**
 * 图标键 → 图形。导出出来是为了让冒烟能逐键核对（键与图形错配是静默的：
 * 界面照常渲染，只是画了一个不相干的图标）。
 */
export const AGENT_ICON_GLYPHS: Record<AgentIconKey, LucideIcon> = {
  report: FileText,
  summary: ClipboardList,
  write: PenLine,
  translate: Languages,
  review: ShieldCheck,
  plan: ListChecks,
  code: Code,
  chart: ChartLine,
  compute: Calculator,
  search: FileSearch,
  research: Microscope,
  knowledge: Database,
  converse: MessageSquare,
  media: Image,
  bot: Bot,
};

/** 回退键：没匹配到任何语义时画通用 Agent。 */
export const FALLBACK_AGENT_ICON: AgentIconKey = "bot";

/**
 * 全部图标键（图标选择器的候选清单）。顺序即 `AGENT_ICON_GLYPHS` 的声明顺序，
 * 与 `AGENT_ICON_RULES` 无关——选择器要的是「有哪些可挑」，不是匹配优先级。
 */
export const AGENT_ICON_KEYS = Object.keys(AGENT_ICON_GLYPHS) as AgentIconKey[];

function matchIconKey(haystack: string): AgentIconKey | null {
  const text = haystack.toLowerCase();
  for (const [key, words] of AGENT_ICON_RULES) {
    if (words.some((word) => text.includes(word))) return key;
  }
  return null;
}

/**
 * 角色 → 图标键。先按 `role` 全表扫一遍，再把显示名当第二判据。
 *
 * 两轮扫而不是把两者拼成一个串：拼串会让**名字里的关键词压过 role**，
 * 而 `role` 才是稳定语义键（见模块开头的口径）。
 */
export function agentIconKey(role: string, name = ""): AgentIconKey {
  return matchIconKey(role) ?? matchIconKey(name) ?? FALLBACK_AGENT_ICON;
}

/**
 * 角色图标。装饰性元素（旁边永远有名字或 `aria-label`），因此标 `aria-hidden`。
 */
export function AgentGlyph({
  role,
  name,
  icon,
  size = 16,
  className,
}: {
  role: string;
  /** 显示名；`role` 没匹配上时的第二判据。 */
  name?: string;
  /**
   * 显式图标键（ADR-036：角色目录的 `icon` 列）。**优先级最高**——用户特意挑过的
   * 图标不该被推断规则翻案；键不在图标集里时按未指定处理（推断 + 兜底）。
   */
  icon?: string | null;
  size?: number;
  className?: string;
}) {
  const explicit =
    icon && icon in AGENT_ICON_GLYPHS ? (icon as AgentIconKey) : null;
  const Icon = AGENT_ICON_GLYPHS[explicit ?? agentIconKey(role, name)];
  return <Icon size={size} className={className} aria-hidden="true" />;
}
