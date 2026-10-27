import dataclasses

import pytest

from memory_core.importance import (
    ImportanceSignals,
    effective_llm_score,
    score_importance,
)
from memory_core.types import MemoryType


def test_working_memory_is_capped_at_half():
    assert score_importance("临时信息", MemoryType.WORKING, ImportanceSignals(), 0.9) == 0.5


def test_explicit_remember_has_floor_090():
    assert score_importance("记住我不吃香菜", MemoryType.SEMANTIC, ImportanceSignals(explicit_remember=True), 0.2) == 0.9


def test_helpful_feedback_increases_but_clamps():
    assert score_importance("x", MemoryType.SEMANTIC, ImportanceSignals(helpful_feedback=20), 0.99) == 1.0


def test_missing_llm_score_falls_back_to_neutral():
    assert score_importance("普通内容", MemoryType.SEMANTIC, ImportanceSignals(), None) == 0.5


def test_pinned_memory_scores_one():
    assert score_importance("置顶内容", MemoryType.SEMANTIC, ImportanceSignals(pinned=True), 0.3) == 1.0


def test_type_cap_is_applied_after_signals():
    signals = ImportanceSignals(explicit_remember=True, pinned=True, helpful_feedback=5)
    assert score_importance("置顶的临时信息", MemoryType.WORKING, signals, 0.95) == 0.5


def test_each_helpful_feedback_adds_five_points():
    assert score_importance(
        "x", MemoryType.EPISODIC, ImportanceSignals(helpful_feedback=3), 0.5
    ) == pytest.approx(0.65)


def test_score_is_clamped_to_unit_interval():
    assert score_importance("x", MemoryType.SEMANTIC, ImportanceSignals(), -0.4) == 0.0
    assert score_importance("x", MemoryType.SEMANTIC, ImportanceSignals(), 1.4) == 1.0


def test_effective_llm_score_uses_neutral_baseline():
    assert effective_llm_score(None, ImportanceSignals()) == 0.5


def test_effective_llm_score_applies_signals_without_type_cap():
    assert effective_llm_score(0.2, ImportanceSignals(explicit_remember=True)) == 0.9
    assert effective_llm_score(0.2, ImportanceSignals(pinned=True)) == 1.0
    assert effective_llm_score(0.7, ImportanceSignals(helpful_feedback=2)) == pytest.approx(0.8)


def test_signals_are_frozen_with_defaults():
    signals = ImportanceSignals()
    assert (signals.explicit_remember, signals.pinned, signals.helpful_feedback) == (
        False,
        False,
        0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        signals.pinned = True
