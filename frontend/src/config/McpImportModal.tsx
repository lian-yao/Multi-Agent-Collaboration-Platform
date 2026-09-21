import { useMemo, useState } from "react";
import { api } from "../api/client";
import {
  Chip,
  Field,
  Modal,
  describeError,
  truncate,
} from "./shared";
import {
  MCP_IMPORT_PLACEHOLDER,
  MCP_IMPORT_SHAPE_LABEL,
  TRANSPORT_LABELS,
  TRANSPORT_TINT,
  draftSummary,
  draftToPayload,
  parseMcpImport,
  type McpImportDraft,
  type McpImportIssue,
  type McpImportParseResult,
} from "./mcpConfig";

/** 提交结果：成功登记的 ID 与逐条失败原因（含「ID 已存在」这类后端拒绝）。 */
type Outcome = { created: string[]; failed: McpImportIssue[] };

/**
 * 粘贴导入：把外部 MCP 客户端的一份 JSON 配置翻成本项目的 Server 条目。
 *
 * 与「新建 Server」的分工——表单适合从零手填一个，导入适合从别处整份搬过来
 * （可能一次搬多个）。解析与校验在 `mcpConfig.ts`，本组件只管展示与提交。
 */
export function McpImportModal({
  existingIds,
  initialText = "",
  onClose,
  onSaved,
}: {
  /** 已登记的 Server ID，用于在提交前标出「会跳过」的条目。 */
  existingIds: readonly string[];
  /** 预填内容：便于「从别处复制过来先看一眼」，也让渲染冒烟能覆盖有内容的分支。 */
  initialText?: string;
  onClose: () => void;
  onSaved: (message: string) => Promise<void>;
}) {
  const [text, setText] = useState(initialText);
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const known = useMemo(() => new Set(existingIds), [existingIds]);

  /** 解析是纯函数且很便宜，直接在渲染期算，省掉一份需要同步的状态。 */
  const parsed = useMemo<{ result: McpImportParseResult | null; error: string }>(() => {
    try {
      return { result: parseMcpImport(text), error: "" };
    } catch (cause) {
      const reason =
        cause instanceof SyntaxError
          ? `JSON 格式有误：${cause.message}`
          : describeError(cause, "配置解析失败。");
      return { result: null, error: reason };
    }
  }, [text]);

  const drafts = parsed.result?.drafts ?? [];
  const issues = parsed.result?.issues ?? [];
  const duplicates = drafts.filter((draft) => known.has(draft.id));
  const fresh = drafts.length - duplicates.length;

  const submit = async () => {
    if (!drafts.length || saving) return;
    setSaving(true);
    const created: string[] = [];
    const failed: McpImportIssue[] = [];
    // 逐条提交：后端只有单条创建接口（§5.11），一条失败不该拖垮其余条目，
    // 因此这里串行尝试并把每条的结果分别汇报。
    for (const draft of drafts) {
      try {
        const server = await api.createMcpServer(draftToPayload(draft));
        created.push(server.id);
      } catch (cause) {
        failed.push({ source: draft.id, reason: describeError(cause, "登记失败。") });
      }
    }
    setSaving(false);
    if (created.length) {
      await onSaved(`已登记 ${created.length} 个 MCP Server：${created.join("、")}。`);
    }
    if (failed.length) {
      setOutcome({ created, failed });
      return;
    }
    onClose();
  };

  const footer = (
    <>
      <button
        type="button"
        className="cfg-primary"
        onClick={() => void submit()}
        disabled={!drafts.length || saving}
      >
        {saving ? "登记中…" : drafts.length ? `登记 ${fresh} 个 Server` : "登记 Server"}
      </button>
      <button type="button" className="cfg-quiet" onClick={onClose} disabled={saving}>
        取消
      </button>
    </>
  );

  return (
    <Modal
      title="粘贴导入 MCP Server"
      subtitle="把外部 MCP 客户端的配置 JSON 整份贴进来，解析后逐条登记。"
      wide
      onClose={onClose}
      footer={footer}
    >
      <div className="cfg-mcp-import-head">
        <p className="cfg-hint">
          支持 <code>mcpServers</code> 映射（Claude Desktop / Cursor 等）、<code>mcpServers</code> 数组、
          以及单条参数对象。<code>type</code> 字段会归一成项目的传输方式，缺省时有启动命令按本地进程、
          有地址按 HTTP。
        </p>
        <button
          type="button"
          className="cfg-quiet"
          onClick={() => {
            setText(MCP_IMPORT_PLACEHOLDER);
            setOutcome(null);
          }}
          disabled={saving}
        >
          填入示例
        </button>
      </div>

      <Field label="配置 JSON" htmlFor="mcp-import-json" wide>
        <textarea
          id="mcp-import-json"
          rows={10}
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setOutcome(null);
          }}
          spellCheck={false}
          placeholder={MCP_IMPORT_PLACEHOLDER}
        />
      </Field>

      {parsed.error && (
        <p role="alert" className="cfg-alert">
          {parsed.error}
        </p>
      )}

      {parsed.result && !text.trim() && (
        <p className="cfg-hint">粘贴后立即解析并预览；不会自动提交，确认无误再点右下角按钮。</p>
      )}

      {parsed.result && text.trim() && (
        <div className="cfg-mcp-import-status" role="status">
          <Chip tone={parsed.result.shape === "unknown" ? "slate" : "teal"}>
            {MCP_IMPORT_SHAPE_LABEL[parsed.result.shape]}
          </Chip>
          <span>
            可登记 <b>{fresh}</b> 个
            {duplicates.length > 0 && ` · ID 已存在 ${duplicates.length} 个`}
            {issues.length > 0 && ` · 无法登记的 ${issues.length} 条`}
          </span>
        </div>
      )}

      {drafts.length > 0 && (
        <>
          <h4 className="cfg-mcp-import-section">将登记</h4>
          <div className="cfg-mcp-import-list">
            {drafts.map((draft) => (
              <ImportDraftRow draft={draft} duplicate={known.has(draft.id)} key={draft.id} />
            ))}
          </div>
        </>
      )}

      {issues.length > 0 && (
        <>
          <h4 className="cfg-mcp-import-section">无法登记</h4>
          <ul className="cfg-mcp-import-issues">
            {issues.map((issue) => (
              <li key={`${issue.source}-${issue.reason}`}>
                <code>{issue.source}</code>
                <span>{issue.reason}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      {outcome && (
        <div className="cfg-mcp-import-outcome" role="alert">
          <p>
            已登记 <b>{outcome.created.length}</b> 个，失败 <b>{outcome.failed.length}</b> 个：
          </p>
          <ul className="cfg-mcp-import-issues">
            {outcome.failed.map((issue) => (
              <li key={`${issue.source}-${issue.reason}`}>
                <code>{issue.source}</code>
                <span>{issue.reason}</span>
              </li>
            ))}
          </ul>
          <p className="cfg-hint">失败多为 ID 已存在或字段不符；可在列表里编辑原有条目，或改掉提示的字段后重试。</p>
        </div>
      )}
    </Modal>
  );
}

/** 预览行：与服务卡片同一套层次（行内不画自己的边框）。 */
function ImportDraftRow({ draft, duplicate }: { draft: McpImportDraft; duplicate: boolean }) {
  return (
    <article className={`cfg-mcp-import-item${duplicate ? " dup" : ""}`}>
      <div className="cfg-mcp-import-item-head">
        <code>{draft.id}</code>
        <Chip tone={TRANSPORT_TINT[draft.transport] ?? "slate"}>
          {TRANSPORT_LABELS[draft.transport] ?? draft.transport}
        </Chip>
        {duplicate && <Chip tone="amber">ID 已存在，会失败</Chip>}
      </div>
      <p className="cfg-mcp-import-item-meta" title={draftSummary(draft)}>
        {truncate(draftSummary(draft), 120) || "（无命令或地址）"}
      </p>
      <p className="cfg-mcp-import-item-facts">
        {draft.name !== draft.id && <>名称 {draft.name} · </>}
        环境变量 {Object.keys(draft.env).length} 项 · 请求头 {Object.keys(draft.headers).length} 项 · 参数{" "}
        {draft.args.length} 项
      </p>
    </article>
  );
}
