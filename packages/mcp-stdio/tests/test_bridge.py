"""stdio 薄桥：工具转发、参数透传与错误码传播。

不启动子进程：用官方 ``create_connected_server_and_client_session`` 直接连到
桥接 FastMCP，``caller`` 用替身记录转发参数。真实 HTTP 转发由 MCP 契约测试
（Streamable HTTP）覆盖。
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from memory_mcp.__main__ import ProxyFailure, build_server

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
def forwarded() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture()
def bridge(forwarded: list[tuple[str, dict[str, Any]]]):
    async def caller(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        forwarded.append((name, arguments))
        if name == "memory_stats":
            return {"total_memories": 3, "workspace_id": "ws1"}
        if name == "memory_write" and arguments["content"] == "触发限流":
            raise ProxyFailure(-32004, "请求过于频繁")
        return {"ok": True}

    return build_server("http://api.local:8000", "mem_fake_key", caller=caller)


async def test_bridge_lists_and_forwards(bridge, forwarded) -> None:
    async with create_connected_server_and_client_session(bridge) as client:
        tools = {item.name for item in (await client.list_tools()).tools}
        assert "memory_search" in tools
        assert "conversation_ingest" in tools

        result = await client.call_tool("memory_stats", {})
        assert result.structuredContent == {
            "total_memories": 3,
            "workspace_id": "ws1",
        }

        await client.call_tool(
            "memory_write",
            {
                "content": "用户对花生过敏",
                "scope": "user",
                "scope_id": "u1",
                "workspace_id": "ws1",
            },
        )
        name, arguments = forwarded[-1]
        assert name == "memory_write"
        assert arguments["content"] == "用户对花生过敏"
        assert arguments["idempotency_key"] is None


async def test_bridge_propagates_error_code(bridge) -> None:
    async with create_connected_server_and_client_session(bridge) as client:
        with pytest.raises(McpError) as exc:
            await client.call_tool("memory_write", {"content": "触发限流"})
        assert exc.value.error.code == -32004
