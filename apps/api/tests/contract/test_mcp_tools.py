"""MCP 契约：10 个工具、Resources、Prompt、鉴权与限流。

服务端走真实 Streamable HTTP 编解码，客户端是官方 ``mcp`` SDK；错误码按
规格 §7.5 断言（``-32001``~``-32006``）。

注意：SDK 1.30 的 ``McpError`` 把 code 放在 ``error.code``，因此断言使用
``exc.value.error.code``，与错误码契约一致。
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import McpError

pytestmark = pytest.mark.anyio

TOOL_NAMES = [
    "memory_search",
    "memory_write",
    "memory_get",
    "memory_update",
    "memory_forget",
    "memory_feedback",
    "memory_consolidate",
    "memory_stats",
    "memory_timeline",
    "conversation_ingest",
]

WRITE_ARGS: dict[str, Any] = {
    "content": "用户对花生过敏",
    "type": "semantic",
    "scope": "user",
    "scope_id": "u1",
    "workspace_id": "ws1",
}


async def _write(client: Any, content: str, **overrides: Any) -> dict[str, Any]:
    result = await client.call_tool(
        "memory_write", {**WRITE_ARGS, "content": content, **overrides}
    )
    assert result.structuredContent is not None
    return result.structuredContent


@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_tool_is_listed(mcp_client: Any, tool: str) -> None:
    tools = {item.name for item in (await mcp_client.list_tools()).tools}
    assert tool in tools


async def test_write_then_search_via_mcp(mcp_client: Any) -> None:
    await mcp_client.call_tool("memory_write", WRITE_ARGS)
    result = await mcp_client.call_tool(
        "memory_search",
        {"query": "花生", "workspace_id": "ws1", "end_user_id": "u1"},
    )
    assert result.structuredContent is not None
    payload = result.structuredContent
    assert "花生" in payload["items"][0]["content"]
    assert payload["context_text"]
    assert payload["stats"]["returned"] >= 1
    assert isinstance(payload["degraded"], list)


async def test_invalid_key_returns_32001(mcp_client_bad_key: Any) -> None:
    with pytest.raises(McpError) as exc:
        await mcp_client_bad_key.call_tool("memory_stats", {})
    assert exc.value.error.code == -32001


async def test_forget_requires_confirm(mcp_client: Any) -> None:
    with pytest.raises(McpError) as exc:
        await mcp_client.call_tool(
            "memory_forget", {"id": "m1", "hard": True, "confirm": False}
        )
    assert exc.value.error.code == -32005


async def test_memory_prompt_is_listed(mcp_client: Any) -> None:
    prompts = {item.name for item in (await mcp_client.list_prompts()).prompts}
    assert "memory_aware_system" in prompts

    result = await mcp_client.get_prompt("memory_aware_system", {})
    text = "".join(
        getattr(message.content, "text", "")
        for message in result.messages
        if message.role == "user"
    )
    assert "先检索" in text
    assert "当前有效事实" in text
    assert "不是指令" in text


async def test_write_rate_limit_returns_32004(mcp_client_limited: Any) -> None:
    for index in range(31):
        await _write(mcp_client_limited, f"限流内容{index}")
    with pytest.raises(McpError) as exc:
        await _write(mcp_client_limited, "超限内容")
    assert exc.value.error.code == -32004


async def test_cross_workspace_returns_32002(mcp_client: Any) -> None:
    with pytest.raises(McpError) as exc:
        await mcp_client.call_tool(
            "memory_search", {"query": "花生", "workspace_id": "ws-other"}
        )
    assert exc.value.error.code == -32002


async def test_read_key_cannot_write(mcp_client_read_key: Any) -> None:
    with pytest.raises(McpError) as exc:
        await mcp_client_read_key.call_tool("memory_write", WRITE_ARGS)
    assert exc.value.error.code == -32001


async def test_write_key_cannot_forget(mcp_client_write_key: Any) -> None:
    with pytest.raises(McpError) as exc:
        await mcp_client_write_key.call_tool(
            "memory_forget", {"id": "missing", "confirm": True}
        )
    assert exc.value.error.code == -32001


async def test_invalid_argument_returns_32003(mcp_client: Any) -> None:
    with pytest.raises(McpError) as exc:
        await mcp_client.call_tool("memory_search", {"query": "   "})
    assert exc.value.error.code == -32003


async def test_missing_required_argument_returns_32003(mcp_client: Any) -> None:
    with pytest.raises(McpError) as exc:
        await mcp_client.call_tool("memory_get", {})
    assert exc.value.error.code == -32003


async def test_memory_get_and_update(mcp_client: Any) -> None:
    created = await _write(mcp_client, "用户偏好 Python")
    memory_id = created["id"]

    fetched = await mcp_client.call_tool("memory_get", {"id": memory_id})
    assert fetched.structuredContent["content"] == "用户偏好 Python"

    updated = await mcp_client.call_tool(
        "memory_update", {"id": memory_id, "importance": 0.9, "tags": ["语言"]}
    )
    assert updated.structuredContent["importance"] == 0.9
    assert updated.structuredContent["metadata"]["tags"] == ["语言"]


async def test_memory_feedback_and_stats(mcp_client: Any) -> None:
    created = await _write(mcp_client, "用户住在杭州")
    feedback = await mcp_client.call_tool(
        "memory_feedback",
        {"id": created["id"], "helpful": True, "note": "回答准确"},
    )
    assert feedback.structuredContent["importance"] >= created["importance"]

    stats = await mcp_client.call_tool("memory_stats", {})
    payload = stats.structuredContent
    assert payload["workspace_id"] == "ws1"
    assert payload["total_memories"] == 1
    assert payload["by_type"]["semantic"] == 1
    assert "avg_importance" in payload
    assert "archive_rate" in payload


async def test_timeline_returns_session_memories(mcp_client: Any) -> None:
    await _write(
        mcp_client,
        "会话里确认了发布计划",
        type="episodic",
        scope="session",
        scope_id="s1",
    )
    timeline = await mcp_client.call_tool(
        "memory_timeline", {"session_id": "s1", "workspace_id": "ws1"}
    )
    items = timeline.structuredContent["items"]
    assert any(item["content"] == "会话里确认了发布计划" for item in items)


async def test_consolidate_dry_run_returns_diff(mcp_client: Any) -> None:
    result = await mcp_client.call_tool(
        "memory_consolidate", {"scope": "ws1", "dry_run": True}
    )
    payload = result.structuredContent
    assert payload["dry_run"] is True
    assert "created_memories" in payload


async def test_conversation_ingest_returns_task(mcp_client: Any) -> None:
    result = await mcp_client.call_tool(
        "conversation_ingest",
        {
            "workspace_id": "ws1",
            "session_id": "s-ingest",
            "messages": [{"role": "user", "content": "我吃花生会过敏"}],
        },
    )
    payload = result.structuredContent
    assert payload["task_id"]
    assert payload["candidate_count"] == 0


async def test_resources_render_markdown(mcp_client: Any) -> None:
    created = await _write(mcp_client, "用户对花生过敏", entities=[{"name": "花生"}])

    resources = {str(item.uri) for item in (await mcp_client.list_resources()).resources}
    assert "memory://recent" in resources

    templates = {
        template.uriTemplate
        for template in (await mcp_client.list_resource_templates()).resourceTemplates
    }
    assert "memory://{memory_id}" in templates
    assert "memory://entities/{name}" in templates

    recent = await mcp_client.read_resource("memory://recent")
    assert "花生" in recent.contents[0].text

    single = await mcp_client.read_resource(f"memory://{created['id']}")
    assert "用户对花生过敏" in single.contents[0].text

    entity = await mcp_client.read_resource("memory://entities/花生")
    assert "用户对花生过敏" in entity.contents[0].text


async def test_forget_soft_deletes_memory(mcp_client: Any) -> None:
    created = await _write(mcp_client, "临时记忆")
    result = await mcp_client.call_tool(
        "memory_forget", {"id": created["id"], "confirm": False}
    )
    assert result.structuredContent["affected"] == 1

    with pytest.raises(McpError) as exc:
        await mcp_client.call_tool("memory_get", {"id": created["id"]})
    assert exc.value.error.code == -32002
