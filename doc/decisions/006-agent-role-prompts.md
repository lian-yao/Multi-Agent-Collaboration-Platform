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

## 修订（2026-09-23）：提示词与软件现状对齐（ADR-037 §4）

三个角色的 system prompt 原先只描述「收集 / 分析 / 报告」这套抽象分工，**完全没提平台真实
给到它们的能力**：会话工作区文件工具、附件、按需调用的 MCP 工具、网页搜索、破坏性动作要
人工审批、路径只能在会话工作区内。模型不知道自己手上有这些，自然也不会用——提示词与软件
脱节，是这次使用者要求「每个 agent 的提示词，使其更符合现在的软件」的直接原因。

重写后的每一段都固定写三件事，缺一不可：

1. **位置**：本次执行的第几步、上游给什么、下游要什么（下游看不到你的过程，只看你写出来的）；
2. **手上真有什么**：具体工具名（`list_work_files` / `read_work_file`，可写档位下另有
   `write_work_file` / `make_work_dir` / `move_work_entry`）、附件、MCP 与网页搜索；
3. **边界与纪律**：文件只能碰会话工作区内的路径；**覆盖与删除是人工审批、不是失败**；
   不臆造来源；不复述上游原文。

规划 Agent 的 prompt 同步补齐（各角色**实际能力** + `instruction` 会直接作为该角色的任务说明 +
任务已被问题改写补全）。这条对齐**有测试兜底**：`tests/unit/test_agent_roles.py` 与
`test_dynamic_pipeline.py` 会检查三段提示词各自提到工作区、文件工具与审批——避免以后有人精简
提示词时又把能力说明删掉。
