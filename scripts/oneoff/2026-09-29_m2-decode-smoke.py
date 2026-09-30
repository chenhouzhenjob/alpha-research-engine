"""M2 端到端冒烟（临时代码）：用解码框架和全部首批家族解码基准钱包的全部交易，统计覆盖和对账。

流程：
1. 从 Ankr Advanced API 拉基准钱包的交易、token 转账、NFT 转账，汇总出所有和钱包有关的交易
   （包括别人发起、落到钱包上的交易，例如地址投毒）；
2. 批量抓交易和回执，缓存在系统临时目录（不进仓库），重跑时不重复抓；
3. 读取相关 token 的元数据（风险标记要用）；
4. 协议识别：注册表发现 + 第一层识别，结果写进本地库的 contract_registry（需要已执行迁移 0008）；
5. 逐笔解码；第一遍解出的未识别合约，用 M1 的 ABI 来源（Sourcify 按地址、openchain → 4byte 按 topic0）
   查 ABI 和事件签名（结果缓存在本地库 abi_cache），放进解码上下文后再解一遍，统计通用 ABI 解码的覆盖；
6. 输出统计：事件、家族、覆盖等级分布，告警，未识别合约排行，聚合器交换归并情况，原生币总账缺口，
   钱包全部 V3 仓位的估值。

执行：cd research && uv run python scripts/oneoff/2026-09-29_m2-decode-smoke.py [--refresh]
输出：终端报表 + 系统临时目录下的 summary.json
删除条件：M3 的 wallet_sync + 解码任务上线后，由正式流程产出同样的统计时删除。期限：2026-12-31。
不被任何应用、定时任务或默认测试路径 import。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import requests
from alpha_chains.erc20 import read_metadata_batch
from alpha_chains.factory import build_evm_adapter, rpc_urls_env
from alpha_core.chain_data import RawLog, TxInfo, TxReceipt
from alpha_core.errors import DataSourceUnavailableError
from alpha_core.ports import AbiKeyType, AbiStatus
from alpha_core.types import Chain
from alpha_datasources.abi_sources import default_resolver
from alpha_protocols.decoding.evm.flows import TOKEN_STANDARD_TOPICS
from alpha_protocols.decoding.models import NATIVE, PositionKind, PositionRef, TokenMeta, leaf_flows
from alpha_protocols.identification.runner import discover_registries, identify
from alpha_protocols.runtime import decode_context, decode_tx, identities_from_records, multicall_reader, value
from alpha_protocols.valuation.models import Component, ValuationRequest
from alpha_storage.stores import DbAbiStore, DbContractRegistryStore
from dotenv import load_dotenv

WALLET = "0x05bbf9032f4c829e31a1f1b0b725d77329fad6be"
NPM = "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364"
CACHE = Path(tempfile.gettempdir()) / "alpha-engine-m2-smoke"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def ankr(key: str, method: str, params: dict) -> dict:
    url = f"https://rpc.ankr.com/multichain/{key}"
    return requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=180).json()[
        "result"
    ]


def fetch(adapter, hashes: list[str], refresh: bool) -> dict[str, tuple[TxInfo, TxReceipt]]:
    """批量抓交易和回执，按行追加写入缓存（JSONL），中断后重跑接着抓。

    限速由链适配器处理（同一端点按提示时间退避重试，用尽再切换端点）；额度耗尽或持续限速时适配器抛异常，
    已抓的部分都在缓存里。
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / "txs.jsonl"
    cached: dict[str, dict] = {}
    if not refresh:
        legacy = CACHE / "txs.json"  # 早期版本整份写入的缓存
        if legacy.exists() and legacy.stat().st_size:
            cached.update(json.loads(legacy.read_text()))
        if path.exists():
            for line in path.read_text().splitlines():
                item = json.loads(line)
                cached[item["hash"]] = item
    elif path.exists():
        path.unlink()
    missing = [h for h in hashes if h not in cached]
    with path.open("a") as out:
        for i in range(0, len(missing), 100):
            chunk = missing[i : i + 100]
            txs, rcs = adapter.get_transactions(chunk), adapter.get_transaction_receipts(chunk)
            for h in chunk:
                if h in txs.ok and h in rcs.ok:
                    item = {"hash": h, "tx": asdict(txs.ok[h]), "receipt": asdict(rcs.ok[h])}
                    cached[h] = item
                    out.write(json.dumps(item) + "\n")
            out.flush()
            print(f"  抓取 {min(i + 100, len(missing))}/{len(missing)}", flush=True)
    result = {}
    for h in hashes:
        if h in cached:
            rc = cached[h]["receipt"]
            result[h] = (TxInfo(**cached[h]["tx"]), TxReceipt(**{**rc, "logs": [RawLog(**lg) for lg in rc["logs"]]}))
    return result


def resolve_abis(data, decoded, ctx):
    """为第一遍解码报出的未识别合约查 ABI（按地址）和它们相关日志的事件签名（按 topic0）。

    候选日志的口径和解码框架一致：未识别合约发出、不是 token 标准事件，且是钱包发起的交易里交易目标发出的，
    或 topic 里带钱包。
    某个来源临时不可用时这一条记为 unavailable，不中断冒烟。

    @return (统计, 带上 ABI 和签名的新解码上下文)
    """
    unknown = {a for d in decoded.values() for a in d.unknown_contracts}
    wallet_topic = "0x" + "0" * 24 + WALLET[2:]
    candidates = [
        (tx.tx_hash, lg)
        for tx, rc in data.values()
        if rc.status != 0
        for lg in rc.logs
        if lg.address in unknown
        and lg.topics
        and lg.topics[0] not in TOKEN_STANDARD_TOPICS
        and ((tx.from_address == WALLET and lg.address == (tx.to_address or "")) or wallet_topic in lg.topics[1:])
    ]
    resolver = default_resolver(store=DbAbiStore())
    status: Counter = Counter()

    def lookup(key_type: AbiKeyType, key: str):
        try:
            entry = resolver.resolve(Chain.BSC, key_type, key)
        except DataSourceUnavailableError:
            status[f"{key_type.value}:unavailable"] += 1
            return None
        status[f"{key_type.value}:{entry.status.value}"] += 1
        return entry.abi if entry.status is AbiStatus.SUCCESS else None

    topics = sorted({lg.topics[0] for _, lg in candidates})
    print(f"查 ABI：{len(unknown)} 个未识别合约，{len(topics)} 个事件 topic0", flush=True)
    abis = {a: abi for a in sorted(unknown) if (abi := lookup(AbiKeyType.ADDRESS, a))}
    sigs = {t: tuple(sig) for t in topics if (sig := lookup(AbiKeyType.EVENT, t))}
    stats = {
        "unknown_contracts": len(unknown),
        "candidate_logs": len(candidates),
        "event_topics": len(topics),
        "lookups": dict(status),
    }
    return stats, candidates, replace(ctx, contract_abis=abis, event_signatures=sigs)


def main() -> None:
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，重新抓交易和回执")
    args = parser.parse_args()
    key = re.findall(r"rpc\.ankr\.com/bsc/([A-Za-z0-9]+)", os.environ["BNB_RPC_URLS"])[1]
    os.environ[rpc_urls_env(Chain.BSC)] = f"https://rpc.ankr.com/bsc/{key}"
    adapter = build_evm_adapter(Chain.BSC)
    reader = multicall_reader(adapter)

    # 1. 和钱包有关的全部交易
    base = {"blockchain": "bsc", "address": WALLET, "pageSize": 10000, "descOrder": False}
    txs = ankr(key, "ankr_getTransactionsByAddress", base)["transactions"]
    tokens = ankr(key, "ankr_getTokenTransfers", base)["transfers"]
    nfts = ankr(key, "ankr_getNftTransfers", base)["transfers"]
    hashes = sorted({t["hash"].lower() for t in txs} | {t["transactionHash"].lower() for t in tokens + nfts})
    print(f"和钱包有关的交易：{len(hashes)}（钱包发出 {sum(t['from'].lower() == WALLET for t in txs)}）")

    # 2. 交易和回执
    data = fetch(adapter, hashes, args.refresh)
    print(f"抓到 {len(data)} 笔")

    # 3. token 元数据：钱包参与的转账日志的发出合约
    wallet_topic = "0x" + "0" * 24 + WALLET[2:]
    token_addrs = sorted(
        {
            lg.address
            for _, rc in data.values()
            for lg in rc.logs
            if lg.topics[:1] == [TRANSFER] and wallet_topic in lg.topics[1:3]
        }
    )
    meta = read_metadata_batch(adapter, token_addrs)
    token_meta = {a: TokenMeta(a, m.symbol, m.name, m.decimals) for a, m in meta.items()}

    # 4. 识别
    store = DbContractRegistryStore()
    store.upsert_many(discover_registries(Chain.BSC, reader))
    addresses = {tx.to_address for tx, _ in data.values() if tx.to_address} | {
        lg.address for _, rc in data.values() for lg in rc.logs
    }
    records = identify(
        Chain.BSC, sorted(addresses), store=store, reader=reader, code_reader=lambda a: adapter.get_codes(list(a)).ok
    )
    kinds = Counter((r.kind if r.family else r.kind) for r in records.values())
    print(f"识别：{len(records)} 个地址 {dict(kinds.most_common(8))}")
    # 注册表发现的市场也要进解码上下文（它们不一定出现在钱包的交易里）
    registry_records = discover_registries(Chain.BSC, reader)
    ctx = decode_context(
        Chain.BSC, tokens=token_meta, extra_identities=identities_from_records([*records.values(), *registry_records])
    )

    # 5. 解码：第一遍找出未识别合约，查 ABI 和事件签名后再解一遍
    decoded = {h: decode_tx(Chain.BSC, tx, rc, WALLET, ctx=ctx) for h, (tx, rc) in data.items()}
    abi_stats, candidates, ctx = resolve_abis(data, decoded, ctx)
    decoded = {h: decode_tx(Chain.BSC, tx, rc, WALLET, ctx=ctx) for h, (tx, rc) in data.items()}
    abi_stats["decoded_logs"] = sum(
        1 for d in decoded.values() for e in d.events if e.event_subtype.value == "decoded_log"
    )
    abi_stats["coverage"] = (
        abi_stats["decoded_logs"] / abi_stats["candidate_logs"] if abi_stats["candidate_logs"] else None
    )
    # 没解出来的候选日志按原因分：合约 ABI 和签名都没有 / 有但解不开（参数布局对不上）
    done = {
        (d.tx_hash, int(e.extra["log_index"]))
        for d in decoded.values()
        for e in d.events
        if e.event_subtype.value == "decoded_log"
    }
    left = [(h, lg) for h, lg in candidates if (h, lg.log_index) not in done]
    abi_stats["undecoded_reasons"] = dict(
        Counter(
            "no_abi_no_signature"
            if lg.address not in ctx.contract_abis and lg.topics[0] not in ctx.event_signatures
            else "abi_or_signature_mismatch"
            for _, lg in left
        )
    )
    abi_stats["undecoded_top"] = Counter(f"{lg.address}:{lg.topics[0][:10]}" for _, lg in left).most_common(8)
    abi_stats["by_source"] = dict(
        Counter(
            e.extra["abi_source"] for d in decoded.values() for e in d.events if e.event_subtype.value == "decoded_log"
        )
    )

    # 6. 统计
    events = [e for d in decoded.values() for e in d.events]
    by_family = Counter(e.family or "（兜底）" for e in events)
    by_kind = Counter(f"{e.event_type.value}/{e.event_subtype.value}" for e in events)
    tiers = Counter(e.coverage_tier.value for e in events if e.claimed_flow_ids)
    warnings = Counter(w.code.value for d in decoded.values() for w in d.warnings)
    unclaimed = sum(len(d.unclaimed_flow_ids) for d in decoded.values())
    unknown = Counter(a for d in decoded.values() for a in d.unknown_contracts)
    agg = [
        d
        for h, d in decoded.items()
        if data[h][0].to_address == "0xb300000b72deaeb607a12d5f54773d1c19c7028d" and d.succeeded
    ]
    agg_two_legs = sum(
        1
        for d in agg
        if {e.event_subtype.value for e in d.events if e.family == "dex_aggregator"} == {"spend", "receive"}
    )
    agg_incomplete = sum(1 for d in agg if any(e.extra.get("incomplete") for e in d.events))
    # 验收：每笔成功的聚合器交易要么归并出交换，要么带告警（不能静默漏掉）
    agg_silent = [
        d.tx_hash
        for d in agg
        if not any(e.family == "dex_aggregator" for e in d.events)
        and not any(w.code.value in ("unrecognized_call", "internal_unavailable") for w in d.warnings)
    ]
    agg_unrecognized = sum(1 for d in agg if any(w.code.value == "unrecognized_call" for w in d.warnings))
    npm = [d for h, d in decoded.items() if data[h][0].to_address == NPM and d.succeeded]
    npm_by_family = sum(1 for d in npm if any(e.family == "uniswap_v3_like" for e in d.events))

    # 原生币总账：全部交易的原生币净额（叶子流水）vs 链上余额（钱包第一笔交易前余额为 0）
    native_net = sum(
        (f.amount_raw if f.to_address == WALLET else 0) - (f.amount_raw if f.from_address == WALLET else 0)
        for d in decoded.values()
        for f in leaf_flows(d.flows)
        if f.asset == NATIVE
    )
    balance = adapter.get_balances([WALLET]).ok[WALLET]
    residual = balance - native_net

    # 缺口归因：聚合器里没有归并出两条腿的交易，逐笔用区块前后余额差核对（钱包在该区块只有这一笔交易时才可归因）
    def tx_native_net(d) -> int:
        return sum(
            (f.amount_raw if f.to_address == WALLET else 0) - (f.amount_raw if f.from_address == WALLET else 0)
            for f in leaf_flows(d.flows)
            if f.asset == NATIVE
        )

    suspects = [
        h
        for h, d in decoded.items()
        if d in agg
        and {e.event_subtype.value for e in d.events if e.family == "dex_aggregator"} != {"spend", "receive"}
    ]
    attributed, unattributable = 0, 0
    per_tx = []
    for h in suspects:
        block = data[h][1].block_number
        nonce = adapter.get_transaction_count(WALLET, block=block) - adapter.get_transaction_count(
            WALLET, block=block - 1
        )
        delta = (
            adapter.get_balances([WALLET], block=block).ok[WALLET]
            - adapter.get_balances([WALLET], block=block - 1).ok[WALLET]
        )
        if nonce != 1:
            unattributable += 1
            continue
        gap = delta - tx_native_net(decoded[h])
        attributed += gap
        per_tx.append((h, gap / 1e18))

    # V3 仓位估值
    token_ids = sorted({int(t["tokenId"]) for t in nfts if t["contractAddress"].lower() == NPM})
    requests_ = [
        ValuationRequest(PositionRef("bsc", "pancakeswap-v3", PositionKind.NFT, str(t), WALLET)) for t in token_ids
    ]
    vals = value(Chain.BSC, requests_, reader)
    open_positions = [v for v in vals if not v.error and v.extra.get("liquidity")]
    fees_left = [
        v for v in vals if not v.error and any(a.amount_raw for a in v.amounts if a.component is Component.FEE)
    ]

    summary = {
        "transactions": len(decoded),
        "events": len(events),
        "unclaimed_flows": unclaimed,
        "events_by_family": dict(by_family.most_common()),
        "events_by_kind": dict(by_kind.most_common(25)),
        "coverage_tiers_of_flow_events": dict(tiers),
        "warnings": dict(warnings),
        "unknown_contracts_top": unknown.most_common(15),
        "generic_abi_decoding": abi_stats,
        "aggregator": {
            "successful_swaps": len(agg),
            "two_legs": agg_two_legs,
            "incomplete_to_native": agg_incomplete,
            "unrecognized_call": agg_unrecognized,
            "silent": len(agg_silent),
            "silent_examples": agg_silent[:5],
        },
        "npm": {"successful_txs": len(npm), "decoded_by_family": npm_by_family},
        "native_ledger": {
            "decoded_net_wei": native_net,
            "balance_wei": balance,
            "residual_wei": residual,
            "residual_bnb": residual / 1e18,
        },
        "gap_attribution": {
            "aggregator_txs_without_two_legs": len(suspects),
            "attributable": len(per_tx),
            "unattributable": unattributable,
            "gap_bnb": attributed / 1e18,
            "share_of_residual": attributed / residual if residual else None,
            "largest": sorted(per_tx, key=lambda x: -abs(x[1]))[:5],
        },
        "v3_positions": {
            "nfts": len(token_ids),
            "valued": sum(1 for v in vals if not v.error),
            "open": len(open_positions),
            "with_unclaimed_fees": len(fees_left),
        },
    }
    out = CACHE / "summary.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
