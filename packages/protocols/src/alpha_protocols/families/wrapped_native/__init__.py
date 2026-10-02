"""包装原生币家族（WBNB、WETH 等 WETH9 实现）。"""

from __future__ import annotations

from ..base import ProtocolFamily
from .decoder import DEPOSIT, WITHDRAWAL, WrappedNativeDecoder


class WrappedNativeFamily(ProtocolFamily):
    key = "wrapped_native"
    version = 1
    roles = frozenset({"wrapped"})  # 包装原生币合约
    signature_topics = frozenset({DEPOSIT, WITHDRAWAL})

    @classmethod
    def decoder(cls, deployment):
        return WrappedNativeDecoder(
            family=cls.key,
            instance_key=deployment.instance_key,
            decoder_version=cls.decoder_version(),
        )
