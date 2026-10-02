"""`_collect_rationale` 是纯函数（只读 `PoolDailyMetrics` 里各 `MetricValue` 的可用性/reason，
不碰数据库/链上状态），直接构造一个手工的 `PoolDailyMetrics` 做测试，不需要真实环境。
"""

from datetime import date

from alpha_core.metrics import MetricValue
from alpha_core.types import Chain, DepthTier
from alpha_metrics.compute import PoolDailyMetrics
from live_signal.main import _collect_rationale


def _make_metrics(**overrides: MetricValue) -> PoolDailyMetrics:
    defaults = {
        "fee_apr": MetricValue.available(0.1),
        "cake_apr": MetricValue.no_incentive(),
        "nominal_apr": MetricValue.available(0.1),
        "vt_ratio": MetricValue.available(1.0),
        "sigma_price": MetricValue.available(0.01),
        "expected_il_ref": MetricValue.available(-0.01),
        "capital_volatility": MetricValue.unavailable("TVL 历史样本不足 14 天"),
        "age_penalty": MetricValue.available(0.0),
        "depth_tier": MetricValue.available(DepthTier.MEDIUM),
    }
    defaults.update(overrides)
    return PoolDailyMetrics(
        chain=Chain.BSC,
        pool_address="0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4",
        as_of=date(2026, 8, 16),
        **defaults,
    )


def test_collect_rationale_only_includes_unavailable_reasons():
    metrics = _make_metrics()
    rationale = _collect_rationale(metrics)
    assert rationale == ["TVL 历史样本不足 14 天"]


def test_collect_rationale_excludes_no_incentive_since_it_is_a_confirmed_state_not_missing_data():
    # NO_INCENTIVE（"确认当前无激励"）不是 UNAVAILABLE（"取不到"），不该出现在 rationale 里——
    # 这是三态约定的核心区别，见 alpha_core.metrics 的文档。其余字段都设为 available，
    # 确保结果为空真的是因为 NO_INCENTIVE 被排除，不是巧合。
    metrics = _make_metrics(
        cake_apr=MetricValue.no_incentive("该池没有挂 CAKE farm"),
        capital_volatility=MetricValue.available(0.05),
    )
    rationale = _collect_rationale(metrics)
    assert rationale == []


def test_collect_rationale_returns_empty_when_everything_available():
    metrics = _make_metrics(capital_volatility=MetricValue.available(0.05))
    assert _collect_rationale(metrics) == []
