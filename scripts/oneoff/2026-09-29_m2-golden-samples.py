"""M2 金标准样本的挑选与抓取（临时代码）。

两个子命令：
- `select`：按第 7.1 节的规则挑交易，写出清单 `packages/protocols/tests/golden/cases.json`。
  清单提交进仓库，挑选结果从此固定；链上继续出块不影响已挑好的样本。
- `fetch`：按清单抓取交易、回执、区块时间，以及钱包在该区块前后的原生币余额和 nonce，
  写成 `packages/protocols/tests/golden/<chain>/<family>/<case>.json`。已存在的文件跳过。

挑选规则见 `research/docs/wallet-analyzer-M2-协议解码核心实施规划.md` 7.1。基准钱包的交易清单
来自 Ankr Advanced API（M0 选定的主源）；正式的 `AddressHistorySource` 实现在 M1 步骤 6b，
这里直接调接口，不复用。公开样本按事件签名在最近的区块区间里找，取第一笔满足条件的交易。

执行：
    cd research
    uv run python scripts/oneoff/2026-09-29_m2-golden-samples.py select [--chains ethereum,base]
    uv run python scripts/oneoff/2026-09-29_m2-golden-samples.py fetch [--refresh]

需要 `BNB_RPC_URLS` 里至少有一个可用的 Ankr 端点（Advanced API 和归档读取都用它）；同一个 key 用于 BSC、以太坊、Base。
删除条件：第二阶段 `protocol-adapter-author` Skill 的样本采集流程实现后删除。期限：2026-12-31。
不被任何应用、定时任务或默认测试路径 import。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import requests
from alpha_chains.evm_common import EvmChainAdapter
from alpha_chains.factory import build_evm_adapter, rpc_urls_env
from alpha_core.metering import InMemoryCallMeter
from alpha_core.types import Chain
from dotenv import load_dotenv
from eth_utils import keccak

GOLDEN_DIR = Path("packages/protocols/tests/golden")
MANIFEST = GOLDEN_DIR / "cases.json"
# 2：tx/receipt 增加 tx_type、mint、l1_fee（多链，见规划 5.12）
SCHEMA_VERSION = 2

WALLET = "0x05bbf9032f4c829e31a1f1b0b725d77329fad6be"  # 基准钱包
NPM = "0x46a15b0b27311cedf172ab29e4f4766fbe7f4364"  # PancakeSwap V3 NonfungiblePositionManager
AGGREGATOR = "0xb300000b72deaeb607a12d5f54773d1c19c7028d"  # 基准钱包使用的 DEX 聚合器（Diamond）
USDT = "0x55d398326f99059ff775485246999027b3197955"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
V2_ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"  # factory() 已核实为 PancakeSwap V2 factory
COMPTROLLER = "0xfd36e2c2a6789db23113685031d7f16329158384"  # Venus 核心池
XVS = "0xcf6bb5389c92bdda8a3747ddb454cb7a64626c63"  # Comptroller.getXVSAddress() 已核实
VUSDT = "0xfd5840cd36d94d7229439859c0112a4185bc0255"
VBNB = "0xa07c5b74c9b40447a954e1466938b865b6bbea36"
VBNB_GATEWAY = "0x4d2e4add7bbed906e949954516bb735cd51a5dca"  # vBNB 存取最常经过的中间合约（2026-09-29 实测）
VENUS_LIQUIDATION_MARKETS = [
    VUSDT,
    VBNB,
    "0x882c173bc7ff3b7786ca16dfed3dfffb9ee7847b",  # vBTC
    "0xf508fcd89b8bd15579dc79a6827cb4686a3592c8",  # vETH
    "0xeca88125a5adbe82614ffc12d0db554e2e2867c8",  # vUSDC
]

AGGREGATOR_SELECTORS = ["0xe5e8894b", "0x810c705b", "0xa03de6a9", "0xdad12b6c"]

# 以太坊、Base 的公开样本用到的合约（2026-09-29 已用 CREATE2 和 factory()/WETH9() 核实，见规划 5.12）
ETH_CONTRACTS = {
    "weth": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "usdt": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "v3_npm": "0xc36442b4a4522e871399cd717abdd847ab11fe88",
    "v2_router": "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",
}
BASE_CONTRACTS = {
    "weth": "0x4200000000000000000000000000000000000006",
    "v3_npm": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1",
    "v2_router": "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
}
# Base 在以太坊 L1 上的 OptimismPortal：用户存款在 L1 发出 TransactionDeposited，L2 上生成类型 0x7e 的交易
BASE_PORTAL_ON_L1 = "0x49048044d57e1c92a77f79988d21fa8faf74e97e"


def topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


TRANSFER = topic("Transfer(address,address,uint256)")
WBNB_DEPOSIT = topic("Deposit(address,uint256)")
WBNB_WITHDRAWAL = topic("Withdrawal(address,uint256)")
VENUS_MINT = topic("Mint(address,uint256,uint256,uint256)")
VENUS_REDEEM = topic("Redeem(address,uint256,uint256,uint256)")
# vBNB 是更早部署的合约，Mint/Redeem 沿用 Compound 原版的 3 个字段（2026-09-29 实测）。
COMPOUND_MINT = topic("Mint(address,uint256,uint256)")
COMPOUND_REDEEM = topic("Redeem(address,uint256,uint256)")
VENUS_BORROW = topic("Borrow(address,uint256,uint256,uint256)")
VENUS_REPAY = topic("RepayBorrow(address,address,uint256,uint256,uint256)")
VENUS_LIQUIDATE = topic("LiquidateBorrow(address,address,uint256,address,uint256)")


# ----------------------------------------------------------------------
# Ankr 端点
# ----------------------------------------------------------------------


# Ankr 对各链的叫法（以太坊叫 eth）。正式的映射在链画像的 provider_slugs 里（M2 步骤 4）。
ANKR_SLUGS = {"bsc": "bsc", "ethereum": "eth", "base": "base"}


class Ankr:
    """从 `BNB_RPC_URLS` 里找一个可用的 Ankr key，同时提供 Advanced API 和普通 JSON-RPC。

    同一个 key 可以访问 Ankr 支持的所有链；`for_chain` 返回指向另一条链的视图。
    """

    def __init__(self, key: str | None = None, chain: str = "bsc") -> None:
        self.chain = chain
        slug = ANKR_SLUGS[chain]
        candidates = (
            [key] if key else re.findall(r"rpc\.ankr\.com/bsc/([A-Za-z0-9]+)", os.environ.get("BNB_RPC_URLS", ""))
        )
        for k in candidates:
            self.key = k
            self.multichain = f"https://rpc.ankr.com/multichain/{k}"
            self.rpc_url = f"https://rpc.ankr.com/{slug}/{k}"
            if key or self._post(self.rpc_url, "eth_blockNumber", [], allow_error=True) is not None:
                return
        raise SystemExit("BNB_RPC_URLS 里没有可用的 Ankr 端点")

    def for_chain(self, chain: str) -> Ankr:
        return Ankr(self.key, chain)

    @staticmethod
    def _post(url: str, method: str, params: Any, *, allow_error: bool = False) -> Any:
        resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=120)
        if resp.status_code != 200:
            if allow_error:
                return None
            raise RuntimeError(f"{method} HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if "error" in body:
            if allow_error:
                return None
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]

    def rpc(self, method: str, params: list[Any]) -> Any:
        return self._post(self.rpc_url, method, params)

    def history(self, method: str, list_key: str, address: str, *, desc: bool, max_pages: int = 1) -> list[dict]:
        """分页拉取某地址的交易或转账；`desc=True` 时从最新开始。"""
        items: list[dict] = []
        token = None
        for _ in range(max_pages):
            params = {
                "blockchain": ANKR_SLUGS[self.chain],
                "address": address,
                "pageSize": 10000 if not desc else 1000,
                "descOrder": desc,
            }
            if token:
                params["pageToken"] = token
            result = self._post(self.multichain, method, params)
            items += result.get(list_key) or []
            token = result.get("nextPageToken")
            if not token:
                break
        return items

    def logs(self, address: str | list[str], topic0: str, span: int, *, end: int | None = None) -> list[dict]:
        """在最近 `span` 个区块里按合约和 topic0 取日志，按区块升序返回。"""
        head = end or int(self.rpc("eth_blockNumber", []), 16)
        out: list[dict] = []
        step = 50_000
        start = head - span
        while start < head:
            to = min(start + step - 1, head)
            try:
                out += self.rpc(
                    "eth_getLogs",
                    [{"address": address, "topics": [topic0], "fromBlock": hex(start), "toBlock": hex(to)}],
                )
            except RuntimeError as exc:
                # Ankr 的跨度上限和过滤条件（地址个数）有关，报"范围太大"时减半重试。
                if "too large" in str(exc) and step > 1_000:
                    step //= 2
                    continue
                raise
            start = to + 1
        return sorted(out, key=lambda lg: (int(lg["blockNumber"], 16), int(lg["logIndex"], 16)))

    def txs(self, hashes: list[str]) -> dict[str, dict]:
        out = {}
        for h in hashes:
            out[h] = self.rpc("eth_getTransactionByHash", [h])
        return out


def _words(data: str) -> list[int]:
    raw = data.removeprefix("0x")
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def _addr_word(word: int) -> str:
    return "0x" + format(word, "064x")[-40:]


# ----------------------------------------------------------------------
# select：挑选规则
# ----------------------------------------------------------------------


class Picker:
    """收集样本；找不到的类别记下来，最后统一报告，不中断挑选。"""

    def __init__(self) -> None:
        self.cases: list[dict] = []
        self.missing: list[str] = []
        self.chain = "bsc"  # 当前在挑哪条链的样本，由各 select_* 函数设置

    def add(self, family: str, case: str, tx_hash: str | None, subject: str | None, note: str) -> None:
        if not tx_hash or not subject:
            self.missing.append(f"{self.chain}/{family}/{case}")
            print(f"  ✗ {self.chain}/{family}/{case}：没有找到符合条件的交易")
            return
        self.cases.append(
            {
                "family": family,
                "case": case,
                "chain": self.chain,
                "tx_hash": tx_hash.lower(),
                "subject_wallet": subject.lower(),
                "note": note,
            }
        )
        print(f"  ✓ {self.chain}/{family}/{case} {tx_hash}")


# 挑样本用的简化同形字符表；正式规则在 decoding/risk.py（步骤 3）。
_CONFUSABLE = str.maketrans({"Ѕ": "S", "Т": "T", "О": "O", "Ս": "U", "Ꭰ": "D", "5": "S", "0": "O"})


def _looks_like(canonical: str, symbol: str) -> bool:
    """symbol 去掉空白和零宽字符、映射同形字符后是否等于 canonical，且本身不等于 canonical。"""
    cleaned = "".join(ch for ch in symbol if not ch.isspace() and ch not in "\u200b\u200c\u200d\ufeff")
    return cleaned != canonical and cleaned.translate(_CONFUSABLE).upper() == canonical


def _first(items, pred):
    return next((x for x in items if pred(x)), None)


def select_benchmark(ankr: Ankr, p: Picker) -> None:
    """基准钱包：通用、V3、聚合器样本。"""
    print("基准钱包 …")
    txs = sorted(
        ankr.history("ankr_getTransactionsByAddress", "transactions", WALLET, desc=False),
        key=lambda t: int(t["blockNumber"], 16),
    )
    tokens = ankr.history("ankr_getTokenTransfers", "transfers", WALLET, desc=False)
    nfts = ankr.history("ankr_getNftTransfers", "transfers", WALLET, desc=False)
    own = {t["hash"].lower() for t in txs if t["from"].lower() == WALLET}
    ok = lambda t: t["status"] == "0x1"  # noqa: E731

    def sent(t: dict, to: str, sel: str) -> bool:
        return t["from"].lower() == WALLET and (t["to"] or "").lower() == to and t["input"].startswith(sel)

    t = _first(txs, lambda t: (t["to"] or "").lower() == WALLET and int(t["value"], 16) > 0 and t["input"] == "0x")
    p.add("generic", "native_in", t and t["hash"], WALLET, "外部地址直接转入 BNB")
    t = _first(txs, lambda t: ok(t) and sent(t, USDT, "0xa9059cbb"))
    p.add("generic", "erc20_out", t and t["hash"], WALLET, "钱包直接调用 USDT.transfer 转出")
    tr = _first(
        tokens,
        lambda x: (
            x["direction"] == "in" and x["contractAddress"].lower() == USDT and x["transactionHash"].lower() not in own
        ),
    )
    p.add("generic", "erc20_in", tr and tr["transactionHash"], WALLET, "别人转入真 USDT，交易不是钱包发起的")
    t = _first(txs, lambda t: ok(t) and sent(t, USDT, "0x095ea7b3"))
    p.add("generic", "approve", t and t["hash"], WALLET, "USDT approve")
    t = _first(txs, lambda t: not ok(t) and sent(t, AGGREGATOR, ""))
    p.add("generic", "failed", t and t["hash"], WALLET, "失败的聚合器交易，只有 gas")
    # 地址投毒的仿冒 token 多数是"从钱包转出"方向（伪造一笔钱包转给相似地址的记录），所以不限方向。
    fake = lambda x, ascii_only: (  # noqa: E731
        x["contractAddress"].lower() != USDT
        and _looks_like("USDT", x["tokenSymbol"] or "")
        and (x["tokenSymbol"] or "").isascii() == ascii_only
    )
    tr = _first(tokens, lambda x: fake(x, True))
    p.add(
        "generic",
        "impersonator_ascii",
        tr and tr["transactionHash"],
        WALLET,
        f"仿冒 USDT：ASCII 形近字符 {tr and tr['tokenSymbol']!r}，方向 {tr and tr['direction']}",
    )
    tr = _first(tokens, lambda x: fake(x, False))
    p.add(
        "generic",
        "impersonator_confusable",
        tr and tr["transactionHash"],
        WALLET,
        f"仿冒 USDT：含非 ASCII 同形字符 {tr and tr['tokenSymbol']!r}，方向 {tr and tr['direction']}",
    )
    tr = _first(
        tokens,
        lambda x: x["direction"] == "out" and x["valueRawInteger"] == "0" and x["transactionHash"].lower() not in own,
    )
    p.add(
        "generic",
        "zero_transfer_from",
        tr and tr["transactionHash"],
        WALLET,
        "别人发起的、从钱包转出 0 数量的 transferFrom（地址投毒）",
    )
    nf = _first(
        nfts, lambda x: x["type"] == "ERC1155" and x["direction"] == "in" and ".us" in (x.get("collectionName") or "")
    )
    p.add("generic", "spam_nft", nf and nf["transactionHash"], WALLET, "ERC1155 垃圾空投，名称含网址")
    t = _first(txs, lambda t: ok(t) and t["from"].lower() == WALLET and t["input"].startswith("0x183ff085"))
    p.add("generic", "misc_checkin", t and t["hash"], WALLET, "checkIn() 签到，不动资产")
    t = _first(txs, lambda t: ok(t) and t["from"].lower() == WALLET and t["input"].startswith("0x5d29339c"))
    p.add("generic", "misc_claim_v2", t and t["hash"], WALLET, "claimV2 领取")

    t = _first(txs, lambda t: ok(t) and sent(t, NPM, "0x88316456") and int(t["value"], 16) == 0)
    p.add("uniswap_v3_like", "mint_token", t and t["hash"], WALLET, "NPM.mint，两边都付 ERC20")
    t = _first(
        txs, lambda t: ok(t) and sent(t, NPM, "0xac9650d8") and all(s in t["input"] for s in ("0c49ccbe", "fc6f7865"))
    )
    p.add(
        "uniswap_v3_like",
        "exit_multicall",
        t and t["hash"],
        WALLET,
        "multicall：decreaseLiquidity + collect（基准钱包的退出全是这种，不含 unwrap）",
    )
    t = _first(txs, lambda t: ok(t) and sent(t, NPM, "0xfc6f7865"))
    p.add("uniswap_v3_like", "collect_only", t and t["hash"], WALLET, "单独 collect")

    for sel in AGGREGATOR_SELECTORS:
        t = _first(txs, lambda t, sel=sel: ok(t) and sent(t, AGGREGATOR, sel))
        p.add("dex_aggregator", f"swap_{sel[2:]}", t and t["hash"], WALLET, f"聚合器交换，选择器 {sel}")
    token_dirs: dict[str, set[str]] = {}
    for x in tokens:
        token_dirs.setdefault(x["transactionHash"].lower(), set()).add(x["direction"])
    t = _first(txs, lambda t: ok(t) and sent(t, AGGREGATOR, "") and token_dirs.get(t["hash"].lower()) == {"out"})
    p.add(
        "dex_aggregator",
        "to_native",
        t and t["hash"],
        WALLET,
        "只有 token 流出、没有 token 流入：换成了 BNB，缺内部交易",
    )


def _by_selector(ankr: Ankr, contract: str, pages: int = 3) -> list[dict]:
    """某合约最近收到的交易（新到旧），只保留成功的、直接发给它的。"""
    txs = ankr.history("ankr_getTransactionsByAddress", "transactions", contract, desc=True, max_pages=pages)
    return [t for t in txs if t["status"] == "0x1" and (t["to"] or "").lower() == contract]


def _sel(tx: dict) -> str:
    return tx["input"][:10] if len(tx["input"]) >= 10 else tx["input"]


def select_wrapped_native(ankr: Ankr, p: Picker) -> None:
    print("WBNB …")
    txs = _by_selector(ankr, WBNB)
    t = _first(txs, lambda t: _sel(t) in ("0xd0e30db0", "0x") and int(t["value"], 16) > 0)
    p.add(
        "wrapped_native",
        "wrap",
        t and t["hash"],
        t and t["from"],
        "直接调用 WBNB.deposit，或直接向 WBNB 转 BNB 触发 deposit",
    )
    t = _first(txs, lambda t: _sel(t) == "0x2e1a7d4d")
    p.add("wrapped_native", "unwrap", t and t["hash"], t and t["from"], "直接调用 WBNB.withdraw")


def select_v2(ankr: Ankr, p: Picker) -> None:
    print("PancakeSwap V2 …")
    txs = ankr.history("ankr_getTransactionsByAddress", "transactions", V2_ROUTER, desc=True, max_pages=5)
    rules = [
        ("add_liquidity", ["0xe8e33700"], "addLiquidity：两种 ERC20"),
        ("add_liquidity_eth", ["0xf305d719"], "addLiquidityETH：一边付 BNB"),
        ("remove_liquidity", ["0xbaa2abde"], "removeLiquidity"),
        ("remove_liquidity_eth", ["0x02751cec", "0xaf2979eb"], "removeLiquidityETH（含支持转账税的版本）：收到 BNB"),
        ("swap_tokens_for_tokens", ["0x38ed1739", "0x5c11d795"], "token 换 token"),
        ("swap_tokens_for_eth", ["0x18cbafe5", "0x791ac947"], "token 换 BNB：路由 Withdrawal 后转原生币"),
        ("swap_eth_for_tokens", ["0x7ff36ab5", "0xb6f9de95"], "BNB 换 token"),
    ]
    for case, sels, note in rules:
        t = _first(txs, lambda t, sels=sels: t["status"] == "0x1" and t["input"][:10] in sels)
        p.add("uniswap_v2_like", case, t and t["hash"], t and t["from"], note)


def select_v3_public(ankr: Ankr, p: Picker) -> None:
    """基准钱包没有的 V3 情形：付 BNB 开仓、退出时 unwrapWETH9 成原生币。"""
    print("PancakeSwap V3（公开交易）…")
    txs = ankr.history("ankr_getTransactionsByAddress", "transactions", NPM, desc=True, max_pages=5)
    ok = lambda t: t["status"] == "0x1"  # noqa: E731
    t = _first(txs, lambda t: ok(t) and int(t["value"], 16) > 0 and "88316456" in t["input"])
    p.add(
        "uniswap_v3_like",
        "mint_native",
        t and t["hash"],
        t and t["from"],
        "付 BNB 开仓：multicall(mint + refundETH) 带 value",
    )
    t = _first(
        txs,
        lambda t: (
            ok(t)
            and t["input"].startswith("0xac9650d8")
            and all(s in t["input"] for s in ("0c49ccbe", "fc6f7865", "49404b7c"))
        ),
    )
    p.add(
        "uniswap_v3_like",
        "exit_multicall_unwrap",
        t and t["hash"],
        t and t["from"],
        "multicall：decrease + collect + unwrapWETH9，由推断钩子补原生币",
    )


def select_venus(ankr: Ankr, p: Picker) -> None:
    print("Venus …")
    sig = lambda s: "0x" + keccak(text=s).hex()[:8]  # noqa: E731
    vusdt = _by_selector(ankr, VUSDT)
    for case, sels, note in [
        ("supply_vusdt", [sig("mint(uint256)")], "vUSDT 存入（Venus 4 字段 Mint）"),
        (
            "redeem_vusdt",
            [sig("redeem(uint256)"), sig("redeemUnderlying(uint256)")],
            "vUSDT 取出（Venus 4 字段 Redeem）",
        ),
        ("borrow_vusdt", [sig("borrow(uint256)")], "vUSDT 借款"),
        ("repay_vusdt", [sig("repayBorrow(uint256)")], "借款人自己还款"),
    ]:
        t = _first(vusdt, lambda t, sels=sels: _sel(t) in sels)
        p.add("compound_v2_like", case, t and t["hash"], t and t["from"], note)
    # 替别人还款：直接调用 repayBorrowBehalf 很少见，从 RepayBorrow 事件里找 payer ≠ borrower 的。
    behalf = None
    for lg in ankr.logs(VUSDT, VENUS_REPAY, 200_000):
        w = _words(lg["data"])
        if _addr_word(w[0]) != _addr_word(w[1]):
            behalf = (lg["transactionHash"], _addr_word(w[0]), _addr_word(w[1]))
            break
    p.add(
        "compound_v2_like",
        "repay_on_behalf_payer",
        behalf and behalf[0],
        behalf and behalf[1],
        "替别人还款：还款人视角",
    )
    p.add(
        "compound_v2_like",
        "repay_on_behalf_borrower",
        behalf and behalf[0],
        behalf and behalf[2],
        "替别人还款：借款人视角（状态事件）",
    )

    vbnb = _by_selector(ankr, VBNB)
    t = _first(vbnb, lambda t: _sel(t) == sig("borrow(uint256)"))
    p.add("compound_v2_like", "borrow_vbnb", t and t["hash"], t and t["from"], "vBNB 借款：原生币由推断钩子补齐")
    # vBNB 的存取几乎都经过 gateway（事件里的 minter/redeemer 是 gateway，不是用户），
    # 正好作为"用户经中间合约操作协议"的样本：主体取交易发起人。
    gateway = _by_selector(ankr, VBNB_GATEWAY)
    found = None
    for t in gateway[:200]:
        rc = ankr.rpc("eth_getTransactionReceipt", [t["hash"]])
        if any(lg["address"].lower() == VBNB and lg["topics"][0] == COMPOUND_MINT for lg in rc["logs"]):
            found = t
            break
    p.add(
        "compound_v2_like",
        "supply_vbnb_via_gateway",
        found and found["hash"],
        found and found["from"],
        "vBNB 存入（Compound 原版 3 字段 Mint），经 gateway",
    )
    # gateway 只负责存入；取出优先选用户直接调用 vBNB 的，其次选经中间合约、发起人是普通地址
    # （排除 0x00000000 开头的机器人靓号）的。
    direct = indirect = None
    for lg in ankr.logs(VBNB, COMPOUND_REDEEM, 100_000)[:150]:
        tx = ankr.rpc("eth_getTransactionByHash", [lg["transactionHash"]])
        if (tx["to"] or "").lower() == VBNB:
            direct = tx
            break
        if indirect is None and not tx["from"].lower().startswith("0x00000000"):
            indirect = tx
    t = direct or indirect
    how = "直接调用 vBNB" if direct else f"经中间合约 {t and t['to']}"
    p.add(
        "compound_v2_like",
        "redeem_vbnb",
        t and t["hash"],
        t and t["from"],
        f"vBNB 取出（Compound 原版 3 字段 Redeem），{how}：原生币由推断钩子补齐",
    )

    liq = ankr.logs(VENUS_LIQUIDATION_MARKETS, VENUS_LIQUIDATE, 400_000)
    lg = liq[0] if liq else None
    w = _words(lg["data"]) if lg else None
    p.add(
        "compound_v2_like",
        "liquidation_borrower",
        lg and lg["transactionHash"],
        w and _addr_word(w[1]),
        "清算：借款人视角",
    )
    p.add(
        "compound_v2_like",
        "liquidation_liquidator",
        lg and lg["transactionHash"],
        w and _addr_word(w[0]),
        "清算：清算人视角",
    )

    comptroller = _by_selector(ankr, COMPTROLLER)
    claim_sels = [
        sig(s) for s in ("claimVenus(address)", "claimVenus(address,address[])", "claimVenusAsCollateral(address)")
    ]
    t = _first(comptroller, lambda t: _sel(t) in claim_sels)
    p.add("compound_v2_like", "claim_xvs", t and t["hash"], t and t["from"], "从 Comptroller 领取 XVS")


def _sole_tx_in_block(ankr: Ankr, t: dict) -> bool:
    """发起人在该区块里只发了这一笔交易（nonce 差为 1），样本的余额差才能完整归因到它。"""
    block = int(t["blockNumber"], 16)
    count = lambda b: int(ankr.rpc("eth_getTransactionCount", [t["from"], hex(b)]), 16)  # noqa: E731
    return count(block) - count(block - 1) == 1


def _pick_common_evm(ankr: Ankr, p: Picker, c: dict[str, str]) -> None:
    """以太坊、Base 共用的挑选规则：同一套家族在另一条链、另一个部署上的样本。

    这两条链的样本都要求发起人在该区块里只有这一笔交易，保证余额差可以归因。
    """
    ok = lambda t: t["status"] == "0x1" and _sole_tx_in_block(ankr, t)  # noqa: E731
    weth = _by_selector(ankr, c["weth"])
    t = _first(weth, lambda t: _sel(t) in ("0xd0e30db0", "0x") and int(t["value"], 16) > 0 and ok(t))
    p.add("wrapped_native", "wrap", t and t["hash"], t and t["from"], "直接调用 WETH.deposit 或直接转 ETH")
    t = _first(weth, lambda t: _sel(t) == "0x2e1a7d4d" and ok(t))
    p.add("wrapped_native", "unwrap", t and t["hash"], t and t["from"], "直接调用 WETH.withdraw")

    npm = ankr.history("ankr_getTransactionsByAddress", "transactions", c["v3_npm"], desc=True, max_pages=3)
    t = _first(npm, lambda t: "88316456" in t["input"] and ok(t))
    p.add("uniswap_v3_like", "mint", t and t["hash"], t and t["from"], "Uniswap V3 开仓（mint 或含 mint 的 multicall）")
    t = _first(
        npm,
        lambda t: (
            t["input"].startswith("0xac9650d8")
            and all(x in t["input"] for x in ("0c49ccbe", "fc6f7865", "49404b7c"))
            and ok(t)
        ),
    )
    p.add("uniswap_v3_like", "exit_multicall_unwrap", t and t["hash"], t and t["from"], "退出含 unwrapWETH9")
    t = _first(npm, lambda t: _sel(t) == "0xfc6f7865" and ok(t))
    p.add("uniswap_v3_like", "collect_only", t and t["hash"], t and t["from"], "单独 collect")

    router = ankr.history("ankr_getTransactionsByAddress", "transactions", c["v2_router"], desc=True, max_pages=3)
    for case, sels, note in [
        ("add_liquidity", ["0xe8e33700", "0xf305d719"], "Uniswap V2 添加流动性"),
        ("remove_liquidity_eth", ["0x02751cec", "0xaf2979eb"], "removeLiquidityETH：收到 ETH"),
        ("swap_tokens_for_eth", ["0x18cbafe5", "0x791ac947"], "token 换 ETH"),
    ]:
        t = _first(router, lambda t, sels=sels: _sel(t) in sels and ok(t))
        p.add("uniswap_v2_like", case, t and t["hash"], t and t["from"], note)


def select_ethereum(ankr: Ankr, p: Picker) -> None:
    print("以太坊 …")
    p.chain = "ethereum"
    eth = ankr.for_chain("ethereum")
    usdt = _by_selector(eth, ETH_CONTRACTS["usdt"])
    t = _first(usdt, lambda t: _sel(t) == "0xa9059cbb" and _sole_tx_in_block(eth, t))
    p.add("generic", "erc20_out", t and t["hash"], t and t["from"], "以太坊 USDT.transfer（非标准 ERC20：不返回 bool）")
    _pick_common_evm(eth, p, ETH_CONTRACTS)

    # 带转账税的 token：token 在转账过程中自己通过路由卖出税费，同一笔交易里路由会解包两次 WETH，
    # 只有"钱包转出 token 之后的第一次"属于钱包（规划 5.5）。挑一笔回执里路由解包 ≥2 次的作为回归样本。
    withdrawal = topic("Withdrawal(address,uint256)")
    router = ETH_CONTRACTS["v2_router"]
    txs = eth.history("ankr_getTransactionsByAddress", "transactions", router, desc=True, max_pages=3)
    found = None
    for t in [t for t in txs if t["status"] == "0x1" and _sel(t) == "0x791ac947"][:80]:
        logs = eth.rpc("eth_getTransactionReceipt", [t["hash"]])["logs"]
        hits = [lg for lg in logs if lg["topics"][0] == withdrawal and _addr_word(int(lg["topics"][1], 16)) == router]
        if len(hits) >= 2 and _sole_tx_in_block(eth, t):
            found = t
            break
    p.add(
        "uniswap_v2_like",
        "swap_tokens_for_eth_tax_token",
        found and found["hash"],
        found and found["from"],
        "带转账税的 token 换 ETH：路由解包两次，只有钱包转出 token 之后的第一次属于钱包",
    )


def select_base(ankr: Ankr, p: Picker) -> None:
    print("Base …")
    p.chain = "base"
    base = ankr.for_chain("base")
    weth = _by_selector(base, BASE_CONTRACTS["weth"])
    # 普通转账要验证 op_stack gas（余额差 = value + gas + l1Fee）；用 WETH 的 withdraw 发起人作为 EOA 候选，
    # 从其历史里找一笔发出的纯 ETH 转账。
    found = None
    for t in weth[:30]:
        hist = base.history("ankr_getTransactionsByAddress", "transactions", t["from"], desc=True)
        found = _first(
            hist,
            lambda x, who=t["from"]: (
                x["status"] == "0x1"
                and x["from"].lower() == who.lower()
                and x["input"] == "0x"
                and int(x["value"], 16) > 0
            ),
        )
        if found:
            break
    p.add(
        "generic", "native_out", found and found["hash"], found and found["from"], "Base 纯 ETH 转出：gas 必须加 l1Fee"
    )

    # L1 存款：在以太坊上找 Base OptimismPortal 的 TransactionDeposited，挑"自己存给自己、不带 calldata"
    # 的 ETH 存款，再到 Base 上该地址的历史里找对应的 0x7e 交易。
    eth = ankr.for_chain("ethereum")
    deposited = topic("TransactionDeposited(address,address,uint256,bytes)")
    dep = None
    for lg in eth.logs(BASE_PORTAL_ON_L1, deposited, 5_000):
        frm, to = _addr_word(int(lg["topics"][1], 16)), _addr_word(int(lg["topics"][2], 16))
        raw = bytes.fromhex(lg["data"][2:])
        opaque = raw[64 : 64 + int.from_bytes(raw[32:64], "big")]
        mint, calldata = int.from_bytes(opaque[:32], "big"), opaque[73:]
        if frm == to and mint > 0 and not calldata:
            hist = base.history("ankr_getTransactionsByAddress", "transactions", to, desc=True)
            # Ankr 返回的交易不一定带 mint 字段；自存 ETH 时 value 等于 mint，用任一字段匹配
            dep = _first(
                hist,
                lambda x: x.get("type") == "0x7e" and mint in (int(x.get("mint", "0x0"), 16), int(x["value"], 16)),
            )
            if dep:
                break
    p.add(
        "generic",
        "l1_deposit",
        dep and dep["hash"],
        dep and dep["to"],
        "OP Stack 存款交易：凭空铸造 ETH，无日志，不付 gas",
    )
    _pick_common_evm(base, p, BASE_CONTRACTS)


def _select_bsc(ankr: Ankr, p: Picker) -> None:
    select_benchmark(ankr, p)
    select_wrapped_native(ankr, p)
    select_v2(ankr, p)
    select_v3_public(ankr, p)
    select_venus(ankr, p)


SELECTORS = {"bsc": _select_bsc, "ethereum": select_ethereum, "base": select_base}


def cmd_select(ankr: Ankr, chains: list[str]) -> None:
    """只重挑 `chains` 里的链；清单里其他链的样本原样保留，已提交的挑选结果不被改动。"""
    p = Picker()
    for chain in chains:
        SELECTORS[chain](ankr, p)
    kept = []
    if MANIFEST.exists():
        kept = [c for c in json.loads(MANIFEST.read_text())["cases"] if c["chain"] not in chains]
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "cases": kept + p.cases}, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"\n写出 {MANIFEST}：{len(p.cases)} 个样本；缺失 {len(p.missing)} 个：{p.missing}")


# ----------------------------------------------------------------------
# fetch：按清单抓取
# ----------------------------------------------------------------------


def cmd_fetch(ankr: Ankr, *, refresh: bool) -> None:
    manifest = json.loads(MANIFEST.read_text())
    meter = InMemoryCallMeter(app="oneoff", job_ref="m2-golden")
    adapters: dict[str, EvmChainAdapter] = {}
    written = 0
    for case in manifest["cases"]:
        out = GOLDEN_DIR / case["chain"] / case["family"] / f"{case['case']}.json"
        if out.exists() and not refresh:
            continue
        chain = case["chain"]
        if chain not in adapters:
            # 走正式的链适配器（记账、故障转移），只用可用的 Ankr 端点，避开已停用的 key。
            os.environ[rpc_urls_env(Chain(chain))] = ankr.for_chain(chain).rpc_url
            adapters[chain] = build_evm_adapter(Chain(chain), meter=meter)
        adapter = adapters[chain]
        h, subject = case["tx_hash"], case["subject_wallet"]
        tx = adapter.get_transactions([h])
        rc = adapter.get_transaction_receipts([h])
        if h not in tx.ok or h not in rc.ok:
            print(f"  ✗ {chain}/{case['family']}/{case['case']}：{tx.failed or rc.failed}")
            continue
        receipt = rc.ok[h]
        block = receipt.block_number
        block_time = adapter.get_block_timestamps([block]).ok[block]
        before = adapter.get_balances([subject], block=block - 1).ok[subject]
        after = adapter.get_balances([subject], block=block).ok[subject]
        nonce_before = adapter.get_transaction_count(subject, block=block - 1)
        nonce_after = adapter.get_transaction_count(subject, block=block)
        sent_by_subject = tx.ok[h].from_address == subject
        doc = {
            "schema_version": SCHEMA_VERSION,
            **case,
            "tx": asdict(tx.ok[h]),
            "receipt": asdict(receipt),
            "block_time": block_time.isoformat(),
            # 钱包在该区块前后的原生币余额和 nonce，用于原生币对账（发现缺失的内部交易）。
            # nonce 差值等于"本交易是否由钱包发出"时（0 或 1），说明该区块里钱包没有发出别的交易，
            # 余额差才能完整归因到这笔交易；否则 `balance_delta_attributable` 为 false。
            "subject_state": {
                "balance_before": before,
                "balance_after": after,
                "nonce_before": nonce_before,
                "nonce_after": nonce_after,
                "balance_delta_attributable": nonce_after - nonce_before == int(sent_by_subject),
            },
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        written += 1
        print(f"  ✓ {out}")
    print(f"\n写入 {written} 个样本文件")
    for key, total in sorted(meter.snapshot().items(), key=lambda kv: (kv[0].provider, kv[0].method)):
        print(f"  {key.provider:10s} {key.method:28s} {key.status.value:12s} n={total.call_count}")


def main() -> None:
    load_dotenv(".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["select", "fetch"])
    parser.add_argument("--refresh", action="store_true", help="fetch 时覆盖已存在的样本文件（样本格式升级时用）")
    parser.add_argument("--chains", default="bsc,ethereum,base", help="select 时重挑哪些链，逗号分隔")
    args = parser.parse_args()
    ankr = Ankr()
    if args.command == "select":
        cmd_select(ankr, [c.strip() for c in args.chains.split(",") if c.strip()])
    else:
        cmd_fetch(ankr, refresh=args.refresh)


if __name__ == "__main__":
    main()
