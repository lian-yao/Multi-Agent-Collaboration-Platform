FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
# 搜索网关与出网代理和 backend 共用这一镜像：网关只用标准库，代理复用 app 里的策略实现，
# 一起 COPY 进来后 compose 里那两个服务不必再拉基础镜像（ADR-032 / ADR-034）。
# `.dockerignore` 只放行这两个脚本文件。
COPY scripts/search_gateway.py ./scripts/search_gateway.py
COPY scripts/egress_proxy.py ./scripts/egress_proxy.py

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "python", "-m", "app.workflows.worker"]
