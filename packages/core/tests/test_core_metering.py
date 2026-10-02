from alpha_core.metering import CallStatus, InMemoryCallMeter, NullCallMeter


def test_records_aggregate_by_provider_method_and_status():
    meter = InMemoryCallMeter(app="test")
    meter.record("nodereal", "eth_call", cu=20)
    meter.record("nodereal", "eth_call", count=2, cu=40)
    meter.record("nodereal", "eth_call", cu=20, status=CallStatus.RATE_LIMITED)

    totals = {(k.method, k.status): v for k, v in meter.snapshot().items()}
    assert totals[("eth_call", CallStatus.OK)].call_count == 3
    assert totals[("eth_call", CallStatus.OK)].est_cu == 60
    assert totals[("eth_call", CallStatus.RATE_LIMITED)].call_count == 1


def test_unknown_cu_marks_total_incomplete():
    meter = InMemoryCallMeter(app="test")
    meter.record("nodereal", "eth_foo", cu=10)
    meter.record("nodereal", "eth_foo", cu=None)
    (total,) = meter.snapshot().values()
    assert total.call_count == 2
    assert total.est_cu is None


def test_job_ref_is_part_of_key():
    meter = InMemoryCallMeter(app="test", job_ref="job:1")
    meter.record("nodereal", "eth_call", cu=20)
    meter.set_job_ref("job:2")
    meter.record("nodereal", "eth_call", cu=20)
    assert {k.job_ref for k in meter.snapshot()} == {"job:1", "job:2"}


def test_drain_clears_and_restore_adds_back():
    meter = InMemoryCallMeter(app="test")
    meter.record("nodereal", "eth_call", cu=20)
    drained = meter.drain()
    assert meter.snapshot() == {}
    meter.record("nodereal", "eth_call", cu=20)
    meter.restore(drained)
    (total,) = meter.snapshot().values()
    assert total.call_count == 2
    assert total.est_cu == 40


def test_null_meter_accepts_calls():
    NullCallMeter().record("x", "y", count=3, cu=None, status=CallStatus.ERROR)
