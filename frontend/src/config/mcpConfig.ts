/**
 * MCP 配置的领域层：传输常量、外部 JSON 的归一化、草稿校验。
 *
 * 一、**粘贴导入**：MCP 生态里每个客户端都有一份自己的 JSON 约定（Claude Desktop 的
 * `mcpServers`、VS Code 的 `servers`、`type` 字段的 `streamable-http` 写法……）。
 * 让用户照着本项目的表单手抄一遍既慢又容易抄错，所以这里集中做一次**归一化**，
 * 把「别人的 JSON」翻译成本项目 `doc/api.md` §5.11 的字段。
 *
 * 二、**单一事实来源**：传输标签、传输清单、每种的必填字段口径都在本模块，
 * 配置面板与导入弹层共用，避免两处各写一份、加传输时漏改。
 *
 * 只做纯函数与常量：解析、归一化、校验都在这里，UI 只负责展示与提交。
 * 校验口径与后端 `app/core/mcp_registry.py` 一致（本地先拦一道，错误信息更可读）；
 * 真正的事实来源仍是后端，这里放行的条目后端依然会再校验一次。
 */

import type { McpServerCreate, McpTransport } from "../types/api";

/** 上限与 `app/core/mcp_registry.py` 的常量逐条对应。 */
export const MCP_ID_MAX_LENGTH = 50;
export const MCP_NAME_MAX_LENGTH = 100;
export const MCP_COMMAND_MAX_LENGTH = 500;
export const MCP_URL_MAX_LENGTH = 500;
export const MCP_CWD_MAX_LENGTH = 500;
export const MCP_ARGS_MAX_ITEMS = 64;
export const MCP_PAIRS_MAX_ITEMS = 20;

const ID_PATTERN = /^[A-Za-z0-9._-]+$/;

/** 传输的展示名；`TRANSPORT_GROUPS` 的分组顺序即下拉里的顺序。 */
export const TRANSPORT_LABELS: Record<string, string> = {
  stdio: "本地进程（stdio）",
  http: "HTTP",
  sse: "SSE",
  ws: "WebSocket",
};

/** 徽章配色，与 `shared.tsx::Chip` 的 tone 对应。 */
export const TRANSPORT_TINT: Record<string, string> = {
  stdio: "indigo",
  http: "blue",
  sse: "teal",
  ws: "purple",
};

/** 下拉分组：先远程后本地（多数用户的第一个 Server 是远程托管服务）。 */
export const TRANSPORT_GROUPS: Array<{ label: string; options: string[] }> = [
  { label: "远程", options: ["http", "sse", "ws"] },
  { label: "本地", options: ["stdio"] },
];

export const isStdioTransport = (transport: string): boolean => transport === "stdio";

/* -------------------------------------------------------------------------- */
/* 粘贴导入                                                                    */
/* -------------------------------------------------------------------------- */

/** 一条待登记条目的草稿：字段与「新建 Server」表单一一对应，便于两边共用校验。 */
export type McpImportDraft = {
  id: string;
  name: string;
  transport: McpTransport;
  command: string;
  args: string[];
  env: Record<string, string>;
  cwd: string;
  url: string;
  headers: Record<string, string>;
};

/** 被跳过的条目及原因；`source` 是来源标识（Server ID 或「第 N 项」）。 */
export type McpImportIssue = { source: string; reason: string };

/** 识别到的输入形态，用于给用户一句「我读出了什么」。 */
export type McpImportShape =
  | "mcpServers-map"
  | "mcpServers-list"
  | "server-map"
  | "single-parameter"
  | "unknown";

export type McpImportParseResult = {
  drafts: McpImportDraft[];
  issues: McpImportIssue[];
  shape: McpImportShape;
};

export const MCP_IMPORT_SHAPE_LABEL: Record<McpImportShape, string> = {
  "mcpServers-map": "Claude Desktop 风格（mcpServers 映射）",
  "mcpServers-list": "mcpServers 数组",
  "server-map": "裸映射（顶层键即 Server ID）",
  "single-parameter": "单条 Server 参数",
  unknown: "尚未识别",
};

/** 粘贴框里的示例，与解析器支持的形态保持一致。 */
export const MCP_IMPORT_PLACEHOLDER = `{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
    },
    "notion": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}`;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** 把 `type` / `transport` 的各种写法归一成本项目的四种取值；识别不了返回 null。 */
function normalizeTransportToken(raw: unknown): McpTransport | null {
  if (typeof raw !== "string") return null;
  const token = raw.trim().toLowerCase().replace(/[_\s]/g, "-");
  if (!token) return null;
  if (token === "stdio" || token === "local" || token === "inprocess") return "stdio";
  if (token === "sse") return "sse";
  if (token === "ws" || token === "websocket") return "ws";
  // `streamable-http` / `streamablehttp` / `http` / `remote` 都归到 http。
  if (token.startsWith("streamable") || token === "http" || token === "remote") return "http";
  return null;
}

/** 传输缺省时的推断：有启动命令按本地进程，否则按远程 HTTP。 */
function inferTransport(raw: Record<string, unknown>): McpTransport {
  const declared = normalizeTransportToken(raw.transport) ?? normalizeTransportToken(raw.type);
  if (declared) return declared;
  if (typeof raw.command === "string" && raw.command.trim()) return "stdio";
  return "http";
}

/** 键值对取值：丢弃空键与对象值，标量统一转字符串。 */
function normalizeStringMap(value: unknown, limit: number): Record<string, string> {
  if (!isRecord(value)) return {};
  const resolved: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) {
    const name = key.trim();
    if (!name) continue;
    if (item === null || item === undefined) continue;
    if (typeof item === "object") continue;
    resolved[name] = String(item);
    if (Object.keys(resolved).length >= limit) break;
  }
  return resolved;
}

/** 参数列表：数组按原样（去空白项），字符串按行拆——对齐「每行一个参数」的表单约定。 */
function normalizeArgs(value: unknown): string[] {
  const raw = Array.isArray(value)
    ? value.map((item) =>
        typeof item === "string" ? item.trim() : item === null || item === undefined ? "" : String(item).trim(),
      )
    : typeof value === "string"
      ? value.split("\n").map((line) => line.trim())
      : [];
  return raw.filter(Boolean).slice(0, MCP_ARGS_MAX_ITEMS);
}

function normalizeText(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

/**
 * 校验草稿；返回空串表示通过。
 *
 * 「新建 Server」表单与「粘贴导入」共用这一份口径，改动时两边同时生效。
 */
export function draftProblem(draft: McpImportDraft): string {
  if (!draft.id) return "ID 不能为空。";
  if (draft.id.length > MCP_ID_MAX_LENGTH) return `ID 不能超过 ${MCP_ID_MAX_LENGTH} 个字符。`;
  if (!ID_PATTERN.test(draft.id)) return "ID 只允许字母、数字与 . _ - 。";
  if (!draft.name) return "名称不能为空。";
  if (draft.name.length > MCP_NAME_MAX_LENGTH) return `名称不能超过 ${MCP_NAME_MAX_LENGTH} 个字符。`;
  if (isStdioTransport(draft.transport)) {
    if (!draft.command) return "本地进程传输必须填写启动命令。";
    if (draft.command.length > MCP_COMMAND_MAX_LENGTH)
      return `启动命令不能超过 ${MCP_COMMAND_MAX_LENGTH} 个字符。`;
    if (draft.args.length > MCP_ARGS_MAX_ITEMS) return `参数最多 ${MCP_ARGS_MAX_ITEMS} 项。`;
    if (draft.url) return "本地进程传输不能同时提供地址。";
  } else {
    if (!draft.url) return "远程传输必须填写地址。";
    if (draft.url.length > MCP_URL_MAX_LENGTH) return `地址不能超过 ${MCP_URL_MAX_LENGTH} 个字符。`;
    if (draft.transport === "ws") {
      if (!/^wss?:\/\//.test(draft.url)) return "WebSocket 地址需以 ws:// 或 wss:// 开头。";
    } else if (!/^https?:\/\//.test(draft.url)) {
      return "地址需以 http:// 或 https:// 开头。";
    }
    if (draft.command || draft.args.length || draft.cwd || Object.keys(draft.env).length) {
      return "远程传输不能同时提供命令、参数、工作目录或环境变量。";
    }
  }
  if (Object.keys(draft.env).length > MCP_PAIRS_MAX_ITEMS)
    return `环境变量最多 ${MCP_PAIRS_MAX_ITEMS} 项。`;
  if (Object.keys(draft.headers).length > MCP_PAIRS_MAX_ITEMS)
    return `请求头最多 ${MCP_PAIRS_MAX_ITEMS} 项。`;
  return "";
}

/** 把一份「参数对象」归一化成草稿；`fallbackId` 用于没有 id 的单条形态。 */
function toDraft(raw: Record<string, unknown>, fallbackId = ""): McpImportDraft {
  // `parameters` 是部分客户端的嵌套写法，展开一层再取字段（外层显式字段优先）。
  const nested = isRecord(raw.parameters) ? raw.parameters : {};
  const source: Record<string, unknown> = { ...nested, ...raw };
  // id 取值优先级：显式 id → 来源键名（`mcpServers` 里键名就是 ID 约定）→ 展示名。
  const id = normalizeText(raw.id) || fallbackId || normalizeText(raw.name);
  const name = normalizeText(raw.name) || normalizeText(nested.name) || id;
  return {
    id,
    name: name.length > MCP_NAME_MAX_LENGTH ? name.slice(0, MCP_NAME_MAX_LENGTH) : name,
    transport: inferTransport(source),
    command: normalizeText(source.command),
    args: normalizeArgs(source.args),
    env: normalizeStringMap(source.env, MCP_PAIRS_MAX_ITEMS),
    cwd: normalizeText(source.cwd),
    url: normalizeText(source.url),
    headers: normalizeStringMap(source.headers, MCP_PAIRS_MAX_ITEMS),
  };
}

function looksLikeParameters(value: Record<string, unknown>): boolean {
  return ["transport", "type", "command", "url"].some((key) => key in value);
}

/**
 * 解析粘贴的 JSON 文本。
 *
 * 支持四种形态（顺序即判定优先级）：
 *
 * 1. `{"mcpServers": {id: params}}` —— Claude Desktop / Cursor 等
 * 2. `{"mcpServers": [{id, ...}]}` / `{"servers": [...]}` —— 数组
 * 3. `{"id": "...", "parameters": {...}}` 或直接一份参数对象 —— 单条
 * 4. `{id: params}` —— 裸映射（每个值须含 transport/type/command/url 之一才认）
 *
 * `JSON.parse` 失败会抛出 `SyntaxError`，由调用方转成可读提示。
 */
export function parseMcpImport(text: string): McpImportParseResult {
  const trimmed = text.trim();
  if (!trimmed) return { drafts: [], issues: [], shape: "unknown" };

  const parsed: unknown = JSON.parse(trimmed);
  if (!isRecord(parsed)) {
    return {
      drafts: [],
      issues: [{ source: "根节点", reason: "JSON 根节点必须是对象。" }],
      shape: "unknown",
    };
  }

  const collected: Array<{ source: string; raw: Record<string, unknown>; fallbackId?: string }> = [];
  let shape: McpImportShape = "unknown";

  const container = isRecord(parsed.mcpServers)
    ? parsed.mcpServers
    : Array.isArray(parsed.mcpServers)
      ? parsed.mcpServers
      : Array.isArray(parsed.servers)
        ? parsed.servers
        : null;

  if (isRecord(container)) {
    shape = "mcpServers-map";
    for (const [key, value] of Object.entries(container)) {
      // 值不是对象时留一份空参数：让它走到校验分支报出可读原因，而不是被静默丢弃。
      collected.push({ source: key, raw: isRecord(value) ? value : {}, fallbackId: key });
    }
  } else if (Array.isArray(container)) {
    shape = "mcpServers-list";
    container.forEach((value, index) => {
      const source = `第 ${index + 1} 项`;
      // 数组形态没有键名兜底，ID 必须由条目自己给出。
      collected.push({ source, raw: isRecord(value) ? value : {} });
    });
  } else if (looksLikeParameters(parsed) || isRecord(parsed.parameters)) {
    shape = "single-parameter";
    collected.push({ source: normalizeText(parsed.id) || "单条配置", raw: parsed });
  } else {
    const entries = Object.entries(parsed);
    if (entries.length && entries.every(([, value]) => isRecord(value))) {
      shape = "server-map";
      for (const [key, value] of entries) {
        const item = value as Record<string, unknown>;
        if (!looksLikeParameters(item) && !isRecord(item.parameters)) continue;
        collected.push({ source: key, raw: item, fallbackId: key });
      }
    }
  }

  const drafts: McpImportDraft[] = [];
  const issues: McpImportIssue[] = [];
  const seen = new Set<string>();

  if (!collected.length) {
    issues.push({
      source: "根节点",
      reason: "没找到可识别的 Server 配置；支持 mcpServers 映射/数组，或直接粘贴一份参数对象。",
    });
    return { drafts, issues, shape };
  }

  for (const item of collected) {
    const draft = toDraft(item.raw, item.fallbackId ?? "");
    const label = draft.id || item.source;
    if (draft.id && seen.has(draft.id)) {
      issues.push({ source: label, reason: "同一份配置里 ID 重复，只登记第一次出现。" });
      continue;
    }
    const problem = draftProblem(draft);
    if (problem) {
      issues.push({ source: label, reason: problem });
      continue;
    }
    seen.add(draft.id);
    drafts.push(draft);
  }

  return { drafts, issues, shape };
}

/** 草稿 → `McpServerCreate` 请求体（只带该传输用得上的字段）。 */
export function draftToPayload(draft: McpImportDraft, enabled = true): McpServerCreate {
  const base = { id: draft.id, name: draft.name, transport: draft.transport, enabled };
  if (isStdioTransport(draft.transport)) {
    return {
      ...base,
      command: draft.command,
      args: draft.args,
      env: draft.env,
      cwd: draft.cwd || null,
    };
  }
  return { ...base, url: draft.url, headers: draft.headers };
}

/** 卡片上的一行摘要，避免把长命令铺满预览区。 */
export function draftSummary(draft: McpImportDraft): string {
  if (isStdioTransport(draft.transport)) {
    return [draft.command, ...draft.args].filter(Boolean).join(" ");
  }
  return draft.url;
}
