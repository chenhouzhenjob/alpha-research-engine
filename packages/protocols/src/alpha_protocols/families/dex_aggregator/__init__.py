"""DEX 聚合器家族：路由细节不透明，按钱包净额把一笔交换归并成 trade。"""

from __future__ import annotations

from pydantic import Field

from ..base import FamilyOptions, ProtocolFamily
from .decoder import DexAggregatorDecoder


class DexAggregatorOptions(FamilyOptions):
    """聚合器实例的配置项。"""

    # 表示"交换"的方法选择器（小写、带 0x）；其他方法（授权、签到等）不归并成 trade
    swap_selectors: list[str] = Field(min_length=1)


class DexAggregatorFamily(ProtocolFamily):
    key = "dex_aggregator"
    version = 1
    roles = frozenset({"router"})  # 聚合器的交换入口合约
    options_model = DexAggregatorOptions

    @classmethod
    def decoder(cls, deployment):
        return DexAggregatorDecoder(
            family=cls.key,
            instance_key=deployment.instance_key,
            decoder_version=cls.decoder_version(),
            routers=frozenset(deployment.roles["router"]),
            swap_selectors=frozenset(s.lower() for s in deployment.options.swap_selectors),
        )
