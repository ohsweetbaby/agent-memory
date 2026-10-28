"""演示数据种子。

用法::

    uv run python -m app.seed --workspace ws_demo

脚本直接写 Postgres 事实源（不经过写入 API、不调用模型），用于本地演示
与管理台空状态；重复执行会先清空同一 workspace 的演示数据，因此是幂等的。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import build_engine, build_session_factory
from app.models import (
    Agent,
    EndUser,
    Episode,
    Memory,
    ReviewItem,
    Workspace,
)
from app.models import Session as SessionModel
from app.models.base import utcnow

DEMO_AGENTS = ("Atlas 助手", "代码助手", "写作助手", "数据分析助手", "运维助手")
DEMO_USERS = (("demo_u1", "林舟"), ("demo_u2", "宋雨"), ("demo_u3", "陈默"))


@dataclass(frozen=True, slots=True)
class SeedSummary:
    workspace_id: str
    agents: int
    end_users: int
    sessions: int
    memories: int
    review_items: int
    episodes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _content(index: int) -> str:
    topics = (
        "用户偏好使用 Python 与 PostgreSQL，代码评审要求先写测试。",
        "用户对花生过敏，聚餐时需要避开含花生酱的菜。",
        "项目 Atlas 的记忆检索采用四路召回与 RRF 融合。",
        "用户习惯在每周一上午整理上周的决策记录。",
        "部署环境使用 Docker Compose，Qdrant 与 Neo4j 都是可重建索引。",
        "用户不喜欢过度设计的方案，优先选择能回滚的小步改动。",
        "团队约定提交信息使用中文，并附上验证命令与结果。",
        "用户希望管理台默认展示待审队列，而不是先看统计图。",
        "处理 Excel 转图表时，先确认列类型再选择图表类型。",
        "用户搬家后从北京迁到上海，旧地址只保留在历史中。",
    )
    return f"{topics[index % len(topics)]}（示例记录 {index:03d}）"


def seed_workspace(
    session: Session,
    workspace_id: str = "ws_demo",
    *,
    memory_count: int = 200,
    review_count: int = 20,
) -> SeedSummary:
    """写入一套可重复的演示数据；调用方负责提交事务。"""
    session.execute(delete(ReviewItem).where(ReviewItem.workspace_id == workspace_id))
    session.execute(delete(Memory).where(Memory.workspace_id == workspace_id))
    session.execute(
        delete(SessionModel).where(SessionModel.workspace_id == workspace_id)
    )
    session.execute(delete(Agent).where(Agent.workspace_id == workspace_id))
    session.execute(delete(EndUser).where(EndUser.workspace_id == workspace_id))
    session.execute(delete(Workspace).where(Workspace.id == workspace_id))
    session.flush()

    now = utcnow()
    session.add(Workspace(id=workspace_id, name="演示工作区"))
    session.flush()

    users: list[EndUser] = []
    for external_id, display_name in DEMO_USERS:
        user = EndUser(
            id=str(uuid4()),
            workspace_id=workspace_id,
            external_id=external_id,
            display_name=display_name,
        )
        users.append(user)
        session.add(user)

    agents: list[Agent] = []
    for name in DEMO_AGENTS:
        agent = Agent(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name=name,
            description=f"{name}的演示配置",
        )
        agents.append(agent)
        session.add(agent)
    session.flush()

    sessions: list[SessionModel] = []
    for offset, agent in enumerate(agents[:2]):
        record = SessionModel(
            id=str(uuid4()),
            workspace_id=workspace_id,
            agent_id=agent.id,
            end_user_id=users[offset % len(users)].id,
            started_at=now - timedelta(days=offset + 1),
            ended_at=now - timedelta(days=offset, hours=2),
            summary="演示会话：偏好确认与任务复盘",
            metadata_={"seed": True},
        )
        sessions.append(record)
        session.add(record)
    session.flush()

    scopes = (
        ("user", users[0].id),
        ("user", users[1].id),
        ("user", users[2].id),
        ("agent", agents[0].id),
        ("agent", agents[1].id),
        ("global", None),
        ("session", sessions[0].id),
    )
    types = ("semantic", "semantic", "semantic", "episodic", "procedural", "working")

    for index in range(memory_count):
        scope, scope_id = scopes[index % len(scopes)]
        memory_type = types[index % len(types)]
        if memory_type == "working" and scope != "session":
            scope, scope_id = "session", sessions[index % len(sessions)].id
        content = _content(index)
        session.add(
            Memory(
                id=str(uuid4()),
                workspace_id=workspace_id,
                scope=scope,
                scope_id=scope_id,
                type=memory_type,
                content=content,
                summary=content[:40],
                idempotency_key=f"seed:{index}",
                importance=round(0.35 + (index % 60) / 100.0, 2),
                confidence=0.9,
                stability=1.0,
                access_count=index % 4,
                version=1,
                last_reinforced_at=now - timedelta(days=index % 45),
                valid_from=now - timedelta(days=index % 90),
                status="active",
                source_type="message",
                source_id=f"seed:message:{index}",
                metadata_={
                    "source_quote": content[:120],
                    "tags": ["seed", memory_type],
                },
            )
        )

    session.flush()

    for index in range(review_count):
        content = f"待审候选 {index:02d}：用户可能希望把默认检索模式改为 balanced。"
        scope, scope_id = scopes[index % len(scopes)]
        session.add(
            ReviewItem(
                id=str(uuid4()),
                workspace_id=workspace_id,
                candidate={
                    "content": content,
                    "type": "semantic",
                    "importance": 0.55,
                    "confidence": 0.55,
                    "scope": scope,
                    "scope_id": scope_id,
                    "entities": [{"name": "balanced", "type": "concept"}],
                },
                evidence={"source_quote": content, "session_id": sessions[0].id},
                confidence=0.55,
                status="pending",
            )
        )

    episodes = 0
    for index in range(8):
        session.add(
            Episode(
                id=str(uuid4()),
                session_id=sessions[index % len(sessions)].id,
                title=f"演示事件 {index + 1}",
                summary="用户确认了偏好，并约定下次复盘时间。",
                started_at=now - timedelta(days=index + 1),
                ended_at=now - timedelta(days=index, hours=3),
                outcome="completed",
                metadata_={"seed": True},
            )
        )
        episodes += 1

    session.flush()
    return SeedSummary(
        workspace_id=workspace_id,
        agents=len(agents),
        end_users=len(users),
        sessions=len(sessions),
        memories=int(
            session.scalar(
                select(func.count(Memory.id)).where(
                    Memory.workspace_id == workspace_id
                )
            )
            or 0
        ),
        review_items=int(
            session.scalar(
                select(func.count(ReviewItem.id)).where(
                    ReviewItem.workspace_id == workspace_id
                )
            )
            or 0
        ),
        episodes=episodes,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.seed")
    parser.add_argument("--workspace", default="ws_demo")
    parser.add_argument("--memories", type=int, default=200)
    parser.add_argument("--review", type=int, default=20)
    args = parser.parse_args(argv)

    settings = Settings()
    engine = build_engine(settings)
    if not inspect(engine).has_table("memory"):
        print("数据库尚未迁移，请先执行：")
        print("  uv run alembic -c apps/api/alembic.ini upgrade head")
        return 2

    factory = build_session_factory(engine)
    try:
        with factory() as session:
            summary = seed_workspace(
                session,
                args.workspace,
                memory_count=args.memories,
                review_count=args.review,
            )
            session.commit()
    finally:
        engine.dispose()

    import json

    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
