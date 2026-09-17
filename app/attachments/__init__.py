"""附件与多模态输入（ADR-021）。

模块分工：

- `spec`：类型白名单、大小与数量限额、按「怎么被模型消费」分类；
- `extract`：把字节解析成正文（文本类 / docx / xlsx / pdf）或图片载荷；
- `prompt`：把附件拼进 `HumanMessage` 的 content（文本内联 + 图片内容块）；
- `prepare`：校验 + 解析 + 行 ↔ 载荷转换（**不含持久化**，持久化走 `api_store` 接缝）。

上层（`app/api`、`app/workflows`、`app/orchestration`）只 import 本模块，
不直接碰 `checkpoint` 里的附件行，也不重复实现限额与分类判断。
"""

from app.attachments.prepare import (
    AttachmentRejected,
    load_payloads,
    prepare_upload,
    to_payload,
)
from app.attachments.prompt import (
    AttachmentPayload,
    attachment_manifest,
    build_human_content,
)
from app.attachments.spec import (
    MAX_FILES_PER_MESSAGE,
    MAX_FILE_BYTES,
    MAX_TEXT_CHARS_PER_FILE,
    MAX_TEXT_CHARS_TOTAL,
    SUPPORTED_EXTENSIONS,
    AttachmentKind,
    AttachmentStatus,
    accept_attribute,
    classify,
    describe_limits,
)

__all__ = [
    "MAX_FILES_PER_MESSAGE",
    "MAX_FILE_BYTES",
    "MAX_TEXT_CHARS_PER_FILE",
    "MAX_TEXT_CHARS_TOTAL",
    "SUPPORTED_EXTENSIONS",
    "AttachmentKind",
    "AttachmentPayload",
    "AttachmentRejected",
    "AttachmentStatus",
    "accept_attribute",
    "attachment_manifest",
    "build_human_content",
    "classify",
    "describe_limits",
    "load_payloads",
    "prepare_upload",
    "to_payload",
]
