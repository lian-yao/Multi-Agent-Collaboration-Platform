import { useCallback, useEffect, useState } from "react";
import { Network } from "lucide-react";
import { api } from "../api/client";
import type { EgressStatus } from "../types/api";
import { Chip, describeError, EmptyState } from "./shared";

/**
 * 出网策略（`doc/api.md` §5.21、ADR-034）。**只读**。
 *
 * 与「执行边界」（沙箱）同类：它是部署期安全边界，后端没有写接口。把它做成运行时可改的
 * 开关，等于在 Web 上点两下就能放开自己的出网限制。
 *
 * 一条不能含糊的展示口径：`blocked` 是**进程内**计数。工具调用发生在 worker 进程，
 * 所以 API 进程里这个数**通常就是 0**——界面必须说明这一点，不能让人读成
 * 「从来没有被拦过」。要看全局得去 Prometheus 的 `macp_egress_blocked_total`。
 */

export function EgressBoundary({
  status,
  error = "",
  onReload,
  reloading = false,
}: {
  status: EgressStatus | null;
  error?: string;
  onReload?: () => void;
  reloading?: boolean;
}) {
  const blockedTotal = status
    ? Object.values(status.blocked).reduce((sum, value) => sum + value, 0)
    : 0;

  return (
    <section className="cfg-block">
      <div className="cfg-block-head">
        <div className="cfg-block-title">
          <Network size={16} />
          <div>
            <h3>出网策略</h3>
            <p>
              平台与 Agent 的出网口径，只读展示——这是部署期安全边界，没有运行期开关。
            </p>
          </div>
        </div>
        <div className="cfg-row-actions">
          {status && (
            <Chip tone={status.mode === "allowlist" ? "amber" : "teal"}>
              {status.mode === "allowlist" ? "白名单模式" : "只放公网"}
            </Chip>
          )}
          {onReload && (
            <button type="button" className="cfg-quiet" onClick={onReload} disabled={reloading}>
              重新读取
            </button>
          )}
        </div>
      </div>

      {error && <p className="cfg-alert">{error}</p>}
      {!error && !status && <p className="cfg-hint">读取中…</p>}
      {!error && status && (
        <>
          <dl className="cfg-facts">
            <div>
              <dt>域名白名单</dt>
              <dd>
                {status.allow_hosts.length ? status.allow_hosts.join("、") : "未设置"}
                <span className="cfg-hint">（白名单模式下未命中的一律拒绝）</span>
              </dd>
            </div>
            <div>
              <dt>域名黑名单</dt>
              <dd>{status.deny_hosts.length ? status.deny_hosts.join("、") : "未设置"}</dd>
            </div>
            <div>
              <dt>平台内部服务</dt>
              <dd>
                {status.internal_hosts.length
                  ? status.internal_hosts.join("、")
                  : "未设置"}
                <span className="cfg-hint">
                  （放行的是这些精确主机名，不是整个内网）
                </span>
              </dd>
            </div>
            <div>
              <dt>允许端口</dt>
              <dd>{status.allowed_ports.join("、") || "—"}</dd>
            </div>
            <div>
              <dt>模型流量</dt>
              <dd>
                {status.model_exempt
                  ? "豁免私网判定（Ollama 与内网网关可用）"
                  : "不豁免（模型端点也必须是公网）"}
              </dd>
            </div>
            <div>
              <dt>重定向上限</dt>
              <dd>{status.max_redirects} 跳</dd>
            </div>
          </dl>

          {blockedTotal === 0 ? (
            <EmptyState
              title="本进程还没有拒绝记录"
              hint={
                blockedTotal === 0
                  ? "工具调用发生在 worker 进程，这里通常是空的；全局计数看 Prometheus 的 macp_egress_blocked_total。"
                  : ""
              }
            />
          ) : (
            <div className="cfg-egress-blocked">
              <b>本进程拒绝计数</b>
              <ul>
                {Object.entries(status.blocked).map(([reason, count]) => (
                  <li key={reason}>
                    <code>{reason}</code>
                    <span>{count} 次</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <p className="cfg-hint">
            私网与保留网段（含云元数据地址）一律拦截；连接会钉在解析出的 IP 上。
            被拒的调用不会重试。调整口径请改部署环境变量（`EGRESS_*`）。
          </p>
        </>
      )}
    </section>
  );
}

/** 容器：取一次策略状态。 */
export function EgressPanel() {
  const [status, setStatus] = useState<EgressStatus | null>(null);
  const [error, setError] = useState("");
  const [reloading, setReloading] = useState(false);

  const load = useCallback(async () => {
    setReloading(true);
    try {
      setStatus(await api.getEgressStatus());
      setError("");
    } catch (cause) {
      setError(describeError(cause, "出网策略读取失败"));
    } finally {
      setReloading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <EgressBoundary
      status={status}
      error={error}
      onReload={() => void load()}
      reloading={reloading}
    />
  );
}
