"""数据库引擎与会话工厂。连接串统一从 `RESEARCH_DATABASE_URL` 读取。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL_ENV = "RESEARCH_DATABASE_URL"

_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None


def get_engine() -> Engine:
    """惰性初始化并返回全局 Engine（进程内单例，避免重复建连接池）。"""
    global _engine
    if _engine is None:
        url = os.environ.get(DATABASE_URL_ENV)
        if not url:
            raise ValueError(f"环境变量 {DATABASE_URL_ENV} 未配置")
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def _session_factory() -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """提供一个事务边界：正常退出提交，异常退出回滚。"""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
