"""把附件拼成模型输入（提示词侧，不碰存储）。

调用点在**接收原始任务的根步骤**：静态链路的 collector，动态链路里
``depends_on`` 为空的步骤。理由：附件是「用户给的原始材料」，只有拿到原始任务
的那一步才该看到它；下游步骤拿到的是上游产出的正文，再塞一遍附件既没有新增信息，
又会让图片在每次调用里重复计费。多根步骤（动态图的并行起点）会各拿一份，
这是有意的——当前不做去重，重复成本记在 `doc/api.md` §5.16 的边界说明里。
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.attachments.spec import MAX_TEXT_CHARS_TOTAL, AttachmentKind, AttachmentStatus

IMAGE_NOTE = "（以下为随本条消息上传的图片，请依据图像内容作答）"


@dataclass(frozen=True)
class AttachmentPayload:
    """执行阶段真正用得上的附件字段；其余（大小、上传时间等）只服务于界面。"""

    id: str
    name: str
    kind: str
    status: str
    mime: str = ""
    data: bytes | None = None
    text: str | None = None
    error: str | None = None
    meta: dict[str, str] = field(default_factory=dict)

    def data_uri(self) -> str | None:
        if self.data is None or not self.mime:
            return None
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


def _usable_texts(payloads: Sequence[AttachmentPayload]) -> tuple[list[str], int]:
    """按顺序取文档正文，返回 (正文块, 跳过的数量)，受总量上限约束。"""

    blocks: list[str] = []
    used = 0
    skipped = 0
    for payload in payloads:
        # 全用 `==` 比较：payload 可能来自数据库（普通字符串），也可能由代码直接
        # 构造（StrEnum 成员）。StrEnum 两边都能比，`is` 则只能命中后者。
        if payload.kind == AttachmentKind.IMAGE or payload.status != AttachmentStatus.READY:
            continue
        body = (payload.text or "").strip()
        if not body:
            skipped += 1
            continue
        if used + len(body) > MAX_TEXT_CHARS_TOTAL:
            skipped += 1
            continue
        used += len(body)
        blocks.append(f"【附件：{payload.name}】\n{body}")
    return blocks, skipped


def attachment_manifest(payloads: Sequence[AttachmentPayload]) -> str:
    """给模型一份「用户到底传了什么」的清单，含解析失败项。

    失败项**必须**出现在清单里。静默忽略等于让模型以为用户没传这份东西，
    进而编出一个"你没有提供附件"的答复——用户看到的却是自己明明传了。
    """

    lines: list[str] = []
    for index, payload in enumerate(payloads, start=1):
        if payload.status == AttachmentStatus.READY:
            descriptor = "图片" if payload.kind == AttachmentKind.IMAGE else "文档"
            lines.append(f"{index}. {payload.name}（{descriptor}）")
        else:
            lines.append(
                f"{index}. {payload.name}（未能解析：{payload.error or '格式不受支持'}）"
            )
    return "\n".join(lines)


def build_human_content(
    prompt: str,
    payloads: Sequence[AttachmentPayload] = (),
) -> str | list[dict[str, object]]:
    """构造 ``HumanMessage`` 的 content。

    没有图片时返回**纯字符串**——多模态 content block 对不支持视觉的模型是
    陌生的输入形状，能用文本就别换形状。只有真的存在可用的图片附件才升级成列表。
    """

    if not payloads:
        return prompt

    texts, skipped = _usable_texts(payloads)
    images = [
        payload
        for payload in payloads
        if payload.kind == AttachmentKind.IMAGE
        and payload.status == AttachmentStatus.READY
        and payload.data
    ]

    sections = [prompt, f"用户随本条消息上传了 {len(payloads)} 个附件：\n{attachment_manifest(payloads)}"]
    if texts:
        sections.append("附件正文：\n\n" + "\n\n".join(texts))
    if skipped:
        sections.append(
            f"（另有 {skipped} 个文档附件因超出长度上限未能附上正文，如需其内容请向用户说明。）"
        )
    if images:
        sections.append(IMAGE_NOTE)

    assembled = "\n\n".join(section for section in sections if section)
    if not images:
        return assembled

    blocks: list[dict[str, object]] = [{"type": "text", "text": assembled}]
    for payload in images:
        uri = payload.data_uri()
        if uri is None:
            continue
        blocks.append({"type": "image_url", "image_url": {"url": uri}})
    return blocks


__all__ = [
    "IMAGE_NOTE",
    "AttachmentPayload",
    "attachment_manifest",
    "build_human_content",
]
