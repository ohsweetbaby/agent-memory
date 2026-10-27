"""Postgres 全文检索适配器（中文友好）。

迁移只为 ``search_tsv`` 选择了 ``simple`` 分词：整段中文会被当成一个
lexeme，因此仅靠 ``to_tsvector`` 无法让“花生过敏”命中“用户对花生过敏”。
本适配器在应用层做三件事：

1. 用 jieba 把查询切成词，拼成 OR 语义的 ``to_tsquery``，覆盖英文与已分词
   文档；
2. 对每个 token 单独做 ``ILIKE`` 匹配，解决 ``simple`` 配置下整段中文只是
   一个 lexeme、非连续查询无法命中 ``search_tsv`` 的问题；
3. 用 ``pg_trgm`` 相似度兜底（CJK 查询使用更宽松的阈值），保证词序变化与
   轻微改写仍可召回。

排序由“命中 token 覆盖率 + trigram 相似度 + ts_rank”组成，连续子串与
非连续关键词都能得到稳定名次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

import jieba
from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.models import Memory

#: 每路召回默认条数。
SEARCH_TOP_K = 50

#: 非 CJK 查询的 pg_trgm 兜底阈值；低于该值的词面噪声不进入候选。
TRGM_FALLBACK_THRESHOLD = 0.15

#: CJK 查询的兜底阈值：中文短查询的 trigram 覆盖率天然偏低。
CJK_TRGM_FALLBACK_THRESHOLD = 0.1

#: 单条查询最多参与 tsquery / token 匹配的词数，避免超长表达式。
MAX_QUERY_TOKENS = 24

_TOKEN_UNSAFE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_CJK = re.compile(r"[\u4e00-\u9fff]", re.UNICODE)


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


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

        tokens = tokenize(query)
        is_cjk_query = _has_cjk(query)
        # 每个 token 单独 ILIKE：simple 分词下整段中文只是一个 lexeme，
        # 非连续中文查询必须靠 token 级匹配才能召回。纯英文查询仍走
        # tsquery，避免单词子串匹配降低既有精确性。
        token_matches = (
            [
                Memory.content.ilike(
                    f"%{_escape_like(token)}%", escape="\\"
                )
                for token in tokens
            ]
            if is_cjk_query
            else []
        )

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
        if token_matches:
            if exact:
                # exact 模式要求所有 query token 都出现，避免退化成宽召回。
                matches.append(and_(*token_matches))
            else:
                matches.append(or_(*token_matches))

        threshold = (
            CJK_TRGM_FALLBACK_THRESHOLD
            if is_cjk_query
            else TRGM_FALLBACK_THRESHOLD
        )
        if not exact:
            matches.append(similarity >= threshold)

        # 命中 token 覆盖率：同等相似度下，命中更多查询词的结果优先。
        coverage: Any | None = None
        for condition in token_matches:
            term = case((condition, 1.0), else_=0.0)
            coverage = term if coverage is None else coverage + term
        if coverage is not None:
            coverage = coverage / float(len(token_matches))
            rank_parts.append(coverage)

        rank = rank_parts[0]
        for part in rank_parts[1:]:
            rank = rank + part
        rank = rank.label("keyword_rank")

        statement: Select[Any] = (
            select(Memory, rank)
            .where(*conditions, or_(*matches))
            .order_by(rank.desc(), Memory.importance.desc(), Memory.id)
            .limit(limit)
        )
        rows = self.session.execute(statement).all()
        return [KeywordHit(memory=row[0], score=float(row[1] or 0.0)) for row in rows]


__all__ = [
    "CJK_TRGM_FALLBACK_THRESHOLD",
    "KeywordHit",
    "MAX_QUERY_TOKENS",
    "PostgresFTS",
    "SEARCH_TOP_K",
    "TRGM_FALLBACK_THRESHOLD",
    "build_tsquery",
    "tokenize",
]
