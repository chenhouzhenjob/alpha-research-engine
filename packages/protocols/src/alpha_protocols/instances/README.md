# 协议实例配置

一个协议一份 `<instance_key>.yaml`，文件名等于 `instance_key`。格式和校验规则见
`alpha_protocols/config/instances.py`，设计见 `research/docs/wallet-analyzer-M2-协议解码核心实施规划.md` 5.10。

```yaml
instance_key: uniswap-v2        # 全局唯一
family: uniswap_v2_like         # 已注册的家族键
protocol: Uniswap
version: v2
options: {...}                  # 各链共用的配置项，按家族的 options_model 校验
deployments:
  ethereum:
    roles: {factory: ["0x..."], router: ["0x..."]}   # 地址小写；可写 $chain.wrapped_native 引用链画像
    options: {...}              # 可选：覆盖共用配置项
    from_block: 10000835        # 可选：部署区块
```

新增一个已有家族的分叉：只加一份 YAML 和金标准样本，不改代码。
