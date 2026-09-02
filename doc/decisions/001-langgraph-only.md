# ADR-001: 编排框架固定为 LangGraph

状态：已接受

## 背景

设计文档原方案同时考虑 LangGraph 与 CrewAI。实际执行时确定先深度实现一个框架。

## 决策

编排框架固定为 LangGraph，不引入 CrewAI。多 Agent 协作通过 LangGraph 多节点图实现。

## 影响

- 减少框架间概念切换成本。
- 聚焦 LangGraph 的图、Checkpoint 与状态管理能力。
- 后续不维护 CrewAI 相关代码与依赖。
