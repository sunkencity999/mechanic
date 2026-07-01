"""Tests for the sampler daemon loop.

The sampler is the glue: it walks available sensors, calls sample(), and writes to the
store. Key invariants: unavailable sensors are skipped, one sensor's failure never
stops the loop, the interval is honored, and SIGTERM/SIGINT trigger a clean exit.
"""

import signal
import threading
import time

from mechanic.plugins.base import SensorError
from mechanic.sampler import Sampler


class FakeSensor:
    """Minimal fake sensor for loop tests."""

    def __init__(self, name, available=True, payload=None, raises=None):
        self.name = name
        self._available = available
        self.payload = payload if payload is not None else {"v": 1}
        self.raises = raises
        self.sample_count = 0

    def is_available(self):
        return self._available

    def sample(self):
        self.sample_count += 1
        if self.raises:
            raise self.raises
        return dict(self.payload)


def test_run_once_collects_from_available_sensors(config):
    s = Store_stub(config)
    store = s  # alias for clarity
    sampler = Sampler(store, sensors=[FakeSensor("a"), FakeSensor("b", payload={"x": 2})])
    n = sampler.run_once()
    assert n == 2
    assert store.count("a") == 1
    assert store.count("b") == 1


def test_run_once_skips_unavailable_sensors(config):
    store = Store_stub(config)
    sampler = Sampler(store, sensors=[FakeSensor("a"), FakeSensor("b", available=False)])
    sampler.run_once()
    assert store.count("a") == 1
    assert store.count("b") == 0


def test_one_sensor_failure_does_not_stop_loop(config):
    store = Store_stub(config)
    bad = FakeSensor("bad", raises=SensorError("boom"))
    good = FakeSensor("good")
    sampler = Sampler(store, sensors=[bad, good])
    n = sampler.run_once()
    assert n == 1  # only the good sensor contributed
    assert store.count("bad") == 0
    assert store.count("good") == 1


def test_unexpected_exception_also_isolated(config):
    """Even a non-SensorError exception shouldn't kill the loop."""
    store = Store_stub(config)
    bad = FakeSensor("bad", raises=ValueError("unexpected"))
    good = FakeSensor("good")
    sampler = Sampler(store, sensors=[bad, good])
    n = sampler.run_once()
    assert n == 1
    assert store.count("good") == 1


def test_enabled_sensors_filter(config):
    """If config.enabled_sensors is set, only those are sampled."""
    store = Store_stub(config)
    config.enabled_sensors = ["a"]
    sampler = Sampler(
        store,
        sensors=[FakeSensor("a"), FakeSensor("b"), FakeSensor("c")],
        enabled=["a"],
    )
    n = sampler.run_once()
    assert n == 1
    assert store.count("a") == 1
    assert store.count("b") == 0


def test_loop_honors_interval_and_stops(config):
    """run() should sample at interval_seconds and return when stop() is called."""
    store = Store_stub(config)
    sensor = FakeSensor("a")
    sampler = Sampler(store, sensors=[sensor], interval=0.05)

    t = threading.Timer(0.18, sampler.stop)
    t.start()
    start = time.time()
    sampler.run()
    elapsed = time.time() - start
    t.join()

    # at 50ms interval over ~180ms we expect ~3-4 cycles. Assert >= 2 (loose, CI-safe).
    assert sensor.sample_count >= 2
    assert elapsed < 1.5


def test_loop_finishes_cycle_on_sigterm(config):
    """SIGTERM should set the stop flag so the loop exits cleanly."""
    store = Store_stub(config)
    sensor = FakeSensor("a")
    sampler = Sampler(store, sensors=[sensor], interval=0.05)

    # install the sampler's signal handler, then raise SIGTERM from a thread.
    sampler._install_signal_handlers()
    t = threading.Timer(0.12, lambda: _raise_signal(signal.SIGTERM))
    t.start()
    sampler.run()
    t.join()
    assert sensor.sample_count >= 1


# ---- helpers ----------------------------------------------------------------


class Store_stub:
    """Minimal store stub that records writes — decouples sampler tests from SQLite."""

    def __init__(self, config):
        self.config = config
        self._counts: dict[str, int] = {}

    def open(self):
        return self

    def close(self):
        pass

    def write_sample(self, sensor, payload):
        self._counts[sensor] = self._counts.get(sensor, 0) + 1
        return self._counts[sensor]

    def count(self, sensor):
        return self._counts.get(sensor, 0)


def _raise_signal(sig):
    import os

    os.kill(os.getpid(), sig)
