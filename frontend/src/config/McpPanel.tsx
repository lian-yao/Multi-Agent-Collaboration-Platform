import { useCallback, useEffect, useMemo, useState, type ChangeEvent } from "react";
import { api } from "../api/client";
import {
  MCP_TRANSPORTS,
  type McpCompactTool,
  type McpCompactToolList,
  type McpServer,
  type McpServerCreate,
  type McpServerUpdate,
  type McpTransport,
  type Tool,
} from "../types/api";
import {
  Chip,
  EmptyState,
  Field,
  Modal,
  NoticeBar,
  Switch,
  describeError,
  formatPairs,
  formatTime,
  parseLines,
  parsePairs,
  truncate,
  type NoticeState,
} from "./shared";

const TRANSPORT_LABELS: Record<string, string> = {
  stdio: "本地进程（stdio）",
  http: "HTTP",
  sse: "SSE",
  ws: "WebSocket",
};

const TRANSPORT_TINT: Record<string, string> = {
  stdio: "indigo",
  http: "blue",
  sse: "teal",
  ws: "purple",
};

const isStdio = (transport: string) => transport === "stdio";

/* -------------------------------------------------------------------------- */
/* 工具卡片（L3 行：无独立边框，靠分隔线与 hover 底色）                          */
/* -------------------------------------------------------------------------- */

function ToolSchema({
  tool,
  catalog,
  onRequest,
  error,
}: {
  tool: McpCompactTool;
  catalog: Record<string, Tool> | null;
  onRequest: () => void;
  error: string;
}) {
  const schema = catalog?.[tool.name]?.input_schema;
  return (
    <details
      className="cfg-tool-schema"
      onToggle={(event) => {
        if ((event.target as HTMLDetailsElement).open) onRequest();
      }}
    >
      <summary>输入 Schema</summary>
      {error && (
        <p role="alert" className="cfg-alert">
          {error}
        </p>
      )}
      {!error && !catalog && <p className="cfg-hint">读取中…</p>}
      {!error && catalog && schema && <pre>{JSON.stringify(schema, null, 2)}</pre>}
      {!error && catalog && !schema && (
        <p className="cfg-hint">
          内置工具目录里没有该条目的 Schema；它由 MCP Server 在连接后动态提供，需先「发现工具」。
        </p>
      )}
    </details>
  );
}

function ToolCard({
  tool,
  busy,
  onToggle,
  catalog,
  onRequestSchema,
  schemaError,
}: {
  tool: McpCompactTool;
  busy: boolean;
  onToggle: () => void;
  catalog: Record<string, Tool> | null;
  onRequestSchema: () => void;
  schemaError: string;
}) {
  return (
    <article className={`cfg-tool-card${tool.tool_enabled ? "" : " off"}`}>
      <div className="cfg-tool-main">
        <Switch
          checked={tool.tool_enabled}
          disabled={busy}
          onChange={onToggle}
          label={`${tool.tool_enabled ? "停用" : "启用"} ${tool.name}`}
        />
        <div className="cfg-tool-text">
          <b>{tool.name}</b>
          <small title={tool.description}>
            {tool.description ? truncate(tool.description, 110) : "（无描述）"}
          </small>
        </div>
        <div className="cfg-tool-badges">
          {!tool.available && (
            <Chip tone="amber" title="最近一次发现结果里没有这个工具">
              发现结果中不存在
            </Chip>
          )}
          {!tool.enabled && <Chip tone="slate">所属 Server 已停用</Chip>}
        </div>
      </div>
      <ToolSchema tool={tool} catalog={catalog} onRequest={onRequestSchema} error={schemaError} />
    </article>
  );
}

/* -------------------------------------------------------------------------- */
/* Server 表单弹层                                                              */
/* -------------------------------------------------------------------------- */

type ServerForm = {
  id: string;
  name: string;
  transport: string;
  command: string;
  args: string;
  env: string;
  cwd: string;
  url: string;
  headers: string;
  enabled: boolean;
};

function formFromServer(server: McpServer | null): ServerForm {
  return {
    id: server?.id ?? "",
    name: server?.name ?? "",
    transport: server?.transport ?? "stdio",
    command: server?.command ?? "",
    args: (server?.args ?? []).join("\n"),
    env: formatPairs(server?.env),
    cwd: server?.cwd ?? "",
    url: server?.url ?? "",
    headers: formatPairs(server?.headers),
    enabled: server?.enabled ?? true,
  };
}

/** 与后端 `McpServerCreateRequest` / `UpdateRequest` 的校验口径一致，先本地拦一道。 */
function serverProblem(form: ServerForm, editing: boolean): string {
  if (!editing) {
    if (!form.id.trim()) return "ID 不能为空。";
    if (form.id.length > 50) return "ID 不能超过 50 个字符。";
    if (!/^[A-Za-z0-9._-]+$/.test(form.id)) return "ID 只允许字母、数字与 . _ - 。";
  }
  if (!form.name.trim()) return "名称不能为空。";
  if (form.name.length > 100) return "名称不能超过 100 个字符。";
  if (isStdio(form.transport)) {
    if (!form.command.trim()) return "stdio 传输必须填写启动命令。";
    if (form.command.length > 500) return "启动命令不能超过 500 个字符。";
    if (parseLines(form.args).length > 64) return "参数最多 64 项。";
    if (form.env.trim() && parsePairs(form.env) === null) return "环境变量每行需为 KEY=VALUE。";
  } else {
    if (!form.url.trim()) return "远程传输必须填写地址。";
    if (!/^(https?|wss?):\/\//.test(form.url.trim())) return "地址需以 http(s):// 或 ws(s):// 开头。";
    if (form.url.length > 500) return "地址不能超过 500 个字符。";
    if (form.headers.trim() && parsePairs(form.headers) === null) return "请求头每行需为 KEY=VALUE。";
  }
  return "";
}

function ServerFormModal({
  editing,
  onClose,
  onSaved,
}: {
  editing: McpServer | null;
  onClose: () => void;
  onSaved: (message: string) => Promise<void>;
}) {
  const [form, setForm] = useState<ServerForm>(() => formFromServer(editing));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);
  const problem = serverProblem(form, Boolean(editing));
  const stdio = isStdio(form.transport);
  /** `enabled` 是布尔开关，不走这里。 */
  const edit = (key: Exclude<keyof ServerForm, "enabled">) =>
    (event: ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
      setForm((current) => ({ ...current, [key]: event.target.value }) as ServerForm);

  const submit = async () => {
    if (problem) {
      setNotice({ tone: "bad", text: problem });
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        // 只提交真正改动的字段：省略 = 不改动，显式 null = 清除（doc/api.md §5.11）。
        const patch: McpServerUpdate = {};
        if (form.name.trim() !== editing.name) patch.name = form.name.trim();
        if (form.transport !== editing.transport) patch.transport = form.transport as McpTransport;
        const nextCommand = stdio ? form.command.trim() || null : null;
        if (nextCommand !== editing.command) patch.command = nextCommand;
        const nextArgs = stdio ? parseLines(form.args) : [];
        if (nextArgs.join("\n") !== editing.args.join("\n")) patch.args = nextArgs;
        const nextEnv = stdio ? parsePairs(form.env) ?? {} : {};
        if (formatPairs(nextEnv) !== formatPairs(editing.env)) patch.env = nextEnv;
        const nextCwd = stdio ? form.cwd.trim() || null : null;
        if (nextCwd !== editing.cwd) patch.cwd = nextCwd;
        const nextUrl = stdio ? null : form.url.trim() || null;
        if (nextUrl !== editing.url) patch.url = nextUrl;
        const nextHeaders = stdio ? {} : parsePairs(form.headers) ?? {};
        if (formatPairs(nextHeaders) !== formatPairs(editing.headers)) patch.headers = nextHeaders;
        if (form.enabled !== editing.enabled) patch.enabled = form.enabled;
        if (!Object.keys(patch).length) {
          setNotice({ tone: "bad", text: "没有需要保存的改动。" });
          setSaving(false);
          return;
        }
        await api.patchMcpServer(editing.id, patch);
        await onSaved(`已更新 MCP Server ${editing.id}。`);
      } else {
        const payload: McpServerCreate = {
          id: form.id.trim(),
          name: form.name.trim(),
          transport: form.transport as McpTransport,
          enabled: form.enabled,
        };
        if (stdio) {
          payload.command = form.command.trim();
          payload.args = parseLines(form.args);
          payload.env = parsePairs(form.env) ?? {};
          payload.cwd = form.cwd.trim() || null;
        } else {
          payload.url = form.url.trim();
          payload.headers = parsePairs(form.headers) ?? {};
        }
        const created = await api.createMcpServer(payload);
        await onSaved(`已登记 MCP Server ${created.id}。`);
      }
      onClose();
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "保存失败，请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={editing ? `编辑 MCP Server · ${editing.id}` : "新建 MCP Server"}
      subtitle="传输方式决定必填字段：本地进程用命令与参数，远程用 URL 与请求头。"
      wide
      onClose={onClose}
      footer={
        <>
          <button type="button" className="cfg-primary" onClick={() => void submit()} disabled={saving || Boolean(problem)}>
            {saving ? "保存中…" : editing ? "保存修改" : "登记 Server"}
          </button>
          <button type="button" className="cfg-quiet" onClick={onClose} disabled={saving}>
            取消
          </button>
          <NoticeBar notice={notice} />
        </>
      }
    >
      <div className="cfg-form-grid">
        <Field
          label="ID"
          htmlFor="sf-id"
          hint={editing ? "创建后不可修改。" : "1–50 字符，仅 A-Za-z0-9._-"}
          tone={!editing && serverProblem(form, false) ? "bad" : undefined}
        >
          <input
            id="sf-id"
            value={form.id}
            onChange={edit("id")}
            disabled={Boolean(editing)}
            placeholder="filesystem"
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="名称" htmlFor="sf-name" hint="1–100 字符。">
          <input id="sf-name" value={form.name} onChange={edit("name")} autoComplete="off" />
        </Field>
        <Field label="传输方式" htmlFor="sf-transport" hint="切换后另一组字段会被清空。">
          <select id="sf-transport" value={form.transport} onChange={edit("transport")}>
            {MCP_TRANSPORTS.map((transport) => (
              <option value={transport} key={transport}>
                {TRANSPORT_LABELS[transport] ?? transport}
              </option>
            ))}
          </select>
        </Field>
        <Field label="启用" htmlFor="sf-enabled" hint="停用后其工具不参与 Agent 调用。">
          <div className="cfg-inline-switch">
            <Switch
              checked={form.enabled}
              onChange={(next) => setForm((current) => ({ ...current, enabled: next }))}
              label="启用该 Server"
            />
            <span>{form.enabled ? "已启用" : "已停用"}</span>
          </div>
        </Field>
      </div>

      {stdio ? (
        <div className="cfg-form-grid">
          <Field label="启动命令" htmlFor="sf-command" hint="例如 npx、uvx 或可执行文件绝对路径。">
            <input
              id="sf-command"
              value={form.command}
              onChange={edit("command")}
              placeholder="npx"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field label="工作目录" htmlFor="sf-cwd" hint="可选，留空用服务进程的当前目录。">
            <input id="sf-cwd" value={form.cwd} onChange={edit("cwd")} autoComplete="off" spellCheck={false} />
          </Field>
          <Field label="参数" htmlFor="sf-args" hint="每行一个参数，最多 64 项。" wide>
            <textarea
              id="sf-args"
              rows={3}
              value={form.args}
              onChange={edit("args")}
              spellCheck={false}
              placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/data"}
            />
          </Field>
          <Field label="环境变量" htmlFor="sf-env" hint="每行 KEY=VALUE；# 开头为注释。" wide>
            <textarea
              id="sf-env"
              rows={3}
              value={form.env}
              onChange={edit("env")}
              spellCheck={false}
              placeholder="API_TOKEN=..."
            />
          </Field>
        </div>
      ) : (
        <div className="cfg-form-grid">
          <Field label="地址" htmlFor="sf-url" hint="http(s):// 或 ws(s)://，不超过 500 字符。" wide>
            <input
              id="sf-url"
              value={form.url}
              onChange={edit("url")}
              placeholder="https://mcp.example.com/sse"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field label="请求头" htmlFor="sf-headers" hint="每行 KEY=VALUE；凭据只写入、不回读。" wide>
            <textarea
              id="sf-headers"
              rows={3}
              value={form.headers}
              onChange={edit("headers")}
              spellCheck={false}
              placeholder="Authorization=Bearer ..."
            />
          </Field>
        </div>
      )}
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* 面板主体                                                                    */
/* -------------------------------------------------------------------------- */

export function McpPanel() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [tools, setTools] = useState<McpCompactToolList | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState<NoticeState>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState<McpServer | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [catalog, setCatalog] = useState<Record<string, Tool> | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [serverList, toolList] = await Promise.all([api.listMcpServers(), api.listMcpCompactTools()]);
      setServers(serverList.items);
      setTools(toolList);
      setLoadError("");
    } catch (cause) {
      setLoadError(describeError(cause, "MCP 注册表读取失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /** 完整 Schema 体积大，只有展开折叠区或点「读取目录」时才拉取。 */
  const loadCatalog = useCallback(async () => {
    if (catalog || catalogError) return;
    try {
      const page = await api.getTools(1, 100);
      setCatalog(Object.fromEntries(page.items.map((item) => [item.name, item])));
    } catch (cause) {
      setCatalogError(describeError(cause, "工具目录读取失败。"));
    }
  }, [catalog, catalogError]);

  const toolsByServer = useMemo(() => {
    const grouped: Record<string, McpCompactTool[]> = {};
    for (const tool of tools?.items ?? []) {
      (grouped[tool.server_id] ??= []).push(tool);
    }
    return grouped;
  }, [tools]);

  const serverMeta = (id: string) => tools?.servers.find((item) => item.id === id);

  const toggleServer = async (server: McpServer) => {
    setBusy(server.id);
    try {
      await api.patchMcpServer(server.id, { enabled: !server.enabled });
      await refresh();
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "切换启用状态失败。") });
    } finally {
      setBusy(null);
    }
  };

  const toggleTool = async (server: McpServer, tool: McpCompactTool) => {
    setBusy(tool.name);
    try {
      const options = { ...server.tool_options };
      options[tool.name] = { ...options[tool.name], disabled: tool.tool_enabled };
      await api.patchMcpServer(server.id, { tool_options: options });
      await refresh();
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "切换工具开关失败。") });
    } finally {
      setBusy(null);
    }
  };

  const discover = async (server: McpServer) => {
    setBusy(server.id);
    setNotice(null);
    try {
      const result = await api.discoverMcpServer(server.id);
      await refresh();
      setNotice({
        tone: "ok",
        text: `${server.name}：发现 ${result.total} 个工具（${formatTime(result.discovered_at)}）。发现结果只写缓存，不改启用状态。`,
      });
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "发现失败，请检查命令、地址与网络。") });
    } finally {
      setBusy(null);
    }
  };

  const remove = async (server: McpServer) => {
    if (!window.confirm(`删除 MCP Server「${server.name}」？其工具入口会一并移除。`)) return;
    setBusy(server.id);
    try {
      await api.deleteMcpServer(server.id);
      await refresh();
      setNotice({ tone: "ok", text: `已删除 MCP Server ${server.id}。` });
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "删除失败。") });
    } finally {
      setBusy(null);
    }
  };

  const afterSave = async (message: string) => {
    await refresh();
    setNotice({ tone: "ok", text: message });
  };

  return (
    <div className="cfg-stack">
      <section className="cfg-block">
        <div className="cfg-block-head">
          <div>
            <h3>MCP Server</h3>
            <p>
              {servers.length} 个条目 · 共 {tools?.total ?? 0} 个工具
              {tools && tools.servers.length > 0 &&
                ` · 最近发现 ${tools.servers.filter((item) => item.discovered_at).length} 个`}
            </p>
          </div>
          <button type="button" className="cfg-primary" onClick={() => setCreateOpen(true)}>
            新建 Server
          </button>
        </div>

        <p className="cfg-hint">
          「发现工具」会按条目配置建立连接并缓存工具清单，由你显式触发，不会随页面加载自动执行。
        </p>
        {loadError && (
          <p role="alert" className="cfg-alert">
            {loadError}
          </p>
        )}
        {loading && !servers.length && <p className="cfg-hint">加载中…</p>}
        {!loading && !servers.length && !loadError && (
          <EmptyState
            title="还没有 MCP Server"
            hint="登记一个 Server 并执行「发现工具」后，工具卡片才会出现。"
          />
        )}

        <div className="cfg-server-list">
          {servers.map((server) => {
            const meta = serverMeta(server.id);
            const serverTools = toolsByServer[server.id] ?? [];
            const isCollapsed = collapsed[server.id] ?? false;
            return (
              <article className={`cfg-server-card${server.enabled ? "" : " off"}`} key={server.id}>
                <div className="cfg-block-head">
                  <div>
                    <h4>
                      {server.name}
                      <Chip tone={TRANSPORT_TINT[server.transport] ?? "slate"}>
                        {TRANSPORT_LABELS[server.transport] ?? server.transport}
                      </Chip>
                    </h4>
                    <p>
                      <code>{server.id}</code> · 工具 {meta?.tool_count ?? server.tool_count} 个 · 最近发现{" "}
                      {formatTime(meta?.discovered_at ?? server.discovered_at)}
                    </p>
                  </div>
                  <div className="cfg-row-actions">
                    <Switch
                      checked={server.enabled}
                      disabled={busy === server.id}
                      onChange={() => void toggleServer(server)}
                      label={`${server.enabled ? "停用" : "启用"} ${server.name}`}
                    />
                    <button
                      type="button"
                      className="cfg-quiet"
                      onClick={() => void discover(server)}
                      disabled={busy === server.id}
                    >
                      {busy === server.id ? "处理中…" : "发现工具"}
                    </button>
                    <button
                      type="button"
                      className="cfg-quiet"
                      onClick={() => setEditing(server)}
                      disabled={busy === server.id}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className="cfg-quiet danger"
                      onClick={() => void remove(server)}
                      disabled={busy === server.id}
                    >
                      删除
                    </button>
                  </div>
                </div>

                <dl className="cfg-facts">
                  {isStdio(server.transport) ? (
                    <div>
                      <dt>启动命令</dt>
                      <dd>
                        <code>{[server.command, ...server.args].filter(Boolean).join(" ")}</code>
                      </dd>
                    </div>
                  ) : (
                    <div>
                      <dt>地址</dt>
                      <dd>
                        <code>{server.url}</code>
                      </dd>
                    </div>
                  )}
                  {server.server_info && (
                    <div>
                      <dt>服务端</dt>
                      <dd>
                        {String(server.server_info.name ?? "未知")}
                        {server.server_info.version ? ` · v${String(server.server_info.version)}` : ""}
                      </dd>
                    </div>
                  )}
                </dl>

                {serverTools.length > 0 && (
                  <>
                    <button
                      type="button"
                      className="cfg-quiet cfg-toggle-tools"
                      onClick={() =>
                        setCollapsed((current) => ({ ...current, [server.id]: !isCollapsed }))
                      }
                      aria-expanded={!isCollapsed}
                    >
                      {isCollapsed ? `展开 ${serverTools.length} 个工具` : `收起 ${serverTools.length} 个工具`}
                    </button>
                    {!isCollapsed && (
                      <div className="cfg-tool-list">
                        {serverTools.map((tool) => (
                          <ToolCard
                            tool={tool}
                            busy={busy === tool.name}
                            onToggle={() => void toggleTool(server, tool)}
                            catalog={catalog}
                            onRequestSchema={() => void loadCatalog()}
                            schemaError={catalogError}
                            key={tool.name}
                          />
                        ))}
                      </div>
                    )}
                  </>
                )}
                {!serverTools.length && (
                  <p className="cfg-hint">还没有缓存的工具清单，点「发现工具」建立连接后即可看到卡片。</p>
                )}
              </article>
            );
          })}
        </div>
      </section>

      <section className="cfg-block">
        <div className="cfg-block-head">
          <div>
            <h3>完整工具目录</h3>
            <p>编排层当前可调用的内置工具；完整 Schema 默认折叠。</p>
          </div>
          <button type="button" className="cfg-quiet" onClick={() => void loadCatalog()}>
            读取目录
          </button>
        </div>
        {catalogError && (
          <p role="alert" className="cfg-alert">
            {catalogError}
          </p>
        )}
        {!catalog && !catalogError && (
          <p className="cfg-hint">尚未读取。点「读取目录」或展开任一工具卡片的 Schema。</p>
        )}
        {catalog && (
          <div className="cfg-tool-list">
            {Object.values(catalog).map((tool) => (
              <article className="cfg-tool-card" key={tool.name}>
                <div className="cfg-tool-main">
                  <div className="cfg-tool-text">
                    <b>{tool.name}</b>
                    <small title={tool.description}>{truncate(tool.description, 110)}</small>
                  </div>
                  <Chip tone="slate">{tool.status}</Chip>
                </div>
                <details className="cfg-tool-schema">
                  <summary>输入 Schema</summary>
                  <pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
                </details>
              </article>
            ))}
          </div>
        )}
      </section>

      <NoticeBar notice={notice} />

      {createOpen && (
        <ServerFormModal editing={null} onClose={() => setCreateOpen(false)} onSaved={afterSave} />
      )}
      {editing && (
        <ServerFormModal editing={editing} onClose={() => setEditing(null)} onSaved={afterSave} />
      )}
    </div>
  );
}
