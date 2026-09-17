import { useCallback, useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { api } from "../api/client";
import type { SandboxStatus } from "../types/api";
import { Chip, describeError } from "./shared";

/**
 * 执行边界（`doc/api.md` §5.15）。
 *
 * **刻意只读**：`SANDBOX_*` 是部署期安全边界（cgroup 限额、网络开关、后端选择）。
 * 把它做成运行时可改的开关会有一个直接后果——在 Web 上点两下就能放宽自己容器的隔离。
 * 所以这里只回答一个问题：**代码执行现在到底能不能跑，不能的话为什么**。
 *
 * 这个问题不是修辞：`app/sandbox` 的隔离实现是完整的，但部署里 backend 容器没有挂
 * `/var/run/docker.sock`，运行期探测因此恒为不可用。没有这块展示，这个事实只能靠翻
 * 容器日志才能发现——而这恰恰是最该被看见的信息。
 */

function describeLimit(key: keyof SandboxStatus["limits"], value: unknown): string {
  switch (key) {
    case "timeout_seconds":
      return `${String(value)} 秒`;
    case "cpu_limit":
      return `${String(value)} 核（nano_cpus 折算）`;
    case "network_enabled":
      return value ? "允许" : "禁用";
    case "pids_limit":
      return `${String(value)} 个`;
    case "output_limit_chars":
    case "max_code_chars":
      return `${String(value)} 字符`;
    default:
      return String(value);
  }
}

const LIMIT_ROWS: readonly [keyof SandboxStatus["limits"], string][] = [
  ["timeout_seconds", "单次执行超时"],
  ["memory_limit", "内存上限"],
  ["cpu_limit", "CPU 上限"],
  ["pids_limit", "进程数上限"],
  ["network_enabled", "容器网络"],
  ["output_limit_chars", "输出截断阈值"],
  ["max_code_chars", "代码长度上限"],
];

/** 纯 props 驱动的展示体，便于 `frontend/rendercheck` 直接挂载断言。 */
export function SandboxBoundary({
  status,
  onReload,
  reloading = false,
}: {
  status: SandboxStatus;
  onReload?: () => void;
  reloading?: boolean;
}) {
  return (
    <section className="cfg-block">
      <div className="cfg-block-head">
        <div className="cfg-block-title">
          <ShieldCheck size={16} />
          <div>
            <h3>执行边界</h3>
            <p>敏感工具（代码执行）的隔离参数，只读展示——这些是部署期安全边界。</p>
          </div>
        </div>
        <div className="cfg-row-actions">
          <Chip tone="slate">{status.backend}</Chip>
          <Chip tone={status.available ? "teal" : "amber"}>
            {status.available ? "可用" : "不可用"}
          </Chip>
          {onReload && (
            <button
              type="button"
              className="cfg-quiet"
              onClick={onReload}
              disabled={reloading}
            >
              重新探测
            </button>
          )}
        </div>
      </div>

      {status.reason && <p className="cfg-alert">{status.reason}</p>}

      <dl className="cfg-facts">
        <div>
          <dt>执行镜像</dt>
          <dd>
            <code>{status.image}</code>
          </dd>
        </div>
        {LIMIT_ROWS.map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{describeLimit(key, status.limits[key])}</dd>
          </div>
        ))}
      </dl>

      <p className="cfg-hint">
        这里没有编辑入口，是有意的：放宽限额属于部署决策，请在部署环境用 `SANDBOX_*`
        调整。界面只负责让「当前边界是什么」与「为什么不生效」可见。
      </p>
    </section>
  );
}

export function SandboxPanel() {
  const [status, setStatus] = useState<SandboxStatus | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await api.getSandboxStatus());
      setError("");
    } catch (cause) {
      setStatus(null);
      setError(describeError(cause, "执行边界读取失败。"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="cfg-stack">
      {error && (
        <p role="alert" className="cfg-alert">
          {error}{" "}
          <button type="button" className="cfg-quiet" onClick={() => void load()}>
            重试
          </button>
        </p>
      )}
      {loading && !status && <p className="cfg-hint">正在探测执行边界…</p>}
      {status && (
        <SandboxBoundary status={status} onReload={() => void load()} reloading={loading} />
      )}
    </div>
  );
}
