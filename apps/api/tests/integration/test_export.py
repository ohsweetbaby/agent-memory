"""任务 16：工作区导出（JSON / Markdown）与导出任务下载。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.embeddings import FakeEmbedding
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime
from app.models import JobRun

pytestmark = pytest.mark.integration


class _UnusedQdrant:
    """导出任务不访问索引；提供最小接口避免构建真实客户端。"""

    collection = "unused"
    dim = 8

    def close(self) -> None:
        return None


@pytest.fixture()
def seeded_memories(client, admin, workspace, valid_write_payload) -> list[dict]:
    created: list[dict] = []
    for content, memory_type in (
        ("用户对花生过敏", "semantic"),
        ("用户喜欢喝绿茶", "semantic"),
        ("发布窗口在周四", "episodic"),
    ):
        response = client.post(
            "/api/v1/memories",
            json=valid_write_payload(
                content=content,
                type=memory_type,
                scope_id="u-export",
                dedupe=False,
            ),
        )
        assert response.status_code == 201, response.text
        created.append(response.json())
    return created


@pytest.fixture()
def export_runtime(session_factory):
    configure_runtime(
        Runtime(
            session_scope=session_factory,
            embedding=FakeEmbedding(dim=8),
            qdrant=_UnusedQdrant(),
            neo4j=None,
            eager=True,
        )
    )
    try:
        yield
    finally:
        reset_runtime()


@pytest.fixture()
def read_export(session_factory):
    def _read(job_id: str) -> str:
        with session_factory() as session:
            job = session.get(JobRun, job_id)
            assert job is not None
            assert job.status == "completed", job.error
            path = Path(job.result["path"])
            assert path.is_file()
            return path.read_text(encoding="utf-8")

    return _read


def test_export_contains_memories_and_audit(
    client,
    admin,
    seeded_memories,
    export_runtime,
    run_worker_once,
    read_export,
    workspace,
) -> None:
    response = client.post(
        "/api/v1/jobs/run", json={"type": "export", "params": {"format": "json"}}
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    run_worker_once()

    content = read_export(job_id)
    assert "memories" in content and "audit" in content
    payload = json.loads(content)
    assert payload["workspace"]["id"] == workspace["id"]
    assert len(payload["memories"]) == len(seeded_memories)
    assert payload["episodes"] == []
    assert payload["audit"]

    job = client.get(f"/api/v1/jobs/{job_id}")
    assert job.status_code == 200
    assert job.json()["result"]["format"] == "json"

    # 导出不包含 API Key 哈希、Provider 密文或明文密钥字段。
    lowered = content.lower()
    for forbidden in ("key_hash", "encrypted_secret", "api_key", "password"):
        assert forbidden not in lowered


def test_export_markdown_groups_by_type(
    client,
    admin,
    seeded_memories,
    export_runtime,
    run_worker_once,
    read_export,
    workspace,
) -> None:
    response = client.post(
        "/api/v1/jobs/run",
        json={
            "type": "export",
            "params": {"format": "md", "workspace_id": workspace["id"]},
        },
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    run_worker_once()

    content = read_export(job_id)
    assert "# " in content
    assert "semantic" in content and "episodic" in content
    assert "用户对花生过敏" in content


def test_export_download_returns_file(
    client,
    admin,
    seeded_memories,
    export_runtime,
    run_worker_once,
    workspace,
) -> None:
    job_id = client.post(
        "/api/v1/jobs/run",
        json={
            "type": "export",
            "params": {"format": "json", "workspace_id": workspace["id"]},
        },
    ).json()["job_id"]
    run_worker_once()

    response = client.get(f"/api/v1/export/{job_id}")
    assert response.status_code == 200, response.text
    assert "application/json" in response.headers["content-type"]
    assert "memories" in response.text
