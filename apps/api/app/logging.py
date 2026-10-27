"""JSON 行日志。

每条日志占一行，便于容器日志采集；禁止在日志中输出密钥与完整记忆内容。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_HANDLER_MARKER = "_agent_memory_json_handler"

# uvicorn 默认给这些日志器装 plaintext handler 且关闭传播，必须统一改走 root JSON。
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# HTTP 客户端每次请求都打 INFO，参数可能含敏感内容，只保留告警。
_QUIET_LOGGERS = ("httpx", "httpcore")

# LogRecord 自带的字段不重复输出，其余通过 extra 传入的字段原样保留。
_RESERVED_FIELDS = set(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", (), None))
) | {"message", "asctime"}


def _jsonable(value: Any) -> Any:
    """把日志字段转换为 JSON 可序列化值。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


class JsonFormatter(logging.Formatter):
    """输出单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_FIELDS or key.startswith("_"):
                continue
            payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """安装 JSON 日志处理器；重复调用不会叠加 handler。

    uvicorn 自带的 plaintext handler 会被移除并改为向 root 传播，
    确保启动、访问与异常日志同样是一行一个 JSON。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        for uvicorn_handler in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(uvicorn_handler)
        uvicorn_logger.propagate = True

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
