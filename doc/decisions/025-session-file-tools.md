# ADR-025: 会话级文件工具（Agent 按需读回本次会话的附件）

状态：已接受

## 背景

「Agent 能不能自己读文件」被反复当成一件事，其实是**三件**：

1. **模型能看见你传的文件**：附件随消息进提示词。ADR-021 已落地。
2. **模型能按需再去读一份**：用户在第 7 轮问"上一份预算表里差旅那栏是多少"，模型应当能回去
   打开那份 xlsx，而不是只有"当初被塞进上下文的那一版"。本轮之前**做不到**。
3. **沙箱里的代码能打开宿主文件**：`code_execution` 里 `open()` 能读到平台的文件系统。
   本轮之前做不到，**本轮之后仍然做不到，而且是刻意不做**。

第 2 条缺的不只是一个工具。ADR-012 时定下的沙箱策略禁止 `open` 与 `import os`（实测：
`open()` → `SandboxViolation: 禁止调用危险内建: open`），`deploy/compose.yaml` 也没有给沙箱
挂任何宿主卷；内置工具目前只有 `calculator` / `web_search` / `sql_query` / `code_execution`
四个（`app/mcp/registry.py`），grep `file|read|write|fs` 零命中。所以第 2 条是**四缺**：
缺工具、策略禁打开、无挂载卷、沙箱在部分环境不可用（ADR-023 已修最后一条）。

## 决策

**新增两个会话级只读工具 `list_session_files` / `read_session_file`，数据来自 `attachments`
表；沙箱策略、沙箱挂载、内置工具静态目录一概不动。**

### 1. 读附件表，而不是给沙箱挂卷

沙箱已经拿到宿主 Docker 的控制权（ADR-023：为让沙箱在部署环境可用，挂了
`/var/run/docker.sock`）。在这个前提下再给它挂一个宿主目录，等于把「能起越权容器」
升级成「能起越权容器**并直接读到平台的数据库文件与配置**」。风险是叠加的，收益只是
"换成用 `open()` 读"，没有必须做的理由。

附件表这条路不碰沙箱：不放宽 `SANDBOX_*` 策略、不挂卷、不新增镜像，还天然继承 ADR-021
的限额（单文件 5 MB、单条消息 4 份、单会话条目数由 `list_attachment` 分页）。代价是它只能读
**用户上传过的东西**——它本来也就该是这个范围。

### 2. 工具就地拼进本次执行的注册表，不进静态目录

`build_tool_registry()` 的结果按 ADR-009 在**进程级**缓存，而这两个工具的作用域是**一次执行**
（`session_id` 绑定的那次）。所以：

- `SessionFileRegistry`（`app/mcp/registry.py`）是装饰器：`list_tools()` 追加两项，
  `call()` 按名字分派到它们、否则透传内层注册表；
- `session_scoped_registry(registry, session_id)`（`app/orchestration/tools.py`）在每个阶段
  真正执行前包一层，`advance_pipeline_stage` 传入 `session_id`；
- **`GET /api/v1/tools` 不列这两项**。那份目录（`doc/api.md` §5.3）描述的是进程级注册表，
  把会话级工具算进去，前端「工具与配置」页会显示两个"随时可调"的开关——它们既关不掉、
  也不在无会话的执行里存在。**不给假开关**是这个项目反复在修的毛病。

`TOOL_SESSION_FILES_ENABLED=false` 时不挂（退回"只有提示词里的附件"），
`session_file_max_chars` / `session_file_list_limit` 控制单次读取与清单上限。

### 3. 只读，且不把原件字节交给模型

两个工具能做的是「列清单」和「读正文」，没有写入、没有删除、没有列目录树。返回的载荷
**不含 `data`（原件字节）**：`read_attachment_text` 只取 `text_content` 与元信息。
让模型自己拿字节去解析，等于把 ADR-021 已经做对的那层抽取（分类、限额、失败可诊断）
在提示词里重做一遍，而且是不可控的一遍。

### 4. 名称解析要"宁缺勿猜"

`_match_file` 的匹配顺序是 **附件 id → 完整文件名（不区分大小写）→ 唯一子串**。
子串命中多于一条时**拒绝**（`ToolExecutionError`，消息里列出本次会话的全部文件名）。

用户会说"读那份发票"，而 `invoice.pdf` 与 `invoice-2026.pdf` 同时存在时，猜哪一个都是错的：
猜错的代价是模型拿着另一份文件的内容一本正经地回答问题，用户看不出哪里错了。让模型
带着候选清单再问一次，是最便宜的正确的失败。

### 5. 无正文也要说清楚为什么

三种"读不到"分别有各自的文案，而不是统一报错：

- **图片**：`readable=false`（`_file_summary` 按类型算，不只看 `status=ready`），
  读取时明确告知"没有文本可读，它的内容已随消息作为图像交给你了"——否则模型会反复尝试，
  或者在回答里承认"我打不开图片"，而图明明就在它的上下文里；
- **解析失败**（扫描版 PDF 等）：把 `extract` 的失败原因原样带出（例如"扫描件没有文本层"、
  "字体码冲突"），并说明原件保留、用户可下载；
- **超过 `session_file_max_chars`**：截断并标注 `truncated=true`，让模型知道自己没看到全文。

## 备选方案

- **给沙箱挂一个只读的附件目录，让 `code_execution` 用 `open()` 读**。否决：见「决策 1」。
  它是唯一能让"沙箱里的代码处理文件"成立的方案，但当前沙箱已持有宿主 Docker 控制权，
  这一步的边际风险远大于收益。若将来 ADR-023 的取舍被推翻（沙箱回到无套接字形态），
  这条方案应当被重新拿出来评估。
- **放宽策略层，只允许 `open()` 读白名单路径**。否决：策略层是**静态拒绝清单**
  （禁内建名、禁模块名），它没有"路径"这个概念，要做就得引入路径白名单解析——而沙箱里
  跑的是模型生成的代码，绕过路径检查的手法（软链、`/proc/self/cwd`、`os.open` 变体）
  远多于静态名检查。要放开的不是一个开关，是一整套容器内文件系统沙箱。
- **把会话里所有附件的正文一次性全塞进提示词**。否决：这是现状的放大版而不是改进。
  它让"第 7 轮还想看第 2 轮的文件"确实能用，代价是每条消息都背上全部附件正文，
  与 `session_file_max_chars` 的初衷（只取需要的那一份）正好相反。
- **做成 MCP 工具而不是内置工具**。否决：MCP 侧的工具是**进程级**注册的，而这两个工具
  天然带 `session_id`；把它按会话注册进 MCP 会污染 MCP 的连接与缓存语义。
  它们和 `code_execution` 一样属于内置工具，只是作用域不同。
- **让模型直接拿 `data` 原件自己解析**。否决：见「决策 3」。

## 代价（如实记录）

- **每次执行多一次附件表查询**：`list_session_files` 一次、`read_session_file` 每次一次。
  查询已按 ADR-024 的收尾改成**不读 `data` 列**（`_attachment_meta_columns` 里用
  `data IS NOT NULL` 出一个 `has_original`），所以是按元信息量级读的，不是按字节量级。
- **工具输出会顶上下文**：`session_file_max_chars` 默认 20000 字符，与附件注入上限同量级。
  调大它就是在拿上下文换信息，没有免费的午餐。
- **前端「工具与配置」页看不到这两个工具**。这是有意的（见「决策 2」），
  但它们会出现在任务记录的工具调用链里——**调用过的工具必须留痕**，否则审计不完整。
- **只覆盖用户上传过的文件**。Agent 仍然无法读平台自己的源码、日志或数据目录，
  这是范围边界而不是缺陷。

## 影响

- 新增 `app/tools/session_files.py`（两个工具 + `FileSource` 协议 + `CheckpointFileSource`）；
- `app/tools/config.py` 增 `session_files_enabled` / `session_file_max_chars` /
  `session_file_list_limit`（环境变量 `TOOL_SESSION_FILES_ENABLED` 等）；
- `app/mcp/registry.py` 增 `SessionFileRegistry` / `with_session_files`；
- `app/orchestration/tools.py` 增 `session_scoped_registry`；
- `app/workflows/pipeline.py` 的 `advance_pipeline_stage` 增 `session_id` 参数；
- `app/attachments/prepare.py` 增 `list_session_attachments` / `read_attachment_text`
  （**不返回 `data`**），`app/attachments/__init__.py` 转出；
- 测试：`tests/unit/test_session_files_tools.py`（工具行为、注册表装饰、接线到
  `advance_pipeline_stage`）；`tests/e2e/test_pipeline_e2e.py` 的真实工具断言里加上这两个名字，
  证明它们在**执行时**确实被绑定，而不是只在单元测试里存在。
