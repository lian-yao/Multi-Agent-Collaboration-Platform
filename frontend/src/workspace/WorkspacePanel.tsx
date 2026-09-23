import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { FolderTree, HardDrive, ShieldAlert } from "lucide-react";
import { ApiError, api } from "../api/client";
import { InlineConfirm } from "../components/InlineConfirm";
import { fileToBase64 } from "./attachments";
import type {
  Workspace,
  WorkspaceEntry,
  WorkspaceHostTree,
  WorkspaceImportFile,
  WorkspaceMode,
  WorkspaceRootTree,
  WorkspaceTree,
} from "../types/api";
import {
  Chip,
  EmptyState,
  Field,
  NoticeBar,
  describeError,
  formatTime,
  truncate,
  type NoticeState,
} from "../config/shared";
// 工作区面板用的是 cfg 设计系统（`.cfg-*` 在 config/config.css 里定义）；
// 工作台里其它跨视图组件（CollabCanvas / AgentStageModal）同样这样引用。
import "../config/config.css";
import "./workspace.css";

/**
 * 工作区（`doc/api.md` §5.19 / §7.1、ADR-033）。
 *
 * 入口在**工作台**（2026-09-23 从「工具与配置」搬过来）：工作区按 `session_id` 生效——
 * 会话级注册表只挑**绑定本会话**的那一条，别的登记不会给这次执行的 Agent 任何文件工具。
 *
 * 四件刻意的事：
 *
 * 1. **草稿态只是暂存**：工作台起步于 `session = null`（§4.2 草稿态），此时选好的
 *    「路径 + 档位」只记在前端，等首条消息把会话建出来再补一次登记。不提前建会话，
 *    也不造 `session_id=null` 的工作区——那是一条不会被选中、Agent 什么也拿不到的记录。
 * 2. **「选择文件夹」按钮导入的是副本**：浏览器能给的只有相对路径 + 内容
 *    （`<input type="file" webkitdirectory>`），拿不到宿主绝对路径，所以这条路的语义是
 *    「把这份文件夹导入工作区」。想让 Agent 直接操作你本机那个目录，走宿主侧脚本 +
 *    bind mount（`scripts/pick_work_dir.ps1`）。界面文案必须写清这个区别。
 * 3. **档位只有两档**且提档是**人的动作**：`read_only` ⇄ `workspace_write`。
 *    不出现 `full_access`（没实现），也不做「Agent 申请提权」的入口（ADR-033 §3）。
 * 4. **目录树只读**：指向工作区之外的符号链接要标出来、且不可展开——它们不是可读内容，
 *    看起来像普通目录会让人误判「Agent 能看到那些」。
 */

/** 草稿态（还没有会话）里先记下的工作区选择；首条消息建出会话后再登记。 */
export interface WorkspaceDraft {
  path: string;
  mode: WorkspaceMode;
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 用量文案：读不到就说读不到，**不显示成 0**（`doc/api.md` §5.19）。 */
export function describeUsage(workspace: Workspace): string {
  const usage = workspace.usage;
  if (!usage) return "未读取";
  if (!usage.available) return `读不到：${usage.reason ?? "工作区目录不可用"}`;
  const entries = usage.entries ?? 0;
  const bytes = formatBytes(usage.total_bytes ?? 0);
  const cap = `${bytes} / ${String(entries)} 项`;
  return usage.truncated ? `≥ ${cap}（扫描已截断）` : cap;
}

export function describeKind(kind: WorkspaceEntry["kind"]): string {
  switch (kind) {
    case "dir":
      return "目录";
    case "file":
      return "文件";
    case "symlink":
      return "符号链接";
    default:
      return "其它";
  }
}

const MODE_LABEL: Record<WorkspaceMode, string> = {
  read_only: "只读",
  workspace_write: "可写",
};

/** 单次请求最多带多少个文件：服务端 `WORKSPACE_IMPORT_MAX_FILES` 默认 200，这里更保守。 */
export const IMPORT_CHUNK_FILES = 50;
/** 与 `WORKSPACE_IMPORT_MAX_FILE_BYTES` 一致：20 MB。 */
export const IMPORT_MAX_FILE_BYTES = 20 * 1024 * 1024;

/**
 * 把 `<input webkitdirectory>` 给的文件列表整理成导入项。
 *
 * 路径取 `webkitRelativePath`（形如 `myproject/docs/a.md`）并**剥掉第一段**：用户选的是
 * 「我的工作文件夹」，其内容该落在工作区根下，而不是再套一层同名目录。
 */
export function folderTargets(files: readonly File[]): {
  targets: { path: string; file: File }[];
  oversized: string[];
} {
  const targets: { path: string; file: File }[] = [];
  const oversized: string[] = [];
  for (const file of files) {
    const relative = (file.webkitRelativePath || file.name).replace(/\\/g, "/");
    const segments = relative.split("/").filter(Boolean);
    const path = (segments.length > 1 ? segments.slice(1) : segments).join("/");
    if (!path) continue;
    if (file.size > IMPORT_MAX_FILE_BYTES) {
      oversized.push(path);
      continue;
    }
    targets.push({ path, file });
  }
  return { targets, oversized };
}

/**
 * 「选择文件夹位置」：**根内**的目录浏览器（ADR-033 §1 的「`/workspace` 内的目录选择器」）。
 *
 * 它补的是「登记路径只能盲打」这个缺口。能选的只有**服务端挂进来的那个根下面的子目录**：
 * 浏览器拿不到宿主路径、bind mount 又在容器创建时固定，所以这里不是「浏览你的磁盘」。
 * 真正的换根仍然是部署动作（`scripts/pick_work_dir.ps1`），界面上必须分清这两件事。
 *
 * 只列目录：这一层的产物是一个路径，把文件也铺出来只会让人误点。
 * 越界的符号链接**列出来但不可进**——它们不是可读内容，也不该看起来像普通目录。
 */
/**
 * **宿主**目录浏览器（ADR-035 §3）：用户当场选一个本机文件夹。
 *
 * 纯 props 驱动（容器在 `WorkspaceLocationPicker` 里取数），便于 rendercheck 直接挂载断言。
 * 这里显示的**是绝对路径**——宿主形态下路径本身就是用户在意的信息，不能再缩成相对路径。
 */
export function HostLocationBrowser({
  tree,
  loading,
  error,
  onNavigate,
  onRetry,
  onPick,
  onClose,
}: {
  tree: WorkspaceHostTree | null;
  loading: boolean;
  error: string;
  onNavigate: (path: string) => void;
  onRetry: () => void;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const dirs = (tree?.entries ?? []).filter((entry) => entry.kind === "dir");
  const current = tree?.path ?? "";

  return (
    <div className="cfg-ws-picker" role="group" aria-label="选择本机文件夹">
      <div className="cfg-ws-picker-head">
        <span className="cfg-ws-picker-crumbs">
          <FolderTree size={14} />
          <b>{current || "读取中…"}</b>
        </span>
        <button type="button" className="cfg-quiet" onClick={onClose}>
          收起
        </button>
      </div>

      <p className="cfg-hint">
        浏览的是后端所在机器上的文件夹。宿主直跑时那就是你这台电脑：选中的文件夹里
        Agent 可以读写，文件夹之外一律拒绝。
      </p>

      {error && (
        <p role="alert" className="cfg-alert">
          {error}{" "}
          <button type="button" className="cfg-quiet" onClick={onRetry}>
            重试
          </button>
        </p>
      )}
      {loading && <p className="cfg-hint">读取中…</p>}

      {!loading && !error && (
        <>
          <div className="cfg-ws-picker-jumps">
            {(tree?.roots ?? []).map((root) => (
              <button
                key={root.path}
                type="button"
                className="cfg-quiet"
                onClick={() => onNavigate(root.path)}
              >
                {root.name}
              </button>
            ))}
            {tree?.home && (
              <button
                type="button"
                className="cfg-quiet"
                onClick={() => onNavigate(tree.home)}
              >
                家目录
              </button>
            )}
            {tree?.parent && (
              <button
                type="button"
                className="cfg-quiet"
                onClick={() => onNavigate(tree.parent as string)}
              >
                ↑ 上一级
              </button>
            )}
          </div>

          {dirs.length === 0 ? (
            <p className="cfg-hint">这一层没有子文件夹，可以直接选定当前位置。</p>
          ) : (
            <ul className="cfg-ws-picker-list">
              {dirs.map((entry) => (
                <li key={entry.path}>
                  <button
                    type="button"
                    disabled={entry.outside}
                    title={entry.outside ? "指向别处的符号链接：不跟随" : entry.path}
                    onClick={() => onNavigate(entry.path)}
                  >
                    <FolderTree size={14} />
                    {entry.name}
                  </button>
                  {entry.outside && <Chip tone="amber">链接</Chip>}
                </li>
              ))}
            </ul>
          )}
          {tree?.truncated && (
            <p className="cfg-hint">条目数超过 {tree.limit}，只列出前面一部分。</p>
          )}

          <div className="cfg-ws-picker-foot">
            <button type="button" className="cfg-primary" onClick={() => onPick(current)}>
              选定此文件夹
            </button>
            <span className="cfg-hint">将登记 {current}</span>
          </div>
        </>
      )}
    </div>
  );
}

export function WorkspaceLocationPicker({
  onPick,
  onClose,
}: {
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [tree, setTree] = useState<WorkspaceRootTree | null>(null);
  const [current, setCurrent] = useState("");
  const [host, setHost] = useState<WorkspaceHostTree | null>(null);
  /**
   * 形态判定：先问**宿主**目录（默认形态就是宿主直跑，ADR-035 §2），
   * 容器形态会以 503 `WORKSPACE_HOST_BROWSE_DISABLED` 拒绝，那时才退回根内浏览。
   * 按错误码判、而不是"两个接口都试一遍"，是为了默认路径只发一次请求。
   */
  const [mode, setMode] = useState<"probing" | "host" | "root">("probing");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadRoot = useCallback(async (path: string) => {
    setLoading(true);
    setError("");
    try {
      const value = await api.workspaceRootTree(path, 1);
      setTree(value);
      setCurrent(value.path);
    } catch (cause) {
      setTree(null);
      setError(describeError(cause, "读取工作区根失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadHost = useCallback(
    async (path?: string) => {
      setLoading(true);
      setError("");
      try {
        const value = await api.hostTree(path, 1);
        setHost(value);
        setMode("host");
        setLoading(false);
        return;
      } catch (cause) {
        if (cause instanceof ApiError && cause.code === "WORKSPACE_HOST_BROWSE_DISABLED") {
          // 容器形态：容器里看不到宿主路径，退回根内浏览。
          setMode("root");
          setLoading(false);
          await loadRoot("");
          return;
        }
        setHost(null);
        setError(describeError(cause, "读取目录失败"));
        setLoading(false);
      }
    },
    [loadRoot],
  );

  useEffect(() => {
    void loadHost();
  }, [loadHost]);

  if (mode === "host") {
    return (
      <HostLocationBrowser
        tree={host}
        loading={loading}
        error={error}
        onNavigate={(path) => void loadHost(path)}
        onRetry={() => void loadHost(host?.path)}
        onPick={onPick}
        onClose={onClose}
      />
    );
  }

  const segments = current.split("/").filter(Boolean);
  const parent = segments.slice(0, -1).join("/");
  // 目录已按「目录优先」排过，这里再筛一次：路径选择只对目录有意义。
  const dirs = (tree?.entries ?? []).filter((entry) => entry.kind === "dir");

  return (
    <div className="cfg-ws-picker" role="group" aria-label="选择工作区位置">
      <div className="cfg-ws-picker-head">
        <span className="cfg-ws-picker-crumbs">
          <button type="button" className="cfg-quiet" onClick={() => void loadRoot("")}>
            根
          </button>
          {segments.map((segment, index) => (
            <Fragment key={segments.slice(0, index + 1).join("/")}>
              <span aria-hidden="true">/</span>
              <button
                type="button"
                className="cfg-quiet"
                onClick={() => void loadRoot(segments.slice(0, index + 1).join("/"))}
              >
                {segment}
              </button>
            </Fragment>
          ))}
        </span>
        <button type="button" className="cfg-quiet" onClick={onClose}>
          收起
        </button>
      </div>

      {error && (
        <p role="alert" className="cfg-alert">
          {error}{" "}
          <button type="button" className="cfg-quiet" onClick={() => void loadRoot(current)}>
            重试
          </button>
        </p>
      )}
      {loading && <p className="cfg-hint">读取中…</p>}

      {!loading && !error && (
        <>
          {segments.length > 0 && (
            <button
              type="button"
              className="cfg-quiet"
              onClick={() => void loadRoot(parent)}
            >
              ↑ 上一级
            </button>
          )}
          {dirs.length === 0 ? (
            <p className="cfg-hint">这一层没有子目录，可以直接选定当前位置。</p>
          ) : (
            <ul className="cfg-ws-picker-list">
              {dirs.map((entry) => (
                <li key={entry.path}>
                  <button
                    type="button"
                    disabled={entry.outside}
                    title={entry.outside ? "指向工作区之外的符号链接：不跟随" : entry.path}
                    onClick={() => void loadRoot(entry.path)}
                  >
                    <FolderTree size={14} />
                    {entry.name}
                  </button>
                  {entry.outside && <Chip tone="amber">工作区外</Chip>}
                </li>
              ))}
            </ul>
          )}
          {tree?.truncated && (
            <p className="cfg-hint">
              条目数超过 {tree.limit}，只列出前面一部分。
            </p>
          )}
          <div className="cfg-ws-picker-foot">
            <button type="button" className="cfg-primary" onClick={() => onPick(current)}>
              选定此文件夹
            </button>
            <span className="cfg-hint">
              {current ? `将登记 ${current}` : "将登记工作区根"}
            </span>
          </div>
        </>
      )}
    </div>
  );
}

function TreeList({ entries, depth = 0 }: { entries: WorkspaceEntry[]; depth?: number }) {
  return (
    <ul className="cfg-ws-tree" data-depth={depth}>
      {entries.map((entry) => (
        <li key={entry.path || entry.name} className={entry.outside ? "outside" : undefined}>
          <span className="cfg-ws-tree-name">{entry.name}</span>
          <span className="cfg-ws-tree-facts">
            {describeKind(entry.kind)}
            {entry.kind === "file" && entry.size_bytes !== null && ` · ${formatBytes(entry.size_bytes)}`}
          </span>
          {entry.outside && (
            <Chip tone="amber" title="指向工作区之外的符号链接：不跟随、不可读">
              工作区外
            </Chip>
          )}
          {/* 越界链接不展开：跟随它就等于把根外的目录结构展示出来。 */}
          {!entry.outside && (entry.children?.length ?? 0) > 0 && (
            <TreeList entries={entry.children ?? []} depth={depth + 1} />
          )}
        </li>
      ))}
    </ul>
  );
}

/** 纯 props 驱动的展示体，便于 `frontend/rendercheck` 直接挂载断言。 */
export function WorkspaceBoundary({
  workspaces,
  selectedId,
  tree,
  treeError,
  sessionId = null,
  draft = null,
  busy = false,
  importBusy = false,
  notice,
  onCreate,
  onSelect,
  onToggleMode,
  onDelete,
  onImportFolder,
  onClearDraft,
}: {
  workspaces: Workspace[];
  selectedId: string | null;
  tree: WorkspaceTree | null;
  treeError?: string;
  /** 当前会话 id；`null` = 草稿态（还没发第一条消息）。 */
  sessionId?: string | null;
  /** 草稿态里暂存的选择。 */
  draft?: WorkspaceDraft | null;
  busy?: boolean;
  importBusy?: boolean;
  notice?: NoticeState;
  onCreate?: (path: string, mode: WorkspaceMode) => void;
  onSelect?: (id: string) => void;
  onToggleMode?: (workspace: Workspace) => void;
  onDelete?: (workspace: Workspace) => void;
  /** 「选择文件夹」：拿到浏览器给的 FileList（相对路径 + 内容），由容器上传。 */
  onImportFolder?: (files: FileList, overwrite: boolean) => void;
  onClearDraft?: () => void;
}) {
  const selected = workspaces.find((item) => item.id === selectedId) ?? null;
  const [path, setPath] = useState("");
  const [mode, setMode] = useState<WorkspaceMode>("read_only");
  const [overwrite, setOverwrite] = useState(false);
  /** 「浏览根目录」展开态：选位置是**看一眼再填**，不展开就不发那次请求。 */
  const [picking, setPicking] = useState(false);
  const folderPicker = useRef<HTMLInputElement>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    onCreate?.(path.trim(), mode);
    setPath("");
  };

  return (
    <section className="cfg-block">
      <div className="cfg-block-head">
        <div className="cfg-block-title">
          <HardDrive size={16} />
          <div>
            <h3>工作区</h3>
            <p>
              Agent 的文件业务限定在这里，而且按会话生效：绑定的那个工作区，它的文件工具
              才会进这次执行的工具集。<b>一个会话只绑定一个工作区</b>，再登记就是改绑
              （旧绑定自动解除）。宿主形态下这里的路径就是本机那个文件夹。
            </p>
          </div>
        </div>
        <div className="cfg-row-actions">
          <Chip tone="slate">{workspaces.length} 个</Chip>
        </div>
      </div>

      <NoticeBar notice={notice ?? null} />

      {/* 草稿态与已绑定的区别必须写在界面上：否则用户以为「已经选好了」，
          而真正的登记要等首条消息。 */}
      {!sessionId && (
        <p className="cfg-hint">
          当前是草稿态（还没有会话）：这里选的路径与档位先记在草稿里，
          <b>发出第一条消息</b>、会话建好之后才登记并绑定。
        </p>
      )}
      {!sessionId && draft && (
        <p className="cfg-hint">
          草稿工作区：<b>{draft.path || "/（工作区根）"}</b> · {MODE_LABEL[draft.mode]}
          <button type="button" className="cfg-quiet" onClick={() => onClearDraft?.()}>
            清除
          </button>
        </p>
      )}

      {workspaces.length === 0 ? (
        // 草稿态的措辞不能说成「还没有登记工作区」：草稿态根本没列过列表，
        // 这句话会让人以为库里的登记没了（它们只是不属于本会话）。
        sessionId ? (
          <EmptyState
            title="还没有登记工作区"
            hint="留空路径登记时，服务端会按会话建 sessions/<会话 id>/。"
          />
        ) : (
          <EmptyState
            title="还没有选工作区"
            hint="在下面填好路径与档位：发出第一条消息时登记并绑定本会话。留空路径 = sessions/<会话 id>/。"
          />
        )
      ) : (
        <ul className="cfg-ws-list">
          {workspaces.map((workspace) => (
            <li
              key={workspace.id}
              className={`cfg-ws-item${workspace.id === selectedId ? " active" : ""}`}
            >
              <button type="button" className="cfg-ws-open" onClick={() => onSelect?.(workspace.id)}>
                <b>{workspace.path || "/（工作区根）"}</b>
                <span className="cfg-ws-meta">
                  <Chip tone={workspace.mode === "workspace_write" ? "teal" : "slate"}>
                    {MODE_LABEL[workspace.mode]}
                  </Chip>
                  {workspace.session_id && <Chip tone="violet">会话绑定</Chip>}
                  <span className="cfg-hint">用量 {describeUsage(workspace)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <form className="cfg-form-grid" onSubmit={submit}>
        <Field
          label="登记路径"
          hint="宿主形态下选本机文件夹（点「浏览根目录」逐层选）；容器形态下写根内相对路径，如 project/reports。留空 = 按会话自动建 sessions/<会话 id>/。再登记会替换本会话当前的绑定。"
        >
          <input
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="project/reports"
          />
          <button
            type="button"
            className="cfg-quiet"
            aria-expanded={picking}
            onClick={() => setPicking((value) => !value)}
          >
            {picking ? "收起目录浏览" : "浏览根目录"}
          </button>
          {/* 浏览范围随形态变（宿主＝后端所在机器上的文件夹；容器＝挂进来的根），
              所以措辞交给选择器自己讲——这里写死一句必然会在另一种形态下说错。 */}
          {picking && (
            <WorkspaceLocationPicker
              onPick={(picked) => {
                setPath(picked);
                setPicking(false);
              }}
              onClose={() => setPicking(false)}
            />
          )}
        </Field>
        <Field
          label="档位"
          hint="默认只读：Agent 只能看。可写档位会让本会话多出三个写工具（新建 / 建目录 / 移动）。"
        >
          <select value={mode} onChange={(event) => setMode(event.target.value as WorkspaceMode)}>
            <option value="read_only">只读（read_only）</option>
            <option value="workspace_write">可写（workspace_write）</option>
          </select>
        </Field>
        <div className="cfg-actions">
          <button type="submit" className="cfg-primary" disabled={busy}>
            登记工作区
          </button>
        </div>
      </form>

      {selected && (
        <div className="cfg-ws-detail">
          <div className="cfg-block-head">
            <div className="cfg-block-title">
              <FolderTree size={16} />
              <div>
                <h3>{selected.path || "/（工作区根）"}</h3>
                <p>
                  配额 {formatBytes(selected.quota.max_total_bytes)} /{" "}
                  {selected.quota.max_entries} 项 · 单文件上限{" "}
                  {formatBytes(selected.quota.max_file_bytes)}。
                </p>
              </div>
            </div>
            <div className="cfg-row-actions">
              <label className="cfg-check" title="同名文件已存在时是否覆盖">
                <input
                  type="checkbox"
                  checked={overwrite}
                  onChange={(event) => setOverwrite(event.target.checked)}
                />
                覆盖同名
              </label>
              <button
                type="button"
                className="cfg-quiet"
                disabled={busy || importBusy}
                onClick={() => folderPicker.current?.click()}
              >
                {importBusy ? "导入中…" : "选择文件夹并导入"}
              </button>
              <button
                type="button"
                className="cfg-quiet"
                disabled={busy}
                onClick={() => onToggleMode?.(selected)}
              >
                {selected.mode === "workspace_write" ? "降为只读" : "提档为可写"}
              </button>
              <InlineConfirm
                label="解除登记"
                question="解除登记？（不删宿主文件）"
                triggerLabel="解除登记"
                triggerClassName="cfg-quiet"
                onConfirm={() => onDelete?.(selected)}
                disabled={busy}
              >
                解除登记
              </InlineConfirm>
            </div>
          </div>

          {/* 浏览器只能给相对路径 + 内容，所以这里做的是**导入副本**；文案必须说清楚。 */}
          <input
            ref={folderPicker}
            type="file"
            multiple
            tabIndex={-1}
            aria-hidden="true"
            className="cfg-file-input-proxy"
            onChange={(event) => {
              const files = event.target.files;
              if (files && files.length) onImportFolder?.(files, overwrite);
              // 允许重复选同一个目录（否则第二次 change 不触发）。
              event.target.value = "";
            }}
            {...({ webkitdirectory: "true", directory: "true" } as Record<string, string>)}
          />

          <dl className="cfg-facts">
            <div>
              <dt>档位</dt>
              <dd>{MODE_LABEL[selected.mode]}</dd>
            </div>
            <div>
              <dt>用量</dt>
              <dd>{describeUsage(selected)}</dd>
            </div>
            <div>
              <dt>提档人</dt>
              <dd>{selected.updated_by ?? "—"}</dd>
            </div>
            <div>
              <dt>登记时间</dt>
              <dd>{formatTime(selected.created_at)}</dd>
            </div>
          </dl>

          <p className="cfg-hint">
            覆盖与删除需要人工审批：Agent 第一次调用只会提交申请，批准后它重试同一调用才执行。
            审批入口在对话流的执行过程里。
          </p>
          <p className="cfg-hint">
            「选择文件夹并导入」把本机那个文件夹的内容**复制**进工作区（浏览器拿不到宿主路径，
            所以只能传内容）；想让 Agent 直接操作你本机那个目录，用宿主侧脚本
            <code> scripts/pick_work_dir.ps1</code> 把目录挂进来。
          </p>

          {treeError && <p className="cfg-alert">{treeError}</p>}
          {!treeError && !tree && <p className="cfg-hint">读取目录树中…</p>}
          {!treeError && tree && tree.entries.length === 0 && (
            <p className="cfg-hint">这个目录是空的。</p>
          )}
          {!treeError && tree && tree.entries.length > 0 && (
            <TreeList entries={tree.entries} />
          )}
          {tree?.truncated && (
            <p className="cfg-hint">
              条目数超过 {tree.limit}，只展示了前面一部分。
            </p>
          )}
        </div>
      )}

      <p className="cfg-hint">
        <ShieldAlert size={14} /> 没有「完整磁盘访问」这一档：需要更大范围只能由部署者改
        `WORKSPACE_HOST_ROOT`（ADR-033 §2）。
      </p>
    </section>
  );
}

/** 容器：负责取数与动作，展示交给 `WorkspaceBoundary`。 */
export function WorkspacePanel({
  sessionId,
  draft,
  onDraftChange,
}: {
  /** 当前会话 id；`null` = 草稿态（还没发第一条消息）。 */
  sessionId: string | null;
  draft: WorkspaceDraft | null;
  onDraftChange: (draft: WorkspaceDraft | null) => void;
}) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tree, setTree] = useState<WorkspaceTree | null>(null);
  const [treeError, setTreeError] = useState("");
  const [busy, setBusy] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);

  const loadTree = useCallback(async (id: string) => {
    setTree(null);
    setTreeError("");
    try {
      setTree(await api.workspaceTree(id, "", 2));
    } catch (cause) {
      setTreeError(describeError(cause, "目录树读取失败"));
    }
  }, []);

  const reload = useCallback(
    async (select?: string) => {
      // 没有会话就不问服务端：草稿态下 `GET /workspaces` 返回的是**所有**登记，
      // 摆在这里会让人以为「已经选好了」。草稿的选择只存在于本地 draft 里。
      if (!sessionId) {
        setWorkspaces([]);
        setSelectedId(null);
        setTree(null);
        setTreeError("");
        return;
      }
      try {
        const listing = await api.listWorkspaces(sessionId);
        setWorkspaces(listing.items);
        const next = select ?? selectedId;
        if (next && listing.items.some((item) => item.id === next)) {
          setSelectedId(next);
          await loadTree(next);
        } else if (listing.items.length) {
          setSelectedId(listing.items[0].id);
          await loadTree(listing.items[0].id);
        } else {
          setSelectedId(null);
          setTree(null);
        }
      } catch (cause) {
        setNotice({ tone: "bad", text: describeError(cause, "工作区列表读取失败") });
      }
    },
    [loadTree, selectedId, sessionId],
  );

  useEffect(() => {
    void reload();
    // 会话切换时重拉（草稿 → 首条消息建出会话，这时才可能有绑定结果）；
    // 其余刷新一律由动作触发，避免与其它视图抢轮询。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const create = async (path: string, mode: WorkspaceMode) => {
    // 草稿态只暂存：这里就发 `POST /workspaces` 的话，拿到的是 `session_id=null`
    // 的记录——会话级注册表不会选它，Agent 一个文件工具也拿不到。
    if (!sessionId) {
      onDraftChange({ path, mode });
      setNotice({
        tone: "info",
        text: `已记在草稿：${path || "/（工作区根）"} · ${MODE_LABEL[mode]}。发出第一条消息时登记并绑定本会话。`,
      });
      return;
    }
    setBusy(true);
    try {
      // 改绑前的绑定：登记成功后要如实告诉使用者"原来那个已经解除"——静默换掉等于让人
      // 以为两个目录都还挂着，而执行侧只可能用一个。按 **id** 比较，不比较路径字符串：
      // 同一个目录可能有多种写法，而 id 是权威的。
      const previous = workspaces[0] ?? null;
      const created = await api.createWorkspace({
        session_id: sessionId,
        path: path || null,
        mode,
      });
      const rebound =
        previous && previous.id !== created.id
          ? `（原 ${truncate(previous.path || "/（工作区根）", 40)} 已解除）`
          : "";
      setNotice({
        tone: "ok",
        text: `已登记并绑定本会话：${created.path || "/（工作区根）"}${rebound}`,
      });
      await reload(created.id);
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "登记失败") });
    } finally {
      setBusy(false);
    }
  };

  const toggleMode = async (workspace: Workspace) => {
    const next: WorkspaceMode = workspace.mode === "workspace_write" ? "read_only" : "workspace_write";
    setBusy(true);
    try {
      await api.patchWorkspace(workspace.id, { mode: next });
      setNotice({
        tone: "ok",
        text:
          next === "workspace_write"
            ? "已提档为可写：本会话的 Agent 多出三个写工具（新建 / 建目录 / 移动）"
            : "已降回只读：写工具从本会话的工具集里移除",
      });
      await reload(workspace.id);
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "档位调整失败") });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (workspace: Workspace) => {
    setBusy(true);
    try {
      await api.deleteWorkspace(workspace.id);
      setNotice({ tone: "ok", text: `已解除登记：${truncate(workspace.path || "/", 40)}（宿主文件未删）` });
      await reload();
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "解除登记失败") });
    } finally {
      setBusy(false);
    }
  };

  /**
   * 「选择文件夹并导入」：把浏览器给的文件（相对路径 + 内容）分片传上去。
   *
   * 分片是为了不让一次请求带上成千上万个文件；单文件上限与配额由服务端兜底，
   * 这里先做一遍**本地预筛**，让用户立刻看到"哪些没进来、为什么"。
   */
  const importFolder = async (files: FileList, overwrite: boolean) => {
    if (!selectedId) return;
    setImportBusy(true);
    try {
      const { targets, oversized } = folderTargets(Array.from(files));
      if (!targets.length) {
        setNotice({
          tone: "bad",
          text: oversized.length
            ? `全部文件都超过 ${formatBytes(IMPORT_MAX_FILE_BYTES)}，没有可导入的内容`
            : "这个文件夹里没有文件",
        });
        return;
      }
      let imported = 0;
      let skipped = 0;
      let failed = 0;
      for (let index = 0; index < targets.length; index += IMPORT_CHUNK_FILES) {
        const chunk = targets.slice(index, index + IMPORT_CHUNK_FILES);
        const payload: WorkspaceImportFile[] = [];
        for (const target of chunk) {
          payload.push({
            path: target.path,
            content_base64: await fileToBase64(target.file),
          });
        }
        const result = await api.importWorkspaceFiles(selectedId, payload, overwrite);
        imported += result.imported;
        skipped += result.skipped;
        failed += result.failed;
      }
      const parts = [`已导入 ${imported} 个文件`];
      if (skipped) {
        parts.push(
          `跳过 ${skipped} 个（同名已存在${overwrite ? "" : "；要覆盖请勾选「覆盖同名」"}）`,
        );
      }
      if (failed) parts.push(`失败 ${failed} 个`);
      if (oversized.length) {
        parts.push(`超过 ${formatBytes(IMPORT_MAX_FILE_BYTES)} 的 ${oversized.length} 个未导入`);
      }
      setNotice({ tone: skipped || failed || oversized.length ? "info" : "ok", text: parts.join("；") });
      await reload(selectedId);
    } catch (cause) {
      setNotice({ tone: "bad", text: describeError(cause, "导入失败") });
    } finally {
      setImportBusy(false);
    }
  };

  return (
    <WorkspaceBoundary
      workspaces={workspaces}
      selectedId={selectedId}
      tree={tree}
      treeError={treeError}
      sessionId={sessionId}
      draft={draft}
      busy={busy}
      importBusy={importBusy}
      notice={notice}
      onCreate={(path, mode) => void create(path, mode)}
      onClearDraft={() => onDraftChange(null)}
      onSelect={(id) => {
        setSelectedId(id);
        void loadTree(id);
      }}
      onToggleMode={(workspace) => void toggleMode(workspace)}
      onDelete={(workspace) => void remove(workspace)}
      onImportFolder={(files, overwrite) => void importFolder(files, overwrite)}
    />
  );
}
