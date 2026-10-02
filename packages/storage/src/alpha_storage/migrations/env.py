"""Alembic 迁移环境：连接串从 `RESEARCH_DATABASE_URL` 读取，与 db.py 保持一致。"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from alpha_storage.db import DATABASE_URL_ENV
from alpha_storage.models import Base
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 和 lp_backtest 的 CLI 入口保持一致：自动从 research/.env 加载，不用手动 export。
# python-dotenv 默认会从当前目录往上找 .env，在 packages/storage/ 下执行也能定位到 research/.env。
load_dotenv()

db_url = os.environ.get(DATABASE_URL_ENV)
if not db_url:
    raise ValueError(f"环境变量 {DATABASE_URL_ENV} 未配置，无法运行迁移")
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
