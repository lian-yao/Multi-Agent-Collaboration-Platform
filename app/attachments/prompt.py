"""把附件拼成模型输入（提示词侧，不碰存储）。

调用点在**接收原始任务的根步骤**：静态链路的 collector，动态链路里
``depends_on`` 为空的步骤。理由：附件是「用户给的原始材料」，只有拿到原始任务
的那一步才该看到它；下游步骤拿到的是上游产出的正文，再塞一遍附件既没有新增信息，
又会让图片在每次调用里重复计费。多根步骤（动态图的并行起点）会各拿一份，
这是有意的——当前不做去重，重复成本记在 `doc/api.md` §5.16 的边界说明里。

扫描版 PDF 在这里表现为**若干张页面图**（ADR-027，展开发生在 `prepare.load_payloads`）：
它们就是普通图片附件，走同一条 `image_url` 通路，本模块不需要知道它们是 PDF 变的。
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


def _usable_texts(
    payloads: Sequence[AttachmentPayload],
) -> tuple[list[str], int, int]:
    """按顺序取文档正文，返回 (正文块, 超上限跳过的数量, 没有正文的数量)。

    两种「没附上正文」要分开数：**超出长度上限**是截断策略的结果，用户把文档拆小就能解决；
    **没有可用正文**（读不出文字、页面也转不成图）是解析能力的结果，用户只能换格式。
    合成一句「因超出长度上限未能附上正文」会把后者说成前者——那是**错的**。
    """

    blocks: list[str] = []
    used = 0
    over_limit = 0
    no_text = 0
    for payload in payloads:
        # 全用 `==` 比较：payload 可能来自数据库（普通字符串），也可能由代码直接
        # 构造（StrEnum 成员）。StrEnum 两边都能比，`is` 则只能命中后者。
        if payload.kind == AttachmentKind.IMAGE or payload.status != AttachmentStatus.READY:
            continue
        body = (payload.text or "").strip()
        if not body:
            no_text += 1
            continue
        if used + len(body) > MAX_TEXT_CHARS_TOTAL:
            over_limit += 1
            continue
        used += len(body)
        blocks.append(f"【附件：{payload.name}】\n{body}")
    return blocks, over_limit, no_text


def attachment_manifest(payloads: Sequence[AttachmentPayload]) -> str:
    """给模型一份「用户到底传了什么」的清单，含解析失败项与降级说明。

    失败项**必须**出现在清单里。静默忽略等于让模型以为用户没传这份东西，
    进而编出一个"你没有提供附件"的答复——用户看到的却是自己明明传了。

    `ready` 但带 `error` 的项要把那句说明带上：它是**降级说明**而不是失败原因
    （纯文本按替换字符解码、扫描件改走页面图像），恰是模型解释自己看到了什么、
    没看到什么时最需要的一句话。
    """

    lines: list[str] = []
    for index, payload in enumerate(payloads, start=1):
        if payload.status == AttachmentStatus.READY:
            descriptor = "图片" if payload.kind == AttachmentKind.IMAGE else "文档"
            note = f"；{payload.error}" if payload.error else ""
            lines.append(f"{index}. {payload.name}（{descriptor}{note}）")
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

    texts, over_limit, no_text = _usable_texts(payloads)
    images = [
        payload
        for payload in payloads
        if payload.kind == AttachmentKind.IMAGE
        and payload.status == AttachmentStatus.READY
        and payload.data
    ]

    # 附件数按 **id 去重**数：一份扫描版 PDF 在执行侧会被展开成 N 张页面图
    # （ADR-027），`len(payloads)` 会把「1 个附件」说成「5 个」。
    distinct_ids = {payload.id for payload in payloads if payload.id}
    uploaded = len(distinct_ids) or len(payloads)

    sections = [
        prompt,
        f"用户随本条消息上传了 {uploaded} 个附件：\n{attachment_manifest(payloads)}",
    ]
    if texts:
        sections.append("附件正文：\n\n" + "\n\n".join(texts))
    if over_limit:
        sections.append(
            f"（另有 {over_limit} 个文档附件因超出长度上限未能附上正文，如需其内容请向用户说明。）"
        )
    if no_text:
        sections.append(
            f"（有 {no_text} 个文档附件没有可用正文：读不出文字，页面也未能转成图像。"
            "如需其内容请向用户说明，不要凭附件名猜测。）"
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
