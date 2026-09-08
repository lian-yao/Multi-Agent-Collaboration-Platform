# ADR-005: 会话与长期记忆的数据结构

状态：已接受

## 背景

分工表 D3-4 由角色 C 负责「定义会话与记忆数据结构」（里程碑 M2）。`doc/data-model.md` §4
只给出 Redis key 与类型：会话记忆为 `session:{id}:messages`（List），长期记忆为
`agent:{id}:memory`（Hash/向量记录）。文档未定义两条 key 的内部结构，向量检索在设计文档
`doc/15 AI Native多智能体协作平台.md` 模块4 中被标记为「可选」。落地前需要消除该空白。

## 决策

1. **会话记忆（短期）**：`session:{id}:messages` 为 Redis List，元素为 `SessionMessage`
   的 JSON（`session_id/role/content/id/agent_run_id/status/created_at`）。语义：
   - TTL 7 天；可丢失；仅服务会话上下文读取；
   - PostgreSQL `messages` 表为最终事实源（data-model §4/§5）；
   - `role`/`status` 取值对齐 data-model §3 与 `doc/api.md` §2。
2. **长期记忆**：`agent:{id}:memory` 为 Redis Hash，field=记忆项 key，value=JSON
   `{content, agent_id, updated_at}`（不含 key）。语义：写前先落审计；无 TTL；
   key 相同则覆盖。**本期不引入向量检索**，长期记忆先做结构化偏好记录。
3. **落地边界**：D3-4 仅定义 Pydantic Schema、序列化函数、key 命名与读写接口契约
   （`ConversationMemory` / `LongTermMemory`），不接入真实 IO；真实存储实现由存储层
   （`app/core`，成员 B）待 Dapr State Management / 存储配置就绪后按契约接入。
4. **读写通道**：会话记忆（List）与长期记忆（Hash）均为 Redis 原生结构，
   记忆层内封装，不依赖 Dapr State Management，不触碰 `app/core` 目录边界。

## 影响

- 数据结构的代码事实源为 `app/memory/schemas.py` 与 `app/memory/store.py`；
  `doc/data-model.md` §4.1 同步细化为实现基线。
- 消息与会话字段以 data-model/api.md 契约为准，D 端会话 API 与 B 端存储实现可引用
  `app.memory` 而无需自行重定义。
- 后续引入语义检索（向量库）需新增 ADR 并修订设计文档后再排期，不改变既有 Hash 契约。
