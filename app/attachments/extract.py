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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.attachments.render import PDF_RENDER_MAX_PAGES, RenderedPages, render_pdf_pages
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


def _pdf_stream_chunks(data: bytes) -> list[bytes]:
    """按文档顺序解压所有流；未压缩的流原样返回。

    未压缩的流少见但合法，而且**恰恰是最要紧的那一个**：`/ToUnicode` CMap 通常不压缩。
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


_CMAP_BFCHAR_BLOCK = re.compile(rb"beginbfchar(.*?)endbfchar", re.DOTALL)
_CMAP_BFRANGE_BLOCK = re.compile(rb"beginbfrange(.*?)endbfrange", re.DOTALL)
_CMAP_BFCHAR_PAIR = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_CMAP_BFRANGE_TRIPLE = re.compile(
    rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>"
)
_HEX_STRING = re.compile(rb"<([0-9A-Fa-f]+)>")
_TEXT_OPERATOR = re.compile(rb"\bTj\b|\bTJ\b|\bBT\b")


def _decode_cmap_target(hexdigits: bytes) -> str:
    """CMap 的目标码按 UTF-16BE 解（Adobe 规范）；奇数长度退化为单字节。"""

    try:
        raw = bytes.fromhex(hexdigits.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not raw:
        return ""
    if len(raw) % 2:
        return raw.decode("latin-1")
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _assign_code(
    mapping: dict[int, str], ambiguous: set[int], code: int, text: str
) -> None:
    if code in ambiguous:
        return
    existing = mapping.get(code)
    if existing is None:
        mapping[code] = text
    elif existing != text:
        ambiguous.add(code)


def _pdf_to_unicode_map(chunks: Sequence[bytes]) -> tuple[dict[int, str], bool]:
    """合并文档内所有 `/ToUnicode` CMap，得到「字符码 → 文本」。

    为什么必须支持：内嵌 TTF 子集字体的 PDF（Word / LaTeX / fpdf2 的默认形态）把正文
    写成字形码而不是字符——字面量串里是 `\\000\\001\\000\\002` 这样的原始双字节，十六进制
    串里是 `<00010002>`。没有这张表就只能看见一串序号；有了它才能还原成字。

    返回值第二项表示**是否发生过码冲突**，用于把失败原因说清楚（见下）。

    冲突即放弃：多字体文档里同一个码在不同子集字体下指向不同的字——Identity-H 的码
    从 0 起编，两个字体必然撞车。这时把该码当未映射处理，宁可少读一段，也不能把 A 字体
    的码拿 B 字体的表解出乱码塞进提示词。**代价是「同一页用了两种以上内嵌字体」的文档
    目前读不出正文**（`two-font-heading.pdf` 夹具钉住这个边界）；按字体分别建表要跟踪
    内容流里的 `/Fx Tf` 并回解页面 `/Resources`，属于另一次改动，暂不做。

    只解 `beginbfchar` 与 `beginbfrange` 的 `<lo> <hi> <dst>` 形式：它们是真实生成器
    会写出来的形态；`bfrange` 的数组形式（`<lo> <hi> [<u1> <u2>]`）很少见，不猜。
    """

    mapping: dict[int, str] = {}
    ambiguous: set[int] = set()
    for chunk in chunks:
        if b"beginbfchar" not in chunk and b"beginbfrange" not in chunk:
            continue
        for block in _CMAP_BFCHAR_BLOCK.findall(chunk):
            for code_hex, target_hex in _CMAP_BFCHAR_PAIR.findall(block):
                text = _decode_cmap_target(target_hex)
                if text:
                    _assign_code(mapping, ambiguous, int(code_hex, 16), text)
        for block in _CMAP_BFRANGE_BLOCK.findall(chunk):
            for lo_hex, hi_hex, target_hex in _CMAP_BFRANGE_TRIPLE.findall(block):
                lo, hi = int(lo_hex, 16), int(hi_hex, 16)
                base_text = _decode_cmap_target(target_hex)
                if len(base_text) != 1 or hi < lo or hi - lo > 0xFFFF:
                    continue
                base = ord(base_text)
                for offset in range(hi - lo + 1):
                    _assign_code(mapping, ambiguous, lo + offset, chr(base + offset))

    for code in ambiguous:
        mapping.pop(code, None)
    return mapping, bool(ambiguous)


def _printable_share(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if ch.isprintable() or ch in " \n\t") / len(text)


def _decode_with_cmap(raw: bytes, cmap: Mapping[int, str]) -> str:
    """把一串字节按 CMap 解成文本；**任一码解不出就返回空串**。

    双字节码（Identity-H）优先，但两种切法都试：简单字体也可能只用单字节码。
    两边都能解时取可打印字符占比高的那个——双字节在打平时胜出，因为它是内嵌 TTF
    子集字体的主流形态，而单字节解出"看起来也可打印"常常只是巧合。

    内嵌 TTF 子集字体的正文有两种写法，都要走这里：
    - **字面量串** `(\\000\\001\\000\\002...) Tj`——fpdf2 / 多数生成器的写法，
      解出来是**字形码原文**，直接按 latin-1 看就是一堆控制字符；
    - **十六进制串** `<00010002...> Tj`——同样要靠 CMap 才认得出字。
    """

    if not cmap or not raw or len(raw) > 4096:
        return ""

    best = ""
    best_score = 0.0
    for width in (2, 1):
        if len(raw) % width:
            continue
        chars = [
            cmap.get(int.from_bytes(raw[index : index + width], "big"))
            for index in range(0, len(raw), width)
        ]
        if any(ch is None for ch in chars):
            continue
        text = "".join(chars)
        if not text.strip():
            continue
        score = _printable_share(text) + (0.01 if width == 2 else 0.0)
        if score > best_score:
            best, best_score = text, score
    return best


def _hex_to_bytes(hexdigits: bytes) -> bytes:
    try:
        return bytes.fromhex(hexdigits.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return b""


def _pdf_chunk_pieces(chunk: bytes, cmap: Mapping[int, str]) -> list[str]:
    """一条流里的文本片段，按出现顺序返回（字面量串与十六进制串混排）。

    只在看起来是**内容流**的块里取值（出现 `BT` / `Tj` / `TJ`）：PDF 里 `(Adobe)`
    这样的字典值遍地都是，把它们当正文抓出来就是往提示词里塞噪声；二进制流
    （CIDSet、字体文件）里也可能凑出 `<hex>` 形状的字节。
    """

    if not _TEXT_OPERATOR.search(chunk):
        return []

    found: list[tuple[int, str]] = []
    for match in _PDF_STRING.finditer(chunk):
        raw = _pdf_unescape(match.group(0))
        # CMap 解不出时退回 latin-1：核心字体的 `(text)` 本来就是这么读的。
        found.append((match.start(), _decode_with_cmap(raw, cmap) or raw.decode("latin-1")))
    if cmap:
        for match in _HEX_STRING.finditer(chunk):
            raw = _hex_to_bytes(match.group(1))
            if not raw:
                continue
            text = _decode_with_cmap(raw, cmap)
            if text:
                found.append((match.start(), text))

    found.sort(key=lambda item: item[0])
    return [text for _, text in found if text.strip()]


def extract_pdf(data: bytes) -> ExtractionResult:
    """尽力提取 PDF 正文，两条路都走。

    1. 内容流里的**字面量串** `(text) Tj`：核心字体（Helvetica / 宋体）走这条；
    2. 内嵌 TTF 子集字体写出的**十六进制串** `<hex> Tj`（`/Encoding /Identity-H`）：
       靠文档自带的 `/ToUnicode` 还原，这是真实生成器的主流形态。

    抽不出正文时**不直接判失败**，先探一次「能不能把页面渲成图」（ADR-027）：能，就以
    `status=ready` + `text=None` + 降级说明返回，让执行阶段把页面图像交给视觉模型；不能，
    才报 `FAILED`。

    三条出口都必须是明确的：有正文就给正文；没正文但有页面图就**说清这一点**；两样都
    没有才失败，并在 `error` 里说明原因。与其让模型读一段乱码后自信地编出结论，不如让它
    知道这份附件到底能给到什么。
    """

    if not data.startswith(b"%PDF"):
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.DOCUMENT,
            error="不是有效的 PDF（缺少 %PDF 头），已跳过正文。",
        )

    chunks = _pdf_stream_chunks(data)
    cmap, conflicted = _pdf_to_unicode_map(chunks)
    pieces: list[str] = []
    for chunk in chunks:
        pieces.extend(_pdf_chunk_pieces(chunk, cmap))

    joined = " ".join(pieces)
    joined = re.sub(r"[ \t]{2,}", " ", joined).strip()
    joined = re.sub(r"\n{3,}", "\n\n", joined)

    if len(joined) < 40 or _pdf_meaningful_ratio(joined) < 0.7:
        # 只渲第一页做**探测**（几十毫秒）：要的是「这份文件在这台机器上能不能渲出来」，
        # 真正的整份渲染留给执行阶段——那份页面图只在真的要送进模型时才有价值，
        # 上传时就整份渲掉会把没人看的位图白算一遍。
        probe = render_pdf_pages(data, max_pages=1)
        if probe.usable():
            return ExtractionResult(
                status=AttachmentStatus.READY,
                kind=AttachmentKind.DOCUMENT,
                error=_pdf_render_note(conflicted, probe),
            )
        return ExtractionResult(
            status=AttachmentStatus.FAILED,
            kind=AttachmentKind.DOCUMENT,
            error=_pdf_failure_reason(conflicted, probe),
        )
    return ExtractionResult(
        status=AttachmentStatus.READY,
        kind=AttachmentKind.DOCUMENT,
        text=_truncate(joined),
    )


def _pdf_render_note(conflicted: bool, rendered: RenderedPages) -> str:
    """「正文没抽出来，但页面图能用」的降级说明（ADR-027）。

    这条说明走 `error` 字段，与纯文本编码降级（`extract_plain_text`）同一语义：
    **解析成功但有话要说**。用户要能看出这份 PDF 为什么是「按图片读的」。

    刻意写短：它同时会出现在消息气泡的附件条目上（`describeAttachment`），
    一段话的解释在那里会挤成三行。详细的「为什么不用文本层」留在 ADR-027 里。
    """

    if conflicted:
        head = "文档内多种字体子集共用了相同的字符码，无法安全还原文字"
    else:
        head = "这份 PDF 没有文本层（扫描件）"
    note = head + "，已改用页面图像提供正文"
    if rendered.total_pages > PDF_RENDER_MAX_PAGES:
        note += f"（全 {rendered.total_pages} 页，只带前 {PDF_RENDER_MAX_PAGES} 页）"
    return note + "。"


def _pdf_failure_reason(conflicted: bool, rendered: RenderedPages | None = None) -> str:
    """失败原因要能指向下一步动作，而不是一句「读不出来」。

    「字体码冲突」与「扫描件」对用户是两件完全不同的事：前者换个导出方式（嵌出字体
    子集之外的写法、或改用文本/图片）就能解决，后者只能换成图片或补文本层。
    ADR-027 之后，两条路都会**先试页面转图**；这里出现的失败，说明转图也没成——
    所以要把转图失败的原因一并带上，否则运维看到的只是一句「读不出来」。
    """

    if conflicted:
        base = (
            "未能从 PDF 提取出可读正文：文档内多个内嵌字体子集使用了相同的字符码"
            "（Identity-H 子集字体各自从 0 起编号），无法安全还原文字，已跳过正文以避免"
            "填入乱码。可改用文本或图片形式提供，或导出为只使用一种字体的 PDF。"
        )
    else:
        base = (
            "未能从 PDF 提取出可读正文（常见原因：扫描件没有文本层，"
            "或字体子集缺少可用的 ToUnicode 映射）。已跳过正文，请改用文本或图片形式提供。"
        )
    if rendered is not None and rendered.reason:
        base += f"页面转图也未能完成：{rendered.reason}"
    return base


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
