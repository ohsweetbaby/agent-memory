"""统一错误模型与异常处理器。

REST 错误体固定为 ``{"error": {"code", "message", "details"}}``，
所有路由只抛 ``ApiError`` 子类，由处理器转成对应状态码。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
}


class ApiError(Exception):
    """可转换为统一错误响应的业务异常。"""

    status_code: int = 400
    code: str = "bad_request"
    default_message: str = "请求无效"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self._code = code
        self._status_code = status_code
        self.details = details
        self.headers = headers or {}

    @property
    def resolved_code(self) -> str:
        return self._code or self.code

    @property
    def resolved_status(self) -> int:
        return self._status_code or self.status_code

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.resolved_status,
            content=error_body(self.resolved_code, self.message, self.details),
            headers=self.headers,
        )


class BadRequestError(ApiError):
    status_code = 400
    code = "bad_request"


class UnauthorizedError(ApiError):
    status_code = 401
    code = "unauthorized"
    default_message = "未认证或凭证无效"


class ForbiddenError(ApiError):
    status_code = 403
    code = "forbidden"
    default_message = "权限不足"


class NotFoundError(ApiError):
    status_code = 404
    code = "not_found"
    default_message = "资源不存在"


class ConflictError(ApiError):
    status_code = 409
    code = "conflict"
    default_message = "资源冲突"


class RateLimitError(ApiError):
    status_code = 429
    code = "rate_limited"
    default_message = "请求过于频繁"

    def __init__(self, message: str | None = None, *, retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        super().__init__(message, headers=headers)


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    """只保留可 JSON 序列化的字段，避免把 ValueError 对象塞进响应。"""
    details: list[dict[str, Any]] = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ())]
        details.append(
            {
                "loc": ".".join(location),
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    return details


def install_error_handlers(app: FastAPI) -> None:
    """注册统一的 Business / 校验 / HTTP 异常处理器。"""

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_body("validation_error", "请求参数校验失败", _validation_details(exc)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        detail = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, detail),
            headers=getattr(exc, "headers", None) or {},
        )
