"""附件的装配层：校验 + 解析 + 行 ↔ 载荷转换。

这里**不做持久化**。持久化走 `app.api.store` 的 `api_store` 接缝——
与其它所有业务数据一致（`doc/testing.md` §3.2：接口测试用 `InMemoryApiStore`，
不依赖 PostgreSQL）。附件不算例外：把它直接写到 `checkpoint` 上会让
「上传附件」成为整个接口测试套件里唯一需要真实数据库的路径。

执行阶段读取附件载荷是唯一的例外（`load_payloads`）：那条路径跑在 Dapr 活动里，
本来就在真实进程上，没有替身可用，也不需要替身。
"""

from __future__ import annotations

from typing import Any

from app.attachments.extract import extract
from app.attachments.prompt import AttachmentPayload
from app.attachments.spec import (
    MAX_FILE_BYTES,
    AttachmentKind,
    classify,
    sanitize_name,
)
from app.core import checkpoint


class AttachmentRejected(ValueError):
    """上传被拒。`code` 供接口层转成稳定的错误码，`message` 直接给用户看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def prepare_upload(name: str, data: bytes, mime: str = "") -> dict[str, Any]:
    """校验并解析一个附件，返回可直接落库的字段（不含 id / 归属）。

    被拒的两种情况都在这里抛 `AttachmentRejected`：大小超限与类型不受支持。
    **两者都必须在上传时拒**——收下一个永远用不上的附件，等于在界面上给用户
    一个「我传上去了」的假信号，而这正是项目反复踩过的坑（`allowAutoExecution`、
    `decision_agent_id`、沙箱可编辑开关）。
    """

    safe_name = sanitize_name(name)
    if not data:
        raise AttachmentRejected("ATTACHMENT_EMPTY", "附件内容为空。")
    if len(data) > MAX_FILE_BYTES:
        limit_mb = MAX_FILE_BYTES / 1024 / 1024
        raise AttachmentRejected(
            "ATTACHMENT_TOO_LARGE",
            f"附件「{safe_name}」超过单文件上限 {limit_mb:.0f} MB。",
        )

    kind = classify(safe_name)
    if kind is AttachmentKind.UNSUPPORTED:
        raise AttachmentRejected(
            "ATTACHMENT_TYPE_UNSUPPORTED",
            f"附件「{safe_name}」的格式不在支持范围内"
            "（图片 png/jpg/jpeg/webp/gif、常见文本与代码扩展名、以及 pdf/docx/xlsx）。",
        )

    result = extract(safe_name, data)
    is_image = result.kind is AttachmentKind.IMAGE
    return {
        "name": safe_name,
        "mime": (mime or "").strip() or (result.image_mime or ""),
        "kind": result.kind.value,
        "status": result.status.value,
        "size_bytes": len(data),
        # 只有图片留字节（要转 base64 进模型请求）；文本与文档在上传时已抽出正文，
        # 原始字节不再需要——省库体积，也让执行阶段不必再解析一遍。
        "data": data if is_image else None,
        "text_content": result.text,
        "error": result.error,
    }


def to_payload(row: dict[str, Any]) -> AttachmentPayload:
    """数据库行 → 提示词侧载荷。宽容缺失字段：列表接口不返回字节与正文。"""

    return AttachmentPayload(
        id=str(row.get("id") or ""),
        name=str(row.get("name") or "附件"),
        kind=str(row.get("kind") or ""),
        status=str(row.get("status") or ""),
        mime=str(row.get("mime") or ""),
        data=row.get("data"),
        text=row.get("text_content"),
        error=row.get("error"),
    )


def load_payloads(attachment_ids: list[str]) -> list[AttachmentPayload]:
    """执行阶段用：按传入顺序取附件载荷（含字节与正文）。

    顺序即用户上传顺序——提示词里的附件编号必须和界面上一致，否则用户在界面上
    看到「附件 1 是发票」，模型读到的却是另一份。
    """

    return [to_payload(row) for row in checkpoint.load_attachment_payloads(attachment_ids)]


__all__ = ["AttachmentRejected", "load_payloads", "prepare_upload", "to_payload"]
