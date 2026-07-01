"""Sensor plugins. Each plugin is one module satisfying the SensorPlugin protocol.

Auto-discovered via the registry in this package. Add a sensor by dropping a new
module here that defines a class with name, is_available(), and sample().
"""

from __future__ import annotations

from mechanic.plugins.base import (  # noqa: F401 - re-exported for convenience
    SensorError,
    SensorPlugin,
    discover,
    registry,
)

# Eagerly discover on import so `registry.all()` is populated without callers having
# to remember to call discover() first. Safe: discovery swallows per-module errors.
discover()
