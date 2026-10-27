"""并发安全：setup 单实例与登录限流的串行化。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.auth.ratelimit import get_redis
from app.models import AdminUser

pytestmark = pytest.mark.integration

PASSWORD = "p" * 12


def test_concurrent_setup_creates_exactly_one_admin(app, session_factory) -> None:
    """两个线程用不同用户名同时 setup：恰好一个 201、一个 409。"""
    barrier = threading.Barrier(2)

    def attempt(username: str) -> int:
        with TestClient(app) as thread_client:
            barrier.wait(timeout=30)
            return thread_client.post(
                "/api/v1/auth/setup",
                json={"password": PASSWORD, "username": username},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(attempt, ["first-admin", "second-admin"]))

    assert statuses == [201, 409]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AdminUser)) == 1


def test_concurrent_login_failures_are_gated(app, admin, monkeypatch) -> None:
    """校验在途时其它并发登录直接 429，失败计数不超过阈值。"""
    import app.api.auth as auth_module

    real_verify = auth_module.verify_password

    def slow_verify(raw: str, hashed: str) -> bool:
        time.sleep(0.15)  # 拉长“校验在途”窗口，保证并发请求真实重叠
        return real_verify(raw, hashed)

    monkeypatch.setattr(auth_module, "verify_password", slow_verify)

    workers = 5
    barrier = threading.Barrier(workers)

    def attempt(_: int) -> int:
        with TestClient(app) as thread_client:
            barrier.wait(timeout=30)
            return thread_client.post(
                "/api/v1/auth/login", json={"password": "wrong"}
            ).status_code

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(attempt, range(workers)))

    assert 429 in statuses
    assert statuses.count(401) <= 2

    client_redis = get_redis()
    fail_keys = list(client_redis.keys("login:fail:*"))
    total_failures = sum(int(client_redis.get(key) or 0) for key in fail_keys)
    assert total_failures <= 5


def test_login_failure_counter_and_attempt_token_have_ttl(client, admin) -> None:
    assert (
        client.post("/api/v1/auth/login", json={"password": "wrong"}).status_code == 401
    )
    client_redis = get_redis()
    fail_keys = list(client_redis.keys("login:fail:*"))
    assert fail_keys
    assert all(client_redis.ttl(key) > 0 for key in fail_keys)
    # attempt 令牌在请求结束后释放，不留孤儿键。
    assert client_redis.keys("login:attempt:*") == []


def test_api_key_rate_bucket_has_ttl(client, read_key, workspace) -> None:
    client.cookies.clear()
    response = client.get(
        f"/api/v1/agents?workspace_id={workspace['id']}",
        headers={"Authorization": f"Bearer {read_key['key']}"},
    )
    assert response.status_code == 200

    client_redis = get_redis()
    buckets = list(client_redis.keys("api_key:*"))
    assert buckets
    assert all(client_redis.ttl(key) > 0 for key in buckets)


def test_setup_advisory_lock_is_reentrant_within_transaction(session_factory) -> None:
    """同一持有者重复获取 xact advisory lock 不阻塞（PG 语义），提交后释放。"""
    from app.api.auth import SETUP_ADVISORY_LOCK_ID

    with session_factory() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETUP_ADVISORY_LOCK_ID}
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": SETUP_ADVISORY_LOCK_ID}
        )
        session.rollback()
