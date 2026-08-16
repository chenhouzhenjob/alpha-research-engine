from lp_backtest.models.normalize import normalize_min_max


def test_empty_list():
    assert normalize_min_max([]) == []


def test_all_identical_returns_half():
    assert normalize_min_max([5.0, 5.0, 5.0]) == [0.5, 0.5, 0.5]


def test_min_max_scaling():
    result = normalize_min_max([0.0, 5.0, 10.0])
    assert result == [0.0, 0.5, 1.0]
