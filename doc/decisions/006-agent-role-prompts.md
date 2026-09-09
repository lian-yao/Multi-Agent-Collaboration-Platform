# ADR-006: 角色 Prompt 与示例场景数据的落点与边界

状态：已接受

## 背景

分工表 D5-6（里程碑 M3「多 Agent 协作」）由成员 C 负责「编写角色 Prompt、场景数据」。
多 Agent 流水线的三角色（collector / analyst / reporter）已由
`app/orchestration/pipeline.py` 的 `PipelineStage` 与 `doc/data-model.md` §3 冻结，
但角色系统 Prompt 与演示场景数据尚未落地。

同时，`app/agents` 目录按分工 §1 归成员 A（Agent 角色与团队定义），而角色 Prompt 内容
按分工 §2/§3 由成员 C 编写，存在目录归属与内容职责的重叠，需要先消除该空白。

## 决策

1. **角色 Prompt 落点**：`app/agents/roles.py`。目录归成员 A，内容由成员 C 编写，
   通过文件级隔离（A：图与团队；C：`roles.py`）解决重叠；合并前需 A 确认
   （协作机制 §4「跨目录改动需与归属人确认」）。
2. **Schema**：`RoleDefinition`（`id` / `name` / `model` / `temperature` / `system_prompt`）。
   前四者对齐 `doc/data-model.md` §3 agents 表；`system_prompt` 为应用层静态内容，
   本期不作为数据库列。
3. **场景数据落点**：`examples/scenarios.py`，提供演示场景（用户任务文本 + 三步角色
   目标说明），供 M3 演示与 A/B 接线、D 端 Web 展示复用。
4. **边界（本次）**：仅交付内容层，不把角色 Prompt 接进 `app/workflows` /
   `app/orchestration`（替换 `fake_stage_result` 属于 A 的图 + B 的 Workflow 的 D5-6 交付）。

## 影响

- 角色 Prompt 内容的事实源为 `app/agents/roles.py`；场景数据事实源为
  `examples/scenarios.py`。
- A/B 接线时以 `get_role(role_id)` 取系统 Prompt，不必自行重定义。
- `system_prompt` 若后续需动态配置（PATCH `/config/agents`），需新增 ADR 并修订
  `doc/data-model.md` §3 agents 表。
