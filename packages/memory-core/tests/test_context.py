from datetime import datetime, timezone

import pytest

from memory_core.context import (
    MAX_SOURCE_QUOTE_CHARS,
    ContextItem,
    assemble_context,
    estimate_tokens,
)
from memory_core.types import MemoryType, Scope

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def make_item(
    item_id: str,
    content: str,
    *,
    type: str = "semantic",
    importance: float = 0.5,
    confidence: float = 0.9,
    retention: float = 0.8,
    source_quote: str | None = None,
    historical: bool = False,
    score: float | None = None,
) -> ContextItem:
    return ContextItem(
        id=item_id,
        type=type,
        content=content,
        importance=importance,
        confidence=confidence,
        retention=retention,
        timestamp=NOW,
        scope=Scope.USER,
        source_quote=source_quote,
        historical=historical,
        score=score,
    )


def test_estimate_tokens_rounds_up_by_two_characters():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a") == 1
    assert estimate_tokens("ab") == 1
    assert estimate_tokens("abc") == 2


def test_context_truncates_to_budget():
    items = [
        ContextItem(str(i), "semantic", "字" * 400, 0.5, 0.9, 0.8, NOW, Scope.USER)
        for i in range(10)
    ]
    text, kept = assemble_context(items, budget_tokens=100)
    assert len(kept) < 10
    assert estimate_tokens(text) <= 100


def test_context_format_contains_type_time_confidence():
    item = ContextItem(
        "m1", "semantic", "用户对花生过敏", 0.9, 0.95, 0.8, NOW, Scope.USER, "我吃花生会过敏"
    )
    text, _ = assemble_context([item])
    assert text.startswith("[语义|2026-10-27|0.95|")
    assert "我吃花生会过敏" in text


def test_missing_source_uses_unknown_marker():
    text, _ = assemble_context([make_item("m1", "用户偏好 Python")])
    assert text.startswith("[语义|2026-10-27|0.90|未知] ")


def test_source_quote_is_truncated_to_200_characters():
    quote = "原" * (MAX_SOURCE_QUOTE_CHARS + 50)
    text, _ = assemble_context([make_item("m1", "用户对花生过敏", source_quote=quote)])
    assert "原" * MAX_SOURCE_QUOTE_CHARS in text
    assert "原" * (MAX_SOURCE_QUOTE_CHARS + 1) not in text


def test_near_duplicate_items_are_collapsed():
    a = ContextItem("a", "semantic", "用户对花生过敏", 0.9, 0.9, 0.8, NOW, Scope.USER)
    b = ContextItem("b", "semantic", "用户对花生严重过敏", 0.7, 0.8, 0.8, NOW, Scope.USER)
    _, kept = assemble_context([a, b])
    assert len(kept) == 1
    assert kept[0].id == "a"


def test_contained_content_is_treated_as_duplicate():
    long_item = make_item("long", "用户对花生过敏", importance=0.8)
    short_item = make_item("short", "花生过敏", importance=0.4)
    _, kept = assemble_context([long_item, short_item])
    assert [item.id for item in kept] == ["long"]


def test_duplicate_keeps_higher_score_regardless_of_input_order():
    low = make_item("low", "用户对花生过敏", importance=0.2)
    high = make_item("high", "用户对花生严重过敏", importance=0.9)
    _, kept = assemble_context([low, high])
    assert [item.id for item in kept] == ["high"]


def test_distinct_items_are_both_kept():
    first = make_item("a", "用户喜欢猫")
    second = make_item("b", "用户养了一条狗")
    _, kept = assemble_context([first, second])
    assert {item.id for item in kept} == {"a", "b"}


def test_groups_follow_semantic_procedural_episodic_order():
    episodic = make_item("e", "昨天部署失败", type=MemoryType.EPISODIC, importance=0.99)
    procedural = make_item("p", "先用 uv sync 再跑测试", type=MemoryType.PROCEDURAL, importance=0.9)
    semantic = make_item("s", "用户对花生过敏", type=MemoryType.SEMANTIC, importance=0.1)
    _, kept = assemble_context([episodic, procedural, semantic])
    assert [item.id for item in kept] == ["s", "p", "e"]


def test_historical_items_are_placed_last_regardless_of_score():
    archived = ContextItem(
        "x",
        "semantic",
        "旧版本部署流程",
        1.0,
        0.9,
        0.8,
        NOW,
        Scope.USER,
        historical=True,
        score=10.0,
    )
    episodic = make_item("e", "昨天部署失败", type="episodic", importance=0.1, score=0.0)
    _, kept = assemble_context([archived, episodic])
    assert [item.id for item in kept] == ["e", "x"]
    text, _ = assemble_context([archived])
    assert text.startswith("[语义|")


def test_items_in_same_group_are_sorted_by_score_descending():
    low = make_item("low", "用户喜欢摇滚", importance=0.2)
    high = make_item("high", "用户喜欢爵士", importance=0.9)
    _, kept = assemble_context([low, high])
    assert [item.id for item in kept] == ["high", "low"]


def test_explicit_score_overrides_proxy_priority():
    high_score = make_item(
        "high", "用户喜欢爵士", importance=0.1, confidence=0.1, retention=0.1, score=0.9
    )
    low_score = make_item(
        "low", "用户喜欢摇滚", importance=0.99, confidence=0.99, retention=0.99, score=0.1
    )
    _, kept = assemble_context([low_score, high_score])
    assert [item.id for item in kept] == ["high", "low"]


def test_duplicate_keeps_higher_explicit_score():
    proxy_high = make_item(
        "a", "用户对花生过敏", importance=0.99, confidence=0.99, retention=0.99, score=0.1
    )
    score_high = make_item(
        "b", "用户对花生严重过敏", importance=0.1, confidence=0.1, retention=0.1, score=0.9
    )
    _, kept = assemble_context([proxy_high, score_high])
    assert [item.id for item in kept] == ["b"]


def test_budget_keeps_priority_prefix_and_drops_only_tail():
    items = [
        make_item("high", "高" * 60, score=0.9),
        make_item("mid", "中" * 60, score=0.5),
        make_item("low", "低" * 60, score=0.1),
    ]
    text, kept = assemble_context(items, budget_tokens=100)
    assert kept
    assert [item.id for item in kept] == ["high", "mid"]
    assert "高" * 60 in text
    assert "中" * 60 in text
    assert "低" * 60 not in text
    assert estimate_tokens(text) <= 100


def test_oversized_item_is_skipped_so_smaller_items_still_fit():
    big = make_item("big", "大" * 400, importance=0.9)
    small = make_item("small", "小", importance=0.1)
    text, kept = assemble_context([big, small], budget_tokens=40)
    assert [item.id for item in kept] == ["small"]
    assert estimate_tokens(text) <= 40


def test_zero_budget_returns_empty_context():
    text, kept = assemble_context([make_item("m1", "用户对花生过敏")], budget_tokens=0)
    assert text == ""
    assert kept == []


def test_negative_budget_is_rejected():
    with pytest.raises(ValueError):
        assemble_context([], budget_tokens=-1)


def test_empty_items_return_empty_context():
    assert assemble_context([]) == ("", [])


def test_negation_and_single_char_location_changes_are_not_collapsed():
    items = [
        make_item("allergy", "用户对花生过敏", importance=0.9),
        make_item("not-allergy", "用户对花生不过敏", importance=0.8),
        make_item("beijing", "用户住在北京", importance=0.7),
        make_item("nanjing", "用户住在南京", importance=0.6),
    ]
    _, kept = assemble_context(items)
    assert {item.id for item in kept} == {
        "allergy",
        "not-allergy",
        "beijing",
        "nanjing",
    }
