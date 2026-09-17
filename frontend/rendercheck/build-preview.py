#!/usr/bin/env python3
"""构建自包含的 UI 评审预览页 `rendercheck/ui-preview.html`。

为什么需要它：后端起真需要 Postgres + Dapr，评审「版式有没有改对」不该被环境卡住。
本脚本把**真实的 `App`** 与**项目真实样式**打成一个单文件 HTML，后端请求由
`preview-seed.py` 生成的 mock 数据应答，双击即可打开、无需服务、无需数据库。

三步：
  1. 引入同目录 `preview-seed.py`，把它的数据摊平成 `"<METHOD> <path>" -> payload`
     （动态 id 在 Python 侧就固定成常量，浏览器里的 mock 因此只需一次查表）；
  2. 用 esbuild 把 `preview.tsx` 打成浏览器 IIFE（含真实 CSS，不做 empty 替换）；
  3. 把 JS / CSS / 种子 JSON 内联进一个 .html。

用法（在任意目录均可执行）：
    python frontend/rendercheck/build-preview.py            # 产出 rendercheck/ui-preview.html
    python frontend/rendercheck/build-preview.py --out /tmp/x.html

可用环境变量覆盖工具位置：`NODE_BIN`、`ESBUILD_BIN`。
产物 `ui-preview.html` 与中间产物 `.preview-bundle.*` 已在 .gitignore 中。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.dirname(BASE)
sys.path.insert(0, BASE)

import preview_seed as S  # noqa: E402  (同目录，需先把 BASE 放进 sys.path)

DEFAULT_OUT = os.path.join(BASE, "ui-preview.html")
BUNDLE_JS = os.path.join(BASE, ".preview-bundle.js")

# 预览里固定使用的 id，好让浏览器端 mock 只需查表、不必造 id
SESSION_ID = "s-preview"
WORKFLOW_ID = "wf-preview"
RUN_ID = "run-preview"


def flatten() -> dict:
    """把 `preview-seed` 的数据摊成 `"<METHOD> <path>" -> payload` 路由表。"""
    session = {"id": SESSION_ID, "user_id": "demo-user", "status": "active",
               "created_at": S.iso(-3600), "updated_at": S.iso()}

    workflow = {
        "id": WORKFLOW_ID, "session_id": SESSION_ID, "agent_run_id": RUN_ID,
        "status": "running", "current_step": "report",
        "checkpoint": {"status": "running", "current_step": "report",
                       "completed_steps": ["collect", "analyze"], "updated_at": S.iso()},
        "created_at": S.iso(-300), "updated_at": S.iso(), "completed_at": None,
    }

    tool_calls = [
        {"id": "tc-1", "run_id": RUN_ID, "workflow_run_id": WORKFLOW_ID,
         "tool_name": "web_search", "input": {"query": "2026 年多智能体协作平台调研"},
         "output": {"hits": 12}, "status": "succeeded", "error": None,
         "created_at": S.iso(-280), "updated_at": S.iso(-275)},
        {"id": "tc-2", "run_id": RUN_ID, "workflow_run_id": WORKFLOW_ID,
         "tool_name": "read_file", "input": {"path": "/data/source/q3.md"},
         "output": {"bytes": 40960}, "status": "succeeded", "error": None,
         "created_at": S.iso(-240), "updated_at": S.iso(-238)},
        {"id": "tc-3", "run_id": RUN_ID, "workflow_run_id": WORKFLOW_ID,
         "tool_name": "write_file", "input": {"path": "/data/out/draft.md"},
         "output": None, "status": "failed",
         "error": "权限不足：该工具已被配置为禁用。",
         "created_at": S.iso(-120), "updated_at": S.iso(-119)},
        {"id": "tc-4", "run_id": RUN_ID, "workflow_run_id": WORKFLOW_ID,
         "tool_name": "list_directory", "input": {"path": "/data/out"},
         "output": {"entries": 4}, "status": "running", "error": None,
         "created_at": S.iso(-10), "updated_at": S.iso(-10)},
    ]

    messages = [
        {"id": "m-1", "session_id": SESSION_ID, "role": "user",
         "content": "帮我调研 2026 年多智能体协作平台的开源方案，并输出一份对比报告。",
         "agent_run_id": None, "status": "done", "created_at": S.iso(-320)},
        {"id": "m-2", "session_id": SESSION_ID, "role": "assistant",
         "content": "已拆分为「信息收集 → 数据分析 → 报告生成」三步，前两步已完成，正在生成报告。",
         "agent_run_id": RUN_ID, "status": "done", "created_at": S.iso(-300)},
    ]

    tools = [
        {"name": t["name"], "description": t["description"],
         "input_schema": {"type": "object", "properties": {}}, "status": "available"}
        for t in S.MCP_TOOLS
    ]

    models_by_provider: dict = {}
    for m in S.MODELS:
        models_by_provider.setdefault(m["provider_id"], []).append(m)

    table: dict = {
        "POST /api/v1/sessions": session,
        # 侧栏「当前任务」下拉 + 记录页「历史会话」分区共用：当前会话摘要 + 两条演示历史。
        "GET /api/v1/sessions": {
            "items": [
                {**session, "title": "帮我调研 2026 年多智能体协作平台的开源方案",
                 "latest_workflow_status": "running", "latest_workflow_id": WORKFLOW_ID},
                {"id": "s-history-1", "user_id": "demo-user", "status": "active",
                 "title": "对比三种向量数据库的检索性能",
                 "latest_workflow_status": "completed", "latest_workflow_id": "wf-history-1",
                 "created_at": S.iso(-86400), "updated_at": S.iso(-86300)},
                {"id": "s-history-2", "user_id": "demo-user", "status": "paused",
                 "title": "整理本周需求评审会议纪要",
                 "latest_workflow_status": "paused", "latest_workflow_id": "wf-history-2",
                 "created_at": S.iso(-172800), "updated_at": S.iso(-172700)},
            ],
            "page": 1, "page_size": 20, "total": 3,
        },
        f"GET /api/v1/sessions/{SESSION_ID}": session,
        f"DELETE /api/v1/sessions/{SESSION_ID}": None,
        f"DELETE /api/v1/sessions/s-history-1": None,
        f"DELETE /api/v1/sessions/s-history-2": None,
        f"GET /api/v1/sessions/{SESSION_ID}/messages": {"items": messages},
        f"POST /api/v1/sessions/{SESSION_ID}/messages": {
            "message_id": "m-3", "session_id": SESSION_ID, "agent_run_id": RUN_ID,
            "workflow_id": WORKFLOW_ID, "status": "accepted",
        },
        f"POST /api/v1/sessions/{SESSION_ID}/pause": {
            "session": {**session, "status": "paused"}, "workflow": {**workflow, "status": "paused"}},
        f"POST /api/v1/sessions/{SESSION_ID}/resume": {"session": session, "workflow": workflow},
        "GET /api/v1/sessions/s-history-1": {
            "id": "s-history-1", "user_id": "demo-user", "status": "active",
            "created_at": S.iso(-86400), "updated_at": S.iso(-86300)},
        "GET /api/v1/sessions/s-history-1/messages": {
            "items": [{"id": "mh-1", "session_id": "s-history-1", "role": "user",
                       "content": "对比三种向量数据库的检索性能", "agent_run_id": None,
                       "status": "done", "created_at": S.iso(-86400)}]},
        "GET /api/v1/workflows/wf-history-1": {
            "id": "wf-history-1", "session_id": "s-history-1", "agent_run_id": "run-h1",
            "status": "completed", "current_step": None,
            "checkpoint": {"status": "completed", "current_step": None,
                           "completed_steps": ["collect", "analyze", "report"],
                           "updated_at": S.iso(-86300)},
            "created_at": S.iso(-86300), "updated_at": S.iso(-86300), "completed_at": S.iso(-86200)},
        f"GET /api/v1/workflows/{WORKFLOW_ID}": workflow,
        f"GET /api/v1/workflows/{WORKFLOW_ID}/tool-calls": S.page(tool_calls),
        "GET /api/v1/metrics": S.page(S.metrics_for(WORKFLOW_ID)),
        "GET /api/v1/agents": {"items": S.AGENTS},
        "GET /api/v1/providers": {"items": S.PROVIDERS},
        "GET /api/v1/tools": S.page(tools),
        "GET /api/v1/config/provider": S.PROVIDER_CONFIG,
        "PUT /api/v1/config/provider": S.PROVIDER_CONFIG,
        "GET /api/v1/config/provider-presets": S.PRESETS,
        # 执行边界（§5.15）：只读分区，预览按真实部署给「不可用 + 原因」。
        "GET /api/v1/config/sandbox": S.SANDBOX_STATUS,
        "GET /api/v1/config/providers": {"items": S.PROVIDERS, "total": len(S.PROVIDERS)},
        "GET /api/v1/config/models": {"items": S.MODELS, "total": len(S.MODELS)},
        "GET /api/v1/config/mcp/servers": {"items": S.MCP_SERVERS, "total": len(S.MCP_SERVERS)},
        "GET /api/v1/config/mcp/tools": {
            "items": S.MCP_TOOLS, "total": len(S.MCP_TOOLS),
            "servers": [{k: s[k] for k in
                         ("id", "name", "transport", "enabled", "tool_count", "discovered_at")}
                        for s in S.MCP_SERVERS],
        },
        "GET /api/v1/config/agents": {
            "items": S.AGENTS,
            "available_models": [{k: m[k] for k in
                                  ("id", "provider_id", "model", "name", "enabled")}
                                 for m in S.MODELS if m["enabled"]],
        },
    }

    for base in S.PROVIDERS:
        pid = base["id"]
        table[f"GET /api/v1/config/providers/{pid}"] = {
            **base, "models": models_by_provider.get(pid, []),
        }
        table[f"GET /api/v1/config/providers/{pid}/models/discover"] = {
            "provider_id": pid, "source": "preset",
            "items": [{"id": f"{pid}-remote-a", "name": f"{pid}-remote-a", "owned_by": pid},
                      {"id": f"{pid}-remote-b", "name": f"{pid}-remote-b", "owned_by": pid}],
            "existing": [m["model"] for m in models_by_provider.get(pid, [])],
            "total": 2,
        }

    for srv in S.MCP_SERVERS:
        sid = srv["id"]
        table[f"POST /api/v1/config/mcp/servers/{sid}/discover"] = {
            "server_id": sid, "server_info": srv["server_info"],
            "tools": [{"name": t["name"], "description": t["description"]}
                      for t in S.MCP_TOOLS if t["server_id"] == sid],
            "total": len([t for t in S.MCP_TOOLS if t["server_id"] == sid]),
            "discovered_at": S.iso(),
        }

    return table


def resolve_bins() -> tuple:
    node = os.environ.get("NODE_BIN") or shutil.which("node") or shutil.which("node.exe")
    if not node:
        raise SystemExit("找不到 node；请设置 NODE_BIN 环境变量。")
    esbuild = os.environ.get("ESBUILD_BIN") or os.path.join(
        FRONTEND, "node_modules", "esbuild", "bin", "esbuild")
    if not os.path.exists(esbuild):
        raise SystemExit(
            f"找不到 esbuild（{esbuild}）。先在 frontend/ 下执行 npm ci。")
    return node, esbuild


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>多智能体协作平台 · UI 评审预览</title>
<style>
/* ===== 项目真实样式：styles.css + config.css + page-tabs.css + records.css ===== */
{css}
</style>
</head>
<body>
<div id="root"></div>
<script id="seed" type="application/json">{seed}</script>
<script>
/* 评审用 mock 数据，由 rendercheck/build-preview.py 从 preview-seed.py 生成。 */
window.__SEED__ = JSON.parse(document.getElementById("seed").textContent);
</script>
<script>
{js}
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="构建自包含 UI 评审预览页")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出 HTML 路径")
    args = parser.parse_args()

    node, esbuild = resolve_bins()
    table = flatten()
    seed_json = json.dumps(table, ensure_ascii=False)

    env = dict(os.environ)
    env["PATH"] = os.path.dirname(node) + os.pathsep + env.get("PATH", "")
    build = subprocess.run(
        [node, esbuild, os.path.relpath(os.path.join(BASE, "preview.tsx"), FRONTEND),
         "--bundle", "--platform=browser", "--format=iife", "--jsx=automatic",
         "--target=es2020", "--charset=utf8",
         '--define:process.env.NODE_ENV="production"',
         f"--outfile={BUNDLE_JS}"],
        cwd=FRONTEND, env=env, capture_output=True, text=True,
    )
    if build.returncode != 0:
        print(build.stdout)
        print(build.stderr)
        return build.returncode

    css_path = os.path.splitext(BUNDLE_JS)[0] + ".css"
    js = open(BUNDLE_JS, encoding="utf-8").read()
    css = open(css_path, encoding="utf-8").read() if os.path.exists(css_path) else ""

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(PAGE.format(css=css, seed=seed_json, js=js))

    print(f"路由 {len(table)} 条 | js {len(js) // 1024}KB | css {len(css) // 1024}KB "
          f"| seed {len(seed_json) // 1024}KB")
    print(f"已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
