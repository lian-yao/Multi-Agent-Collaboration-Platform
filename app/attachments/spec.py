"""附件类型、限额与分类：上传侧的唯一判据来源。

前端也有一份等价的「可接受类型 / 大小上限」判断（`frontend/src/workspace/attachments.ts`），
但**判据以本模块为准**：前端那份只是为了让用户在选择文件时立刻得到反馈，
真正的准入检查在服务端做一次，前端拦截不了的一律在这里被拒。

为什么不做完整的 MIME 嗅探：上传方（浏览器）给的 `type` 与扩展名都不可信，
但它们只影响**怎么解析**，不影响安全边界——所有附件都不执行、不落盘、不进沙箱，
只作为模型输入的一部分。因此这里按扩展名分类，服务端再按分类选择解析器；
解析器对畸形输入必须能安全失败（返回 ``None`` + 错误原因），不能抛出去。
"""

from __future__ import annotations

import re
from enum import StrEnum

MAX_FILE_BYTES = 5 * 1024 * 1024
"""单个附件上限。取 5 MB 的理由：图片要 base64 后进模型请求，多数模型服务对
单张图片的 base64 体量有 ~5 MB 量级的上限；再大就会在模型侧失败而不是在我们这里。"""

MAX_FILES_PER_MESSAGE = 4
"""单条消息允许的附件数。上限同时约束「一次执行要传多少份内容进模型」的成本。"""

MAX_TEXT_CHARS_PER_FILE = 20_000
"""单份文档注入提示词的最大字符数，超出截断并在正文里显式标注截断位置。"""

MAX_TEXT_CHARS_TOTAL = 40_000
"""一份消息内所有文档附件注入提示词的总字符上限，按顺序取用，后面的整份跳过。"""

MAX_NAME_CHARS = 160


class AttachmentKind(StrEnum):
    """附件按「怎么被模型消费」分类，而不是按文件类型分类。"""

    IMAGE = "image"
    """转成 image_url 内容块交给多模态模型，需要模型支持视觉输入。"""

    TEXT = "text"
    """纯文本类，直接解码后内联进提示词。"""

    DOCUMENT = "document"
    """容器格式（docx / xlsx / pdf），服务端尽力抽取正文后内联。"""

    UNSUPPORTED = "unsupported"
    """不接受的扩展名。登记时就拒绝，不产生「传了但没用上」的假象。"""


class AttachmentStatus(StrEnum):
    """附件在「能被模型用上」这件事上的状态。"""

    READY = "ready"
    FAILED = "failed"
    """解析失败（例如扫描版 PDF）。会上传成功但**明确标注**无法解析，
    并让模型知道这份附件没有内容可用，而不是假装它不存在。"""

    UNSUPPORTED = "unsupported"


IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "gif"})
"""只收主流视觉模型能直接吃的格式。bmp / tiff / svg 一律不收：
前两者多数模型服务不接受，svg 是可变文本、语义上更接近代码而非图片。"""

TEXT_EXTENSIONS = frozenset(
    {
        "txt", "md", "markdown", "rst", "log",
        "csv", "tsv", "json", "jsonl", "yaml", "yml", "toml", "ini", "env",
        "xml", "html", "htm", "sql",
        "py", "js", "jsx", "ts", "tsx", "vue", "go", "rs", "java", "kt",
        "c", "h", "cc", "cpp", "hpp", "cs", "rb", "php", "swift", "scala",
        "sh", "bash", "ps1", "bat", "css", "scss", "less", "lua", "r", "m",
    }
)
"""文本类扩展名的白名单。用白名单而不是「二进制黑名单」：黑名单漏掉一个格式就会
把二进制内容解码成乱码塞进提示词，白名单漏掉一个只是让用户换个格式。"""

DOCUMENT_EXTENSIONS = frozenset({"pdf", "docx", "xlsx"})

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS

IMAGE_MIME_BY_EXTENSION = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

_NAME_SAFE = re.compile(r"[\x00-\x1f\x7f]")


def extension_of(name: str) -> str:
    """取小写扩展名；没有扩展名时返回空串。"""

    cleaned = (name or "").strip()
    if "." not in cleaned:
        return ""
    return cleaned.rsplit(".", 1)[1].strip().lower()


def sanitize_name(raw: str) -> str:
    """收敛文件名：去掉路径分隔符与控制字符，压掉多余的空白，并限制长度。

    只用于**展示与判断类型**，不参与任何路径拼接——附件不落盘，没有目录穿越面。
    但仍然要收敛：文件名会进提示词，`../../etc/passwd` 这种名字会让模型误解输入。
    """

    name = _NAME_SAFE.sub("", (raw or "").strip())
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = " ".join(name.split())
    if not name:
        return "未命名附件"
    if len(name) > MAX_NAME_CHARS:
        stem, _, ext = name.rpartition(".")
        if ext and len(ext) <= 10:
            keep = MAX_NAME_CHARS - len(ext) - 1
            name = f"{stem[:keep]}.{ext}"
        else:
            name = name[:MAX_NAME_CHARS]
    return name


def classify(name: str) -> AttachmentKind:
    """按扩展名把附件分到「怎么被消费」的四类之一。"""

    ext = extension_of(name)
    if ext in IMAGE_EXTENSIONS:
        return AttachmentKind.IMAGE
    if ext in TEXT_EXTENSIONS:
        return AttachmentKind.TEXT
    if ext in DOCUMENT_EXTENSIONS:
        return AttachmentKind.DOCUMENT
    return AttachmentKind.UNSUPPORTED


def accept_attribute(extensions: frozenset[str] | None = None) -> str:
    """前端 `<input accept>` 用的属性串；服务端不消费，仅供 `/config/limits` 类接口导出。"""

    exts = sorted(extensions if extensions is not None else SUPPORTED_EXTENSIONS)
    return ",".join(f".{ext}" for ext in exts)


def describe_limits() -> dict[str, int]:
    """把限额导出给前端，避免前端再抄一份数字。"""

    return {
        "max_file_bytes": MAX_FILE_BYTES,
        "max_files_per_message": MAX_FILES_PER_MESSAGE,
        "max_text_chars_per_file": MAX_TEXT_CHARS_PER_FILE,
        "max_text_chars_total": MAX_TEXT_CHARS_TOTAL,
    }
