# ADR-024: 附件原件一律留档（已发送附件可下载原件）

状态：已接受

## 背景

ADR-021 决定「只有图片保留原始字节」：图片要转 base64 进模型请求，文本与文档在上传时就把正文
抽进 `text_content`，**原始字节用完即弃**，理由是省库体积。该 ADR 同时把这个代价写进了
「未解决」一节，`doc/roadmap.md` §「未做」表也记着一条：

> 「已发送文档的原件下载」（当前只有图片留字节，§5.16.2）—— **未做**，需先改存储策略。

用户本轮授权「可以改策略」。触发它的是一个很直白的体验问题：**用户在历史消息里点开一个附件，
期望拿到的是他当初传的那份文件，而不是我们抽取出来的纯文本**。而按旧策略，`GET /attachments/
{id}/content` 对文本与文档一律 404，界面上那个条目除了看一眼文件名之外什么也做不了。

## 决策

**`data` 对所有类型留档；抽取出来的正文另存 `text_content`。**

1. `prepare_upload` 无条件写 `data`（含解析失败的附件——**读不出正文不等于不该能下载它**，
   扫描版 PDF 恰恰是用户最想拿回原件的一类）。
2. `text_content` 的语义不变：它只服务提示词，执行阶段仍读它，不再重新解析。
3. `GET /api/v1/attachments/{id}/content` 对所有类型回**原件**，按类型选处置方式：
   图片 `inline`（气泡里的 `<img>` 与「查看原图」要能直接渲染），其余 `attachment`
   （txt/docx/xlsx 在浏览器里没有渲染器，`inline` 只会开出一个空白页）。
4. 响应体新增 `has_original`，界面据此决定要不要给「打开原件」入口。
   它只对**旧策略之前落库的行**为 `false`——不给一个必然 404 的链接，是这个项目反复在修的那类病。

## 代价（如实记录）

- **库体积**：单文件 5 MB、单条消息 4 个，最坏 20 MB/条落在 PostgreSQL 的 `bytea`。
  限额本来就在（`app/attachments/spec.py`），所以增长有界；要再省只能换对象存储。
- **列表路径会读到不要的东西**（**已在 D9-10 收尾修掉**）：`list_attachments_for_messages`
  当时用 `select(AttachmentRecord)` 取实体，SQLAlchemy 默认加载全部列，`data` / `text_content`
  在列表接口里被读出来又丢掉。这是 ADR-021 时期就有的行为。现在改为
  `_attachment_meta_columns()` 显式列元信息，并用 `data IS NOT NULL` 在**库侧**算
  `has_original`——列表路径再也不读 `data` 列。回归由
  `tests/unit/test_attachment_readback_columns.py` 编译语句后断言选中列里没有
  `data` / `text_content` 钉住。

## 备选方案

- **维持原策略，只把 404 的文案改清楚**。否决：用户要的是文件本身，不是更礼貌的 404。
- **原件放文件系统 / 对象存储，库里只留路径**。否决：要引入卷挂载、孤儿清理与备份策略，
  而 ADR-021 选「放数据库」正是为了用 `ON DELETE CASCADE` 一行解决「删会话就该一起没」。
  为一个演示规模的项目加这套东西不划算，但**这是规模上来之后该走的路**。
- **原件放 Redis**。否决：Redis 在本项目里是 `provider:config` 的读镜像，不是业务数据存储；
  大对象还会顶掉内存。
- **只给文档留档、图片继续留**。无意义，两者体积同源。

## 影响

- `app/attachments/prepare.py`（无条件留档）、`app/core/checkpoint.py`（`data` 列注释、
  `_attachment_meta` 增 `has_original`）、`app/api/store.py`（内存实现同口径）、
  `app/api/main.py`（`AttachmentResponse.has_original`、下载端点的处置方式与文案）。
- 前端：`types/api.ts` 的 `Attachment` 增 `has_original`；`workspace/AttachmentList.tsx` 把
  气泡条目本身变成「打开原件」的链接（图片不带 `download`，其余带下载文件名），
  `styles.css` 补 `a.message-attachment`（否则退化成蓝字下划线）。
- 测试：`tests/unit/test_attachments.py` 把「丢弃字节」改成「保留字节」，
  `tests/integration/test_attachments_api.py` 新增「非图片与失败附件都能下载原件」；
  预览种子加一条 `has_original=false` 的历史行，jsdom 自检钉住「有原件的才是链接」。
- **实机证据**（重建 backend 后实测）：文本附件 `has_original=true`、下载 44 字节与上传**逐字节
  一致**、`content-type: text/markdown`、`content-disposition: attachment`；解析失败的 PDF
  同样能下载回 `%PDF-1.4` 开头的 30 字节原件；图片 31220 字节下载一致且为 `inline`。
