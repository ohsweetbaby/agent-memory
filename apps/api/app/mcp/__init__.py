"""MCP 适配层：FastMCP 服务、10 个工具、Resources 与 Prompt。

依赖 ``mcp``（官方 Python SDK）。该包未安装时，导入本包会抛
``ModuleNotFoundError``；:mod:`app.main` 会捕获并降级为 503，API 仍可启动。
"""

from __future__ import annotations

__all__ = ["McpServices", "build_mcp_server", "default_mcp_services"]


def __getattr__(name: str):
    """延迟导入，避免 ``import app.mcp`` 在未安装 mcp 时失败。"""
    if name in __all__:
        from app.mcp import server as _server

        return getattr(_server, name)
    raise AttributeError(name)
