"""把附件字节解析成模型可消费的输入。

解析在**上传时**做一次，结果落库；执行阶段只读结果。这样做的理由：

- 解析是纯函数、有成本、且与执行次数无关——同一条消息重试三次不该解析三遍；
- Dapr 活动输入必须可序列化且体积受限，把原始字节塞进工作流输入会把
  gRPC 默认 4 MB 上限顶爆；只传 `attachment_id` 就没有这个问题；
- 解析失败是一个**用户可见的状态**（"这份 PDF 读不出正文"），在上传时就该知道，
  而不是等执行到一半才变成一段没人看懂的错误。

所有解析器都遵循同一条契约：畸形输入返回 ``(None, 原因)``，**不抛异常**。
上传接口不应该因为一个损坏的 zip 头而 500。
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

from app.attachments.spec import (
    MAX_TEXT_CHARS_PER_FILE,
    AttachmentKind,
    AttachmentStatus,
    classify,
    extension_of,
)
from app.attachments.spec import IMAGE_MIME_BY_EXTENSION

TRUNCATION_MARK = "\n\n……（附件正文超过长度上限，此处已截断）"


@dataclass(frozen=True)
class ExtractionResult:
    """一次解析的结果。`text` 与 `image_mime` 二选一有值。"""

    status: AttachmentStatus
    kind: AttachmentKind
    text: str | None = None
    image_mime: str | None = None
    error: str | None = None


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS_PER_FILE:
        return text
    return text[:MAX_TEXT_CHARS_PER_FILE].rstrip() + TRUNCATION_MARK


# --------------------------------------------------------------------------------------
# 纯文本类
# --------------------------------------------------------------------------------------

_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "utf-16", "latin-1")
"""按序尝试的编码。

`gb18030` 覆盖 GBK / GB2312（在中国用户上传的 txt/csv 里很常见），
`latin-1` 永远成功因此只能放最后——它是兜底而不是判断，
真落到它说明前面全失败，此时内容多半是二进制，交给可打印率闸门拦。"""


def _decode_text(data: bytes) -> tuple[str, str | None]:
    """解码纯文本；返回 (文本, 降级说明)。"""

    for encoding in _TEXT_ENCODINGS:
        try:
            return data.decode(encoding), None
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "文件不是有效文本编码，已按替换字符解码。"


def _printable_ratio(text: str) -> float:
    """可打印字符占比，用来识别「解码成功但其实是二进制」。"""

    if not text:
        return 0.0
    sample = text[:4000]
    printable = sum(
        1
        for ch in sample
        if ch.isprintable() or ch in "\n\r\t"
    )
    return printable / len(sample)


def extract_plain_text(data: bytes) -> ExtractionResult:
    text, note = _decode_text(data)
    if _printable_ratio(text) < 0.85:
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.TEXT,
            error="文件内容不是可读文本（可能是改了扩展名的二进制），已跳过正文。",
        )
    return ExtractionResult(
        status=AttachmentStatus.READY,
        kind=AttachmentKind.TEXT,
        text=_truncate(text),
        error=note,
    )


# --------------------------------------------------------------------------------------
# docx / xlsx：都是 zip + XML，用标准库解，不引第三方依赖
# --------------------------------------------------------------------------------------

_XML_TAG = re.compile(r"<[^>]+>")
_DOCX_PARAGRAPH = re.compile(r"<w:p[ >].*?</w:p>|<w:p/>", re.DOTALL)
_DOCX_TEXT = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.DOTALL)
_XLSX_SHARED = re.compile(r"<si>(.*?)</si>", re.DOTALL)
_XLSX_ROW = re.compile(r"<row[ >].*?</row>", re.DOTALL)
_XLSX_CELL = re.compile(r"<c\b([^>]*)>(.*?)</c>", re.DOTALL)
_XLSX_TYPE = re.compile(r'\bt="([^"]*)"')
_XLSX_VALUE = re.compile(r"<v>(.*?)</v>", re.DOTALL)
_XLSX_INLINE = re.compile(r"<t[^>]*>(.*?)</t>", re.DOTALL)


def _unescape_xml(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&#39;", "'")
        .replace("&amp;", "&")
    )


def extract_docx(data: bytes) -> ExtractionResult:
    """从 `word/document.xml` 抽段落文本。

    不引 `python-docx` 的理由：docx 的正文就是 zip 里一份 XML，抽文本只需要
    `w:p` / `w:t` 两级；而记分表里已知**本机与容器都没有 `python-docx`**，
    加依赖要重建镜像与锁文件，收益与风险不成比例。
    """

    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            raw = archive.read("word/document.xml")
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.DOCUMENT,
            error=f"不是有效的 docx（{type(exc).__name__}），已跳过正文。",
        )

    xml = raw.decode("utf-8", errors="replace")
    paragraphs: list[str] = []
    for block in _DOCX_PARAGRAPH.findall(xml):
        text = "".join(_DOCX_TEXT.findall(block)).strip()
        if text:
            paragraphs.append(_unescape_xml(text))
    if not paragraphs:
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.DOCUMENT,
            error="docx 正文为空或全部内容在文本框/图片里，未提取到文本。",
        )
    return ExtractionResult(
        status=AttachmentStatus.READY,
        kind=AttachmentKind.DOCUMENT,
        text=_truncate("\n".join(paragraphs)),
    )


def extract_xlsx(data: bytes) -> ExtractionResult:
    """把每个工作表转成 TSV 文本。

    只取单元格的字面值，不求值公式（没有计算引擎）；公式单元格会拿到底层结果值，
    没有缓存结果时退化成空——这一点写进正文抬头，避免模型把空单元格当成"没有数据"。
    """

    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_xml = archive.read("xl/sharedStrings.xml").decode(
                    "utf-8", errors="replace"
                )
                shared = [
                    _unescape_xml("".join(_XML_TAG.sub("", item)))
                    for item in _XLSX_SHARED.findall(shared_xml)
                ]
            sheets = [
                name
                for name in sorted(names)
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            ]
            blocks: list[str] = []
            for index, sheet in enumerate(sheets, start=1):
                sheet_xml = archive.read(sheet).decode("utf-8", errors="replace")
                rows: list[str] = []
                for row in _XLSX_ROW.findall(sheet_xml):
                    cells: list[str] = []
                    for cell in _XLSX_CELL.finditer(row):
                        declared = _XLSX_TYPE.search(cell.group(1))
                        cell_type = declared.group(1) if declared else ""
                        body = cell.group(2)
                        if cell_type == "s":
                            value = _XLSX_VALUE.search(body)
                            if value:
                                position = int(value.group(1) or 0)
                                cells.append(
                                    shared[position] if position < len(shared) else ""
                                )
                            else:
                                cells.append("")
                        elif cell_type == "inlineStr":
                            cells.append(
                                _unescape_xml(
                                    "".join(_XLSX_INLINE.findall(body)).strip()
                                )
                            )
                        else:
                            value = _XLSX_VALUE.search(body)
                            cells.append(
                                _unescape_xml((value.group(1) if value else "").strip())
                            )
                    if any(cells):
                        rows.append("\t".join(cells))
                if rows:
                    blocks.append(f"--- 工作表 {index}：{sheet} ---\n" + "\n".join(rows))
    except (KeyError, OSError, zipfile.BadZipFile, ValueError) as exc:
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.DOCUMENT,
            error=f"不是有效的 xlsx（{type(exc).__name__}），已跳过正文。",
        )

    if not blocks:
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.DOCUMENT,
            error="表格里没有可提取的单元格文本。",
        )
    header = "（下表为单元格字面值，公式未求值；空白单元格代表原表为空或仅有公式而无缓存结果）\n"
    return ExtractionResult(
        status=AttachmentStatus.READY,
        kind=AttachmentKind.DOCUMENT,
        text=_truncate(header + "\n\n".join(blocks)),
    )


# --------------------------------------------------------------------------------------
# pdf：尽力而为 + 质量闸门
# --------------------------------------------------------------------------------------

_PDF_STREAM = re.compile(rb"stream\r?\n")
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)", re.DOTALL)
_PDF_ESCAPE = re.compile(rb"\\([nrtbf()\\]|[0-7]{1,3})")
_ESCAPES = {
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"b": b"\b",
    b"f": b"\f",
    b"(": b"(",
    b")": b")",
    b"\\": b"\\",
}


def _pdf_unescape(raw: bytes) -> bytes:
    def replace(match: re.Match[bytes]) -> bytes:
        body = match.group(1)
        if body in _ESCAPES:
            return _ESCAPES[body]
        try:
            return bytes([int(body, 8) & 0xFF])
        except ValueError:
            return b""

    return _PDF_ESCAPE.sub(replace, raw[1:-1])


def _pdf_text_candidates(data: bytes) -> list[bytes]:
    """解压所有内容流，返回其中的 `(...)` 字面量串。

    只认字面量串（`Tj` / `TJ` 的操作数），不处理十六进制串 `<...>`：后者在
    中文字体里通常是 CID 编码，解出来是双字节序号而不是字，产出的是垃圾。
    宁可不认，也不要往提示词里塞垃圾。
    """

    chunks: list[bytes] = []
    for match in _PDF_STREAM.finditer(data):
        end = data.find(b"endstream", match.end())
        if end == -1:
            continue
        raw = data[match.end() : end]
        try:
            chunks.append(zlib.decompress(raw))
        except zlib.error:
            # 未压缩的内容流也存在（少见但合法）。
            chunks.append(raw)
    return chunks


def _pdf_meaningful_ratio(text: str) -> float:
    if not text:
        return 0.0
    sample = text[:4000]
    meaningful = sum(
        1
        for ch in sample
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" or ch in " \n.,;:!?()[]-—，。；：！？、（）"
    )
    return meaningful / len(sample)


def extract_pdf(data: bytes) -> ExtractionResult:
    """尽力提取 PDF 正文；中文 CID 字体或扫描件会失败，此时**明确报失败**。

    为什么能接受"尽力而为"：项目刻意不引 PDF 解析依赖（见 `doc/decisions/021`），
    而 PDF 正文流的文本提取在标准库范围内只能做到这个程度。与其让模型读一段
    乱码后自信地编出结论，不如让它知道这份附件没有可用正文。
    """

    if not data.startswith(b"%PDF"):
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.DOCUMENT,
            error="不是有效的 PDF（缺少 %PDF 头），已跳过正文。",
        )

    pieces: list[str] = []
    for chunk in _pdf_text_candidates(data):
        for literal in _PDF_STRING.findall(chunk):
            text = _pdf_unescape(literal).decode("latin-1")
            if text.strip():
                pieces.append(text)

    joined = " ".join(pieces)
    joined = re.sub(r"[ \t]{2,}", " ", joined).strip()
    joined = re.sub(r"\n{3,}", "\n\n", joined)

    if len(joined) < 40 or _pdf_meaningful_ratio(joined) < 0.7:
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.DOCUMENT,
            error=(
                "未能从 PDF 提取出可读正文（常见原因：扫描件没有文本层，"
                "或使用了 CID 子集字体）。已跳过正文，请改用文本或图片形式提供。"
            ),
        )
    return ExtractionResult(
        status=AttachmentStatus.READY,
        kind=AttachmentKind.DOCUMENT,
        text=_truncate(joined),
    )


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------


def extract(name: str, data: bytes) -> ExtractionResult:
    """按附件分类选择解析器；未知类型一律拒绝，不做"当作图片试试"的猜测。"""

    kind = classify(name)
    if kind is AttachmentKind.IMAGE:
        mime = IMAGE_MIME_BY_EXTENSION.get(extension_of(name))
        if mime is None:
            return ExtractionResult(
                status=AttachmentStatus.UNSUPPORTED,
                kind=AttachmentKind.UNSUPPORTED,
                error="不支持的图片格式。",
            )
        return ExtractionResult(
            status=AttachmentStatus.READY, kind=kind, image_mime=mime
        )
    if kind is AttachmentKind.TEXT:
        return extract_plain_text(data)
    if kind is AttachmentKind.DOCUMENT:
        ext = extension_of(name)
        if ext == "docx":
            return extract_docx(data)
        if ext == "xlsx":
            return extract_xlsx(data)
        if ext == "pdf":
            return extract_pdf(data)
    return ExtractionResult(
        status=AttachmentStatus.UNSUPPORTED,
        kind=AttachmentKind.UNSUPPORTED,
        error="该格式不在支持的附件类型内。",
    )


__all__ = [
    "ExtractionResult",
    "extract",
    "extract_docx",
    "extract_pdf",
    "extract_plain_text",
    "extract_xlsx",
]
