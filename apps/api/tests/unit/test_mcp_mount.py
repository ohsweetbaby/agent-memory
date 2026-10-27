"""MCP 可选依赖与挂载开关。

未安装官方 SDK 时 API 仍必须启动；``/mcp`` 返回可读的 503。关闭
``MCP_ENABLED`` 时则不挂载路由。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_mcp_missing_sdk_returns_503(monkeypatch) -> None:
    monkeypatch.setattr("app.main.build_mcp_server", None)
    monkeypatch.setattr("app.main.MCP_IMPORT_ERROR", "No module named 'mcp'")

    application = create_app(Settings(environment="test"))
    with TestClient(application) as client:
        response = client.get("/mcp")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "mcp_unavailable"
    assert "uv sync" in body["error"]["message"]


def test_mcp_can_be_disabled() -> None:
    application = create_app(Settings(environment="test", mcp_enabled=False))
    with TestClient(application) as client:
        response = client.get("/mcp")
    assert response.status_code == 404
