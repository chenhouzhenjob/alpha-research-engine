"""流水账：家族推断流水、认领流水的地方（与链无关，规划 5.4 的认领机制和 5.5 的推断钩子）。

一笔交易解码时只有一本流水账。第一段产出的流水先入账；家族可以：

- **推断**：依据自己的事件确定性地补上链上没有转账日志的流水（例如 WBNB 解包转给钱包的原生币、
  包装时钱包收到的 WBNB）。推断出的流水追加在账尾，`source = inferred`，带着证据日志的序号；
- **认领**：声明某几条流水属于自己产出的事件。同一条流水只能被认领一次，冲突时抛 `DecodeConflictError`，
  在测试里暴露，不在运行时静默挑一个。

数据源给了内部交易时（`internal_available=True`），原生币以数据源为准：推断原生币改为匹配已有的内部流水，
匹配不上说明推断规则和数据源不一致，记告警、不补流水，避免重复计算。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .models import AssetFlow, AssetFlowKind, DecodeWarning, FlowSource, WarningCode


class DecodeConflictError(RuntimeError):
    """两个解码器认领了同一条流水：说明分派规则或家族的认领条件有重叠。"""


class FlowLedger:
    def __init__(self, flows: Iterable[AssetFlow], *, internal_available: bool) -> None:
        """
        @param flows 第一段产出的流水
        @param internal_available 数据源是否给了内部交易（给了空列表也算给了）
        """
        self._flows: list[AssetFlow] = list(flows)
        self._owner: dict[int, str] = {}
        self.internal_available = internal_available
        self.warnings: list[DecodeWarning] = []

    @property
    def flows(self) -> tuple[AssetFlow, ...]:
        return tuple(self._flows)

    def get(self, flow_id: int) -> AssetFlow:
        return self._flows[flow_id]

    def is_claimed(self, flow_id: int) -> bool:
        return flow_id in self._owner

    def unclaimed(self) -> list[AssetFlow]:
        return [f for f in self._flows if f.flow_id not in self._owner]

    def find(self, predicate: Callable[[AssetFlow], bool], *, include_claimed: bool = False) -> list[AssetFlow]:
        """按条件查找流水，默认只看未认领的。"""
        return [f for f in self._flows if (include_claimed or f.flow_id not in self._owner) and predicate(f)]

    def claim(self, flow_ids: Iterable[int], owner: str) -> None:
        """认领流水。

        @param owner 认领方（家族的 decoder_version），写进冲突信息
        @raises DecodeConflictError 有流水已经被认领
        """
        ids = list(flow_ids)
        for i in ids:
            if i in self._owner:
                raise DecodeConflictError(f"流水 {i} 已被 {self._owner[i]} 认领，{owner} 又认领了一次")
        for i in ids:
            self._owner[i] = owner

    def infer(
        self,
        kind: AssetFlowKind,
        asset: str,
        amount_raw: int,
        from_address: str,
        to_address: str,
        *,
        evidence_log_index: int,
        token_id: int | None = None,
    ) -> int | None:
        """补一条推断出的流水，返回它的 flow_id。

        数据源给了内部交易且推断的是原生币时，改为匹配一条未认领的内部流水（同付款方、收款方、数量），
        返回它的 flow_id；匹配不上返回 None 并记告警。

        @param evidence_log_index 推断依据的日志序号，决定事件在交易里的位置
        """
        if kind is AssetFlowKind.NATIVE and self.internal_available:
            matches = self.find(
                lambda f: (
                    f.source is FlowSource.INTERNAL
                    and (f.from_address, f.to_address, f.amount_raw) == (from_address, to_address, amount_raw)
                )
            )
            if matches:
                return matches[0].flow_id
            self.warnings.append(
                DecodeWarning(
                    WarningCode.INFERENCE_MISMATCH,
                    f"日志 {evidence_log_index} 推断 {from_address}→{to_address} 原生币 {amount_raw}，"
                    "数据源没有对应的内部交易",
                )
            )
            return None
        flow = AssetFlow(
            len(self._flows),
            kind,
            asset,
            amount_raw,
            from_address,
            to_address,
            FlowSource.INFERRED,
            token_id=token_id,
            log_index=evidence_log_index,
        )
        self._flows.append(flow)
        return flow.flow_id
