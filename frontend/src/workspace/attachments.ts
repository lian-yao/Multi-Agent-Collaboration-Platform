/**
 * 附件的本地判断与文案（ADR-021）。
 *
 * 这里那份「可接受类型 / 大小上限」是**给用户即时反馈**用的：选文件时就能知道
 * 哪个太大、哪个不支持，不用等一次往返。真正的准入判据在服务端
 * （`app/attachments/spec.py`），前端拦不住的照样会被拒——两处口径必须一致，
 * 改一边记得改另一边，冒烟里有读文件断言钉着。
 */

import type { Attachment, AttachmentKind } from "../types/api";

/** 与 `app/attachments/spec.py` 的 `SUPPORTED_EXTENSIONS` 对应。 */
export const ATTACHMENT_EXTENSIONS = [
  "png", "jpg", "jpeg", "webp", "gif",
  "txt", "md", "markdown", "rst", "log",
  "csv", "tsv", "json", "jsonl", "yaml", "yml", "toml", "ini", "env",
  "xml", "html", "htm", "sql",
  "py", "js", "jsx", "ts", "tsx", "vue", "go", "rs", "java", "kt",
  "c", "h", "cc", "cpp", "hpp", "cs", "rb", "php", "swift", "scala",
  "sh", "bash", "ps1", "bat", "css", "scss", "less", "lua", "r", "m",
  "pdf", "docx", "xlsx",
] as const;

export const ATTACHMENT_ACCEPT = ATTACHMENT_EXTENSIONS.map((ext) => `.${ext}`).join(",");

/** 与 `MAX_FILE_BYTES` 一致：5 MB。图片要 base64 后进模型请求，再大就会在模型侧失败。 */
export const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;
/** 与 `MAX_FILES_PER_MESSAGE` 一致：4 个。 */
export const MAX_ATTACHMENT_COUNT = 4;

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp", "gif"]);
const DOCUMENT_EXTENSIONS = new Set(["pdf", "docx", "xlsx"]);

/** 输入区的附件条目：从选文件到发消息之间的本地状态。 */
export interface PendingAttachment {
  /** 本地唯一键。上传成功后仍用它做列表 key，避免 id 到达时行被重建、动画重放。 */
  key: string;
  name: string;
  size: number;
  kind: AttachmentKind | "unsupported";
  state: "uploading" | "ready" | "failed";
  /** 上传成功后才有；发消息时提交的就是这些 id。 */
  id?: string;
  error?: string;
}

export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).trim().toLowerCase();
}

export function classifyLocal(name: string): AttachmentKind | "unsupported" {
  const ext = extensionOf(name);
  if (IMAGE_EXTENSIONS.has(ext)) return "image";
  if (DOCUMENT_EXTENSIONS.has(ext)) return "document";
  if ((ATTACHMENT_EXTENSIONS as readonly string[]).includes(ext)) return "text";
  return "unsupported";
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 选文件时的即时拒绝理由；通过则返回 `null`。
 *
 * 三种拒绝都要给出理由并且**指名是哪个文件**：只说「有文件超限」，
 * 用户面对一批文件时无从下手。
 */
export function rejectionReason(
  file: { name: string; size: number },
  pending: readonly PendingAttachment[],
): string | null {
  if (!file.size) return `「${file.name}」是空文件。`;
  if (file.size > MAX_ATTACHMENT_BYTES) {
    return `「${file.name}」超过单文件上限 ${formatBytes(MAX_ATTACHMENT_BYTES)}。`;
  }
  if (classifyLocal(file.name) === "unsupported") {
    return `「${file.name}」的格式不支持（图片 / 常见文本与代码 / pdf、docx、xlsx）。`;
  }
  if (pending.length >= MAX_ATTACHMENT_COUNT) {
    return `单条消息最多附带 ${MAX_ATTACHMENT_COUNT} 个附件。`;
  }
  return null;
}

/** 与后端同样的折叠口径：超长文件名截断，扩展名保留。 */
export function shortenName(name: string, limit = 28): string {
  if (name.length <= limit) return name;
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot) : "";
  if (ext && ext.length <= 8) {
    return `${name.slice(0, limit - ext.length - 1)}…${ext}`;
  }
  return `${name.slice(0, limit - 1)}…`;
}

/**
 * 已发送附件的说明文案。
 *
 * `failed` 必须显式说清「没读出来」；`ready` 也可能带一句**降级说明**——服务端把这个
 * 字段当「解析成功但有话要说」用（纯文本按替换字符解码、扫描版 PDF 改走页面图像，
 * ADR-027）。这行说明比「文档 · 9.7 KB」重要得多：用户要能看出自己传的 PDF
 * 是按图片读的，而不是以为正文被正常提取了。
 */
export function describeAttachment(attachment: Attachment): string {
  if (attachment.status === "failed") {
    return attachment.error || "未能解析出正文，已跳过内容。";
  }
  if (attachment.error) {
    return attachment.error;
  }
  const labels: Record<string, string> = {
    image: "图片",
    text: "文本",
    document: "文档",
    unsupported: "不支持的格式",
  };
  return `${labels[attachment.kind] ?? "附件"} · ${formatBytes(attachment.size_bytes)}`;
}

/** 把文件读成 base64（不带 `data:` 前缀，服务端两种都收）。 */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`读取「${file.name}」失败。`));
    reader.onload = () => {
      const result = String(reader.result ?? "");
      // FileReader 给的是 data URL，取逗号后的部分。
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}
