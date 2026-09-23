import { useMemo } from "react";
import { Boxes, Plug, TriangleAlert } from "lucide-react";
import { Chip, EmptyState, Switch } from "./shared";

/**
 * 角色工具授权（ADR-035，契约见 `doc/api.md` §5.7 的 `tool_names`）。
 *
 * 三态语义：
 * - `null`：未配置 → 该角色可以用工具目录里的**全部**工具（界面上叫「不受限」）；
 * - `[]`：显式取消全部授权 → 一个工具也用不了（会话级附件工具除外）；
 * - 非空数组：只允许这些。
 *
 * 两组「不受限」的分辨，是这一块最容易被做错的地方：
 *
 * 1. **不受限 ≠ 面板空白**。不受限时也要把清单画出来（勾选、但不可编辑），
 *    否则「不受限」等于让用户去别的页面自己数到底放行了什么。
 * 2. **不受限 ≠ 「按名单选了全部」**。前者是跟随目录（新登记的工具自动可用），
 *    后者是快照（名单冻住，后加的工具不会被授权）。所以切到「按名单」时用当前目录
 *    预填，并在界面上说清这一点——否则用户看到两条路径长得一样，却有两种未来行为。
 *
 * 会话级附件工具（`list_session_files` / `read_session_file`）**不在这里**，也不参与
 * 授权——它们不进工具目录（§5.3），所以界面上不会出现一个关不掉、又只在有附件的
 * 会话里存在的开关。
 */

/** 可按来源分组的工具目录。 */
export interface ToolCatalogEntry {
  name: string;
  description: string;
  /** 全局层面是否可用：被 MCP 配置里 `tool_options.disabled` 停用的工具在这里为 `false`。 */
  available: boolean;
  /** 不可用的具体原因，原样显示；不换成「不可用」三个字。 */
  unavailableReason: string | null;
}

export interface ToolCatalogGroup {
  /** `builtin` 或 MCP Server 的 id。 */
  id: string;
  label: string;
  /** 分组来源说明（内置 / 某个 Server）。 */
  source: "builtin" | "mcp";
  tools: ToolCatalogEntry[];
}

/** 目录里全部工具名，按分组顺序。切到「按名单」时用它预填。 */
export function allToolNames(groups: readonly ToolCatalogGroup[]): string[] {
  return groups.flatMap((group) => group.tools.map((tool) => tool.name));
}

/** 把工具目录（§5.3）与 MCP 紧凑工具目录（§5.11）合成前端要渲染的分组。 */
export function buildToolCatalogGroups(
  catalog: readonly { name: string; description: string }[] | null,
  mcp: {
    items: readonly {
      name: string;
      description: string;
      tool_enabled: boolean;
      enabled: boolean;
      server_id: string;
    }[];
    servers: readonly { id: string; name: string }[];
  } | null,
): ToolCatalogGroup[] {
  const mcpTools = mcp?.items ?? [];
  const mcpNames = new Set(mcpTools.map((item) => item.name));
  // 目录里除 MCP 工具之外的就是平台内置工具（§5.3 的目录 = 内置 + 已发现的登记工具）。
  const builtin = (catalog ?? []).filter((tool) => !mcpNames.has(tool.name));

  const groups: ToolCatalogGroup[] = [];

  if (builtin.length) {
    groups.push({
      id: "builtin",
      label: "内置工具",
      source: "builtin",
      tools: builtin.map((tool) => ({
        name: tool.name,
        description: tool.description,
        available: true,
        unavailableReason: null,
      })),
    });
  }

  for (const server of [...(mcp?.servers ?? [])].sort((a, b) => a.name.localeCompare(b.name))) {
    const tools = mcpTools.filter((item) => item.server_id === server.id);
    if (!tools.length) continue;
    groups.push({
      id: server.id,
      label: `MCP · ${server.name}`,
      source: "mcp",
      tools: tools.map((item) => ({
        name: item.name,
        description: item.description,
        // 两级都要成立：Server 启用 + 该工具没被 `tool_options.disabled` 停用。
        available: item.enabled && item.tool_enabled,
        unavailableReason: !item.enabled
          ? "所属 MCP Server 已在配置里停用"
          : !item.tool_enabled
            ? "已在 MCP 配置里停用该工具（tool_options.disabled）"
            : null,
      })),
    });
  }

  return groups;
}

/**
 * 名单里有、但当前目录里没有的名字。
 *
 * 必须单独列出来：这些名字在保存时**不能被丢掉**。丢掉它们等于用户点一次保存就
 * 悄悄改了他没碰过的配置——而这类名字是有真实来路的：Server 临时离线导致目录
 * 暂时为空、或者名字是先前配置留下的。想删就让用户自己取消勾选。
 */
export function orphanToolNames(
  selected: readonly string[],
  groups: readonly ToolCatalogGroup[],
): string[] {
  const known = new Set(groups.flatMap((group) => group.tools.map((tool) => tool.name)));
  return selected.filter((name) => !known.has(name));
}

function ToolRow({
  entry,
  checked,
  restricted,
  onToggle,
}: {
  entry: ToolCatalogEntry;
  checked: boolean;
  restricted: boolean;
  onToggle: (next: boolean) => void;
}) {
  // 全局停用的工具：已勾选的可以取消（否则名单里会留一个去不掉的项），
  // 未勾选的不许勾上（勾了也不生效，放行只会制造「配了却没出现」的假象）。
  const locked = !restricted || (!entry.available && !checked);
  return (
    <article className={`cfg-tool-card${checked && entry.available ? "" : " off"}`}>
      <div className="cfg-tool-main">
        <input
          type="checkbox"
          className="cfg-tool-check"
          checked={checked}
          disabled={locked}
          aria-label={`授权工具 ${entry.name}`}
          onChange={(event) => onToggle(event.target.checked)}
        />
        <div className="cfg-tool-text">
          <b>{entry.name}</b>
          <small title={entry.description}>
            {entry.unavailableReason ?? entry.description ?? "（该工具没有提供说明）"}
          </small>
        </div>
        <div className="cfg-tool-badges">
          {!restricted && <Chip tone="slate">随目录自动生效</Chip>}
          {restricted && checked && !entry.available && <Chip tone="amber">全局停用</Chip>}
          {restricted && !checked && entry.available && <Chip tone="slate">未授权</Chip>}
        </div>
      </div>
    </article>
  );
}

/**
 * 角色工具授权面板（props 驱动，供 `frontend/rendercheck/config-smoke.tsx` 直接挂载）。
 *
 * 不受控组件：勾选状态由父组件持有，本面板只发出意图。这样「保存」时能一次把
 * `tool_names` 与其余六个字段合成同一个 patch，不会出现两套保存路径。
 */
export function AgentToolsPanel({
  groups,
  groupsError,
  restricted,
  selected,
  onToggle,
  onRestrictedChange,
  onSelectAll,
  onSelectNone,
  onReload,
}: {
  groups: readonly ToolCatalogGroup[];
  /** 目录读取失败的原因；非空时不允许编辑（否则保存会把这份名单算成空）。 */
  groupsError: string;
  restricted: boolean;
  selected: readonly string[];
  onToggle: (name: string, next: boolean) => void;
  onRestrictedChange: (restricted: boolean) => void;
  onSelectAll: () => void;
  onSelectNone: () => void;
  /** 重新读取目录。必须给：读失败之后把用户卡在这里，等于逼他关掉弹窗再点一次。 */
  onReload: () => void;
}) {
  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const orphans = useMemo(() => orphanToolNames(selected, groups), [selected, groups]);
  const total = groups.reduce((sum, group) => sum + group.tools.length, 0);
  const blocked = groupsError.length > 0;
  // 受限态下勾选数才是「生效的工具数」；不受限态下生效的是整个目录。
  const effectiveCount = restricted ? selectedSet.size : total;

  return (
    <section className="cfg-tools" aria-label="角色工具授权">
      <div className="cfg-tools-head">
        <div>
          <b>
            工具授权
            <span className="cfg-count">
              {effectiveCount} / {total} 个生效
            </span>
          </b>
          <small>
            {restricted
              ? "只有勾选的工具会绑给这个角色：模型看不到它，也调不动它。名单是快照，之后新登记的工具需要回来手动勾上。"
              : "当前不受限：该角色可以用工具目录里的全部工具，新登记的工具也会自动可用。"}
          </small>
        </div>
        <div className="cfg-inline-switch">
          <span>按名单授权</span>
          <Switch
            checked={restricted}
            onChange={onRestrictedChange}
            disabled={blocked}
            label="按名单授权工具"
            title="开启后只放行勾选的工具；关闭则回到「工具目录里的全部工具」"
          />
        </div>
      </div>

      {blocked && (
        <p role="alert" className="cfg-alert">
          {groupsError}（工具目录读不到时不允许编辑，否则保存会把这份名单算成空）
          <button type="button" className="cfg-quiet" onClick={onReload}>
            重新读取工具目录
          </button>
        </p>
      )}

      {!blocked && restricted && (
        <div className="cfg-tools-actions">
          <button type="button" className="cfg-quiet" onClick={onSelectAll}>
            全选
          </button>
          <button type="button" className="cfg-quiet" onClick={onSelectNone}>
            全不选
          </button>
          {selectedSet.size === 0 ? (
            <small className="cfg-tools-warn">
              <TriangleAlert size={12} aria-hidden="true" />
              名单是空的：保存后这个角色将没有任何工具可用。要恢复默认请关掉「按名单授权」。
            </small>
          ) : (
            <small>取消「按名单授权」会清除白名单，回退到「可用全部工具」。</small>
          )}
        </div>
      )}

      {!groups.length && !blocked && (
        <EmptyState
          title="工具目录是空的"
          hint="先在「工具与配置 → MCP 工具」里登记并「发现」一个 Server，或确认内置工具注册表已加载。"
        />
      )}

      {groups.map((group) => (
        <div className="cfg-tool-group" key={group.id}>
          <div className="cfg-tool-group-head">
            {group.source === "mcp" ? (
              <Plug size={12} aria-hidden="true" />
            ) : (
              <Boxes size={12} aria-hidden="true" />
            )}
            <b>{group.label}</b>
            <span className="cfg-count">{group.tools.length} 个</span>
          </div>
          <div className="cfg-tool-list">
            {group.tools.map((entry) => (
              <ToolRow
                key={entry.name}
                entry={entry}
                // 不受限时全部画成已勾选且不可编辑——它表达的就是「全部生效」。
                checked={restricted ? selectedSet.has(entry.name) : true}
                restricted={restricted}
                onToggle={(next) => onToggle(entry.name, next)}
              />
            ))}
          </div>
        </div>
      ))}

      {orphans.length > 0 && (
        <div className="cfg-tool-group warn">
          <div className="cfg-tool-group-head">
            <TriangleAlert size={12} aria-hidden="true" />
            <b>名单里有、目录里没有</b>
            <span className="cfg-count">{orphans.length} 个</span>
          </div>
          <p className="cfg-hint">
            这些名字仍在授权名单里，保存时<b>原样保留</b>。目录里没有，通常是因为那个 MCP
            Server 当前没被发现（离线 / 还没点过「发现」）——只有取消勾选才会真的从名单里去掉。
          </p>
          <div className="cfg-tool-list">
            {orphans.map((name) => (
              <ToolRow
                key={name}
                entry={{
                  name,
                  description: "",
                  available: false,
                  unavailableReason: "当前工具目录里没有这个名字",
                }}
                checked
                restricted={restricted}
                onToggle={(next) => onToggle(name, next)}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

export default AgentToolsPanel;
