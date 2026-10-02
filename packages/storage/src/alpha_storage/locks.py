"""Postgres advisory lock 辅助函数：同步任务执行期间按钱包互斥。

为什么用会话级 advisory lock：锁挂在一条专用连接上，进程崩溃或被杀时连接断开、锁自动释放，
不会留下需要人工清理的"僵尸锁"；而任务表的部分唯一索引只能防止重复建任务，防不住两个进程
同时 resume 同一个任务。两者配合使用（M3 实施规划 5.3）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, func, select

from .db import get_engine


def wallet_lock_key(chain: str, address: str) -> int:
    """钱包同步锁的键：稳定哈希成 64 位有符号整数（pg_advisory_lock 的参数类型是 bigint）。"""
    digest = hashlib.blake2b(f"wallet-sync:{chain}:{address.lower()}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def try_advisory_lock(key: int, *, engine: Engine | None = None) -> Iterator[bool]:
    """尝试在一条专用连接上拿会话级 advisory lock，不等待。

    返回（yield）是否拿到锁；拿到时在退出上下文时释放并归还连接。拿不到时调用方应报"已有任务在跑"。
    锁和业务事务用不同的连接，业务事务提交或回滚都不影响锁。
    """
    conn = (engine or get_engine()).connect()
    try:
        got = bool(conn.scalar(select(func.pg_try_advisory_lock(key))))
        conn.commit()  # 结束 autobegin 的事务；会话级锁不随事务结束释放
        try:
            yield got
        finally:
            if got:
                conn.scalar(select(func.pg_advisory_unlock(key)))
                conn.commit()
    finally:
        conn.close()
