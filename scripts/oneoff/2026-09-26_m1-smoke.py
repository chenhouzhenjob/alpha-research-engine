"""M1 数据基建冒烟脚本（临时代码）。

用途：对真实 RPC 和外部接口跑一遍 M1 的各项能力，全部落库，并打印本次的额度账本。
连续运行两次，第二次的不可变数据（回执、字节码、区块时间、token 元数据、ABI、历史价格）
应当全部命中缓存，外部调用为 0。

执行：cd research && uv run python scripts/oneoff/2026-09-26_m1-smoke.py --tx <哈希> [--tx ...]
删除条件：M3 的回填任务实现后删除（届时由正式的 wallet_sync 覆盖这里的全部流程）。期限：2026-11-30。
不被任何应用、定时任务或默认测试路径 import。

地址索引源（M1 步骤 6b）等 M0 选型后再补进来。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from alpha_chains.bsc import build_bsc_adapter
from alpha_chains.erc20 import read_metadata_batch
from alpha_chains.multicall import get_eth_balances
from alpha_core.metering import InMemoryCallMeter
from alpha_core.ports import AbiKeyType
from alpha_core.types import Chain
from alpha_datasources.abi_sources import default_resolver
from alpha_datasources.geckoterminal import GeckoTerminalClient
from alpha_datasources.prices import CachingPriceFetcher, CoinGeckoPriceSource, GeckoTerminalPriceSource
from alpha_storage.db import session_scope
from alpha_storage.repositories.chain_txs import ChainTxRepository
from alpha_storage.repositories.external_call_ledger import ExternalCallLedgerRepository
from alpha_storage.repositories.tokens import TokenRepository
from alpha_storage.stores import DbAbiStore, DbBlockTimeStore, DbPriceStore
from dotenv import load_dotenv

WALLET = "0x05bbf9032f4c829e31a1f1b0b725d77329fad6be"
NPM = "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364"  # PancakeSwap V3 NonfungiblePositionManager
CAKE = "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def main() -> None:
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--tx", action="append", required=True, help="要取回执的交易哈希，可重复")
    args = parser.parse_args()

    run_ref = "smoke:" + datetime.now(UTC).strftime("%H%M%S")
    meter = InMemoryCallMeter(app="oneoff", job_ref=run_ref)
    adapter = build_bsc_adapter(meter=meter, block_time_store=DbBlockTimeStore())
    chain = Chain.BSC

    # 1. 回执：只取库里还没有的
    with session_scope() as s:
        missing = ChainTxRepository(s).list_missing_receipts(chain, args.tx)
    receipts = adapter.get_transaction_receipts(missing) if missing else None
    if receipts:
        with session_scope() as s:
            ChainTxRepository(s).save_receipts(chain, list(receipts.ok.values()))
    print(f"回执：需要拉取 {len(missing)} 笔，失败 {len(receipts.failed) if receipts else 0} 笔")

    # 2. 区块时间（走 block_times 持久化缓存）
    with session_scope() as s:
        logs = ChainTxRepository(s).get_logs(chain, args.tx)
    blocks = sorted({lg.block_number for items in logs.values() for lg in items})
    times = adapter.get_block_timestamps(blocks)
    print(f"区块时间：{len(times.ok)} 个，失败 {len(times.failed)} 个")

    # 3. token 元数据：只查库里没有的。token 只从 ERC20 Transfer 事件取（3 个 topic；4 个 topic 是 NFT），
    #    不能把所有发出日志的合约都当 token（池子、路由读不出 decimals，每次都会重查）。
    tokens = sorted(
        {lg.address for items in logs.values() for lg in items if lg.topics[:1] == [TRANSFER] and len(lg.topics) == 3}
        | {CAKE}
    )
    with session_scope() as s:
        known = TokenRepository(s).get_many(chain, tokens)
    unknown = [t for t in tokens if t not in known]
    if unknown:
        meta = read_metadata_batch(adapter, unknown)
        with session_scope() as s:
            repo = TokenRepository(s)
            for addr, m in meta.items():
                if m.decimals is not None:
                    repo.upsert_metadata(
                        chain, addr, decimals=m.decimals, symbol=m.symbol, name=m.name, standard="erc20", source="rpc"
                    )
    print(f"token 元数据：共 {len(tokens)} 个，本次查询 {len(unknown)} 个")

    # 4. 可变状态：BNB 余额（每次都会查，属于正常消耗）
    bal = get_eth_balances(adapter, [WALLET])
    print(f"BNB 余额：{bal.ok}")

    # 5. ABI：NPM 合约 + Transfer 事件签名
    resolver = default_resolver(store=DbAbiStore(), meter=meter)
    npm = resolver.resolve(chain, AbiKeyType.ADDRESS, NPM)
    sig = resolver.resolve(chain, AbiKeyType.EVENT, TRANSFER)
    print(f"ABI：NPM={npm.status}/{npm.name}，Transfer 事件={sig.status}/{sig.name}")

    # 6. 历史价格（已收盘的日桶会被缓存）
    fetcher = CachingPriceFetcher(
        [GeckoTerminalPriceSource(GeckoTerminalClient(meter=meter)), CoinGeckoPriceSource(meter=meter)],
        store=DbPriceStore(),
    )
    at = datetime(2026, 8, 27, 12, tzinfo=UTC)
    price = fetcher.get_price_at(chain, CAKE, at)
    print(f"CAKE 在 {at.date()} 的价格：{price.price_usd if price else None}（{price.source if price else '-'}）")

    # 7. 额度账本
    print(f"\n本次外部调用（job_ref={run_ref}）：")
    for key, total in sorted(meter.snapshot().items(), key=lambda kv: (kv[0].provider, kv[0].method)):
        print(
            f"  {key.provider:14s} {key.method:45s} {key.status.value:10s} n={total.call_count:<4d} CU={total.est_cu}"
        )
    with session_scope() as s:
        ExternalCallLedgerRepository(s).flush(meter)


if __name__ == "__main__":
    main()
