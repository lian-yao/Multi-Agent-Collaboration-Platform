/**
 * InternalToolsPanel.tsx — 「内部工具」分区（`/api/v1/tools` 目录，§5.3）。
 *
 * ## 为什么升为一等分区
 *
 * 这份目录原先藏在「MCP 工具」页底部的「完整工具目录」区块里，还要手动点
 * 「读取目录」才出现——「平台自带了哪些工具、它们做什么」这个最基本的问题在界面上
 * 没有答案。现在进入分区即自动读取；MCP 分区只负责 Server 的登记、发现与逐工具开关。
 *
 * ## 分组口径
 *
 * 与角色工具授权面板（`AgentTools.tsx`）共用同一套 `buildToolCatalogGroups`：
 * 目录 = 内置工具 + 已发现的 MCP 工具。这里只完整展示**内置**组（配合 Schema 折叠）；
 * MCP 组只出一条指引——工具开关在「MCP 工具」分区维护，两个分区不养同一批可开关的
 * 卡片，否则「在这里关掉的开关」和「在那边关掉的开关」迟早说不清谁生效。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Boxes, Plug, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import type { Tool } from "../types/api";
import { Chip, EmptyState, describeError } from "./shared";
import { buildToolCatalogGroups } from "./AgentTools";

/** 单页上限与最多翻页数：目录超限就出声，不静默截断。 */
const PAGE_SIZE = 100;
const PAGE_LIMIT = 20;

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export function InternalToolsPanel() {
  const [tools, setTools] = useState<Tool[] | null>(null);
  const [mcp, setMcp] = useState<Awaited<ReturnType<typeof api.listMcpCompactTools>> | null>(
    null,
  );
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const first = await api.getTools(1, PAGE_SIZE);
      const all = [...first.items];
      let page = 2;
      while (all.length < first.total && page <= PAGE_LIMIT) {
        const chunk = await api.getTools(page, PAGE_SIZE);
        all.push(...chunk.items);
        page += 1;
      }
      setTools(all);
      // MCP 紧凑目录拿不到时按 null 处理：条目会全部落进「内置」组，只是分不清来源，
      // 不值得为它失败让整个分区变只读。
      setMcp(await api.listMcpCompactTools().catch(() => null));
      setError("");
    } catch (cause) {
      setTools(null);
      setError(describeError(cause, "工具目录读取失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const groups = useMemo(() => buildToolCatalogGroups(tools ?? [], mcp), [tools, mcp]);
  const builtinCount = groups
    .filter((group) => group.source === "builtin")
    .reduce((sum, group) => sum + group.tools.length, 0);
  const mcpCount = (tools?.length ?? 0) - builtinCount;

  return (
    <div className="cfg-stack">
      <section className="cfg-block">
        <div className="cfg-block-head">
          <div>
            <h3>内部工具</h3>
            <p>
              {tools
                ? `编排层当前可调用 ${tools.length} 个工具；完整 Schema 默认折叠`
                : "正在读取工具目录"}
            </p>
          </div>
          <button type="button" className="cfg-quiet" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={13} aria-hidden="true" />
            重新读取
          </button>
        </div>
        <p className="cfg-hint">
          这些是平台自带、无需任何外部服务的工具；在「Agent 团队」页的角色弹窗「工具」分区里
          可以按角色收紧授权（默认不受限）。
        </p>
        {error && (
          <p role="alert" className="cfg-alert">
            {error}{" "}
            <button type="button" className="cfg-quiet" onClick={() => void load()}>
              重试
            </button>
          </p>
        )}
        {loading && !tools && <p className="cfg-hint">加载中…</p>}
        {tools && !builtinCount && (
          <EmptyState
            title="内置工具目录是空的"
            hint="内置工具注册表没有返回任何条目；确认后端已正常启动后点「重新读取」。"
          />
        )}
        {groups.map((group) => (
          <div className="cfg-tool-group" key={group.id}>
            <div className="cfg-tool-group-head">
              <Boxes size={12} aria-hidden="true" />
              <b>{group.label}</b>
              <span className="cfg-count">{group.tools.length} 个</span>
            </div>
            <div className="cfg-tool-list">
              {group.tools.map((entry) => {
                const tool = tools?.find((item) => item.name === entry.name);
                return (
                  <article className="cfg-tool-card" key={entry.name}>
                    <div className="cfg-tool-main">
                      <div className="cfg-tool-text">
                        <b>{entry.name}</b>
                        <small title={entry.description}>
                          {truncate(entry.description, 110)}
                        </small>
                      </div>
                      {tool && <Chip tone="slate">{tool.status}</Chip>}
                    </div>
                    {tool && (
                      <details className="cfg-tool-schema">
                        <summary>输入 Schema</summary>
                        <pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
                      </details>
                    )}
                  </article>
                );
              })}
            </div>
          </div>
        ))}

        {mcpCount > 0 && (
          <div className="cfg-tool-group">
            <div className="cfg-tool-group-head">
              <Plug size={12} aria-hidden="true" />
              <b>MCP 工具</b>
              <span className="cfg-count">{mcpCount} 个</span>
            </div>
            <p className="cfg-hint">
              工具目录里还有由 MCP Server 提供的工具；它们的登记、发现与逐工具开关在
              「MCP 工具」分区管理，这里不重复列出。
            </p>
          </div>
        )}
      </section>
    </div>
  );
}

export default InternalToolsPanel;
