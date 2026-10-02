from datetime import UTC, datetime, timedelta

from alpha_metrics.features.age_penalty import age_penalty


def test_unavailable_when_created_at_unknown():
    result = age_penalty(None, datetime.now(UTC))
    assert not result.is_available


def test_penalty_applied_for_new_pool():
    now = datetime.now(UTC)
    result = age_penalty(now - timedelta(days=10), now)
    assert result.is_available
    assert result.value == 0.05


def test_no_penalty_for_old_pool():
    now = datetime.now(UTC)
    result = age_penalty(now - timedelta(days=40), now)
    assert result.is_available
    assert result.value == 0.0
