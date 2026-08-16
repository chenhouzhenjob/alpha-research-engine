"""PancakeSwap V3 官方 Subgraph（The Graph 去中心化网络）适配器：查逐池按天的
`totalValueLockedUSD`/`dailyVolumeUSD` 历史。

真实的历史 TVL/24h volume 序列——`geckoterminal.py` 只能拿到"当前值"（见其模块文档），
这个数据源直接查 The Graph 索引出来的按天快照，是这份代码库里第一个 TVL/volume
历史序列的来源。

**需要付费 API key**（The Graph 迁移到去中心化网络后查询按 GRT 计费，新账号有免费额度）——
跟 `geckoterminal.py`/`coingecko.py` 那种完全免费公开的数据源不同，这里的重试策略更保守，
失败重试是要花钱的，不是纯粹浪费时间。

**必须带浏览器风格的 `User-Agent`**：实测过 `requests`/`urllib` 默认 UA 会被 Cloudflare
拦下（403 `error code: 1010`），换成浏览器 UA 就正常了——不确定是 Cloudflare 单纯不喜欢
默认 UA 还是别的指纹检测，没有深究，反正带上这个头能用。

Schema 用的是 Messari 标准化 DeFi subgraph schema（不是经典 Uniswap 风格的
`Pool`/`PoolDayData`），字段名是 `LiquidityPool`/`LiquidityPoolDailySnapshot`，
`day`（Int，从 The Graph 返回的原始值等于 `timestamp // 86400`）不是常见的 `date`，
是现场用 `__type` introspection 查出来的实际 schema，不是照抄某个文档假设的字段名。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

import requests
from alpha_core.errors import DataSourceUnavailableError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# PancakeSwap V3 BSC 的 subgraph id（Messari 标准化 schema），来自 The Graph Explorer，
# 不是猜的——实测查过 _meta { block { number } } 确认过这个 id 真的指向 PancakeSwap V3 BSC。
PANCAKESWAP_V3_BSC_SUBGRAPH_ID = "78EUqzJmEVJsAKvWghn7qotf9LVGqcTQxJhT5z84ZmgJ"
GATEWAY_BASE_URL = "https://gateway.thegraph.com/api"

# 不用默认 UA，会被 Cloudflare 拦（见模块文档）。
_BROWSER_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class _RetryableHttpError(Exception):
    """标记可重试的 HTTP 状态码（限流/网关类错误），触发 tenacity 重试。"""


@dataclass(frozen=True)
class SubgraphDailySnapshot:
    """某个池子某一天的真实历史 TVL/volume（不是"当前值"）。"""

    day: date
    tvl_usd: float
    volume_24h_usd: float
    swap_count: int


class PancakeswapSubgraphClient:
    """查询 PancakeSwap V3 Subgraph 的客户端，需要 The Graph 的 API key（按查询计费，
    见模块文档）。
    """

    def __init__(self, api_key: str, session: requests.Session | None = None) -> None:
        self._url = f"{GATEWAY_BASE_URL}/{api_key}/subgraphs/id/{PANCAKESWAP_V3_BSC_SUBGRAPH_ID}"
        self._session = session or requests.Session()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),  # 查询按 GRT 计费，比免费数据源保守，不多次重试硬扛
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((_RetryableHttpError, requests.RequestException)),
    )
    def _query(self, query: str) -> dict:
        resp = self._session.post(
            self._url,
            json={"query": query},
            headers={"Content-Type": "application/json", "User-Agent": _BROWSER_USER_AGENT},
            timeout=20,
        )
        if resp.status_code in _RETRYABLE_STATUS_CODES:
            raise _RetryableHttpError(f"POST subgraph -> {resp.status_code}")
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            raise DataSourceUnavailableError(f"subgraph 查询报错: {payload['errors']}")
        return payload["data"]

    def get_daily_tvl_volume(self, pool_address: str, *, days: int) -> list[SubgraphDailySnapshot]:
        """按天拉取该池子最近 `days` 天的真实 TVL/24h volume 历史，按日期升序返回。

        @param pool_address 池子地址（不区分大小写，subgraph 里存的是小写）
        @param days 最多拉取的天数（池子上线不足这么久，返回值会更短，不是错误）
        """
        query = (
            "{ liquidityPoolDailySnapshots("
            f'first: {days}, orderBy: day, orderDirection: desc, '
            f'where: {{pool: "{pool_address.lower()}"}}'
            ") { day totalValueLockedUSD dailyVolumeUSD dailySwapCount } }"
        )
        try:
            data = self._query(query)
        except DataSourceUnavailableError:
            logger.warning("subgraph 该池子无历史 TVL/volume 数据: %s", pool_address)
            return []
        rows = data.get("liquidityPoolDailySnapshots", [])
        points = [
            SubgraphDailySnapshot(
                day=datetime.fromtimestamp(row["day"] * 86400, tz=UTC).date(),
                tvl_usd=float(row["totalValueLockedUSD"]),
                volume_24h_usd=float(row["dailyVolumeUSD"]),
                swap_count=int(row["dailySwapCount"]),
            )
            for row in rows
        ]
        return sorted(points, key=lambda p: p.day)
