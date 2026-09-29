"""估值调度：多轮读取 + 嵌套解包（纯函数，读取由注入的 `reader` 执行）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from ..decoding.models import PositionKind, PositionRef
from .models import Component, PositionValuer, StateRead, UnderlyingAmount, Valuation, ValuationRequest

# 每个持仓最多读几轮（V3 需要 2 轮：先读仓位，再读池子）；超过说明估值器的 plan 有问题
MAX_READ_ROUNDS = 4
# 嵌套解包的最大深度（设计文档 5.6）；超过的底层资产按原样保留，不再解包
MAX_NESTING = 3

# 执行一批状态读取，返回键 → 返回数据（失败为 None）。同一批必须在同一个区块上执行
Reader = Callable[[Sequence[StateRead]], dict[str, bytes | None]]


class ValuationError(RuntimeError):
    """估值器的读取计划没有收敛。"""


def _run_one(valuer: PositionValuer, request: ValuationRequest, reader: Reader) -> Valuation:
    reads: dict[str, bytes | None] = {}
    for _ in range(MAX_READ_ROUNDS):
        pending = [r for r in valuer.plan(request, reads) if r.key not in reads]
        if not pending:
            return valuer.unwrap(request, reads)
        reads.update(reader(pending))
    raise ValuationError(f"{request.position.key} 的读取计划超过 {MAX_READ_ROUNDS} 轮仍未结束")


def value_positions(
    requests: Sequence[ValuationRequest],
    valuers: Mapping[str, PositionValuer],
    reader: Reader,
    *,
    share_tokens: Mapping[str, str] | None = None,
) -> list[Valuation]:
    """估值一批持仓。

    @param valuers 实例键 → 估值器
    @param reader 执行状态读取的函数（调用方提供，负责在同一个区块上批量执行）
    @param share_tokens 份额 token 地址 → 实例键：解包出的底层资产如果在这里，说明它本身也是某个协议的份额，
        继续递归解包（例如金库份额的底层是 LP token）
    """
    share_tokens = share_tokens or {}
    out = []
    for request in requests:
        valuer = valuers.get(request.position.instance_key)
        if valuer is None:
            out.append(Valuation(request, (), error=f"实例 {request.position.instance_key} 没有估值器"))
            continue
        out.append(_expand(_run_one(valuer, request, reader), valuers, reader, share_tokens, depth=1))
    return out


def _expand(
    valuation: Valuation,
    valuers: Mapping[str, PositionValuer],
    reader: Reader,
    share_tokens: Mapping[str, str],
    depth: int,
) -> Valuation:
    """把底层资产里的份额 token 继续解包，负债和不可再解的资产原样保留。"""
    if valuation.error or depth >= MAX_NESTING:
        return valuation
    amounts: list[UnderlyingAmount] = []
    nested: dict[str, Valuation] = {}
    for amount in valuation.amounts:
        instance = share_tokens.get(amount.asset)
        valuer = valuers.get(instance) if instance else None
        if valuer is None or amount.sign < 0 or amount.amount_raw == 0:
            amounts.append(amount)
            continue
        owner = valuation.request.position.owner
        position = PositionRef(valuation.request.position.chain, instance, PositionKind.SHARE, amount.asset, owner)
        inner = _expand(
            _run_one(valuer, ValuationRequest(position, amount.amount_raw), reader),
            valuers,
            reader,
            share_tokens,
            depth + 1,
        )
        if inner.error:
            amounts.append(amount)  # 解不开就保留份额本身，不丢数量
            nested[position.key] = inner
            continue
        # 外层是本金时，里面拆出来的各部分保持原样；外层是手续费或奖励（例如以 LP 形式发放的手续费）时，
        # 里面拆出来的"本金"其实属于外层的那一类
        amounts.extend(
            replace(a, component=amount.component)
            if amount.component is not Component.PRINCIPAL and a.component is Component.PRINCIPAL
            else a
            for a in inner.amounts
        )
        nested[position.key] = inner
    extra = dict(valuation.extra)
    if nested:
        extra["nested"] = {k: v.error or "ok" for k, v in nested.items()}
    return replace(valuation, amounts=tuple(amounts), extra=extra)
