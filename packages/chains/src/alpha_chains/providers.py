"""RPC 供应商识别、计费单价（CU），以及"限速还是额度耗尽"的判断。

计费单价用于两件事：额度账本估算消耗（`CallMeter`），以及 CU 令牌桶限速（`rate_limit`）。
单价来自 NodeReal 官方文档（2026-09 查询，见钱包链上行为分析设计方案 5.1）；表里没有的方法
返回 None 并告警一次，由 M0 实测后补齐，不猜一个值。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PROVIDER_NODEREAL = "nodereal"  # NodeReal MegaNode，按 CU 计费，有月度配额和每秒 CU 上限
PROVIDER_PUBLICNODE = "publicnode"  # Allnodes PublicNode 免费公共端点，不计费但无 SLA
PROVIDER_ANKR = "ankr"  # Ankr，计费口径未接入
PROVIDER_UNKNOWN = "unknown"  # 未识别的供应商，只计次数不估 CU

# NodeReal MegaNode 各方法单价（CU/次）。
NODEREAL_CU: dict[str, int] = {
    "eth_blockNumber": 5,
    "eth_call": 20,
    "eth_getLogs": 50,
    "eth_getTransactionReceipt": 15,
    "eth_getCode": 15,
    "eth_getStorageAt": 15,
    "eth_getTransactionCount": 25,
    "eth_getBlockByNumber": 15,
    "eth_subscribe": 10,
    "nr_getAssetTransfers": 250,
    "nr_getTokenHoldings": 300,
}

# 限速时对单价未知的方法按这个值预扣，宁可慢一点也不冲破每秒上限。
FALLBACK_CU_FOR_RATE_LIMIT = 20

_warned_unknown: set[tuple[str, str]] = set()


def detect_provider(url: str) -> str:
    """按 RPC URL 的域名识别供应商。"""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("nodereal.io"):
        return PROVIDER_NODEREAL
    if host.endswith("publicnode.com"):
        return PROVIDER_PUBLICNODE
    if host.endswith("ankr.com"):
        return PROVIDER_ANKR
    return PROVIDER_UNKNOWN


def cu_for(provider: str, method: str) -> int | None:
    """返回一次调用的计费单位；免费端点为 0；单价未知返回 None（只告警一次）。"""
    if provider == PROVIDER_PUBLICNODE:
        return 0
    if provider == PROVIDER_NODEREAL:
        cu = NODEREAL_CU.get(method)
        if cu is None and (provider, method) not in _warned_unknown:
            _warned_unknown.add((provider, method))
            logger.warning("NodeReal 方法 %s 的 CU 单价未知，账本中该方法的 CU 记为不完整", method)
        return cu
    return None


def cu_for_rate_limit(provider: str, method: str) -> int:
    """限速用的预扣值：单价未知时按保守值预扣。"""
    cu = cu_for(provider, method)
    return FALLBACK_CU_FOR_RATE_LIMIT if cu is None else cu


class LimitKind(StrEnum):
    """RPC 拒绝服务的两类原因：处理方式完全不同，不能混为一谈。"""

    RATE_LIMITED = "rate_limited"  # 短时限速（每秒请求数、每秒 CU、并发），几秒后恢复：同一端点退避重试
    QUOTA_EXHAUSTED = "quota_exhausted"  # 计划额度用完（月度 CU、日请求上限），下个周期才恢复：不重试，切换端点


@dataclass(frozen=True)
class LimitSignal:
    """一次被拒绝的分类结果。"""

    kind: LimitKind
    retry_after: float | None  # 厂商给出的建议等待秒数（响应原文或 Retry-After 头）；没给为 None


# 各厂商"计划额度用完"的原文（只认文档里的那一句；每秒吞吐、并发类的 429 不算）。与 alpha-lp
# packages/chain/src/rpc-vendor.ts 的 isPlanQuotaExhausted 保持一致（提交 e5d1997）。
_PLAN_QUOTA_PATTERNS: dict[str, re.Pattern[str]] = {
    PROVIDER_NODEREAL: re.compile(r"monthly quota limit|reached your monthly quota", re.IGNORECASE),
    PROVIDER_ANKR: re.compile(r"monthly quota|maximum allowed number of requests", re.IGNORECASE),
}
# 任何厂商的原文里出现 monthly：月度额度不可能几秒后恢复，一律按额度耗尽
_MONTHLY = re.compile(r"monthly", re.IGNORECASE)
# 被拒绝的迹象：HTTP 429 之外，200 / 4xx 响应里的错误原文出现这些词也算
_LIMIT_WORDS = re.compile(
    r"quota|rate.?limit|too many requests|exceeded the limit|limit exceeded|capacity", re.IGNORECASE
)
# 厂商原文里的建议等待时间，例如 Ankr 的 "retry in 10s"
_RETRY_IN = re.compile(r"retry in (\d+(?:\.\d+)?)\s*(ms|s)\b", re.IGNORECASE)


def classify_limit(
    provider: str, message: str, *, http_status: int | None = None, retry_after_header: str | None = None
) -> LimitSignal | None:
    """判断一次失败是不是限速或额度耗尽；都不是返回 None（交给普通错误处理）。

    @param provider 由 URL 识别出的厂商（`detect_provider`）
    @param message 响应原文（HTTP 正文或 JSON-RPC 错误信息）
    @param http_status HTTP 状态码；429 一定算被拒绝
    @param retry_after_header 响应头 Retry-After（秒数形式）；日期形式不解析
    """
    if http_status != 429 and not _LIMIT_WORDS.search(message):
        return None
    pattern = _PLAN_QUOTA_PATTERNS.get(provider)
    if provider != PROVIDER_PUBLICNODE and (
        (pattern is not None and pattern.search(message)) or _MONTHLY.search(message)
    ):
        return LimitSignal(LimitKind.QUOTA_EXHAUSTED, None)
    wait: float | None = None
    if m := _RETRY_IN.search(message):
        wait = float(m.group(1)) / (1000 if m.group(2).lower() == "ms" else 1)
    elif retry_after_header and retry_after_header.strip().replace(".", "", 1).isdigit():
        wait = float(retry_after_header.strip())
    return LimitSignal(LimitKind.RATE_LIMITED, wait)
