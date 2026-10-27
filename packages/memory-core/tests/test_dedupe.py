from datetime import datetime, timezone

from memory_core.dedupe import (
    ConflictDecision,
    ConflictKind,
    ExistingCandidate,
    decide_conflict,
)

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def test_near_duplicate_merges():
    c = ExistingCandidate("m1", "用户住在北京", 0.95, ("用户", "居住"))
    assert decide_conflict("用户住在北京市", [c], contradiction=None).kind is ConflictKind.MERGE


def test_explicit_contradiction_supersedes_old():
    c = ExistingCandidate("m1", "用户住在北京", 0.85, ("用户", "居住"))
    decision = decide_conflict("用户搬到上海", [c], contradiction=True)
    assert decision.kind is ConflictKind.SUPERSEDE and decision.target_id == "m1"


def test_temporal_change_coexists():
    c = ExistingCandidate("m1", "用户住在北京", 0.85, ("用户", "居住"))
    assert (
        decide_conflict("用户搬到上海", [c], contradiction=True, temporal_change=True).kind
        is ConflictKind.COEXIST
    )


def test_unrelated_content_creates():
    c = ExistingCandidate("m1", "用户住在北京", 0.4, ("用户", "居住"))
    assert decide_conflict("用户喜欢摇滚", [c], contradiction=None).kind is ConflictKind.CREATE


def test_high_similarity_without_contradiction_still_merges():
    c = ExistingCandidate("m1", "用户偏好 Python", 0.99, ("用户", "偏好"))
    assert (
        decide_conflict("用户偏爱 Python", [c], contradiction=False).kind is ConflictKind.MERGE
    )


def test_empty_candidates_creates():
    decision = decide_conflict("用户喜欢摇滚", [], contradiction=None)
    assert decision == ConflictDecision(ConflictKind.CREATE, None, decision.reason)
    assert decision.target_id is None
    assert decision.reason


def test_contradiction_below_conflict_threshold_creates():
    c = ExistingCandidate("m1", "用户住在北京", 0.6, ("用户", "居住"))
    decision = decide_conflict("用户喜欢摇滚", [c], contradiction=True)
    assert decision.kind is ConflictKind.CREATE
    assert decision.target_id is None


def test_supersede_picks_most_similar_candidate():
    candidates = [
        ExistingCandidate("m1", "用户住在北京", 0.80, ("用户", "居住")),
        ExistingCandidate("m2", "用户住在北京市", 0.90, ("用户", "居住")),
        ExistingCandidate("m3", "用户住在杭州", 0.76, ("用户", "居住")),
    ]
    decision = decide_conflict("用户搬到上海", candidates, contradiction=True)
    assert decision.kind is ConflictKind.SUPERSEDE
    assert decision.target_id == "m2"


def test_coexist_targets_most_similar_candidate():
    candidates = [
        ExistingCandidate("m1", "用户住在北京", 0.80, ("用户", "居住")),
        ExistingCandidate("m2", "用户住在北京市", 0.88, ("用户", "居住")),
    ]
    decision = decide_conflict(
        "用户 2026 年搬到上海", candidates, contradiction=True, temporal_change=True
    )
    assert decision.kind is ConflictKind.COEXIST
    assert decision.target_id == "m2"


def test_merge_threshold_boundary_is_inclusive():
    c = ExistingCandidate("m1", "用户住在北京", 0.92, ("用户", "居住"))
    assert decide_conflict("用户住在北京市", [c], contradiction=None).kind is ConflictKind.MERGE


def test_conflict_threshold_boundary_is_inclusive():
    c = ExistingCandidate("m1", "用户住在北京", 0.75, ("用户", "居住"))
    decision = decide_conflict("用户搬到上海", [c], contradiction=True)
    assert decision.kind is ConflictKind.SUPERSEDE


def test_future_valid_to_candidate_is_not_excluded():
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    c = ExistingCandidate("m1", "用户住在北京", 0.99, ("用户", "居住"), valid_to=future)
    decision = decide_conflict("用户住在北京市", [c], contradiction=None)
    assert decision.kind is ConflictKind.MERGE
    assert decision.target_id == "m1"


def test_valid_to_filtering_is_callers_responsibility():
    c = ExistingCandidate("m1", "用户住在北京", 0.99, ("用户", "居住"), valid_to=NOW)
    decision = decide_conflict("用户住在北京市", [c], contradiction=None)
    assert decision.kind is ConflictKind.MERGE


def test_different_subject_predicate_does_not_merge():
    c = ExistingCandidate("m1", "用户喜欢猫", 0.95, ("用户", "喜欢"))
    decision = decide_conflict(
        "用户住在北京市",
        [c],
        contradiction=None,
        new_subject_predicate=("用户", "居住"),
    )
    assert decision.kind is ConflictKind.CREATE
    assert decision.target_id is None


def test_mixed_candidates_prefer_matching_subject_predicate():
    candidates = [
        ExistingCandidate("m1", "用户住在北京", 0.95, ("用户", "居住")),
        ExistingCandidate("m2", "用户喜欢住在北京", 0.80, ("用户", "喜欢")),
    ]
    decision = decide_conflict(
        "用户喜欢上海",
        candidates,
        contradiction=True,
        new_subject_predicate=("用户", "喜欢"),
    )
    assert decision.kind is ConflictKind.SUPERSEDE
    assert decision.target_id == "m2"


def test_merge_prefers_matching_subject_predicate():
    candidates = [
        ExistingCandidate("m1", "用户住在北京", 0.97, ("用户", "居住")),
        ExistingCandidate("m2", "用户喜欢猫咪", 0.93, ("用户", "喜欢")),
    ]
    decision = decide_conflict(
        "用户喜欢猫科动物",
        candidates,
        contradiction=None,
        new_subject_predicate=("用户", "喜欢"),
    )
    assert decision.kind is ConflictKind.MERGE
    assert decision.target_id == "m2"


def test_missing_new_subject_predicate_keeps_legacy_behavior():
    c = ExistingCandidate("m1", "用户住在北京", 0.95, ("用户", "居住"))
    decision = decide_conflict("用户住在北京市", [c], contradiction=None)
    assert decision.kind is ConflictKind.MERGE
    assert decision.target_id == "m1"


def test_candidate_without_subject_predicate_is_not_filtered():
    c = ExistingCandidate("m1", "用户住在北京", 0.95)
    decision = decide_conflict(
        "用户住在北京市",
        [c],
        contradiction=None,
        new_subject_predicate=("用户", "居住"),
    )
    assert decision.kind is ConflictKind.MERGE


def test_every_decision_has_audit_reason():
    cases = [
        decide_conflict("x", [], contradiction=None),
        decide_conflict("x", [ExistingCandidate("m1", "y", 0.99)], contradiction=None),
        decide_conflict("x", [ExistingCandidate("m1", "y", 0.85)], contradiction=True),
        decide_conflict(
            "x", [ExistingCandidate("m1", "y", 0.85)], contradiction=True, temporal_change=True
        ),
        decide_conflict("x", [ExistingCandidate("m1", "y", 0.2)], contradiction=None),
    ]
    assert {case.kind for case in cases} == set(ConflictKind)
    assert all(case.reason.strip() for case in cases)
