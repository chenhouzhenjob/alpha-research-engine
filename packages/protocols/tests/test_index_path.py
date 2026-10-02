"""M3 步骤 4：索引路径解码、持仓类型声明、EIP-7702 委托地址识别。

索引路径的等价性：把金标准样本回执里的转账日志按地址索引源的形态还原成 `AddressTransfer`，
回执去掉日志后走索引路径，与回执路径（不跑家族解码）比较，钱包侧流水和资产事件必须逐条相同。
这证明两条路径共用的流水编号、兜底和风险标记没有分叉；索引源本身是否完整由步骤 1 的夹具测试负责。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from _golden import all_case_ids, load
from alpha_core.chain_data import TxReceipt
from alpha_core.ports import AddressTransfer, TransferKind
from alpha_core.types import Chain
from alpha_protocols.decoding.evm.flows import TRANSFER, TRANSFER_BATCH, TRANSFER_SINGLE
from alpha_protocols.decoding.evm.pipeline import decode_evm_transfers, decode_evm_tx
from alpha_protocols.decoding.models import (
    AssetFlowKind,
    EventSubtype,
    EventType,
    FlowSource,
    InternalTransfer,
    PositionCategory,
    PositionKind,
)
from alpha_protocols.families import FAMILIES
from alpha_protocols.identification.runner import eip7702_delegate, identify
from alpha_protocols.runtime import (
    internal_from_transfers,
    position_categories_for,
    position_category,
    valuers_for,
)
from eth_abi import decode as abi_decode

WALLET = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20


def _addr(topic: str) -> str:
    return "0x" + topic[-40:]


def _indexed_transfers(receipt: TxReceipt) -> list[AddressTransfer]:
    """模拟地址索引源：把回执里全部标准转账日志还原成 AddressTransfer（不限钱包，由解码按钱包过滤）。"""
    out: list[AddressTransfer] = []
    for log in receipt.logs:
        t = log.topics
        if not t or len(t) < 3:
            continue
        common = dict(tx_hash=receipt.tx_hash, block_number=receipt.block_number, token_address=log.address)
        if t[0] == TRANSFER and len(t) == 3 and len(log.data) >= 66:
            out.append(
                AddressTransfer(
                    kind=TransferKind.ERC20,
                    from_address=_addr(t[1]),
                    to_address=_addr(t[2]),
                    amount_raw=int(log.data[2:66], 16),
                    log_index=log.log_index,
                    **common,
                )
            )
        elif t[0] == TRANSFER and len(t) == 4:
            out.append(
                AddressTransfer(
                    kind=TransferKind.ERC721,
                    from_address=_addr(t[1]),
                    to_address=_addr(t[2]),
                    amount_raw=1,
                    token_id=int(t[3], 16),
                    log_index=log.log_index,
                    **common,
                )
            )
        elif t[0] in (TRANSFER_SINGLE, TRANSFER_BATCH) and len(t) == 4:
            data = bytes.fromhex(log.data[2:])
            if t[0] == TRANSFER_SINGLE:
                pairs = [tuple(abi_decode(["uint256", "uint256"], data))]
            else:
                ids, values = abi_decode(["uint256[]", "uint256[]"], data)
                pairs = list(zip(ids, values, strict=True))
            for i, (token_id, value) in enumerate(pairs):
                out.append(
                    AddressTransfer(
                        kind=TransferKind.ERC1155,
                        from_address=_addr(t[2]),
                        to_address=_addr(t[3]),
                        amount_raw=value,
                        token_id=token_id,
                        log_index=log.log_index,
                        batch_index=i,
                        **common,
                    )
                )
    return out


def _asset_events(decoded):
    """资产事件（去掉只有回执路径才有的授权、ABI 解码等信息事件），按原顺序比较、忽略 seq。

    seq 是交易内所有事件（含信息事件）的统一序号：回执路径里授权事件也占序号，索引路径没有授权，
    所以同一笔交易两条路径的 seq 可能错位。seq 只要求同一路径内稳定，一笔交易走哪条路径由规划器固定。
    """
    return [replace(e, seq=0) for e in decoded.events if e.event_type is not EventType.INFORMATIONAL]


@pytest.mark.parametrize("case_id", all_case_ids())
def test_index_path_matches_receipt_path(case_id):
    s = load(case_id)
    ctx = s.context()
    by_receipt = decode_evm_tx(s.tx, s.receipt, s.subject, ctx, s.rules)
    stripped = replace(s.receipt, logs=[])
    by_index = decode_evm_transfers(s.tx, stripped, _indexed_transfers(s.receipt), s.subject, ctx, s.rules)
    assert by_index.flows == by_receipt.flows
    assert _asset_events(by_index) == _asset_events(by_receipt)
    assert by_index.unclaimed_flow_ids == ()
    assert by_index.succeeded == by_receipt.succeeded


@pytest.mark.parametrize(
    ("case_id", "expected"),
    [
        ("bsc/generic/erc20_in", (EventType.TRANSFER, EventSubtype.NONE)),
        ("bsc/generic/zero_transfer_from", (EventType.RECEIVE, EventSubtype.SPAM)),
        ("bsc/generic/spam_nft", (EventType.RECEIVE, EventSubtype.SPAM)),
    ],
)
def test_index_path_third_party_cases(case_id, expected):
    """第三方发起的交易（M3 走索引路径的主要对象）：分类和回执路径一致，且没有 gas（不是钱包付的）。"""
    s = load(case_id)
    decoded = decode_evm_transfers(
        s.tx, replace(s.receipt, logs=[]), _indexed_transfers(s.receipt), s.subject, s.context(), s.rules
    )
    assert expected in {(e.event_type, e.event_subtype) for e in decoded.events}
    assert all(e.event_type is not EventType.FEE for e in decoded.events)


def test_index_path_internal_transfers_and_native_entries_ignored():
    """内部转账通过 internal 传入；索引结果里的原生币条目不重复计入（交易 value 取自 tx）。"""
    s = load("bsc/dex_aggregator/to_native")
    received = 10**17
    router = s.tx.to_address
    entries = [
        *_indexed_transfers(s.receipt),
        AddressTransfer(
            tx_hash=s.tx.tx_hash,
            block_number=s.receipt.block_number,
            kind=TransferKind.INTERNAL,
            from_address=router,
            to_address=s.subject,
            amount_raw=received,
            trace_id="0_1",
        ),
        AddressTransfer(  # 索引源给的交易本身（value）条目：应被忽略
            tx_hash=s.tx.tx_hash,
            block_number=s.receipt.block_number,
            kind=TransferKind.EXTERNAL,
            from_address=s.subject,
            to_address=router,
            amount_raw=s.tx.value,
        ),
    ]
    internal = internal_from_transfers(entries)
    assert internal == [InternalTransfer(router, s.subject, received)]
    decoded = decode_evm_transfers(
        s.tx, replace(s.receipt, logs=[]), entries, s.subject, s.context(), s.rules, internal=internal
    )
    native = [f for f in decoded.flows if f.kind is AssetFlowKind.NATIVE]
    assert [(f.source, f.amount_raw) for f in native if f.source is FlowSource.INTERNAL] == [
        (FlowSource.INTERNAL, received)
    ]
    assert sum(1 for f in native if f.source is FlowSource.TX) == (1 if s.tx.value else 0)


def test_index_path_rejects_token_transfer_without_log_index():
    s = load("bsc/generic/erc20_in")
    bad = [replace(t, log_index=None) for t in _indexed_transfers(s.receipt)]
    with pytest.raises(ValueError):
        decode_evm_transfers(s.tx, replace(s.receipt, logs=[]), bad, s.subject, s.context(), s.rules)


@pytest.mark.parametrize(
    ("transfer", "key"),
    [
        (dict(kind=TransferKind.EXTERNAL), "tx"),
        (dict(kind=TransferKind.ERC20, log_index=7), "log:7"),
        (dict(kind=TransferKind.ERC721, log_index=7, token_id=5), "log:7"),
        (dict(kind=TransferKind.ERC1155, log_index=7, token_id=5, batch_index=2), "log:7:2"),
        (dict(kind=TransferKind.INTERNAL, trace_id="0_3_1"), "internal:0_3_1"),
    ],
)
def test_transfer_key(transfer, key):
    base = dict(tx_hash="0x01", block_number=1, from_address=OTHER, to_address=WALLET, amount_raw=1)
    assert AddressTransfer(**{**base, **transfer}).transfer_key == key


@pytest.mark.parametrize("kind", [TransferKind.ERC20, TransferKind.INTERNAL])
def test_transfer_key_requires_locator(kind):
    t = AddressTransfer(tx_hash="0x01", block_number=1, kind=kind, from_address=OTHER, to_address=WALLET, amount_raw=1)
    with pytest.raises(ValueError):
        _ = t.transfer_key


# ---------------------------------------------------------------- 持仓类型


def test_position_categories_on_bsc():
    cats = position_categories_for(Chain.BSC)
    assert cats["pancakeswap-v3"] == {PositionKind.NFT: PositionCategory.CONCENTRATED_LP}
    assert cats["pancakeswap-v2"] == {PositionKind.SHARE: PositionCategory.FUNGIBLE_LP}
    assert cats["venus-core"][PositionKind.DEBT] is PositionCategory.LENDING_DEBT
    assert dict(cats["wrapped-native"]) == {}
    assert position_category("bsc:venus-core:share:0xfd5840cd36d94d7229439859c0112a4185bc0255", cats) is (
        PositionCategory.LENDING_SUPPLY
    )
    assert position_category("bsc:unknown-instance:nft:1", cats) is None
    with pytest.raises(ValueError):
        position_category("bad-key", cats)


@pytest.mark.parametrize("chain", list(Chain))
def test_every_valuable_kind_has_a_category(chain):
    """家族能估值的持仓形态都必须声明持仓类型，否则分析层会把可估值的持仓当成未分类。"""
    cats = position_categories_for(chain)
    for instance_key, valuer in valuers_for(chain).items():
        assert set(valuer.position_kinds) <= set(cats[instance_key]), instance_key


def test_position_categories_are_immutable():
    for family in FAMILIES.values():
        with pytest.raises(TypeError):
            family.position_categories[PositionKind.NFT] = PositionCategory.CONCENTRATED_LP  # type: ignore[index]


# ---------------------------------------------------------------- EIP-7702


DELEGATE = "0x63c0c19a282a1b52b07dd5a65b58948a07dae32b"


def test_eip7702_delegate_parsing():
    assert eip7702_delegate("0xef0100" + DELEGATE[2:]) == DELEGATE
    assert eip7702_delegate("0xEF0100" + DELEGATE[2:].upper()) == DELEGATE
    assert eip7702_delegate("0x") is None
    assert eip7702_delegate("0x6080604052") is None
    assert eip7702_delegate("0xef0100" + DELEGATE[2:] + "00") is None  # 长度不对


class _Store:
    def __init__(self):
        self.rows = {}

    def get_many(self, chain, addresses):
        return {a: self.rows[a] for a in addresses if a in self.rows}

    def upsert_many(self, records):
        self.rows.update({r.address: r for r in records})


def test_identify_marks_eip7702_account_as_eoa():
    delegated, contract = "0x" + "71" * 20, "0x" + "72" * 20
    codes = {delegated: "0xef0100" + DELEGATE[2:], contract: "0x6080604052"}
    got = identify(
        Chain.BSC,
        [delegated, contract],
        store=_Store(),
        reader=lambda batch: {r.key: None for r in batch},
        code_reader=lambda addrs: {a: codes[a] for a in addrs},
    )
    assert (got[delegated].kind, got[delegated].evidence) == ("eoa", {"eip7702_delegate": DELEGATE})
    assert got[contract].kind == "unknown"
