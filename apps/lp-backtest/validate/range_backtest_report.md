# 1d：推荐区间回测报告

覆盖池子数: 27；总样本数: 1782（3 档画像共用同一批取样时间点）。

## 范围说明

手续费捕获只回测「资金效率利用率」（真实历史价格路径决定，不依赖历史 volume），不是「历史每天赚了多少美元」——后者需要 GeckoTerminal 免费层拿不到的历史 volume 数据，详见本文件模块 docstring。

## 分画像结果

### 主动型（T_target = 3 天）

样本数: 594

- 目标周期内实际出界的比例: 27.9%
- 平均资金利用率（留在区间内时间占比）: 91.1%
- 平均资金效率倍数（相对全范围做市）: 156.84x
- IL 预测 MAE: 0.0151%（594 个可比样本）
- IL 预测 RMSE: 0.0305%

### 平衡型（T_target = 7 天）

样本数: 594

- 目标周期内实际出界的比例: 33.8%
- 平均资金利用率（留在区间内时间占比）: 86.6%
- 平均资金效率倍数（相对全范围做市）: 102.85x
- IL 预测 MAE: 0.0282%（567 个可比样本）
- IL 预测 RMSE: 0.0430%

### 被动型（T_target = 30 天）

样本数: 594

- 目标周期内实际出界的比例: 38.2%
- 平均资金利用率（留在区间内时间占比）: 81.9%
- 平均资金效率倍数（相对全范围做市）: 49.95x
- IL 预测 MAE: 0.1741%（486 个可比样本）
- IL 预测 RMSE: 0.2743%

## 结论

三档目标周期内的实际出界比例分别是 主动型(28%)、平衡型(34%)、被动型(38%)。设计方案本身预见的局限是：真实价格有趋势，单边行情下实际出界速度会比模型快，模型偏乐观——如果这里看到的出界比例明显高于「一半左右」这个 GBM 直觉基准（区间宽度本来就是按 1 个标准差设的，到期时出界属于正常概率事件，不是模型失败），说明这批池子近期确实偏单边走势，该三档推荐区间在维护频率上可能需要比设计值更保守（更频繁跳仓）才能达到预期的资金利用率。

同样受 1c 报告已经指出的样本构成限制：这批池子目前以白名单内部的蓝筹配对为主，波动率整体中等偏低，长尾高波动池子的表现还没有被这次回测覆盖到。

## 按池子拆分（平衡型 7 天档，前 20 个，按样本数排序）

| 交易对 | 样本数 | 出界比例 | 平均资金利用率 | IL 预测 MAE |
|---|---|---|---|---|
| [Cake / ETH 0.05%](https://www.geckoterminal.com/bsc/pools/0xeb7528398b2725e1e0374734a87320132223a5c6) | 22 | 27.3% | 89.0% | 0.0369% |
| [Cake / USDT 0.05%](https://www.geckoterminal.com/bsc/pools/0x8ec186cd1ad51c380bd23fde29f852226647616c) | 22 | 31.8% | 85.7% | 0.0461% |
| [Cake / USDT 0.25%](https://www.geckoterminal.com/bsc/pools/0x7f51c8aaa6b0599abd16674e2b17fec7a9f674a1) | 22 | 31.8% | 86.4% | 0.0440% |
| [Cake / BTCB 0.25%](https://www.geckoterminal.com/bsc/pools/0x380a466ae6896d7d4fcd571e1e24cd5061a836b3) | 22 | 22.7% | 90.3% | 0.0318% |
| [Cake / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x1e213600fa9317feac4ef4087acdf5d0e25d7187) | 22 | 45.5% | 85.7% | 0.0194% |
| [Cake / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0xafb2da14056725e3ba3a30dd846b6bbbd7886c56) | 22 | 40.9% | 86.4% | 0.0195% |
| [Cake / WBNB 0.25%](https://www.geckoterminal.com/bsc/pools/0x133b3d95bad5405d14d53473671200e9342896bf) | 22 | 40.9% | 85.7% | 0.0215% |
| [ETH / USDT 0.01%](https://www.geckoterminal.com/bsc/pools/0x9f599f3d64a9d99ea21e68127bb6ce99f893da61) | 22 | 31.8% | 89.0% | 0.0587% |
| [ETH / USDT 0.05%](https://www.geckoterminal.com/bsc/pools/0xbe141893e4c6ad9272e8c04bab7e6a10604501a5) | 22 | 31.8% | 89.0% | 0.0586% |
| [ETH / BTCB 0.05%](https://www.geckoterminal.com/bsc/pools/0x4bba1018b967e59220b22ca03f68821a3276c9a6) | 22 | 31.8% | 85.1% | 0.0131% |
| [ETH / USDC 0.05%](https://www.geckoterminal.com/bsc/pools/0x539e0ebfffd39e54a0f7e5f8fec40ade7933a664) | 22 | 31.8% | 89.0% | 0.0586% |
| [ETH / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x62fcb3c1794fb95bd8b1a97f6ad5d8a7e4943a1e) | 22 | 45.5% | 84.4% | 0.0228% |
| [ETH / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0xd0e226f674bbf064f54ab47f42473ff80db98cba) | 22 | 45.5% | 81.8% | 0.0227% |
| [ETH / WBNB 0.25%](https://www.geckoterminal.com/bsc/pools/0x7d05c84581f0c41ad80ddf677a510360bae09a5a) | 22 | 45.5% | 83.8% | 0.0243% |
| [USDT / BTCB 0.01%](https://www.geckoterminal.com/bsc/pools/0x247f51881d1e3ae0f759afb801413a6c948ef442) | 22 | 31.8% | 89.0% | 0.0328% |
| [USDT / BTCB 0.05%](https://www.geckoterminal.com/bsc/pools/0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4) | 22 | 31.8% | 87.7% | 0.0325% |
| [USDT / USDC 0.01%](https://www.geckoterminal.com/bsc/pools/0x92b7807bf19b7dddf89b706143896d05228f3121) | 22 | 27.3% | 90.9% | 0.0000% |
| [USDT / USDC 0.05%](https://www.geckoterminal.com/bsc/pools/0x4f31fa980a675570939b737ebdde0471a4be40eb) | 22 | 0.0% | 100.0% | 0.0000% |
| [USDT / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x172fcd41e0913e95784454622d1c3724f546f849) | 22 | 31.8% | 84.4% | 0.0241% |
| [USDT / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0x36696169c63e42cd08ce11f5deebbcebae652050) | 22 | 31.8% | 84.4% | 0.0244% |