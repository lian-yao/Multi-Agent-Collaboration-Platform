# ADR-021: 多模态附件（登记 → 抽取 → 只在根步骤注入）

状态：已接受（2026-09-17）

## 背景

用户要求「聊天框需要能够引入多模态文件，便于图片理解，文档理解等等」。核实当前能力，结论是
**这条通路整段不存在**，且不是「加个上传接口」就能补上：

- `POST /api/v1/sessions/{id}/messages` 只收 `content`，请求体里没有任何附件位置；
- `MessageRequest.content` 当时带 `min_length=1`，所以「只发一张截图、不打字」这个多模态里
  **最常见的用法**在契约层就表达不出来；
- 内置工具只有 `calculator / web_search / sql_query / code_execution`（`app/mcp/registry.py`），
  全仓 grep `file|read|write|fs` 零命中 → Agent **没有任何读文件的工具**；
- 沙箱的策略层显式禁止 `open()` 与 `import os`（实测 `SandboxViolation: 禁止调用危险内建: open`），
  且部署环境里 `build_sandbox().available()` 恒为 `False`（ADR-020），`deploy/compose.yaml`
  也没给 backend 挂任何业务卷。

四件事叠起来（没有工具 + 禁止 `open` + 沙箱不可用 + 没有挂载卷），「让 Agent 自己读文件」在当且
仅当四件全部解决之后才成立。**要让模型看见一个文件，唯一现在就能跑通的通路，是把内容随消息一起
送进去。**

同时有两个既有约束决定了实现形态，不能在实现里绕开：

1. **Dapr 的 gRPC 默认限制 4 MB**：附件内容若跟着消息在编排链路里逐级传递，一张稍大的截图就会把
   整条链路压死；
2. 沙箱不可用（同上），所以做不到「把文件放进沙箱让代码读」。

## 决策

### 1. 附件是**一等资源**：先登记、后引用

新增 `attachments` 表与三个接口（`doc/api.md` §5.16）。上传（`POST /api/v1/attachments`）返回
附件 id，发消息时只提交 `attachment_ids`，**内容不进消息体、不进编排链路**——这是对 4 MB 上限的
正面回应：链路里流动的永远是 id。

归属在发消息时用 `link_attachments` 回填，且**只认 `message_id IS NULL` 的行**：已归属别的消息的
附件不会被改挂（重复提交同一个 id 只会进 `unattached_attachment_ids`，是部分失败而不是重挂）。

### 2. 类型白名单 + 三层限额，两侧口径必须一致

`app/attachments/spec.py` 与 `frontend/src/workspace/attachments.ts` 各持一份口径：
5 MB / 单条消息 4 个 / 单文件 2 万字符 / 合计 4 万字符。

前端那份**只负责即时反馈**（选文件时就能知道哪个太大、哪个不支持，省一次往返），准入判据始终在
服务端。两处口径一散就会「前端放行、后端 400」或者反过来「前端拦掉后端本来收得下的文件」，
所以 `workspace-smoke` 里有断言钉着扩展名表、5 MB 与 4 个这三个数。

### 3. 抽取在服务端做，**用标准库，不引第三方依赖**

- 文本 / 代码：文本解码，带 `gb18030` 回退与「可打印率」闸门，用来把二进制挡在门外；
- `docx` / `xlsx`：`zipfile` + 正则取 XML（docx 抽段落，xlsx 抽单元格拼 TSV）；
- `pdf`：解压内容流取 `(...)` 字面量，**带质量闸门**。

不引 `pypdf` / `python-docx` 的理由见「备选方案」。

### 4. 内容只在「接收原始任务的根步骤」注入

静态链路注入 `collect`；动态链路注入 `depends_on` 为空的根步骤（`app/orchestration/dynamic_graph.py`）。
判据是「谁在接收用户的原始任务」，不是「谁是第一个执行的」。

每步都注入会同时犯两个错：重复占用上下文，以及对下游步骤没有新增信息（下游拿到的是上游的产出，
不是原始附件）。

### 5. 图片走 `image_url` content block，文本与文档内联进提示词

`build_human_content()`（`app/attachments/prompt.py`）：**没有图片时返回纯字符串**，有图片时才升级
成 content block 列表。这让「不带图片的既有调用」在链路上一个字节都不变，不会因为这次改动而改变
任何既有行为。

失败项由 `attachment_manifest()` **强制列出**：解析失败的附件必须在提示词里出现，并写明失败原因。

### 6. 「空正文 + 有附件」是合法请求

`content` 去掉 `min_length=1`，改由 `MessageRequest` 的模型校验器保证「`content` 与
`attachment_ids` 不能同时为空」。对应地，前端气泡在 `content` 为空时**不渲染空的正文段落**。

## 备选方案

- **引入 `python-multipart` 走标准 multipart 上传**。否决：它当前只是 `mcp` 的传递依赖
  （不在 `pyproject.toml` 的一等依赖里），为一个上传接口把它提成一等依赖要动 `uv.lock`，
  收益不抵代价。改用 JSON + base64（服务端两种都收）。
- **引入 `pypdf` / `python-docx` 做「正经」解析**。否决：本机与容器都装不了（无公网出口），
  而 `docx`/`xlsx` 本来就是 zip + XML，标准库足够；PDF 用质量闸门明确区分「能读」与「读不了」，
  比引入一个装不上的依赖更接近可用状态。
- **先把沙箱挂上卷、再加 `read_file` 工具，让 Agent 自己读**。暂缓：这正是上面「四缺」里的四件，
  任意一件没解决这个方案就不成立（ADR-020 已确认沙箱在当前部署下不可用）。本 ADR 走的是
  「不依赖沙箱也能让模型看到文件」的那条路，等 C 线把沙箱与文件工具补齐后两者可以并存。
- **把附件内容塞进 `checkpoint` 一路传下去**。否决：撞 4 MB 上限，且违反第 4 条的判断。
- **在 UI 上给 `allowAutoExecution` 之类的开关**。否决：`tool_options.allowAutoExecution`
  是**只存不用**的字段（可写、库里有、回读有，执行链路零消费），给它做界面就是做假开关，
  与 ADR-018 认定的同一类问题。

## 影响

- 新增 `app/attachments/`（`spec` / `extract` / `prompt` / `prepare`）、`attachments` 表
  （`doc/data-model.md` §3.2）、三个接口（`doc/api.md` §5.16）、前端
  `workspace/attachments.ts` + `workspace/AttachmentList.tsx`。
- `MessageRequest` / `MessageAccepted` / `Message` 三个契约各加字段；`content` 取消 `min_length`。
- 编排侧三处入口加 `attachments` 透传：`pipeline_graph._run_role_stage`（静态）、
  `dynamic_graph.run_plan_step`（动态）、`workflows/pipeline.py` 与 `workflows/dynamic.py` 的活动。
- 前端新增：附件按钮、拖拽、粘贴收图（截图直接粘贴比存盘再选少两步）、chip 三态
  （uploading / ready / failed）、气泡附件条目。**失败项一律留在界面上并写出原因**——
  它本来就没进模型上下文，静默丢掉会让用户以为传上了。
- 测试：`tests/unit/test_attachments.py`（39 例）、`tests/integration/test_attachments_api.py`
  （9 例）、`tests/unit/test_dynamic_pipeline.py` 增 3 例（只进根步骤 / 每个根步骤都拿到 /
  静态 `collect` 收到附件），前端 `workspace-smoke` 增附件断言并纳入 `ui-preview.html`。
- **未解决**：PDF 是「尽力而为」——扫描件与 CID 字体编码的 PDF 会被质量闸门判为 `failed`；
  图片理解依赖所选模型支持视觉（前端提示文案已写明这一点，不做臆测）。
  → **已解决（2026-09-20）**：由 **[ADR-027](027-scanned-pdf-page-images.md)** 处理——
  抽不出正文的 PDF 把页面渲成图片走本条 ADR 定下的 `image_url` 通路（`status` 由 `failed`
  改为 `ready` + 一句降级说明），模型不再看到零内容。仍然不做 OCR。
- **已解决（2026-09-17）**：下面这条代价由 **ADR-024** 处理——原件改为所有类型一律留档，
  `/content` 对所有类型可下载（图片 `inline`、其余 `attachment`），并新增 `has_original`
  让界面不给必然 404 的链接。原措辞保留如下：
  <sub>`GET /api/v1/attachments/{id}/content` **只对图片回字节**。文本与文档的原始字节在上传时
  就丢掉了（只留抽取后的正文），因此**界面上无法下载已发送的文档原件**。这是「只有图片留字节」
  的直接代价；若将来需要「下载原件」，要先改存储策略（全部留字节，或有条件留），而不是只加一个
  按钮。</sub>
- 「Agent 自主读文件」（沙箱 + 文件工具）仍属 C 线 `app/sandbox` 的范围，本 ADR 不解决。
