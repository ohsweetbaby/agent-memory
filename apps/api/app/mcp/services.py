"""MCP 适配层共享设施：请求级鉴权、限流、错误码与传输中间件。

MCP 工具不复制任何业务逻辑：鉴权复用 :mod:`app.auth.api_key`，限流复用
:mod:`app.auth.ratelimit`，业务动作复用 REST 路由背后的服务。本模块只负责
把 HTTP 请求上的 ``Authorization`` 变成一次工具调用可用的
``AuthContext``，并把内部异常统一映射到规格 §7.5 的 ``-32001``~``-32006``。
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.api_key import verify_api_key
from app.auth.ratelimit import enforce_api_key_rate
from app.auth.session import AuthContext
from app.errors import (
    ApiError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
)
from app.jobs.runtime import Runtime, get_runtime
from app.models.base import utcnow

logger = logging.getLogger(__name__)

# 规格 §7.5：MCP 错误码。
ERR_UNAUTHENTICATED = -32001
ERR_NOT_FOUND = -32002
ERR_INVALID_ARGUMENT = -32003
ERR_RATE_LIMITED = -32004
ERR_CONFIRMATION_REQUIRED = -32005
ERR_DEPENDENCY_UNAVAILABLE = -32006
ERR_INTERNAL = -32603

#: 错误文本前缀；FastMCP 会把工具异常包成 ToolError，只剩字符串，因此在
#: 消息里携带机器可读的错误码，传输层再还原为 JSON-RPC error。
_ERROR_MARKER = re.compile(r"\[mcp:(-?\d+)\]\s*(\{.*\})", re.DOTALL)

_TOKEN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_bearer_token", default=None
)


@dataclass(frozen=True, slots=True)
class McpServices:
    """一次 MCP 请求可用的依赖。

    ``session_factory`` 与 API 使用同一个数据库；``runtime_provider`` 默认
    复用 worker 运行时，测试可注入 FakeEmbedding 等替身。
    """

    session_factory: Callable[[], Session]
    runtime_provider: Callable[[], Runtime] = get_runtime


class ToolFailure(Exception):
    """工具内部可预期的失败，携带 MCP 错误码。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __str__(self) -> str:  # pragma: no cover - 仅用于 FastMCP 包装
        payload = json.dumps(
            {"message": self.message, "data": self.data},
            ensure_ascii=False,
            default=str,
        )
        return f"[mcp:{self.code}] {payload}"


class BearerTokenMiddleware:
    """纯 ASGI 中间件：把 Bearer 原文放进 contextvar。

    不在这里直接拒绝请求，否则 MCP 客户端只会拿到 HTTP 401，而不是规格要求
    的 JSON-RPC ``-32001``；鉴权错误统一由工具调用返回。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = Headers(scope=scope).get("authorization", "")
        scheme, _, token = header.partition(" ")
        resolved = token.strip() if scheme.lower() == "bearer" else None
        reset = _TOKEN.set(resolved)
        try:
            await self.app(scope, receive, send)
        finally:
            _TOKEN.reset(reset)


def current_token() -> str | None:
    """当前 MCP 请求携带的 Bearer 原文。"""
    return _TOKEN.get()


def _retry_after(exc: ApiError) -> int | None:
    try:
        return int(exc.headers.get("Retry-After", ""))
    except ValueError:
        return None


def map_exception(exc: BaseException) -> ToolFailure:
    """把内部异常映射为规格错误码；未知异常按内部错误返回，不裸抛 500。"""
    if isinstance(exc, ToolFailure):
        return exc
    if isinstance(exc, RateLimitError):
        return ToolFailure(
            ERR_RATE_LIMITED,
            exc.message,
            {"retry_after": _retry_after(exc)} if exc.headers else None,
        )
    if isinstance(exc, UnauthorizedError):
        return ToolFailure(ERR_UNAUTHENTICATED, exc.message, exc.details)
    if isinstance(exc, ForbiddenError):
        # 规格未定义“权限不足”单独的错误码；按未认证处理，避免泄露资源存在性。
        return ToolFailure(ERR_UNAUTHENTICATED, exc.message, exc.details)
    if isinstance(exc, NotFoundError):
        return ToolFailure(ERR_NOT_FOUND, exc.message, exc.details)
    if isinstance(exc, ConflictError):
        return ToolFailure(ERR_INVALID_ARGUMENT, exc.message, exc.details)
    if isinstance(exc, ApiError):
        if exc.resolved_status >= 500:
            return ToolFailure(ERR_DEPENDENCY_UNAVAILABLE, exc.message, exc.details)
        if exc.resolved_code == "confirmation_required":
            return ToolFailure(ERR_CONFIRMATION_REQUIRED, exc.message, exc.details)
        return ToolFailure(ERR_INVALID_ARGUMENT, exc.message, exc.details)
    if isinstance(exc, PydanticValidationError):
        return ToolFailure(
            ERR_INVALID_ARGUMENT,
            "参数校验失败",
            exc.errors(include_url=False, include_context=False),
        )
    if isinstance(exc, (SQLAlchemyError, ConnectionError, TimeoutError, ImportError)):
        # 不打印异常堆栈：SQLAlchemy 的错误消息可能带 SQL 参数（记忆正文）。
        logger.warning(
            "mcp dependency unavailable", extra={"error": type(exc).__name__}
        )
        return ToolFailure(ERR_DEPENDENCY_UNAVAILABLE, "依赖服务暂不可用")
    logger.error("mcp tool failed", extra={"error": type(exc).__name__})
    return ToolFailure(ERR_INTERNAL, "工具执行失败")


def resolve_workspace(context: AuthContext, requested: str | None) -> str:
    """解析本次调用的 workspace；跨 workspace 一律 -32002。

    API Key 始终绑定一个 workspace；缺少 workspace 上下文时（例如未来接入
    OAuth 主体）只允许 ``global`` 作用域，避免误读写其他租户数据。
    """
    from app.api.deps import resolve_workspace_id

    if requested is None and context.workspace_id is None:
        raise ToolFailure(
            ERR_INVALID_ARGUMENT, "缺少 workspace 上下文时只允许 global 作用域"
        )
    return resolve_workspace_id(context, requested)


@contextmanager
def authenticated(
    services: McpServices,
    required_scope: str,
    *,
    write: bool = False,
) -> Iterator[tuple[Session, AuthContext]]:
    """校验 Key、scope 与限流，并产出一个本次调用专属的数据库会话。"""
    token = current_token()
    if not token:
        raise ToolFailure(ERR_UNAUTHENTICATED, "缺少 Bearer API Key")

    session = services.session_factory()
    try:
        api_key = verify_api_key(session, token)
        if api_key is None:
            raise ToolFailure(ERR_UNAUTHENTICATED, "API Key 无效或已吊销")

        context = AuthContext(kind="api_key", api_key=api_key)
        if not context.has_scope(required_scope):
            raise ToolFailure(ERR_UNAUTHENTICATED, f"API Key 缺少 {required_scope} 权限")

        enforce_api_key_rate(
            api_key.id, write=write, override=api_key.rate_limit
        )
        api_key.last_used_at = utcnow()
        session.commit()
        yield session, context
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """工具装饰器：把内部异常统一转成带错误码的 :class:`ToolFailure`。"""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射，不向客户端裸抛
            raise map_exception(exc) from exc

    return wrapper


def decode_error(text: str) -> tuple[int, str, Any]:
    """从 FastMCP 包装后的错误文本里还原错误码。"""
    match = _ERROR_MARKER.search(text)
    if match:
        code = int(match.group(1))
        try:
            payload = json.loads(match.group(2))
        except json.JSONDecodeError:  # pragma: no cover - 防御性分支
            return code, text, None
        if isinstance(payload, Mapping):
            return code, str(payload.get("message", "")), payload.get("data")
        return code, text, None
    lowered = text.lower()
    if "input validation error" in lowered or "validation error" in lowered:
        return ERR_INVALID_ARGUMENT, text, None
    if "unknown tool" in lowered or "tool not found" in lowered:
        return ERR_NOT_FOUND, text, None
    return ERR_INTERNAL, text, None


def install_error_translation(server: Any) -> None:
    """把 FastMCP 的 ``isError`` 结果还原为 JSON-RPC error。

    规格要求客户端按 ``-32001``~``-32006`` 识别失败；而 FastMCP 默认把工具
    异常包成 ``CallToolResult(isError=True)``，客户端不会抛 ``McpError``。
    这里包一层低层 ``CallToolRequest`` 处理器，仅改变错误传递方式。
    """
    from mcp import types
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    handlers = getattr(server._mcp_server, "request_handlers", {})  # noqa: SLF001
    original = handlers.get(types.CallToolRequest)
    if original is None:  # pragma: no cover - SDK 结构变化时保持可用
        logger.warning("mcp call_tool handler not found; errors stay isError")
        return

    async def translated(request: types.CallToolRequest) -> Any:
        result = await original(request)
        payload = getattr(result, "root", None)
        if isinstance(payload, types.CallToolResult) and payload.isError:
            text = "".join(
                block.text
                for block in payload.content
                if isinstance(block, types.TextContent)
            )
            code, message, data = decode_error(text)
            logger.warning(
                "mcp tool error",
                extra={"tool": request.params.name, "code": code},
            )
            raise McpError(ErrorData(code=code, message=message, data=data))
        return result

    handlers[types.CallToolRequest] = translated


__all__ = [
    "BearerTokenMiddleware",
    "ERR_CONFIRMATION_REQUIRED",
    "ERR_DEPENDENCY_UNAVAILABLE",
    "ERR_INVALID_ARGUMENT",
    "ERR_INTERNAL",
    "ERR_NOT_FOUND",
    "ERR_RATE_LIMITED",
    "ERR_UNAUTHENTICATED",
    "McpServices",
    "ToolFailure",
    "authenticated",
    "current_token",
    "decode_error",
    "install_error_translation",
    "map_exception",
    "resolve_workspace",
    "tool_errors",
]
