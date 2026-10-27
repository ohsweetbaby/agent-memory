"""管理员认证：setup、会话 Cookie、CSRF、登录限流与密钥加密。"""

from __future__ import annotations

import pytest

from app.auth.password import decrypt_secret, encrypt_secret

pytestmark = pytest.mark.integration

PASSWORD = "p" * 12


def test_setup_allowed_only_once(client) -> None:
    assert client.post("/api/v1/auth/setup", json={"password": PASSWORD}).status_code == 201
    assert client.post("/api/v1/auth/setup", json={"password": PASSWORD}).status_code == 409


def test_setup_rejects_short_password(client) -> None:
    response = client.post("/api/v1/auth/setup", json={"password": "short"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_login_sets_httponly_cookie(client, admin) -> None:
    response = client.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert response.json()["csrf_token"]


def test_me_accepts_session_cookie(client, admin) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["kind"] == "session"
    assert response.json()["admin"]["username"] == "admin"


def test_me_requires_authentication(client) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_login_rejects_wrong_password(client, admin) -> None:
    response = client.post("/api/v1/auth/login", json={"password": "not-the-password"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_csrf_is_required_for_cookie_writes(client, admin) -> None:
    client.headers.pop("X-CSRF-Token")
    response = client.post("/api/v1/workspaces", json={"name": "没有 CSRF"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_login_is_rate_limited_after_five_failures(client, admin) -> None:
    for _ in range(5):
        assert (
            client.post("/api/v1/auth/login", json={"password": "wrong"}).status_code == 401
        )
    response = client.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.headers["retry-after"]


def test_logout_clears_session(client, admin) -> None:
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401


def test_production_session_cookie_is_secure(app_factory) -> None:
    """生产环境 Cookie 必须带 Secure；开发/测试环境不加，方便本地 HTTP。"""
    from fastapi.testclient import TestClient

    with TestClient(app_factory(environment="production")) as production:
        assert production.post(
            "/api/v1/auth/setup", json={"password": PASSWORD}
        ).status_code == 201
        response = production.post("/api/v1/auth/login", json={"password": PASSWORD})
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()


def test_provider_secret_is_encrypted_and_roundtrips() -> None:
    blob = encrypt_secret("sk-secret")
    assert b"sk-secret" not in blob
    assert decrypt_secret(blob) == "sk-secret"
