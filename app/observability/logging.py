"""成员 A D7-8 增量：应用行为日志（结构化事件）。

职责边界：

- 本模块只提供日志基建——统一的 logger 命名（`macp.<component>`）、可 grep 的
  `event=<name> field=value` 事件格式，以及幂等的初始化入口；
- 具体事件由各模块按需发出（当前：编排阶段的开始/结束/失败、工具调用）；
- 追踪（OpenTelemetry → Jaeger）与指标（Prometheus）不在本模块范围。

对齐事实源：

- `doc/architecture.md`：可观测性层负责「追踪、指标、行为日志」；
- `doc/15 AI Native多智能体协作平台.md` 模块 5「行为追踪」：记录 Agent 的
  ReAct 循环与每次工具调用，保存在结构化日志中供审计和分析。

环境变量 `LOG_LEVEL`（默认 `INFO`）控制应用日志级别；第三方库（uvicorn、
durabletask）自带 handler，不受影响。
"""

from __future__ import annotations

import logging
import os
from typing import Any

LOGGER_NAME = "macp"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_LEVEL = "INFO"
LEVEL_ENV_VAR = "LOG_LEVEL"


def get_logger(component: str) -> logging.Logger:
    """返回统一命名的组件 logger（`macp.<component>`）。"""

    return logging.getLogger(f"{LOGGER_NAME}.{component}")


def configure_logging(level: str | int | None = None) -> None:
    """配置应用日志（幂等）：统一格式与级别，输出到进程 stderr。

    在进程入口调用一次即可；`logging.basicConfig` 在根 logger 已有 handler 时
    不会重复添加 handler，因此重复调用是安全的。
    """

    resolved = _resolve_level(level)
    logging.basicConfig(level=resolved, format=LOG_FORMAT)
    logging.getLogger(LOGGER_NAME).setLevel(resolved)


def log_event(
    logger: logging.Logger,
    event: str,
    /,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """输出一行结构化行为日志：`event=<name> field=value ...`。

    字段按传入顺序输出，值为 None 的字段直接省略；含空格或 `=` 的值加引号，
    保证一行一条事件、可用 `Select-String "event=stage.finish"` 直接过滤。
    """

    if not logger.isEnabledFor(level):
        return
    parts = [f"event={event}"]
    parts.extend(
        f"{key}={_format_value(value)}"
        for key, value in fields.items()
        if value is not None
    )
    logger.log(level, " ".join(parts))


def _resolve_level(level: str | int | None) -> int:
    if level is None:
        level = os.getenv(LEVEL_ENV_VAR, DEFAULT_LEVEL)
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    if not isinstance(resolved, int):
        raise ValueError(f"未知日志级别: {level}")
    return resolved


def _format_value(value: Any) -> str:
    text = " ".join(str(value).split())
    if not text:
        return '""'
    if any(character.isspace() for character in text) or "=" in text:
        return f'"{text}"'
    return text
