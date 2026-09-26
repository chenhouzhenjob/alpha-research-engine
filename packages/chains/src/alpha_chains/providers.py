"""RPC 供应商识别与计费单价（CU）。

计费单价用于两件事：额度账本估算消耗（`CallMeter`），以及 CU 令牌桶限速（`rate_limit`）。
单价来自 NodeReal 官方文档（2026-09 查询，见钱包链上行为分析设计方案 5.1）；表里没有的方法
返回 None 并告警一次，由 M0 实测后补齐，不猜一个值。
"""

from __future__ import annotations

import logging
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
