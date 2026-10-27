"""Postgres 全文检索适配器（中文友好）。

迁移只为 ``search_tsv`` 选择了 ``simple`` 分词：整段中文会被当成一个
lexeme，因此仅靠 ``to_tsvector`` 无法让“花生过敏”命中“用户对花生过敏”。
本适配器在应用层做两件事：

1. 用 jieba 把查询切成词，拼成 OR 语义的 ``to_tsquery``，覆盖英文与已分词
   文档；
2. 同时用 ``pg_trgm`` 相似度与 ``ILIKE`` 兜底，保证连续中文子串可召回。

两路分数取 ``greatest`` 排序，调用方看到的仍是单通道的稳定名次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

import jieba
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.models import Memory

#: 每路召回默认条数。
SEARCH_TOP_K = 50

#: pg_trgm 兜底阈值；低于该值的词面噪声不进入候选。
TRGM_FALLBACK_THRESHOLD = 0.15

#: 单条查询最多参与 tsquery 的词数，避免超长 OR 表达式。
MAX_QUERY_TOKENS = 24

_TOKEN_UNSAFE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """用 jieba 切词并去重；空白与标点不参与检索。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in jieba.lcut(text or ""):
        token = _TOKEN_UNSAFE.sub("", raw).strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= MAX_QUERY_TOKENS:
            break
    return tokens


def build_tsquery(text: str, *, conjunction: bool = False) -> str | None:
    """把一段文本拼成合法的 ``to_tsquery`` 表达式。

    默认 OR 语义（任一词命中即可），``conjunction=True`` 时改为 AND，供
    ``exact`` 模式使用。没有可用词时返回 ``None``。
    """
    tokens = tokenize(text)
    if not tokens:
        return None
    operator = " & " if conjunction else " | "
    return operator.join(tokens)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True, slots=True)
class KeywordHit:
    """关键词通道命中；``score`` 是 ts_rank 与 trigram 的较大值。"""

    memory: Memory
    score: float


class PostgresFTS:
    """在给定 SQL 条件上执行全文检索。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def search(
        self,
        *,
        conditions: Sequence[Any],
        query: str,
        limit: int = SEARCH_TOP_K,
        exact: bool = False,
    ) -> list[KeywordHit]:
        if limit <= 0 or not query.strip():
            return []

        tsquery_text = build_tsquery(query, conjunction=exact)
        similarity = func.similarity(Memory.content, query)
        rank_parts: list[Any] = [similarity]
        matches: list[Any] = []

        if tsquery_text is not None:
            tsquery = func.to_tsquery("simple", tsquery_text)
            matches.append(Memory.search_tsv.op("@@")(tsquery))
            rank_parts.append(func.ts_rank_cd(Memory.search_tsv, tsquery))

        escaped = _escape_like(query.strip())
        matches.append(Memory.content.ilike(f"%{escaped}%", escape="\\"))
        if not exact:
            matches.append(similarity >= TRGM_FALLBACK_THRESHOLD)

        rank = func.greatest(*rank_parts).label("keyword_rank")
        statement: Select[Any] = (
            select(Memory, rank)
            .where(*conditions, or_(*matches))
            .order_by(rank.desc(), Memory.importance.desc(), Memory.id)
            .limit(limit)
        )
        rows = self.session.execute(statement).all()
        return [KeywordHit(memory=row[0], score=float(row[1] or 0.0)) for row in rows]


__all__ = [
    "KeywordHit",
    "MAX_QUERY_TOKENS",
    "PostgresFTS",
    "SEARCH_TOP_K",
    "TRGM_FALLBACK_THRESHOLD",
    "build_tsquery",
    "tokenize",
]
