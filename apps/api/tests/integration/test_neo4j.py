"""Neo4j 图投影：MERGE 幂等、实体扩展与删除。"""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.embeddings import FakeEmbedding
from app.adapters.neo4j import GraphEntity, GraphLink, Neo4jIndex
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime
from app.models import Entity, Memory, ProjectionState
from app.models.base import utcnow
from app.worker_cli import run_rebuild

pytestmark = pytest.mark.integration


@pytest.fixture()
def memory(db_session: Session) -> Memory:
    row = Memory(
        workspace_id="ws1",
        scope="user",
        scope_id="u1",
        type="semantic",
        content="用户喜欢喝绿茶",
        importance=0.7,
        status="active",
        last_reinforced_at=utcnow(),
        valid_from=utcnow(),
        metadata_={"episode_id": "ep-1", "consolidation_run_id": "run-1"},
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_neo4j_upsert_is_idempotent_and_expandable(
    neo4j_index: Neo4jIndex, db_session: Session, memory: Memory
) -> None:
    entity = Entity(workspace_id="ws1", name="绿茶", type="drink")
    db_session.add(entity)
    db_session.flush()

    entities = [GraphEntity(id=entity.id, name=entity.name, type=entity.type, role="object")]
    links = [
        GraphLink(
            from_id=memory.id,
            to_id=memory.id,
            relation="RELATED_TO",
            weight=1.0,
            evidence={"source": "test"},
        )
    ]

    neo4j_index.upsert_memory(memory, entities, links)
    neo4j_index.upsert_memory(memory, entities, links)

    node = neo4j_index.get(memory.id)
    assert node is not None
    assert node["workspace_id"] == "ws1"
    assert node["status"] == "active"
    assert node["entity_ids"] == [entity.id]

    expanded = neo4j_index.expand([entity.id], hops=1, limit=10)
    assert memory.id in expanded

    # MERGE 保证重复投影不产生重复节点。
    assert neo4j_index.count_memory_nodes() == 1

    neo4j_index.delete(memory.id)
    assert neo4j_index.get(memory.id) is None


def test_neo4j_delete_is_idempotent(neo4j_index: Neo4jIndex) -> None:
    neo4j_index.delete("missing-memory")
    assert neo4j_index.get("missing-memory") is None


def test_neo4j_rebuild_is_workspace_scoped(
    neo4j_index: Neo4jIndex, db_session: Session
) -> None:
    first = Memory(
        workspace_id="ws1",
        scope="user",
        scope_id="u1",
        type="semantic",
        content="ws1 的记忆",
        last_reinforced_at=utcnow(),
        valid_from=utcnow(),
    )
    second = Memory(
        workspace_id="ws2",
        scope="user",
        scope_id="u2",
        type="semantic",
        content="ws2 的记忆",
        last_reinforced_at=utcnow(),
        valid_from=utcnow(),
    )
    db_session.add_all([first, second])
    db_session.flush()
    neo4j_index.upsert_memory(first)
    neo4j_index.upsert_memory(second)
    state = ProjectionState(
        memory_id=second.id, target="neo4j", version=second.version
    )
    db_session.add(state)
    db_session.flush()

    configure_runtime(
        Runtime(
            session_scope=lambda: nullcontext(db_session),
            embedding=FakeEmbedding(dim=8),
            qdrant=None,
            neo4j=neo4j_index,
            eager=True,
        )
    )
    try:
        run_rebuild("neo4j", workspace="ws1")
    finally:
        reset_runtime()

    assert neo4j_index.get(first.id) is not None
    # 另一个 workspace 的节点与 projection_state 保持不变。
    assert neo4j_index.get(second.id) is not None
    second_state = db_session.scalars(
        select(ProjectionState).where(
            ProjectionState.memory_id == second.id,
            ProjectionState.target == "neo4j",
        )
    ).one()
    assert second_state.version == second.version
    first_state = db_session.scalars(
        select(ProjectionState).where(
            ProjectionState.memory_id == first.id,
            ProjectionState.target == "neo4j",
        )
    ).one()
    assert first_state.version == first.version
