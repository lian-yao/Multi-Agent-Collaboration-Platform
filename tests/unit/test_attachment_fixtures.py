"""真实二进制附件的解析与留档回归（ADR-021 / ADR-024）。

`tests/unit/test_attachments.py` 里的 docx / xlsx / pdf 样本都是手工拼的字节串：能钉住
"某段正则处理某种形状"，但钉不住"真实工具产出的文件长什么样"。这个文件用
`tests/fixtures/attachments/` 里**由 fpdf2 / python-docx / openpyxl 真实生成**的文件来跑，
夹具生成脚本是 `scripts/make_attachment_fixtures.py`（测试不依赖它，也不需要那几个库）。

四类 PDF 形态都被钉住（ADR-021 / ADR-027）：

| 夹具 | 形态 | 期望 |
| --- | --- | --- |
| `quarterly-report.pdf` | 核心字体 + 压缩内容流 + 多页 | 读出正文 |
| `embedded-subset-font.pdf` | 内嵌 TTF 子集，CID 写成**字面量串** | 读出正文（靠 `/ToUnicode`） |
| `two-font-heading.pdf` | 两种内嵌字体的码冲突 | 不填正文（**不填乱码**），改走页面图像 |
| `scanned-invoice.pdf` | 整页位图，没有文本层 | 不填正文，改走页面图像 |

外加 docx / xlsx 各一份真实文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.attachments import prepare_upload
from app.attachments.extract import extract
from app.attachments.spec import AttachmentKind, AttachmentStatus

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "attachments"


def fixture_bytes(name: str) -> bytes:
    data = (FIXTURE_DIR / name).read_bytes()
    assert data, f"夹具为空: {name}（跑 scripts/make_attachment_fixtures.py 重新生成）"
    return data


def extract_fixture(name: str):
    return extract(name, fixture_bytes(name))


# --------------------------------------------------------------------------------------
# pdf
# --------------------------------------------------------------------------------------


def test_real_multipage_pdf_yields_its_whole_text() -> None:
    """三页、压缩内容流、核心字体：整份正文都要读到，不能只读第一页。"""

    result = extract_fixture("quarterly-report.pdf")

    assert result.status is AttachmentStatus.READY
    assert result.kind is AttachmentKind.DOCUMENT
    text = result.text or ""
    assert "Quarterly Revenue Review" in text
    assert "4.82 million CNY" in text
    # 第三页的附录，用来确认多页都被合并而不是只看第一个内容流。
    assert "Appendix A. Table of Contents" in text


def test_real_embedded_subset_font_pdf_is_readable_via_tounicode() -> None:
    """内嵌 TTF 子集的 PDF：正文字形码靠文档自带的 `/ToUnicode` 还原。

    fpdf2 把 CID 写成**字面量串** `(\\000\\001...)`——这一条如果只按 latin-1 解，
    拿到的是控制字符，解析器会整份放弃。真实的 Word / LaTeX 导出大量使用这种写法。
    """

    result = extract_fixture("embedded-subset-font.pdf")

    assert result.status is AttachmentStatus.READY
    text = result.text or ""
    assert "Vendor Onboarding Notice" in text
    assert "VN-2026-0042" in text
    assert "2026-01-15" in text


def test_real_two_font_pdf_refuses_instead_of_emitting_garbage() -> None:
    """两种内嵌字体码冲突时**安全拒绝正文**，改走页面图像。

    子集字体的字符码各自从 0 起编，同一个码在两个字体下指向不同的字。合并 CMap 会让
    这些码变成冲突码；按"能解出来就用"的策略会产出通顺但完全错误的句子——比读不出来
    更糟，因为模型会当真。这里断言：**一个字的正文都没有**、原因说得清是码冲突、
    并且给出替代出路（ADR-027：页面渲染成图，交给视觉模型）。

    这条夹具恰好证明「渲染不是 OCR 的降级品」：它渲染出来完全可读
    （`Two Font Heading` / `The body uses a second embedded font subset.`），
    而文本通路出于正确性**刻意**不给。
    """

    result = extract_fixture("two-font-heading.pdf")

    assert result.status is AttachmentStatus.READY
    assert result.text is None
    assert "字符码" in (result.error or "")
    # 要说出「为什么不用这份正文」，而不只是「读出不来」：按错表解码出来的句子
    # 是通顺的、看不出问题的，所以原因必须写清是**还原不了**。
    assert "无法安全还原文字" in (result.error or "")
    assert "页面图像" in (result.error or "")


def test_real_scanned_pdf_yields_page_images_instead_of_text() -> None:
    """整页位图：PDF 结构合法但一个字都不是文本 → 正文为空，改走页面图像。"""

    result = extract_fixture("scanned-invoice.pdf")

    assert result.status is AttachmentStatus.READY
    assert result.text is None
    assert "文本层" in (result.error or "")
    assert "页面图像" in (result.error or "")


# --------------------------------------------------------------------------------------
# docx / xlsx
# --------------------------------------------------------------------------------------


def test_real_docx_extracts_paragraphs_and_table_cells() -> None:
    result = extract_fixture("meeting-notes.docx")

    assert result.status is AttachmentStatus.READY
    text = result.text or ""
    assert "Release Notes 2026.01" in text
    assert "attachment retention gap" in text
    # 表格单元格也要出来：python-docx 写的表格同样是 `w:t` 段落。
    assert "Attachments" in text and "Ready" in text


def test_real_xlsx_extracts_sheets_and_admits_formula_cells_are_empty() -> None:
    result = extract_fixture("budget-plan.xlsx")

    assert result.status is AttachmentStatus.READY
    text = result.text or ""
    assert "Cloud\t612000\tPlatform" in text
    assert "Amounts are in CNY." in text
    # 表头必须声明"公式未求值"：openpyxl 不写缓存结果，模型不该把空单元格当成"没有数据"。
    assert "公式未求值" in text


# --------------------------------------------------------------------------------------
# 留档（ADR-024）：解析成功与失败都要能拿回原件
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "has_text"),
    [
        ("quarterly-report.pdf", True),
        ("embedded-subset-font.pdf", True),
        ("two-font-heading.pdf", False),
        ("scanned-invoice.pdf", False),
        ("meeting-notes.docx", True),
        ("budget-plan.xlsx", True),
    ],
)
def test_every_real_fixture_is_validated_and_kept_byte_for_byte(
    name: str, has_text: bool
) -> None:
    """上传校验后的 `data` 必须与磁盘上的原件逐字节相同——包括读不出正文的那些。

    读不出正文的两份（扫描件、字体码冲突）恰恰是用户最需要拿回原件的：我们给不出正文字，
    但用户点下载时期望拿到的是他当初传的那份文件。

    `has_text=False` 的两份现在**也是 `ready`**（ADR-027：页面渲染成图交给视觉模型），
    所以「有没有正文」不能再拿来推「成不成功」——这两件事从这个版本起就是分开的。
    """

    data = fixture_bytes(name)
    prepared = prepare_upload(name, data, "")

    assert prepared["data"] == data
    assert prepared["size_bytes"] == len(data)
    assert prepared["kind"] == AttachmentKind.DOCUMENT.value
    assert prepared["status"] == AttachmentStatus.READY.value
    assert (prepared["text_content"] is not None) is has_text
    if not has_text:
        assert "页面图像" in (prepared["error"] or "")


def test_fixture_directory_is_fully_covered() -> None:
    """夹具目录里不能有"没人测"的文件：新增夹具必须同时加断言。"""

    covered = {
        "budget-plan.xlsx",
        "embedded-subset-font.pdf",
        "meeting-notes.docx",
        "quarterly-report.pdf",
        "scanned-invoice.pdf",
        "two-font-heading.pdf",
    }
    assert {path.name for path in FIXTURE_DIR.iterdir() if path.is_file()} == covered
