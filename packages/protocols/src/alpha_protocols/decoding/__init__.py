"""alpha_protocols.decoding：两段式解码框架（规划 5.4）。

与链无关的部分（模型、分类表、整合原语、风险标记）放在本目录；从 EVM 回执提取资产流水等
EVM 专用逻辑放在 `decoding/evm/`。本目录不得 import `decoding/evm/` 和任何家族（见 tests/test_architecture.py）。
"""
