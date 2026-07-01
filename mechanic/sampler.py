"""The sampler daemon.

Single writer to the store. Walks the available sensors once per interval, calls
sample(), and persists. Per-sensor errors are isolated so one flaky backend can never
stop the loop. Shuts down cleanly on SIGTERM/SIGINT (finishes the current cycle, exits 0).

Kept deliberately small: all the interesting logic lives in the sensors, the store,
and the baseline. This module is just the heartbeat.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Iterable

from mechanic.plugins.base import SensorError, SensorPlugin

log = logging.getLogger("mechanic.sampler")


class Sampler:
    def __init__(
        self,
        store,
        sensors: Iterable[SensorPlugin] | None = None,
        interval: float | None = None,
        enabled: list[str] | None = None,
    ):
        # Resolve sensors: explicit list, or auto-discover from the registry.
        if sensors is None:
            from mechanic.plugins import registry as reg

            sensors = reg.all()
        self.sensors = list(sensors)
        self.store = store
        self.interval = (
            interval if interval is not None
            else getattr(store.config, "interval_seconds", 30.0)
        )
        self.enabled = (
            list(enabled) if enabled is not None
            else list(getattr(store.config, "enabled_sensors", []) or [])
        )
        self._stop = threading.Event()
        self._installed_handlers = False
        self._total_written = 0

    # ---- one-shot ---------------------------------------------------------

    def run_once(self) -> int:
        """Sample every available sensor once. Returns the number of samples written."""
        written = 0
        for sensor in self.sensors:
            if self.enabled and sensor.name not in self.enabled:
                continue
            if not sensor.is_available():
                log.debug("skipping unavailable sensor: %s", sensor.name)
                continue
            try:
                payload = sensor.sample()
            except SensorError as exc:
                log.warning("sensor %s error: %s", sensor.name, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - never let one sensor kill the loop
                log.warning("sensor %s raised unexpectedly: %s", sensor.name, exc)
                continue
            try:
                self.store.write_sample(sensor.name, payload)
                written += 1
                self._total_written += 1
            except Exception as exc:  # noqa: BLE001 - storage failure is also isolated
                log.error("failed to store sample for %s: %s", sensor.name, exc)
        return written

    # ---- loop --------------------------------------------------------------

    def run(self) -> None:
        """Sample forever at self.interval until stop() or a signal."""
        self._install_signal_handlers()
        sensor_names = [s.name for s in self.sensors]
        log.info("sampler starting; interval=%.1fs, sensors=%s", self.interval, sensor_names)
        while not self._stop.is_set():
            self.run_once()
            # event.wait returns True if set during the wait → exit immediately
            if self._stop.wait(self.interval):
                break
        log.info("sampler stopped; wrote %d samples this run", self._total_written)

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        if self._installed_handlers:
            return
        # Only install when running on the main thread; signal.raise_signal from a
        # worker (as in tests) still works because the handler is registered on main.
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
            self._installed_handlers = True
        except (ValueError, OSError):  # not main thread → rely on stop() instead
            log.debug("could not install signal handlers (not main thread)")

    def _on_signal(self, signum, frame):  # noqa: ANN001
        log.info("received signal %d, stopping after current cycle", signum)
        self._stop.set()
