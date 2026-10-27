import pytest

from memory_core.fusion import SCOPE_WEIGHTS, final_score, rrf
from memory_core.types import Scope


def test_rrf_rewards_agreement():
    scores = rrf({"vector": ["a", "b"], "bm25": ["b", "a"]})
    assert scores["a"] == scores["b"]
    assert scores["a"] > 0


def test_rrf_uses_reciprocal_rank_with_k():
    scores = rrf({"vector": ["a", "b"]}, k=60)
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)


def test_rrf_sums_contributions_from_all_channels():
    scores = rrf({"vector": ["a", "b"], "bm25": ["b"], "graph": []})
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert set(scores) == {"a", "b"}


def test_rrf_counts_duplicate_id_in_same_channel_once():
    scores = rrf({"vector": ["a", "a", "b"]})
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 63)


def test_rrf_empty_rankings_yield_no_scores():
    assert rrf({}) == {}
    assert rrf({"vector": []}) == {}


def test_rrf_rejects_negative_k():
    with pytest.raises(ValueError):
        rrf({"vector": ["a"]}, k=-1)


def test_scope_weight_orders_session_before_global():
    assert final_score(0.1, 0.5, Scope.SESSION) > final_score(0.1, 0.5, Scope.GLOBAL)


def test_scope_weights_are_most_specific_first():
    ordered = [Scope.SESSION, Scope.AGENT, Scope.USER, Scope.GLOBAL]
    scores = [final_score(1.0, 0.5, scope) for scope in ordered]
    assert scores == sorted(scores, reverse=True)
    assert SCOPE_WEIGHTS == {
        Scope.SESSION: 1.0,
        Scope.AGENT: 0.98,
        Scope.USER: 0.96,
        Scope.GLOBAL: 0.94,
    }


def test_final_score_multiplies_importance_and_scope_weight():
    assert final_score(0.1, 0.5, Scope.USER) == pytest.approx(0.1 * 0.9 * 0.96)


def test_final_score_accepts_scope_string():
    assert final_score(0.1, 0.5, "user") == pytest.approx(final_score(0.1, 0.5, Scope.USER))


def test_final_score_rejects_unknown_scope():
    with pytest.raises(ValueError):
        final_score(0.1, 0.5, "planetary")
