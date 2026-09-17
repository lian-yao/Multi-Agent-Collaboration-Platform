"""附件解析与提示词构造（ADR-021）。

这里刻意**不碰数据库**：解析与拼装都是纯函数，用真实数据库测它们只会让用例
依赖环境（`doc/testing.md` §1）。接口层的归属回填在 `tests/integration/test_api.py`。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.attachments import (
    MAX_FILE_BYTES,
    MAX_TEXT_CHARS_PER_FILE,
    AttachmentKind,
    AttachmentPayload,
    AttachmentRejected,
    AttachmentStatus,
    build_human_content,
    classify,
    describe_limits,
    prepare_upload,
)
from app.attachments.extract import (
    extract,
    extract_docx,
    extract_pdf,
    extract_plain_text,
    extract_xlsx,
)
from app.attachments.prompt import attachment_manifest
from app.attachments.spec import sanitize_name


# --------------------------------------------------------------------------------------
# 分类与文件名
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("shot.PNG", AttachmentKind.IMAGE),
        ("photo.jpeg", AttachmentKind.IMAGE),
        ("notes.md", AttachmentKind.TEXT),
        ("data.csv", AttachmentKind.TEXT),
        ("main.py", AttachmentKind.TEXT),
        ("report.docx", AttachmentKind.DOCUMENT),
        ("book.pdf", AttachmentKind.DOCUMENT),
        ("sheet.xlsx", AttachmentKind.DOCUMENT),
        ("archive.zip", AttachmentKind.UNSUPPORTED),
        ("vector.svg", AttachmentKind.UNSUPPORTED),
        ("noextension", AttachmentKind.UNSUPPORTED),
    ],
)
def test_classify_by_extension(name: str, expected: AttachmentKind) -> None:
    assert classify(name) == expected


def test_sanitize_name_strips_paths_and_whitespace() -> None:
    # 文件名会进提示词，`../../etc/passwd` 这类名字会让模型误解输入。
    assert sanitize_name("../../etc/pass wd.txt") == "pass wd.txt"
    assert sanitize_name("C:\\Users\\me\\报表.csv") == "报表.csv"
    assert sanitize_name("  ") == "未命名附件"
    assert "\x00" not in sanitize_name("a\x00b.txt")


def test_sanitize_name_keeps_extension_when_truncating() -> None:
    long_name = "x" * 400 + ".pdf"
    trimmed = sanitize_name(long_name)
    assert trimmed.endswith(".pdf")
    assert len(trimmed) <= 160


def test_describe_limits_matches_constants() -> None:
    limits = describe_limits()
    assert limits["max_file_bytes"] == MAX_FILE_BYTES
    assert limits["max_text_chars_per_file"] == MAX_TEXT_CHARS_PER_FILE


# --------------------------------------------------------------------------------------
# 纯文本
# --------------------------------------------------------------------------------------


def test_plain_text_utf8() -> None:
    result = extract_plain_text("标题\n正文".encode())
    assert result.status is AttachmentStatus.READY
    assert result.text == "标题\n正文"


def test_plain_text_gb18030_fallback() -> None:
    # 中国用户上传的 csv/txt 有相当比例是 GBK 系编码，utf-8 解码会直接抛错。
    result = extract_plain_text("姓名,金额\n张三,100".encode("gb18030"))
    assert result.status is AttachmentStatus.READY
    assert "张三" in (result.text or "")


def test_binary_masquerading_as_text_is_rejected() -> None:
    # 改了扩展名的二进制：解码"成功"但内容不可读，必须拦下来而不是塞进提示词。
    result = extract_plain_text(bytes(range(256)) * 8)
    assert result.status is AttachmentStatus.FAILED
    assert result.text is None
    assert "不是可读文本" in (result.error or "")


def test_long_text_is_truncated_with_marker() -> None:
    result = extract_plain_text(("行\n" * (MAX_TEXT_CHARS_PER_FILE)).encode())
    assert result.status is AttachmentStatus.READY
    assert len(result.text or "") <= MAX_TEXT_CHARS_PER_FILE + 40
    assert "已截断" in (result.text or "")


# --------------------------------------------------------------------------------------
# docx / xlsx：zip + XML，标准库解析
# --------------------------------------------------------------------------------------


def _docx_bytes(*paragraphs: str) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f"<w:document><w:body>{body}</w:body></w:document>",
        )
    return buffer.getvalue()


def test_docx_extracts_paragraph_text() -> None:
    result = extract_docx(_docx_bytes("第一段", "Hello &amp; 二"))
    assert result.status is AttachmentStatus.READY
    assert result.text == "第一段\nHello & 二"


def test_docx_rejects_non_zip() -> None:
    result = extract_docx(b"definitely not a zip")
    assert result.status is AttachmentStatus.FAILED
    assert result.text is None


def test_docx_without_text_reports_failure() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document><w:body/></w:document>")
    result = extract_docx(buffer.getvalue())
    assert result.status is AttachmentStatus.FAILED
    assert "未提取到文本" in (result.error or "")


def _xlsx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            "<sst><si><t>姓名</t></si><si><t>张三</t></si></sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            "<worksheet><sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>1</v></c></row>'
            "</sheetData></worksheet>",
        )
    return buffer.getvalue()


def test_xlsx_extracts_cells_as_tsv() -> None:
    result = extract_xlsx(_xlsx_bytes())
    assert result.status is AttachmentStatus.READY
    assert "姓名\t42" in (result.text or "")
    assert "张三" in (result.text or "")
    # 公式未求值这件事必须写在正文里，否则模型会把空单元格当成"没有这一列数据"。
    assert "公式未求值" in (result.text or "")


def test_xlsx_rejects_non_zip() -> None:
    assert extract_xlsx(b"nope").status is AttachmentStatus.FAILED


# --------------------------------------------------------------------------------------
# PDF：尽力而为 + 质量闸门
# --------------------------------------------------------------------------------------


def _pdf_bytes(text: str) -> bytes:
    """构造一个未压缩、单内容流的极简 PDF，用于验证文本运算符提取。"""

    body = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET"
    stream = body.encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    for index, obj in enumerate(objects, start=1):
        out += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def test_pdf_extracts_literal_text() -> None:
    sentence = "Quarterly revenue grew by twelve percent in the last period."
    result = extract_pdf(_pdf_bytes(sentence))
    assert result.status is AttachmentStatus.READY
    assert "revenue" in (result.text or "")


def test_pdf_without_header_is_rejected() -> None:
    result = extract_pdf(b"just some bytes")
    assert result.status is AttachmentStatus.FAILED
    assert "%PDF" in (result.error or "")


def test_pdf_with_garbage_text_layer_is_rejected() -> None:
    # 模拟 CID 子集字体：字面量串解出来是控制字符，可读比例极低。
    payload = "".join(chr(1 + (index % 6)) for index in range(200))
    result = extract_pdf(_pdf_bytes(payload))
    assert result.status is AttachmentStatus.FAILED
    assert "未能从 PDF 提取出可读正文" in (result.error or "")


def test_pdf_with_no_text_layer_is_rejected() -> None:
    result = extract_pdf(b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n")
    assert result.status is AttachmentStatus.FAILED


# --------------------------------------------------------------------------------------
# extract 入口
# --------------------------------------------------------------------------------------


def test_extract_image_keeps_bytes_and_reports_mime() -> None:
    result = extract("shot.png", b"\x89PNG\r\n\x1a\n")
    assert result.kind is AttachmentKind.IMAGE
    assert result.status is AttachmentStatus.READY
    assert result.image_mime == "image/png"
    assert result.text is None


def test_extract_unknown_extension_is_unsupported() -> None:
    result = extract("payload.exe", b"MZ")
    assert result.kind is AttachmentKind.UNSUPPORTED
    assert result.status is AttachmentStatus.UNSUPPORTED


# --------------------------------------------------------------------------------------
# prepare_upload：准入
# --------------------------------------------------------------------------------------


def test_prepare_upload_rejects_empty_and_oversize_and_unknown() -> None:
    with pytest.raises(AttachmentRejected) as empty:
        prepare_upload("a.txt", b"")
    assert empty.value.code == "ATTACHMENT_EMPTY"

    with pytest.raises(AttachmentRejected) as big:
        prepare_upload("a.txt", b"x" * (MAX_FILE_BYTES + 1))
    assert big.value.code == "ATTACHMENT_TOO_LARGE"

    with pytest.raises(AttachmentRejected) as unknown:
        prepare_upload("a.rar", b"Rar!")
    assert unknown.value.code == "ATTACHMENT_TYPE_UNSUPPORTED"


def test_prepare_upload_drops_document_bytes_but_keeps_text() -> None:
    prepared = prepare_upload("notes.md", "正文".encode())
    assert prepared["kind"] == "text"
    assert prepared["status"] == "ready"
    assert prepared["text_content"] == "正文"
    # 文档正文已抽出，原始字节不再保留——省库体积，也让执行阶段不必再解析一次。
    assert prepared["data"] is None


def test_prepare_upload_keeps_image_bytes() -> None:
    prepared = prepare_upload("shot.jpg", b"\xff\xd8\xff\xe0jpeg")
    assert prepared["kind"] == "image"
    assert prepared["data"] == b"\xff\xd8\xff\xe0jpeg"
    assert prepared["text_content"] is None
    # 前端没给 mime 时按扩展名补上，否则下载响应会退化成 octet-stream。
    assert prepared["mime"] == "image/jpeg"


def test_prepare_upload_records_parse_failure_without_rejecting() -> None:
    # 扫描版 PDF：能传上来，但正文取不出来，状态要如实标注。
    prepared = prepare_upload("scan.pdf", b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n")
    assert prepared["status"] == "failed"
    assert prepared["error"]
    assert prepared["text_content"] is None


# --------------------------------------------------------------------------------------
# build_human_content：拼装
# --------------------------------------------------------------------------------------


def _payload(**overrides: object) -> AttachmentPayload:
    base = {
        "id": "a1",
        "name": "a.md",
        "kind": "text",
        "status": "ready",
        "mime": "",
        "data": None,
        "text": "正文",
        "error": None,
    }
    base.update(overrides)
    return AttachmentPayload(**base)  # type: ignore[arg-type]


def test_no_attachments_keeps_plain_string_content() -> None:
    # 没有图片时不要升级成 content block 列表：多模态输入形状对纯文本模型是陌生输入。
    assert build_human_content("用户任务", []) == "用户任务"


def test_text_attachment_is_inlined_not_blockified() -> None:
    content = build_human_content("用户任务", [_payload()])
    assert isinstance(content, str)
    assert "【附件：a.md】" in content
    assert "正文" in content


def test_image_attachment_upgrades_to_content_blocks() -> None:
    content = build_human_content(
        "看看这张图",
        [_payload(kind="image", mime="image/png", data=b"\x89PNG", text=None, name="s.png")],
    )
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    blocks = [item for item in content if item["type"] == "image_url"]
    assert len(blocks) == 1
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_failed_attachment_is_still_listed_for_the_model() -> None:
    """解析失败的附件必须出现在清单里。

    静默忽略等于让模型以为用户没传这份东西，于是回一句「你没有提供附件」——
    用户看到的却是自己明明传了。
    """

    content = build_human_content(
        "看附件",
        [_payload(status="failed", text=None, error="未能从 PDF 提取出可读正文")],
    )
    assert "未能解析" in str(content)
    assert "未能从 PDF 提取出可读正文" in str(content)


def test_attachment_manifest_numbers_follow_upload_order() -> None:
    manifest = attachment_manifest(
        [
            _payload(id="1", name="一.png", kind="image", mime="image/png", data=b"x", text=None),
            _payload(id="2", name="二.md"),
        ]
    )
    lines = manifest.splitlines()
    assert lines[0].startswith("1. 一.png")
    assert lines[1].startswith("2. 二.md")


def test_image_without_bytes_is_not_emitted_as_block() -> None:
    # 元数据接口不返回字节，如果调用方误把元数据当载荷传进来，不能产出一个空的 image_url。
    content = build_human_content(
        "看图", [_payload(kind="image", mime="image/png", data=None, text=None)]
    )
    assert isinstance(content, str)
