"""统一异常定义。业务层应抛出这里的类型，而不是裸 `Exception`/`ValueError`。"""

from __future__ import annotations


class AlphaResearchError(Exception):
    """所有 research 工作区自定义异常的基类。"""


class DataSourceUnavailableError(AlphaResearchError):
    """外部数据源（GeckoTerminal/RPC 等）在重试后仍不可用。

    调用方应当把对应指标标记为 unavailable，而不是让异常静默变成 0 或往上抛崩溃整个批处理。
    """


class ChainAdapterError(AlphaResearchError):
    """链适配器（RPC 调用、日志解码等）出错。"""
