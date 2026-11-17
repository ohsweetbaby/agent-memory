"""检索纯函数：中文分词、tsquery 构造与程序性成功率加成。"""

from __future__ import annotations

from app.adapters.fts import build_tsquery, tokenize
from app.schemas.search import SearchRequest
from app.services.search import _build_filters, procedural_bonus


def test_procedural_bonus_defaults_to_half() -> None:
    assert procedural_bonus({}) == 0.5
    assert procedural_bonus({"skill": {}}) == 0.5


def test_procedural_bonus_uses_skill_counters() -> None:
    assert procedural_bonus({"skill": {"success_count": 3, "failure_count": 0}}) == 0.8
    # 顶层字段兼容；失败多于成功时显著降权。
    assert procedural_bonus({"success": 0, "failure": 3}) == 0.2


def test_tokenize_splits_contiguous_chinese() -> None:
    tokens = tokenize("花生过敏")
    assert "花生" in tokens
    assert "过敏" in tokens
    # 整段中文不再是唯一 token，否则 simple 配置无法召回。
    assert "花生过敏" not in tokens


def test_build_tsquery_uses_or_and_sanitizes_punctuation() -> None:
    assert build_tsquery("花生, 过敏") == "花生 | 过敏"
    assert build_tsquery("花生 过敏", conjunction=True) == "花生 & 过敏"
    assert build_tsquery("!!!") is None


def test_empty_scopes_mean_global_only_not_all_scopes() -> None:
    # 显式空列表必须按最小可见只返回 global，不能因 falsy 被当成"未指定=全部"。
    req = SearchRequest(
        query="过敏",
        scopes=[],
        agent_id="a1",
        end_user_id="u1",
        session_id="s1",
    )
    assert _build_filters(req, "ws1").scope_pairs == (("global", None),)


def test_omitted_scopes_include_context_scopes() -> None:
    req = SearchRequest(query="过敏", agent_id="a1", end_user_id="u1")
    assert _build_filters(req, "ws1").scope_pairs == (
        ("global", None),
        ("agent", "a1"),
        ("user", "u1"),
    )
