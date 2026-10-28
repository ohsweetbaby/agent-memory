"""Provider 管理：密钥只进不出、更新留空不修改、连通性失败可读。"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditLog, ProviderConfig

pytestmark = pytest.mark.integration


class StubProviderServer:
    """本地 OpenAI 兼容 stub：记录请求路径/头，可返回慢响应或重定向。"""

    def __init__(self) -> None:
        self.status = 200
        self.delay = 0.0
        self.requests: list[tuple[str, dict[str, str]]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 协议
                owner.requests.append(
                    (
                        self.path,
                        {
                            key.lower(): value
                            for key, value in self.headers.items()
                        },
                    )
                )
                if owner.delay:
                    time.sleep(owner.delay)
                try:
                    self.send_response(owner.status)
                    if 300 <= owner.status < 400:
                        self.send_header("Location", "http://127.0.0.1:9/")
                    self.end_headers()
                    self.wfile.write(b'{"data": []}')
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args: Any) -> None:
                return None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def stub_server() -> Iterator[StubProviderServer]:
    server = StubProviderServer()
    try:
        yield server
    finally:
        server.close()


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


def test_provider_connectivity_failure_is_readable(
    client, admin, session_factory: sessionmaker[Session]
):
    response = client.post(
        "/api/v1/providers/test",
        json={"base_url": "http://127.0.0.1:9"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["message"].startswith("连接失败")
    assert "sk-" not in response.text

    with session_factory() as session:
        audit = session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "provider.tested")
            .order_by(AuditLog.created_at.desc())
        ).first()
        assert audit is not None
        assert audit.target_id == "draft"
        assert audit.after["ok"] is False
        assert "sk-" not in json.dumps(audit.after, ensure_ascii=False)


def test_saved_provider_test_uses_encrypted_secret(
    client, admin, session_factory: sessionmaker[Session]
):
    created = _create(client).json()
    response = client.post(f"/api/v1/providers/{created['id']}/test")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert "连接失败" in response.json()["message"]

    with session_factory() as session:
        audit = session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "provider.tested")
            .order_by(AuditLog.created_at.desc())
        ).first()
        assert audit is not None
        assert audit.target_id == created["id"]
        assert audit.after["ok"] is False
        assert "sk-" not in json.dumps(audit.after, ensure_ascii=False)


def test_probe_hard_timeout_stops_slow_response(
    client, admin, stub_server: StubProviderServer, monkeypatch
):
    monkeypatch.setattr(
        "app.api.providers.PROVIDER_TEST_TIMEOUT_SECONDS", 0.2
    )
    stub_server.delay = 1.0
    started = time.perf_counter()
    response = client.post(
        "/api/v1/providers/test", json={"base_url": stub_server.url}
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert "请求超时" in response.json()["message"]
    # 硬超时生效，而不是等 stub 的 1 秒响应。
    assert elapsed < 0.8


def test_probe_treats_redirect_as_failure(
    client, admin, stub_server: StubProviderServer
):
    stub_server.status = 302
    response = client.post(
        "/api/v1/providers/test", json={"base_url": stub_server.url}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert "重定向" in body["message"]
    assert body["status_code"] == 302


def test_stored_secret_is_bound_to_stored_base_url(
    client, admin, stub_server: StubProviderServer
):
    created = _create(
        client,
        name="绑定地址 Provider",
        base_url=f"{stub_server.url}/stored/v1",
    ).json()

    # 请求覆盖 base_url；复用已存密钥时必须忽略它，只访问 stored.base_url。
    response = client.post(
        "/api/v1/providers/test",
        json={
            "provider_id": created["id"],
            "base_url": "http://127.0.0.1:9/evil/v1",
            "api_key": "",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert [path for path, _ in stub_server.requests] == ["/stored/v1/models"]
    assert (
        stub_server.requests[0][1]["authorization"] == "Bearer sk-super-secret"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://[fd00:ec2::254]/latest",
    ],
)
def test_probe_rejects_metadata_targets(client, admin, url: str):
    started = time.perf_counter()
    response = client.post(
        "/api/v1/providers/test", json={"base_url": url}
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert "禁止" in body["message"]
    assert elapsed < 1.0


def test_provider_delete(client, admin):
    created = _create(client, name="待删除").json()
    deleted = client.delete(f"/api/v1/providers/{created['id']}")
    assert deleted.status_code == 204
    missing = client.get(f"/api/v1/providers/{created['id']}")
    assert missing.status_code == 404
