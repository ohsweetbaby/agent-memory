"""FastAPI 应用入口：健康检查、Prometheus 指标与开发期 CORS。"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from starlette.types import Receive, Scope, Send

from app.api import ROUTERS
from app.config import Settings, get_current_settings, set_current_settings
from app.errors import install_error_handlers
from app.logging import configure_logging
from app.mcp.services import BearerTokenMiddleware

try:  # MCP 是可选能力：SDK 未安装时 API 仍然要能启动。
    from app.mcp.server import build_mcp_server, default_mcp_services
except ModuleNotFoundError as _mcp_import_error:  # pragma: no cover - 环境相关
    if (_mcp_import_error.name or "").split(".")[0] != "mcp":
        raise
    build_mcp_server = None  # type: ignore[assignment]
    default_mcp_services = None  # type: ignore[assignment]
    MCP_IMPORT_ERROR: str | None = str(_mcp_import_error)
else:
    MCP_IMPORT_ERROR = None

APP_VERSION = "0.1.0"
CHECK_TIMEOUT_SECONDS = 0.5
MCP_PATH = "/mcp"

REQUEST_COUNT = Counter(
    "http_requests",
    "HTTP 请求总数",
    labelnames=("route", "status"),
)


def _current_settings() -> Settings:
    """返回当前应用实例的配置。"""
    return get_current_settings()


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


class _UnavailableMcpApp:
    """MCP SDK 未安装时的占位 ASGI 应用：返回可读的 503。"""

    def __init__(self, reason: str | None) -> None:
        self.reason = reason or "未安装 mcp 包"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        response = JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "mcp_unavailable",
                    "message": f"MCP 依赖不可用：{self.reason}；请执行 uv sync 安装官方 SDK",
                    "details": None,
                }
            },
        )
        await response(scope, receive, send)


class _McpGateway:
    """按 lifespan 重建 Streamable HTTP 会话管理器。

    FastMCP 的 ``StreamableHTTPSessionManager.run()`` 每个实例只能执行一次。
    Starlette 不会自动运行挂载子应用的 lifespan，而同一应用在测试（并发
    客户端、多个 TestClient 上下文）或热重载时可能多次进入 lifespan，因此
    这里每次启动都新建一个子应用并在请求期转发。
    """

    def __init__(self, server: Any) -> None:
        self.server = server
        self._inner: Any | None = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        # 重置私有缓存；_mcp_server 与注册的工具/资源保持不变。
        self.server._session_manager = None  # noqa: SLF001
        app = self.server.streamable_http_app()
        self._inner = app
        try:
            async with app.router.lifespan_context(app):
                yield
        finally:
            if self._inner is app:
                self._inner = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._inner is None:  # pragma: no cover - lifespan 未启动
            response = JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "mcp_unavailable",
                        "message": "MCP 服务尚未启动",
                        "details": None,
                    }
                },
            )
            await response(scope, receive, send)
            return
        await self._inner(scope, receive, send)


def _route_label(request: Request) -> str:
    """优先使用路由模板，避免把 ID 写进指标标签。"""
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


def create_app(
    settings: Settings | None = None,
    *,
    mcp_services: Any | None = None,
) -> FastAPI:
    """创建 API 应用。

    ``mcp_services`` 仅用于测试注入数据库会话与检索替身；生产环境按配置惰性
    构建。MCP 通过 ``/mcp`` 挂载 Streamable HTTP，lifespan 与主应用合并。
    """
    resolved = settings or Settings()
    set_current_settings(resolved)
    configure_logging(resolved.log_level)

    mcp_gateway: _McpGateway | None = None
    mcp_unavailable = False
    if resolved.mcp_enabled:
        if build_mcp_server is None:  # pragma: no cover - 依赖缺失分支
            mcp_unavailable = True
        else:
            services = mcp_services or default_mcp_services(resolved)
            mcp_gateway = _McpGateway(build_mcp_server(services))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if mcp_gateway is None:
            yield
            return
        # 挂载的子应用不会自动执行 lifespan；MCP session manager 依赖它。
        async with mcp_gateway.session():
            yield

    application = FastAPI(
        title="Agent Memory API",
        version=APP_VERSION,
        docs_url="/docs" if resolved.is_development else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    install_error_handlers(application)
    for router in ROUTERS:
        application.include_router(router)

    if mcp_gateway is not None:
        application.mount(MCP_PATH, BearerTokenMiddleware(mcp_gateway))
    elif mcp_unavailable:  # pragma: no cover - 依赖缺失分支
        application.mount(MCP_PATH, _UnavailableMcpApp(MCP_IMPORT_ERROR))

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
