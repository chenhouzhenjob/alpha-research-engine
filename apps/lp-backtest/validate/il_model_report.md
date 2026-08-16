# 1c：IL 模型验证报告

样本数: 567；覆盖池子数: 27；T_ref = 7 天；取样跳步 = 7 天。

## 范围说明

本报告只验证 σ_price 驱动的 `ExpectedIL_ref` 模型本身的预测误差（预测 IL vs 实际发生的 IL）。没有对比 alpha-lp 现有生产代码里"原四档币对分类"的假设 IL 数值——这次调研没有在 alpha-lp 代码里定位到该分类具体的假设 IL 数值（不像 `estimateFeeDailyUsd`/`estimateCakeDaily` 那样有明确公式可查），为避免拿一个瞎猜的数字冒充对比，这部分留作后续独立调研。

## 误差分布

- 平均误差（predicted − realized）: -0.000132
- 误差标准差: 0.000410
- 平均绝对误差 (MAE): 0.000282
- 均方根误差 (RMSE): 0.000430
- 误差中位数: -0.000133
- 误差最大值 / 最小值: 0.002606 / -0.001230

正数误差 = 模型预测的 IL（更接近 0，更乐观）比实际发生的 IL 更小；负数误差 = 模型比实际更悲观。

## 结论

在这批样本上，σ_price 驱动的 `ExpectedIL_ref` 平均绝对误差约 **0.028%**，均方根误差约 **0.043%**，模型整体略偏悲观（高估实际 IL）（平均误差 -0.0132%）。对比典型 LP 一周的手续费收入通常在百分之零点几到几个百分点量级，这个误差幅度不算大，说明模型在这批池子上跟踪实际 IL 是可信的。

**但要注意样本构成的局限**：这批样本的 σ_price 分布在 0.003 ~ 0.724（平均 0.341），覆盖的是候选池目录里目前偏低到中等波动率的"蓝筹"配对（CAKE/WBNB/USDT/USDC/BTCB/ETH 白名单内部两两配对为主）。设计方案本身已经预见的局限——GBM 无漂移假设在单边行情、长尾高波动币对上会更容易失真（模型偏乐观，实际出界更快）——这批样本还没有覆盖到，不能把这个误差水平当作对所有候选池都成立的结论。等 `discover.py` 全量扫描发现更多长尾池子并且积累出足够历史后，应该重跑本验证补上这部分覆盖。

## 按池子拆分（前 20 个，按样本数排序）

| 交易对 | 样本数 | 平均绝对误差 | 平均 σ_price |
|---|---|---|---|
| [Cake / ETH 0.05%](https://www.geckoterminal.com/bsc/pools/0xeb7528398b2725e1e0374734a87320132223a5c6) | 21 | 0.000369 | 0.4092 |
| [Cake / USDT 0.05%](https://www.geckoterminal.com/bsc/pools/0x8ec186cd1ad51c380bd23fde29f852226647616c) | 21 | 0.000461 | 0.5111 |
| [Cake / USDT 0.25%](https://www.geckoterminal.com/bsc/pools/0x7f51c8aaa6b0599abd16674e2b17fec7a9f674a1) | 21 | 0.000440 | 0.5031 |
| [Cake / BTCB 0.25%](https://www.geckoterminal.com/bsc/pools/0x380a466ae6896d7d4fcd571e1e24cd5061a836b3) | 21 | 0.000318 | 0.3671 |
| [Cake / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x1e213600fa9317feac4ef4087acdf5d0e25d7187) | 21 | 0.000194 | 0.2831 |
| [Cake / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0xafb2da14056725e3ba3a30dd846b6bbbd7886c56) | 21 | 0.000195 | 0.2821 |
| [Cake / WBNB 0.25%](https://www.geckoterminal.com/bsc/pools/0x133b3d95bad5405d14d53473671200e9342896bf) | 21 | 0.000215 | 0.2879 |
| [ETH / USDT 0.01%](https://www.geckoterminal.com/bsc/pools/0x9f599f3d64a9d99ea21e68127bb6ce99f893da61) | 21 | 0.000587 | 0.5183 |
| [ETH / USDT 0.05%](https://www.geckoterminal.com/bsc/pools/0xbe141893e4c6ad9272e8c04bab7e6a10604501a5) | 21 | 0.000586 | 0.5176 |
| [ETH / BTCB 0.05%](https://www.geckoterminal.com/bsc/pools/0x4bba1018b967e59220b22ca03f68821a3276c9a6) | 21 | 0.000131 | 0.2350 |
| [ETH / USDC 0.05%](https://www.geckoterminal.com/bsc/pools/0x539e0ebfffd39e54a0f7e5f8fec40ade7933a664) | 21 | 0.000586 | 0.5181 |
| [ETH / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x62fcb3c1794fb95bd8b1a97f6ad5d8a7e4943a1e) | 21 | 0.000228 | 0.3406 |
| [ETH / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0xd0e226f674bbf064f54ab47f42473ff80db98cba) | 21 | 0.000227 | 0.3409 |
| [ETH / WBNB 0.25%](https://www.geckoterminal.com/bsc/pools/0x7d05c84581f0c41ad80ddf677a510360bae09a5a) | 21 | 0.000243 | 0.3449 |
| [USDT / BTCB 0.01%](https://www.geckoterminal.com/bsc/pools/0x247f51881d1e3ae0f759afb801413a6c948ef442) | 21 | 0.000328 | 0.3764 |
| [USDT / BTCB 0.05%](https://www.geckoterminal.com/bsc/pools/0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4) | 21 | 0.000325 | 0.3759 |
| [USDT / USDC 0.01%](https://www.geckoterminal.com/bsc/pools/0x92b7807bf19b7dddf89b706143896d05228f3121) | 21 | 0.000000 | 0.0046 |
| [USDT / USDC 0.05%](https://www.geckoterminal.com/bsc/pools/0x4f31fa980a675570939b737ebdde0471a4be40eb) | 21 | 0.000000 | 0.0133 |
| [USDT / WBNB 0.01%](https://www.geckoterminal.com/bsc/pools/0x172fcd41e0913e95784454622d1c3724f546f849) | 21 | 0.000241 | 0.3765 |
| [USDT / WBNB 0.05%](https://www.geckoterminal.com/bsc/pools/0x36696169c63e42cd08ce11f5deebbcebae652050) | 21 | 0.000244 | 0.3765 |