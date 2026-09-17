/**
 * 附件的两处呈现（ADR-021）：
 *
 * - `PendingFileChips`：输入区上方，展示「已选但还没发出去」的文件与上传状态；
 * - `MessageAttachmentList`：消息气泡里，展示已发出的附件。
 *
 * 两处都按**显式 props**驱动，不自己拉数据——`doc/testing.md` §3.3 的可测性约定：
 * 靠 effect 拉数据的容器静态渲染拿不到内容，卡片本体必须能脱离数据源渲染。
 */

import { AlertTriangle, FileSpreadsheet, FileText, ImageIcon, LoaderCircle, X } from "lucide-react";

import { api } from "../api/client";
import type { Attachment } from "../types/api";
import { describeAttachment, formatBytes, shortenName, type PendingAttachment } from "./attachments";

function KindIcon({ kind }: { kind: string }) {
  if (kind === "image") return <ImageIcon size={14} />;
  if (kind === "document") return <FileSpreadsheet size={14} />;
  return <FileText size={14} />;
}

export function PendingFileChips({
  items,
  onRemove,
}: {
  items: readonly PendingAttachment[];
  onRemove: (key: string) => void;
}) {
  if (!items.length) return null;
  return (
    <ul className="composer-files" aria-label="待发送附件">
      {items.map((item) => (
        <li
          key={item.key}
          className={`composer-file ${item.state}`}
          data-kind={item.kind}
          title={item.error || `${item.name}（${formatBytes(item.size)}）`}
        >
          <span className="composer-file-icon">
            {item.state === "uploading" ? (
              <LoaderCircle size={14} className="spin" />
            ) : item.state === "failed" ? (
              <AlertTriangle size={14} />
            ) : (
              <KindIcon kind={item.kind} />
            )}
          </span>
          <span className="composer-file-body">
            <b>{shortenName(item.name)}</b>
            <small>
              {item.state === "uploading"
                ? "上传中…"
                : item.state === "failed"
                  ? item.error || "上传失败"
                  : formatBytes(item.size)}
            </small>
          </span>
          <button
            type="button"
            className="composer-file-remove"
            aria-label={`移除 ${item.name}`}
            onClick={() => onRemove(item.key)}
          >
            <X size={13} />
          </button>
        </li>
      ))}
    </ul>
  );
}

export function MessageAttachmentList({ items }: { items: readonly Attachment[] }) {
  if (!items.length) return null;
  return (
    <div className="message-attachments">
      {items.map((item) => {
        const link = api.attachmentContentUrl(item.id);
        const thumb =
          item.kind === "image" && item.status === "ready" ? (
            <img src={link} alt={item.name} loading="lazy" />
          ) : (
            <KindIcon kind={item.kind} />
          );
        const body = (
          <span className="message-attachment-body">
            <b>{shortenName(item.name, 34)}</b>
            <small>{describeAttachment(item)}</small>
          </span>
        );

        // 原件留档之后（ADR-024）每种附件都能拿回原件：图片点开看原图、其余点开下载。
        // 没有字节的行（该策略之前落库的）不给任何入口——界面上不该出现一个必然 404 的链接。
        if (!item.has_original) {
          return (
            <div key={item.id} className={`message-attachment ${item.status}`} data-kind={item.kind}>
              <span className="message-attachment-thumb">{thumb}</span>
              {body}
            </div>
          );
        }
        return (
          <a
            key={item.id}
            className={`message-attachment ${item.status}`}
            data-kind={item.kind}
            href={link}
            target="_blank"
            rel="noreferrer"
            // 图片是「看一眼」，不要触发下载；其它类型设成下载，文件名沿用上传时的名字。
            download={item.kind === "image" ? undefined : item.name}
            title={`打开原件：${item.name}`}
          >
            <span className="message-attachment-thumb">{thumb}</span>
            {body}
          </a>
        );
      })}
    </div>
  );
}
