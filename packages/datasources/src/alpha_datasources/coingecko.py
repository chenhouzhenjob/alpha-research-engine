"""CoinGecko Public API 适配器（简单代币美元价格）。

注意和 `geckoterminal.py` 是两个不同的产品/域名，不要混淆：GeckoTerminal 是池子/DEX 行情，
CoinGecko 是代币现货价格（`/simple/price`）。本期只用来查 CAKE/USD 价格，
供 CakeAPR 计算使用——这个价格数据源选择直接对齐 alpha-lp `packages/prices/src/oracle.ts`
的 `getCakeUsdQuote`（同样用 CoinGecko `pancakeswap-token` 这个 coin id），不是另起一套。
"""

from __future__ import annotations

import logging
import threading
import time

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"
RATE_LIMIT_CALLS_PER_MINUTE = 10  # 免费公开层限制较严，保守取值
_MIN_INTERVAL_SECONDS = 60.0 / RATE_LIMIT_CALLS_PER_MINUTE

COINGECKO_CAKE_ID = "pancakeswap-token"


class CoinGeckoClient:
    """CoinGecko 简单价格查询客户端，内置限流节流与重试。"""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last_call_at: float = 0.0

    def _throttle(self) -> None:
        with self._lock:
            wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call_at)
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def get_simple_price_usd(self, coin_id: str) -> float | None:
        """查询单个代币的现货 USD 价格；查不到（拼错 id、被下架等）返回 None，不抛异常。"""
        self._throttle()
        resp = self._session.get(
            f"{BASE_URL}/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        usd = payload.get(coin_id, {}).get("usd")
        if usd is None:
            logger.warning("CoinGecko 查不到该代币的 USD 价格: %s", coin_id)
            return None
        return float(usd)
