"""附件的装配层：校验 + 解析 + 行 ↔ 载荷转换。

这里**不做持久化**。持久化走 `app.api.store` 的 `api_store` 接缝——
与其它所有业务数据一致（`doc/testing.md` §3.2：接口测试用 `InMemoryApiStore`，
不依赖 PostgreSQL）。附件不算例外：把它直接写到 `checkpoint` 上会让
「上传附件」成为整个接口测试套件里唯一需要真实数据库的路径。

执行阶段读取附件载荷是唯一的例外（`load_payloads`）：那条路径跑在 Dapr 活动里，
本来就在真实进程上，没有替身可用，也不需要替身。**扫描版 PDF 的页面渲染**也挂在
这条路径上（`_expand_scanned_pdf`，ADR-027）——它同样是「执行阶段才算得出来的东西」，
不适合塞进上传接口。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.attachments.extract import extract
from app.attachments.prompt import AttachmentPayload
from app.attachments.render import render_pdf_pages
from app.attachments.spec import (
    MAX_FILE_BYTES,
    AttachmentKind,
    AttachmentStatus,
    classify,
    extension_of,
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
    return {
        "name": safe_name,
        "mime": (mime or "").strip() or (result.image_mime or ""),
        "kind": result.kind.value,
        "status": result.status.value,
        "size_bytes": len(data),
        # 原始字节**一律留档**（ADR-024）。图片要转 base64 进模型请求；文本与文档除了
        # 提示词里用的正文（`text_content`）之外，原件本身也要能下载回来——用户在历史
        # 消息里点开附件，期望拿到的是他当初传的那份文件，而不是我们抽取出的纯文本。
        # 解析失败的附件（扫描版 PDF）同样留档：读不出正文不代表不该能下载它。
        # 代价是库体积，已由 `spec.py` 的单文件 5 MB / 单条消息 4 个硬上限约束。
        "data": data,
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


def _expand_scanned_pdf(payload: AttachmentPayload) -> list[AttachmentPayload]:
    """把「没有正文、但页面能渲成图」的 PDF 展开成逐页图像载荷（ADR-027）。

    只在**执行阶段**做，理由有三条：

    1. 页面图唯一的用途就是送进模型请求。上传时就整份渲出来存着，等于给没人会看第二遍
       的位图占库容，还要为它加一张表；
    2. 原件本来就在 `data` 里（ADR-024），派生结果随时能重来，不存在「丢了就没了」；
    3. 代价有界：一页 A4 约 0.03–0.3 秒、20–50 KB，且**只对抽不出正文的 PDF** 发生——
       有文本层的 PDF 在 `extract_pdf` 那一关就拿到正文了，走不到这里。

    展开后 `id` 沿用父附件：界面上的「1 个附件」不该因为内部拆成 5 张图就变成 5 个，
    `build_human_content` 的附件计数按 id 去重也依赖这一点。
    """

    if payload.kind != AttachmentKind.DOCUMENT or payload.status != AttachmentStatus.READY:
        return [payload]
    if (payload.text or "").strip():
        return [payload]
    if not payload.data or extension_of(payload.name) != "pdf":
        return [payload]

    rendered = render_pdf_pages(payload.data)
    if not rendered.usable():
        # 渲染失败就降级成 failed：上传时那次探测是成功的，但**这一次执行**确实什么都没
        # 拿到，提示词里必须这么说。沿用上传时那句「已改以页面图像提供」就是撒谎——
        # 界面上一切正常、模型却两手空空，正是本项目反复要避免的假信号。
        return [
            replace(
                payload,
                status=AttachmentStatus.FAILED.value,
                error=f"页面转图失败：{rendered.reason or '原因未知'}",
            )
        ]

    return [
        AttachmentPayload(
            id=payload.id,
            name=f"{payload.name} · 第{index}页/共{rendered.total_pages}页",
            kind=AttachmentKind.IMAGE.value,
            status=payload.status,
            mime="image/png",
            data=png,
            text=None,
            # 页码写在名字里就够模型判断「看全了没有」，不再逐页重复父附件那条说明——
            # 同一句话重复 5 遍是白花的 token。
            error=None,
        )
        for index, png in enumerate(rendered.pages, start=1)
    ]


def load_payloads(attachment_ids: list[str]) -> list[AttachmentPayload]:
    """执行阶段用：按传入顺序取附件载荷（含字节与正文）。

    顺序即用户上传顺序——提示词里的附件编号必须和界面上一致，否则用户在界面上
    看到「附件 1 是发票」，模型读到的却是另一份。扫描版 PDF 会在这里被展开成逐页图像
    （ADR-027），展开后的相对顺序仍与上传顺序一致。
    """

    payloads: list[AttachmentPayload] = []
    for row in checkpoint.load_attachment_payloads(attachment_ids):
        payloads.extend(_expand_scanned_pdf(to_payload(row)))
    return payloads


def list_session_attachments(session_id: str) -> list[dict[str, Any]]:
    """会话内全部附件的元数据（不含字节与正文），按上传时间。

    供 Agent 的「自己读文件」工具使用：它们要能先看看这次会话里有哪些文件，
    再决定读哪一份，而不是只能读提示词里已经塞进来的那一份。
    """

    return checkpoint.list_attachments_for_session(session_id)


def read_attachment_text(attachment_id: str) -> dict[str, Any] | None:
    """取一份附件的可读正文（含状态与失败原因）；不存在时返回 None。

    只服务「以文本方式读一份附件」这一个用途，因此**不返回 `data`**：
    一次工具调用的输出会回填给模型，把 5 MB 原件塞进去只会顶爆上下文。
    原件下载走接口层 `GET /attachments/{id}/content`（ADR-024）。
    """

    row = checkpoint.get_attachment_content(attachment_id)
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "status": row["status"],
        "mime": row["mime"],
        "size_bytes": row["size_bytes"],
        "has_original": bool(row.get("has_original")),
        "text": row.get("text_content"),
        "error": row.get("error"),
    }


__all__ = [
    "AttachmentRejected",
    "list_session_attachments",
    "load_payloads",
    "prepare_upload",
    "read_attachment_text",
    "to_payload",
]
