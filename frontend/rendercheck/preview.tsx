/**
 * 静态 UI 预览（**仅供人工评审版式，不参与生产构建**）。
 *
 * 把真实的 `App` 挂到一个自包含的 HTML 里：后端请求全部由内联的 mock fetch 应答，
 * 所以不需要起服务、不需要数据库。用途是让「工具与配置 / Agent 团队 / 任务记录」
 * 三页的左对齐版式与副路由能被肉眼确认一次。
 *
 * 数据来自 `rescue/stub_api.py`（生成 `window.__SEED__`），路由表是
 * `"<METHOD> <path>" → payload` 的扁平映射，动态路径在 Python 侧就已展开成固定 id，
 * 因此这里的 mock 只需要做一次查表。
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "../src/App";
import { api } from "../src/api/client";
import { classifyLocal } from "../src/workspace/attachments";
import type { Attachment, Message } from "../src/types/api";
// 真实入口 `src/main.tsx` 才有这一行；这里必须显式引入，否则预览会缺全局令牌与外壳版式。
import "../src/styles.css";

type Seed = Record<string, unknown>;

const seed: Seed = (globalThis as { __SEED__?: Seed }).__SEED__ ?? {};

const jsonResponse = (payload: unknown, status = 200): Response =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/* -------------------------------------------------------------------------- */
/* 附件的预览替身（ADR-021）                                                    */
/* -------------------------------------------------------------------------- */

/** 已登记的附件（种子里预置的 + 预览里现传的），按 id 索引。 */
const registry = new Map<string, Attachment>();

/** 走一遍种子，把「长得像附件」的对象收进 registry（含消息里内嵌的那些）。 */
const registerSeed = (value: unknown): void => {
  if (Array.isArray(value)) {
    value.forEach(registerSeed);
    return;
  }
  if (!value || typeof value !== "object") return;
  const record = value as Record<string, unknown>;
  if (
    typeof record.id === "string" &&
    typeof record.kind === "string" &&
    typeof record.size_bytes === "number"
  ) {
    registry.set(record.id, record as unknown as Attachment);
    return;
  }
  Object.values(record).forEach(registerSeed);
};
Object.values(seed).forEach(registerSeed);

/**
 * 图片缩略图的替身。
 *
 * `<img src>` 不走 `fetch`，所以 mock 拦不住它；预览页又是 `file://` 打开的单文件，
 * 没有后端可应答。若不替换，附件正文地址必然裂图，「缩略图长什么样」就评审不出来。
 * 这里用内联 SVG 顶上，只为看版式。
 */
const PLACEHOLDER_IMAGE =
  "data:image/svg+xml;charset=utf-8," +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">' +
      '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">' +
      '<stop offset="0" stop-color="#a8ccd9"/><stop offset="1" stop-color="#4f7f92"/>' +
      "</linearGradient></defs>" +
      '<rect width="320" height="200" fill="url(#g)"/>' +
      '<circle cx="238" cy="58" r="32" fill="#fff" opacity=".38"/>' +
      '<path d="M0 158 90 96l70 44 70-56 90 62v54H0z" fill="#2f5566" opacity=".45"/>' +
      "</svg>",
  );

const realContentUrl = api.attachmentContentUrl.bind(api);
api.attachmentContentUrl = (id: string) =>
  registry.get(id)?.kind === "image" ? PLACEHOLDER_IMAGE : realContentUrl(id);

let attachmentSeq = 0;

// jsdom（离线自检用）不提供 fetch，所以这里不能假设它存在。
const realFetch: typeof fetch | null =
  typeof globalThis.fetch === "function" ? globalThis.fetch.bind(globalThis) : null;

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(raw, globalThis.location?.href ?? "http://localhost/");
  const method = (init?.method ?? "GET").toUpperCase();
  const key = `${method} ${url.pathname}`;
  const payload = (): Record<string, unknown> =>
    init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};

  // 会话删除：不真的落库，但把它从种子列表里摘掉，这样「删掉的那一行真的消失」
  // 可以在预览里肉眼确认（评审的是行内二次确认的交互，不是持久化）。
  if (method === "DELETE" && url.pathname.startsWith("/api/v1/sessions/")) {
    const id = decodeURIComponent(url.pathname.slice("/api/v1/sessions/".length));
    const list = seed["GET /api/v1/sessions"] as { items?: { id: string }[] } | undefined;
    if (list?.items) list.items = list.items.filter((item) => item.id !== id);
    return jsonResponse(null);
  }

  // 附件登记：就地在内存里造一条，让「选文件 → chip（上传中 → 已就绪）」这段能真走通。
  if (method === "POST" && url.pathname === "/api/v1/attachments") {
    const body = payload();
    const name = typeof body.name === "string" && body.name ? body.name : "未命名";
    const kind = classifyLocal(name);
    const base64 = typeof body.data_base64 === "string" ? body.data_base64 : "";
    const id = `att-preview-${++attachmentSeq}`;
    // 文件名里带「扫描」的 PDF 强制判成解析失败：这样不用真的准备一份扫描件，
    // 也能在预览里看到失败态的 chip 与气泡（失败与成功必须都能评审）。
    const scanned = /扫描|scan/i.test(name) && name.toLowerCase().endsWith(".pdf");
    const created: Attachment = {
      id,
      session_id: null,
      message_id: null,
      name,
      mime: typeof body.mime === "string" && body.mime ? body.mime : "application/octet-stream",
      // base64 的字节数 ≈ 长度 × 3/4（含 padding 的误差可以忽略，只用来看大小文案）。
      size_bytes: Math.max(1, Math.floor((base64.length * 3) / 4)),
      kind: kind === "unsupported" ? "text" : kind,
      status: scanned ? "failed" : "ready",
      error: scanned ? "未能从 PDF 中可靠提取文本（可能是扫描件）。" : null,
      created_at: new Date().toISOString(),
    };
    registry.set(id, created);
    return jsonResponse(created, 201);
  }

  if (method === "DELETE" && url.pathname.startsWith("/api/v1/attachments/")) {
    registry.delete(decodeURIComponent(url.pathname.slice("/api/v1/attachments/".length)));
    return jsonResponse(null);
  }

  // 发消息：把这条消息真的推进该会话的消息表，否则前端发完立刻重拉 GET /messages，
  // 刚发出去的那条会人间蒸发——评审时会以为是 bug。
  if (method === "POST" && /^\/api\/v1\/sessions\/[^/]+\/messages$/.test(url.pathname)) {
    const body = payload();
    const ids = Array.isArray(body.attachment_ids) ? (body.attachment_ids as string[]) : [];
    const attached = ids
      .map((id) => registry.get(id))
      .filter((item): item is Attachment => Boolean(item));
    const store = seed[`GET ${url.pathname}`] as { items: Message[] } | undefined;
    const messageId = `m-preview-${(store?.items.length ?? 0) + 1}`;
    if (store) {
      store.items.push({
        id: messageId,
        session_id: decodeURIComponent(url.pathname.split("/")[4] ?? ""),
        role: "user",
        content: typeof body.content === "string" ? body.content : "",
        agent_run_id: null,
        status: "done",
        created_at: new Date().toISOString(),
        attachments: attached,
      });
    }
    return jsonResponse(
      {
        message_id: messageId,
        session_id: decodeURIComponent(url.pathname.split("/")[4] ?? ""),
        agent_run_id: "run-preview",
        workflow_id: "wf-preview",
        status: "accepted",
        attachments: attached,
        // 没登记上的 id 如实回传，顺带给「部分失败」那条提示一个可复现的入口。
        unattached_attachment_ids: ids.filter((id) => !registry.has(id)),
      },
      202,
    );
  }

  if (key in seed) return jsonResponse(seed[key]);

  // 变更类请求（新建 / 保存 / 删除）预览不模拟落库，回一个空成功体，
  // 免得点一下「保存」就弹红字把版式评审带偏。
  if (method !== "GET" && url.pathname.startsWith("/api/v1/config/")) return jsonResponse({});

  // 未收录的同源路径：明确 404，方便发现漏配的路由
  if (!realFetch || url.origin === (globalThis.location?.origin ?? "")) {
    return jsonResponse({ code: "NOT_FOUND", message: `预览未收录：${key}` }, 404);
  }
  return realFetch(input as RequestInfo, init);
}) as typeof fetch;

const container = document.getElementById("root");
if (container) {
  createRoot(container).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
