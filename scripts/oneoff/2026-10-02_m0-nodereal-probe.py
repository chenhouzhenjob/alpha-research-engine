"""M0 地址索引源选型实测：NodeReal `nr_getAssetTransfers`（临时代码）。

要回答的问题（设计文档 5.2a 的待补项）：
1. 内部交易是否完整：用 NodeReal 的 internal 类别补上 Ankr 缺的内部转账后，基准钱包原生币总账能否闭合
   （Ankr 估算缺口 74.5 BNB）；
2. 分页与跨度限制：每页条数上限、单次区块跨度上限、全量拉一遍的调用次数；
3. 普通交易、ERC20、NFT 转账的条数与 Ankr 对比（Ankr：交易 2497、ERC20 5207、NFT 144）；
4. `eth_getLogs`（topic 过滤、不带 address）的单次区块跨度上限；
5. Multicall3 大批量子调用能否执行（计费只能在 NodeReal 控制台看，接口不返回 CU）。

执行：cd research && uv run python scripts/oneoff/2026-10-02_m0-nodereal-probe.py [--refresh]
输出：终端报表；原始响应缓存在系统临时目录 alpha-engine-m0-nodereal/（不进仓库）。
删除条件：M1 步骤 6b 的 AddressHistorySource 实现合入、设计文档 5.2a 定稿后删除。期限：2026-12-31。
不被任何应用、定时任务或默认测试路径 import。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections import Counter
from pathlib import Path

import requests
from dotenv import load_dotenv

WALLET = "0x05bbf9032f4c829e31a1f1b0b725d77329fad6be"
# 起点比钱包第一笔发出交易（37,386,150）早一个窗口：最早的转入（37,385,955 的 USDT、首笔 gas 的 BNB 来源）
# 发生在第一笔发出交易之前，以发出交易为起点会漏掉它们
FIRST_BLOCK = 35_386_150
MAX_SPAN = 1_999_999  # nr_getAssetTransfers 单次跨度上限（实测 "range must be less than 2000000"）
CATEGORIES = ["external", "internal", "20", "721", "1155"]
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CACHE = Path(tempfile.gettempdir()) / "alpha-engine-m0-nodereal"
WEI = 10**18


class Rpc:
    """极简 JSON-RPC 客户端，记录调用次数（按方法），用于估算额度消耗。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.calls: Counter[str] = Counter()

    def __call__(self, method: str, params: list) -> dict:
        for attempt in range(5):
            self.calls[method] += 1
            try:
                r = requests.post(
                    self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=300
                )
            except requests.RequestException as e:
                self.calls[f"{method}:失败重试"] += 1
                print(f"    {method} 网络错误，重试：{type(e).__name__}")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            return r.json()
        raise RuntimeError(f"{method} 连续失败")


def pull_transfers(rpc: Rpc, head: int, side: str, refresh: bool) -> list[dict]:
    """按 200 万区块窗口 + pageKey 翻页拉全量转账。side 为 fromAddress 或 toAddress。"""
    path = CACHE / f"transfers_{side}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    out: list[dict] = []
    pages = 0
    start = FIRST_BLOCK
    while start <= head:
        end = min(start + MAX_SPAN, head)
        win_path = CACHE / f"win_{side}_{start}.json"
        if win_path.exists() and not refresh:
            out.extend(json.loads(win_path.read_text()))
            start = end + 1
            continue
        win: list[dict] = []
        t_win = time.time()
        page_key = ""
        while True:
            q = {"category": CATEGORIES, side: WALLET, "fromBlock": hex(start), "toBlock": hex(end), "maxCount": "0x3e8"}
            if page_key:
                q["pageKey"] = page_key
            j = rpc("nr_getAssetTransfers", [q])
            if "error" in j:
                raise RuntimeError(f"{side} {start}-{end}: {j['error']}")
            res = j["result"]
            win.extend(res["transfers"])
            pages += 1
            page_key = res.get("pageKey") or ""
            if not page_key:
                break
        win_path.write_text(json.dumps(win))
        out.extend(win)
        print(f"    {side} {start}-{end}: {len(win)} 条（{time.time() - t_win:.0f}s）", flush=True)
        start = end + 1
    print(f"  {side}: {len(out)} 条，{pages} 页")
    path.write_text(json.dumps(out))
    return out


def pull_ankr(key: str, refresh: bool) -> list[dict]:
    """Ankr 普通交易清单（已验证 nonce 连续），作为外部转账和 gas 的基准。"""
    path = CACHE / "ankr_txs.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    params = {"blockchain": "bsc", "address": WALLET, "pageSize": 10000, "descOrder": False}
    j = requests.post(
        f"https://rpc.ankr.com/multichain/{key}",
        json={"jsonrpc": "2.0", "id": 1, "method": "ankr_getTransactionsByAddress", "params": params},
        timeout=180,
    ).json()
    txs = j["result"]["transactions"]
    path.write_text(json.dumps(txs))
    return txs


def value_of(t: dict) -> int:
    v = t.get("value") or "0x0"
    return int(v, 16) if isinstance(v, str) else int(v)


def ledger(ankr_txs: list[dict], internal_in: list[dict], internal_out: list[dict], balance: int) -> None:
    """原生币总账：外部转入 − 外部转出 − gas + 内部转入 − 内部转出，对比当前余额。"""
    ext_in = ext_out = gas = 0
    for t in ankr_txs:
        ok = int(t.get("status", "0x1"), 16) == 1
        frm, to = t["from"].lower(), (t.get("to") or "").lower()
        if frm == WALLET:
            gas += int(t["gasUsed"], 16) * int(t["gasPrice"], 16)
            if ok:
                ext_out += value_of(t)
        if to == WALLET and ok:
            ext_in += value_of(t)
    i_in = sum(value_of(t) for t in internal_in if t.get("receiptsStatus", 1) == 1)
    i_out = sum(value_of(t) for t in internal_out if t.get("receiptsStatus", 1) == 1)
    before = ext_in - ext_out - gas
    after = before + i_in - i_out
    print("\n== 原生币总账（BNB）")
    print(f"  外部转入 {ext_in / WEI:.4f}  外部转出 {ext_out / WEI:.4f}  gas {gas / WEI:.4f}")
    print(f"  内部转入 {i_in / WEI:.4f}（{len(internal_in)} 条）  内部转出 {i_out / WEI:.4f}（{len(internal_out)} 条）")
    print(f"  当前余额 {balance / WEI:.6f}")
    print(f"  只用外部：缺口 {(balance - before) / WEI:.4f}")
    print(f"  补上内部：缺口 {(balance - after) / WEI:.6f}")


def probe_getlogs(rpc: Rpc, head: int) -> None:
    """eth_getLogs 不带 address、按 topic2=钱包 过滤 ERC20 Transfer，逐级放大跨度找上限。"""
    print("\n== eth_getLogs 跨度（topic 过滤，不带 address）")
    topic = "0x" + "0" * 24 + WALLET[2:]
    for span in (5_000, 10_000, 50_000, 100_000, 200_000, 500_000):
        t = time.time()
        j = rpc("eth_getLogs", [{"fromBlock": hex(head - span), "toBlock": hex(head), "topics": [TRANSFER, None, topic]}])
        dt = time.time() - t
        res = f"错误 {j['error'].get('message', '')[:90]}" if "error" in j else f"{len(j['result'])} 条"
        print(f"  {span:>7} 区块：{res}（{dt:.1f}s）")


def probe_multicall(rpc: Rpc) -> None:
    """Multicall3.aggregate3 打包 N 个 getEthBalance，看大批量能否执行。"""
    print("\n== Multicall3 大批量")
    sel_agg3 = "82ad56cb"  # aggregate3((address,bool,bytes)[])
    sel_bal = "4d2301cc"  # getEthBalance(address)
    for n in (100, 500, 1000, 2000):
        sub = sel_bal + "0" * 24 + WALLET[2:]
        sub_len = len(sub) // 2
        # 手工 ABI 编码 (address target, bool allowFailure, bytes callData)[]
        head_ = f"{32:064x}{n:064x}"
        elem_size = 32 * 3 + 32 + ((sub_len + 31) // 32) * 32  # target, allow, offset, len+data
        offsets = "".join(f"{32 * n + i * elem_size:064x}" for i in range(n))
        elem = (
            "0" * 24 + MULTICALL3[2:].lower()
            + f"{1:064x}"
            + f"{96:064x}"
            + f"{sub_len:064x}"
            + sub.ljust(((sub_len + 31) // 32) * 64, "0")
        )
        data = "0x" + sel_agg3 + head_ + offsets + elem * n
        t = time.time()
        j = rpc("eth_call", [{"to": MULTICALL3, "data": data}, "latest"])
        dt = time.time() - t
        res = f"错误 {j['error'].get('message', '')[:90]}" if "error" in j else f"返回 {len(j['result']) // 2} 字节"
        print(f"  {n:>5} 个子调用：{res}（{dt:.1f}s）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    load_dotenv(".env")
    urls = os.environ["BNB_RPC_URLS"].split(",")
    rpc = Rpc(next(u for u in urls if "nodereal" in u))
    ankr_key = next(u for u in urls[1:] if "ankr" in u).rsplit("/", 1)[1]
    CACHE.mkdir(exist_ok=True)

    head = int(rpc("eth_blockNumber", [])["result"], 16)
    print(f"head={head}，窗口数 {((head - FIRST_BLOCK) // (MAX_SPAN + 1)) + 1}")
    t0 = time.time()
    frm = pull_transfers(rpc, head, "fromAddress", args.refresh)
    to = pull_transfers(rpc, head, "toAddress", args.refresh)
    print(f"  拉取耗时 {time.time() - t0:.0f}s")

    print("\n== 条数（NodeReal，按类别；from/to 合并后去重）")
    seen: dict[tuple, dict] = {}
    for t in frm + to:
        seen[(t["category"], t["hash"], t.get("id"))] = t
    by_cat = Counter(t["category"] for t in seen.values())
    for c in CATEGORIES:
        print(f"  {c:>8}: {by_cat.get(c, 0)}")
    ext_hashes = {t["hash"] for t in seen.values() if t["category"] == "external"}
    print(f"  external 不同交易 {len(ext_hashes)}（Ankr 交易 2497）；ERC20 对照 Ankr 5207；721+1155 对照 Ankr 144")

    ankr_txs = pull_ankr(ankr_key, args.refresh)
    ankr_hashes = {t["hash"].lower() for t in ankr_txs}
    print(f"  Ankr 有而 NodeReal external 没有：{len(ankr_hashes - ext_hashes)}；反过来：{len(ext_hashes - ankr_hashes)}")
    zero_val = sum(1 for t in ankr_txs if value_of(t) == 0)
    print(f"  Ankr 交易中 value=0 的 {zero_val} 笔（看 NodeReal external 是否只含有转账金额的交易）")

    internal_in = [t for t in to if t["category"] == "internal"]
    internal_out = [t for t in frm if t["category"] == "internal"]
    failed = sum(1 for t in internal_in + internal_out if t.get("receiptsStatus", 1) != 1)
    print(f"\n== 内部交易：转入 {len(internal_in)}，转出 {len(internal_out)}，父交易失败的 {failed}")
    top = Counter()
    for t in internal_in:
        top[t["from"]] += value_of(t)
    for a, v in top.most_common(8):
        print(f"  来自 {a}: {v / WEI:.4f} BNB")
    if internal_in:
        print("  样例字段：", json.dumps({k: v for k, v in internal_in[0].items() if k != "input"})[:600])

    balance = int(rpc("eth_getBalance", [WALLET, "latest"])["result"], 16)
    ledger(ankr_txs, internal_in, internal_out, balance)

    probe_getlogs(rpc, head)
    probe_multicall(rpc)
    print(f"\n== 本次 NodeReal 调用次数：{dict(rpc.calls)}")


if __name__ == "__main__":
    main()
