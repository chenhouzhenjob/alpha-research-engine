"""协议家族注册表。新增一个家族：在 `families/<family>/` 实现，再在这里加一行。"""

from __future__ import annotations

from .base import ProtocolFamily
from .uniswap_v2_like import UniswapV2Family
from .uniswap_v3_like import UniswapV3Family
from .wrapped_native import WrappedNativeFamily

# 家族键 → 家族。首批家族随各自的步骤加入（规划第 8 节）。
FAMILIES: dict[str, type[ProtocolFamily]] = {
    WrappedNativeFamily.key: WrappedNativeFamily,
    UniswapV2Family.key: UniswapV2Family,
    UniswapV3Family.key: UniswapV3Family,
}
