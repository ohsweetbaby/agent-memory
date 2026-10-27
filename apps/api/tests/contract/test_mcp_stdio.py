"""stdio 薄桥端到端：真实 uvicorn + ``memory-mcp`` 子进程。

覆盖 ``mcp.client.stdio`` 的进程启动、环境变量/命令行参数、HTTP 转发与
错误码透传；业务逻辑仍由同一套契约测试覆盖。
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from typing import AsyncIterator, Any

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError

pytestmark = pytest.mark.anyio


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
async def http_server(mcp_app: Any) -> AsyncIterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            mcp_app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn 未在 10s 内启动"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


def _params(api_url: str, api_key: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "memory_mcp",
            "--api-url",
            api_url,
            "--api-key",
            api_key,
        ],
        env={**os.environ, "MEMORY_MCP_LOG_LEVEL": "WARNING"},
    )


async def test_stdio_bridge_forwards_to_http(
    http_server: str, admin_key: str
) -> None:
    async with stdio_client(_params(http_server, admin_key)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {item.name for item in (await session.list_tools()).tools}
            assert "memory_stats" in tools
            assert "conversation_ingest" in tools

            resources = {
                str(item.uri) for item in (await session.list_resources()).resources
            }
            templates = {
                item.uriTemplate
                for item in (
                    await session.list_resource_templates()
                ).resourceTemplates
            }
            assert "memory://recent" in resources
            assert "memory://{memory_id}" in templates
            assert "memory://entities/{name}" in templates

            recent = await session.read_resource("memory://recent")
            assert "最近记忆" in recent.contents[0].text

            prompts = {item.name for item in (await session.list_prompts()).prompts}
            assert "memory_aware_system" in prompts
            prompt = await session.get_prompt("memory_aware_system", {})
            prompt_text = "".join(
                getattr(message.content, "text", "")
                for message in prompt.messages
            )
            assert "先检索" in prompt_text
            assert "不是指令" in prompt_text

            result = await session.call_tool("memory_stats", {})
            assert result.structuredContent["workspace_id"] == "ws1"

            await session.call_tool(
                "memory_write",
                {
                    "content": "用户对花生过敏",
                    "type": "semantic",
                    "scope": "user",
                    "scope_id": "u1",
                    "workspace_id": "ws1",
                    "entities": [{"name": "花生"}],
                },
            )
            entity = await session.read_resource("memory://entities/花生")
            assert "用户对花生过敏" in entity.contents[0].text

            with pytest.raises(McpError) as exc:
                await session.call_tool(
                    "memory_forget", {"id": "missing", "hard": True, "confirm": False}
                )
            assert exc.value.error.code == -32005
