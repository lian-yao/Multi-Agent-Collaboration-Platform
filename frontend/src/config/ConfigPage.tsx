import { useCallback, useEffect, useState } from "react";
import { Settings2, Sliders, Route, Boxes, Plug, ShieldCheck, Brain } from "lucide-react";
import { api } from "../api/client";
import { PageTabs, type PageTab } from "../components/PageTabs";
import type { ProviderPresetCatalog } from "../types/api";
import { describeError } from "./shared";
import { ProviderPanel } from "./ProviderPanel";
import { DefaultRoutePanel } from "./DefaultRoutePanel";
import { McpPanel } from "./McpPanel";
import { InternalToolsPanel } from "./InternalToolsPanel";
import { SearchChannelPanel } from "./SearchChannelPanel";
import { SandboxPanel } from "./SandboxPanel";
import { EgressPanel } from "./EgressPanel";
import { MemoryPanel } from "./MemoryPanel";
import "./config.css";

export type ConfigTabId = "providers" | "defaults" | "tools" | "mcp" | "memory" | "sandbox";

/**
 * 六个分区的职责边界（ADR-017 / ADR-018 / doc/api.md §7）：
 * 同一份数据只在一个分区里写，别的页面只读。
 * - providers：多端点登记、批量引入模型、逐条特化调参
 * - defaults：兜底模型与 legacy 五列
 * - tools：搜索渠道（§5.24）与平台内置工具目录（§5.3）——目录进入即读取，含输入 Schema
 * - mcp：多 Server 与紧凑工具卡片
 * - memory：长期记忆（§5.22、ADR-036）——**只列与删**，写入回到对话里的 `记住：…`
 * - sandbox：执行边界 + 出网策略（§5.15 / §5.21），**只读**（都是部署期安全边界）
 *
 * 「工作区」不在这里：它按 `session_id` 生效，入口跟着会话走 —— 工作台顶栏的
 * 「工作区」抽屉（`workspace/WorkspacePanel.tsx`，`doc/api.md` §7.1）。
 */
export const CONFIG_TABS: readonly PageTab<ConfigTabId>[] = [
  {
    id: "providers",
    label: "Provider",
    icon: Sliders,
    title: "多端点登记、批量引入模型、逐条特化调参",
  },
  { id: "defaults", label: "默认路由", icon: Route, title: "兜底模型与 legacy 五列" },
  {
    id: "tools",
    label: "内部工具",
    icon: Boxes,
    title: "搜索渠道（§5.24）与内置工具目录（§5.3）",
  },
  { id: "mcp", label: "MCP 工具", icon: Plug, title: "多 Server 与紧凑工具卡片" },
  {
    id: "memory",
    label: "记忆",
    icon: Brain,
    title: "平台跨会话记住的偏好与信息（看得见、能收回）",
  },
  {
    id: "sandbox",
    label: "执行边界",
    icon: ShieldCheck,
    title: "沙箱与出网策略：当前边界是什么、为什么不生效（只读）",
  },
];

/**
 * 工具与配置页。
 *
 * 版式与「Agent 团队」「任务记录」一致：标题与副路由左对齐全宽，其下内容
 * 限宽 1180px 居中（styles.css 的 `.config-page > :not(...)` 规则）——
 * 内容拉满整行会让 registry 详情卡在宽屏下长得离谱。
 */
export function ConfigPage() {
  const [tab, setTab] = useState<ConfigTabId>("providers");
  const [catalog, setCatalog] = useState<ProviderPresetCatalog | null>(null);
  const [catalogError, setCatalogError] = useState("");

  const loadCatalog = useCallback(async () => {
    try {
      const value = await api.getProviderPresets();
      setCatalog(value);
      setCatalogError("");
    } catch (cause) {
      setCatalog(null);
      setCatalogError(
        describeError(cause, "Provider 预设目录读取失败；仍可手填协议族与端点。"),
      );
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  const active = CONFIG_TABS.find((item) => item.id === tab) ?? CONFIG_TABS[0];

  return (
    <div className="config-page">
      <section className="page-heading">
        <span className="eyebrow">
          <Settings2 size={13} />
          CONFIGURATION
        </span>
        <h1>工具与配置</h1>
        <p>{active.title}</p>
      </section>

      <PageTabs tabs={CONFIG_TABS} active={tab} onChange={setTab} label="配置分区" />

      {catalogError && tab === "providers" && (
        <p role="alert" className="cfg-alert">
          {catalogError}{" "}
          <button type="button" className="cfg-quiet" onClick={() => void loadCatalog()}>
            重试
          </button>
        </p>
      )}

      <div className="cfg-tab-panel" role="tabpanel" id={`cfg-panel-${tab}`}>
        {tab === "providers" && <ProviderPanel catalog={catalog} />}
        {tab === "defaults" && <DefaultRoutePanel />}
        {tab === "tools" && (
          <>
            <SearchChannelPanel />
            <InternalToolsPanel />
          </>
        )}
        {tab === "mcp" && <McpPanel />}
        {tab === "memory" && <MemoryPanel />}
        {tab === "sandbox" && (
          <>
            <SandboxPanel />
            <EgressPanel />
          </>
        )}
      </div>
    </div>
  );
}

/** 兼容既有引用名（冒烟脚本与旧代码里叫 `RuntimeConfig`）。 */
export { ConfigPage as RuntimeConfig };
