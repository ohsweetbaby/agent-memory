"""并发唯一冲突必须转成 409，而不是未捕获 IntegrityError 导致的 500。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import AuditLog

pytestmark = pytest.mark.integration

WORKERS = 4


@pytest.fixture()
def admin_key(client, workspace) -> str:
    """并发请求用 Bearer 认证，避免每个线程各自处理会话 Cookie/CSRF。"""
    response = client.post(
        "/api/v1/keys",
        json={"workspace_id": workspace["id"], "name": "并发测试", "scopes": ["admin"]},
    )
    assert response.status_code == 201, response.text
    return response.json()["key"]


def _run_concurrently(
    app, workers: int, call: Callable[[TestClient, int], int]
) -> list[int]:
    barrier = threading.Barrier(workers)

    def attempt(index: int) -> int:
        with TestClient(app, raise_server_exceptions=False) as thread_client:
            barrier.wait(timeout=30)
            return call(thread_client, index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(attempt, range(workers)))


def _audit_count(session_factory, action: str) -> int:
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
            )
            or 0
        )


def test_concurrent_same_name_agent_creation_returns_409_not_500(
    app, admin_key, workspace, session_factory
) -> None:
    headers = {"Authorization": f"Bearer {admin_key}"}

    def create(thread_client: TestClient, _: int) -> int:
        return thread_client.post(
            "/api/v1/agents",
            json={"workspace_id": workspace["id"], "name": "并发助手"},
            headers=headers,
        ).status_code

    statuses = _run_concurrently(app, WORKERS, create)

    assert 500 not in statuses
    assert set(statuses) <= {201, 409}
    assert statuses.count(201) == 1
    assert _audit_count(session_factory, "agent.created") == 1


def test_concurrent_same_external_id_end_user_creation_returns_409_not_500(
    app, admin_key, workspace, session_factory
) -> None:
    headers = {"Authorization": f"Bearer {admin_key}"}

    def create(thread_client: TestClient, _: int) -> int:
        return thread_client.post(
            "/api/v1/end-users",
            json={"workspace_id": workspace["id"], "external_id": "u_concurrent"},
            headers=headers,
        ).status_code

    statuses = _run_concurrently(app, WORKERS, create)

    assert 500 not in statuses
    assert set(statuses) <= {201, 409}
    assert statuses.count(201) == 1
    assert _audit_count(session_factory, "end_user.created") == 1


def test_concurrent_agent_rename_returns_409_not_500(
    app, client, admin, admin_key, workspace, session_factory
) -> None:
    created = [
        client.post(
            "/api/v1/agents",
            json={"workspace_id": workspace["id"], "name": f"并发改名{index}"},
        ).json()
        for index in range(WORKERS)
    ]
    headers = {"Authorization": f"Bearer {admin_key}"}

    def rename(thread_client: TestClient, index: int) -> int:
        return thread_client.patch(
            f"/api/v1/agents/{created[index]['id']}",
            json={"name": "同一个目标名"},
            headers=headers,
        ).status_code

    statuses = _run_concurrently(app, WORKERS, rename)

    assert 500 not in statuses
    assert set(statuses) <= {200, 409}
    assert statuses.count(200) == 1
    assert _audit_count(session_factory, "agent.updated") == 1


def test_concurrent_end_user_rename_returns_409_not_500(
    app, client, admin, admin_key, workspace, session_factory
) -> None:
    created = [
        client.post(
            "/api/v1/end-users",
            json={
                "workspace_id": workspace["id"],
                "external_id": f"u_rename_{index}",
            },
        ).json()
        for index in range(WORKERS)
    ]
    headers = {"Authorization": f"Bearer {admin_key}"}

    def rename(thread_client: TestClient, index: int) -> int:
        return thread_client.patch(
            f"/api/v1/end-users/{created[index]['id']}",
            json={"external_id": "u_target"},
            headers=headers,
        ).status_code

    statuses = _run_concurrently(app, WORKERS, rename)

    assert 500 not in statuses
    assert set(statuses) <= {200, 409}
    assert statuses.count(200) == 1
    assert _audit_count(session_factory, "end_user.updated") == 1
