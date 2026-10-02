# 1e：复合分权重与分档校准报告

候选池数: 27（全部 qualified 候选池，当前时点单次快照，不是历史面板）。

## 分项覆盖率（该分项当前有多少池子算得出来）

| 分项 | 可得池子数 | 覆盖率 |
|---|---|---|
| NominalAPR | 27 | 100% |
| VTRatio | 27 | 100% |
| IL 风险幅度 | 27 | 100% |
| CapitalVolatility | 0 | 0% |
| AgePenalty | 27 | 100% |

## 分项相关性（皮尔逊系数，只在两项都可得的池子上算）

| | NominalAPR | VTRatio | IL 风险幅度 | CapitalVolatility |
|---|---|---|---|---|
| NominalAPR | 1.00 | 0.79 | -0.10 | n/a |
| VTRatio | 0.79 | 1.00 | -0.17 | n/a |
| IL 风险幅度 | -0.10 | -0.17 | 1.00 | n/a |
| CapitalVolatility | n/a | n/a | n/a | 1.00 |

## 分数分布 / S-A-B-C 分档

分数范围: -0.2339 ~ 0.6148（均值 -0.0012）。

| 分档 | 数量 | 占比 |
|---|---|---|
| S | 0 | 0% |
| A | 1 | 4% |
| B | 0 | 0% |
| C | 26 | 96% |

## 校准建议（基于以上真实数据的发现，不是凭空调整）

1. **理论可达分数范围和 S/A/B/C 阈值不匹配**：原公式加分项权重合计 0.55，扣分项权重合计 0.35 + AgePenalty 0.1，理论上限约 0.55（扣分项都恰好是 0 才能达到），理论下限约 -0.45。S 档阈值 0.75 在这个范围里几乎不可能达到（除非某池子加分项归一化值拉满、扣分项全部为 0）。这次真实数据跑出来的分数范围是 -0.2339 ~ 0.6148，印证了这一点——建议二选一：(a) 把 S/A/B/C 阈值按实际可达范围重新标定（更容易落地，但阈值失去「绝对含义」）；(b) 改公式结构，让加分项权重合计归一到 1（扣分项作为在这个基础上的折扣而不是独立减项），这样分数天然落在 [0,1] 附近，阈值不用改——(b) 是更彻底的方案但改动面更大，需要产品侧确认。
2. **CapitalVolatility 当前覆盖率不足一半**，对应权重被系统性重新分摊给其余分项——这不是这些分项本身没有校准价值，而是 1a 已知的数据积累限制（CapitalVolatility 需要 14 天真实 TVL 历史，目前只有 1 天）。等数据积累够之后应该重跑这个校准，现在的相关性分析和分数分布都是在「这几项系统性缺失」的前提下算出来的，不是终局结论。
3. **AgePenalty 的实际影响力远小于名义权重**：它不参与归一化，原始值只有 0 或 0.05，乘权重 0.10 之后最多影响分数 0.0050，而其余四项每一项都能在 [0, 权重] 的范围里连续变化——0.10 的名义权重和不到 0.005 的实际最大影响力不成比例。建议要么把 AgePenalty 也纳入某种归一化（比如按「距准入门槛 7 天还有多久」连续化，而不是 0/1 两档），要么干脆承认它只是一个象征性的小惩罚，不需要按 10% 的权重量级去理解。
4. **发现高相关分项**：NominalAPR 和 VTRatio（r=0.79），|r|≥0.7。这两项可能在重复计分同一个信号，建议 1e 后续迭代时考虑合并或降低其中一项的权重，具体怎么调需要更多样本验证这不是巧合。

## 按池子拆分（按 CompositeScore 降序）

| 交易对 | NominalAPR | VTRatio | IL 风险 | CapitalVol | AgePenalty | Score | 置信度 | 分档 |
|---|---|---|---|---|---|---|---|---|
| [Cake / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x1e213600fa9317feac4ef4087acdf5d0e25d7187) | 21.143% | 8.6457 | 0.007% | n/a | 0.0000 | 0.6148 | 0.80 | A |
| [USDC / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0xf2688fb5b81049dfb7703ada5e770543770612c4) | 14.195% | 5.6161 | 0.012% | n/a | 0.0000 | 0.3200 | 0.80 | C |
| [BTCB / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x62edaf2a56c9fb55be5f9b1399ac067f6a37013b) | 9.534% | 3.5968 | 0.011% | n/a | 0.0000 | 0.1737 | 0.80 | C |
| [USDT / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x172fcd41e0913e95784454622d1c3724f546f849) | 10.611% | 3.2586 | 0.012% | n/a | 0.0000 | 0.1709 | 0.80 | C |
| [Cake / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0xafb2da14056725e3ba3a30dd846b6bbbd7886c56) | 8.043% | 0.6678 | 0.007% | n/a | 0.0000 | 0.0937 | 0.80 | C |
| [USDC / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0x81a9b5f18179ce2bf8f001b8a634db80771f1824) | 11.474% | 0.5775 | 0.012% | n/a | 0.0000 | 0.0926 | 0.80 | C |
| [USDT / BTCB 0.01%](https://www.geckoterminal.com/bsc/pools/0x247f51881d1e3ae0f759afb801413a6c948ef442) | 6.739% | 2.5615 | 0.012% | n/a | 0.0000 | 0.0804 | 0.80 | C |
| [BTCB / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0x6bbc40579ad1bbd243895ca0acb086bb6300d636) | 10.315% | 0.5264 | 0.011% | n/a | 0.0000 | 0.0748 | 0.80 | C |
| [USDT / WBNB 0.25%](https://www.geckoterminal.com/bsc/pools/0x1401ff943d08a7e098328c1d3a9d388923b115d2) | 9.155% | 0.1291 | 0.012% | n/a | 0.0000 | 0.0292 | 0.80 | C |
| [USDT / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0x36696169c63e42cd08ce11f5deebbcebae652050) | 8.830% | 0.3247 | 0.013% | n/a | 0.0000 | 0.0265 | 0.80 | C |
| [USDT / USDC 0.01%](https://www.geckoterminal.com/bsc/pools/0x92b7807bf19b7dddf89b706143896d05228f3121) | 0.366% | 0.1474 | 0.000% | n/a | 0.0000 | 0.0093 | 0.80 | C |
| [USDT / USDC 0.05%](https://www.geckoterminal.com/bsc/pools/0x4f31fa980a675570939b737ebdde0471a4be40eb) | 0.132% | 0.0110 | 0.000% | n/a | 0.0000 | 0.0003 | 0.80 | C |
| [USDT / BTCB 0.05%](https://www.geckoterminal.com/bsc/pools/0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4) | 5.856% | 0.3721 | 0.012% | n/a | 0.0000 | -0.0169 | 0.80 | C |
| [BTCB / USDC 0.25%](https://www.geckoterminal.com/bsc/pools/0x19ceabe800596ec01164c3680a66e8216d47d517) | 5.804% | 0.0935 | 0.011% | n/a | 0.0000 | -0.0209 | 0.80 | C |
| [ETH / BTCB 0.05%](https://www.geckoterminal.com/bsc/pools/0x4bba1018b967e59220b22ca03f68821a3276c9a6) | 2.423% | 0.2011 | 0.007% | n/a | 0.0000 | -0.0245 | 0.80 | C |
| [ETH / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x62fcb3c1794fb95bd8b1a97f6ad5d8a7e4943a1e) | 4.361% | 1.3054 | 0.017% | n/a | 0.0000 | -0.0624 | 0.80 | C |
| [Cake / WBNB 0.25%](https://www.geckoterminal.com/bsc/pools/0x133b3d95bad5405d14d53473671200e9342896bf) | 1.208% | 0.0184 | 0.008% | n/a | 0.0000 | -0.0649 | 0.80 | C |
| [ETH / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0xd0e226f674bbf064f54ab47f42473ff80db98cba) | 5.077% | 0.3292 | 0.017% | n/a | 0.0000 | -0.0844 | 0.80 | C |
| [ETH / USDC 0.05%](https://www.geckoterminal.com/bsc/pools/0x539e0ebfffd39e54a0f7e5f8fec40ade7933a664) | 7.336% | 0.6091 | 0.022% | n/a | 0.0000 | -0.0934 | 0.80 | C |
| [BTCB / WBNB 0.25%](https://www.geckoterminal.com/bsc/pools/0xfc75f4e78bf71ed5066db9ca771d4ccb7c1264e0) | 0.614% | 0.0099 | 0.011% | n/a | 0.0000 | -0.1067 | 0.80 | C |
| [ETH / USDT 0.01%](https://www.geckoterminal.com/bsc/pools/0x9f599f3d64a9d99ea21e68127bb6ce99f893da61) | 3.768% | 1.5409 | 0.022% | n/a | 0.0000 | -0.1266 | 0.80 | C |
| [ETH / USDT 0.05%](https://www.geckoterminal.com/bsc/pools/0xbe141893e4c6ad9272e8c04bab7e6a10604501a5) | 5.343% | 0.3202 | 0.022% | n/a | 0.0000 | -0.1405 | 0.80 | C |
| [Cake / USDT 0.25%](https://www.geckoterminal.com/bsc/pools/0x7f51c8aaa6b0599abd16674e2b17fec7a9f674a1) | 2.915% | 0.0342 | 0.019% | n/a | 0.0000 | -0.1588 | 0.80 | C |
| [Cake / ETH 0.05%](https://www.geckoterminal.com/bsc/pools/0xeb7528398b2725e1e0374734a87320132223a5c6) | 6.333% | 0.5258 | 0.028% | n/a | 0.0000 | -0.1829 | 0.80 | C |
| [ETH / WBNB 0.25%](https://www.geckoterminal.com/bsc/pools/0x7d05c84581f0c41ad80ddf677a510360bae09a5a) | 0.195% | 0.0031 | 0.018% | n/a | 0.0000 | -0.1985 | 0.80 | C |
| [Cake / USDT 0.05%](https://www.geckoterminal.com/bsc/pools/0x8ec186cd1ad51c380bd23fde29f852226647616c) | 1.695% | 0.1407 | 0.022% | n/a | 0.0000 | -0.2043 | 0.80 | C |
| [Cake / BTCB 0.25%](https://www.geckoterminal.com/bsc/pools/0x380a466ae6896d7d4fcd571e1e24cd5061a836b3) | 1.135% | 0.0183 | 0.023% | n/a | 0.0000 | -0.2339 | 0.80 | C |