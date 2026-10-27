"""Qdrant 向量索引适配器。

Collection 按 ``mem_{provider}_{model}_{dim}`` 隔离；point id 直接复用记忆
的 UUID，payload 字段全部建索引，便于按 workspace/scope/status 过滤。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from qdrant_client import QdrantClient, models

logger = logging.getLogger(__name__)

_COLLECTION_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")

#: payload 索引：规格 §10.2 要求全部建索引。
_PAYLOAD_INDEXES: tuple[tuple[str, models.PayloadSchemaType], ...] = (
    ("workspace_id", models.PayloadSchemaType.KEYWORD),
    ("scope", models.PayloadSchemaType.KEYWORD),
    ("scope_id", models.PayloadSchemaType.KEYWORD),
    ("type", models.PayloadSchemaType.KEYWORD),
    ("status", models.PayloadSchemaType.KEYWORD),
    ("importance", models.PayloadSchemaType.FLOAT),
    ("valid_to", models.PayloadSchemaType.DATETIME),
    ("last_reinforced_at", models.PayloadSchemaType.DATETIME),
    ("entity_ids", models.PayloadSchemaType.KEYWORD),
)


def collection_name(provider: str, model: str, dim: int) -> str:
    """``mem_{provider}_{model}_{dim}``；非法字符替换为下划线。"""

    def clean(value: str) -> str:
        return _COLLECTION_UNSAFE.sub("_", value.strip()) or "unknown"

    return f"mem_{clean(provider)}_{clean(model)}_{dim}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class IndexHit:
    """搜索结果；保持与 Qdrant 解耦的最小字段集。"""

    id: str
    payload: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


class QdrantIndex:
    """同步 Qdrant 客户端封装（Celery worker 为同步任务）。"""

    def __init__(
        self,
        url: str,
        *,
        provider: str,
        model: str,
        dim: int,
        api_key: str | None = None,
        client: QdrantClient | None = None,
    ) -> None:
        self.url = url
        self.provider = provider
        self.model = model
        self.dim = dim
        self.collection = collection_name(provider, model, dim)
        self._client = client or QdrantClient(url=url, api_key=api_key, timeout=30)
        self._ensured = False

    @classmethod
    def from_settings(cls, settings: Any) -> QdrantIndex:
        return cls(
            settings.qdrant_url,
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
        )

    @property
    def client(self) -> QdrantClient:
        return self._client

    def close(self) -> None:
        self._client.close()

    def exists(self) -> bool:
        return bool(self._client.collection_exists(self.collection))

    def delete_collection(self) -> None:
        if self.exists():
            self._client.delete_collection(self.collection)
        self._ensured = False

    def ensure_collection(self) -> None:
        if self._ensured and self.exists():
            return
        if not self.exists():
            self._client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self.dim,
                    distance=models.Distance.COSINE,
                    hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
                ),
            )
            logger.info(
                "qdrant collection created",
                extra={"collection": self.collection, "dim": self.dim},
            )
        for field_name, schema in _PAYLOAD_INDEXES:
            try:
                self._client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field_name,
                    field_schema=schema,
                )
            except Exception:  # 已存在的索引或旧版本不支持 DATETIME 时不阻塞投影
                logger.warning(
                    "qdrant payload index creation failed",
                    extra={"collection": self.collection, "field": field_name},
                )
        self._ensured = True

    def recreate_collection(self) -> None:
        self.delete_collection()
        self.ensure_collection()

    def upsert(
        self, memory_id: str, vector: list[float], payload: dict[str, Any]
    ) -> None:
        if len(vector) != self.dim:
            raise ValueError(
                f"向量维度不匹配: collection={self.dim} actual={len(vector)}"
            )
        self.ensure_collection()
        self._client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=memory_id,
                    vector=[float(value) for value in vector],
                    payload=_jsonable(payload),
                )
            ],
            wait=True,
        )

    def _build_filter(
        self, filters: dict[str, Any] | None
    ) -> models.Filter | None:
        conditions: list[models.FieldCondition] = []
        for key, value in (filters or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set, frozenset)):
                values = [_jsonable(item) for item in value]
                if not values:
                    continue
                conditions.append(
                    models.FieldCondition(key=key, match=models.MatchAny(any=values))
                )
            else:
                conditions.append(
                    models.FieldCondition(
                        key=key, match=models.MatchValue(value=_jsonable(value))
                    )
                )
        return models.Filter(must=conditions) if conditions else None

    def search(
        self,
        vector: list[float],
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[IndexHit]:
        self.ensure_collection()
        response = self._client.query_points(
            collection_name=self.collection,
            query=[float(value) for value in vector],
            query_filter=self._build_filter(filters),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [
            IndexHit(
                id=str(point.id),
                payload=dict(point.payload or {}),
                score=float(point.score or 0.0),
            )
            for point in response.points
        ]

    def get(self, memory_id: str) -> dict[str, Any] | None:
        if not self.exists():
            return None
        points = self._client.retrieve(
            collection_name=self.collection,
            ids=[memory_id],
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return None
        return dict(points[0].payload or {})

    def count(self, workspace_id: str) -> int:
        if not self.exists():
            return 0
        result = self._client.count(
            collection_name=self.collection,
            count_filter=self._build_filter({"workspace_id": workspace_id}),
            exact=True,
        )
        return int(result.count)

    def delete(self, memory_id: str) -> None:
        if not self.exists():
            return
        self._client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=[memory_id]),
            wait=True,
        )

    def delete_many(self, memory_ids: list[str]) -> None:
        """批量删除 point；空列表直接返回。"""
        if not memory_ids or not self.exists():
            return
        self._client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=list(memory_ids)),
            wait=True,
        )

    @staticmethod
    def _require_workspace(workspace_id: str) -> str:
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise ValueError("workspace_id 不能为空或空白")
        return workspace_id.strip()

    def delete_by_workspace(self, workspace_id: str) -> None:
        """删除某 workspace 的全部 point；空/空白 workspace 必须拒绝。"""
        normalized = self._require_workspace(workspace_id)
        if not self.exists():
            return
        self._client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=self._build_filter({"workspace_id": normalized})
            ),
            wait=True,
        )

    def list_ids(self, workspace_id: str | None = None) -> list[str]:
        """列出 collection 中的 point id，可按 workspace 收窄。

        只用于重建后的孤儿清理；M1 规模下一次性收集 id 足够，后续可分页化。
        空字符串不等同于“全部”，会直接抛错，避免误删所有 workspace 数据。
        """
        if workspace_id is not None:
            workspace_id = self._require_workspace(workspace_id)
        if not self.exists():
            return []
        scroll_filter = self._build_filter(
            {"workspace_id": workspace_id} if workspace_id else None
        )
        ids: list[str] = []
        offset: Any | None = None
        while True:
            points, offset = self._client.scroll(
                collection_name=self.collection,
                scroll_filter=scroll_filter,
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.extend(str(point.id) for point in points)
            if offset is None:
                break
        return ids


__all__ = ["IndexHit", "QdrantIndex", "collection_name"]
