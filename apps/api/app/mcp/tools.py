"""10 个 MCP 工具：只做参数校验、鉴权映射与服务调用。

业务逻辑全部复用 REST 路由背后的同一份实现：

- 写入/更新/遗忘/反馈 → :mod:`app.api.memories`（内部调用
  :mod:`app.services.memory_write` 与生命周期服务）；
- 检索 → :mod:`app.services.search`；
- 时间线 → :mod:`app.api.search` 的 timeline 路由；
- 统计 → :mod:`app.services.stats`；
- 巩固 → :mod:`app.services.consolidation`（任务 15）；
- 对话抽取 → :mod:`app.services.extraction`（任务 14）。

Adapter 不落任何业务规则，只把 MCP 参数转成既有请求对象并映射错误码。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Response
from sqlalchemy import func, select

from app.api.ingest import conversation_ingest as ingest_conversation_route
from app.api.memories import (
    ForgetRequest,
    MemoryPatch,
    create_memory,
    delete_memory,
    forget_memories,
    get_memory,
    patch_memory,
    _load_memory,
)
from app.api.search import get_timeline
from app.models import AuditLog, Memory
from app.schemas.memory import MemoryWriteRequest
from app.schemas.search import SearchRequest
from app.services.consolidation import consolidate_nightly, consolidate_session
from app.services.extraction import ConversationIngestRequest
from app.services.memory_write import feedback_memory, get_conflict_judge
from app.services.search import search_memories
from app.services.stats import overview
from mcp.server.fastmcp import FastMCP

from app.mcp.services import (
    ERR_CONFIRMATION_REQUIRED,
    ERR_INVALID_ARGUMENT,
    ERR_NOT_FOUND,
    McpServices,
    ToolFailure,
    authenticated,
    resolve_workspace,
    tool_errors,
)

def _parse_datetime(value: str | datetime | None, field: str) -> datetime | None:
    """解析 ISO 时间；缺省时区按 UTC，非法值统一 -32003。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolFailure(
            ERR_INVALID_ARGUMENT, f"{field} 必须是 ISO 8601 时间"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _write_payload(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if value is not None}


def register_tools(server: FastMCP, services: McpServices) -> None:
    """把 10 个工具注册到 FastMCP 实例。"""

    @server.tool(
        name="memory_search",
        description=(
            "检索记忆。返回 items（机器可读结果）、context_text（可直接注入"
            "提示词）、stats 与 degraded；单通道故障只降级不失败。"
        ),
    )
    @tool_errors
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
        with authenticated(services, "read") as (session, context):
            resolved = resolve_workspace(context, workspace_id)
            request = SearchRequest.model_validate(
                {
                    "query": query,
                    "types": types,
                    "scopes": scopes,
                    "workspace_id": resolved,
                    "end_user_id": end_user_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "time_range": time_range,
                    "limit": limit,
                    "mode": mode,
                    "include_history": include_history,
                    "include_archived": include_archived,
                }
            )
            runtime = services.runtime_provider()
            result = await search_memories(
                session,
                request,
                runtime.embedding,
                runtime.qdrant,
                runtime.neo4j,
            )
            return result.model_dump(mode="json")

    @server.tool(
        name="memory_write",
        description=(
            "写入一条记忆。返回 id、action（created/merged/superseded）、"
            "superseded_ids 与 importance；去重与冲突裁决复用写入服务。"
        ),
    )
    @tool_errors
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
        with authenticated(services, "write", write=True) as (session, context):
            resolved = resolve_workspace(context, workspace_id)
            payload = MemoryWriteRequest.model_validate(
                {
                    "content": content,
                    "type": type,
                    "scope": scope,
                    "scope_id": scope_id,
                    "workspace_id": resolved,
                    "importance": importance,
                    "entities": entities or [],
                    "source": source,
                    "ttl_seconds": ttl_seconds,
                    "dedupe": dedupe,
                    "temporal_change": temporal_change,
                    "idempotency_key": idempotency_key,
                }
            )
            body = create_memory(
                payload, context, session, judge=get_conflict_judge()
            )
            memory = session.get(Memory, body["id"])
            if memory is None:  # pragma: no cover - 同事务刚写入
                raise ToolFailure(ERR_NOT_FOUND, "记忆不存在")
            return {
                "id": body["id"],
                "action": body["action"],
                "superseded_ids": body["superseded_ids"],
                "importance": memory.importance,
            }

    @server.tool(
        name="memory_get",
        description="按 id 读取完整记忆；可附带关联边与版本链。",
    )
    @tool_errors
    async def memory_get(
        id: str,
        include_links: bool = False,
        include_history: bool = False,
    ) -> dict[str, Any]:
        with authenticated(services, "read") as (session, context):
            body = get_memory(id, context, session)
            if not include_history:
                body.pop("history", None)
            if not include_links:
                body.pop("links", None)
            return body

    @server.tool(
        name="memory_update",
        description="更新记忆内容、重要性、摘要、标签或有效期，返回快照与审计 id。",
    )
    @tool_errors
    async def memory_update(
        id: str,
        content: str | None = None,
        importance: float | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        valid_to: str | None = None,
    ) -> dict[str, Any]:
        with authenticated(services, "write", write=True) as (session, context):
            patch = MemoryPatch.model_validate(
                _write_payload(
                    {
                        "content": content,
                        "importance": importance,
                        "summary": summary,
                        "tags": tags,
                        "valid_to": _parse_datetime(valid_to, "valid_to"),
                    }
                )
            )
            body = patch_memory(id, patch, context, session)
            audit = session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.target_id == id,
                    AuditLog.action == "memory.updated",
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            ).first()
            return {**body, "audit_id": audit.id if audit is not None else None}

    @server.tool(
        name="memory_forget",
        description=(
            "遗忘记忆：按 id 或 query 批量匹配。需要 admin Key；"
            "hard=true 还必须 confirm=true，否则返回 -32005。"
        ),
    )
    @tool_errors
    async def memory_forget(
        id: str | None = None,
        query: str | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
        type: str | None = None,
        workspace_id: str | None = None,
        hard: bool = False,
        reason: str | None = None,  # noqa: ARG001 - 保留给审计扩展
        confirm: bool = False,
    ) -> dict[str, Any]:
        if hard and not confirm:
            raise ToolFailure(
                ERR_CONFIRMATION_REQUIRED, "硬删除需要 confirm=true", {"hard": True}
            )
        if not id and not (query and query.strip()):
            raise ToolFailure(ERR_INVALID_ARGUMENT, "id 或 query 至少提供一个")

        with authenticated(services, "admin", write=True) as (session, context):
            resolved = resolve_workspace(context, workspace_id)
            if id:
                result = delete_memory(
                    id,
                    hard=hard,
                    confirm=confirm,
                    context=context,
                    session=session,
                )
                if isinstance(result, Response):
                    return {
                        "affected": 1,
                        "ids": [id],
                        "hard": False,
                        "deleted": True,
                    }
                return {"affected": 1, "ids": [id], **result}

            payload = ForgetRequest.model_validate(
                {
                    "query": query.strip(),
                    "scope": scope,
                    "scope_id": scope_id,
                    "type": type,
                    "workspace_id": resolved,
                    "hard": hard,
                    "confirm": confirm,
                }
            )
            result = forget_memories(payload, context, session)
            return {**result, "hard": hard}

    @server.tool(
        name="memory_feedback",
        description="记录有用/无用反馈，调整记忆的 importance 与 stability。",
    )
    @tool_errors
    async def memory_feedback(
        id: str,
        helpful: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        with authenticated(services, "write", write=True) as (session, context):
            memory = _load_memory(session, context, id)
            feedback_memory(
                session,
                memory,
                helpful=helpful,
                reason=note,
                actor=context,
            )
            return {
                "id": memory.id,
                "importance": memory.importance,
                "stability": memory.stability,
                "access_count": memory.access_count,
            }

    @server.tool(
        name="memory_consolidate",
        description=(
            "触发票据巩固：scope 可为 all、workspace id 或 session:<id>；"
            "dry_run=true 只返回差异摘要，不写库。需要 admin Key。"
        ),
    )
    @tool_errors
    async def memory_consolidate(
        scope: str = "all",
        dry_run: bool = False,
        session_id: str | None = None,
        workspace_id: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> dict[str, Any]:
        with authenticated(services, "admin", write=True) as (session, context):
            resolved = resolve_workspace(context, workspace_id)
            target_session = session_id
            if target_session is None and scope.startswith("session:"):
                target_session = scope.split(":", 1)[1]
            elif (
                target_session is None
                and scope not in {"", "all", resolved}
                and not scope.startswith("session:")
            ):
                # 兼容“直接传 session id”的调用方式；先确认它不是 workspace。
                target_session = scope

            if target_session:
                diff = consolidate_session(
                    session,
                    target_session,
                    dry_run=dry_run,
                    workspace_id=resolved,
                    actor=context,
                )
            else:
                end = _parse_datetime(window_end, "window_end") or datetime.now(UTC)
                start = _parse_datetime(window_start, "window_start") or (
                    end - timedelta(hours=24)
                )
                if end <= start:
                    raise ToolFailure(
                        ERR_INVALID_ARGUMENT, "window_end 必须晚于 window_start"
                    )
                diff = consolidate_nightly(
                    session,
                    start,
                    end,
                    scope=resolved,
                    dry_run=dry_run,
                    actor=context,
                )
            return diff.to_dict()

    @server.tool(
        name="memory_stats",
        description="返回记忆总量、类型/作用域/状态分布、归档率、待审数与平均重要性。",
    )
    @tool_errors
    async def memory_stats(workspace_id: str | None = None) -> dict[str, Any]:
        with authenticated(services, "read") as (session, context):
            resolved = resolve_workspace(context, workspace_id)
            payload = overview(session, resolved).to_dict()
            average = session.scalar(
                select(func.avg(Memory.importance)).where(
                    Memory.workspace_id == resolved,
                    Memory.deleted_at.is_(None),
                    Memory.status != "deleted",
                )
            )
            total = payload["total_memories"]
            archived = payload["by_status"].get("archived", 0)
            payload.update(
                {
                    "workspace_id": resolved,
                    "avg_importance": round(float(average or 0.0), 4),
                    "archive_rate": round(archived / total, 4) if total else 0.0,
                }
            )
            return payload

    @server.tool(
        name="memory_timeline",
        description="按 session_id 或实体名合并时间线，可按 from/to 过滤。",
    )
    @tool_errors
    async def memory_timeline(
        session_id: str | None = None,
        entity: str | None = None,
        start: str | None = None,
        end: str | None = None,
        workspace_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if session_id is None and entity is None:
            raise ToolFailure(
                ERR_INVALID_ARGUMENT, "session_id 或 entity 至少提供一个"
            )
        with authenticated(services, "read") as (session, context):
            resolved = resolve_workspace(context, workspace_id)
            return get_timeline(
                session_id=session_id,
                entity=entity,
                start=_parse_datetime(start, "start"),
                end=_parse_datetime(end, "end"),
                workspace_id=resolved,
                limit=limit,
                context=context,
                session=session,
            )

    @server.tool(
        name="conversation_ingest",
        description="登记一次对话抽取；返回 task_id 与候选数，重复消息幂等。",
    )
    @tool_errors
    async def conversation_ingest(
        messages: list[dict[str, Any]],
        session_id: str,
        workspace_id: str | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
        auto_accept: bool = True,
    ) -> dict[str, Any]:
        with authenticated(services, "write", write=True) as (session, context):
            resolved = resolve_workspace(context, workspace_id)
            payload = ConversationIngestRequest.model_validate(
                {
                    "workspace_id": resolved,
                    "session_id": session_id,
                    "messages": messages,
                    "scope": scope,
                    "scope_id": scope_id,
                    "auto_accept": auto_accept,
                }
            )
            return ingest_conversation_route(payload, context, session)


__all__ = ["register_tools"]
