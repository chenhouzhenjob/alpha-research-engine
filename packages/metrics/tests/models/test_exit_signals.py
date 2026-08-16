"""退出信号模型的单测：5 条信号逐条验证"正常算出来"/"数据不够 unavailable"/
"仓位参数缺失 unavailable"三类用例，外加编排入口 `compute_exit_signals` 和
`any_signal_fired` 的组合场景。手工构造 `PoolDailyMetrics`/`PoolMetricsHistoryRow`，
不碰数据库。
"""

from datetime import date, datetime

from alpha_core.metrics import MetricValue
from alpha_core.types import Chain, DepthTier
from alpha_metrics.compute import PoolDailyMetrics
from alpha_metrics.models.exit_signals import (
    COMPOSITE_RANK_PERCENTILE_THRESHOLD,
    VTRATIO_MA7_DROP_THRESHOLD,
    any_signal_fired,
    compute_exit_signals,
)
from alpha_metrics.models.il_model import realized_il_from_price_ratio
from alpha_storage.models import PoolMetricsHistoryRow


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


def _history_row(day: int, *, volume_24h_usd: float, tvl_usd: float) -> PoolMetricsHistoryRow:
    return PoolMetricsHistoryRow(
        chain=Chain.BSC.value,
        pool_address="0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4",
        snapshot_date=date(2026, 8, day),
        tvl_usd=tvl_usd,
        volume_24h_usd=volume_24h_usd,
        close_price=1.0,
        data_source="geckoterminal",
        fetched_at=datetime(2026, 8, day, tzinfo=None),
    )


_NEUTRAL_PERCENTILE = MetricValue.available(0.9)  # 默认给一个不会触发信号 4 的值，避免互相干扰


def _compute(**kwargs):
    defaults = {
        "daily_metrics": _make_metrics(),
        "history": [],
        "current_price": None,
        "composite_rank_percentile": _NEUTRAL_PERCENTILE,
        "position_open_vtratio_ma7": None,
        "position_open_price": None,
        "position_open_value_usd": None,
        "position_cumulative_fees_usd": None,
    }
    defaults.update(kwargs)
    return compute_exit_signals(**defaults)


def test_net_edge_negative_when_income_below_il_loss():
    # NominalAPR=5%/年，7 天参考期收益 ≈ 0.000959；ExpectedIL_ref=-1% → net_edge 为负。
    metrics = _make_metrics(nominal_apr=MetricValue.available(0.05), expected_il_ref=MetricValue.available(-0.01))
    result = _compute(daily_metrics=metrics)
    assert result.net_edge_negative.is_available
    assert result.net_edge_negative.value is True
    assert result.net_edge_value.value < 0


def test_net_edge_positive_when_income_exceeds_il_loss():
    # NominalAPR=100%/年，7 天参考期收益 ≈ 0.0192；ExpectedIL_ref=-0.1% → net_edge 为正。
    metrics = _make_metrics(nominal_apr=MetricValue.available(1.0), expected_il_ref=MetricValue.available(-0.001))
    result = _compute(daily_metrics=metrics)
    assert result.net_edge_negative.value is False
    assert result.net_edge_value.value > 0


def test_net_edge_unavailable_when_nominal_apr_unavailable():
    metrics = _make_metrics(nominal_apr=MetricValue.unavailable("feeProtocol 读不到"))
    result = _compute(daily_metrics=metrics)
    assert not result.net_edge_negative.is_available
    assert not result.net_edge_value.is_available


def test_cake_incentive_withdrawn_true_when_no_incentive():
    result = _compute(daily_metrics=_make_metrics(cake_apr=MetricValue.no_incentive()))
    assert result.cake_incentive_withdrawn.value is True


def test_cake_incentive_withdrawn_false_when_available():
    result = _compute(daily_metrics=_make_metrics(cake_apr=MetricValue.available(0.05)))
    assert result.cake_incentive_withdrawn.value is False


def test_cake_incentive_withdrawn_unavailable_when_read_fails():
    result = _compute(daily_metrics=_make_metrics(cake_apr=MetricValue.unavailable("链上调用失败")))
    assert not result.cake_incentive_withdrawn.is_available


def test_vtratio_ma7_drop_pct_unavailable_without_position_baseline():
    history = [_history_row(d, volume_24h_usd=100.0, tvl_usd=1000.0) for d in range(1, 8)]
    result = _compute(history=history, position_open_vtratio_ma7=None)
    assert not result.vtratio_ma7_drop_pct.is_available


def test_vtratio_ma7_drop_pct_computes_relative_change():
    # 7 天 VTRatio 恒为 0.1（100/1000），当前 MA7=0.1；开仓时基线 0.2 → 下降 50%。
    history = [_history_row(d, volume_24h_usd=100.0, tvl_usd=1000.0) for d in range(1, 8)]
    result = _compute(history=history, position_open_vtratio_ma7=0.2)
    assert result.vtratio_ma7_drop_pct.is_available
    assert result.vtratio_ma7_drop_pct.value == -0.5
    assert result.vtratio_ma7_drop_pct.value <= VTRATIO_MA7_DROP_THRESHOLD


def test_cumulative_realized_il_unavailable_without_position_baseline():
    result = _compute(current_price=100.0)
    assert not result.cumulative_realized_il_exceeds_fees.is_available
    assert not result.realized_il_usd.is_available


def test_cumulative_realized_il_unavailable_without_current_price():
    result = _compute(
        position_open_price=100.0, position_open_value_usd=1000.0, position_cumulative_fees_usd=5.0
    )
    assert not result.cumulative_realized_il_exceeds_fees.is_available


def test_cumulative_realized_il_fires_when_loss_exceeds_fees():
    # 价格从 100 跌到 80，price_ratio=0.8，IL% = realized_il_from_price_ratio(0.8)（对称，跟传 1.25 一样）。
    price_ratio = 80.0 / 100.0
    expected_il_usd = abs(realized_il_from_price_ratio(price_ratio)) * 1000.0
    result = _compute(
        current_price=80.0,
        position_open_price=100.0,
        position_open_value_usd=1000.0,
        position_cumulative_fees_usd=1.0,  # 远小于 IL，触发信号
    )
    assert result.realized_il_usd.value == expected_il_usd
    assert result.cumulative_realized_il_exceeds_fees.value is True


def test_cumulative_realized_il_does_not_fire_when_fees_cover_loss():
    result = _compute(
        current_price=80.0,
        position_open_price=100.0,
        position_open_value_usd=1000.0,
        position_cumulative_fees_usd=1_000_000.0,  # 远大于 IL，不触发
    )
    assert result.cumulative_realized_il_exceeds_fees.value is False


def test_any_signal_fired_true_when_net_edge_negative():
    metrics = _make_metrics(
        nominal_apr=MetricValue.available(0.01), expected_il_ref=MetricValue.available(-0.05)
    )
    result = _compute(daily_metrics=metrics)
    assert any_signal_fired(result) is True


def test_any_signal_fired_false_when_nothing_fires():
    metrics = _make_metrics(
        nominal_apr=MetricValue.available(1.0),
        expected_il_ref=MetricValue.available(-0.001),
        cake_apr=MetricValue.available(0.05),  # 默认的 no_incentive() 会误触发信号 2，这里显式给它一个激励值
    )
    result = _compute(daily_metrics=metrics)
    assert any_signal_fired(result) is False


def test_any_signal_fired_true_when_composite_rank_below_threshold():
    below_threshold = MetricValue.available(COMPOSITE_RANK_PERCENTILE_THRESHOLD - 0.01)
    metrics = _make_metrics(nominal_apr=MetricValue.available(1.0), expected_il_ref=MetricValue.available(-0.001))
    result = _compute(daily_metrics=metrics, composite_rank_percentile=below_threshold)
    assert any_signal_fired(result) is True


def test_any_signal_fired_ignores_unavailable_signals():
    # 全部信号不可用（没传任何仓位参数、composite_rank_percentile 本身不可用）时不应该误报触发。
    metrics = _make_metrics(
        nominal_apr=MetricValue.unavailable("x"),
        expected_il_ref=MetricValue.unavailable("x"),
        cake_apr=MetricValue.unavailable("x"),
    )
    result = _compute(daily_metrics=metrics, composite_rank_percentile=MetricValue.unavailable("候选池数量 < 2"))
    assert any_signal_fired(result) is False
