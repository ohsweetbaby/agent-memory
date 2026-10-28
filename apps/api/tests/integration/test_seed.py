"""演示数据种子：数量、幂等与重复执行。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Agent, EndUser, Episode, Memory, ReviewItem, Workspace
from app.seed import seed_workspace


def test_seed_creates_demo_data_and_is_idempotent(db_session: Session) -> None:
    first = seed_workspace(
        db_session, "ws_seed_test", memory_count=40, review_count=6
    )
    db_session.commit()

    assert first.workspace_id == "ws_seed_test"
    assert first.agents == 5
    assert first.end_users == 3
    assert first.sessions == 2
    assert first.memories == 40
    assert first.review_items == 6
    assert first.episodes == 8

    second = seed_workspace(
        db_session, "ws_seed_test", memory_count=40, review_count=6
    )
    db_session.commit()

    assert second.memories == 40
    assert second.review_items == 6
    assert db_session.scalar(
        select(func.count(Workspace.id)).where(Workspace.id == "ws_seed_test")
    ) == 1
    assert db_session.scalar(
        select(func.count(Memory.id)).where(Memory.workspace_id == "ws_seed_test")
    ) == 40
    assert db_session.scalar(
        select(func.count(ReviewItem.id)).where(
            ReviewItem.workspace_id == "ws_seed_test"
        )
    ) == 6
    assert db_session.scalar(
        select(func.count(Agent.id)).where(Agent.workspace_id == "ws_seed_test")
    ) == 5
    assert db_session.scalar(
        select(func.count(EndUser.id)).where(EndUser.workspace_id == "ws_seed_test")
    ) == 3
    assert db_session.scalar(
        select(func.count(Episode.id))
    ) == 8


def test_seed_does_not_touch_other_workspaces(db_session: Session) -> None:
    seed_workspace(db_session, "ws_seed_a", memory_count=10, review_count=2)
    db_session.commit()
    seed_workspace(db_session, "ws_seed_b", memory_count=12, review_count=3)
    db_session.commit()
    seed_workspace(db_session, "ws_seed_a", memory_count=10, review_count=2)
    db_session.commit()

    assert db_session.scalar(
        select(func.count(Memory.id)).where(Memory.workspace_id == "ws_seed_b")
    ) == 12
