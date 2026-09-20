"""Dapr Agents 1.0.6 最小可运行 POC（ADR-020 的「未完成项」证据脚本）。

目的：证明 `dapr-agents` 能在本地 Dapr 运行时上注册并跑完一次 Durable Workflow，
而不是只躺在依赖清单里。不触碰生产链路：独立进程、独立 app-id。

用法（宿主机，工作区根目录）：

```powershell
dapr run --app-id macp-agents-poc --dapr-http-port 3510 --dapr-grpc-port 50001 `
    -- uv run python scripts/poc_dapr_agents.py
```

组件用 `dapr init` 生成的默认目录（`~/.dapr/components`：`statestore` 带
`actorStateStore: "true"`、`pubsub`，两者都指向 `dapr_redis` 的 `localhost:6379`），
因此**不要求 compose 栈在跑**——这是本 POC 相对生产部署刻意缩小的前提，
`deploy/dapr/components-local/` 那套指向 compose 的 Redis（6380），栈没起时组件会初始化失败。

端口：HTTP 用 3510，避开 compose `dapr-sidecar` 占用的 3500；gRPC 用 dapr run 默认 50001
（宿主机上没有其他 sidecar 监听）。

预期输出：`POC_OK=True`、`POC_INSTANCE_ID=...`、`POC_OUTPUT=...`，退出码 0。
没有 sidecar 时 `DurableAgent(...)` 构造阶段就会 gRPC UNAVAILABLE——
这正是本 POC 必须跑在 `dapr run` 之下的原因（见 ADR-020）。

**POC 过程中发现的上游不一致（dapr-agents 1.0.6）**：`DurableAgent` 把活动注册成
agent 前缀名（`dapr.agents.<agent>.<method>`），标准 LLM 分支也按前缀名调用
（`ctx.call_activity(self._activity_name(self.call_llm), ...)`），但 **executor 分支**
（`agents/durable.py` 第 675-679 行）传的是裸绑定方法 `self.run_executor`，
运行时据此查找未加前缀的 `run_executor`，于是报
`Activity function named 'run_executor' was not registered`、工作流终态 FAILED。
本脚本按「显式注册一个未加前缀的别名」绕过该问题，使 POC 能跑完；上游修复后
这行可删（保留也无害：只是多注册一个同名活动）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from typing import Any

from dapr.ext.workflow import DaprWorkflowClient, WorkflowRuntime
from dapr_agents import DurableAgent, EchoAgentExecutor

AGENT_NAME = "MacpPocAgent"
TASK = "POC：用 echo 执行器回显这条任务，证明 Dapr Agents 工作流跑通。"


def _build_agent() -> DurableAgent:
    """构造一个不需要模型凭据的 DurableAgent（EchoAgentExecutor 零依赖）。"""

    return DurableAgent(
        name=AGENT_NAME,
        role="POC 验证角色",
        goal="验证 Dapr Agents 1.0.6 在本项目本地 Dapr 运行时上能注册并完成一次工作流",
        instructions=["把收到的任务原样回显，用于证明持久化执行链路已打通。"],
        executor=EchoAgentExecutor(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Dapr Agents 1.0.6 最小可运行 POC")
    parser.add_argument(
        "--inspect",
        metavar="INSTANCE_ID",
        help="只读取指定 Workflow 实例的终态与输出（用于验证状态跨进程持久）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.inspect:
        return _inspect(args.inspect)

    agent = _build_agent()
    runtime = WorkflowRuntime()
    # 与生产链路（app/workflows/worker.py）同一套原语：注册 → 启动 → 调度 → 等终态。
    agent.register(runtime)
    runtime.register_activity(agent.run_executor)  # 见模块 docstring 的上游差异说明
    runtime.start()

    client = DaprWorkflowClient()
    output: Any = None
    instance_id = uuid.uuid4().hex
    try:
        client.schedule_new_workflow(
            workflow=agent.agent_workflow_name,
            input={"task": TASK},
            instance_id=instance_id,
        )
        state = client.wait_for_workflow_completion(
            instance_id,
            timeout_in_seconds=120,
            fetch_payloads=True,
        )
        output = state.serialized_output
        print(f"POC_RUNTIME_STATUS={state.runtime_status}")
    finally:
        try:
            runtime.shutdown()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不该掩盖 POC 结论
            print(f"POC_SHUTDOWN_WARNING={type(exc).__name__}: {exc}")

    if output is None:
        print("POC_OK=False reason=workflow returned no output")
        return 1

    print(f"POC_AGENT={AGENT_NAME}")
    print(f"POC_INSTANCE_ID={instance_id}")
    print(f"POC_TASK={TASK}")
    print("POC_OK=True")
    print(f"POC_OUTPUT={output}")
    return 0


def _inspect(instance_id: str) -> int:
    """读一个已完成实例的终态与输出：证明状态存在 Dapr 状态存储里、跨进程可读。"""

    client = DaprWorkflowClient()
    state = client.get_workflow_state(instance_id, fetch_payloads=True)
    if state is None:
        print(f"POC_INSPECT_OK=False instance={instance_id} reason=not found")
        return 1
    print(f"POC_INSPECT_INSTANCE={instance_id}")
    print(f"POC_INSPECT_STATUS={state.runtime_status}")
    print(f"POC_INSPECT_OUTPUT={state.serialized_output}")
    print("POC_INSPECT_OK=True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
