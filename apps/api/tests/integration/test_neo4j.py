"""Neo4j 图投影：MERGE 幂等、实体扩展与删除。"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.adapters.neo4j import GraphEntity, GraphLink, Neo4jIndex
from app.models import Entity, Memory
from app.models.base import utcnow

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
