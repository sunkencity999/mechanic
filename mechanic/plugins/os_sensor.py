"""OS sensor — system & per-process metrics via psutil.

Always available (psutil is a hard dependency). Produces a flat dict of host-level
metrics: CPU, memory, load average, process count, disk, net. Per-process top-N is
optional and kept small to bound the sample size.
"""

from __future__ import annotations

import time

import psutil

from mechanic.plugins.base import SensorError

_SENSOR_NAME = "os"


class OsSensor:
    name = _SENSOR_NAME

    def is_available(self) -> bool:
        # psutil is a hard dep; if it's broken the whole package is broken.
        try:
            import psutil  # noqa: F401
        except ImportError:  # pragma: no cover - defensive; psutil is required
            return False
        return True

    def sample(self) -> dict:
        try:
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            sm = psutil.swap_memory()
            load = self._load_avg()
            n_procs = len(psutil.pids())
            disk = psutil.disk_usage("/")
            net = psutil.net_io_counters()
            return {
                "cpu_pct": float(cpu),
                "mem_pct": float(vm.percent),
                "mem_used_gb": round(vm.used / 1024**3, 3),
                "mem_total_gb": round(vm.total / 1024**3, 3),
                "swap_pct": float(sm.percent),
                "swap_used_gb": round(sm.used / 1024**3, 3),
                "load_avg_1m": load[0] if load else 0.0,
                "load_avg_5m": load[1] if load else 0.0,
                "load_avg_15m": load[2] if load else 0.0,
                "n_procs": int(n_procs),
                "disk_used_gb": round(disk.used / 1024**3, 3),
                "disk_total_gb": round(disk.total / 1024**3, 3),
                "disk_pct": float(disk.percent),
                "net_bytes_sent": int(net.bytes_sent),
                "net_bytes_recv": int(net.bytes_recv),
                "ts": time.time(),
            }
        except SensorError:
            raise
        except Exception as exc:  # noqa: BLE001 - sampler isolates, but be explicit
            raise SensorError(f"os sensor failed: {exc}") from exc

    @staticmethod
    def _load_avg() -> tuple[float, float, float] | None:
        try:
            import os

            return os.getloadavg()  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            # Windows has no getloadavg; degrade gracefully.
            return None


sensor = OsSensor()
