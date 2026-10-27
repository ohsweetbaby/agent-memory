"""MCP 可选依赖、挂载开关与初始化失败降级。

未安装官方 SDK 或 SDK 结构不兼容时 API 仍必须启动，但 ``/mcp`` 返回可读的
503 且 ``/health`` 明确标记 ``mcp=false``（degraded），不允许“能启动但错误
码静默失效”。关闭 ``MCP_ENABLED`` 时则不挂载路由，也不计入健康检查。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from mcp import types
from mcp.server.fastmcp import FastMCP

from app.config import Settings
from app.main import create_app
from app.mcp.services import install_error_translation


def test_mcp_missing_sdk_returns_503(monkeypatch) -> None:
    monkeypatch.setattr("app.main.build_mcp_server", None)
    monkeypatch.setattr("app.main.MCP_IMPORT_ERROR", "No module named 'mcp'")

    application = create_app(Settings(environment="test"))
    with TestClient(application) as client:
        response = client.get("/mcp")
        health = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "mcp_unavailable"
    assert "uv sync" in body["error"]["message"]
    assert health.json()["components"]["mcp"] is False
    assert health.json()["status"] == "degraded"


def test_mcp_initialization_failure_is_degraded(monkeypatch) -> None:
    def _broken(services) -> None:
        raise RuntimeError("MCP SDK 结构不兼容：缺少 CallToolRequest handler")

    monkeypatch.setattr("app.main.build_mcp_server", _broken)

    application = create_app(Settings(environment="test"))
    with TestClient(application) as client:
        health = client.get("/health")
        response = client.get("/mcp")

    assert health.json()["components"]["mcp"] is False
    assert health.json()["status"] == "degraded"
    assert response.status_code == 503
    assert "CallToolRequest" in response.json()["error"]["message"]


def test_error_translation_fails_fast_when_handler_missing() -> None:
    server = FastMCP("handler-check")
    # 模拟 SDK 主版本升级导致私有结构消失。
    server._mcp_server.request_handlers.pop(types.CallToolRequest, None)
    with pytest.raises(RuntimeError, match="mcp>=1.30,<1.31"):
        install_error_translation(server)


def test_mcp_can_be_disabled() -> None:
    application = create_app(Settings(environment="test", mcp_enabled=False))
    with TestClient(application) as client:
        response = client.get("/mcp")
        health = client.get("/health")
    assert response.status_code == 404
    assert "mcp" not in health.json()["components"]
