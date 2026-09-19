"""附件解析与提示词构造（ADR-021）。

这里刻意**不碰数据库**：解析与拼装都是纯函数，用真实数据库测它们只会让用例
依赖环境（`doc/testing.md` §1）。接口层的归属回填在 `tests/integration/test_api.py`。
"""

from __future__ import annotations

import io
import zipfile
import zlib
from pathlib import Path

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
    load_payloads,
    prepare_upload,
)
from app.attachments.extract import (
    ExtractionResult,
    extract,
    extract_docx,
    extract_pdf,
    extract_plain_text,
    extract_xlsx,
)
from app.attachments.prompt import attachment_manifest
from app.attachments.spec import sanitize_name

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "attachments"


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


def _pdf_bytes(*texts: str, compress: bool = False) -> bytes:
    """构造极简 PDF：**每个入参一个内容流**（真实 PDF 就是每页一个内容流）。

    `compress=True` 时用 `FlateDecode` 并照实写上 `/Filter`——真实 PDF 的内容流几乎都是
    压缩的，只留未压缩样本等于没测到 `extract_pdf` 里的 `zlib.decompress` 分支。
    """

    streams: list[bytes] = []
    for text in texts:
        body = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
        streams.append(zlib.compress(body) if compress else body)

    first_page, first_content = 3, 3 + len(streams)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{first_page + i} 0 R".encode() for i in range(len(streams)))
        + b"] /Count "
        + str(len(streams)).encode()
        + b" >>",
    ]
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents "
        + str(first_content + i).encode()
        + b" 0 R >>"
        for i in range(len(streams))
    ]
    for stream in streams:
        dictionary = f"<< /Length {len(stream)}".encode()
        if compress:
            dictionary += b" /Filter /FlateDecode"
        objects.append(dictionary + b" >>\nstream\n" + stream + b"\nendstream")

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


def test_pdf_with_flate_compressed_stream_is_extracted() -> None:
    sentence = "Compressed content streams are the normal case in real documents."
    result = extract_pdf(_pdf_bytes(sentence, compress=True))
    assert result.status is AttachmentStatus.READY
    assert "Compressed" in (result.text or "")


def test_pdf_with_multiple_page_streams_joins_them() -> None:
    """多页 PDF 有多个内容流；只看第一个会让模型读到半份文档。"""

    first = "Overview of the quarterly results for the platform."
    second = "Second page carries the detailed breakdown by team."
    result = extract_pdf(_pdf_bytes(first, second))
    assert result.status is AttachmentStatus.READY
    assert "Overview" in (result.text or "")
    assert "Second page" in (result.text or "")


def test_pdf_without_header_is_rejected() -> None:
    result = extract_pdf(b"just some bytes")
    assert result.status is AttachmentStatus.FAILED
    assert "%PDF" in (result.error or "")


def _assert_no_readable_text(result: ExtractionResult) -> None:
    """核心不变量：正文要么是真读出来的，要么就没有——**绝不允许「像正文的垃圾」**。

    ADR-027 之后「抽不出正文」有两种收场，两种都算通过：

    - 页面能渲成图 → `ready` + `text=None` + 降级说明里写明改走页面图像；
    - 页面也渲不出来 → `failed` + 原因。

    被否掉的只有第三种：报 `ready`、正文却是一串乱码，或者没有任何交代。
    """

    assert result.text is None
    assert result.error
    if result.status is AttachmentStatus.READY:
        assert "页面图像" in result.error
    else:
        assert result.status is AttachmentStatus.FAILED


def test_pdf_with_garbage_text_layer_is_rejected() -> None:
    # 模拟 CID 子集字体：字面量串解出来是控制字符，可读比例极低。
    payload = "".join(chr(1 + (index % 6)) for index in range(200))
    result = extract_pdf(_pdf_bytes(payload))
    _assert_no_readable_text(result)


def test_pdf_with_no_text_layer_is_rejected() -> None:
    result = extract_pdf(b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n")
    assert result.status is AttachmentStatus.FAILED


# --------------------------------------------------------------------------------------
# PDF：/ToUnicode（内嵌子集字体的字形码）
# --------------------------------------------------------------------------------------

CID_SENTENCE = "Vendor onboarding notice for the platform review."


def _objects_pdf(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    for index, obj in enumerate(objects, start=1):
        out += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    return bytes(out)


def _stream(body: bytes) -> bytes:
    return b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream"


def _code_table(text: str) -> dict[str, int]:
    """按首次出现顺序从 1 开始编号——Identity-H 子集字体就是这么编的。"""

    table: dict[str, int] = {}
    for char in text:
        if char not in table:
            table[char] = len(table) + 1
    return table


def _bfchar_cmap(pairs: list[tuple[str, str]]) -> bytes:
    lines = "".join(f"<{code}> <{target}>\n" for code, target in pairs)
    return (
        "/CIDInit /ProcSet findresource begin\nbegincmap\n"
        f"{len(pairs)} beginbfchar\n{lines}endbfchar\nendcmap\n"
    ).encode("ascii")


def _cmap_for(text: str) -> bytes:
    table = _code_table(text)
    return _bfchar_cmap(
        [(f"{code:04X}", f"{ord(char):04X}") for char, code in table.items()]
    )


def _cid_content(text: str, *, hex_form: bool) -> bytes:
    """按 Identity-H 的两种真实写法之一编码正文。

    - `hex_form=True`：十六进制串 `<00010002...>`；
    - `hex_form=False`：**八进制转义的字面量串** `(\\000\\001...)`——fpdf2 与多数
      生成器用的就是这种，也是最初漏掉的那一种。
    """

    table = _code_table(text)
    if hex_form:
        operand = ("<" + "".join(f"{table[ch]:04X}" for ch in text) + ">").encode()
    else:
        raw = b"".join(bytes.fromhex(f"{table[ch]:04X}") for ch in text)
        operand = b"(" + b"".join(b"\\%03o" % byte for byte in raw) + b")"
    return b"BT /F1 12 Tf 20 100 Td " + operand + b" Tj ET"


def _cid_pdf(content: bytes, *, cmap: bytes | None) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 5 0 R "
        b"/Resources << /Font << /F1 4 0 R >> >> >>",
        b"<< /Type /Font /Subtype /Type0 /Encoding /Identity-H /ToUnicode 6 0 R >>",
        _stream(content),
    ]
    objects.append(_stream(cmap) if cmap is not None else b"<< >>")
    return _objects_pdf(objects)


def test_pdf_decodes_hex_cid_strings_through_tounicode() -> None:
    data = _cid_pdf(_cid_content(CID_SENTENCE, hex_form=True), cmap=_cmap_for(CID_SENTENCE))
    result = extract_pdf(data)
    assert result.status is AttachmentStatus.READY
    assert CID_SENTENCE in (result.text or "")


def test_pdf_decodes_escaped_literal_cid_strings_through_tounicode() -> None:
    """fpdf2 的写法：`(\\000\\001...)`。按 latin-1 看是一串控制字符。"""

    data = _cid_pdf(
        _cid_content(CID_SENTENCE, hex_form=False), cmap=_cmap_for(CID_SENTENCE)
    )
    result = extract_pdf(data)
    assert result.status is AttachmentStatus.READY
    assert CID_SENTENCE in (result.text or "")


def test_pdf_without_tounicode_does_not_guess_hex_strings() -> None:
    """没有 CMap 就不解十六进制串：解出来是字形序号，猜出来的只能是垃圾。"""

    data = _cid_pdf(_cid_content(CID_SENTENCE, hex_form=True), cmap=None)
    result = extract_pdf(data)
    _assert_no_readable_text(result)


def test_conflicting_tounicode_maps_refuse_instead_of_mixing_alphabets() -> None:
    """同一个码被两张表映射成不同的字 → 该码作废，整份正文读不出而不是读错。"""

    table = _code_table(CID_SENTENCE)
    real = [(f"{code:04X}", f"{ord(char):04X}") for char, code in table.items()]
    # 第二张表把同样的码全指向 'X'：冲突面足够大，正文必然解不出。
    wrong = [(f"{code:04X}", "0058") for code in table.values()]
    data = _cid_pdf(
        _cid_content(CID_SENTENCE, hex_form=True),
        cmap=_bfchar_cmap(real) + _bfchar_cmap(wrong),
    )
    result = extract_pdf(data)
    _assert_no_readable_text(result)
    # 码冲突这条要说得出「为什么不用这份正文」：ADR-027 之后它改走页面图像，
    # 但绝不能补一句「按错表解码也行」。
    assert "字符码" in (result.error or "")
    assert "字符码" in (result.error or "")


def test_bfrange_expands_a_sequential_code_run() -> None:
    from app.attachments.extract import _pdf_to_unicode_map

    cmap_body = (
        b"begincmap\n1 beginbfrange\n<0001> <0005> <0041>\nendbfrange\nendcmap\n"
    )
    mapping, conflicted = _pdf_to_unicode_map([cmap_body])

    assert mapping == {1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}
    assert conflicted is False


def test_binary_stream_shapes_are_not_mined_for_text() -> None:
    """没有文本算子的流不解：二进制流里凑出 `<hex>` 形状的概率不低，误采就是塞垃圾。"""

    from app.attachments.extract import _pdf_chunk_pieces

    cmap = {0x41: "A", 0x42: "B"}
    assert _pdf_chunk_pieces(b"<< /Registry <41> /Ordering <4241> >>", cmap) == []
    assert _pdf_chunk_pieces(b"\x00\x12<0041>\x00\x00binary", cmap) == []


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


def test_prepare_upload_keeps_document_bytes_alongside_text() -> None:
    """原件留档（ADR-024）：正文进提示词，字节留着给「下载原件」。"""

    prepared = prepare_upload("notes.md", "正文".encode())
    assert prepared["kind"] == "text"
    assert prepared["status"] == "ready"
    assert prepared["text_content"] == "正文"
    # 抽出正文不等于可以丢掉原件：历史消息里点开附件要拿到用户当初传的那份文件。
    assert prepared["data"] == "正文".encode()


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
    # 读不出正文不等于不留下原件：用户仍然能把它下载回去（ADR-024）。
    assert prepared["data"]


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


def test_document_without_text_is_not_reported_as_over_limit() -> None:
    """「没有可用正文」与「超出长度上限」是两件事，不能合成一句话。

    前者是解析能力的结果（用户只能换格式），后者是截断策略的结果（用户拆小就能解决）。
    把前者说成后者，等于让用户去做一件没用的事。
    """

    content = build_human_content(
        "看附件",
        [_payload(kind="document", name="scan.pdf", text=None, error=None)],
    )
    text = str(content)
    assert "没有可用正文" in text
    assert "超出长度上限" not in text


# --------------------------------------------------------------------------------------
# 扫描版 PDF → 页面图像载荷（ADR-027）
# --------------------------------------------------------------------------------------


def _patch_stored_row(monkeypatch: pytest.MonkeyPatch, name: str, payload_id: str = "s1") -> None:
    """把执行阶段的取数换成内存里的一行：`load_payloads` 唯一的 IO 就是这一次查询。"""

    row = prepare_upload(name, (FIXTURE_DIR / name).read_bytes(), "")
    row["id"] = payload_id
    monkeypatch.setattr(
        "app.core.checkpoint.load_attachment_payloads", lambda ids: [row]
    )


def test_scan_pdf_expands_into_page_images_with_the_parent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描件在执行侧变成图片载荷：走的是**既有的** image_url 通路，不是新通路。"""

    _patch_stored_row(monkeypatch, "scanned-invoice.pdf")

    payloads = load_payloads(["s1"])

    assert len(payloads) == 1
    page = payloads[0]
    assert page.kind == AttachmentKind.IMAGE
    assert page.mime == "image/png"
    assert page.data is not None and page.data.startswith(b"\x89PNG")
    assert "第1页/共1页" in page.name
    # id 沿用父附件：界面上是 1 个附件，内部拆成几页不该改变这个事实。
    assert page.id == "s1"


def test_scan_expansion_keeps_page_order_and_total_page_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """页码要能读出「看全了没有」：取名带「第 k 页/共 N 页」，且顺序即文档顺序。"""

    from app.attachments import prepare as prepare_module
    from app.attachments.render import RenderedPages

    _patch_stored_row(monkeypatch, "scanned-invoice.pdf")
    monkeypatch.setattr(
        prepare_module,
        "render_pdf_pages",
        lambda data, **kwargs: RenderedPages(
            pages=(b"\x89PNG-1", b"\x89PNG-2"), total_pages=9, skipped_pages=7
        ),
    )

    payloads = load_payloads(["s1"])

    assert [payload.name for payload in payloads] == [
        "scanned-invoice.pdf · 第1页/共9页",
        "scanned-invoice.pdf · 第2页/共9页",
    ]
    assert [payload.id for payload in payloads] == ["s1", "s1"]


def test_render_failure_this_run_is_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传时探测成功、执行时渲染失败 → 这一次执行必须说自己**什么都没拿到**。

    沿用上传那句「已改以页面图像提供」就是撒谎：界面上一切正常、模型却两手空空，
    正是本项目反复要避免的假信号。
    """

    from app.attachments import prepare as prepare_module
    from app.attachments.render import RenderedPages

    _patch_stored_row(monkeypatch, "scanned-invoice.pdf")
    monkeypatch.setattr(
        prepare_module,
        "render_pdf_pages",
        lambda data, **kwargs: RenderedPages(reason="模拟：渲染组件不可用"),
    )

    payloads = load_payloads(["s1"])

    assert len(payloads) == 1
    assert payloads[0].status == AttachmentStatus.FAILED
    assert "页面转图失败" in (payloads[0].error or "")
    # 附件仍然占一行：读不出内容不代表可以当它没传。
    assert "未能解析" in str(build_human_content("看附件", payloads))


def test_attachment_count_is_by_uploaded_file_not_by_page() -> None:
    """展开成 N 张图之后，「上传了 N 个附件」仍要按**文件**数报。"""

    pages = [
        _payload(
            id="s1",
            name=f"scan.pdf · 第{index}页/共2页",
            kind="image",
            mime="image/png",
            data=b"\x89PNG",
            text=None,
        )
        for index in (1, 2)
    ]

    content = build_human_content("看看这份扫描件", pages)

    assert isinstance(content, list)
    assert "上传了 1 个附件" in str(content[0]["text"])
    assert len([item for item in content if item["type"] == "image_url"]) == 2
