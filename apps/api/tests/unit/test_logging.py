import json
import logging
import sys

from app.logging import JsonFormatter, configure_logging

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _logger_emits_json(logger: logging.Logger) -> bool:
    """日志器自身或传播链上的 root 使用 JSON formatter。"""
    handlers = list(logger.handlers)
    if logger.propagate:
        handlers.extend(logging.getLogger().handlers)
    return any(isinstance(handler.formatter, JsonFormatter) for handler in handlers)


def _install_plaintext_uvicorn_handlers() -> dict[str, tuple]:
    """模拟 uvicorn 默认日志配置：plaintext handler + 不传播。"""
    original: dict[str, tuple] = {}
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        original[name] = (list(logger.handlers), logger.propagate, logger.level)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
    return original


def _restore_loggers(original: dict[str, tuple]) -> None:
    for name, (handlers, propagate, level) in original.items():
        logger = logging.getLogger(name)
        logger.handlers = handlers
        logger.propagate = propagate
        logger.setLevel(level)


def test_logs_are_json_lines(capsys):
    configure_logging()
    logging.getLogger("app").info("hello", extra={"memory_id": "m1"})
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["level"] == "INFO" and payload["memory_id"] == "m1"


def test_uvicorn_loggers_emit_json_and_http_clients_are_quiet(capsys):
    original = _install_plaintext_uvicorn_handlers()
    try:
        configure_logging()

        for name in _UVICORN_LOGGERS:
            assert _logger_emits_json(logging.getLogger(name)), f"{name} 未输出 JSON"

        logging.getLogger("uvicorn.access").info("GET /health")
        line = capsys.readouterr().err.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["logger"] == "uvicorn.access"
        assert payload["message"] == "GET /health"

        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        _restore_loggers(original)
