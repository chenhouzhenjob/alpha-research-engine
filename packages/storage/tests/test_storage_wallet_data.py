"""M3 钱包数据表仓储的测试：纯函数部分不需要数据库，其余在事务内执行、结束回滚。"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from alpha_core.types import Chain
from alpha_storage.locks import try_advisory_lock, wallet_lock_key
from alpha_storage.models import WALLET_JOB_ACTIVE_STATES_SQL
from alpha_storage.repositories.chain_txs import ChainTxRepository, TxRecord
from alpha_storage.repositories.wallet_decodes import (
    DecodePath,
    TxDecodeRecord,
    WalletDecodeRepository,
    WalletEventRecord,
)
from alpha_storage.repositories.wallet_sync_jobs import (
    ActiveJobExistsError,
    JobKind,
    JobState,
    SyncDepth,
    WalletSyncJobRepository,
)
from alpha_storage.repositories.wallet_transfers import (
    TransferDirection,
    TransferKind,
    TransferRecord,
    WalletTransferRepository,
)
from alpha_storage.repositories.wallets import (
    BlockRange,
    SyncLayer,
    WalletRepository,
    WalletSyncRangeRepository,
    uncovered,
)

CHAIN = "bsc"
WALLET = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
TOKEN = "0x" + "12" * 20


def _tx(n: int) -> str:
    return "0x" + f"{n:064x}"


# ---------------------------------------------------------------- 纯函数


@pytest.mark.parametrize(
    ("covered", "want", "expected"),
    [
        ([], (10, 20), [(10, 20)]),
        ([(10, 20)], (10, 20), []),
        ([(0, 12), (15, 16), (19, 30)], (10, 20), [(13, 14), (17, 18)]),
        ([(5, 9), (21, 25)], (10, 20), [(10, 20)]),
        ([(12, 14)], (10, 20), [(10, 11), (15, 20)]),
    ],
)
def test_uncovered(covered, want, expected):
    got = uncovered([BlockRange(*c) for c in covered], BlockRange(*want))
    assert got == [BlockRange(*e) for e in expected]


def test_active_states_match_partial_index():
    in_sql = set(re.findall(r"'([a-z_]+)'", WALLET_JOB_ACTIVE_STATES_SQL))
    assert in_sql == {s.value for s in JobState.active()}
    assert not (JobState.active() & JobState.terminal())


def test_wallet_lock_key_is_stable_and_case_insensitive():
    assert wallet_lock_key("bsc", WALLET.upper().replace("0X", "0x")) == wallet_lock_key("bsc", WALLET)
    assert wallet_lock_key("bsc", WALLET) != wallet_lock_key("base", WALLET)
    assert -(2**63) <= wallet_lock_key("bsc", WALLET) < 2**63


# ---------------------------------------------------------------- 数据库


def test_wallet_ensure_is_idempotent(session):
    repo = WalletRepository(session)
    repo.ensure(CHAIN, WALLET.upper().replace("0X", "0x"))
    repo.ensure(CHAIN, WALLET)
    assert repo.exists(CHAIN, WALLET)


def test_sync_ranges_merge_overlapping_and_adjacent(session):
    repo = WalletSyncRangeRepository(session)
    repo.add(CHAIN, WALLET, SyncLayer.TRANSFERS, BlockRange(100, 200), source="ankr")
    repo.add(CHAIN, WALLET, SyncLayer.TRANSFERS, BlockRange(300, 400), source="ankr")
    repo.add(CHAIN, WALLET, SyncLayer.INTERNAL, BlockRange(150, 350), source="nodereal")  # 另一层，互不影响
    assert repo.list(CHAIN, WALLET, SyncLayer.TRANSFERS) == [BlockRange(100, 200), BlockRange(300, 400)]
    assert repo.gaps(CHAIN, WALLET, SyncLayer.TRANSFERS, BlockRange(0, 500)) == [
        BlockRange(0, 99),
        BlockRange(201, 299),
        BlockRange(401, 500),
    ]
    merged = repo.add(CHAIN, WALLET, SyncLayer.TRANSFERS, BlockRange(201, 299), source="ankr")  # 两边都相邻
    assert merged == BlockRange(100, 400)
    assert repo.list(CHAIN, WALLET, SyncLayer.TRANSFERS) == [BlockRange(100, 400)]
    assert repo.add(CHAIN, WALLET, SyncLayer.TRANSFERS, BlockRange(50, 120), source="ankr") == BlockRange(50, 400)
    assert repo.list(CHAIN, WALLET, SyncLayer.INTERNAL) == [BlockRange(150, 350)]
    with pytest.raises(ValueError):
        repo.add(CHAIN, WALLET, SyncLayer.TRANSFERS, BlockRange(10, 9), source="ankr")


def _transfer(n: int, key: str = "tx", **kw) -> TransferRecord:
    base = dict(
        wallet_address=WALLET,
        tx_hash=_tx(n),
        transfer_key=key,
        kind=TransferKind.EXTERNAL,
        token_address=None,
        token_id=None,
        amount_raw=10**18,
        from_address=OTHER,
        to_address=WALLET,
        direction=TransferDirection.IN,
        block_number=1000 + n,
        source="ankr",
    )
    return TransferRecord(**{**base, **kw})


def test_transfers_insert_is_idempotent_and_chunked(session):
    repo = WalletTransferRepository(session)
    big = [
        _transfer(i, kind=TransferKind.ERC20, key=f"log:{i}", token_address=TOKEN, amount_raw=2**200)
        for i in range(2500)
    ]
    repo.insert_many(CHAIN, big)
    # 同主键第二次写入（换了来源和数量）被忽略：原始数据以先到的为准
    repo.insert_many(
        CHAIN, [_transfer(0, key="log:0", kind=TransferKind.ERC20, token_address=TOKEN, source="nodereal")]
    )
    got = repo.list_for_wallet(CHAIN, WALLET)
    assert len(got) == 2500
    assert got[0].amount_raw == 2**200 and got[0].source == "ankr"
    assert [t.block_number for t in repo.list_for_wallet(CHAIN, WALLET, from_block=1010, to_block=1012)] == [
        1010,
        1011,
        1012,
    ]
    assert len(repo.list_for_wallet(CHAIN, WALLET, tx_hashes=[_tx(5).upper().replace("0X", "0x")])) == 1


def _event(seq: int, **kw) -> WalletEventRecord:
    base = dict(
        seq=seq,
        event_type="transfer",
        event_subtype="none",
        direction="in",
        coverage_tier="T0",
        confidence="exact",
        decoder_version="generic@1",
        asset="native",
        amount_raw=10**18,
    )
    return WalletEventRecord(**{**base, **kw})


def _decode(n: int, block: int, **kw) -> TxDecodeRecord:
    base = dict(tx_hash=_tx(n), block_number=block, path=DecodePath.RECEIPT, succeeded=True, internal_available=False)
    return TxDecodeRecord(**{**base, **kw})


def test_save_tx_replaces_events_and_summary(session):
    repo = WalletDecodeRepository(session)
    repo.save_tx(
        CHAIN,
        WALLET,
        _decode(1, 500, decoder_versions={"generic": 1}, unknown_contracts=(OTHER.upper().replace("0X", "0x"),)),
        [_event(0), _event(1, extra={"liquidity": "123"}, claimed_flow_ids=(0, 1), position_key="bsc:x:nft:1")],
    )
    repo.save_tx(
        CHAIN,
        WALLET,
        _decode(1, 500, decoder_versions={"generic": 2}, warnings=({"code": "internal_unavailable", "detail": "x"},)),
        [_event(0, amount_raw=5)],
    )
    events = repo.list_events(CHAIN, WALLET)
    assert [(e.event.seq, e.event.amount_raw) for e in events] == [(0, 5)]
    [summary] = repo.list_decodes(CHAIN, WALLET)
    assert summary.decoder_versions == {"generic": 2}
    assert summary.unknown_contracts == ()
    assert summary.warnings == ({"code": "internal_unavailable", "detail": "x"},)
    # 另一个视角钱包的同一笔交易各存一份
    repo.save_tx(CHAIN, OTHER, _decode(1, 500), [_event(0, direction="out")])
    assert len(repo.list_events(CHAIN, WALLET)) == 1
    with pytest.raises(ValueError):
        repo.save_tx(CHAIN, WALLET, _decode(2, 501), [_event(0), _event(0)])


def test_list_events_follows_onchain_order(session):
    txs = ChainTxRepository(session)
    # 同一区块：tx_index 3 的交易哈希更大但排在前面；没有 tx_index 的排最后
    txs.upsert_txs(
        Chain.BSC,
        [
            TxRecord(tx_hash=_tx(9), block_number=700, from_address=WALLET, to_address=OTHER, tx_index=3),
            TxRecord(tx_hash=_tx(1), block_number=700, from_address=WALLET, to_address=OTHER, tx_index=8),
        ],
        source="indexer",
    )
    repo = WalletDecodeRepository(session)
    for n in (1, 9, 5):
        repo.save_tx(CHAIN, WALLET, _decode(n, 700), [_event(1), _event(0)])
    repo.save_tx(CHAIN, WALLET, _decode(7, 699), [_event(0)])
    order = [(e.tx_hash, e.event.seq) for e in repo.list_events(CHAIN, WALLET)]
    assert order == [
        (_tx(7), 0),
        (_tx(9), 0),
        (_tx(9), 1),
        (_tx(1), 0),
        (_tx(1), 1),
        (_tx(5), 0),
        (_tx(5), 1),
    ]
    assert [e.block_number for e in repo.list_events(CHAIN, WALLET, from_block=700)][0] == 700


def test_jobs_one_active_per_wallet(session):
    repo = WalletSyncJobRepository(session)
    job = repo.create(CHAIN, WALLET, kind=JobKind.BACKFILL, depth=SyncDepth.DECODED, budget_usd=Decimal("0.5"))
    assert job.state is JobState.ESTIMATING and job.estimated_usd is None and job.finished_at is None
    with pytest.raises(ActiveJobExistsError) as exc:
        repo.create(CHAIN, WALLET, kind=JobKind.REDECODE, depth=SyncDepth.DECODED, budget_usd=Decimal("0"))
    assert exc.value.job_id == job.id
    # 冲突在 savepoint 里回滚，外层事务仍可用；其他钱包不受影响
    repo.create(CHAIN, OTHER, kind=JobKind.BACKFILL, depth=SyncDepth.FULL, budget_usd=Decimal("1"))

    updated = repo.update(
        job.id, state=JobState.RUNNING, checkpoint={"phase": "receipts", "cursor": 120}, estimated_usd=Decimal("0.12")
    )
    assert updated.checkpoint == {"phase": "receipts", "cursor": 120}
    assert updated.estimated_usd == Decimal("0.1200")
    failed = repo.update(job.id, state=JobState.FAILED, error="boom")
    assert failed.error == "boom" and failed.finished_at is None  # failed 不是终态
    assert repo.update(job.id, state=JobState.RUNNING, error=None).error is None
    done = repo.update(job.id, state=JobState.DONE, used_usd=Decimal("0.1"))
    assert done.finished_at is not None and done.checkpoint == {"phase": "receipts", "cursor": 120}
    assert repo.get_active(CHAIN, WALLET) is None
    # 上一个任务结束后可以建新任务
    again = repo.create(CHAIN, WALLET, kind=JobKind.REDECODE, depth=SyncDepth.DECODED, budget_usd=Decimal("0"))
    assert repo.get_active(CHAIN, WALLET).id == again.id
    assert [j.id for j in repo.list_for_wallet(CHAIN, WALLET)] == [again.id, job.id]
    with pytest.raises(KeyError):
        repo.update(-1, state=JobState.DONE)


def test_advisory_lock_is_exclusive_and_released(engine):
    key = wallet_lock_key("test-chain", WALLET)
    with try_advisory_lock(key, engine=engine) as first:
        assert first
        with try_advisory_lock(key, engine=engine) as second:  # 另一条连接拿不到
            assert not second
    with try_advisory_lock(key, engine=engine) as again:  # 释放后能再拿到
        assert again
