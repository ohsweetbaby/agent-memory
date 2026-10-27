"""四路召回检索服务。

通道：Qdrant 向量、Postgres 关键词、Neo4j 图扩展、重要性 × 时近性先验。
四路并行（``asyncio.gather``），同步适配器统一走线程池；单通道失败只记录
``degraded``，整体仍返回 200。

融合严格遵循规格 §6.2：``RRF(k=60)`` → ``final = rrf × (0.8 + 0.2×importance)
× scope_weight``，程序性记忆再乘成功率 ``(success+1)/(success+failure+2)``。
所有通道先按 workspace 与可见 ``(scope, scope_id)`` 收窄，向量/图通道还会
在应用层复核 payload / 事实源，避免跨作用域泄漏。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import UUID

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from memory_core.context import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextItem,
    assemble_context,
)
from memory_core.decay import DecayParams, retention, stability_days
from memory_core.fusion import DEFAULT_RRF_K, final_score, rrf
from memory_core.scope import ScopeContext, visible_scopes
from memory_core.types import MemoryType, Scope

from app.adapters.fts import SEARCH_TOP_K, PostgresFTS, tokenize
from app.adapters.llm import NoopReranker
from app.config import get_current_settings
from app.errors import BadRequestError
from app.models import Entity, Memory
from app.models.base import utcnow
from app.schemas.search import SearchItem, SearchRequest, SearchResult, SearchStats

logger = logging.getLogger(__name__)

#: 向量与图通道在过滤前多取一些，留足作用域/有效期过滤的余量。
VECTOR_OVERSAMPLE = 8

#: 先验通道先在 SQL 取一批候选，再在应用层按保留率排序。
PRIOR_CANDIDATES = 200

#: deep 模式参与重排的条数。
RERANK_TOP_N = 20

#: 图扩展的种子实体上限与结果上限。
GRAPH_SEED_ENTITIES = 3
GRAPH_RESULT_LIMIT = 30

#: recent 模式的时近窗口。
RECENT_WINDOW_DAYS = 30

#: 历史/归档状态；用于 ``historical`` 标记与上下文分组。
_HISTORICAL_STATUSES = frozenset({"archived", "superseded"})

_CHANNEL_ORDER = ("vector", "keyword", "graph", "prior")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_datetime(value: Any) -> datetime | None:
    """解析 Qdrant payload 中的 ISO 时间；非法值视为缺失。"""
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _as_utc(datetime.fromisoformat(value.strip()))
        except ValueError:
            return None
    return None


def _counter(mapping: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = mapping.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(max(value, 0))
    return None


def procedural_bonus(metadata: dict[str, Any]) -> float:
    """程序性记忆成功率加成：``(success+1)/(success+failure+2)``。

    计数优先读 ``metadata.skill``（规格 §5.6），兼容顶层字段；缺失时视为
    0/0，自然得到缺省 0.5。
    """
    if not isinstance(metadata, dict):
        return 0.5
    skill = metadata.get("skill")
    sources = [skill, metadata] if isinstance(skill, dict) else [metadata]

    success = failure = None
    for source in sources:
        if success is None:
            success = _counter(source, "success_count", "successes", "success")
        if failure is None:
            failure = _counter(source, "failure_count", "failures", "failure")
    resolved_success = success or 0.0
    resolved_failure = failure or 0.0
    return (resolved_success + 1.0) / (
        resolved_success + resolved_failure + 2.0
    )


@dataclass(frozen=True, slots=True)
class _SearchFilters:
    """一次检索的固定过滤条件，供四路通道共享。"""

    workspace_id: str
    scope_pairs: tuple[tuple[str, str | None], ...]
    types: tuple[str, ...] | None
    statuses: tuple[str, ...]
    include_history: bool
    time_from: datetime | None
    time_to: datetime | None
    recent_since: datetime | None


def _build_filters(req: SearchRequest, workspace_id: str) -> _SearchFilters:
    context = ScopeContext(
        workspace_id=workspace_id,
        agent_id=req.agent_id,
        end_user_id=req.end_user_id,
        session_id=req.session_id,
    )
    requested = list(req.scopes) if req.scopes else None
    pairs = tuple(
        (scope.value, scope_id)
        for scope, scope_id in visible_scopes(context, requested)
    )

    statuses = ["active"]
    if req.include_history:
        statuses.append("superseded")
    if req.include_archived or req.mode == "deep":
        statuses.append("archived")

    time_range = req.time_range
    return _SearchFilters(
        workspace_id=workspace_id,
        scope_pairs=pairs,
        types=tuple(memory_type.value for memory_type in req.types)
        if req.types
        else None,
        statuses=tuple(dict.fromkeys(statuses)),
        include_history=req.include_history,
        time_from=_as_utc(time_range.from_) if time_range else None,
        time_to=_as_utc(time_range.to) if time_range else None,
        recent_since=(utcnow() - timedelta(days=RECENT_WINDOW_DAYS))
        if req.mode == "recent"
        else None,
    )


def _scope_sql(filters: _SearchFilters) -> Any:
    conditions = []
    for scope, scope_id in filters.scope_pairs:
        if scope_id:
            conditions.append(
                and_(Memory.scope == scope, Memory.scope_id == scope_id)
            )
        else:
            conditions.append(
                and_(Memory.scope == scope, Memory.scope_id.is_(None))
            )
    return or_(*conditions) if conditions else false()


def _sql_conditions(filters: _SearchFilters) -> list[Any]:
    conditions: list[Any] = [
        Memory.workspace_id == filters.workspace_id,
        Memory.deleted_at.is_(None),
        Memory.status.in_(filters.statuses),
        _scope_sql(filters),
    ]
    if filters.types:
        conditions.append(Memory.type.in_(filters.types))
    if not filters.include_history:
        conditions.append(
            or_(Memory.valid_to.is_(None), Memory.valid_to > func.now())
        )
    if filters.time_from is not None:
        conditions.append(Memory.last_reinforced_at >= filters.time_from)
    if filters.time_to is not None:
        conditions.append(Memory.last_reinforced_at <= filters.time_to)
    if filters.recent_since is not None:
        conditions.append(Memory.last_reinforced_at >= filters.recent_since)
    return conditions


def _row_allowed(memory: Memory, filters: _SearchFilters, now: datetime) -> bool:
    if memory.workspace_id != filters.workspace_id:
        return False
    if memory.status not in filters.statuses or memory.deleted_at is not None:
        return False
    key = (memory.scope, memory.scope_id)
    if key not in filters.scope_pairs:
        return False
    if filters.types and memory.type not in filters.types:
        return False
    if not filters.include_history:
        valid_to = _as_utc(memory.valid_to)
        if valid_to is not None and valid_to <= now:
            return False
    reinforced = _as_utc(memory.last_reinforced_at)
    if filters.time_from and (reinforced is None or reinforced < filters.time_from):
        return False
    if filters.time_to and (reinforced is None or reinforced > filters.time_to):
        return False
    if filters.recent_since and (
        reinforced is None or reinforced < filters.recent_since
    ):
        return False
    return True


def _qdrant_filter(filters: _SearchFilters) -> dict[str, Any]:
    resolved: dict[str, Any] = {
        "workspace_id": filters.workspace_id,
        "status": list(filters.statuses),
    }
    if filters.types:
        resolved["type"] = list(filters.types)
    return resolved


def _payload_allowed(
    payload: dict[str, Any], filters: _SearchFilters, now: datetime
) -> bool:
    """Qdrant 只支持等值匹配，作用域/有效期在这里复核。"""
    if payload.get("workspace_id") != filters.workspace_id:
        return False
    scope_id = payload.get("scope_id")
    key = (str(payload.get("scope")), str(scope_id) if scope_id else None)
    if key not in filters.scope_pairs:
        return False
    if filters.types and payload.get("type") not in filters.types:
        return False
    if not filters.include_history:
        valid_to = _parse_datetime(payload.get("valid_to"))
        if valid_to is not None and valid_to <= now:
            return False
    reinforced = _parse_datetime(payload.get("last_reinforced_at"))
    if filters.time_from and (reinforced is None or reinforced < filters.time_from):
        return False
    if filters.time_to and (reinforced is None or reinforced > filters.time_to):
        return False
    if filters.recent_since and (
        reinforced is None or reinforced < filters.recent_since
    ):
        return False
    return True


@dataclass(slots=True)
class _ChannelResult:
    ids: list[str] = field(default_factory=list)
    rows: dict[str, Memory] = field(default_factory=dict)


def _memory_retention(memory: Memory, now: datetime) -> float:
    stability = stability_days(
        float(memory.importance or 0.0),
        int(memory.access_count or 0),
        float(memory.stability or 1.0),
        DecayParams(),
    )
    reinforced = _as_utc(memory.last_reinforced_at) or now
    return retention(reinforced, now, stability)


# ---------------------------------------------------------------------------
# 通道实现
# ---------------------------------------------------------------------------


async def _vector_channel(
    query: str,
    filters: _SearchFilters,
    embedding: Any,
    qdrant: Any,
) -> _ChannelResult:
    vectors = await embedding.embed([query])
    if not vectors:
        return _ChannelResult()
    hits = await run_in_threadpool(
        qdrant.search,
        vectors[0],
        _qdrant_filter(filters),
        SEARCH_TOP_K * VECTOR_OVERSAMPLE,
    )
    now = utcnow()
    ids: list[str] = []
    for hit in hits:
        if _payload_allowed(dict(hit.payload or {}), filters, now):
            ids.append(str(hit.id))
            if len(ids) >= SEARCH_TOP_K:
                break
    return _ChannelResult(ids=ids)


def _keyword_sync(
    db: Session,
    conditions: Sequence[Any],
    query: str,
    *,
    exact: bool,
) -> _ChannelResult:
    hits = PostgresFTS(db).search(
        conditions=conditions,
        query=query,
        limit=SEARCH_TOP_K,
        exact=exact,
    )
    return _ChannelResult(
        ids=[hit.memory.id for hit in hits],
        rows={hit.memory.id: hit.memory for hit in hits},
    )


def _exact_extra_sync(
    db: Session, filters: _SearchFilters, query: str
) -> _ChannelResult:
    """exact 模式的 ID 精确匹配与标签匹配。"""
    now = utcnow()
    ids: list[str] = []
    rows: dict[str, Memory] = {}
    try:
        memory_id = str(UUID(query.strip()))
    except (ValueError, AttributeError):
        memory_id = ""
    if memory_id:
        row = db.get(Memory, memory_id)
        if row is not None and _row_allowed(row, filters, now):
            ids.append(row.id)
            rows[row.id] = row

    tag = query.strip()
    if tag:
        statement = (
            select(Memory)
            .where(
                *_sql_conditions(filters),
                Memory.metadata_["tags"].contains([tag]),
            )
            .limit(10)
        )
        for row in db.scalars(statement):
            if row.id not in rows:
                rows[row.id] = row
                ids.append(row.id)
    return _ChannelResult(ids=ids, rows=rows)


async def _keyword_channel(
    db: Session,
    filters: _SearchFilters,
    query: str,
    lock: asyncio.Lock,
    *,
    exact: bool,
) -> _ChannelResult:
    conditions = _sql_conditions(filters)
    async with lock:
        result = await run_in_threadpool(
            _keyword_sync, db, conditions, query, exact=exact
        )
    if not exact:
        return result

    async with lock:
        extra = await run_in_threadpool(_exact_extra_sync, db, filters, query)
    if extra.ids:
        extra_set = set(extra.ids)
        result.ids = list(
            dict.fromkeys(
                extra.ids
                + [value for value in result.ids if value not in extra_set]
            )
        )
        result.rows.update(extra.rows)
    return result


def _prior_sync(
    db: Session, conditions: Sequence[Any], mode: str
) -> _ChannelResult:
    now = utcnow()
    rows = list(
        db.scalars(
            select(Memory)
            .where(*conditions)
            .order_by(
                Memory.importance.desc(),
                Memory.last_reinforced_at.desc(),
                Memory.id,
            )
            .limit(PRIOR_CANDIDATES)
        )
    )
    scored: list[tuple[Memory, float]] = []
    for memory in rows:
        current = _memory_retention(memory, now)
        if mode == "recent":
            # 时近性权重更高：保留率按平方根衰减，近期记忆的相对优势更明显。
            prior = float(memory.importance or 0.0) * (current**0.5)
        else:
            prior = float(memory.importance or 0.0) * current
        scored.append((memory, prior))
    scored.sort(key=lambda item: (-item[1], item[0].id))
    ordered = [memory for memory, _ in scored[:SEARCH_TOP_K]]
    return _ChannelResult(
        ids=[memory.id for memory in ordered],
        rows={memory.id: memory for memory in ordered},
    )


async def _prior_channel(
    db: Session,
    filters: _SearchFilters,
    mode: str,
    lock: asyncio.Lock,
) -> _ChannelResult:
    conditions = _sql_conditions(filters)
    async with lock:
        return await run_in_threadpool(_prior_sync, db, conditions, mode)


def _seed_entity_ids(db: Session, workspace_id: str, query: str) -> list[str]:
    terms = [query.strip(), *tokenize(query)]
    unique: list[str] = []
    for term in terms:
        if len(term) < 2 or term in unique:
            continue
        unique.append(term)
        if len(unique) >= 12:
            break
    if not unique:
        return []
    whole = query.strip()
    statement = (
        select(Entity.id)
        .where(
            Entity.workspace_id == workspace_id,
            or_(
                Entity.name.in_(unique),
                func.strpos(whole, Entity.name) > 0,
            ),
        )
        .order_by(Entity.name)
        .limit(GRAPH_SEED_ENTITIES)
    )
    return [str(value) for value in db.scalars(statement)]


def _allowed_ids(
    db: Session, filters: _SearchFilters, memory_ids: Sequence[str]
) -> set[str]:
    if not memory_ids:
        return set()
    statement = select(Memory.id).where(
        *_sql_conditions(filters),
        Memory.id.in_(list(memory_ids)),
    )
    return {str(value) for value in db.scalars(statement)}


async def _graph_channel(
    db: Session,
    filters: _SearchFilters,
    query: str,
    neo4j: Any,
    lock: asyncio.Lock,
    *,
    hops: int,
) -> _ChannelResult:
    if neo4j is None:
        raise RuntimeError("图通道未配置")
    async with lock:
        seeds = await run_in_threadpool(
            _seed_entity_ids, db, filters.workspace_id, query
        )
    if not seeds:
        return _ChannelResult()

    expanded = await run_in_threadpool(
        neo4j.expand, seeds, hops, GRAPH_RESULT_LIMIT
    )
    if not expanded:
        return _ChannelResult()

    async with lock:
        allowed = await run_in_threadpool(_allowed_ids, db, filters, expanded)
    ordered = [str(value) for value in expanded if str(value) in allowed]
    return _ChannelResult(ids=ordered[:SEARCH_TOP_K])


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


async def _rewrite_query(llm: Any, query: str, mode: str) -> str:
    """deep 模式下可选查询改写；失败或未配置时原样返回。"""
    settings = get_current_settings()
    if mode != "deep" or llm is None or not settings.search_query_rewrite:
        return query
    try:
        rewritten = await llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "把用户问题改写为适合记忆检索的简短中文关键词，"
                        "只输出改写结果，不要解释。"
                    ),
                },
                {"role": "user", "content": query},
            ]
        )
    except Exception:  # 改写是可选增强，失败必须静默回退
        logger.warning("query rewrite failed; using original query")
        return query
    text = (rewritten or "").strip()
    return text if text and len(text) <= 200 else query


def _source_view(memory: Memory) -> tuple[dict[str, Any] | None, str | None]:
    """只暴露来源类型/id/原话，绝不回传完整 metadata。"""
    metadata = memory.metadata_ if isinstance(memory.metadata_, dict) else {}
    quote = metadata.get("source_quote")
    quote = quote.strip() if isinstance(quote, str) and quote.strip() else None
    source: dict[str, Any] = {}
    if memory.source_type:
        source["type"] = memory.source_type
    if memory.source_id:
        source["id"] = memory.source_id
    if quote:
        source["quote"] = quote
    return (source or None), quote


def _build_why(
    memory_id: str,
    rank_maps: dict[str, dict[str, int]],
    rrf_scores: dict[str, float],
) -> dict[str, Any]:
    return {
        "vector_rank": rank_maps.get("vector", {}).get(memory_id),
        "bm25_rank": rank_maps.get("keyword", {}).get(memory_id),
        "graph_rank": rank_maps.get("graph", {}).get(memory_id),
        "prior_rank": rank_maps.get("prior", {}).get(memory_id),
        "rrf": rrf_scores.get(memory_id, 0.0),
    }


async def search_memories(
    db: Session,
    req: SearchRequest,
    embedding_provider: Any,
    qdrant: Any,
    neo4j: Any,
    llm: Any = None,
    reranker: Any = None,
) -> SearchResult:
    """执行四路召回并返回融合结果。

    ``db`` 是同步 Session；关键词/先验/图种子查询共用一个 ``asyncio.Lock``，
    保证同一时刻只有一个线程池任务使用该 Session。
    """
    started = time.perf_counter()
    workspace_id = (req.workspace_id or "").strip()
    if not workspace_id:
        raise BadRequestError("必须指定 workspace_id", code="invalid_request")
    filters = _build_filters(req, workspace_id)
    query = await _rewrite_query(llm, req.query, req.mode)
    db_lock = asyncio.Lock()

    channels: list[str] = ["keyword"] if req.mode == "exact" else list(_CHANNEL_ORDER)
    tasks: dict[str, Any] = {}
    if "vector" in channels:
        tasks["vector"] = _vector_channel(
            query, filters, embedding_provider, qdrant
        )
    if "keyword" in channels:
        tasks["keyword"] = _keyword_channel(
            db, filters, query, db_lock, exact=req.mode == "exact"
        )
    if "graph" in channels:
        tasks["graph"] = _graph_channel(
            db,
            filters,
            query,
            neo4j,
            db_lock,
            hops=2 if req.mode == "deep" else 1,
        )
    if "prior" in channels:
        tasks["prior"] = _prior_channel(db, filters, req.mode, db_lock)

    gathered = await asyncio.gather(
        *(tasks[name] for name in channels), return_exceptions=True
    )
    degraded: list[str] = []
    results: dict[str, _ChannelResult] = {}
    for name, outcome in zip(channels, gathered):
        if isinstance(outcome, BaseException):
            degraded.append(name)
            logger.warning(
                "search channel failed",
                extra={"channel": name, "workspace_id": workspace_id},
                exc_info=outcome,
            )
        else:
            results[name] = outcome

    rows: dict[str, Memory] = {}
    for result in results.values():
        rows.update(result.rows)

    candidate_ids = list(
        dict.fromkeys(
            memory_id
            for name in channels
            if name in results
            for memory_id in results[name].ids
        )
    )
    missing = [memory_id for memory_id in candidate_ids if memory_id not in rows]
    if missing:
        # 用事实源复核向量/图通道的 id：投影可能滞后于 Postgres。
        loaded = db.scalars(
            select(Memory).where(
                *_sql_conditions(filters),
                Memory.id.in_(missing),
            )
        )
        rows.update({memory.id: memory for memory in loaded})

    rank_maps: dict[str, dict[str, int]] = {}
    rankings: dict[str, list[str]] = {}
    for name, result in results.items():
        ordered_ids = [memory_id for memory_id in result.ids if memory_id in rows]
        if not ordered_ids:
            continue
        rankings[name] = ordered_ids
        rank_maps[name] = {
            memory_id: rank
            for rank, memory_id in enumerate(ordered_ids, start=1)
        }
    rrf_scores = rrf(rankings, k=DEFAULT_RRF_K) if rankings else {}

    now = utcnow()
    final_scores: dict[str, float] = {}
    retention_values: dict[str, float] = {}
    for memory_id, rrf_value in rrf_scores.items():
        memory = rows[memory_id]
        current = _memory_retention(memory, now)
        retention_values[memory_id] = current
        score = final_score(
            rrf_value, float(memory.importance or 0.0), memory.scope
        )
        if memory.type == MemoryType.PROCEDURAL.value:
            score *= procedural_bonus(memory.metadata_ or {})
        final_scores[memory_id] = score

    ordered = sorted(
        final_scores, key=lambda memory_id: (-final_scores[memory_id], memory_id)
    )

    if req.mode == "deep" and ordered:
        reranker = reranker or NoopReranker()
        head = ordered[:RERANK_TOP_N]
        try:
            reranked = await reranker.rerank(
                query, [rows[memory_id].content for memory_id in head]
            )
            if len(reranked) == len(head):
                ranked = sorted(
                    (
                        (float(score), position, memory_id)
                        for position, (score, memory_id) in enumerate(
                            zip(reranked, head)
                        )
                    ),
                    key=lambda item: (-item[0], item[1]),
                )
                ordered = [memory_id for _, _, memory_id in ranked] + ordered[
                    len(head) :
                ]
        except Exception:  # 重排是可选增强，失败静默跳过
            logger.warning("rerank failed; keeping fusion order")

    selected = ordered[: req.limit]
    items: list[SearchItem] = []
    context_items: list[ContextItem] = []
    for memory_id in selected:
        memory = rows[memory_id]
        source, quote = _source_view(memory)
        historical = memory.status in _HISTORICAL_STATUSES
        score = final_scores[memory_id]
        why = _build_why(memory_id, rank_maps, rrf_scores)
        items.append(
            SearchItem(
                id=memory.id,
                type=memory.type,
                scope=memory.scope,
                scope_id=memory.scope_id,
                content=memory.content,
                importance=float(memory.importance or 0.0),
                confidence=float(memory.confidence or 0.0),
                retention=round(retention_values[memory_id], 6),
                status=memory.status,
                historical=historical,
                valid_from=memory.valid_from,
                timestamp=memory.last_reinforced_at,
                superseded_by=memory.superseded_by,
                source=source,
                why=why,
                score=score,
            )
        )
        context_items.append(
            ContextItem(
                id=memory.id,
                type=memory.type,
                content=memory.content,
                importance=float(memory.importance or 0.0),
                confidence=float(memory.confidence or 0.0),
                retention=retention_values[memory_id],
                timestamp=memory.last_reinforced_at,
                scope=Scope(memory.scope),
                source_quote=quote,
                why=why,
                historical=historical,
                # T6 裁决：同批 ContextItem 必须全部携带 final 分数。
                score=score,
            )
        )

    budget = getattr(
        get_current_settings(),
        "search_context_budget_tokens",
        DEFAULT_CONTEXT_BUDGET_TOKENS,
    )
    context_text, _ = assemble_context(context_items, budget_tokens=budget)

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    stats = SearchStats(
        candidates=len(rrf_scores),
        returned=len(items),
        latency_ms=latency_ms,
        mode=req.mode,
        channels={
            name: len(rankings.get(name, []))
            for name in channels
            if name in results
        },
    )
    return SearchResult(
        items=items,
        context_text=context_text,
        stats=stats,
        degraded=[name for name in _CHANNEL_ORDER if name in degraded],
    )


__all__ = [
    "procedural_bonus",
    "search_memories",
]
