FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
# 搜索网关与 backend 共用这一镜像：它只用标准库，一起 COPY 进来后 compose 里的
# search-gateway 服务不必再拉一个基础镜像（ADR-032）。`.dockerignore` 只放行这一个文件。
COPY scripts/search_gateway.py ./scripts/search_gateway.py

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "python", "-m", "app.workflows.worker"]
