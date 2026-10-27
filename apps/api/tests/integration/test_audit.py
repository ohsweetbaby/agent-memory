"""审计覆盖（规格 §12）：登录与 Key/工作区/Agent/EndUser 的写操作。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import AuditLog

pytestmark = pytest.mark.integration


def _rows(session_factory):
    with session_factory() as session:
        return list(session.scalars(select(AuditLog).order_by(AuditLog.created_at)))


def _actions(session_factory) -> set[str]:
    return {row.action for row in _rows(session_factory)}


def _count_action(session_factory, action: str) -> int:
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
            )
            or 0
        )


def test_auth_events_are_audited(client, admin, session_factory) -> None:
    # admin fixture 已完成 setup + login。
    assert {"auth.setup", "auth.login"} <= _actions(session_factory)

    with session_factory() as session:
        setup_row = session.scalars(
            select(AuditLog).where(AuditLog.action == "auth.setup")
        ).one()
    assert setup_row.actor_type == "system"
    assert setup_row.target_type == "admin_user"
    assert setup_row.workspace_id is None

    assert client.post("/api/v1/auth/logout").status_code == 204
    assert (
        client.post("/api/v1/auth/login", json={"password": "wrong"}).status_code == 401
    )
    assert {"auth.logout", "auth.login_failed"} <= _actions(session_factory)

    with session_factory() as session:
        failed = session.scalars(
            select(AuditLog).where(AuditLog.action == "auth.login_failed")
        ).one()
    assert failed.workspace_id is None
    assert failed.target_id == "admin"
    assert failed.actor_type == "anonymous"


def test_resource_events_are_audited(client, admin, session_factory) -> None:
    ws = client.post("/api/v1/workspaces", json={"name": "审计工作区"}).json()
    assert (
        client.patch(
            f"/api/v1/workspaces/{ws['id']}", json={"name": "改名后的工作区"}
        ).status_code
        == 200
    )

    agent = client.post(
        "/api/v1/agents", json={"workspace_id": ws["id"], "name": "审计助手"}
    ).json()
    assert (
        client.patch(
            f"/api/v1/agents/{agent['id']}", json={"description": "新描述"}
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/agents/{agent['id']}").status_code == 204

    key = client.post(
        "/api/v1/keys",
        json={"workspace_id": ws["id"], "name": "审计 Key", "scopes": ["read"]},
    ).json()
    assert client.delete(f"/api/v1/keys/{key['id']}").status_code == 204

    end_user = client.post(
        "/api/v1/end-users", json={"workspace_id": ws["id"], "external_id": "u_audit"}
    ).json()
    assert (
        client.patch(
            f"/api/v1/end-users/{end_user['id']}", json={"display_name": "审计用户"}
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/end-users/{end_user['id']}").status_code == 204

    expected = {
        "workspace.created",
        "workspace.updated",
        "agent.created",
        "agent.updated",
        "agent.deleted",
        "key.created",
        "key.revoked",
        "end_user.created",
        "end_user.updated",
        "end_user.deleted",
    }
    assert expected <= _actions(session_factory)

    rows = _rows(session_factory)
    resource_rows = [row for row in rows if row.action in expected]
    assert all(row.actor_type == "admin" for row in resource_rows)
    assert all(row.actor_id == admin.id for row in resource_rows)
    assert all(row.workspace_id == ws["id"] for row in resource_rows)

    created_key = next(row for row in rows if row.action == "key.created")
    assert created_key.target_id == key["id"]
    # 审计里绝不能出现一次性明文或哈希。
    assert "key" not in (created_key.after or {})
    assert key["key"] not in str(created_key.after)

    updated_agent = next(row for row in rows if row.action == "agent.updated")
    assert updated_agent.before is not None and updated_agent.after is not None
    assert updated_agent.before["description"] != updated_agent.after["description"]


def test_failed_business_write_is_not_audited(client, admin, session_factory) -> None:
    ws = client.post("/api/v1/workspaces", json={"name": "冲突工作区"}).json()
    payload = {"workspace_id": ws["id"], "name": "重复助手"}
    assert client.post("/api/v1/agents", json=payload).status_code == 201
    assert client.post("/api/v1/agents", json=payload).status_code == 409

    with session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "agent.created", AuditLog.workspace_id == ws["id"])
        )
    assert count == 1


def test_duplicate_end_user_creation_returns_409_without_audit(
    client, admin, workspace, session_factory
) -> None:
    payload = {"workspace_id": workspace["id"], "external_id": "u_duplicate"}
    assert client.post("/api/v1/end-users", json=payload).status_code == 201
    duplicate = client.post("/api/v1/end-users", json=payload)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "conflict"
    assert _count_action(session_factory, "end_user.created") == 1


def test_duplicate_rename_returns_409_without_audit(
    client, admin, workspace, session_factory
) -> None:
    first = client.post(
        "/api/v1/agents", json={"workspace_id": workspace["id"], "name": "甲"}
    ).json()
    second = client.post(
        "/api/v1/agents", json={"workspace_id": workspace["id"], "name": "乙"}
    ).json()

    renamed = client.patch(f"/api/v1/agents/{second['id']}", json={"name": "甲"})
    assert renamed.status_code == 409, renamed.text
    assert renamed.json()["error"]["code"] == "conflict"
    assert client.get(f"/api/v1/agents/{second['id']}").json()["name"] == "乙"
    assert _count_action(session_factory, "agent.updated") == 0

    user_one = client.post(
        "/api/v1/end-users",
        json={"workspace_id": workspace["id"], "external_id": "u_one"},
    ).json()
    user_two = client.post(
        "/api/v1/end-users",
        json={"workspace_id": workspace["id"], "external_id": "u_two"},
    ).json()
    assert first["id"] and user_one["id"]

    renamed_user = client.patch(
        f"/api/v1/end-users/{user_two['id']}", json={"external_id": "u_one"}
    )
    assert renamed_user.status_code == 409, renamed_user.text
    assert renamed_user.json()["error"]["code"] == "conflict"

    items = client.get(f"/api/v1/end-users?workspace_id={workspace['id']}").json()["items"]
    by_id = {item["id"]: item for item in items}
    assert by_id[user_two["id"]]["external_id"] == "u_two"
    assert by_id[user_one["id"]]["external_id"] == "u_one"
    assert _count_action(session_factory, "end_user.updated") == 0
