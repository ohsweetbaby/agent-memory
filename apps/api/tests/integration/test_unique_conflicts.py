"""唯一冲突回归：预检查未命中（并发窗口）时必须由数据库唯一索引兜底成 409。

这些测试通过屏蔽重复预检查，稳定复现“两个请求同时通过预检查”的窗口，
确保 `flush` / `write_audit` 触发的 IntegrityError 被转成 409 而不是 500。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog

pytestmark = pytest.mark.integration


@contextmanager
def _tolerant_client(app, client):
    """服务端异常返回 500 响应，便于断言“不是 500”。"""
    with TestClient(app, raise_server_exceptions=False) as tolerant:
        tolerant.cookies.update(client.cookies)
        tolerant.headers["X-CSRF-Token"] = client.headers["X-CSRF-Token"]
        yield tolerant


def _miss_duplicate_precheck(monkeypatch) -> None:
    """模拟并发窗口：预检查看不到即将冲突的行。"""
    monkeypatch.setattr(Session, "scalar", lambda self, *args, **kwargs: None)


def _count_action(session_factory, action: str) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
            ).scalar_one()
        )


def test_agent_create_conflict_at_flush_returns_409(
    app, client, admin, workspace, session_factory, monkeypatch
) -> None:
    payload = {"workspace_id": workspace["id"], "name": "并发助手"}
    with _tolerant_client(app, client) as tolerant:
        assert tolerant.post("/api/v1/agents", json=payload).status_code == 201
        _miss_duplicate_precheck(monkeypatch)
        response = tolerant.post("/api/v1/agents", json=payload)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert _count_action(session_factory, "agent.created") == 1


def test_end_user_create_conflict_at_flush_returns_409(
    app, client, admin, workspace, session_factory, monkeypatch
) -> None:
    payload = {"workspace_id": workspace["id"], "external_id": "u_race"}
    with _tolerant_client(app, client) as tolerant:
        assert tolerant.post("/api/v1/end-users", json=payload).status_code == 201
        _miss_duplicate_precheck(monkeypatch)
        response = tolerant.post("/api/v1/end-users", json=payload)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert _count_action(session_factory, "end_user.created") == 1


def test_agent_rename_conflict_at_flush_returns_409(
    app, client, admin, workspace, session_factory, monkeypatch
) -> None:
    first = client.post(
        "/api/v1/agents", json={"workspace_id": workspace["id"], "name": "甲"}
    ).json()
    second = client.post(
        "/api/v1/agents", json={"workspace_id": workspace["id"], "name": "乙"}
    ).json()

    with _tolerant_client(app, client) as tolerant:
        _miss_duplicate_precheck(monkeypatch)
        response = tolerant.patch(
            f"/api/v1/agents/{second['id']}", json={"name": "甲"}
        )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert _count_action(session_factory, "agent.updated") == 0

    with session_factory() as session:
        from app.models import Agent

        assert session.get(Agent, second["id"]).name == "乙"
        assert session.get(Agent, first["id"]).name == "甲"


def test_end_user_rename_conflict_at_flush_returns_409(
    app, client, admin, workspace, session_factory, monkeypatch
) -> None:
    first = client.post(
        "/api/v1/end-users",
        json={"workspace_id": workspace["id"], "external_id": "u_a"},
    ).json()
    second = client.post(
        "/api/v1/end-users",
        json={"workspace_id": workspace["id"], "external_id": "u_b"},
    ).json()

    with _tolerant_client(app, client) as tolerant:
        _miss_duplicate_precheck(monkeypatch)
        response = tolerant.patch(
            f"/api/v1/end-users/{second['id']}", json={"external_id": "u_a"}
        )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert _count_action(session_factory, "end_user.updated") == 0

    with session_factory() as session:
        from app.models import EndUser

        assert session.get(EndUser, second["id"]).external_id == "u_b"
        assert session.get(EndUser, first["id"]).external_id == "u_a"
