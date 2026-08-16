from lp_backtest.features.vt_ratio import vt_ratio


def test_unavailable_when_inputs_missing():
    assert not vt_ratio(None, 1000.0).is_available
    assert not vt_ratio(100.0, None).is_available


def test_unavailable_when_tvl_non_positive():
    assert not vt_ratio(100.0, 0.0).is_available


def test_computes_ratio():
    result = vt_ratio(100.0, 1000.0)
    assert result.is_available
    assert result.value == 0.1
