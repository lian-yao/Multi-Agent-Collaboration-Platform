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
// 真实入口 `src/main.tsx` 才有这一行；这里必须显式引入，否则预览会缺全局令牌与外壳版式。
import "../src/styles.css";

type Seed = Record<string, unknown>;

const seed: Seed = (globalThis as { __SEED__?: Seed }).__SEED__ ?? {};

const jsonResponse = (payload: unknown, status = 200): Response =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// jsdom（离线自检用）不提供 fetch，所以这里不能假设它存在。
const realFetch: typeof fetch | null =
  typeof globalThis.fetch === "function" ? globalThis.fetch.bind(globalThis) : null;

globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(raw, globalThis.location?.href ?? "http://localhost/");
  const method = (init?.method ?? "GET").toUpperCase();
  const key = `${method} ${url.pathname}`;

  // 会话删除：不真的落库，但把它从种子列表里摘掉，这样「删掉的那一行真的消失」
  // 可以在预览里肉眼确认（评审的是行内二次确认的交互，不是持久化）。
  if (method === "DELETE" && url.pathname.startsWith("/api/v1/sessions/")) {
    const id = decodeURIComponent(url.pathname.slice("/api/v1/sessions/".length));
    const list = seed["GET /api/v1/sessions"] as { items?: { id: string }[] } | undefined;
    if (list?.items) list.items = list.items.filter((item) => item.id !== id);
    return jsonResponse(null);
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
