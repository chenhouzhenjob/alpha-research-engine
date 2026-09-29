"""M2 估值金标准样本（临时代码）：V3 NFT 仓位、V2 LP 份额在固定区块上的全部状态读取 + 链上真值。

V2：取金标准里的真实移除流动性交易，在交易前一个区块上按"交出的 LP 数量"估值，真值是交易对 Burn 事件
的 amount0 / amount1（burn 先按 _mintFee 增发协议费 LP，再按份额从 token 余额里拆）。要求交易所在区块里，
这笔交易之前没有别的交易动过这个交易对，否则前一个区块的状态不等于交易执行时的状态（脚本会检查并跳过）。

V3：

对每个仓位：
1. 在固定区块上跑一遍估值器的读取计划，记录全部读取结果（测试时回放，不访问网络）；
2. 以仓位 owner 身份在同一区块静态调用 NPM：
   - `decreaseLiquidity(tokenId, 全部流动性, 0, 0, 最大 deadline)` 的返回值 = 取出全部流动性能拿回的本金；
   - `collect(tokenId, owner, 2¹²⁸−1, 2¹²⁸−1)` 的返回值 = 当前可领取的手续费（含 tokensOwed）。
   这两个数就是估值必须逐 wei 相等的链上真值。

仓位来源：BSC 取基准钱包当前仍有流动性的仓位；以太坊、Base 取 NPM 最近铸造、仍有流动性的仓位。
每条链尽量各取一个在区间内、一个在区间外的仓位。

执行：cd research && uv run python scripts/oneoff/2026-09-29_m2-valuation-samples.py
输出：packages/protocols/tests/valuation_golden/<chain>/<instance>_<tokenId>.json（已存在的跳过）
删除条件：M3 的估值任务上线、能用正式流程重新生成估值样本后删除。期限：2026-12-31。
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

OUT = Path("packages/protocols/tests/valuation_golden")
BENCHMARK_WALLET = "0x05bbf9032f4c829e31a1f1b0b725d77329fad6be"
SLUGS = {"bsc": "bsc", "ethereum": "eth", "base": "base"}
INSTANCE = {"bsc": "pancakeswap-v3", "ethereum": "uniswap-v3", "base": "uniswap-v3"}
MAX128 = (1 << 128) - 1


def sel(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()[:8]


class Rpc:
    def __init__(self, key: str, chain: str) -> None:
        self.url = f"https://rpc.ankr.com/{SLUGS[chain]}/{key}"
        self.mc = f"https://rpc.ankr.com/multichain/{key}"
        self.chain = chain

    def call(self, to: str, data: str, block: int, sender: str | None = None) -> bytes | None:
        tx = {"to": to, "data": data} | ({"from": sender} if sender else {})
        body = requests.post(
            self.url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [tx, hex(block)]}, timeout=60
        ).json()
        return None if "error" in body else bytes.fromhex(body["result"][2:])

    def head(self) -> int:
        return int(
            requests.post(
                self.url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, timeout=60
            ).json()["result"],
            16,
        )

    def history(self, method: str, key: str, address: str, desc: bool = True) -> list[dict]:
        params = {"blockchain": SLUGS[self.chain], "address": address, "pageSize": 1000, "descOrder": desc}
        return (
            requests.post(self.mc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=120)
            .json()["result"]
            .get(key, [])
        )


def candidates(rpc: Rpc, npm: str) -> list[int]:
    """候选 tokenId：BSC 取基准钱包持有的，其他链取 NPM 最近铸造的。"""
    out: list[int] = []
    if rpc.chain == "bsc":
        nfts = rpc.history("ankr_getNftTransfers", "transfers", BENCHMARK_WALLET)
        out = sorted({int(t["tokenId"]) for t in nfts if t["contractAddress"].lower() == npm}, reverse=True)
    # 基准钱包凑不齐区间内、区间外两种时，补上 NPM 最近铸造的仓位
    txs = rpc.history("ankr_getTransactionsByAddress", "transactions", npm)
    for t in txs[:60]:
        if t["status"] != "0x1" or "88316456" not in t["input"]:
            continue
        rc = requests.post(
            rpc.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [t["hash"]]},
            timeout=60,
        ).json()["result"]
        for lg in rc["logs"]:
            if lg["address"].lower() == npm and len(lg["topics"]) == 4 and int(lg["topics"][1], 16) == 0:
                out.append(int(lg["topics"][3], 16))
    return out


GOLDEN = Path("packages/protocols/tests/golden")
V2_INSTANCE = {"bsc": "pancakeswap-v2", "ethereum": "uniswap-v2", "base": "uniswap-v2"}
BURN = "0x" + keccak(text="Burn(address,uint256,uint256,address)").hex()
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


def v2_burns(key: str) -> None:
    """V2：用真实 Burn 校验 LP 估值。"""
    for case in sorted(GOLDEN.glob("*/uniswap_v2_like/remove_liquidity*.json")):
        doc = json.loads(case.read_text())
        chain_name, subject = doc["chain"], doc["subject_wallet"]
        chain, instance = Chain(chain_name), V2_INSTANCE[chain_name]
        rpc = Rpc(key, chain_name)
        os.environ[rpc_urls_env(chain)] = rpc.url
        logs = doc["receipt"]["logs"]
        burn = next(lg for lg in logs if lg["topics"][0] == BURN)
        pair = burn["address"]
        lp = next(
            int(lg["data"][2:66], 16)
            for lg in logs
            if lg["address"] == pair and lg["topics"][0] == TRANSFER and "0x" + lg["topics"][1][-40:] == subject
        )
        block = doc["receipt"]["block_number"]
        # 同一区块里、这笔交易之前有没有别的交易动过这个交易对
        blk = requests.post(
            rpc.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber", "params": [hex(block), False]},
            timeout=60,
        ).json()["result"]
        earlier = blk["transactions"][: doc["receipt"]["tx_index"]]
        touched = False
        for h in earlier[-200:]:
            rc = requests.post(
                rpc.url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [h]},
                timeout=60,
            ).json()["result"]
            if any(lg["address"].lower() == pair for lg in rc["logs"]):
                touched = True
                break
        if touched:
            print(f"  ✗ {case.name}：同一区块里更早的交易动过交易对，跳过")
            continue
        valuer = valuers_for(chain)[instance]
        reader = multicall_reader(build_evm_adapter(chain), block=block - 1)
        request = ValuationRequest(PositionRef(chain_name, instance, PositionKind.SHARE, pair, subject), lp)
        reads: dict[str, bytes | None] = {}
        for _ in range(4):
            pending = [r for r in valuer.plan(request, reads) if r.key not in reads]
            if not pending:
                break
            reads.update(reader(pending))
        truth = [int(burn["data"][2:66], 16), int(burn["data"][66:130], 16)]
        out = {
            "kind": "v2_burn",
            "chain": chain_name,
            "instance_key": instance,
            "pair": pair,
            "owner": subject,
            "liquidity": lp,
            "block": block - 1,
            "tx_hash": doc["tx_hash"],
            "reads": {k: (v.hex() if v is not None else None) for k, v in reads.items()},
            "truth": {"principal": truth},
        }
        path = OUT / chain_name / f"{instance}_burn_{doc['tx_hash'][2:10]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2) + "\n")
        print(f"  ✓ {path}  liquidity={lp} truth={truth}")


def main() -> None:
    load_dotenv(".env")
    key = re.findall(r"rpc\.ankr\.com/bsc/([A-Za-z0-9]+)", os.environ["BNB_RPC_URLS"])[1]
    v2_burns(key)
    for chain_name, instance in INSTANCE.items():
        chain = Chain(chain_name)
        rpc = Rpc(key, chain_name)
        os.environ[rpc_urls_env(chain)] = rpc.url
        adapter = build_evm_adapter(chain)
        block = rpc.head() - 5  # 留几个区块，避开链头重组
        dep = instance_registry().instances[instance].deployments[chain]
        npm = dep.roles["position_manager"][0]
        valuer = valuers_for(chain)[instance]
        picked: dict[bool, int] = {}
        for existing in (OUT / chain_name).glob(f"{instance}_*.json"):
            doc = json.loads(existing.read_text())
            picked[doc["in_range"]] = doc["token_id"]
        for tid in candidates(rpc, npm):
            if len(picked) == 2:
                break
            raw = rpc.call(npm, sel("positions(uint256)") + encode(["uint256"], [tid]).hex(), block)
            if (
                raw is None
                or decode(
                    ["uint96", "address", "address", "address", "uint24", "int24", "int24", "uint128"], raw[: 32 * 8]
                )[7]
                == 0
            ):
                continue
            owner_raw = rpc.call(npm, sel("ownerOf(uint256)") + encode(["uint256"], [tid]).hex(), block)
            owner = "0x" + owner_raw[-20:].hex()
            reads: dict[str, bytes | None] = {}
            reader = multicall_reader(adapter, block=block)
            request = ValuationRequest(PositionRef(chain_name, instance, PositionKind.NFT, str(tid), owner))
            for _ in range(4):
                pending = [r for r in valuer.plan(request, reads) if r.key not in reads]
                if not pending:
                    break
                reads.update(reader(pending))
            val = valuer.unwrap(request, reads)
            in_range = val.extra["in_range"]
            if in_range in picked:
                continue
            liquidity = val.extra["liquidity"]
            dec = rpc.call(
                npm,
                sel("decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))")
                + encode(["(uint256,uint128,uint256,uint256,uint256)"], [(tid, liquidity, 0, 0, 2**64)]).hex(),
                block,
                owner,
            )
            col = rpc.call(
                npm,
                sel("collect((uint256,address,uint128,uint128))")
                + encode(["(uint256,address,uint128,uint128)"], [(tid, owner, MAX128, MAX128)]).hex(),
                block,
                owner,
            )
            if dec is None or col is None:
                print(f"  ✗ {chain_name} {tid}：静态调用失败")
                continue
            picked[in_range] = tid
            doc = {
                "chain": chain_name,
                "instance_key": instance,
                "token_id": tid,
                "owner": owner,
                "block": block,
                "reads": {k: (v.hex() if v is not None else None) for k, v in reads.items()},
                "truth": {
                    "principal": list(decode(["uint256", "uint256"], dec)),
                    "fees": list(decode(["uint256", "uint256"], col)),
                },
                "in_range": in_range,
            }
            path = OUT / chain_name / f"{instance}_{tid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc, indent=2) + "\n")
            print(f"  ✓ {path}  in_range={in_range} truth={doc['truth']}")


if __name__ == "__main__":
    main()
