"""Sensor plugin protocol + registry.

A sensor is anything that satisfies the SensorPlugin protocol:
  - name:        a stable, short string used as the storage key (e.g. "os", "docker")
  - is_available(): cheap probe — is this backend usable on this box right now?
  - sample():    return a flat, JSON-serializable dict of current state.

Adding a sensor is one file: drop a module in this package whose class satisfies the
protocol. The registry auto-discovers it via the package's __init__.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SensorError(RuntimeError):
    """Raised by a sensor when it cannot produce a sample (backend missing, daemon
    down in a way that's not just '0 models', etc.). The sampler isolates these so one
    sensor's failure never stops the loop."""


@runtime_checkable
class SensorPlugin(Protocol):
    name: str

    def is_available(self) -> bool:  # noqa: D401 - protocol method
        """True if this sensor can produce samples on the current box."""
        ...

    def sample(self) -> dict:
        """Return a flat, JSON-serializable dict of current state."""
        ...


@dataclass
class _Registry:
    _sensors: list

    def register(self, sensor: SensorPlugin) -> None:
        # dedupe by name — last registration wins (lets tests override)
        self._sensors = [s for s in self._sensors if s.name != sensor.name]
        self._sensors.append(sensor)

    def all(self) -> list[SensorPlugin]:
        return list(self._sensors)

    def get(self, name: str) -> SensorPlugin | None:
        for s in self._sensors:
            if s.name == name:
                return s
        return None

    def available(self) -> list[SensorPlugin]:
        return [s for s in self._sensors if s.is_available()]


# Singleton registry. Populated by auto-discovery in this package's __init__.
registry = _Registry(_sensors=[])


def discover() -> list[SensorPlugin]:
    """Import every module in this package and register any SensorPlugin it defines
    via a top-level `sensor` instance or a `Sensor` class with a default ctor."""
    import mechanic.plugins as pkg

    found: list[SensorPlugin] = []
    for modinfo in pkgutil.iter_modules(pkg.__path__):
        if modinfo.name in ("base", "__init__"):
            continue
        try:
            mod = importlib.import_module(f"mechanic.plugins.{modinfo.name}")
        except Exception:  # noqa: BLE001 - a broken sensor module must not break discovery
            continue
        # Convention: a module may expose a `sensor` instance or a `Sensor` class.
        inst = getattr(mod, "sensor", None)
        if inst is None:
            cls = getattr(mod, "Sensor", None)
            if cls is not None:
                try:
                    inst = cls()
                except Exception:  # noqa: BLE001
                    inst = None
        if (
            inst is not None
            and hasattr(inst, "name")
            and hasattr(inst, "is_available")
            and hasattr(inst, "sample")
        ):
            found.append(inst)
            registry.register(inst)
    return found
