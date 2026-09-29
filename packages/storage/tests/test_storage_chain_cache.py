"""M1 新增仓储的数据库测试（事务内执行，结束回滚）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from alpha_core.chain_data import RawLog, TxReceipt
from alpha_core.metering import CallStatus, InMemoryCallMeter
from alpha_core.ports import (
    AbiEntry,
    AbiKeyType,
    AbiStatus,
    BlockTimeSource,
    PriceConfidence,
    PriceGranularity,
    PricePoint,
)
from alpha_core.types import Chain
from alpha_storage.models import ChainTxRow
from alpha_storage.repositories.block_times import BlockTimeRepository
from alpha_storage.repositories.caches import AbiCacheRepository, ChainStateCacheRepository, PricePointRepository
from alpha_storage.repositories.chain_txs import ChainTxRepository, TxRecord
from alpha_storage.repositories.external_call_ledger import ExternalCallLedgerRepository
from alpha_storage.repositories.tokens import TokenRepository

TX = "0x" + "ab" * 32
TOKEN = "0x" + "12" * 20


def test_block_times_put_is_immutable(session):
    repo = BlockTimeRepository(session)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    repo.put_many(Chain.BSC, {10**9: t1}, BlockTimeSource.INDEXER)
    repo.put_many(Chain.BSC, {10**9: t1 + timedelta(hours=1), 10**9 + 1: t1}, BlockTimeSource.RPC)
    got = repo.get_many(Chain.BSC, [10**9, 10**9 + 1, 10**9 + 2])
    assert got == {10**9: t1, 10**9 + 1: t1}


def test_tokens_metadata_fills_nulls_but_keeps_decimals(session):
    repo = TokenRepository(session)
    repo.upsert(Chain.BSC, TOKEN, 18)  # 历史接口只写 decimals
    repo.upsert_metadata(Chain.BSC, TOKEN, decimals=6, symbol="ABC", name=None, standard="erc20", source="rpc")
    repo.upsert_metadata(Chain.BSC, TOKEN, decimals=6, symbol="XYZ", name="Abc Token", standard="erc20", source="rpc")
    rec = repo.get_many(Chain.BSC, [TOKEN.upper().replace("0X", "0x")])[TOKEN]
    assert (rec.decimals, rec.symbol, rec.name, rec.risk_flag) == (18, "ABC", "Abc Token", "normal")
    assert repo.get_decimals(Chain.BSC, TOKEN) == 18


def _receipt():
    logs = [
        RawLog(
            address=TOKEN,
            topics=["0x" + "dd" * 32, "0x" + "01" * 32],
            data="0x00",
            log_index=5,
            block_number=100,
            tx_hash=TX,
        ),
        RawLog(address=TOKEN, topics=[], data="0x", log_index=6, block_number=100, tx_hash=TX),
    ]
    return TxReceipt(
        tx_hash=TX,
        block_number=100,
        tx_index=3,
        from_address="0x" + "01" * 20,
        to_address=TOKEN,
        status=1,
        gas_used=21000,
        effective_gas_price=10**9,
        contract_address=None,
        logs=logs,
    )


def test_indexer_tx_then_receipt_fills_fields_and_logs(session):
    repo = ChainTxRepository(session)
    repo.upsert_txs(
        Chain.BSC,
        [
            TxRecord(
                tx_hash=TX,
                block_number=100,
                from_address="0x" + "01" * 20,
                to_address=TOKEN,
                value_raw=10**20,
                method_selector="0xa9059cbb",
            )
        ],
        source="indexer",
    )
    assert repo.list_missing_receipts(Chain.BSC, [TX, "0x" + "cd" * 32]) == [TX, "0x" + "cd" * 32]
    repo.save_receipts(Chain.BSC, [_receipt()])
    repo.save_receipts(Chain.BSC, [_receipt()])  # 重复写入幂等
    assert repo.list_missing_receipts(Chain.BSC, [TX]) == []
    logs = repo.get_logs(Chain.BSC, [TX])[TX]
    assert [lg.log_index for lg in logs] == [5, 6]
    assert logs[0].topics == ["0x" + "dd" * 32, "0x" + "01" * 32]
    assert logs[1].topics == []


def test_l2_fields_and_input_round_trip(session):
    """input_data / tx_type / mint_raw 来自索引源或交易本身，l1_fee 来自回执，都能写入并互相补齐。"""
    repo = ChainTxRepository(session)
    repo.upsert_txs(
        Chain.BASE,
        [TxRecord(tx_hash=TX, block_number=100, from_address="0x" + "01" * 20, to_address=TOKEN)],
        source="indexer",
    )
    repo.upsert_txs(
        Chain.BASE,
        [
            TxRecord(
                tx_hash=TX,
                block_number=100,
                from_address="0x" + "01" * 20,
                to_address=TOKEN,
                input_data="0xA9059CBB00",
                tx_type=0x7E,
                mint_raw=10**18,
            )
        ],
        source="rpc",
    )
    repo.save_receipts(Chain.BASE, [replace(_receipt(), l1_fee=123)])
    row = session.get(ChainTxRow, ("base", TX))
    got = (row.input_data, row.tx_type, row.mint_raw, row.l1_fee)
    assert got == ("0xa9059cbb00", 126, Decimal(10**18), Decimal(123))


def test_abi_cache_overwrites_expired_negative_entry(session):
    repo = AbiCacheRepository(session)
    now = datetime.now(UTC)
    key = "0x" + "ef" * 20
    repo.put(
        AbiEntry("bsc", AbiKeyType.ADDRESS, key, AbiStatus.NOT_FOUND, None, None, None, now, now + timedelta(days=7))
    )
    assert repo.get("bsc", AbiKeyType.ADDRESS, key.upper().replace("0X", "0x")).status == AbiStatus.NOT_FOUND
    repo.put(
        AbiEntry(
            "bsc", AbiKeyType.ADDRESS, key, AbiStatus.SUCCESS, "sourcify", "Vault", [{"type": "function"}], now, None
        )
    )
    got = repo.get("bsc", AbiKeyType.ADDRESS, key)
    assert (got.status, got.name, got.retry_after) == (AbiStatus.SUCCESS, "Vault", None)


def test_price_points_keep_first_value(session):
    repo = PricePointRepository(session)
    bucket = datetime(2026, 9, 1, tzinfo=UTC)
    p = PricePoint(
        "bsc", TOKEN, PriceGranularity.DAY, bucket, Decimal("1.2345"), "geckoterminal", "0xpool", PriceConfidence.HIGH
    )
    repo.put(p)
    repo.put(
        PricePoint("bsc", TOKEN, PriceGranularity.DAY, bucket, Decimal("9"), "coingecko", None, PriceConfidence.LOW)
    )
    got = repo.get("bsc", TOKEN, PriceGranularity.DAY, bucket)
    assert (got.price_usd, got.source) == (Decimal("1.2345"), "geckoterminal")


def test_state_cache_respects_max_age(session):
    repo = ChainStateCacheRepository(session)
    repo.put("bsc", "balance:x:y", {"raw": "100"}, block_number=5)
    assert repo.get("bsc", "balance:x:y", max_age_seconds=60).value == {"raw": "100"}
    assert repo.get("bsc", "balance:x:y", max_age_seconds=-1) is None


def test_ledger_flush_accumulates_and_marks_unknown_cu(session):
    repo = ExternalCallLedgerRepository(session)
    meter = InMemoryCallMeter(app="m1-test", job_ref="job:t")
    meter.record("nodereal", "eth_getTransactionReceipt", count=10, cu=150)
    meter.record("nodereal", "eth_foo", cu=None)
    assert repo.flush(meter) == 2
    meter.record("nodereal", "eth_getTransactionReceipt", count=2, cu=30, status=CallStatus.OK)
    repo.flush(meter)
    lines = {(ln.method, ln.status): ln for ln in repo.summarize(since=date.today() - timedelta(days=1), app="m1-test")}
    assert (
        lines[("eth_getTransactionReceipt", "ok")].call_count,
        lines[("eth_getTransactionReceipt", "ok")].est_cu,
    ) == (12, 180)
    assert lines[("eth_foo", "ok")].est_cu is None
    assert meter.snapshot() == {}


def test_contract_registry_upsert_keeps_confirmed(session):
    from alpha_core.ports import ContractRecord, ReviewStatus
    from alpha_storage.repositories.contract_registry import ContractRegistryRepository

    repo = ContractRegistryRepository(session)
    pool = "0x" + "ab" * 20
    human = "0x" + "cd" * 20
    repo.upsert_many(
        [
            ContractRecord(
                "bsc",
                pool.upper().replace("0X", "0x"),
                "pool",
                "create2",
                "uniswap_v3_like",
                "pancakeswap-v3",
                evidence={"fee": 500},
            ),
            ContractRecord(
                "bsc", human, "router", "manual", "dex_aggregator", "agg", review_status=ReviewStatus.CONFIRMED
            ),
        ]
    )
    # 自动流程再次写入：pool 被刷新，人工确认的 human 保持不变
    repo.upsert_many(
        [
            ContractRecord("bsc", pool, "pool", "known_table", "uniswap_v3_like", "pancakeswap-v3"),
            ContractRecord("bsc", human, "unknown", "code"),
        ]
    )
    got = repo.get_many("bsc", [pool, human, "0x" + "ef" * 20])
    assert set(got) == {pool, human}
    assert (got[pool].source, got[pool].evidence) == ("known_table", {})
    assert (got[human].kind, got[human].review_status) == ("router", ReviewStatus.CONFIRMED)
