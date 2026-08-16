"""手动注册人工核实过的候选池：直接以 `qualified` 状态 upsert 进 `pool_candidates`，
不走 `discover.py`/`qualify.py` 的自动发现 + 门槛判定流程，同时登记对应的 `instruments` 行。

适用场景：像"实时研究驱动决策系统"阶段 0 的试跑池子（BTC/USDT、QQQB/USDT）这种地址和参数
已经人工核实过、想直接开始采集数据的池子——不需要再跑一遍全量 Factory 扫描或白名单配对查询。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import click
from alpha_core.instrument_id import MarketType
from alpha_core.models import Instrument, PoolCandidate
from alpha_core.types import AssetClass, Chain, DexId, PoolCandidateStatus
from alpha_storage.db import session_scope
from alpha_storage.repositories.instruments import InstrumentRepository
from alpha_storage.repositories.pool_candidates import PoolCandidateRepository
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@click.command()
@click.option("--pool-address", required=True, help="池子合约地址")
@click.option("--token0", required=True, help="token0 地址（Factory 约定：字典序较小的一侧）")
@click.option("--token1", required=True, help="token1 地址")
@click.option("--fee-pips", required=True, type=int, help="费率，单位 1e-6（如 500 = 0.05%）")
@click.option("--tick-spacing", required=True, type=int, help="池子 tick 间距")
@click.option(
    "--asset-class",
    type=click.Choice([c.value for c in AssetClass]),
    default=AssetClass.CRYPTO_NATIVE.value,
    show_default=True,
)
@click.option(
    "--created-at",
    "created_at_str",
    default=None,
    help="池子真实创建日期 YYYY-MM-DD（人工核实过就填，不填则 AgePenalty 会一直标 unavailable）",
)
def main(
    pool_address: str,
    token0: str,
    token1: str,
    fee_pips: int,
    tick_spacing: int,
    asset_class: str,
    created_at_str: str | None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()

    chain = Chain.BSC
    dex_id = DexId.PANCAKESWAP_V3_BSC
    created_at = (
        datetime.combine(date.fromisoformat(created_at_str), datetime.min.time(), tzinfo=UTC)
        if created_at_str
        else None
    )

    candidate = PoolCandidate(
        chain=chain,
        dex_id=dex_id,
        pool_address=pool_address,
        token0_address=token0,
        token1_address=token1,
        fee_pips=fee_pips,
        tick_spacing=tick_spacing,
        created_at_block=0,  # 人工注册，不是事件扫描发现，沿用"未知创建区块"的既有哨兵值约定
        created_at=created_at,
        status=PoolCandidateStatus.QUALIFIED,
        asset_class=AssetClass(asset_class),
    )
    base, quote = sorted([token0.lower(), token1.lower()])
    instrument = Instrument(
        venue=dex_id.value,
        market_type=MarketType.DEX_POOL,
        base=base,
        quote=quote,
        settle=None,
        symbol_raw=pool_address.lower(),
        chain=chain,
        listed_at=created_at,
    )

    with session_scope() as session:
        PoolCandidateRepository(session).upsert_manual(candidate)
        InstrumentRepository(session).upsert(instrument)

    logger.info(
        "已注册 %s（asset_class=%s），instrument_id=%s",
        candidate.pool_address,
        asset_class,
        instrument.instrument_id,
    )


if __name__ == "__main__":
    main()
