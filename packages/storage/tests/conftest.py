"""存储层测试夹具：连本地 research 库，每个测试在一个事务里执行，结束时回滚，不留数据。

没配置 `RESEARCH_DATABASE_URL`（或库连不上）时整组跳过，不影响没有本地 Postgres 的环境。
需要先 `alembic upgrade head`。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

load_dotenv(Path(__file__).resolve().parents[3] / ".env")


@pytest.fixture(scope="session")
def engine():
    url = os.environ.get("RESEARCH_DATABASE_URL")
    if not url:
        pytest.skip("未配置 RESEARCH_DATABASE_URL，跳过数据库测试")
    eng = create_engine(url)
    try:
        with eng.connect():
            pass
    except OperationalError:
        pytest.skip("research 数据库连不上，跳过数据库测试")
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    trans = conn.begin()
    s = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield s
    finally:
        s.close()
        trans.rollback()
        conn.close()
