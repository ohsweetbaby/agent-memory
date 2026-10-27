"""任务 12：记忆 REST 资源、权限边界、统计与时间线。

本文件覆盖计划中的 10 个用例，并按既有裁决做了两处适配：

- 控制字符不使用 NUL（T9 已裁决 NUL 返回 422），改用 ``\\x07`` 与 ESC；
- 跨 workspace 用例使用 API Key，因为管理员会话按 T8 裁决可跨工作区管理。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, or_, select

from app.models import Episode, Memory, MemoryLink, ReviewItem, Session
from app.models.base import utcnow

pytestmark = pytest.mark.integration


@pytest.fixture()
def seeded_memories(
    client,
    admin,
    workspace,
    valid_write_payload,
    session_factory,
) -> list[SimpleNamespace]:
    """一组可复用的记忆，覆盖分页、遗忘、统计与时间线。"""
    rows = [
        ("用户对花生过敏", "user", "user-1", "semantic"),
        ("用户喜欢喝绿茶", "user", "user-1", "semantic"),
        ("用户在北京工作", "user", "user-2", "semantic"),
        ("项目代号是海鸥", "agent", "agent-1", "procedural"),
        ("用户偏好深色主题", "user", "user-1", "semantic"),
        ("当前会话讨论了发布计划", "session", "s1", "episodic"),
        ("s1 决定周五发布", "session", "s1", "episodic"),
        ("部署窗口在周四晚上", "global", None, "procedural"),
    ]
    created: list[SimpleNamespace] = []
    for content, scope, scope_id, memory_type in rows:
        response = client.post(
            "/api/v1/memories",
            json=valid_write_payload(
                content=content,
                scope=scope,
                scope_id=scope_id,
                type=memory_type,
                dedupe=False,
            ),
        )
        assert response.status_code == 201, response.text
        created.append(SimpleNamespace(**response.json()))

    now = utcnow()
    with session_factory() as session:
        session.add(
            Session(
                id="s1",
                workspace_id=workspace["id"],
                started_at=now - timedelta(minutes=30),
            )
        )
        session.flush()
        session.add(
            Episode(
                session_id="s1",
                title="发布计划讨论",
                summary="决定周五发布",
                started_at=now - timedelta(minutes=10),
                ended_at=now - timedelta(minutes=5),
                outcome="planned",
            )
        )
        session.add(
            ReviewItem(
                workspace_id=workspace["id"],
                candidate={"content": "待审核的候选记忆"},
            )
        )
        session.commit()

    return created


@pytest.fixture()
def other_workspace_memory(
    client, admin, valid_write_payload
) -> SimpleNamespace:
    other = client.post("/api/v1/workspaces", json={"name": "另一个工作区"})
    assert other.status_code == 201, other.text
    response = client.post(
        "/api/v1/memories",
        json=valid_write_payload(
            workspace_id=other.json()["id"], content="其他工作区的秘密"
        ),
    )
    assert response.status_code == 201, response.text
    return SimpleNamespace(**response.json())


@pytest.fixture()
def superseded_pair(
    client, admin, valid_write_payload, session_factory
) -> tuple[str, str]:
    old = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="用户住在北京", dedupe=False),
    )
    assert old.status_code == 201, old.text
    new = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="用户搬到上海", dedupe=False),
    )
    assert new.status_code == 201, new.text

    with session_factory() as session:
        row = session.get(Memory, old.json()["id"])
        assert row is not None
        row.status = "superseded"
        row.superseded_by = new.json()["id"]
        row.valid_to = utcnow()
        session.commit()
    return old.json()["id"], new.json()["id"]


@pytest.fixture()
def mergeable_pair(
    client, admin, valid_write_payload
) -> tuple[SimpleNamespace, SimpleNamespace]:
    first = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="用户喜欢咖啡", dedupe=False),
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="用户喜欢咖啡和茶", dedupe=False),
    )
    assert second.status_code == 201, second.text
    return SimpleNamespace(**first.json()), SimpleNamespace(**second.json())


def test_get_memory_from_other_workspace_returns_404(
    client, admin, read_key, other_workspace_memory
) -> None:
    response = client.get(
        f"/api/v1/memories/{other_workspace_memory.id}",
        headers={"Authorization": f"Bearer {read_key['key']}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_patch_other_workspace_memory_returns_404(
    client, admin, write_key, other_workspace_memory
) -> None:
    response = client.patch(
        f"/api/v1/memories/{other_workspace_memory.id}",
        headers={"Authorization": f"Bearer {write_key}"},
        json={"importance": 1},
    )
    assert response.status_code == 404


def test_hard_delete_requires_confirm(client, admin, memory) -> None:
    first = client.delete(f"/api/v1/memories/{memory.id}?hard=true")
    assert first.status_code == 400
    assert first.json()["error"]["code"] == "confirmation_required"

    second = client.delete(f"/api/v1/memories/{memory.id}?hard=true&confirm=true")
    assert second.status_code == 200
    assert second.json()["hard"] is True
    assert client.get(f"/api/v1/memories/{memory.id}").status_code == 404


def test_control_characters_roundtrip(
    client, admin, valid_write_payload
) -> None:
    content = "用户说\x07\x1b[31m红色\x1b[0m"
    response = client.post(
        "/api/v1/memories", json=valid_write_payload(content=content)
    )
    assert response.status_code == 201, response.text
    detail = client.get(f"/api/v1/memories/{response.json()['id']}")
    assert detail.status_code == 200
    assert detail.json()["content"] == content


def test_pagination_is_stable(client, admin, seeded_memories) -> None:
    seen: list[str] = []
    cursor: str | None = None
    while True:
        url = "/api/v1/memories?limit=5"
        if cursor:
            url = f"{url}&cursor={cursor}"
        response = client.get(url)
        assert response.status_code == 200, response.text
        body = response.json()
        page_ids = [item["id"] for item in body["items"]]
        assert not set(page_ids) & set(seen)
        seen.extend(page_ids)
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert set(seen) == {memory.id for memory in seeded_memories}


def test_detail_returns_superseded_history(
    client, admin, superseded_pair
) -> None:
    old_id, new_id = superseded_pair
    detail = client.get(f"/api/v1/memories/{new_id}")
    assert detail.status_code == 200, detail.text
    history = detail.json()["history"]
    assert history and history[0]["id"] == old_id
    assert history[0]["status"] == "superseded"


def test_merge_moves_links_and_archives_sources(
    client, admin, mergeable_pair, valid_write_payload, session_factory
) -> None:
    source, target = mergeable_pair
    related = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="共同关联的记忆", dedupe=False),
    )
    assert related.status_code == 201, related.text
    related_id = related.json()["id"]

    with session_factory() as session:
        session.add_all(
            [
                MemoryLink(
                    from_id=source.id, to_id=related_id, relation="related"
                ),
                MemoryLink(
                    from_id=related_id, to_id=source.id, relation="related"
                ),
                # 目标已有等价边，迁移时必须去重而不是制造重复。
                MemoryLink(
                    from_id=target.id, to_id=related_id, relation="related"
                ),
            ]
        )
        session.commit()

    response = client.post(
        "/api/v1/memories/merge",
        json={"source_ids": [source.id], "target_id": target.id},
    )
    assert response.status_code == 200, response.text

    archived = client.get(f"/api/v1/memories/{source.id}").json()
    assert archived["status"] == "archived"
    assert archived["metadata"]["merged_into"] == target.id

    with session_factory() as session:
        dangling = session.scalar(
            select(func.count())
            .select_from(MemoryLink)
            .where(
                or_(
                    MemoryLink.from_id == source.id,
                    MemoryLink.to_id == source.id,
                )
            )
        )
        assert dangling == 0

        outgoing = session.scalar(
            select(func.count())
            .select_from(MemoryLink)
            .where(
                MemoryLink.from_id == target.id,
                MemoryLink.to_id == related_id,
                MemoryLink.relation == "related",
            )
        )
        assert outgoing == 1

        incoming = session.scalar(
            select(func.count())
            .select_from(MemoryLink)
            .where(
                MemoryLink.from_id == related_id,
                MemoryLink.to_id == target.id,
                MemoryLink.relation == "related",
            )
        )
        assert incoming == 1


def test_forget_by_query_soft_deletes_matches(
    client, admin, seeded_memories
) -> None:
    response = client.request(
        "DELETE",
        "/api/v1/memories",
        json={"query": "花生过敏", "scope": "user", "hard": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["affected"] >= 1
    assert body["ids"]
    assert client.get(f"/api/v1/memories/{body['ids'][0]}").status_code == 404


def test_stats_overview_returns_counts(client, admin, seeded_memories) -> None:
    response = client.get("/api/v1/stats/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_memories"] >= 1
    assert "by_type" in body and body["by_type"]["semantic"] >= 1
    assert "pending_review" in body and body["pending_review"] >= 1
    assert "by_scope" in body
    assert body["total_episodes"] >= 1


def test_timeline_is_time_ordered(client, admin, seeded_memories) -> None:
    response = client.get("/api/v1/timeline?session_id=s1")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) >= 3
    timestamps = [item["timestamp"] for item in items]
    assert timestamps == sorted(timestamps)
    kinds = {item["kind"] for item in items}
    assert kinds == {"memory", "episode"}


def test_list_hides_history_archived_and_expired_by_default(
    client, admin, superseded_pair, mergeable_pair, memory
) -> None:
    old_id, _ = superseded_pair
    source, target = mergeable_pair

    merged = client.post(
        "/api/v1/memories/merge",
        json={"source_ids": [source.id], "target_id": target.id},
    )
    assert merged.status_code == 200, merged.text

    past = (utcnow() - timedelta(days=1)).isoformat()
    expired = client.patch(
        f"/api/v1/memories/{memory.id}", json={"valid_to": past}
    )
    assert expired.status_code == 200, expired.text

    default_ids = {
        item["id"] for item in client.get("/api/v1/memories?limit=200").json()["items"]
    }
    assert {old_id, source.id, memory.id}.isdisjoint(default_ids)

    history_ids = {
        item["id"]
        for item in client.get(
            "/api/v1/memories?limit=200&include_history=true"
        ).json()["items"]
    }
    assert old_id in history_ids
    assert source.id not in history_ids

    archived_ids = {
        item["id"]
        for item in client.get(
            "/api/v1/memories?limit=200&include_archived=true"
        ).json()["items"]
    }
    assert source.id in archived_ids
    assert old_id not in archived_ids


def test_hard_delete_requires_admin_scope(
    client, admin, write_key, memory
) -> None:
    response = client.delete(
        f"/api/v1/memories/{memory.id}?hard=true&confirm=true",
        headers={"Authorization": f"Bearer {write_key}"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirmation_required"


def test_forget_by_query_hard_requires_confirmation(
    client, admin, seeded_memories
) -> None:
    response = client.request(
        "DELETE",
        "/api/v1/memories",
        json={"query": "花生过敏", "hard": True},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "confirmation_required"


def test_stats_timeseries_returns_daily_points(
    client, admin, seeded_memories
) -> None:
    response = client.get("/api/v1/stats/timeseries?days=7")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 7
    assert body["items"][-1]["count"] >= 1
    assert sum(item["count"] for item in body["items"]) >= 1


def test_admin_session_can_access_other_workspace(
    client, admin, other_workspace_memory
) -> None:
    """T8 裁决：管理台会话可跨 workspace；受限于 Key 的是 API 路径。"""
    response = client.get(f"/api/v1/memories/{other_workspace_memory.id}")
    assert response.status_code == 200
    assert response.json()["workspace_id"] == other_workspace_memory.workspace_id


def test_stats_and_timeline_are_workspace_scoped_for_api_keys(
    client, admin, seeded_memories, other_workspace_memory
) -> None:
    primary_key = client.post(
        "/api/v1/keys",
        json={
            "workspace_id": seeded_memories[0].workspace_id,
            "name": "primary-reader",
            "scopes": ["read"],
        },
    )
    assert primary_key.status_code == 201, primary_key.text
    other_key = client.post(
        "/api/v1/keys",
        json={
            "workspace_id": other_workspace_memory.workspace_id,
            "name": "other-reader",
            "scopes": ["read"],
        },
    )
    assert other_key.status_code == 201, other_key.text

    primary_overview = client.get(
        "/api/v1/stats/overview",
        headers={"Authorization": f"Bearer {primary_key.json()['key']}"},
    ).json()
    other_overview = client.get(
        "/api/v1/stats/overview",
        headers={"Authorization": f"Bearer {other_key.json()['key']}"},
    ).json()
    assert primary_overview["total_memories"] >= len(seeded_memories)
    assert other_overview["total_memories"] == 1

    other_timeline = client.get(
        "/api/v1/timeline?session_id=s1",
        headers={"Authorization": f"Bearer {other_key.json()['key']}"},
    )
    assert other_timeline.status_code == 200, other_timeline.text
    assert other_timeline.json()["items"] == []


def test_timeline_supports_time_range(
    client, admin, seeded_memories
) -> None:
    now = utcnow()
    response = client.get(
        "/api/v1/timeline",
        params={
            "session_id": "s1",
            "from": (now - timedelta(hours=1)).isoformat(),
            "to": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"]


def test_timeline_entity_filter(
    client, admin, valid_write_payload
) -> None:
    created = client.post(
        "/api/v1/memories",
        json=valid_write_payload(
            content="Alice 负责发布",
            scope="session",
            scope_id="s1",
            entities=[{"name": "Alice", "type": "person"}],
            dedupe=False,
        ),
    )
    assert created.status_code == 201, created.text

    response = client.get("/api/v1/timeline?entity=Alice")
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert created.json()["id"] in ids
