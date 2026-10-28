"""巩固任务：会话/夜间巩固、dry-run、幂等、回滚与任务 REST。"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.embeddings import FakeEmbedding
from app.errors import ConflictError
from app.jobs.runtime import Runtime, configure_runtime, reset_runtime
from app.models import (
    AuditLog,
    Entity,
    Episode,
    JobRun,
    Memory,
    MemoryEmbedding,
    MemoryEntity,
    MemoryLink,
    Session as SessionModel,
    Workspace,
)
from app.models.base import utcnow
from app.repositories.memory import MemoryRepository
from app.services.consolidation import (
    consolidate_nightly,
    consolidate_session,
    find_sessions_to_consolidate,
    rollback_run,
)

pytestmark = pytest.mark.integration


class NullIndex:
    """巩固测试不触发投影；占位以满足 Runtime 依赖。"""

    def upsert(self, *args: Any, **kwargs: Any) -> None:
        return None

    def delete(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def consolidation_runtime(fake_llm) -> Any:
    """巩固服务与抽取一样，从 Runtime 取 LLM；测试注入 FakeLLM。"""
    configure_runtime(
        Runtime(
            session_scope=lambda: nullcontext(None),
            embedding=FakeEmbedding(dim=8),
            qdrant=NullIndex(),
            neo4j=None,
            eager=True,
            llm=fake_llm,
        )
    )
    try:
        yield
    finally:
        reset_runtime()


def snapshot_counts(db: Session) -> dict[str, int]:
    """dry-run 不得改动任何事实源/任务行。"""
    return {
        "memory": db.query(Memory).count(),
        "episode": db.query(Episode).count(),
        "job_run": db.query(JobRun).count(),
    }


def _add_entity(db: Session, workspace_id: str, name: str) -> Entity:
    entity = Entity(workspace_id=workspace_id, name=name, type="topic")
    db.add(entity)
    db.flush()
    return entity


def _working_memory(
    db: Session,
    *,
    workspace_id: str,
    memory_id: str,
    content: str,
    entity: Entity | None = None,
    session_id: str = "s1",
) -> Memory:
    memory = Memory(
        id=memory_id,
        workspace_id=workspace_id,
        scope="session",
        scope_id=session_id,
        type="working",
        content=content,
        importance=0.4,
        confidence=0.6,
        status="active",
    )
    db.add(memory)
    db.flush()
    if entity is not None:
        db.add(MemoryEntity(memory_id=memory.id, entity_id=entity.id, role="topic"))
        db.flush()
    return memory


@pytest.fixture()
def session_with_working_memories(db_session: Session) -> dict[str, Any]:
    """会话 s1：三条同一实体的工作记忆，等待巩固为情景+语义。"""
    workspace = Workspace(name="巩固工作区")
    db_session.add(workspace)
    db_session.flush()

    started = utcnow() - timedelta(hours=1)
    session = SessionModel(
        id="s1",
        workspace_id=workspace.id,
        started_at=started,
        ended_at=started + timedelta(minutes=40),
    )
    db_session.add(session)
    db_session.flush()

    entity = _add_entity(db_session, workspace.id, "花生")
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-allergy-1",
        content="用户吃花生会过敏",
        entity=entity,
    )
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-allergy-2",
        content="用户对花生严重过敏",
        entity=entity,
    )
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-allergy-3",
        content="用户不能吃花生",
        entity=entity,
    )
    db_session.flush()
    return {
        "workspace_id": workspace.id,
        "session_id": "s1",
        "entity_id": entity.id,
        "memory_ids": ["w-allergy-1", "w-allergy-2", "w-allergy-3"],
    }


@pytest.fixture()
def mergeable_memories(db_session: Session, fake_llm) -> dict[str, Any]:
    """已有语义记忆与工作记忆共享实体，巩固时应合并而不是新建。"""
    workspace = Workspace(name="合并工作区")
    db_session.add(workspace)
    db_session.flush()

    started = utcnow() - timedelta(hours=1)
    session = SessionModel(
        id="s1",
        workspace_id=workspace.id,
        started_at=started,
        ended_at=started + timedelta(minutes=40),
    )
    db_session.add(session)
    db_session.flush()

    entity = _add_entity(db_session, workspace.id, "花生")
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-merge-1",
        content="用户吃花生会过敏",
        entity=entity,
    )
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-merge-2",
        content="用户对花生严重过敏",
        entity=entity,
    )
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-merge-3",
        content="用户不能吃花生",
        entity=entity,
    )

    existing = Memory(
        id="m-merged",
        workspace_id=workspace.id,
        scope="session",
        scope_id="s1",
        type="semantic",
        content="原始内容",
        importance=0.7,
        confidence=0.7,
        status="active",
    )
    db_session.add(existing)
    db_session.flush()
    db_session.add(
        MemoryEntity(memory_id=existing.id, entity_id=entity.id, role="topic")
    )
    db_session.flush()

    fake_llm.set_json(
        {"summary": "用户讨论了花生过敏", "facts": ["用户对花生过敏"]}
    )
    return {
        "workspace_id": workspace.id,
        "session_id": "s1",
        "entity_id": entity.id,
        "existing_id": existing.id,
    }


def test_session_consolidation_creates_episode_and_facts(
    db_session, session_with_working_memories, fake_llm
):
    fake_llm.set_json({"summary": "用户讨论了过敏问题", "facts": ["用户对花生过敏"]})
    diff = consolidate_session(db_session, "s1")
    assert diff.created_episodes == 1
    assert diff.created_memories >= 1
    assert db_session.query(Episode).count() == 1


def test_dry_run_changes_nothing(db_session, session_with_working_memories, fake_llm):
    fake_llm.set_json({"summary": "用户讨论了过敏问题", "facts": ["用户对花生过敏"]})
    before = snapshot_counts(db_session)
    diff = consolidate_session(db_session, "s1", dry_run=True)
    assert diff.created_memories >= 1
    assert snapshot_counts(db_session) == before
    assert diff.job_run_id is None
    assert fake_llm.calls == 1
    assert diff.skipped_actions == ()
    assert diff.actions and all(
        action["status"] == "planned" for action in diff.actions
    )


def test_consolidation_is_idempotent(db_session, session_with_working_memories, fake_llm):
    fake_llm.set_json({"summary": "用户讨论了过敏问题", "facts": ["用户对花生过敏"]})
    first = consolidate_session(db_session, "s1")
    second = consolidate_session(db_session, "s1")
    assert second.created_memories == 0 and first.job_run_id == second.job_run_id
    # 第二次调用不应再次请求 LLM，也不应重复创建情景/语义记忆。
    assert fake_llm.calls == 1
    assert db_session.query(Episode).count() == 1
    assert (
        db_session.query(Memory)
        .filter(Memory.metadata_["consolidation_run_id"].astext == first.job_run_id)
        .count()
        > 0
    )


def test_rollback_restores_previous_state(
    db_session, session_with_working_memories, fake_llm
):
    fake_llm.set_json({"summary": "用户讨论了过敏问题", "facts": ["用户对花生过敏"]})
    diff = consolidate_session(db_session, "s1")
    assert rollback_run(db_session, diff.job_run_id) == diff.created_memories
    assert (
        db_session.query(Memory)
        .filter(Memory.metadata_["consolidation_run_id"].astext == diff.job_run_id)
        .count()
        == 0
    )
    # 工作记忆恢复为 active，情景/语义记忆被软删。
    restored = db_session.get(Memory, "w-allergy-1")
    assert restored is not None and restored.status == "active"
    assert db_session.query(Episode).count() == 0


def test_rollback_restores_merged_memory_from_audit(
    db_session, mergeable_memories, fake_llm
):
    diff = consolidate_session(db_session, "s1")
    assert diff.merged_memories == 1
    merged = db_session.get(Memory, "m-merged")
    assert merged is not None and merged.content != "原始内容"

    rollback_run(db_session, diff.job_run_id)
    db_session.expire_all()
    assert db_session.query(Memory).filter_by(id="m-merged").one().content == "原始内容"


def test_rollback_after_multiple_merges_into_same_target(
    db_session, mergeable_memories, fake_llm
) -> None:
    """同一目标在一次运行内被多条事实 merge 时，回滚不得误判 409。"""
    fake_llm.set_json(
        {
            "summary": "用户讨论了花生过敏",
            "facts": ["用户对花生过敏", "用户不能吃花生"],
        }
    )
    diff = consolidate_session(db_session, "s1")
    assert diff.merged_memories == 2

    target = db_session.get(Memory, "m-merged")
    assert target is not None
    assert target.version > 1
    assert target.content != "原始内容"

    # 用最后一条审计的 after.version 校验、第一条审计的 before 恢复。
    assert rollback_run(db_session, diff.job_run_id) == diff.created_memories
    db_session.expire_all()
    restored = db_session.query(Memory).filter_by(id="m-merged").one()
    assert restored.content == "原始内容"
    assert restored.status == "active"
    assert "merge_evidence" not in (restored.metadata_ or {})


def test_rollback_orders_same_timestamp_audits_by_version(
    db_session, mergeable_memories, fake_llm
) -> None:
    """同一目标多次审计时间戳相同且 id 乱序时，仍按 version 找到最后状态。"""
    fake_llm.set_json(
        {
            "summary": "用户讨论了花生过敏",
            "facts": ["用户对花生过敏", "用户不能吃花生"],
        }
    )
    diff = consolidate_session(db_session, "s1")
    assert diff.merged_memories == 2

    job = db_session.get(JobRun, diff.job_run_id)
    assert job is not None and job.finished_at is not None
    fixed = job.finished_at - timedelta(milliseconds=1)
    audits = list(
        db_session.scalars(
            select(AuditLog).where(
                AuditLog.action == "memory.consolidated",
                AuditLog.target_id == "m-merged",
                AuditLog.after["consolidation_run_id"].astext == diff.job_run_id,
            )
        )
    )
    assert len(audits) == 2
    low = next(entry for entry in audits if entry.after["version"] == 2)
    high = next(entry for entry in audits if entry.after["version"] == 3)
    low.id = "ffffffff-ffff-7fff-8fff-ffffffffffff"
    high.id = "00000000-0000-7000-8000-000000000001"
    low.created_at = fixed
    high.created_at = fixed
    db_session.commit()

    assert rollback_run(db_session, diff.job_run_id) == diff.created_memories
    db_session.expire_all()
    restored = db_session.query(Memory).filter_by(id="m-merged").one()
    assert restored.content == "原始内容"
    assert restored.status == "active"


def test_rollback_restores_superseded_validity(
    db_session: Session, fake_llm
) -> None:
    workspace = Workspace(name="取代工作区")
    db_session.add(workspace)
    db_session.flush()
    started = utcnow() - timedelta(hours=1)
    db_session.add(
        SessionModel(
            id="s1",
            workspace_id=workspace.id,
            started_at=started,
            ended_at=started + timedelta(minutes=30),
        )
    )
    db_session.flush()
    entity = _add_entity(db_session, workspace.id, "用户")
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-loc-1",
        content="用户住在北京",
        entity=entity,
    )
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-loc-2",
        content="用户长期住在北京",
        entity=entity,
    )
    _working_memory(
        db_session,
        workspace_id=workspace.id,
        memory_id="w-loc-3",
        content="用户在北京工作",
        entity=entity,
    )
    old = Memory(
        id="m-old-location",
        workspace_id=workspace.id,
        scope="session",
        scope_id="s1",
        type="semantic",
        content="用户住在北京",
        importance=0.7,
        status="active",
    )
    db_session.add(old)
    db_session.flush()
    db_session.add(
        MemoryEntity(memory_id=old.id, entity_id=entity.id, role="topic")
    )
    db_session.flush()

    fake_llm.set_json(
        {"summary": "用户搬家了", "facts": ["用户搬到上海"]}
    )
    diff = consolidate_session(db_session, "s1")
    assert diff.superseded_memories == 1
    supersede_action = next(
        action for action in diff.actions if action["action"] == "supersede"
    )
    # 硬删除依赖 action 中的新记忆 id 去脱敏审计/任务快照。
    assert supersede_action["memory_id"]
    db_session.refresh(old)
    assert old.status == "superseded" and old.valid_to is not None

    rollback_run(db_session, diff.job_run_id)
    db_session.refresh(old)
    assert old.status == "active" and old.valid_to is None


def test_llm_failure_falls_back_to_template_summary(
    db_session, session_with_working_memories, fake_llm
) -> None:
    fake_llm.set_text("这不是 JSON")
    diff = consolidate_session(db_session, "s1")
    assert diff.created_episodes == 1
    assert diff.created_memories == 1
    episode = db_session.query(Episode).one()
    assert episode.summary and "工作记忆" in episode.summary


def test_nightly_requires_cluster_size_three(
    db_session: Session, fake_llm
) -> None:
    workspace = Workspace(name="夜间工作区")
    db_session.add(workspace)
    db_session.flush()
    entity = _add_entity(db_session, workspace.id, "咖啡")
    for index in range(3):
        memory = Memory(
            id=f"e-coffee-{index}",
            workspace_id=workspace.id,
            scope="session",
            scope_id=f"s-{index}",
            type="episodic",
            content=f"用户第 {index + 1} 次讨论手冲咖啡",
            status="active",
        )
        db_session.add(memory)
        db_session.flush()
        db_session.add(
            MemoryEntity(memory_id=memory.id, entity_id=entity.id, role="topic")
        )
    # 只有 2 条的簇不应处理。
    lonely_a = Memory(
        id="e-lonely-a",
        workspace_id=workspace.id,
        scope="session",
        scope_id="s-a",
        type="episodic",
        content="用户提到一次茶",
        status="active",
    )
    lonely_b = Memory(
        id="e-lonely-b",
        workspace_id=workspace.id,
        scope="session",
        scope_id="s-b",
        type="episodic",
        content="用户又提到茶",
        status="active",
    )
    db_session.add_all([lonely_a, lonely_b])
    db_session.flush()

    fake_llm.set_json(
        {"summary": "用户偏爱手冲咖啡", "facts": ["用户喜欢手冲咖啡"]}
    )
    start = utcnow() - timedelta(hours=24)
    end = utcnow() + timedelta(minutes=1)
    diff = consolidate_nightly(
        db_session, start, end, scope=workspace.id
    )

    assert diff.clusters == 1
    assert diff.created_memories == 1
    assert diff.skipped_actions == ()
    facts = (
        db_session.query(Memory)
        .filter(
            Memory.metadata_["consolidation_run_id"].astext == diff.job_run_id,
            Memory.type == "semantic",
        )
        .all()
    )
    assert [fact.content for fact in facts] == ["用户喜欢手冲咖啡"]
    links = db_session.scalars(
        select(MemoryLink).where(
            MemoryLink.evidence["run_id"].astext == diff.job_run_id
        )
    ).all()
    assert {link.to_id for link in links} == {
        "e-coffee-0",
        "e-coffee-1",
        "e-coffee-2",
    }

    replay = consolidate_nightly(db_session, start, end, scope=workspace.id)
    assert replay.created_memories == 0
    assert replay.job_run_id == diff.job_run_id

    assert rollback_run(db_session, diff.job_run_id) == diff.created_memories
    db_session.expire_all()
    assert (
        db_session.query(Memory)
        .filter(Memory.metadata_["consolidation_run_id"].astext == diff.job_run_id)
        .count()
        == 0
    )
    assert (
        db_session.query(MemoryLink)
        .filter(MemoryLink.evidence["run_id"].astext == diff.job_run_id)
        .count()
        == 0
    )
    source = db_session.get(Memory, "e-coffee-0")
    assert source is not None and source.status == "active"
    assert "nightly_consolidation_run_id" not in (source.metadata_ or {})


def test_nightly_clusters_by_embedding_similarity(
    db_session: Session, fake_llm
) -> None:
    workspace = Workspace(name="嵌入簇工作区")
    db_session.add(workspace)
    db_session.flush()
    for index in range(3):
        memory = Memory(
            id=f"e-vector-{index}",
            workspace_id=workspace.id,
            scope="user",
            scope_id="u-vector",
            type="episodic",
            content=f"用户第 {index + 1} 次讨论跑步",
            status="active",
        )
        db_session.add(memory)
        db_session.flush()
        db_session.add(
            MemoryEmbedding(
                memory_id=memory.id,
                model="fake:test",
                dim=3,
                vector=[1.0, 0.0, 0.0],
            )
        )
    # 与簇内向量正交，不能并入。
    db_session.add(
        Memory(
            id="e-vector-other",
            workspace_id=workspace.id,
            scope="user",
            scope_id="u-vector",
            type="episodic",
            content="用户讨论游泳",
            status="active",
        )
    )
    db_session.flush()
    db_session.add(
        MemoryEmbedding(
            memory_id="e-vector-other",
            model="fake:test",
            dim=3,
            vector=[0.0, 1.0, 0.0],
        )
    )
    db_session.flush()

    fake_llm.set_json({"summary": "用户坚持跑步", "facts": ["用户每周跑步"]})
    diff = consolidate_nightly(
        db_session,
        utcnow() - timedelta(hours=24),
        utcnow() + timedelta(minutes=1),
        scope=workspace.id,
    )
    assert diff.clusters == 1
    assert diff.created_memories == 1


def test_jobs_rest_run_dry_run_and_rollback(
    client, admin, workspace, session_factory, fake_llm
) -> None:
    with session_factory() as db:
        started = utcnow() - timedelta(hours=1)
        db.add(
            SessionModel(
                id="s1",
                workspace_id=workspace["id"],
                started_at=started,
                ended_at=started + timedelta(minutes=30),
            )
        )
        entity = Entity(workspace_id=workspace["id"], name="花生", type="topic")
        db.add(entity)
        db.flush()
        for index in range(3):
            memory = Memory(
                id=f"w-rest-{index}",
                workspace_id=workspace["id"],
                scope="session",
                scope_id="s1",
                type="working",
                content=f"用户讨论花生过敏第 {index} 次",
                status="active",
            )
            db.add(memory)
            db.flush()
            db.add(
                MemoryEntity(memory_id=memory.id, entity_id=entity.id, role="topic")
            )
        db.commit()

    fake_llm.set_json(
        {"summary": "用户讨论了花生过敏", "facts": ["用户对花生过敏"]}
    )

    preview = client.post(
        "/api/v1/jobs/run",
        json={
            "type": "consolidate_session",
            "params": {"session_id": "s1"},
            "dry_run": True,
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["job_run_id"] is None
    assert body["created_memories"] >= 1
    assert client.get("/api/v1/jobs").json()["items"] == []

    created = client.post(
        "/api/v1/jobs/run",
        json={
            "type": "consolidate_session",
            "params": {"session_id": "s1"},
            "dry_run": False,
        },
    )
    assert created.status_code == 200, created.text
    job_id = created.json()["job_run_id"]
    assert job_id

    listed = client.get("/api/v1/jobs").json()["items"]
    assert [item["id"] for item in listed] == [job_id]
    detail = client.get(f"/api/v1/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"

    events = client.get(f"/api/v1/jobs/{job_id}/events")
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event: snapshot" in events.text
    assert "event: done" in events.text

    rolled_back = client.post(f"/api/v1/jobs/{job_id}/rollback")
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["restored"] == created.json()["created_memories"]


def test_jobs_cross_workspace_is_not_found(
    client, admin, workspace, session_factory, fake_llm
) -> None:
    other = client.post("/api/v1/workspaces", json={"name": "另一个工作区"})
    assert other.status_code == 201, other.text
    other_id = other.json()["id"]
    with session_factory() as db:
        started = utcnow() - timedelta(hours=1)
        db.add(
            SessionModel(
                id="s-other",
                workspace_id=other_id,
                started_at=started,
                ended_at=started + timedelta(minutes=10),
            )
        )
        db.add(
            Memory(
                id="w-other",
                workspace_id=other_id,
                scope="session",
                scope_id="s-other",
                type="working",
                content="其他工作区的工作记忆",
                status="active",
            )
        )
        db.commit()

    key = client.post(
        "/api/v1/keys",
        json={
            "workspace_id": workspace["id"],
            "name": "jobs-reader",
            "scopes": ["read"],
        },
    ).json()["key"]

    response = client.get(
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []

    # 写入/回滚需要 admin scope；read key 直接 403（而不是泄露 404）。
    forbidden = client.post(
        "/api/v1/jobs/run",
        json={"type": "consolidate_session", "params": {"session_id": "s-other"}},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert forbidden.status_code == 403

    # 有 admin scope 的 Key 也只能看到/回滚自己 workspace 的任务（404）。
    admin_key = client.post(
        "/api/v1/keys",
        json={
            "workspace_id": workspace["id"],
            "name": "jobs-admin",
            "scopes": ["admin"],
        },
    ).json()["key"]
    fake_llm.set_json({"summary": "其他工作区摘要", "facts": []})
    other_job = client.post(
        "/api/v1/jobs/run",
        json={"type": "consolidate_session", "params": {"session_id": "s-other"}},
    )
    assert other_job.status_code == 200, other_job.text
    other_job_id = other_job.json()["job_run_id"]
    hidden = client.get(
        f"/api/v1/jobs/{other_job_id}",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert hidden.status_code == 404
    hidden_rollback = client.post(
        f"/api/v1/jobs/{other_job_id}/rollback",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert hidden_rollback.status_code == 404


def test_job_audit_trail(db_session, session_with_working_memories, fake_llm) -> None:
    fake_llm.set_json({"summary": "用户讨论了过敏问题", "facts": ["用户对花生过敏"]})
    diff = consolidate_session(db_session, "s1")
    actions = {
        row.action for row in db_session.scalars(select(AuditLog))
    }
    assert {"consolidation.started", "consolidation.completed"} <= actions
    rollback_run(db_session, diff.job_run_id)
    actions = {row.action for row in db_session.scalars(select(AuditLog))}
    assert {
        "consolidation.started",
        "consolidation.completed",
        "consolidation.rolled_back",
    } <= actions


def test_closed_and_idle_sessions_are_detected(db_session: Session) -> None:
    workspace = Workspace(name="触发工作区")
    db_session.add(workspace)
    db_session.flush()
    now = utcnow()

    def _session(session_id: str, *, ended: bool) -> None:
        db_session.add(
            SessionModel(
                id=session_id,
                workspace_id=workspace.id,
                started_at=now - timedelta(hours=3),
                ended_at=now - timedelta(minutes=5) if ended else None,
            )
        )
        memory = Memory(
            id=f"w-{session_id}",
            workspace_id=workspace.id,
            scope="session",
            scope_id=session_id,
            type="working",
            content=f"会话 {session_id} 的工作记忆",
            status="active",
        )
        db_session.add(memory)
        db_session.flush()
        memory.last_reinforced_at = (
            now - timedelta(minutes=45) if session_id != "s-active" else now
        )
        db_session.flush()

    _session("s-closed", ended=True)
    _session("s-idle", ended=False)
    _session("s-active", ended=False)

    assert set(find_sessions_to_consolidate(db_session, now=now)) == {
        "s-closed",
        "s-idle",
    }


def test_rollback_rejects_manual_edit_after_consolidation(
    db_session, session_with_working_memories, fake_llm
) -> None:
    """巩固后编辑过记忆：整次回滚 409，且不产生任何部分修改。"""
    fake_llm.set_json({"summary": "用户讨论了过敏问题", "facts": ["用户对花生过敏"]})
    diff = consolidate_session(db_session, "s1")
    fact = (
        db_session.query(Memory)
        .filter(
            Memory.metadata_["consolidation_run_id"].astext == diff.job_run_id,
            Memory.type == "semantic",
        )
        .one()
    )
    MemoryRepository(db_session).update(fact, content="用户只对花生轻微过敏")
    db_session.commit()

    with pytest.raises(ConflictError) as excinfo:
        rollback_run(db_session, diff.job_run_id)

    assert excinfo.value.resolved_code == "rollback_conflict"
    assert fact.id in excinfo.value.details["conflict_ids"]

    db_session.expire_all()
    # 数据保持回滚前状态：编辑保留、job 仍 completed、episode 未被删除。
    assert db_session.get(Memory, fact.id).content == "用户只对花生轻微过敏"
    assert db_session.get(JobRun, diff.job_run_id).status == "completed"
    assert db_session.query(Episode).count() == 1
    for memory_id in session_with_working_memories["memory_ids"]:
        assert db_session.get(Memory, memory_id).status == "archived"


def test_rollback_blocks_scanner_and_force_allows_rerun(
    db_session, session_with_working_memories, fake_llm
) -> None:
    fake_llm.set_json({"summary": "用户讨论了过敏问题", "facts": ["用户对花生过敏"]})
    diff = consolidate_session(db_session, "s1")
    now = utcnow()
    assert "s1" not in find_sessions_to_consolidate(db_session, now=now)

    rollback_run(db_session, diff.job_run_id)
    # 回滚恢复 active 工作记忆，但最新 job 是 rolled_back 且窗口无新活动。
    assert find_sessions_to_consolidate(db_session, now=now) == []

    replay = consolidate_session(db_session, "s1")
    assert replay.replayed is True
    assert replay.job_run_id == diff.job_run_id
    assert replay.created_memories == 0

    # 新活动（last_reinforced_at 晚于窗口）后，扫描器重新选中该会话。
    new_memory = Memory(
        id="w-new-activity",
        workspace_id=session_with_working_memories["workspace_id"],
        scope="session",
        scope_id="s1",
        type="working",
        content="用户补充：只是轻微不适",
        status="active",
    )
    db_session.add(new_memory)
    db_session.flush()
    new_memory.last_reinforced_at = now + timedelta(minutes=1)
    db_session.flush()
    later = now + timedelta(minutes=5)
    assert "s1" in find_sessions_to_consolidate(db_session, now=later)

    forced = consolidate_session(db_session, "s1", force=True)
    assert forced.replayed is False
    assert forced.job_run_id not in (None, diff.job_run_id)
    assert forced.created_episodes == 1


def test_diff_counts_actual_writes_and_records_skips(
    db_session, mergeable_memories, fake_llm, monkeypatch
) -> None:
    from app.services import consolidation as service

    monkeypatch.setattr(service, "_merge_memory", lambda *args, **kwargs: False)
    diff = consolidate_session(db_session, "s1")

    assert diff.merged_memories == 0
    assert diff.created_memories == 1  # 仅新建的 episodic 记忆
    merge_actions = [
        action for action in diff.actions if action["action"] == "merge"
    ]
    assert merge_actions and merge_actions[0]["status"] == "skipped"
    assert diff.skipped_actions
    assert diff.skipped_actions[0]["action"] == "merge"


def test_conflict_recall_uses_recent_facts_without_entities(
    db_session: Session, fake_llm
) -> None:
    workspace = Workspace(name="无实体冲突工作区")
    db_session.add(workspace)
    db_session.flush()
    started = utcnow() - timedelta(hours=1)
    db_session.add(
        SessionModel(
            id="s1",
            workspace_id=workspace.id,
            started_at=started,
            ended_at=started + timedelta(minutes=30),
        )
    )
    db_session.flush()
    for index in range(3):
        _working_memory(
            db_session,
            workspace_id=workspace.id,
            memory_id=f"w-conflict-{index}",
            content=f"用户提到准备搬到上海（第 {index} 次）",
        )
    old = Memory(
        id="m-old-city",
        workspace_id=workspace.id,
        scope="session",
        scope_id="s1",
        type="semantic",
        content="用户住在北京",
        importance=0.7,
        status="active",
    )
    db_session.add(old)
    db_session.flush()

    fake_llm.set_json({"summary": "用户搬家了", "facts": ["用户搬到上海"]})
    diff = consolidate_session(db_session, "s1")

    assert diff.superseded_memories == 1
    db_session.refresh(old)
    assert old.status == "superseded" and old.valid_to is not None


def test_weak_candidate_subject_mismatch_does_not_supersede(
    db_session: Session, fake_llm
) -> None:
    """弱候选主语不一致时不得触发 supersede（父母 vs 本人）。"""
    workspace = Workspace(name="主语过滤工作区")
    db_session.add(workspace)
    db_session.flush()
    started = utcnow() - timedelta(hours=1)
    db_session.add(
        SessionModel(
            id="s1",
            workspace_id=workspace.id,
            started_at=started,
            ended_at=started + timedelta(minutes=30),
        )
    )
    db_session.flush()
    for index in range(3):
        _working_memory(
            db_session,
            workspace_id=workspace.id,
            memory_id=f"w-subject-{index}",
            content=f"用户提到准备搬家（第 {index} 次）",
        )
    old = Memory(
        id="m-parents-city",
        workspace_id=workspace.id,
        scope="session",
        scope_id="s1",
        type="semantic",
        content="用户父母住在北京",
        importance=0.7,
        status="active",
    )
    db_session.add(old)
    db_session.flush()

    fake_llm.set_json({"summary": "用户搬家了", "facts": ["用户搬到上海"]})
    diff = consolidate_session(db_session, "s1")

    assert diff.superseded_memories == 0
    assert diff.created_memories == 2  # episodic + 新语义事实
    db_session.refresh(old)
    assert old.status == "active"
    assert old.content == "用户父母住在北京"


def test_unrelated_recent_fact_is_created_not_merged(
    db_session: Session, fake_llm
) -> None:
    """近期事实只用于冲突判定，不能把无关新事实合并进旧记忆。"""
    workspace = Workspace(name="弱候选工作区")
    db_session.add(workspace)
    db_session.flush()
    started = utcnow() - timedelta(hours=1)
    db_session.add(
        SessionModel(
            id="s1",
            workspace_id=workspace.id,
            started_at=started,
            ended_at=started + timedelta(minutes=30),
        )
    )
    db_session.flush()
    for index in range(3):
        _working_memory(
            db_session,
            workspace_id=workspace.id,
            memory_id=f"w-weak-{index}",
            content=f"用户提到喜欢咖啡（第 {index} 次）",
        )
    old = Memory(
        id="m-unrelated",
        workspace_id=workspace.id,
        scope="session",
        scope_id="s1",
        type="semantic",
        content="用户住在北京",
        importance=0.7,
        status="active",
    )
    db_session.add(old)
    db_session.flush()

    fake_llm.set_json({"summary": "用户喜欢咖啡", "facts": ["用户喜欢喝咖啡"]})
    diff = consolidate_session(db_session, "s1")

    assert diff.merged_memories == 0
    assert diff.created_memories == 2  # episodic + 新语义事实
    db_session.refresh(old)
    assert old.content == "用户住在北京"


def test_entity_clustering_never_crosses_workspace(
    db_session: Session, fake_llm
) -> None:
    ws_a = Workspace(name="实体簇 A")
    ws_b = Workspace(name="实体簇 B")
    db_session.add_all([ws_a, ws_b])
    db_session.flush()
    entity = Entity(workspace_id=ws_a.id, name="共享话题", type="topic")
    db_session.add(entity)
    db_session.flush()

    for index in range(3):
        memory = Memory(
            id=f"e-ws-a-{index}",
            workspace_id=ws_a.id,
            scope="user",
            scope_id="u-a",
            type="episodic",
            content=f"用户第 {index + 1} 次讨论共享话题",
            status="active",
        )
        db_session.add(memory)
        db_session.flush()
        db_session.add(
            MemoryEntity(memory_id=memory.id, entity_id=entity.id, role="topic")
        )
    # 脏数据：workspace B 的 episodic 被错误关联到 A 的实体。
    dirty = Memory(
        id="e-ws-b",
        workspace_id=ws_b.id,
        scope="user",
        scope_id="u-b",
        type="episodic",
        content="用户也讨论共享话题",
        status="active",
    )
    db_session.add(dirty)
    db_session.flush()
    db_session.add(
        MemoryEntity(memory_id=dirty.id, entity_id=entity.id, role="topic")
    )
    db_session.flush()

    fake_llm.set_json({"summary": "用户喜欢共享话题", "facts": ["用户喜欢共享话题"]})
    diff = consolidate_nightly(
        db_session,
        utcnow() - timedelta(hours=24),
        utcnow() + timedelta(minutes=1),
    )

    assert len(diff.job_run_ids) == 2
    facts = (
        db_session.query(Memory)
        .filter(
            Memory.metadata_["consolidation_run_id"].astext.in_(
                list(diff.job_run_ids)
            ),
            Memory.type == "semantic",
        )
        .all()
    )
    assert len(facts) == 1
    assert facts[0].workspace_id == ws_a.id
    links = db_session.scalars(
        select(MemoryLink).where(
            MemoryLink.evidence["run_id"].astext.in_(list(diff.job_run_ids))
        )
    ).all()
    assert {link.to_id for link in links} == {"e-ws-a-0", "e-ws-a-1", "e-ws-a-2"}


def test_nightly_builds_one_job_per_workspace_and_tenant_keys_are_scoped(
    client, admin, session_factory, fake_llm
) -> None:
    ws_a = client.post("/api/v1/workspaces", json={"name": "夜间 A"}).json()
    ws_b = client.post("/api/v1/workspaces", json={"name": "夜间 B"}).json()
    now = utcnow()
    with session_factory() as db:
        for workspace_id, tag in ((ws_a["id"], "a"), (ws_b["id"], "b")):
            entity = Entity(
                workspace_id=workspace_id, name=f"topic-{tag}", type="topic"
            )
            db.add(entity)
            db.flush()
            for index in range(3):
                memory = Memory(
                    id=f"e-{tag}-{index}",
                    workspace_id=workspace_id,
                    scope="session",
                    scope_id=f"s-{tag}-{index}",
                    type="episodic",
                    content=f"{tag} 用户讨论手冲咖啡 {index}",
                    status="active",
                )
                db.add(memory)
                db.flush()
                db.add(
                    MemoryEntity(
                        memory_id=memory.id, entity_id=entity.id, role="topic"
                    )
                )
        db.commit()

    fake_llm.set_json({"summary": "用户喜欢手冲咖啡", "facts": ["用户喜欢手冲咖啡"]})
    with session_factory() as db:
        diff = consolidate_nightly(
            db, now - timedelta(hours=24), now + timedelta(minutes=1)
        )
    assert len(diff.job_run_ids) == 2
    assert diff.job_run_id is None

    with session_factory() as db:
        jobs = {
            job.params["workspace_id"]: job
            for job in db.scalars(
                select(JobRun).where(JobRun.type == "consolidate_nightly")
            )
        }
        assert set(jobs) == {ws_a["id"], ws_b["id"]}
        for workspace_id, job in jobs.items():
            assert job.scope == workspace_id
            assert job.params["workspace_id"] == workspace_id
            assert "all" not in (job.params["workspace_id"], job.scope)
        job_a_id = jobs[ws_a["id"]].id
        job_b_id = jobs[ws_b["id"]].id

    def _key(workspace_id: str, name: str) -> str:
        response = client.post(
            "/api/v1/keys",
            json={
                "workspace_id": workspace_id,
                "name": name,
                "scopes": ["admin"],
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["key"]

    key_a = _key(ws_a["id"], "nightly-a")
    key_b = _key(ws_b["id"], "nightly-b")

    assert (
        client.get(
            f"/api/v1/jobs/{job_a_id}",
            headers={"Authorization": f"Bearer {key_a}"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/jobs/{job_b_id}",
            headers={"Authorization": f"Bearer {key_a}"},
        ).status_code
        == 404
    )
    rolled_back = client.post(
        f"/api/v1/jobs/{job_b_id}/rollback",
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert rolled_back.status_code == 200, rolled_back.text

    with session_factory() as db:
        assert db.get(JobRun, job_a_id).status == "completed"
        assert db.get(JobRun, job_b_id).status == "rolled_back"
        fact_a = (
            db.query(Memory)
            .filter(
                Memory.workspace_id == ws_a["id"],
                Memory.type == "semantic",
                Memory.status == "active",
            )
            .all()
        )
        assert len(fact_a) == 1
