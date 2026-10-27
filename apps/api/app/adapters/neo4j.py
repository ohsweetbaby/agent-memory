"""Neo4j 图投影适配器。

只承担 1–2 跳邻域扩展与可视化所需的最小图：节点 ``Memory/Entity/Episode``，
关系 ``RELATED_TO/MENTIONS/SUPERSEDES/DERIVED_FROM``。所有写入使用
``MERGE``，重复投影不会产生重复节点或边。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from neo4j import GraphDatabase
from neo4j.exceptions import DriverError, ServiceUnavailable

logger = logging.getLogger(__name__)

_SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT memory_id_unique IF NOT EXISTS "
    "FOR (m:Memory) REQUIRE m.id IS UNIQUE",
    "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT episode_id_unique IF NOT EXISTS "
    "FOR (e:Episode) REQUIRE e.id IS UNIQUE",
    "CREATE CONSTRAINT entity_workspace_name_unique IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE (e.workspace_id, e.name) IS UNIQUE",
)


@dataclass(frozen=True, slots=True)
class GraphEntity:
    id: str
    name: str
    type: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class GraphLink:
    from_id: str
    to_id: str
    relation: str = "RELATED_TO"
    weight: float = 1.0
    evidence: dict[str, Any] | None = None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class Neo4jIndex:
    """同步 Neo4j driver 封装。"""

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        database: str = "neo4j",
    ) -> None:
        self.uri = uri
        self.database = database
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._schema_ready = False

    @classmethod
    def from_settings(cls, settings: Any) -> Neo4jIndex:
        return cls(
            settings.neo4j_uri,
            settings.neo4j_user,
            settings.neo4j_password,
        )

    def close(self) -> None:
        self._driver.close()

    def wait_ready(self, timeout: float = 180.0, interval: float = 1.0) -> None:
        """社区版容器启动较慢；显式轮询到 Bolt 可连接。"""
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._driver.verify_connectivity()
                return
            except (ServiceUnavailable, DriverError) as exc:
                last_error = exc
                time.sleep(interval)
        raise RuntimeError(f"Neo4j 未在 {timeout:.0f}s 内就绪: {self.uri}") from last_error

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._driver.session(database=self.database) as session:
            for statement in _SCHEMA_STATEMENTS:
                session.run(statement).consume()
        self._schema_ready = True

    def upsert_memory(
        self,
        memory: Any,
        entities: Sequence[GraphEntity] = (),
        links: Sequence[GraphLink] = (),
    ) -> None:
        """幂等写入一条记忆及其实体、关联边。"""
        self._ensure_schema()
        props = {
            "id": memory.id,
            "workspace_id": memory.workspace_id,
            "type": memory.type,
            "scope": memory.scope,
            "scope_id": memory.scope_id,
            "status": memory.status,
            "importance": float(memory.importance or 0.0),
            "valid_to": _iso(memory.valid_to),
            "last_reinforced_at": _iso(memory.last_reinforced_at),
        }
        metadata = getattr(memory, "metadata_", None) or {}

        with self._driver.session(database=self.database) as session:
            session.run(
                "MERGE (m:Memory {id: $props.id}) SET m += $props",
                props=props,
            ).consume()

            if entities:
                session.run(
                    """
                    UNWIND $entities AS entity
                    MERGE (e:Entity {id: entity.id})
                    SET e.name = entity.name,
                        e.workspace_id = $workspace_id
                    SET e.type = coalesce(entity.type, e.type)
                    WITH e, entity
                    MATCH (m:Memory {id: $memory_id})
                    MERGE (m)-[r:MENTIONS]->(e)
                    SET r.role = entity.role
                    """,
                    entities=[
                        {
                            "id": entity.id,
                            "name": entity.name,
                            "type": entity.type,
                            "role": entity.role,
                        }
                        for entity in entities
                    ],
                    workspace_id=memory.workspace_id,
                    memory_id=memory.id,
                ).consume()

            if links:
                session.run(
                    """
                    UNWIND $links AS link
                    MERGE (a:Memory {id: link.from_id})
                    MERGE (b:Memory {id: link.to_id})
                    MERGE (a)-[r:RELATED_TO]->(b)
                    SET r.relation = link.relation,
                        r.weight = link.weight,
                        r.evidence = link.evidence
                    """,
                    links=[
                        {
                            "from_id": link.from_id,
                            "to_id": link.to_id,
                            "relation": link.relation,
                            "weight": float(link.weight),
                            # Neo4j 属性只能是原始类型；证据 JSON 序列化为字符串。
                            "evidence": (
                                json.dumps(link.evidence, ensure_ascii=False)
                                if link.evidence is not None
                                else None
                            ),
                        }
                        for link in links
                    ],
                ).consume()

            if memory.superseded_by:
                session.run(
                    """
                    MATCH (old:Memory {id: $memory_id})
                    MERGE (new:Memory {id: $new_id})
                    MERGE (new)-[r:SUPERSEDES]->(old)
                    SET r.at = $at
                    """,
                    memory_id=memory.id,
                    new_id=memory.superseded_by,
                    at=_iso(memory.valid_to),
                ).consume()

            episode_id = metadata.get("episode_id")
            if episode_id:
                session.run(
                    """
                    MATCH (m:Memory {id: $memory_id})
                    MERGE (ep:Episode {id: $episode_id})
                    SET ep.session_id = coalesce($session_id, ep.session_id)
                    MERGE (m)-[r:DERIVED_FROM]->(ep)
                    SET r.run_id = $run_id
                    """,
                    memory_id=memory.id,
                    episode_id=str(episode_id),
                    session_id=metadata.get("session_id"),
                    run_id=metadata.get("consolidation_run_id"),
                ).consume()

        logger.info(
            "memory projected to neo4j",
            extra={
                "memory_id": memory.id,
                "entity_count": len(entities),
                "link_count": len(links),
            },
        )

    def get(self, memory_id: str) -> dict[str, Any] | None:
        query = """
        MATCH (m:Memory {id: $memory_id})
        OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)
        RETURN m {.*} AS memory, collect(DISTINCT e.id) AS entity_ids
        """
        with self._driver.session(database=self.database) as session:
            record = session.run(query, memory_id=memory_id).single()
        if record is None:
            return None
        node = dict(record["memory"])
        node["entity_ids"] = [value for value in record["entity_ids"] if value]
        return node

    def delete(self, memory_id: str) -> None:
        with self._driver.session(database=self.database) as session:
            session.run(
                "MATCH (m:Memory {id: $memory_id}) DETACH DELETE m",
                memory_id=memory_id,
            ).consume()

    def expand(
        self, entity_ids: Sequence[str], hops: int = 2, limit: int = 30
    ) -> list[str]:
        """从实体出发的 1–2 跳邻域扩展，返回记忆 id。"""
        if not entity_ids:
            return []
        bounded_hops = max(1, min(int(hops), 3))
        query = f"""
        MATCH (e:Entity) WHERE e.id IN $entity_ids
        MATCH path=(e)-[*1..{bounded_hops}]-(m:Memory)
        RETURN DISTINCT m.id AS id, min(length(path)) AS distance
        ORDER BY distance, id
        LIMIT $limit
        """
        with self._driver.session(database=self.database) as session:
            result = session.run(query, entity_ids=list(entity_ids), limit=limit)
            return [record["id"] for record in result]

    def count_memory_nodes(self) -> int:
        with self._driver.session(database=self.database) as session:
            record = session.run("MATCH (m:Memory) RETURN count(m) AS total").single()
        return int(record["total"]) if record else 0

    def clear(self) -> None:
        """重建索引前清空 M1 图投影；不触碰其他标签。"""
        with self._driver.session(database=self.database) as session:
            session.run(
                "MATCH (n) WHERE n:Memory OR n:Entity OR n:Episode "
                "DETACH DELETE n"
            ).consume()


__all__ = ["GraphEntity", "GraphLink", "Neo4jIndex"]
