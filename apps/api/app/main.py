"""FastAPI 应用入口：健康检查、Prometheus 指标与开发期 CORS。"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from app.config import Settings, get_settings
from app.logging import configure_logging

APP_VERSION = "0.1.0"
CHECK_TIMEOUT_SECONDS = 0.5

# create_app 注入的配置；健康检查函数保持无参，便于测试 monkeypatch。
_active_settings: Settings | None = None

REQUEST_COUNT = Counter(
    "http_requests",
    "HTTP 请求总数",
    labelnames=("route", "status"),
)


def _current_settings() -> Settings:
    """返回当前应用实例的配置。"""
    if _active_settings is None:
        return get_settings()
    return _active_settings


def _tcp_reachable(url: str, default_port: int) -> bool:
    """通过 TCP 握手判断服务端口是否可达。"""
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port or default_port
    except ValueError:
        return False
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=CHECK_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def check_postgres() -> bool:
    """Postgres 可达性。"""
    return _tcp_reachable(_current_settings().database_url, 5432)


def check_redis() -> bool:
    """Redis 可达性。"""
    return _tcp_reachable(_current_settings().redis_url, 6379)


def check_neo4j() -> bool:
    """Neo4j Bolt 端口可达性。"""
    return _tcp_reachable(_current_settings().neo4j_uri, 7687)


def check_qdrant() -> bool:
    """Qdrant HTTP 健康端点。"""
    base_url = _current_settings().qdrant_url.rstrip("/")
    if not base_url:
        return False
    try:
        response = httpx.get(
            f"{base_url}/healthz",
            timeout=CHECK_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def collect_health() -> dict[str, Any]:
    """并行检查四个依赖，任一失败即为 degraded。"""
    checks = {
        "postgres": check_postgres,
        "redis": check_redis,
        "qdrant": check_qdrant,
        "neo4j": check_neo4j,
    }
    components: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = {name: executor.submit(check) for name, check in checks.items()}
        for name, future in futures.items():
            try:
                components[name] = bool(future.result())
            except Exception:  # 健康检查本身不能把 /health 打成 500
                components[name] = False
    status = "ok" if all(components.values()) else "degraded"
    return {"status": status, "components": components}


def _route_label(request: Request) -> str:
    """优先使用路由模板，避免把 ID 写进指标标签。"""
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 API 应用。"""
    global _active_settings
    resolved = settings or Settings()
    _active_settings = resolved
    configure_logging(resolved.log_level)

    application = FastAPI(
        title="Agent Memory API",
        version=APP_VERSION,
        docs_url="/docs" if resolved.is_development else None,
        redoc_url=None,
    )

    if resolved.is_development:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @application.middleware("http")
    async def count_requests(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            REQUEST_COUNT.labels(route=_route_label(request), status="500").inc()
            raise
        REQUEST_COUNT.labels(
            route=_route_label(request), status=str(response.status_code)
        ).inc()
        return response

    @application.get("/health")
    def health() -> dict[str, Any]:
        """返回整体状态与各依赖可用性；降级时仍返回 200。"""
        return collect_health()

    @application.get("/metrics")
    def metrics() -> Response:
        """Prometheus 文本格式指标。"""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return application
