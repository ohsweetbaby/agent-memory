"""API Key：一次性明文、Argon2id 哈希、吊销、scope 与 workspace 隔离。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.api_key import create_api_key, verify_api_key

pytestmark = pytest.mark.integration


def test_api_key_hash_is_not_plaintext(db_session, admin) -> None:
    key, raw = create_api_key(db_session, "ws1", "cursor", ["read"])
    assert key.key_hash != raw
    assert key.key_hash.startswith("$argon2")
    assert raw not in key.key_hash


def test_api_key_plaintext_format_and_lookup(db_session, admin) -> None:
    key, raw = create_api_key(db_session, "ws1", "cursor", ["read", "write"])
    assert raw.startswith("mem_")
    assert raw.startswith(f"mem_{key.prefix}_")
    assert key.scopes == ["read", "write"]

    found = verify_api_key(db_session, raw)
    assert found is not None and found.id == key.id
    assert verify_api_key(db_session, raw[:-1] + ("a" if raw[-1] != "a" else "b")) is None


def test_api_key_rejects_unknown_scope(db_session, admin) -> None:
    with pytest.raises(ValueError):
        create_api_key(db_session, "ws1", "bad", ["root"])


def test_revoked_key_is_rejected(client, revoked_key: str) -> None:
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {revoked_key}"}
    )
    assert response.status_code == 401


def test_key_is_returned_in_plaintext_only_on_creation(
    client, admin, workspace, read_key
) -> None:
    assert read_key["key"].startswith(f"mem_{read_key['prefix']}_")

    listed = client.get(f"/api/v1/keys?workspace_id={workspace['id']}")
    assert listed.status_code == 200
    keys = listed.json()["items"]
    assert [item["id"] for item in keys] == [read_key["id"]]
    assert "key" not in keys[0]
    assert "key_hash" not in keys[0]


def test_valid_key_authenticates_me(client, read_key) -> None:
    client.cookies.clear()
    response = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {read_key['key']}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "api_key"
    assert body["api_key"]["workspace_id"] == read_key["workspace_id"]
    assert body["scopes"] == ["read"]


def test_read_key_cannot_write(client, read_key) -> None:
    client.cookies.clear()
    response = client.post(
        "/api/v1/workspaces",
        json={"name": "越权"},
        headers={"Authorization": f"Bearer {read_key['key']}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_key_cannot_reach_other_workspace(client, admin, read_key) -> None:
    other = client.post("/api/v1/workspaces", json={"name": "另一个工作区"})
    assert other.status_code == 201

    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {read_key['key']}"
    response = client.get(f"/api/v1/workspaces/{other.json()['id']}")
    assert response.status_code == 404


def test_key_rate_limit_override_is_honored(db_session, admin) -> None:
    from app.auth.ratelimit import enforce_api_key_rate
    from app.errors import RateLimitError

    key, _ = create_api_key(db_session, "ws1", "limited", ["read"], rate_limit=1)
    enforce_api_key_rate(key.id, write=False, override=key.rate_limit)
    with pytest.raises(RateLimitError):
        enforce_api_key_rate(key.id, write=False, override=key.rate_limit)


def test_api_key_read_rate_limit_is_enforced(app_factory, workspace, read_key) -> None:
    """同一 Key 的读请求超过配置额度后返回 429，计数落在真实 Redis。"""
    with TestClient(app_factory(api_key_read_rpm=2)) as limited:
        headers = {"Authorization": f"Bearer {read_key['key']}"}
        url = f"/api/v1/agents?workspace_id={workspace['id']}"
        assert limited.get(url, headers=headers).status_code == 200
        assert limited.get(url, headers=headers).status_code == 200
        limited_response = limited.get(url, headers=headers)
        assert limited_response.status_code == 429
        assert limited_response.json()["error"]["code"] == "rate_limited"
