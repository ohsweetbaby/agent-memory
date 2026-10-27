"""Provider 管理：密钥只进不出、更新留空不修改、连通性失败可读。"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import ProviderConfig

pytestmark = pytest.mark.integration


def _create(client, **overrides):
    payload = {
        "kind": "embedding",
        "name": "测试 Provider",
        "base_url": "http://127.0.0.1:9/v1",
        "model": "text-embedding-test",
        "dim": 8,
        "api_key": "sk-super-secret",
    }
    payload.update(overrides)
    return client.post("/api/v1/providers", json=payload)


def test_provider_secret_is_write_only(client, admin, session_factory):
    created = _create(client)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["has_secret"] is True
    assert "api_key" not in body
    assert "encrypted_secret" not in body
    assert "sk-super-secret" not in created.text

    listed = client.get("/api/v1/providers")
    assert listed.status_code == 200
    payload = listed.json()
    assert len(payload["items"]) == 1
    assert "sk-super-secret" not in listed.text
    assert payload["policy"]["purge_retention_days"] >= 1

    with session_factory() as session:
        stored = session.scalars(select(ProviderConfig)).one()
        assert stored.encrypted_secret is not None
        assert b"sk-super-secret" not in stored.encrypted_secret


def test_provider_update_blank_secret_keeps_ciphertext(
    client, admin, session_factory: sessionmaker[Session]
):
    created = _create(client).json()
    with session_factory() as session:
        original = session.get(ProviderConfig, created["id"]).encrypted_secret

    renamed = client.patch(
        f"/api/v1/providers/{created['id']}",
        json={"name": "重命名 Provider", "api_key": ""},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "重命名 Provider"
    assert renamed.json()["has_secret"] is True

    with session_factory() as session:
        unchanged = session.get(ProviderConfig, created["id"]).encrypted_secret
    assert unchanged == original

    rotated = client.patch(
        f"/api/v1/providers/{created['id']}",
        json={"api_key": "sk-rotated"},
    )
    assert rotated.status_code == 200
    with session_factory() as session:
        changed = session.get(ProviderConfig, created["id"]).encrypted_secret
    assert changed != original


def test_provider_connectivity_failure_is_readable(client, admin):
    response = client.post(
        "/api/v1/providers/test",
        json={"base_url": "http://127.0.0.1:9"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["message"].startswith("连接失败")
    assert "sk-" not in response.text


def test_saved_provider_test_uses_encrypted_secret(client, admin):
    created = _create(client).json()
    response = client.post(f"/api/v1/providers/{created['id']}/test")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert "连接失败" in response.json()["message"]


def test_provider_delete(client, admin):
    created = _create(client, name="待删除").json()
    deleted = client.delete(f"/api/v1/providers/{created['id']}")
    assert deleted.status_code == 204
    missing = client.get(f"/api/v1/providers/{created['id']}")
    assert missing.status_code == 404
