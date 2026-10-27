"""记忆 REST 资源：权限、反馈、详情、更新、删除与合并。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import AuditLog

pytestmark = pytest.mark.integration


def test_feedback_increases_importance_and_stability(
    client, admin, db_session, memory
) -> None:
    before = client.get(f"/api/v1/memories/{memory.id}").json()
    response = client.post(
        f"/api/v1/memories/{memory.id}/feedback", json={"helpful": True}
    )
    assert response.status_code == 200, response.text
    after = client.get(f"/api/v1/memories/{memory.id}").json()
    assert after["importance"] > before["importance"]
    assert after["stability"] > before["stability"]
    assert db_session.scalar(
        select(AuditLog.id).where(
            AuditLog.action == "memory.feedback",
            AuditLog.target_id == memory.id,
        )
    )


def test_global_scope_requires_admin_key(
    client, write_key, valid_write_payload
) -> None:
    r = client.post(
        "/api/v1/memories",
        headers={"Authorization": f"Bearer {write_key}"},
        json=valid_write_payload(scope="global", scope_id=None),
    )
    assert r.status_code == 403


def test_read_key_cannot_write(client, read_key, valid_write_payload) -> None:
    r = client.post(
        "/api/v1/memories",
        headers={"Authorization": f"Bearer {read_key['key']}"},
        json=valid_write_payload(),
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"


def test_write_is_idempotent_over_http(client, admin, valid_write_payload) -> None:
    payload = valid_write_payload(idempotency_key="http-idem")
    first = client.post("/api/v1/memories", json=payload)
    second = client.post("/api/v1/memories", json=payload)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_detail_patch_pin_and_soft_delete(
    client, admin, db_session, valid_write_payload
) -> None:
    created = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="用户偏好深色主题"),
    ).json()

    detail = client.get(f"/api/v1/memories/{created['id']}").json()
    assert detail["content"] == "用户偏好深色主题"
    assert detail["status"] == "active"

    patched = client.patch(
        f"/api/v1/memories/{created['id']}",
        json={"importance": 0.7, "tags": ["ui", "偏好"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["importance"] == 0.7
    assert patched.json()["metadata"]["tags"] == ["ui", "偏好"]

    pinned = client.post(f"/api/v1/memories/{created['id']}/pin")
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["importance"] == 1.0

    deleted = client.delete(f"/api/v1/memories/{created['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/memories/{created['id']}").status_code == 404

    actions = set(
        db_session.scalars(
            select(AuditLog.action).where(
                AuditLog.target_type == "memory",
                AuditLog.target_id == created["id"],
            )
        )
    )
    assert {"memory.created", "memory.updated", "memory.pinned", "memory.deleted"} <= actions
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "memory.deleted")
        )
        == 1
    )


def test_merge_archives_sources_and_moves_links(
    client, admin, db_session, valid_write_payload
) -> None:
    source = client.post(
        "/api/v1/memories", json=valid_write_payload(content="用户喜欢咖啡")
    ).json()
    target = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="用户喜欢咖啡和茶", dedupe=False),
    ).json()

    response = client.post(
        "/api/v1/memories/merge",
        json={"source_ids": [source["id"]], "target_id": target["id"]},
    )
    assert response.status_code == 200, response.text
    merged_source = client.get(f"/api/v1/memories/{source['id']}").json()
    assert merged_source["status"] == "archived"
    assert merged_source["metadata"]["merged_into"] == target["id"]
    assert db_session.scalar(
        select(AuditLog.id).where(
            AuditLog.action == "memory.merged",
            AuditLog.target_id == source["id"],
        )
    )


def test_cross_workspace_access_returns_404(
    client, admin, read_key, workspace, valid_write_payload
) -> None:
    other = client.post("/api/v1/workspaces", json={"name": "另一个工作区"})
    assert other.status_code == 201
    other_memory = client.post(
        "/api/v1/memories",
        json=valid_write_payload(
            workspace_id=other.json()["id"], content="其他工作区的秘密"
        ),
    )
    assert other_memory.status_code == 201, other_memory.text

    response = client.get(
        f"/api/v1/memories/{other_memory.json()['id']}",
        headers={"Authorization": f"Bearer {read_key['key']}"},
    )
    assert response.status_code == 404


def test_list_is_workspace_scoped_and_excludes_deleted(
    client, admin, valid_write_payload
) -> None:
    first = client.post(
        "/api/v1/memories", json=valid_write_payload(content="列表记忆一")
    ).json()
    second = client.post(
        "/api/v1/memories",
        json=valid_write_payload(content="列表记忆二", dedupe=False),
    ).json()
    client.delete(f"/api/v1/memories/{first['id']}")

    items = client.get("/api/v1/memories?limit=50").json()["items"]
    ids = {item["id"] for item in items}
    assert second["id"] in ids
    assert first["id"] not in ids


def test_memory_audit_omits_snapshots_and_reports_changed_fields(
    client, admin, valid_write_payload
) -> None:
    created = client.post(
        "/api/v1/memories", json=valid_write_payload(content="审计正文不可外泄")
    ).json()
    assert (
        client.patch(
            f"/api/v1/memories/{created['id']}",
            json={"content": "更新后的正文", "importance": 0.8},
        ).status_code
        == 200
    )
    assert client.post(f"/api/v1/memories/{created['id']}/pin").status_code == 200

    response = client.get(f"/api/v1/memories/{created['id']}/audit")
    assert response.status_code == 200, response.text
    body = response.json()
    actions = [item["action"] for item in body["items"]]
    assert {"memory.created", "memory.updated", "memory.pinned"} <= set(actions)

    updated = next(item for item in body["items"] if item["action"] == "memory.updated")
    assert {"content", "importance"} <= set(updated["changed_fields"])
    for item in body["items"]:
        assert set(item) == {
            "id",
            "action",
            "actor_type",
            "changed_fields",
            "created_at",
        }
    serialized = response.text
    assert "审计正文不可外泄" not in serialized
    assert "更新后的正文" not in serialized


def test_memory_audit_returns_404_for_deleted_memory(
    client, admin, valid_write_payload
) -> None:
    created = client.post(
        "/api/v1/memories", json=valid_write_payload(content="删除后无审计入口")
    ).json()
    assert client.delete(f"/api/v1/memories/{created['id']}").status_code == 204
    assert (
        client.get(f"/api/v1/memories/{created['id']}/audit").status_code == 404
    )
