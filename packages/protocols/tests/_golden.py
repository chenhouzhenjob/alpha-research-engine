"""金标准样本的加载工具（测试专用）。

样本由 `scripts/oneoff/2026-09-29_m2-golden-samples.py` 生成，格式 2：交易、回执、区块时间、
主体钱包在区块前后的余额和 nonce、相关 token 的元数据。

链规则和基础资产从链画像加载，与生产代码用同一份配置。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from alpha_core.chain_data import RawLog, TxInfo, TxReceipt
from alpha_protocols.config.chain_profiles import chain_profiles
from alpha_protocols.decoding.context import DecodeContext
from alpha_protocols.decoding.evm.rules import ChainRules
from alpha_protocols.decoding.models import TokenMeta

GOLDEN_DIR = Path(__file__).parent / "golden"

# 链规则和基础资产来自链画像（alpha_protocols/chains/*.yaml）
PROFILES = {c.value: p for c, p in chain_profiles().items()}


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
        kw = {"chain": self.chain, "tokens": self.tokens, "base_assets": PROFILES[self.chain].base_assets, **overrides}
        return DecodeContext(**kw)

    @property
    def rules(self) -> ChainRules:
        return PROFILES[self.chain].rules


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
