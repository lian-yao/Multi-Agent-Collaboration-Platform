"""生成附件解析用的**真实**二进制夹具（成员 C）。

为什么需要这个脚本：`tests/unit/test_attachments.py` 里的 PDF 样本是手工拼的字节串，
只能钉住"我们的正则能处理某种形状"，钉不住"真实工具产出的 PDF 长什么样"。真实 PDF
有对象交叉引用表、有 FlateDecode 内容流、有字体内嵌与子集化、有整页位图——这些差异
恰恰是解析器最容易踩空的地方。

夹具放 `tests/fixtures/attachments/` 并被提交，测试**不依赖本脚本**（测试环境没有
fpdf2 / python-docx / openpyxl，也不该为了读夹具去装它们）。本脚本只在需要重新
生成夹具时手动跑一次：

    uv run --no-project \\
      --with fpdf2 --with pillow --with python-docx --with openpyxl --with matplotlib \\
      python scripts/make_attachment_fixtures.py

用 matplotlib 只是为了拿一份**开源许可**的 TTF（DejaVuSans）：内嵌 TTF 的 PDF 会把
文本写成十六进制 CID 串，这正是解析器刻意不支持的那一类。用系统自带的商业字体
（SimHei / 微软雅黑）会把它内嵌进提交物，许可上不合适。

生成的夹具刻意**不追求逐字节可复现**（PDF 里带 Producer 与 CreationDate，换一个
fpdf2 版本就会变）。夹具以提交版为准，重跑只用于有意更新样例时。
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "attachments"
FIXED_DATE = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)


def _dejavu_path(filename: str = "DejaVuSans.ttf") -> Path:
    import matplotlib

    return Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf" / filename


def _write(pdf, text: str, height: float = 8, **kwargs) -> None:
    """写一整段并回到左边距。

    fpdf2 的 `multi_cell` 默认把光标留在**右边距**（`new_x=RIGHT`），紧接着再写一段
    就会报 "Not enough horizontal space"。夹具生成器里显式固定换行行为，免得每加一段
    文本都要想一次光标在哪。
    """

    pdf.multi_cell(0, height, text, new_x="LMARGIN", new_y="NEXT", **kwargs)


def _new_pdf(*, orientation: str = "P", **extra):
    import warnings

    from fpdf import FPDF

    kwargs = {"orientation": orientation}
    kwargs.update(extra)
    with warnings.catch_warnings():
        # 固定 CreationDate 时 fpdf2 会警告"日期在未来/过去"，与本脚本无关。
        warnings.simplefilter("ignore")
        try:
            return FPDF(**kwargs, creation_date=FIXED_DATE)
        except TypeError:
            return FPDF(**kwargs)


# --------------------------------------------------------------------------------------
# 1. 多页纯文本 PDF：真实成功路径（应能抽出正文）
# --------------------------------------------------------------------------------------

REPORT_PARAGRAPHS = (
    "Quarterly Revenue Review",
    "Prepared by the Analytics Working Group.",
    "Section 1. Summary",
    "Total revenue for the quarter reached 4.82 million CNY, up 17 percent "
    "year over year. Gross margin held at 41 percent.",
    "Section 2. Cost Structure",
    "Cloud infrastructure accounted for 612 thousand CNY, or 12.7 percent of "
    "revenue. Headcount cost grew 6 percent, below the revenue growth rate.",
    "Section 3. Outlook",
    "We expect the next quarter to land between 5.1 and 5.4 million CNY. The "
    "primary risk is renewal timing for three enterprise contracts.",
    "Section 4. Actions",
    "Renew the three enterprise contracts before the end of the quarter.",
    "Publish the cost dashboard to all department leads.",
    "Re-forecast headcount after the renewal outcome is known.",
)


def build_quarterly_report_pdf() -> bytes:
    """三页、核心字体（Helvetica）、fpdf2 默认配置（内容流压缩 + 真实 xref 表）。"""

    pdf = _new_pdf(format="A4")
    pdf.set_title("Quarterly Revenue Review")
    pdf.set_author("Analytics Working Group")
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    for index, paragraph in enumerate(REPORT_PARAGRAPHS):
        if index in (6, 10):
            pdf.add_page()
        if index == 0:
            pdf.set_font("Helvetica", style="B", size=18)
            _write(pdf, paragraph, 10)
            pdf.set_font("Helvetica", size=12)
            pdf.ln(3)
            continue
        _write(pdf, paragraph)
        pdf.ln(1)

    pdf.add_page()
    pdf.set_font("Helvetica", style="B", size=14)
    _write(pdf, "Appendix A. Table of Contents", 9)
    pdf.set_font("Helvetica", size=12)
    pdf.ln(2)
    for name in ("Section 1 Summary", "Section 2 Cost Structure", "Section 3 Outlook"):
        _write(pdf, f"- {name}")
    return bytes(pdf.output())


# --------------------------------------------------------------------------------------
# 2. 内嵌 TTF 子集字体：真实的"读不出正文"路径
# --------------------------------------------------------------------------------------


def build_embedded_font_pdf() -> bytes:
    """内嵌 TTF（子集）的 PDF：正文被写成字形码，靠文档自带的 `/ToUnicode` 还原。

    这一类曾经是解析器**读不出来**的：字形码按 latin-1 看是一串控制字符。现在
    `extract_pdf` 会解析 `/ToUnicode` CMap 把它读回来，所以这份夹具钉住的是**成功**
    路径——而且是真实生成器（fpdf2）产出的，不是我们手搓的样本。

    它同时覆盖了一个容易漏的细节：fpdf2 把 CID 写成**字面量串** `(\\000\\001...)`，
    而不是十六进制串 `<00010002>`。两种写法都要能解。
    """

    pdf = _new_pdf(format="A4")
    pdf.set_title("Vendor Notice")
    pdf.add_page()
    pdf.add_font("DejaVu", "", str(_dejavu_path()))
    pdf.set_font("DejaVu", size=13)
    for line in (
        "Vendor Onboarding Notice",
        "The following vendor has completed security review.",
        "Vendor reference: VN-2026-0042.",
        "Effective date: 2026-01-15.",
        "Please route invoices to the shared mailbox in future.",
    ):
        _write(pdf, line)
        pdf.ln(1)
    return bytes(pdf.output())


def build_two_font_heading_pdf() -> bytes:
    """两种内嵌字体的 PDF：钉住**当前读不出来**的那条边界。

    粗体标题 + 常规正文是真实文档的常态，而 Identity-H 子集字体的字符码各自从 0 起
    编：同一个码在两个字体下指向不同的字。合并所有 CMap 时这些码变成"冲突码"并被
    丢弃——解析器选择**安全拒绝**（宁可不读，也不按错表解出乱码）。要正确读出它，
    得跟踪内容流里的 `/Fx Tf` 并回解页面 `/Resources`，是另一次改动。

    夹具的价值正在于此：这条边界是被测试钉着的已知行为，不是"偶尔读不出来"。
    """

    pdf = _new_pdf(format="A4")
    pdf.set_title("Two Font Notice")
    pdf.add_page()
    pdf.add_font("Regular", "", str(_dejavu_path()))
    pdf.add_font("Bold", "", str(_dejavu_path("DejaVuSans-Bold.ttf")))
    pdf.set_font("Bold", size=16)
    _write(pdf, "Two Font Heading", 10)
    pdf.set_font("Regular", size=12)
    _write(pdf, "The body uses a second embedded font subset.")
    return bytes(pdf.output())


# --------------------------------------------------------------------------------------
# 3. 整页位图：真实的扫描件（没有文本层）
# --------------------------------------------------------------------------------------

INVOICE_LINES = (
    "INVOICE",
    "No. INV-2026-0117",
    "Date: 2026-01-15",
    "",
    "Item                Qty      Amount",
    "Design retainer       1      18,000",
    "Cloud hosting        12       2,340",
    "Support hours         8       4,000",
    "",
    "Total                       24,340",
)


def _scanned_page_image() -> "object":
    from PIL import Image, ImageDraw

    image = Image.new("L", (1240, 1754), 246)
    draw = ImageDraw.Draw(image)
    y = 220
    for line in INVOICE_LINES:
        draw.text((170, y), line, fill=40)
        y += 68
    # 一点纸张质感，避免整页是纯色块（也让文件更接近真实扫描件）。
    for row in range(0, 1754, 4):
        draw.line([(0, row), (1240, row)], fill=244)
    return image


def build_scanned_invoice_pdf() -> bytes:
    """把位图铺满整页：PDF 结构完全合法，但没有一个字是文本。"""

    pdf = _new_pdf(format="A4")
    pdf.set_title("Scanned Invoice INV-2026-0117")
    pdf.add_page()
    image = _scanned_page_image()
    pdf.image(image, x=0, y=0, w=210, h=297)
    return bytes(pdf.output())


# --------------------------------------------------------------------------------------
# 4. 真实 docx（python-docx）
# --------------------------------------------------------------------------------------

DOCX_HEADING = "Release Notes 2026.01"
DOCX_PARAGRAPHS = (
    "This release closes the attachment retention gap.",
    "Originals are now retained for every attachment kind, not only images.",
    "The sandbox probe reports an actionable reason when Docker is unreachable.",
)
DOCX_TABLE = (
    ("Component", "Status"),
    ("Attachments", "Ready"),
    ("Sandbox", "Ready"),
)


def build_meeting_notes_docx() -> bytes:
    from docx import Document

    document = Document()
    document.add_heading(DOCX_HEADING, level=1)
    for paragraph in DOCX_PARAGRAPHS:
        document.add_paragraph(paragraph)
    document.add_heading("Checklist", level=2)
    table = document.add_table(rows=0, cols=2)
    for left, right in DOCX_TABLE:
        cells = table.add_row().cells
        cells[0].text = left
        cells[1].text = right

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------------------
# 5. 真实 xlsx（openpyxl）
# --------------------------------------------------------------------------------------

XLSX_SHEETS = {
    "Budget": (
        ("Item", "Amount", "Owner"),
        ("Cloud", 612000, "Platform"),
        ("Salaries", 3180000, "People"),
        ("Tools", 148500, "Platform"),
    ),
    "Notes": (("Note",), ("Amounts are in CNY.",), ("Formula cells have no cached value.",)),
}


def build_budget_xlsx() -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for name, rows in XLSX_SHEETS.items():
        sheet = book.create_sheet(title=name)
        for row in rows:
            sheet.append(list(row))
    # 公式没有缓存结果（openpyxl 不求值），解析器会拿到空单元格——夹具要覆盖这一点。
    summary = book.create_sheet(title="Totals")
    summary.append(["Total"])
    summary.append(["=SUM(Budget!B2:B4)"])

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------

BUILDERS = (
    ("quarterly-report.pdf", build_quarterly_report_pdf),
    ("embedded-subset-font.pdf", build_embedded_font_pdf),
    ("two-font-heading.pdf", build_two_font_heading_pdf),
    ("scanned-invoice.pdf", build_scanned_invoice_pdf),
    ("meeting-notes.docx", build_meeting_notes_docx),
    ("budget-plan.xlsx", build_budget_xlsx),
)


def _describe(name: str, data: bytes) -> str:
    from app.attachments.extract import extract

    result = extract(name, data)
    detail = (result.text or result.error or "").replace("\n", " ")[:58]
    return f"{name:<26} {len(data):>8} B  {result.status.value:<7} {result.kind.value:<9} {detail}"


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, builder in BUILDERS:
        data = builder()
        (FIXTURE_DIR / name).write_bytes(data)
        lines.append(_describe(name, data))

    # 顺手校验 docx / xlsx 是能被标准库解开的合法 zip（夹具坏了要在生成时就发现）。
    for name in ("meeting-notes.docx", "budget-plan.xlsx"):
        with zipfile.ZipFile(FIXTURE_DIR / name) as archive:
            assert archive.testzip() is None, name

    print(f"fixtures -> {os.path.relpath(FIXTURE_DIR, REPO_ROOT)}")
    for line in lines:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
