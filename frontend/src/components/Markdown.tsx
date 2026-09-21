/**
 * 消息正文的 Markdown 渲染（GFM）。
 *
 * ## 为什么必须有这一层
 *
 * 原先正文就是 `<p>{content}</p>`：模型按 Markdown 组织输出（`**加粗**`、`## 标题`、
 * `- 列表`、`| 表格 |`、围栏代码块），浏览器只会把那些记号**原样吐出来**。报告越长，
 * 记号越多，满屏星号与竖线——这不是排版不好看的问题，是「模型表达的结构全部丢失」，
 * 读的人得自己在脑子里把 Markdown 解析一遍。
 *
 * ## 口径
 *
 * - **只做渲染，不做清洗。** 正文来自模型，但 `react-markdown` 默认就不解析裸 HTML
 *   （不挂 `rehype-raw`），因此不需要再做一层消毒；这条要保住，别为了「支持 HTML」
 *   去挂 `rehype-raw`——那等于把模型输出直接当 DOM 执行。
 * - **表格自己带横向滚动容器。** 报告里的表格常常比对话列宽，靠外层 `overflow` 会把
 *   整段正文一起切掉；只有表格需要滚，就只让表格滚。
 * - **代码块的语言名留在 `data-lang` 上**，由 CSS 画角标。不引高亮库：着色器会再拖进
 *   一套主题与解析器，而这里要的是「看得出来这是代码」，不是编辑器级着色。
 * - **外链一律新开页**，并带 `rel="noreferrer noopener"`，不把当前会话导航掉。
 *
 * 渐进揭示（`useStreamText`）只作用于**显示**，不改内容；它的边界见那个模块的注释。
 */
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useStreamText } from "./useStreamText";

/** react-markdown 会把 AST 节点作为 `node` 透传给自定义组件；它不能落到 DOM 上。 */
type MdProps<T> = Omit<T, "node"> & { node?: unknown };

function Link({ node: _node, children, href, ...rest }: MdProps<ComponentPropsWithoutRef<"a">>) {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" {...rest}>
      {children}
    </a>
  );
}

function Table({ node: _node, children }: MdProps<{ children?: ReactNode }>) {
  return (
    <div className="md-table-wrap" role="region" tabIndex={0} aria-label="可横向滚动的表格">
      <table>{children}</table>
    </div>
  );
}

function Pre({ node: _node, children }: MdProps<{ children?: ReactNode }>) {
  return <pre className="md-pre">{children}</pre>;
}

/**
 * 行内代码与代码块共用 `<code>`，靠 `pre` 祖先区分（见样式表 `.md-pre .md-code`）。
 * 这里只负责把语言名摘到 `data-lang`，不走「行内/块级」的判断——那个判断在 SSR 与
 * 客户端两条渲染路径上容易分叉，交给 CSS 反而只有一处。
 */
function Code({ node: _node, className, children, ...rest }: MdProps<ComponentPropsWithoutRef<"code">>) {
  const language = /language-([\w+#-]+)/.exec(className ?? "")?.[1] ?? "";
  return (
    <code className="md-code" data-lang={language} {...rest}>
      {children}
    </code>
  );
}

/** GFM 任务列表的复选框是只读展示；留下可点的框会让用户以为能勾。 */
function TaskItem({ node: _node, ...rest }: MdProps<ComponentPropsWithoutRef<"input">>) {
  return <input {...rest} disabled readOnly />;
}

const COMPONENTS: Components = {
  a: Link,
  table: Table,
  pre: Pre,
  code: Code,
  input: TaskItem,
};

export function Markdown({
  children,
  stream = false,
}: {
  children: string;
  /** 是否播放渐进揭示。历史消息传 `false`。 */
  stream?: boolean;
}) {
  const { visible, streaming } = useStreamText(children, stream);
  return (
    <div className={`md-body${streaming ? " is-streaming" : ""}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {visible}
      </ReactMarkdown>
    </div>
  );
}
