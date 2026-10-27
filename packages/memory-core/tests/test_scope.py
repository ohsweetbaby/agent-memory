import dataclasses

import pytest

from memory_core import MemoryStatus, MemoryType, Scope, ScopeContext, visible_scopes


def test_visible_scopes_merges_global_with_context():
    ctx = ScopeContext("ws1", agent_id="a1", end_user_id="u1", session_id="s1")
    assert visible_scopes(ctx) == [
        (Scope.GLOBAL, None),
        (Scope.AGENT, "a1"),
        (Scope.USER, "u1"),
        (Scope.SESSION, "s1"),
    ]


def test_missing_identifiers_yield_only_global():
    assert visible_scopes(ScopeContext("ws1")) == [(Scope.GLOBAL, None)]


def test_requested_scopes_cannot_escape_context():
    ctx = ScopeContext("ws1", agent_id="a1")
    assert visible_scopes(ctx, [Scope.USER, Scope.SESSION]) == [(Scope.GLOBAL, None)]


def test_requested_scopes_filter_but_keep_canonical_order():
    ctx = ScopeContext("ws1", agent_id="a1", end_user_id="u1", session_id="s1")
    assert visible_scopes(ctx, [Scope.SESSION, Scope.GLOBAL]) == [
        (Scope.GLOBAL, None),
        (Scope.SESSION, "s1"),
    ]


def test_requested_scopes_are_deduplicated():
    ctx = ScopeContext("ws1", agent_id="a1")
    assert visible_scopes(ctx, [Scope.AGENT, Scope.AGENT]) == [
        (Scope.GLOBAL, None),
        (Scope.AGENT, "a1"),
    ]


def test_blank_identifiers_are_treated_as_missing():
    ctx = ScopeContext("ws1", agent_id="", end_user_id="u1", session_id="  ")
    assert visible_scopes(ctx) == [
        (Scope.GLOBAL, None),
        (Scope.USER, "u1"),
    ]


def test_scope_context_is_frozen():
    ctx = ScopeContext("ws1")
    assert (ctx.agent_id, ctx.end_user_id, ctx.session_id) == (None, None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.agent_id = "a1"


def test_enum_values_match_domain_vocabulary():
    assert [item.value for item in MemoryType] == [
        "working",
        "episodic",
        "semantic",
        "procedural",
        "perceptual",
    ]
    assert [item.value for item in Scope] == ["global", "agent", "user", "session"]
    assert [item.value for item in MemoryStatus] == [
        "active",
        "archived",
        "superseded",
        "deleted",
        "purged",
    ]
