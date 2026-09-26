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


class RpcQuotaExhaustedError(ChainAdapterError):
    """所有 RPC 端点都返回配额耗尽或限流（HTTP 429，或 JSON-RPC 错误信息含 quota/limit）。

    和普通故障分开的原因：月度配额在下个计费周期之前重试多少次都没用，调用方应当立即
    停止、保存断点，等配额恢复后再继续，而不是原地重试把剩余时间耗光。它是
    `ChainAdapterError` 的子类，所以只捕获 `ChainAdapterError` 的现有调用方不受影响。
    """


class SourceRateLimitedError(DataSourceUnavailableError):
    """外部 HTTP 数据源（地址索引源、ABI 来源、价格源）明确返回限流或配额耗尽。

    语义同 `RpcQuotaExhaustedError`：调用方应当暂停，而不是原地重试。
    """
