"""UI 评审预览用的种子数据（`rendercheck/build-preview.py` 的输入）。

两处用途，共用同一份数据、避免漂移：
  1. 被 `build-preview.py` 摊平成静态路由表，内联进 `ui-preview.html`
     —— 这是常规用法，产出单文件预览，离线可开；
  2. 也可以直接跑起来当临时后端（`python rendercheck/preview-seed.py`，监听 8000），
     配合 `npm run dev` 的 `/api` 代理做可交互的联调。

数据只为「版式与状态覆盖」服务，不是真实业务数据；也没有持久化与校验。
"""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NOW = datetime.now(timezone.utc)


def iso(delta_seconds: float = 0) -> str:
    return (NOW + timedelta(seconds=delta_seconds)).isoformat().replace("+00:00", "Z")


def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- seed data

AGENTS = [
    {
        "id": "collector", "name": "信息收集 Agent", "role": "collector",
        "model": "gpt-5.5", "provider": "openai-main", "provider_name": "OpenAI 主端点",
        "llm_model_id": "openai-main:gpt-5.5", "temperature": 0.3, "top_p": None,
        "max_output_tokens": None, "reasoning_type": "openai", "status": "running",
        "override_keys": ["temperature"],
        "builtin": True, "description": "收集、检索并整理任务主题相关的事实与要点。", "enabled": True,
    },
    {
        "id": "analyst", "name": "数据分析 Agent", "role": "analyst",
        "model": "claude-sonnet-4", "provider": "anthropic-main", "provider_name": "Anthropic 主端点",
        "llm_model_id": "anthropic-main:claude-sonnet-4", "temperature": 0.2, "top_p": 0.9,
        "max_output_tokens": 8192, "reasoning_type": "anthropic", "status": "running",
        "override_keys": ["temperature", "top_p", "max_output_tokens"],
        "builtin": True, "description": "基于信息清单进行归纳、对比与提炼。", "enabled": True,
    },
    {
        "id": "reporter", "name": "报告生成 Agent", "role": "reporter",
        "model": "gemini-2.5-pro", "provider": "google-main", "provider_name": "Google 主端点",
        "llm_model_id": "google-main:gemini-2.5-pro", "temperature": 0.5, "top_p": None,
        "max_output_tokens": None, "reasoning_type": "gemini", "status": "idle",
        "override_keys": [],
        "builtin": True, "description": "整合分析摘要，生成结构清晰的正式报告。", "enabled": True,
    },
    {
        "id": "planner", "name": "任务规划 Agent", "role": "planner",
        "model": "deepseek-v3.2", "provider": "deepseek-main", "provider_name": "DeepSeek 端点",
        "llm_model_id": None, "temperature": 0.1, "top_p": None, "max_output_tokens": None,
        "reasoning_type": "none", "status": "idle", "override_keys": [],
        "builtin": False, "description": "拆解任务并规划执行步骤（自定义角色）。", "enabled": True,
    },
]

PROVIDERS = [
    {
        "id": "openai-main", "name": "OpenAI 主端点", "preset_type": "openai",
        "api_type": "openai-compatible", "base_url": "https://api.openai.com/v1",
        "api_key_configured": True, "custom_headers": {}, "additional_settings": {},
        "enabled": True, "model_count": 3,
        "created_at": iso(-86400), "updated_at": iso(-3600), "updated_by": "demo-user",
    },
    {
        "id": "anthropic-main", "name": "Anthropic 主端点", "preset_type": "anthropic",
        "api_type": "anthropic", "base_url": "https://api.anthropic.com",
        "api_key_configured": True, "custom_headers": {"anthropic-version": "2023-06-01"},
        "additional_settings": {}, "enabled": True, "model_count": 2,
        "created_at": iso(-172800), "updated_at": iso(-7200), "updated_by": None,
    },
    {
        "id": "vllm-local", "name": "本地 vLLM（自建）", "preset_type": "openai-compatible",
        "api_type": "openai-compatible", "base_url": "http://host.docker.internal:49411/v1",
        "api_key_configured": False, "custom_headers": {}, "additional_settings": {"timeout_s": 120},
        "enabled": False, "model_count": 1,
        "created_at": iso(-604800), "updated_at": iso(-604800), "updated_by": "demo-user",
    },
]

MODELS = [
    {"id": "openai-main:gpt-5.5", "provider_id": "openai-main", "model": "gpt-5.5", "name": "GPT-5.5",
     "enabled": True, "reasoning_type": "openai", "temperature": 0.3, "top_p": None,
     "max_context_tokens": 200000, "max_output_tokens": 16384, "custom_parameters": [],
     "created_at": iso(-86400), "updated_at": iso(-3600), "updated_by": None},
    {"id": "openai-main:gpt-5.5-mini", "provider_id": "openai-main", "model": "gpt-5.5-mini", "name": "GPT-5.5 mini",
     "enabled": True, "reasoning_type": "openai", "temperature": None, "top_p": None,
     "max_context_tokens": 128000, "max_output_tokens": None, "custom_parameters": [],
     "created_at": iso(-86400), "updated_at": None, "updated_by": None},
    {"id": "anthropic-main:claude-sonnet-4", "provider_id": "anthropic-main", "model": "claude-sonnet-4",
     "name": "Claude Sonnet 4", "enabled": True, "reasoning_type": "anthropic", "temperature": 0.2,
     "top_p": 0.9, "max_context_tokens": 200000, "max_output_tokens": 8192,
     "custom_parameters": [{"key": "thinking_budget", "value": "4096", "type": "number"}],
     "created_at": iso(-172800), "updated_at": iso(-7200), "updated_by": None},
    {"id": "google-main:gemini-2.5-pro", "provider_id": "google-main", "model": "gemini-2.5-pro",
     "name": "Gemini 2.5 Pro", "enabled": True, "reasoning_type": "gemini", "temperature": 0.5,
     "top_p": None, "max_context_tokens": 1000000, "max_output_tokens": 65536, "custom_parameters": [],
     "created_at": iso(-259200), "updated_at": None, "updated_by": None},
    {"id": "vllm-local:qwen3-32b", "provider_id": "vllm-local", "model": "qwen3-32b", "name": None,
     "enabled": False, "reasoning_type": "none", "temperature": None, "top_p": None,
     "max_context_tokens": 32768, "max_output_tokens": None, "custom_parameters": [],
     "created_at": iso(-604800), "updated_at": None, "updated_by": None},
]

MCP_SERVERS = [
    {"id": "filesystem", "name": "本地文件系统", "transport": "stdio", "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"], "env": {},
     "cwd": "/data", "url": None, "headers": {}, "enabled": True,
     "tool_options": {"read_file": {"allowAutoExecution": True}, "write_file": {"disabled": True}},
     "tool_count": 6, "discovered_at": iso(-1800),
     "server_info": {"name": "filesystem", "version": "0.6.2"},
     "created_at": iso(-86400), "updated_at": iso(-1800), "updated_by": "demo-user"},
    {"id": "web-search", "name": "联网检索", "transport": "http", "command": None, "args": [],
     "env": {}, "cwd": None, "url": "http://host.docker.internal:8931/mcp",
     "headers": {"X-Tenant": "research"}, "enabled": True, "tool_options": {},
     "tool_count": 2, "discovered_at": iso(-900), "server_info": {"name": "web-search", "version": "1.1.0"},
     "created_at": iso(-43200), "updated_at": iso(-900), "updated_by": None},
    {"id": "sqlite-lab", "name": "实验数据库", "transport": "stdio", "command": "uvx",
     "args": ["mcp-server-sqlite", "--db", "/data/lab.db"], "env": {}, "cwd": None, "url": None,
     "headers": {}, "enabled": False, "tool_options": {}, "tool_count": 0, "discovered_at": None,
     "server_info": None, "created_at": iso(-7200), "updated_at": iso(-7200), "updated_by": "demo-user"},
]

MCP_TOOLS = [
    {"server_id": "filesystem", "server_name": "本地文件系统", "enabled": True, "name": "read_file",
     "description": "读取指定路径的文件内容。", "tool_enabled": True, "available": True},
    {"server_id": "filesystem", "server_name": "本地文件系统", "enabled": True, "name": "list_directory",
     "description": "列出目录下的文件与子目录。", "tool_enabled": True, "available": True},
    {"server_id": "filesystem", "server_name": "本地文件系统", "enabled": True, "name": "write_file",
     "description": "写入文件内容（当前被禁用）。", "tool_enabled": False, "available": True},
    {"server_id": "filesystem", "server_name": "本地文件系统", "enabled": True, "name": "search_files",
     "description": "按通配符检索文件名。", "tool_enabled": True, "available": True},
    {"server_id": "web-search", "server_name": "联网检索", "enabled": True, "name": "web_search",
     "description": "执行一次联网检索并返回摘要。", "tool_enabled": True, "available": True},
    {"server_id": "web-search", "server_name": "联网检索", "enabled": True, "name": "fetch_page",
     "description": "抓取网页正文并转成 markdown。", "tool_enabled": True, "available": False},
]

PRESETS = {
    "items": [
        {"preset_type": "openai", "label": "OpenAI", "monogram": "OA", "tint": "blue",
         "category": "international", "default_api_type": "openai-compatible",
         "supported_api_types": ["openai-compatible", "openai-responses"],
         "default_base_url": "https://api.openai.com/v1", "requires_api_key": True,
         "api_key_url": "https://platform.openai.com/api-keys", "supports_model_discovery": True},
        {"preset_type": "anthropic", "label": "Anthropic", "monogram": "AN", "tint": "amber",
         "category": "international", "default_api_type": "anthropic",
         "supported_api_types": ["anthropic"], "default_base_url": "https://api.anthropic.com",
         "requires_api_key": True, "api_key_url": "https://console.anthropic.com/settings/keys",
         "supports_model_discovery": True},
        {"preset_type": "google", "label": "Google Gemini", "monogram": "GE", "tint": "purple",
         "category": "international", "default_api_type": "gemini",
         "supported_api_types": ["gemini"], "default_base_url": "https://generativelanguage.googleapis.com",
         "requires_api_key": True, "api_key_url": "https://aistudio.google.com/app/apikey",
         "supports_model_discovery": True},
        {"preset_type": "deepseek", "label": "DeepSeek", "monogram": "DS", "tint": "indigo",
         "category": "domestic", "default_api_type": "openai-compatible",
         "supported_api_types": ["openai-compatible"], "default_base_url": "https://api.deepseek.com/v1",
         "requires_api_key": True, "api_key_url": "https://platform.deepseek.com/api_keys",
         "supports_model_discovery": True},
        {"preset_type": "qwen", "label": "通义千问", "monogram": "QW", "tint": "rose",
         "category": "domestic", "default_api_type": "openai-compatible",
         "supported_api_types": ["openai-compatible"],
         "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
         "requires_api_key": True, "api_key_url": "https://bailian.console.aliyun.com/",
         "supports_model_discovery": True},
        {"preset_type": "openai-compatible", "label": "OpenAI 兼容（自定义）", "monogram": "自",
         "tint": "slate", "category": "gateway", "default_api_type": "openai-compatible",
         "supported_api_types": ["openai-compatible"], "default_base_url": "",
         "requires_api_key": False, "api_key_url": None, "supports_model_discovery": True},
        {"preset_type": "amazon-bedrock", "label": "Amazon Bedrock", "monogram": "AB", "tint": "amber",
         "category": "gateway", "default_api_type": "amazon-bedrock",
         "supported_api_types": ["amazon-bedrock"], "default_base_url": "",
         "requires_api_key": True, "api_key_url": None, "supports_model_discovery": False},
    ],
    "categories": [
        {"id": "international", "label": "国际厂商"},
        {"id": "domestic", "label": "国内厂商"},
        {"id": "gateway", "label": "自建与网关"},
    ],
}

PROVIDER_CONFIG = {
    "provider": "openai", "model": "gpt-5.5", "base_url": "https://api.openai.com/v1",
    "temperature": 0.3, "top_p": 0.95, "max_tokens": 8192, "api_key_configured": True,
    "default_llm_model_id": "openai-main:gpt-5.5", "llm_model_id": "openai-main:gpt-5.5",
    "provider_name": "OpenAI 主端点", "preset_type": "openai", "api_type": "openai-compatible",
    "reasoning_type": "openai", "updated_by": "demo-user", "updated_at": iso(-3600),
}

# ------------------------------------------------------- mutable demo state

STATE = {
    "sessions": {},       # id -> Session
    "messages": {},       # session_id -> [Message]
    "workflows": {},      # id -> Workflow
    "tool_calls": {},     # workflow_id -> [ToolCall]
}

STEPS = ["collect", "analyze", "report"]


def new_session() -> dict:
    sid = uid("s")
    session = {"id": sid, "user_id": "demo-user", "status": "active",
               "created_at": iso(), "updated_at": iso()}
    STATE["sessions"][sid] = session
    STATE["messages"][sid] = []
    return session


def list_sessions_summary() -> dict:
    """`GET /api/v1/sessions` 的种子：按 updated_at 倒序，带 title / workflow 摘要。"""
    items = []
    for session in sorted(
        STATE["sessions"].values(), key=lambda s: s["updated_at"], reverse=True
    ):
        msgs = [m for m in STATE["messages"].get(session["id"], []) if m["role"] == "user"]
        title = msgs[0]["content"][:60] if msgs else "（暂无消息）"
        wfs = [w for w in STATE["workflows"].values() if w["session_id"] == session["id"]]
        latest = max(wfs, key=lambda w: w["created_at"]) if wfs else None
        items.append({
            **session,
            "title": title,
            "latest_workflow_status": latest["status"] if latest else None,
            "latest_workflow_id": latest["id"] if latest else None,
        })
    return {"items": items, "page": 1, "page_size": 20, "total": len(items)}


def new_workflow(session_id: str, completed: int = 2, status: str = "running") -> dict:
    wid = uid("wf")
    done = STEPS[:completed]
    current = STEPS[completed] if completed < len(STEPS) else None
    workflow = {
        "id": wid, "session_id": session_id, "agent_run_id": uid("run"), "status": status,
        "current_step": current,
        "checkpoint": {"status": status, "current_step": current,
                       "completed_steps": done, "updated_at": iso()},
        "created_at": iso(-300), "updated_at": iso(), "completed_at": None,
    }
    STATE["workflows"][wid] = workflow
    STATE["tool_calls"][wid] = [
        {"id": uid("tc"), "run_id": workflow["agent_run_id"], "workflow_run_id": wid,
         "tool_name": "web_search", "input": {"query": "2026 年多智能体协作平台调研"},
         "output": {"hits": 12}, "status": "succeeded", "error": None,
         "created_at": iso(-280), "updated_at": iso(-275)},
        {"id": uid("tc"), "run_id": workflow["agent_run_id"], "workflow_run_id": wid,
         "tool_name": "read_file", "input": {"path": "/data/source/q3.md"},
         "output": {"bytes": 40960}, "status": "succeeded", "error": None,
         "created_at": iso(-240), "updated_at": iso(-238)},
        {"id": uid("tc"), "run_id": workflow["agent_run_id"], "workflow_run_id": wid,
         "tool_name": "write_file", "input": {"path": "/data/out/draft.md"},
         "output": None, "status": "failed",
         "error": "权限不足：该工具已被配置为禁用。",
         "created_at": iso(-120), "updated_at": iso(-119)},
        {"id": uid("tc"), "run_id": workflow["agent_run_id"], "workflow_run_id": wid,
         "tool_name": "list_directory", "input": {"path": "/data/out"},
         "output": {"entries": 4}, "status": "running", "error": None,
         "created_at": iso(-10), "updated_at": iso(-10)},
    ]
    return workflow


# 动态编排的计划种子（ADR-019）。`depends_on` 决定画布上的波次，所以这里刻意给一份
# 「扇出 → 并行 → 汇聚」：s1 收集完分给两路（s2 继续深挖、s3 先做分析），两路并行，
# 最后由 s4 汇总。预览页因此能看到「并行协作区」，而不是只有静态三步那条串行链。
#
# 步骤数只有 4，但形状是三波：`[s1] → [s2, s3] → [s4]`。这正是「只能画串行节点」
# 这个判断的反例，也是动态链路相对静态链路的全部价值所在。
DYNAMIC_PLAN = [
    {"id": "s1", "role": "collector", "depends_on": [], "status": "completed"},
    {"id": "s2", "role": "collector", "depends_on": ["s1"], "status": "completed"},
    {"id": "s3", "role": "analyst", "depends_on": ["s1"], "status": "completed"},
    {"id": "s4", "role": "reporter", "depends_on": ["s2", "s3"], "status": "pending"},
]

# 与后端 `app/api/stage_trace.py::DYNAMIC_TRACE_REASON` 逐字一致：预览页给出的说明
# 不能比真实环境更乐观，否则「动态链路看不到详情」这个真问题在预览里会被掩盖。
DYNAMIC_TRACE_REASON = (
    "本次执行走的是动态编排链路，它当前不落盘逐步骤执行轨迹（ADR-019）；"
    "可执行到的替代信息是各步骤的阶段状态与「任务记录」里的工具调用链路。"
)


def new_dynamic_workflow(session_id: str) -> dict:
    """动态编排链路的工作流种子：`checkpoint` 多出 `mode` / `plan_source` / `plan`。

    形状与 `new_workflow` 一致（前端只认 `checkpoint` 这几个字段），差别全在那三个
    动态独有的字段上——画布正是靠 `plan[].depends_on` 算出波次与并行关系。
    `created_at` 取 -100（晚于静态那条的 -300），让它在「对话 N」里排到最后，
    不挤占既有预览的「对话 1/2」编号。
    """

    wid = uid("wf")
    workflow = {
        "id": wid, "session_id": session_id, "agent_run_id": uid("run"),
        "status": "running", "current_step": None,
        "checkpoint": {
            "mode": "dynamic", "status": "running", "current_step": None,
            "completed_steps": ["s1", "s2", "s3"],
            "plan_source": "llm",
            "plan": [dict(step) for step in DYNAMIC_PLAN],
            "updated_at": iso(),
        },
        "created_at": iso(-100), "updated_at": iso(), "completed_at": None,
    }
    STATE["workflows"][wid] = workflow
    STATE["tool_calls"][wid] = [
        {"id": uid("tc"), "run_id": workflow["agent_run_id"], "workflow_run_id": wid,
         "tool_name": "web_search", "input": {"query": "三份竞品的定价与核心功能"},
         "output": {"hits": 8}, "status": "succeeded", "error": None,
         "created_at": iso(-95), "updated_at": iso(-93)},
        {"id": uid("tc"), "run_id": workflow["agent_run_id"], "workflow_run_id": wid,
         "tool_name": "calculator", "input": {"expression": "2 * (3 + 4)"},
         "output": {"result": 14}, "status": "succeeded", "error": None,
         "created_at": iso(-92), "updated_at": iso(-91)},
    ]
    return workflow


ROLE_OF = {"collect": "collector", "analyze": "analyst", "report": "reporter"}

# 逐阶段轨迹的种子（§5.17）：内容按「一个真的跑过的任务」写，包含一次失败调用——
# 预览页要能看到失败态的样子，而不是三条全是绿灯的假数据。
STAGE_TRACE_SEED = {
    "collect": {
        "input": None,
        "input_from": None,
        "output": "已收集 12 篇来源与 2 份内部资料，去重后保留 9 篇，覆盖 2024–2026 年的基准测试。",
        "tool_calls": [
            {"call_id": "seed-collect-1", "tool_name": "web_search",
             "input": {"query": "向量数据库 检索性能 基准"}, "output": {"hits": 12},
             "status": "succeeded", "error": None},
            {"call_id": "seed-collect-2", "tool_name": "list_session_files",
             "input": {"session_id": "s-demo"}, "output": ["bench-2025.md", "notes.md"],
             "status": "succeeded", "error": None},
        ],
    },
    "analyze": {
        "input": "已收集 12 篇来源与 2 份内部资料，去重后保留 9 篇，覆盖 2024–2026 年的基准测试。",
        "input_from": "collect",
        "output": "三家在 100 万向量规模下的 QPS 分别是 1480 / 960 / 720；召回率差异小于 1%。"
                  "写入吞吐上 B 方案明显落后，若以只读检索为主则差异可以忽略。",
        "tool_calls": [
            {"call_id": "seed-analyze-1", "tool_name": "sql_query",
             "input": {"sql": "select engine, qps from bench where scale = 1e6"},
             "output": {"rows": 3}, "status": "succeeded", "error": None},
            {"call_id": "seed-analyze-2", "tool_name": "code_execution",
             "input": {"language": "python", "code": "import matplotlib"},
             "output": None, "status": "failed",
             "error": "SandboxViolation: 只读沙箱拒绝写文件（charts/ 不在允许的写入范围）"},
        ],
    },
    "report": {
        "input": "三家在 100 万向量规模下的 QPS 分别是 1480 / 960 / 720；召回率差异小于 1%。"
                 "写入吞吐上 B 方案明显落后，若以只读检索为主则差异可以忽略。",
        "input_from": "analyze",
        "output": "结论：只读检索场景选 A；需要频繁写入且对延迟不敏感时选 C；"
                  "B 仅在前两者都不可用时作为候选。建议先按 100 万规模做一次线上压测再定。",
        "tool_calls": [],
    },
}


def stage_traces(
    workflow_id: str,
    *,
    done: tuple = (),
    current: str | None = None,
) -> dict:
    """`GET /api/v1/workflows/{id}/stages` 的种子（§5.17）。

    默认从 `STATE["workflows"]` 取进度：已完成的阶段给轨迹、当前阶段说明「正在执行」、
    其余说明「尚未开始」——三种状态都要能在预览页里看到。构建静态预览时可直接传
    `done` / `current`，不必先造一个 Workflow 进 STATE。
    """
    workflow = STATE["workflows"].get(workflow_id) or {}
    checkpoint = workflow.get("checkpoint") or {}

    # 动态链路不落盘逐步骤轨迹：与后端 `read_stage_traces` 同口径返回 `not_integrated`。
    # 画布的形状来自 `checkpoint.plan`（走另一个字段，不靠这个接口），这里只负责把
    # 「详情为什么是空的」说清楚——不说，预览页就会把「未集成」看成「没跑」。
    if checkpoint.get("mode") == "dynamic":
        return {
            "workflow_id": workflow_id,
            "mode": "dynamic",
            "task": None,
            "availability": "not_integrated",
            "reason": DYNAMIC_TRACE_REASON,
            "items": [],
        }

    if not done and current is None:
        done = tuple(checkpoint.get("completed_steps") or ())
        current = checkpoint.get("current_step")

    items = []
    for stage in STEPS:
        seed = STAGE_TRACE_SEED[stage]
        if stage in done:
            item = {"stage": stage, "role": ROLE_OF[stage], **seed,
                    "truncated": False, "reason": None}
        else:
            reason = ("该阶段正在执行：轨迹在阶段完成后写入状态存储，阶段结束再打开这里即可看到；"
                      "当下想跟进工具调用可以走「任务记录」页。"
                      if current == stage else "该阶段尚未开始。")
            item = {"stage": stage, "role": ROLE_OF[stage], "input": None, "input_from": None,
                    "output": None, "tool_calls": [], "truncated": False, "reason": reason}
        items.append(item)
    return {
        "workflow_id": workflow_id,
        "mode": "static",
        "task": "对比三种向量数据库的检索性能",
        "availability": "available",
        "reason": None,
        "items": items,
    }


def metrics_for(workflow_id: str) -> list:
    """用量采样（§5.5）。

    种子照**真实标签口径**给：后端 `_labels` 走上下文，标签集是 `role` / `stage` / `model`，
    **没有 `agent_id`**。种子要是打了 `agent_id`，预览页就会显示得比真实环境还好看，
    「Token 有没有分到 Agent 头上」这个真问题在预览里反而看不出来。
    """

    rows = (
        ("collector", "collect", 817, 66, 883),
        ("analyst", "analyze", 852, 279, 1131),
        ("reporter", "report", 1045, 511, 1556),
    )
    items = []
    for role, stage, prompt, completion, total in rows:
        for name, value in (
            ("input_tokens", prompt),
            ("output_tokens", completion),
            ("total_tokens", total),
        ):
            items.append({
                "id": len(items) + 1,
                "metric_name": name,
                "value": float(value),
                "labels": {"workflow_id": workflow_id, "role": role, "stage": stage},
                "recorded_at": iso(-120),
            })
    items.append({
        "id": len(items) + 1,
        "metric_name": "stage_duration_ms",
        "value": 7179.5,
        "labels": {"workflow_id": workflow_id, "role": "collector", "stage": "collect"},
        "recorded_at": iso(-110),
    })
    items.append({
        "id": len(items) + 1,
        "metric_name": "workflow_runs",
        "value": 1.0,
        "labels": {"workflow_id": workflow_id, "status": "completed"},
        "recorded_at": iso(-100),
    })
    return items


def session_workflows(session_id: str) -> dict:
    """`GET /api/v1/sessions/{id}/workflows` 的种子（§5.18）。

    这个列表存在的意义是「对话编号」，所以要能看出**不止一次对话**：真实 workflow
    之外补一条更早的已完结对话，编号按下标 + 1，与真实接口同口径（升序）。
    """

    rows = sorted(
        (w for w in STATE["workflows"].values() if w.get("session_id") == session_id),
        key=lambda w: w.get("created_at") or "",
    )
    if rows:
        earlier = dict(rows[0])
        earlier.update({
            "id": f"{rows[0]['id']}-earlier",
            "status": "completed",
            "current_step": None,
            "created_at": iso(-1800),
            "updated_at": iso(-1740),
            "completed_at": iso(-1740),
            "checkpoint": {
                "status": "completed",
                "current_step": None,
                "completed_steps": ["collect", "analyze", "report"],
            },
        })
        rows = [earlier, *rows]
    return {"items": rows, "total": len(rows)}


# ------------------------------------------------------------------ routing

# 沙箱状态（§5.15）：按**真实部署**的样子给种子——后端容器没挂 docker.sock，
# 所以这里是「不可用 + 原因」，而不是一个好看的绿灯。
SANDBOX_STATUS = {
    "backend": "docker",
    "image": "python:3.12-slim",
    "available": False,
    "reason": (
        "Docker 守护进程不可达；常见原因是 backend 容器未挂载 /var/run/docker.sock "
        "或当前用户无权访问该套接字"
    ),
    "limits": {
        "timeout_seconds": 15,
        "memory_limit": "256m",
        "cpu_limit": 0.5,
        "pids_limit": 64,
        "network_enabled": False,
        "output_limit_chars": 4000,
        "max_code_chars": 20000,
    },
}

ROUTES = [
    ("GET", r"^/api/v1/agents$", lambda m, b: {"items": AGENTS}),
    ("GET", r"^/api/v1/sessions$", lambda m, b: list_sessions_summary()),
    ("POST", r"^/api/v1/sessions$", lambda m, b: new_session()),
    ("GET", r"^/api/v1/sessions/(?P<sid>[^/]+)$",
     lambda m, b: STATE["sessions"].get(m.group("sid")) or new_session()),
    ("GET", r"^/api/v1/sessions/(?P<sid>[^/]+)/messages$",
     lambda m, b: {"items": STATE["messages"].get(m.group("sid"), [])}),
    ("GET", r"^/api/v1/sessions/(?P<sid>[^/]+)/workflows$",
     lambda m, b: session_workflows(m.group("sid"))),
    ("POST", r"^/api/v1/sessions/(?P<sid>[^/]+)/messages$", None),  # handled below
    ("POST", r"^/api/v1/sessions/(?P<sid>[^/]+)/(pause|resume)$",
     lambda m, b: {"session": STATE["sessions"].get(m.group("sid"), {}),
                   "workflow": next((w for w in STATE["workflows"].values()
                                     if w["session_id"] == m.group("sid")), None)}),
    ("GET", r"^/api/v1/workflows/(?P<wid>[^/]+)$", lambda m, b: STATE["workflows"].get(m.group("wid"))),
    ("GET", r"^/api/v1/workflows/(?P<wid>[^/]+)/tool-calls$",
     lambda m, b: page(STATE["tool_calls"].get(m.group("wid"), []))),
    ("GET", r"^/api/v1/workflows/(?P<wid>[^/]+)/stages$",
     lambda m, b: stage_traces(m.group("wid"))),
    ("GET", r"^/api/v1/metrics$", lambda m, b: page(metrics_for("demo"))),
    ("GET", r"^/api/v1/providers$", lambda m, b: {"items": PROVIDERS}),
    ("GET", r"^/api/v1/tools$", lambda m, b: page(MCP_TOOLS)),
    ("GET", r"^/api/v1/config/provider$", lambda m, b: PROVIDER_CONFIG),
    ("PUT", r"^/api/v1/config/provider$", lambda m, b: PROVIDER_CONFIG),
    ("GET", r"^/api/v1/config/provider-presets$", lambda m, b: PRESETS),
    ("GET", r"^/api/v1/config/sandbox$", lambda m, b: SANDBOX_STATUS),
    ("GET", r"^/api/v1/config/providers$", lambda m, b: {"items": PROVIDERS, "total": len(PROVIDERS)}),
    ("GET", r"^/api/v1/config/providers/(?P<pid>[^/]+)$", None),  # handled below
    ("GET", r"^/api/v1/config/models$",
     lambda m, b: {"items": MODELS, "total": len(MODELS)}),
    ("GET", r"^/api/v1/config/mcp/servers$",
     lambda m, b: {"items": MCP_SERVERS, "total": len(MCP_SERVERS)}),
    ("GET", r"^/api/v1/config/mcp/tools$",
     lambda m, b: {"items": MCP_TOOLS, "total": len(MCP_TOOLS),
                   "servers": [{k: s[k] for k in
                                ("id", "name", "transport", "enabled", "tool_count", "discovered_at")}
                               for s in MCP_SERVERS]}),
    ("GET", r"^/api/v1/config/agents$",
     lambda m, b: {"items": AGENTS,
                   "available_models": [{k: mm[k] for k in ("id", "provider_id", "model", "name", "enabled")}
                                        for mm in MODELS if mm["enabled"]]}),
]


def page(items: list) -> dict:
    return {"items": items, "page": 1, "page_size": 20, "total": len(items),
            "availability": "available"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console readable
        pass

    def _json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        path = self.path.split("?")[0]
        body = b""
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)

        # submit a message -> create a workflow so the records page has content
        mo = re.match(r"^/api/v1/sessions/(?P<sid>[^/]+)/messages$", path)
        if method == "POST" and mo:
            sid = mo.group("sid")
            payload = json.loads(body or b"{}")
            content = payload.get("content", "")
            msg = {"id": uid("m"), "session_id": sid, "role": "user", "content": content,
                   "agent_run_id": None, "status": "accepted", "created_at": iso()}
            STATE["messages"].setdefault(sid, []).append(msg)
            workflow = new_workflow(sid, completed=2, status="running")
            self._json({"message_id": msg["id"], "session_id": sid,
                        "agent_run_id": workflow["agent_run_id"],
                        "workflow_id": workflow["id"], "status": "accepted"})
            return

        # provider detail with embedded models
        mo = re.match(r"^/api/v1/config/providers/(?P<pid>[^/]+)$", path)
        if method == "GET" and mo:
            pid = mo.group("pid")
            base = next((p for p in PROVIDERS if p["id"] == pid), None)
            if base is None:
                self._json({"code": "NOT_FOUND", "message": f"未登记的 Provider：{pid}"}, 404)
                return
            self._json({**base, "models": [mm for mm in MODELS if mm["provider_id"] == pid]})
            return

        for route_method, pattern, fn in ROUTES:
            if route_method != method or fn is None:
                continue
            mo = re.match(pattern, path)
            if mo:
                result = fn(mo, body)
                if result is None:
                    self._json({"code": "NOT_FOUND", "message": f"没有这个工作流：{path}"}, 404)
                else:
                    self._json(result)
                return
        self._json({"code": "NOT_FOUND", "message": f"stub 未实现：{method} {path}"}, 404)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    # seed one session so the app has something on first load
    s = new_session()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("stub API listening on http://127.0.0.1:8000  (session %s)" % s["id"], flush=True)
    server.serve_forever()
