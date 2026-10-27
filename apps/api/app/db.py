"""数据库引擎与会话工厂。

同步 SQLAlchemy + psycopg3：M1 的写入路径简单，同步实现更利于事务边界
（outbox 与业务写入必须同事务）。异步 API 层可在线程池中调用。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings


def build_engine(settings: Settings | None = None) -> Engine:
    """按配置创建引擎；测试可传入自定义 settings。"""
    resolved = settings or get_settings()
    return create_engine(
        resolved.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """会话默认不自动 flush，写路径显式控制事务与顺序。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


engine: Engine = build_engine()
SessionLocal = build_session_factory(engine)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话，异常回滚。"""
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
