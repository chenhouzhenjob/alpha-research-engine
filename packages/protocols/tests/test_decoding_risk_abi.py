"""风险标记与通用 ABI 解码的单元测试。"""

from __future__ import annotations

from alpha_core.chain_data import RawLog
from alpha_protocols.decoding.evm.abi_logs import (
    AbiSource,
    decode_with_contract_abi,
    decode_with_signature,
    split_types,
)
from alpha_protocols.decoding.models import NATIVE, RiskFlag, TokenMeta
from alpha_protocols.decoding.risk import classify_token, normalize_symbol
from eth_abi import encode
from eth_utils import keccak

USDT = "0x55d398326f99059ff775485246999027b3197955"
BASE = {NATIVE: "BNB", USDT: "USDT"}


def _meta(symbol, name=None, address="0x" + "ab" * 20, **kw):
    return TokenMeta(address=address, symbol=symbol, name=name, **kw)


def test_normalize_symbol_folds_confusables_and_invisible_chars():
    assert normalize_symbol("U5DT") == "USDT"
    assert normalize_symbol("UЅDТ") == "USDT"  # 西里尔 Ѕ、Т
    assert normalize_symbol(" U5DТ ") == "USDT"
    assert normalize_symbol("ＵＳＤＴ") == "USDT"  # 全角，由 NFKC 处理


def test_classify_token():
    assert classify_token(_meta("USDT", address=USDT), BASE) is RiskFlag.NORMAL  # 基础资产本身
    assert classify_token(_meta("USDT"), BASE) is RiskFlag.IMPERSONATOR  # 同名不同地址
    assert classify_token(_meta("UЅDТ"), BASE) is RiskFlag.IMPERSONATOR
    assert classify_token(_meta("BNB"), BASE) is RiskFlag.IMPERSONATOR  # 仿冒原生币符号
    assert classify_token(_meta("币安人生", "币安人生"), BASE) is RiskFlag.NORMAL  # 真实 meme 币，不误伤
    assert classify_token(_meta("Airdrop at [3000usdc.us]"), BASE) is RiskFlag.SPAM
    assert classify_token(_meta("XYZ", "Visit https://claim.example"), BASE) is RiskFlag.SPAM
    assert classify_token(_meta("REWARD", "Reward Token"), BASE) is RiskFlag.NORMAL  # 不按单词判 spam
    assert classify_token(_meta("USDT", risk_flag=RiskFlag.HACKED), BASE) is RiskFlag.HACKED  # 已有标记优先


def test_split_types_handles_tuples():
    assert split_types("address,(uint256,address)[],bytes") == ["address", "(uint256,address)[]", "bytes"]
    assert split_types("") == []


def _log(topics, data: bytes, address="0x" + "cd" * 20):
    return RawLog(address, topics, "0x" + data.hex(), 3, 1, "0x" + "ee" * 32)


SIG = "Deposit(address,uint256,string)"
TOPIC0 = "0x" + keccak(text=SIG).hex()
USER = "0x" + "11" * 20
USER_TOPIC = "0x" + "0" * 24 + USER[2:]


def test_signature_guess_decodes_when_layout_matches():
    log = _log([TOPIC0, USER_TOPIC], encode(["uint256", "string"], [5, "hi"]))
    got = decode_with_signature(log, SIG)
    assert got.source is AbiSource.SIGNATURE_GUESS
    assert got.args == {"arg0": USER, "arg1": 5, "arg2": "hi"}


def test_signature_guess_rejects_layout_that_does_not_fit():
    # data 里只有一个 string，按签名应有 uint256 + string：重新编码对不上原始 data，必须放弃
    assert decode_with_signature(_log([TOPIC0, USER_TOPIC], encode(["string"], ["hi"])), SIG) is None
    assert decode_with_signature(_log([TOPIC0, USER_TOPIC], b"\x00" * 32), SIG) is None
    log = _log([TOPIC0, USER_TOPIC], encode(["uint256", "string"], [5, "hi"]))
    assert decode_with_signature(log, "Other(uint256)") is None  # topic0 对不上


def test_contract_abi_decodes_non_leading_indexed():
    """完整 ABI 知道 indexed 不在最前面的写法，签名猜测做不到。"""
    abi = [
        {
            "type": "event",
            "name": "Deposit",
            "inputs": [
                {"name": "amount", "type": "uint256", "indexed": False},
                {"name": "user", "type": "address", "indexed": True},
                {"name": "memo", "type": "string", "indexed": False},
            ],
        }
    ]
    sig = "Deposit(uint256,address,string)"
    log = _log(["0x" + keccak(text=sig).hex(), USER_TOPIC], encode(["uint256", "string"], [7, "x"]))
    got = decode_with_contract_abi(log, abi)
    assert got.source is AbiSource.CONTRACT_ABI
    assert got.args == {"amount": 7, "user": USER, "memo": "x"}
    # 只有签名时的已知局限：indexed 不在最前面，猜出来的布局可能恰好也能逐字节还原 data，
    # 于是给出一个"形式上成立但含义错误"的结果。所以这类结果必须标为 signature_guess，只作线索。
    guess = decode_with_signature(log, sig)
    assert guess is None or (guess.source is AbiSource.SIGNATURE_GUESS and guess.args["arg0"] != 7)
