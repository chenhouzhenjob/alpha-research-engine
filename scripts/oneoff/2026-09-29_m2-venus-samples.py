"""M2 Venus 样本（临时代码）：核心池的市场清单 + 存款、负债、待领奖励的估值样本。

1. 市场清单：固定区块上 Comptroller.getAllMarkets()，写入 tests/fixtures/venus_core_markets.json。
   正式的发现流程是识别第一层的 registry_call（步骤 12）；在它落地之前，测试用这份清单构造合约识别结果。
2. 估值样本：取金标准里 Venus 样本的几个账户，在固定区块上记录估值器的全部读取，以及链上真值：
   - 存款：以 owner 身份静态调用 `balanceOfUnderlying(owner)`（会先计息，与估值用的 exchangeRateCurrent 一致）；
   - 负债：`borrowBalanceCurrent(owner)`；待领奖励：`venusAccrued(owner)`。

执行：cd research && uv run python scripts/oneoff/2026-09-29_m2-venus-samples.py
输出：tests/fixtures/venus_core_markets.json、tests/valuation_golden/bsc/venus-core_<kind>_<market8>_<owner8>.json
删除条件：步骤 12 的识别 runner 和 M3 估值任务上线后删除。期限：2026-12-31。
不被任何应用、定时任务或默认测试路径 import。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests
from alpha_chains.factory import build_evm_adapter, rpc_urls_env
from alpha_core.types import Chain
from alpha_protocols.config.instances import instance_registry
from alpha_protocols.decoding.models import PositionKind, PositionRef
from alpha_protocols.runtime import multicall_reader, valuers_for
from alpha_protocols.valuation.models import ValuationRequest
from dotenv import load_dotenv
from eth_abi import decode, encode
from eth_utils import keccak

TESTS = Path("packages/protocols/tests")
ACCOUNT_CASES = ["borrow_vusdt", "borrow_vbnb", "supply_vusdt", "repay_vusdt"]


def sel(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()[:8]


def main() -> None:
    load_dotenv(".env")
    key = re.findall(r"rpc\.ankr\.com/bsc/([A-Za-z0-9]+)", os.environ["BNB_RPC_URLS"])[1]
    url = f"https://rpc.ankr.com/bsc/{key}"
    os.environ[rpc_urls_env(Chain.BSC)] = url

    def call(to: str, data: str, block: int, sender: str | None = None) -> bytes | None:
        tx = {"to": to, "data": data} | ({"from": sender} if sender else {})
        body = requests.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [tx, hex(block)]}, timeout=60
        ).json()
        return None if "error" in body else bytes.fromhex(body["result"][2:])

    head = int(
        requests.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, timeout=60
        ).json()["result"],
        16,
    )
    block = head - 5
    dep = instance_registry().instances["venus-core"].deployments[Chain.BSC]
    comptroller = dep.roles["comptroller"][0]
    markets = [m.lower() for m in decode(["address[]"], call(comptroller, sel("getAllMarkets()"), block))[0]]
    fixture = TESTS / "fixtures" / "venus_core_markets.json"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(
        json.dumps({"chain": "bsc", "instance_key": "venus-core", "block": block, "markets": markets}, indent=2) + "\n"
    )
    print(f"  ✓ {fixture}：{len(markets)} 个市场")

    valuer = valuers_for(Chain.BSC)["venus-core"]
    reader = multicall_reader(build_evm_adapter(Chain.BSC), block=block)
    owners = [
        json.loads((TESTS / "golden/bsc/compound_v2_like" / f"{c}.json").read_text())["subject_wallet"]
        for c in ACCOUNT_CASES
    ]
    vusdt, vbnb = "0xfd5840cd36d94d7229439859c0112a4185bc0255", dep.options.native_market
    for owner in dict.fromkeys(owners):
        arg = encode(["address"], [owner]).hex()
        for market in (vusdt, vbnb):
            v_balance = int.from_bytes(call(market, sel("balanceOf(address)") + arg, block), "big")
            debt = int.from_bytes(call(market, sel("borrowBalanceStored(address)") + arg, block), "big")
            for kind, active, truth_sig in (
                (PositionKind.SHARE, v_balance > 0, "balanceOfUnderlying(address)"),
                (PositionKind.DEBT, debt > 0, "borrowBalanceCurrent(address)"),
            ):
                if not active:
                    continue
                truth = int.from_bytes(call(market, sel(truth_sig) + arg, block, owner), "big")
                _record(valuer, reader, kind, market, owner, block, {"amount": truth})
        accrued = int.from_bytes(call(comptroller, sel("venusAccrued(address)") + arg, block), "big")
        if accrued:
            _record(valuer, reader, PositionKind.CLAIMABLE, comptroller, owner, block, {"amount": accrued})


def _record(valuer, reader, kind, target, owner, block, truth) -> None:
    request = ValuationRequest(PositionRef("bsc", "venus-core", kind, target, owner))
    reads: dict[str, bytes | None] = {}
    for _ in range(4):
        pending = [r for r in valuer.plan(request, reads) if r.key not in reads]
        if not pending:
            break
        reads.update(reader(pending))
    doc = {
        "kind": f"venus_{kind.value}",
        "chain": "bsc",
        "instance_key": "venus-core",
        "position_kind": kind.value,
        "target": target,
        "owner": owner,
        "block": block,
        "reads": {k: (v.hex() if v is not None else None) for k, v in reads.items()},
        "truth": truth,
    }
    path = TESTS / "valuation_golden/bsc" / f"venus-core_{kind.value}_{target[2:10]}_{owner[2:10]}.json"
    path.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"  ✓ {path.name} truth={truth}")


if __name__ == "__main__":
    main()
