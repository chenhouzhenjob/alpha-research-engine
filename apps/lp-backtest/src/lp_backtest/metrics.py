"""1b 验收标准的演示 CLI：对任意候选池、任意历史日期，输出完整一组指标值（JSON）。

对应 lp-backtest-设计方案-v1.md 第 4 章 1b 的验收标准："对任意候选池、任意历史日期，
能输出完整的一组指标值"。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import date

import click
from alpha_chains.bsc import build_bsc_adapter
from alpha_core.metrics import MetricValue
from alpha_core.types import Chain
from alpha_datasources.coingecko import COINGECKO_CAKE_ID, CoinGeckoClient
from alpha_metrics.assemble import assemble_daily_metrics
from alpha_protocols.plugins.pancakeswap_v3 import PancakeswapV3Plugin
from alpha_storage.db import session_scope
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from alpha_storage.repositories.pool_metrics import PoolMetricsRepository
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _serialize(value):
    """把 `PoolDailyMetrics`（及嵌套的 `MetricValue`/枚举）转成可 JSON 序列化的结构。"""
    if isinstance(value, MetricValue):
        return {
            "availability": value.availability.value,
            "value": _serialize(value.value),
            "reason": value.reason,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _serialize(v) for k, v in asdict(value).items()}
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    return value


@click.command()
@click.option("--pool-address", required=True, help="候选池地址（需已在 pool_candidates 里）")
@click.option("--date", "as_of_str", default=None, help="YYYY-MM-DD，默认今天")
def main(pool_address: str, as_of_str: str | None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    chain = Chain.BSC
    pool_address = pool_address.lower()
    as_of = date.fromisoformat(as_of_str) if as_of_str else date.today()

    with session_scope() as session:
        candidate = PoolCandidateRepository(session).get_by_address(chain, pool_address)
        if candidate is None:
            raise click.ClickException(f"pool_candidates 里找不到 {pool_address}，先跑 discover/seed-whitelist")
        history = PoolMetricsRepository(session).get_series_up_to(chain, pool_address, as_of)

    logger.info("已加载 %d 天历史快照（截至 %s）", len(history), as_of)

    adapter = build_bsc_adapter()
    plugin = PancakeswapV3Plugin()
    cake_usd_price = CoinGeckoClient().get_simple_price_usd(COINGECKO_CAKE_ID)

    metrics = assemble_daily_metrics(
        chain=chain,
        pool_address=pool_address,
        fee_pips=candidate.fee_pips,
        created_at=candidate.created_at,
        as_of=as_of,
        history=history,
        adapter=adapter,
        plugin=plugin,
        cake_usd_price=cake_usd_price,
    )
    print(json.dumps(_serialize(metrics), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
