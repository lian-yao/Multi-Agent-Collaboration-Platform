"""可观测性配置（成员 C D7-8）。

对齐事实源：`doc/15 AI Native多智能体协作平台.md` 模块 5「可观测性」——
OpenTelemetry 追踪 + Jaeger 展示、Prometheus 指标收集、结构化行为日志。

环境变量统一使用 `OBS_` 前缀，例如 `OBS_TRACING_ENDPOINT`、`OBS_METRICS_ENABLED`。
默认端点与 `deploy/compose.yaml` 的 jaeger 服务（OTLP HTTP 4318）一致。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

MetricsSinkKind = Literal["postgres", "none"]


class ObservabilitySettings(BaseSettings):
    """追踪与指标配置。"""

    model_config = SettingsConfigDict(
        env_prefix="OBS_",
        env_file=".env",
        extra="ignore",
    )

    service_name: str = "macp-backend"
    tracing_enabled: bool = True
    tracing_endpoint: str = "http://localhost:4318/v1/traces"
    tracing_sample_ratio: float = 1.0
    tracing_export_timeout_seconds: float = 5.0

    metrics_enabled: bool = True
    metrics_sink: MetricsSinkKind = "postgres"
    """指标采样落库方式：`postgres` 写入 `metrics` 表（表不存在时跳过并记日志）。"""

    metrics_flush_size: int = 50
    metrics_dedupe_limit: int = 5_000


@lru_cache
def get_observability_settings() -> ObservabilitySettings:
    return ObservabilitySettings()
