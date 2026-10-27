# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.13.6 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# 先复制依赖清单与包源码：uv workspace 的可编辑安装需要目标包存在。
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/api/app apps/api/app

RUN uv sync --frozen --no-dev --package agent-memory-api

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
