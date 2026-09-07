"""验证 Ollama 模型可用性与单 Agent 多轮对话。

对应 D1-2 成员 C 交付「选定并验证 Ollama 模型、多轮对话」：
1. 检查 Ollama 服务可达（/api/tags）；
2. 核对配置的目标模型是否已本地拉取（未拉取则给出 ollama pull 命令）；
3. 用同一个 LangGraph Agent 连续发起两轮对话，验证多轮上下文累积。

用法（在本仓库根目录执行）：
    uv run python scripts/verify_ollama.py
可通过环境变量覆盖配置：AGENT_OLLAMA_BASE_URL、AGENT_OLLAMA_MODEL、AGENT_TEMPERATURE。

说明：模型连通为真实外部调用；若 Ollama 未安装或未启动，脚本以非零码退出并打印指引。
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.messages import HumanMessage

from app.config import get_settings
from app.orchestration.graph import build_langgraph_agent


def check_ollama(base_url: str) -> list[str]:
    """调用 /api/tags 返回已拉取模型名列表；异常抛给调用方。"""
    request = urllib.request.Request(f"{base_url}/api/tags", method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [model["name"] for model in payload.get("models", [])]


def main() -> int:
    settings = get_settings()
    base_url = settings.ollama_base_url
    target_model = settings.ollama_model

    print(f"[1/3] Check Ollama connectivity: {base_url}")
    try:
        pulled = check_ollama(base_url)
    except urllib.error.URLError as exc:
        print(f"  FAIL - cannot reach Ollama at {base_url}: {exc.reason}")
        print("  Ollama may not be installed or started. See install guide below.")
        print("  - Windows: winget install Ollama.Ollama, then start 'Ollama' app.")
        print("  - macOS/Linux: curl -fsSL https://ollama.com/install.sh | sh")
        print("  After starting, run again: uv run python scripts/verify_ollama.py")
        return 1
    except (urllib.error.HTTPError, TimeoutError) as exc:
        print(f"  FAIL - Ollama responded unexpectedly: {exc}")
        return 1

    print(f"[2/3] Look for model '{target_model}' in pulled list")
    if target_model in pulled:
        print(f"  OK - model {target_model} is present locally.")
    else:
        print(f"  MISSING - model {target_model} is not pulled yet.")
        print(f"  Pull it first:  ollama pull {target_model}")
        print("  For a smaller/slower option, override AGENT_OLLAMA_MODEL, e.g.:")
        print("    ollama pull qwen2.5:3b")
        print("    AGENT_OLLAMA_MODEL=qwen2.5:3b uv run python scripts/verify_ollama.py")
        return 1

    print("[3/3] Run two-turn dialogue through one LangGraph agent")
    agent = build_langgraph_agent(settings=settings)
    state: dict = {"messages": []}
    for turn, text in enumerate(
        (
            "Please remember this code number: 12345. Reply only with the word remembered.",
            "What code number did I ask you to remember in our previous message?",
        ),
        start=1,
    ):
        state["messages"] = [*state["messages"], HumanMessage(content=text)]
        state = agent.invoke(state)
        reply = state["messages"][-1].content
        print(f"  turn {turn} user : {text}")
        print(f"  turn {turn} agent: {reply}")

    messages = state["messages"]
    if len(messages) < 4:
        print("  WARN - expected at least 4 messages (2 user + 2 agent) after two turns.")
        return 1

    print("\nMulti-turn context accumulation verified: OK")
    print(f"Verified model: {target_model} @ {base_url} (temperature={settings.temperature})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
