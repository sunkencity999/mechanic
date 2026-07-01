"""Tests for the baseline/anomaly engine.

Pure math: rolling window + Welford mean/std + EWMA + anomaly decision. No I/O, no
sensors, no sqlite. This is the part of Mechanic that has to be correct above all.
"""

import pytest

from mechanic.baseline import Baseline


def test_cold_start_never_anomalous():
    b = Baseline(min_samples=30)
    for i in range(29):
        r = b.update(float(i))
        assert r.is_anomaly is False
        assert r.n == i + 1
    # 30th update crosses the threshold; a value equal to the running mean is normal
    r = b.update(14.0)
    assert r.n == 30
    assert r.is_anomaly is False


def test_anomaly_detected_when_far_from_mean():
    b = Baseline(min_samples=30, z_threshold=3.0)
    # warm up with a tight cluster around 50
    for _ in range(100):
        b.update(50.0)
    # a spike far outside 3 std (std ~0 here) is anomalous
    r = b.update(500.0)
    assert r.is_anomaly is True
    assert r.z_score > 3.0


def test_normal_value_not_flagged():
    b = Baseline(min_samples=30, z_threshold=3.0)
    for _ in range(100):
        b.update(50.0)
    r = b.update(50.5)
    assert r.is_anomaly is False


def test_welford_handles_outlier_without_poisoning():
    """A single extreme value must not permanently skew the rolling mean/std."""
    b = Baseline(min_samples=10, z_threshold=3.0, window_size=100)
    for _ in range(50):
        b.update(10.0)
    # one spike
    spike = b.update(1000.0)
    assert spike.is_anomaly is True
    # the very next normal value should not still be flagged as anomalous once the
    # spike has rolled out of the statistics — feed enough normal samples to flush it
    for _ in range(100):
        b.update(10.0)
    r = b.update(10.0)
    assert r.is_anomaly is False


def test_window_size_bounds_memory():
    b = Baseline(window_size=10, min_samples=5)
    for i in range(50):
        b.update(float(i))
    assert b.n <= 10


def test_ewma_tracks_recent_values():
    b = Baseline(min_samples=5, ewma_alpha=0.5, window_size=1000)
    for _ in range(20):
        b.update(0.0)
    # ewma should now be ~0
    b.update(100.0)
    # with alpha=0.5 the ewma jumps toward 100 quickly but is still < 100
    assert 0 < b.ewma < 100
    # after several more 100s it should converge toward 100
    for _ in range(20):
        b.update(100.0)
    assert b.ewma > 90


def test_z_score_computed_correctly():
    b = Baseline(min_samples=5, z_threshold=3.0)
    # mean=100, controlled spread
    for _ in range(50):
        b.update(100.0)
    r = b.update(100.0)
    assert r.mean == pytest.approx(100.0, abs=1e-6)
    # std is ~0 for constant input; z_score should be 0 (guarded against div by zero)
    assert r.z_score == 0.0 or r.std == 0


def test_update_result_carries_all_fields():
    b = Baseline(min_samples=3)
    r = b.update(5.0)
    assert hasattr(r, "value")
    assert hasattr(r, "is_anomaly")
    assert hasattr(r, "z_score")
    assert hasattr(r, "mean")
    assert hasattr(r, "std")
    assert hasattr(r, "n")
    assert hasattr(r, "ewma")
    assert r.value == 5.0


def test_multiple_metrics_independent():
    b = Baseline(min_samples=5)
    # feed two distinct metric streams
    for _ in range(20):
        b.update("cpu", 10.0)
        b.update("mem", 90.0)
    rc = b.update("cpu", 10.0)
    rm = b.update("mem", 90.0)
    assert rc.mean == pytest.approx(10.0, abs=1e-6)
    assert rm.mean == pytest.approx(90.0, abs=1e-6)
    assert not rc.is_anomaly and not rm.is_anomaly


def test_unknown_metric_starts_cold():
    b = Baseline(min_samples=5)
    r = b.update("newmetric", 42.0)
    assert r.n == 1
    assert r.is_anomaly is False


# ---- evaluate() (non-mutating) ----------------------------------------------


def test_evaluate_does_not_mutate_baseline():
    """evaluate() judges a value against current stats WITHOUT adding it."""
    b = Baseline(min_samples=10, z_threshold=3.0)
    for _ in range(50):
        b.update("cpu", 10.0)
    n_before = b.stats_for("cpu").n
    assert n_before == 50
    # evaluate a spike — must NOT change n
    r = b.evaluate("cpu", 999.0)
    assert b.stats_for("cpu").n == n_before
    assert r.n == n_before  # n reflects history, not the queried value
    assert r.is_anomaly is True


def test_evaluate_normal_value_not_flagged():
    b = Baseline(min_samples=10, z_threshold=3.0)
    for _ in range(50):
        b.update("cpu", 10.0)
    r = b.evaluate("cpu", 10.0)
    assert r.is_anomaly is False
    assert r.n == 50


def test_evaluate_jitter_off_constant_not_flagged():
    b = Baseline(min_samples=10, z_threshold=3.0)
    for _ in range(50):
        b.update("cpu", 10.0)
    r = b.evaluate("cpu", 10.5)
    assert r.is_anomaly is False


def test_evaluate_cold_start_metric():
    b = Baseline(min_samples=30)
    r = b.evaluate("never_seen", 42.0)
    assert r.n == 0
    assert r.is_anomaly is False
    assert r.cold_start is True


def test_update_and_evaluate_agree_on_warm_data():
    """For warm data, update() and evaluate() should reach the same verdict for the
    same value (they share the decision logic); update just also persists the value."""
    b = Baseline(min_samples=10, z_threshold=3.0)
    for _ in range(40):
        b.update("cpu", 10.0)
    # snapshot stats, then compare verdicts on the same test value
    ev = b.evaluate("cpu", 10.0)
    up = b.update("cpu", 10.0)
    assert ev.is_anomaly == up.is_anomaly
    # means should agree (both computed from the same history)
    assert ev.mean == pytest.approx(up.mean, abs=1e-6)

