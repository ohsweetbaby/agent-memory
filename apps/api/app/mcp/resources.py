"""MCP Resources：单条记忆、最近记忆与实体关联记忆的 Markdown 视图。

资源与工具共用同一套 Key 鉴权；单条正文截断到 4 KB，避免把大段原文塞进
客户端上下文。资源 URI 遵循规格 §7.4：

- ``memory://{memory_id}``
- ``memory://recent``（最近 20 条 active）
- ``memory://entities/{name}``
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import unquote

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from sqlalchemy import select

from app.api.memories import _load_memory
from app.models import Entity, Memory, MemoryEntity
from app.mcp.services import (
    ERR_NOT_FOUND,
    McpServices,
    ToolFailure,
    authenticated,
    map_exception,
    resolve_workspace,
)

RECENT_LIMIT = 20
ENTITY_LIMIT = 50
CONTENT_TRUNCATE_BYTES = 4096


def _truncate(text: str, limit: int = CONTENT_TRUNCATE_BYTES) -> str:
    """按 UTF-8 字节截断，避免切坏多字节字符。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + "\n\n…（已截断）"


def _render_memory(memory: Memory) -> str:
    source = memory.source_type or "manual"
    lines = [
        f"### {memory.type} · {memory.scope} · {memory.id}",
        "",
        f"- 重要性：{memory.importance:.2f}",
        f"- 置信度：{memory.confidence:.2f}",
        f"- 状态：{memory.status}",
        f"- 来源：{source}",
        f"- 更新时间：{(memory.updated_at or memory.created_at).isoformat()}",
        "",
        _truncate(memory.content),
    ]
    if memory.summary:
        lines.extend(["", f"> {memory.summary}"])
    return "\n".join(lines)


def _render_memories(memories: Sequence[Memory], title: str) -> str:
    header = f"# {title}\n\n共 {len(memories)} 条；内容按单条 4 KB 截断。"
    if not memories:
        return header + "\n\n暂无记忆。"
    return header + "\n\n" + "\n\n---\n\n".join(_render_memory(item) for item in memories)


def _resource_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """资源异常也映射到规格错误码（资源读取不走工具错误包装）。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except ToolFailure as exc:
            raise McpError(
                ErrorData(code=exc.code, message=exc.message, data=exc.data)
            ) from exc
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射，避免裸栈
            failure = map_exception(exc)
            raise McpError(
                ErrorData(
                    code=failure.code,
                    message=failure.message,
                    data=failure.data,
                )
            ) from exc

    return wrapper


def register_resources(server: FastMCP, services: McpServices) -> None:
    """注册三个资源；字面量 ``memory://recent`` 优先于 ``{memory_id}`` 模板。"""

    @server.resource(
        "memory://recent",
        name="recent_memories",
        description="当前 workspace 最近 20 条 active 记忆（Markdown）。",
        mime_type="text/markdown",
    )
    @_resource_errors
    def recent_memories() -> str:
        with authenticated(services, "read") as (session, context):
            workspace_id = resolve_workspace(context, None)
            rows = list(
                session.scalars(
                    select(Memory)
                    .where(
                        Memory.workspace_id == workspace_id,
                        Memory.status == "active",
                        Memory.deleted_at.is_(None),
                    )
                    .order_by(Memory.created_at.desc(), Memory.id.desc())
                    .limit(RECENT_LIMIT)
                )
            )
            return _render_memories(rows, "最近记忆")

    @server.resource(
        "memory://entities/{name}",
        name="entity_memories",
        description="某实体关联的记忆（Markdown，按更新时间倒序）。",
        mime_type="text/markdown",
    )
    @_resource_errors
    def entity_memories(name: str) -> str:
        # URI 模板参数可能是百分号编码（中文实体名），先解码再查库。
        name = unquote(name)
        with authenticated(services, "read") as (session, context):
            workspace_id = resolve_workspace(context, None)
            statement = (
                select(Memory)
                .join(MemoryEntity, MemoryEntity.memory_id == Memory.id)
                .join(Entity, Entity.id == MemoryEntity.entity_id)
                .where(
                    Entity.workspace_id == workspace_id,
                    Entity.name == name,
                    Memory.workspace_id == workspace_id,
                    Memory.status.not_in(("deleted", "purged")),
                    Memory.deleted_at.is_(None),
                )
                .order_by(Memory.updated_at.desc(), Memory.id.desc())
                .limit(ENTITY_LIMIT)
            )
            rows = list(session.scalars(statement))
            if not rows:
                exists = session.scalar(
                    select(Entity.id).where(
                        Entity.workspace_id == workspace_id, Entity.name == name
                    )
                )
                if exists is None:
                    raise McpError(
                        ErrorData(
                            code=ERR_NOT_FOUND, message=f"实体不存在：{name}"
                        )
                    )
            return _render_memories(rows, f"实体：{name}")

    @server.resource(
        "memory://{memory_id}",
        name="memory",
        description="单条记忆的 Markdown 视图（正文截断到 4 KB）。",
        mime_type="text/markdown",
    )
    @_resource_errors
    def memory_resource(memory_id: str) -> str:
        with authenticated(services, "read") as (session, context):
            memory = _load_memory(session, context, memory_id)
            return _render_memory(memory)


__all__ = ["register_resources"]
