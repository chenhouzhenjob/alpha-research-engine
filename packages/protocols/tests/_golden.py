"""金标准样本的加载工具（测试专用）。

样本由 `scripts/oneoff/2026-09-29_m2-golden-samples.py` 生成，格式 2：交易、回执、区块时间、
主体钱包在区块前后的余额和 nonce、相关 token 的元数据。

链规则和基础资产在步骤 4 的链画像落地之前先写在这里，落地后改为从链画像加载。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from alpha_core.chain_data import RawLog, TxInfo, TxReceipt
from alpha_protocols.decoding.context import DecodeContext
from alpha_protocols.decoding.evm.rules import ChainRules, GasModel, SystemTxRule
from alpha_protocols.decoding.models import NATIVE, TokenMeta

GOLDEN_DIR = Path(__file__).parent / "golden"

# TODO(M2 步骤 4)：改为从链画像加载
CHAIN_RULES = {
    "bsc": ChainRules(),
    "ethereum": ChainRules(),
    "base": ChainRules(GasModel.OP_STACK, frozenset({SystemTxRule.OP_DEPOSIT})),
}
BASE_ASSETS = {
    "bsc": {
        NATIVE: "BNB",
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
        "0x55d398326f99059ff775485246999027b3197955": "USDT",
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
        "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD",
        "0x2170ed0880ac9a755fd29b2688956bd959f933f8": "ETH",
        "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c": "BTCB",
        "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82": "CAKE",
    },
    "ethereum": {
        NATIVE: "ETH",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
        "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    },
    "base": {
        NATIVE: "ETH",
        "0x4200000000000000000000000000000000000006": "WETH",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
    },
}


@dataclass(frozen=True)
class Sample:
    case_id: str  # `<chain>/<family>/<case>`
    chain: str
    subject: str
    tx: TxInfo
    receipt: TxReceipt
    balance_delta: int  # 主体钱包在该区块的原生币净变化
    attributable: bool  # 余额差能否完整归因到这笔交易
    tokens: dict[str, TokenMeta]
    raw: dict

    def context(self, **overrides) -> DecodeContext:
        kw = {"chain": self.chain, "tokens": self.tokens, "base_assets": BASE_ASSETS[self.chain], **overrides}
        return DecodeContext(**kw)

    @property
    def rules(self) -> ChainRules:
        return CHAIN_RULES[self.chain]


def load(case_id: str) -> Sample:
    doc = json.loads((GOLDEN_DIR / f"{case_id}.json").read_text())
    rc = doc["receipt"]
    receipt = TxReceipt(**{**rc, "logs": [RawLog(**lg) for lg in rc["logs"]]})
    st = doc["subject_state"]
    tokens = {a: TokenMeta(address=a, **m) for a, m in doc.get("tokens", {}).items()}
    return Sample(
        case_id=case_id,
        chain=doc["chain"],
        subject=doc["subject_wallet"],
        tx=TxInfo(**doc["tx"]),
        receipt=receipt,
        balance_delta=st["balance_after"] - st["balance_before"],
        attributable=st["balance_delta_attributable"],
        tokens=tokens,
        raw=doc,
    )


def all_case_ids() -> list[str]:
    return sorted(str(p.relative_to(GOLDEN_DIR).with_suffix("")) for p in GOLDEN_DIR.glob("*/*/*.json"))
