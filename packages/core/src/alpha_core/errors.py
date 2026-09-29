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
    """所有 RPC 端点的计划额度都用完了（月度 CU、日请求上限），按厂商原文识别。

    和普通故障分开的原因：月度配额在下个计费周期之前重试多少次都没用，调用方应当立即
    停止、保存断点，等配额恢复后再继续，而不是原地重试把剩余时间耗光。它是
    `ChainAdapterError` 的子类，所以只捕获 `ChainAdapterError` 的现有调用方不受影响。
    短时限速（每秒请求数、每秒 CU）不属于这一类，见 `RpcRateLimitedError`。
    """


class RpcRateLimitedError(ChainAdapterError):
    """所有 RPC 端点都在限速（或部分限速、部分额度耗尽），并且在同一端点按提示时间退避重试后仍被拒绝。

    限速几秒到几十秒就会恢复（例如 Ankr 的 "call rate limit exhausted, retry in 10s"），和额度耗尽不同：
    调用方可以稍后重试同一批请求，不需要像额度耗尽那样暂停到下个计费周期。
    """


class SourceRateLimitedError(DataSourceUnavailableError):
    """外部 HTTP 数据源（地址索引源、ABI 来源、价格源）明确返回限流或配额耗尽。

    语义同 `RpcQuotaExhaustedError`：调用方应当暂停，而不是原地重试。
    """
