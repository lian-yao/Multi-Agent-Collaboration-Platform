"""扫描版 PDF：把页面渲染成图片，交给多模态模型读（ADR-027）。

**这不是 OCR**。OCR 是「本地把图变成文字」，要引一个引擎、要处理版面与置信度，
而且一旦识别错就会产出**通顺但错误**的句子——本项目已经在字体子集那块踩过一次
「读出来比读不出更糟」的坑。这里走的是另一条路：把页面原样渲染成图片，连同版面、
表格线、印章一起交给**已经付费的视觉通路**（`app/attachments/prompt.py` 的
`image_url` 块，实测 10/10，见 `doc/evals/vision.md`）。模型自己会看。

为什么用 pypdfium2 而不是 PyMuPDF：后者是 AGPL，嵌进一个要交付的工程会传染许可；
pypdfium2 是 BSD-3、Apache-2.0 双许可，wheel 自带 pdfium 二进制，BSD/Chrome 的血统。

为什么 PNG 编码自己写、不引 Pillow：pdfium 交出来的是**裸像素缓冲**（实测
`n_channels=3`、`stride=width*3`，即纯 RGB 无 alpha），而 PNG 的容器格式是
IHDR + IDAT(zlib) + IEND，标准库就能拼。引 Pillow 只为一个 `save()`，代价是
镜像里多一个几十 MB 的依赖（和 `extract.py` 里不引 `python-docx` 是同一条理由）。
注意**不能依赖 numpy**：它在 `.venv` 里只是因为别的包把它带进来了，不是声明依赖。

代价与边界（都写进 ADR-027）：

- 渲染是**有上限的**：最多 5 页、单页 1.2 MB、合计 4 MB。超限的页宁可丢掉并
  如实报告，也不截断成半页——半页图会给出「看过了」的假象。
- 渲染**只用于抽不出正文的 PDF**（扫描件、字体码冲突）。有文本层的 PDF 走正文，
  渲染对它们纯属浪费 token。
- 渲染失败**不抛异常**，契约与 `extract.py` 一致：返回空结果 + 原因，调用方降级。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Any

PDF_RENDER_MAX_PAGES = 5
"""单份 PDF 最多渲染的页数。取 5 的理由：每页 base64 后约 0.3–0.5 MB，5 页已经
顶到「单张图片 5 MB 量级」那条模型侧上限的同一数量级；再多的文件该让用户拆开传，
而不是让一次请求把网关顶爆。"""

PDF_RENDER_SCALE = 1.7
"""渲染倍率，相对 PDF 的 72 dpi。1.7 ≈ 122 dpi，A4（842pt 长边）得到约 1430px 长边。

取这个值的依据是**视觉模型的输入分辨率**：主流模型会把图缩到长边 1.5k 像素上下
再切块，渲得更大只会被丢掉；而渲得更小会让 8pt 的小字糊掉。1.7 落在「缩了不亏、
小了会糊」的区间里。"""

PDF_RENDER_MAX_SIDE = 2000
"""长边像素硬上限，用来兜住 A0 图纸这类超大页面：先按 `PDF_RENDER_SCALE` 算，
超过就按比例压下来，避免为了渲一页先分配上百 MB 的位图。"""

PDF_RENDER_FALLBACK_SCALE = 0.9
"""单页 PNG 超体积上限时的重渲倍率。宁可整页降分辨率，也不做降采样——降采样要
逐像素处理（没有 numpy），重渲一次更便宜也更清楚。"""

PDF_RENDER_MAX_PAGE_BYTES = 1_200_000
"""单页 PNG 体积上限。超了先按 `PDF_RENDER_FALLBACK_SCALE` 重渲一次；再超就
放弃这一页并记进 `skipped_pages`。"""

PDF_RENDER_MAX_TOTAL_BYTES = 4_000_000
"""一份 PDF 渲染结果的合计体积上限。到顶就停，剩下的页记进 `skipped_pages`。"""

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_COLOR_TYPE_BY_CHANNELS = {
    1: 0,  # 灰度
    3: 2,  # truecolor
    4: 6,  # truecolor + alpha（pdfium 普通页面给的是 3 通道，这里只是防御）
}


@dataclass(frozen=True)
class RenderedPages:
    """一次渲染的结果；`pages` 为空时 `reason` 说明为什么。"""

    pages: tuple[bytes, ...] = ()
    total_pages: int = 0
    """文档总页数（不是渲染出的页数）——用来把「只渲了前 5 页」如实告诉用户。"""

    skipped_pages: int = 0
    """因为体积或页数上限没能带上的页数。"""

    reason: str | None = None
    """整体失败的原因；成功但有丢弃页时这里为 None（丢弃情况看 `skipped_pages`）。"""

    def usable(self) -> bool:
        return bool(self.pages)


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(bitmap: Any, raw: bytes) -> bytes:
    """把 pdfium 的裸像素缓冲封成 PNG（8 位，无调色板，filter 全 0）。

    filter 全选 0（None）是刻意的：扫描件与文字页在 PNG 自己的 deflate 下已经压得
    很好（实测一页 A4 扫描件 22 KB），上 Sub/Up 预测器要逐行做算术，省下的字节
    不值得多一份可能的实现错误。
    """

    width = int(getattr(bitmap, "width"))
    height = int(getattr(bitmap, "height"))
    stride = int(getattr(bitmap, "stride"))
    channels = int(getattr(bitmap, "n_channels"))
    color_type = _COLOR_TYPE_BY_CHANNELS.get(channels)
    if color_type is None:
        raise ValueError(f"不支持的像素通道数：{channels}")

    step = width * channels
    rows = bytearray()
    for y in range(height):
        start = y * stride
        rows += b"\x00"
        rows += raw[start : start + step]

    header = struct.pack(
        ">IIBBBBB",
        width,
        height,
        8,  # 位深
        color_type,
        0,  # 压缩方法（唯一合法值）
        0,  # filter 方法（唯一合法值）
        0,  # 隔行扫描：不隔行
    )
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _png_chunk(b"IEND", b"")
    )


def _render_page(page: Any, scale: float) -> bytes:
    """渲一页并编码成 PNG。倍率会被 `PDF_RENDER_MAX_SIDE` 压一道。"""

    width, height = page.get_size()
    longest = max(float(width), float(height)) * scale
    if longest > PDF_RENDER_MAX_SIDE:
        scale = scale * (PDF_RENDER_MAX_SIDE / longest)
    bitmap = page.render(scale=scale)
    try:
        return encode_png(bitmap, bytes(memoryview(bitmap.buffer)))
    finally:
        close = getattr(bitmap, "close", None)
        if callable(close):
            close()


def render_pdf_pages(
    data: bytes,
    *,
    max_pages: int = PDF_RENDER_MAX_PAGES,
) -> RenderedPages:
    """把 PDF 的前若干页渲成 PNG；任何失败都降级成「没有页面图」而不是抛异常。

    返回的页序即文档页序——提示词里会带页码，顺序错了等于给模型一份错页的材料。
    """

    if not data.startswith(b"%PDF"):
        return RenderedPages(reason="不是有效的 PDF（缺少 %PDF 头），未渲染页面。")

    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(data)
    except ImportError:
        return RenderedPages(
            reason="服务器缺少 PDF 渲染组件（pypdfium2），无法把页面转为图片。"
        )
    except Exception as exc:  # noqa: BLE001 - 畸形 PDF 的失败形态很多，统一降级
        return RenderedPages(reason=f"无法打开 PDF（{type(exc).__name__}），未渲染页面。")

    pages: list[bytes] = []
    total = 0
    total_bytes = 0
    try:
        total = len(document)
        for index in range(min(total, max_pages)):
            try:
                png = _render_page(document[index], PDF_RENDER_SCALE)
                if len(png) > PDF_RENDER_MAX_PAGE_BYTES:
                    png = _render_page(document[index], PDF_RENDER_FALLBACK_SCALE)
            except Exception:  # noqa: BLE001 - 单页失败不该废掉整份文档
                continue
            if len(png) > PDF_RENDER_MAX_PAGE_BYTES:
                continue
            if total_bytes + len(png) > PDF_RENDER_MAX_TOTAL_BYTES:
                # 体积到顶就停：后面的页不再尝试，如实计入未带上的页数。
                break
            pages.append(png)
            total_bytes += len(png)
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()

    # 未带上的页数一律按「总数 − 实际带上」算，页数上限、体积上限、单页失败三种
    # 丢法都落在同一个数字里：调用方只需要如实说「共 N 页，带上了 M 页」。
    skipped = max(0, total - len(pages))
    if not pages:
        return RenderedPages(
            total_pages=total,
            skipped_pages=skipped,
            reason="PDF 页面渲染失败（可能是加密文档或页面内容异常）。",
        )
    return RenderedPages(pages=tuple(pages), total_pages=total, skipped_pages=skipped)


__all__ = [
    "PDF_RENDER_FALLBACK_SCALE",
    "PDF_RENDER_MAX_PAGES",
    "PDF_RENDER_MAX_PAGE_BYTES",
    "PDF_RENDER_MAX_SIDE",
    "PDF_RENDER_MAX_TOTAL_BYTES",
    "PDF_RENDER_SCALE",
    "RenderedPages",
    "encode_png",
    "render_pdf_pages",
]
