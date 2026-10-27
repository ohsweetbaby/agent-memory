"""``memory_embedding`` 读写：Postgres 是嵌入的事实源，Qdrant 可重建。"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import MemoryEmbedding


class EmbeddingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        memory_id: str,
        model: str,
        dim: int,
        vector: list[float],
    ) -> MemoryEmbedding:
        """按 ``(memory_id, model, dim)`` 幂等写入；重复事件覆盖同一行。"""
        if len(vector) != dim:
            raise ValueError(
                f"嵌入维度不匹配: 声明 {dim}，实际 {len(vector)}"
            )
        statement = (
            pg_insert(MemoryEmbedding)
            .values(memory_id=memory_id, model=model, dim=dim, vector=vector)
            .on_conflict_do_update(
                index_elements=[
                    MemoryEmbedding.memory_id,
                    MemoryEmbedding.model,
                    MemoryEmbedding.dim,
                ],
                set_={"vector": vector, "updated_at": func.now()},
            )
            .returning(MemoryEmbedding.id)
        )
        row_id = self.session.scalar(statement)
        self.session.flush()
        return self.session.get(MemoryEmbedding, row_id)

    def get(self, memory_id: str, model: str) -> list[float] | None:
        """读取指定模型最新的嵌入；模型切换后旧向量自然失效。"""
        statement = (
            select(MemoryEmbedding.vector)
            .where(
                MemoryEmbedding.memory_id == memory_id,
                MemoryEmbedding.model == model,
            )
            .order_by(MemoryEmbedding.updated_at.desc())
            .limit(1)
        )
        vector = self.session.scalar(statement)
        return list(vector) if vector is not None else None

    def delete(self, memory_id: str) -> None:
        self.session.execute(
            delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory_id)
        )


__all__ = ["EmbeddingRepository"]
