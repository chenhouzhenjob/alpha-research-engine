"""最小纸面经纪商；真实交易所客户端应放在 integrations/venues。"""

from __future__ import annotations

from alpha.execution.models import OrderRequest, OrderStatus, RiskLimits


class PaperBroker:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def validate(self, order: OrderRequest) -> OrderStatus:
        if abs(order.target_weight) > self.limits.max_gross_leverage:
            return OrderStatus.REJECTED
        if order.reference_price <= 0:
            return OrderStatus.SKIPPED_NO_PRICE
        return OrderStatus.FILLED
