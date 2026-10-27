"""``memory-mcp``：把 stdio MCP 请求转发到远端 HTTP ``/mcp``。

桥接进程不复制任何业务逻辑，也不直接访问数据库：每次工具调用建立一次
Streamable HTTP 会话，透传 ``Authorization: Bearer`` 与 trace id，然后原样
返回结构化结果或错误码（``-32001``~``-32006``）。

用法::

    memory-mcp --api-url http://localhost:8000 --api-key mem_xxx

也可用环境变量 ``MEMORY_API_URL`` / ``MEMORY_API_KEY``。stdout 只承载 MCP
协议，日志一律写 stderr；日志不会打印 Key 或记忆正文。
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from pydantic import AnyUrl

logger = logging.getLogger("memory_mcp")

DEFAULT_API_URL = "http://localhost:8000"
ERR_INVALID_ARGUMENT = -32003
ERR_DEPENDENCY_UNAVAILABLE = -32006
ERR_INTERNAL = -32603

_ERROR_MARKER = re.compile(r"\[mcp:(-?\d+)\]\s*(\{.*\})", re.DOTALL)

Caller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
Reader = Callable[[str], Awaitable[str]]
Prompter = Callable[[str, dict[str, str] | None], Awaitable[str]]


class ProxyFailure(Exception):
    """带 MCP 错误码的转发失败。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __str__(self) -> str:  # pragma: no cover - 由 FastMCP 包装后解析
        payload = json.dumps(
            {"message": self.message, "data": self.data},
            ensure_ascii=False,
            default=str,
        )
        return f"[mcp:{self.code}] {payload}"


@dataclass(frozen=True, slots=True)
class HttpSettings:
    api_url: str
    api_key: str
    trace_id: str | None = None


def _trace_id() -> str:
    return os.environ.get("MEMORY_TRACE_ID") or uuid.uuid4().hex


def _find_exception(exc: BaseException, kind: type[BaseException]) -> BaseException | None:
    """anyio 任务组会把底层异常包进 ExceptionGroup，这里递归取出首个匹配。"""
    if isinstance(exc, kind):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            found = _find_exception(sub, kind)
            if found is not None:
                return found
    return None


@asynccontextmanager
async def _remote_session(settings: HttpSettings) -> AsyncIterator[ClientSession]:
    """一次短连接 Streamable HTTP 会话；透传 Key 与 trace id。"""
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    trace = settings.trace_id or _trace_id()
    headers["X-Request-ID"] = trace
    if os.environ.get("MEMORY_TRACE_ID"):
        headers["X-Trace-ID"] = trace

    url = f"{settings.api_url.rstrip('/')}/mcp"
    timeout = httpx.Timeout(60.0, connect=10.0, read=300.0)
    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            async with streamable_http_client(
                url, http_client=client, terminate_on_close=True
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    except ProxyFailure:
        raise
    except BaseException as exc:  # noqa: BLE001 - 任务组会包装底层异常
        mcp_error = _find_exception(exc, McpError)
        if isinstance(mcp_error, McpError):
            raise ProxyFailure(
                mcp_error.error.code,
                mcp_error.error.message,
                mcp_error.error.data,
            ) from exc
        http_error = _find_exception(exc, httpx.HTTPError)
        if http_error is not None:
            logger.warning("memory API unreachable", extra={"trace_id": trace})
            raise ProxyFailure(
                ERR_DEPENDENCY_UNAVAILABLE,
                f"无法连接 Agent Memory API：{type(http_error).__name__}",
            ) from exc
        raise


def _text_content(blocks: Any) -> str:
    return "".join(
        block.text for block in blocks if isinstance(block, types.TextContent)
    )


async def call_remote(
    settings: HttpSettings, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """转发工具调用并返回 structuredContent。"""
    async with _remote_session(settings) as session:
        result = await session.call_tool(
            name,
            arguments,
            read_timeout_seconds=timedelta(minutes=5),
        )

    if result.isError:
        text = _text_content(result.content)
        raise ProxyFailure(ERR_INTERNAL, text or "远端工具执行失败")

    if result.structuredContent is not None:
        return result.structuredContent
    return {"content": _text_content(result.content)}


async def read_remote_resource(settings: HttpSettings, uri: str) -> str:
    """读取远端 Resource，返回 Markdown/文本内容。"""
    async with _remote_session(settings) as session:
        result = await session.read_resource(AnyUrl(uri))
    parts: list[str] = []
    for content in result.contents:
        text = getattr(content, "text", None)
        parts.append(text if text is not None else "")
    return "\n".join(part for part in parts if part)


async def get_remote_prompt(
    settings: HttpSettings,
    name: str,
    arguments: dict[str, str] | None = None,
) -> str:
    """读取远端 Prompt，拼成单段文本。"""
    async with _remote_session(settings) as session:
        result = await session.get_prompt(name, arguments)
    return "\n".join(
        text
        for message in result.messages
        if (text := getattr(message.content, "text", ""))
    )


def _guard(fn: Callable[..., Awaitable[dict[str, Any]]]):
    """统一异常映射，避免桥接层把栈或 Key 泄露给客户端。"""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except ProxyFailure:
            raise
        except ValueError as exc:
            raise ProxyFailure(ERR_INVALID_ARGUMENT, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            # 只记录异常类型：远端错误消息可能包含记忆正文。
            logger.error("stdio proxy failed", extra={"error": type(exc).__name__})
            raise ProxyFailure(ERR_INTERNAL, "桥接转发失败") from exc

    return wrapper


def _decode_error(text: str) -> tuple[int, str, Any]:
    match = _ERROR_MARKER.search(text)
    if match:
        code = int(match.group(1))
        try:
            payload = json.loads(match.group(2))
        except json.JSONDecodeError:  # pragma: no cover
            return code, text, None
        return code, str(payload.get("message", text)), payload.get("data")
    return ERR_INTERNAL, text, None


def install_error_translation(server: FastMCP) -> None:
    """让 stdio 客户端也能收到 JSON-RPC 错误码而不是 isError 文本。"""
    handlers = getattr(server._mcp_server, "request_handlers", {})  # noqa: SLF001
    original = handlers.get(types.CallToolRequest)
    if original is None:
        raise RuntimeError(
            "MCP SDK 结构不兼容：缺少 CallToolRequest handler；"
            "请使用 mcp>=1.30,<1.31"
        )

    async def translated(request: types.CallToolRequest) -> Any:
        result = await original(request)
        payload = getattr(result, "root", None)
        if isinstance(payload, types.CallToolResult) and payload.isError:
            text = "".join(
                block.text
                for block in payload.content
                if isinstance(block, types.TextContent)
            )
            code, message, data = _decode_error(text)
            raise McpError(ErrorData(code=code, message=message, data=data))
        return result

    handlers[types.CallToolRequest] = translated


def build_server(
    api_url: str,
    api_key: str,
    *,
    caller: Caller | None = None,
    reader: Reader | None = None,
    prompter: Prompter | None = None,
    trace_id: str | None = None,
) -> FastMCP:
    """构建 stdio MCP 服务；``caller``/``reader``/``prompter`` 仅用于测试替身。"""
    server = FastMCP(
        name="agent-memory-stdio",
        instructions=(
            "把本地 stdio MCP 请求转发到 Agent Memory HTTP 服务；"
            "工具、资源、提示词语义与远端完全一致。"
        ),
    )
    settings = HttpSettings(api_url=api_url, api_key=api_key, trace_id=trace_id)
    invoke = caller or (lambda name, arguments: call_remote(settings, name, arguments))
    read_proxy = reader or (lambda uri: read_remote_resource(settings, uri))
    prompt_proxy = prompter or (
        lambda name, arguments: get_remote_prompt(settings, name, arguments)
    )

    async def _forward(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await invoke(name, arguments)

    @server.resource(
        "memory://recent",
        name="recent_memories",
        description="远端最近 20 条 active 记忆（Markdown）。",
        mime_type="text/markdown",
    )
    async def recent_memories() -> str:
        return await read_proxy("memory://recent")

    @server.resource(
        "memory://entities/{name}",
        name="entity_memories",
        description="远端实体关联记忆（Markdown）。",
        mime_type="text/markdown",
    )
    async def entity_memories(name: str) -> str:
        return await read_proxy(f"memory://entities/{name}")

    @server.resource(
        "memory://{memory_id}",
        name="memory",
        description="远端单条记忆（Markdown，正文截断 4 KB）。",
        mime_type="text/markdown",
    )
    async def memory_resource(memory_id: str) -> str:
        return await read_proxy(f"memory://{memory_id}")

    @server.prompt(
        name="memory_aware_system",
        description="远端记忆感知系统提示词：先检索、引用 id、冲突以有效事实为准。",
    )
    async def memory_aware_system(task: str | None = None) -> str:
        arguments = {"task": task} if task else None
        return await prompt_proxy("memory_aware_system", arguments)

    @server.tool(name="memory_search", description="检索记忆（转发到 HTTP /mcp）。")
    @_guard
    async def memory_search(
        query: str,
        types: list[str] | None = None,
        scopes: list[str] | None = None,
        workspace_id: str | None = None,
        end_user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        time_range: dict[str, Any] | None = None,
        limit: int = 8,
        mode: str = "balanced",
        include_history: bool = False,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return await _forward(
            "memory_search",
            {
                "query": query,
                "types": types,
                "scopes": scopes,
                "workspace_id": workspace_id,
                "end_user_id": end_user_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "time_range": time_range,
                "limit": limit,
                "mode": mode,
                "include_history": include_history,
                "include_archived": include_archived,
            },
        )

    @server.tool(name="memory_write", description="写入记忆（转发到 HTTP /mcp）。")
    @_guard
    async def memory_write(
        content: str,
        type: str = "semantic",
        scope: str = "user",
        scope_id: str | None = None,
        workspace_id: str | None = None,
        importance: float | None = None,
        entities: list[dict[str, Any]] | None = None,
        source: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        dedupe: bool = True,
        temporal_change: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await _forward(
            "memory_write",
            {
                "content": content,
                "type": type,
                "scope": scope,
                "scope_id": scope_id,
                "workspace_id": workspace_id,
                "importance": importance,
                "entities": entities,
                "source": source,
                "ttl_seconds": ttl_seconds,
                "dedupe": dedupe,
                "temporal_change": temporal_change,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(name="memory_get", description="读取记忆（转发到 HTTP /mcp）。")
    @_guard
    async def memory_get(
        id: str,
        include_links: bool = False,
        include_history: bool = False,
    ) -> dict[str, Any]:
        return await _forward(
            "memory_get",
            {
                "id": id,
                "include_links": include_links,
                "include_history": include_history,
            },
        )

    @server.tool(name="memory_update", description="更新记忆（转发到 HTTP /mcp）。")
    @_guard
    async def memory_update(
        id: str,
        content: str | None = None,
        importance: float | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        valid_to: str | None = None,
    ) -> dict[str, Any]:
        return await _forward(
            "memory_update",
            {
                "id": id,
                "content": content,
                "importance": importance,
                "summary": summary,
                "tags": tags,
                "valid_to": valid_to,
            },
        )

    @server.tool(name="memory_forget", description="遗忘记忆（转发到 HTTP /mcp）。")
    @_guard
    async def memory_forget(
        id: str | None = None,
        query: str | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
        type: str | None = None,
        workspace_id: str | None = None,
        hard: bool = False,
        reason: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return await _forward(
            "memory_forget",
            {
                "id": id,
                "query": query,
                "scope": scope,
                "scope_id": scope_id,
                "type": type,
                "workspace_id": workspace_id,
                "hard": hard,
                "reason": reason,
                "confirm": confirm,
            },
        )

    @server.tool(name="memory_feedback", description="记忆反馈（转发到 HTTP /mcp）。")
    @_guard
    async def memory_feedback(
        id: str, helpful: bool, note: str | None = None
    ) -> dict[str, Any]:
        return await _forward(
            "memory_feedback", {"id": id, "helpful": helpful, "note": note}
        )

    @server.tool(name="memory_consolidate", description="巩固记忆（转发到 HTTP /mcp）。")
    @_guard
    async def memory_consolidate(
        scope: str = "all",
        dry_run: bool = False,
        session_id: str | None = None,
        workspace_id: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> dict[str, Any]:
        return await _forward(
            "memory_consolidate",
            {
                "scope": scope,
                "dry_run": dry_run,
                "session_id": session_id,
                "workspace_id": workspace_id,
                "window_start": window_start,
                "window_end": window_end,
            },
        )

    @server.tool(name="memory_stats", description="记忆统计（转发到 HTTP /mcp）。")
    @_guard
    async def memory_stats(workspace_id: str | None = None) -> dict[str, Any]:
        return await _forward("memory_stats", {"workspace_id": workspace_id})

    @server.tool(name="memory_timeline", description="时间线（转发到 HTTP /mcp）。")
    @_guard
    async def memory_timeline(
        session_id: str | None = None,
        entity: str | None = None,
        start: str | None = None,
        end: str | None = None,
        workspace_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await _forward(
            "memory_timeline",
            {
                "session_id": session_id,
                "entity": entity,
                "start": start,
                "end": end,
                "workspace_id": workspace_id,
                "limit": limit,
            },
        )

    @server.tool(
        name="conversation_ingest", description="对话抽取（转发到 HTTP /mcp）。"
    )
    @_guard
    async def conversation_ingest(
        messages: list[dict[str, Any]],
        session_id: str,
        workspace_id: str | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
        auto_accept: bool = True,
    ) -> dict[str, Any]:
        return await _forward(
            "conversation_ingest",
            {
                "messages": messages,
                "session_id": session_id,
                "workspace_id": workspace_id,
                "scope": scope,
                "scope_id": scope_id,
                "auto_accept": auto_accept,
            },
        )

    install_error_translation(server)
    return server


def _configure_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("MEMORY_MCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="memory-mcp",
        description="Agent Memory stdio MCP 薄桥",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("MEMORY_API_URL", DEFAULT_API_URL),
        help="Agent Memory API 根地址（默认取 MEMORY_API_URL）",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("MEMORY_API_KEY", ""),
        help="API Key（默认取 MEMORY_API_KEY）",
    )
    args = parser.parse_args(argv)

    if not args.api_key:
        parser.exit(
            status=2,
            message="缺少 API Key：使用 --api-key 或环境变量 MEMORY_API_KEY\n",
        )

    _configure_logging()
    server = build_server(args.api_url, args.api_key)
    logger.info("memory-mcp started", extra={"api_url": args.api_url})
    server.run("stdio")


if __name__ == "__main__":
    main()
