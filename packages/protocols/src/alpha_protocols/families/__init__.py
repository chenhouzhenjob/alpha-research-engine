"""协议家族注册表。新增一个家族：在 `families/<family>/` 实现，再在这里加一行。"""

from __future__ import annotations

from .base import ProtocolFamily

# 家族键 → 家族。首批家族随各自的步骤加入（规划第 8 节）。
FAMILIES: dict[str, type[ProtocolFamily]] = {}
