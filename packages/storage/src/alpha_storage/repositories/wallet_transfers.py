"""地址视角转账仓储（`wallet_transfers`）。索引源和内部交易源的原始结果不可变，重复写入忽略。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import WalletTransferRow

# 单条 INSERT 的行数上限：Postgres 一条语句最多 65535 个绑定参数，本表 13 列
_CHUNK = 2000


class TransferKind(StrEnum):
    """转账的资产形态。"""

    EXTERNAL = "external"  # 交易本身携带的原生币（tx.value）
    INTERNAL = "internal"  # 合约内部调用转出的原生币，回执里没有日志
    ERC20 = "erc20"  # 同质化代币
    ERC721 = "erc721"  # 非同质化代币，数量恒为 1
    ERC1155 = "erc1155"  # 多代币标准，带编号和数量


class TransferDirection(StrEnum):
    """相对视角钱包的方向。"""

    IN = "in"  # 转入钱包
    OUT = "out"  # 从钱包转出
    SELF = "self"  # 钱包转给自己


@dataclass(frozen=True)
class TransferRecord:
    """一条地址视角的转账（wallet_transfers 的一行）。"""

    wallet_address: str  # 视角钱包
    tx_hash: str
    transfer_key: str  # 交易内去重键：tx / log:<日志序号> / internal:<调用路径或序号>
    kind: TransferKind
    token_address: str | None  # 原生币为 None
    token_id: int | None  # NFT 编号；同质化代币为 None
    amount_raw: int  # 最小单位；ERC721 为 1
    from_address: str
    to_address: str
    direction: TransferDirection
    block_number: int
    source: str  # 数据源：ankr / nodereal / …


class WalletTransferRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def insert_many(self, chain: str, records: list[TransferRecord]) -> None:
        """写入转账；主键已存在的忽略（原始数据不可变，不同来源对同一条转账以先到的为准）。"""
        unique = {(r.wallet_address.lower(), r.tx_hash.lower(), r.transfer_key): r for r in records}
        rows = [
            {
                "chain": chain,
                "wallet_address": w,
                "tx_hash": h,
                "transfer_key": k,
                "kind": r.kind.value,
                "token_address": r.token_address.lower() if r.token_address else None,
                "token_id": None if r.token_id is None else Decimal(r.token_id),
                "amount_raw": Decimal(r.amount_raw),
                "from_address": r.from_address.lower(),
                "to_address": r.to_address.lower(),
                "direction": r.direction.value,
                "block_number": r.block_number,
                "source": r.source,
            }
            for (w, h, k), r in unique.items()
        ]
        for i in range(0, len(rows), _CHUNK):
            stmt = insert(WalletTransferRow).values(rows[i : i + _CHUNK])
            self._session.execute(
                stmt.on_conflict_do_nothing(index_elements=["chain", "wallet_address", "tx_hash", "transfer_key"])
            )

    def list_for_wallet(
        self,
        chain: str,
        wallet_address: str,
        *,
        from_block: int | None = None,
        to_block: int | None = None,
        tx_hashes: list[str] | None = None,
    ) -> list[TransferRecord]:
        """按区块、交易、去重键升序返回钱包视角的转账；区块边界为闭区间，None 表示不限。"""
        row = WalletTransferRow
        stmt = select(row).where(row.chain == chain, row.wallet_address == wallet_address.lower())
        if from_block is not None:
            stmt = stmt.where(row.block_number >= from_block)
        if to_block is not None:
            stmt = stmt.where(row.block_number <= to_block)
        if tx_hashes is not None:
            stmt = stmt.where(row.tx_hash.in_({h.lower() for h in tx_hashes}))
        stmt = stmt.order_by(row.block_number, row.tx_hash, row.transfer_key)
        return [
            TransferRecord(
                wallet_address=r.wallet_address,
                tx_hash=r.tx_hash,
                transfer_key=r.transfer_key,
                kind=TransferKind(r.kind),
                token_address=r.token_address,
                token_id=None if r.token_id is None else int(r.token_id),
                amount_raw=int(r.amount_raw),
                from_address=r.from_address,
                to_address=r.to_address,
                direction=TransferDirection(r.direction),
                block_number=r.block_number,
                source=r.source,
            )
            for r in self._session.scalars(stmt)
        ]
