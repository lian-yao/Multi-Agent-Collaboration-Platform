# 测试策略

## 测试层级

| 层级 | 范围 | 目录 |
| --- | --- | --- |
| 单元测试 | 图节点、工具函数、模型工厂 | `tests/unit` |
| 集成测试 | Dapr、Redis、PostgreSQL、MCP | `tests/integration` |
| 端到端测试 | REST API + Web UI + Dapr | `tests/e2e` |

## 关键场景

- 单 Agent 问答：用户输入到结构化输出的完整链路。
- 多 Agent 协作：信息收集、数据分析、报告生成三步流水线。
- 故障恢复：执行中停止 Agent 服务，重启后从断点继续。
- 工具调用：MCP 工具发现、选择、执行、超时与重试。
- 记忆管理：多轮上下文继承和跨会话长期记忆。

## 性能目标

- Workflow 恢复时间小于 5 秒。
- 支持 10 个并发会话稳定运行。
- Prometheus 可监控 Token 消耗和工具调用成功率。

## 命令

```bash
uv run pytest
```

浏览器与前端验证在 Web UI 阶段补充 Playwright 或等价方案。
