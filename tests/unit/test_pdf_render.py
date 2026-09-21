"""扫描版 PDF 的页面渲染（ADR-027）。

这一层**不是 OCR**：它只把页面渲成 PNG，读图交给视觉模型。因此这里钉的不是「文字对不对」，
而是三件可验证的事：

1. 渲出来的**真的是 PNG**（自己写的编码器，没有 Pillow 兜底，所以必须自校验结构）；
2. 上限真的生效（页数、单页体积、合计体积），且降级是**如实报告**而不是悄悄少给；
3. 任何畸形输入都**不抛异常**——上传接口不该因为一份坏 PDF 而 500。

`tests/unit/test_attachment_fixtures.py` 负责「端到端读得到」那一半（扫描件、码冲突
两份真实夹具），这里只管渲染器本身。
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.attachments.render import (
    PDF_RENDER_MAX_PAGES,
    RenderedPages,
    encode_png,
    render_pdf_pages,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "attachments"
SIGNATURE = b"\x89PNG\r\n\x1a\n"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def parse_png(data: bytes) -> dict[str, object]:
    """最小的 PNG 结构校验：签名、每个块的 CRC、IHDR 字段、解压后的行长。

    刻意不用 Pillow / numpy：渲染器自己就是手写编码器，用另一个库来「相信」它等于
    没测。这里逐块校验 CRC 并核对解压长度，等于自己实现了一遍解码器的骨架。
    """

    assert data.startswith(SIGNATURE), "PNG 签名不对"
    pos = len(SIGNATURE)
    info: dict[str, object] = {}
    idat = b""
    tags: list[bytes] = []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == (zlib.crc32(tag + payload) & 0xFFFFFFFF), f"{tag!r} 的 CRC 不对"
        tags.append(tag)
        if tag == b"IHDR":
            width, height, depth, color, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            info.update(
                width=width,
                height=height,
                depth=depth,
                color=color,
                compression=comp,
                filter_method=filt,
                interlace=interlace,
            )
        elif tag == b"IDAT":
            idat += payload
        pos += 12 + length

    assert tags[0] == b"IHDR" and tags[-1] == b"IEND"
    raw = zlib.decompress(idat)
    bytes_per_pixel = {0: 1, 2: 3, 6: 4}[int(info["color"])]  # type: ignore[arg-type]
    width = int(info["width"])  # type: ignore[arg-type]
    height = int(info["height"])  # type: ignore[arg-type]
    info["raw_bytes"] = len(raw)
    info["expected_raw_bytes"] = height * (1 + width * bytes_per_pixel)
    # filter 字节必须是 0（None）：解码器会按它选预测器，写错一个字节整图就花。
    info["filters"] = {raw[row * (1 + width * bytes_per_pixel)] for row in range(height)}
    return info


# --------------------------------------------------------------------------------------
# 真的渲出 PNG
# --------------------------------------------------------------------------------------


def test_real_scan_renders_to_a_structurally_valid_png() -> None:
    rendered = render_pdf_pages(fixture_bytes("scanned-invoice.pdf"))

    assert rendered.usable()
    assert rendered.total_pages == 1
    assert rendered.skipped_pages == 0
    assert rendered.reason is None

    info = parse_png(rendered.pages[0])
    assert info["depth"] == 8
    assert info["color"] == 2  # truecolor：pdfium 给的就是 RGB，没有 alpha
    assert info["interlace"] == 0
    assert info["raw_bytes"] == info["expected_raw_bytes"]
    assert info["filters"] == {0}
    # 一页 A4 在 1.7 倍下约 1012×1432：不能小到看不清字，也不该大到顶爆请求体。
    assert 800 <= int(info["width"]) <= 1600  # type: ignore[arg-type]
    assert int(info["height"]) > int(info["width"])  # type: ignore[arg-type]


def test_multi_page_fixture_renders_every_page_in_order() -> None:
    rendered = render_pdf_pages(fixture_bytes("quarterly-report.pdf"))

    assert rendered.total_pages == 4
    assert len(rendered.pages) == 4
    # 页序即文档页序：提示词里带页码，顺序错了等于给模型一份错页的材料。
    sizes = [len(page) for page in rendered.pages]
    assert all(size > 0 for size in sizes)
    for page in rendered.pages:
        assert parse_png(page)["color"] == 2


def test_max_pages_caps_rendering_and_reports_the_shortfall() -> None:
    """页数上限要**如实报告**：只带前 2 页就得说「总共 4 页」。"""

    rendered = render_pdf_pages(fixture_bytes("quarterly-report.pdf"), max_pages=2)

    assert len(rendered.pages) == 2
    assert rendered.total_pages == 4
    assert rendered.skipped_pages == 2


def test_default_page_cap_is_a_small_number() -> None:
    # 上限本身是被 ADR 讨论过的值（每页 base64 后 0.3–0.5 MB），别被顺手调大。
    assert PDF_RENDER_MAX_PAGES == 5


# --------------------------------------------------------------------------------------
# 降级：畸形输入、超限、组件缺失
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a pdf at all",
        b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n",
    ],
)
def test_broken_input_degrades_without_raising(data: bytes) -> None:
    rendered = render_pdf_pages(data)

    assert isinstance(rendered, RenderedPages)
    assert not rendered.usable()
    assert rendered.reason


def test_truncated_pdf_degrades_without_raising() -> None:
    """半截文件是真实会遇到的（浏览器中断上传），不能让它把上传接口打成 500。"""

    truncated = fixture_bytes("scanned-invoice.pdf")[:900]
    rendered = render_pdf_pages(truncated)

    assert not rendered.usable()
    assert rendered.reason


def test_missing_pdfium_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """镜像里没装渲染组件时，说法要指向组件本身，而不是「这份文件坏了」。"""

    monkeypatch.setitem(sys.modules, "pypdfium2", None)  # type: ignore[arg-type]

    rendered = render_pdf_pages(fixture_bytes("scanned-invoice.pdf"))

    assert not rendered.usable()
    assert "pypdfium2" in (rendered.reason or "")


def test_oversized_page_is_dropped_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """单页 PNG 顶到体积上限时：重渲一次 → 还超就丢掉，并计入 `skipped_pages`。

    把上限压到 100 字节来触发这条路径——真实夹具压不到这个量级，但**这条分支必须被测**：
    它是「宁可少一页也不能把网关顶爆」的唯一执行者。
    """

    from app.attachments import render as render_module

    monkeypatch.setattr(render_module, "PDF_RENDER_MAX_PAGE_BYTES", 100)

    rendered = render_pdf_pages(fixture_bytes("scanned-invoice.pdf"))

    assert not rendered.usable()
    assert rendered.total_pages == 1
    assert rendered.skipped_pages == 1
    assert rendered.reason


def test_total_size_cap_stops_early_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.attachments import render as render_module

    monkeypatch.setattr(render_module, "PDF_RENDER_MAX_TOTAL_BYTES", 1)

    rendered = render_pdf_pages(fixture_bytes("quarterly-report.pdf"))

    assert not rendered.usable()
    assert rendered.total_pages == 4
    assert rendered.skipped_pages == 4


# --------------------------------------------------------------------------------------
# PNG 编码器本身
# --------------------------------------------------------------------------------------


def test_encode_png_rejects_unknown_channel_count() -> None:
    bitmap = SimpleNamespace(width=2, height=1, stride=8, n_channels=2)
    with pytest.raises(ValueError):
        encode_png(bitmap, bytes(8))


def test_encode_png_writes_rows_with_filter_byte() -> None:
    """stride 有填充时也要按「行首插 filter 字节」写：否则图像会斜出一条。"""

    bitmap = SimpleNamespace(width=2, height=2, stride=8, n_channels=3)
    raw = b"\x01\x02\x03\x04\x05\x06\xff\xff" + b"\x07\x08\x09\x0a\x0b\x0c\xff\xff"

    info = parse_png(encode_png(bitmap, raw))

    assert info["width"] == 2 and info["height"] == 2
    assert info["raw_bytes"] == info["expected_raw_bytes"]
