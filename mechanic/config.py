"""Configuration for Mechanic.

Resolved from environment variables (and an optional ini file in a later revision).
All paths derive from the user's home / XDG dirs so the same package runs on anyone's
box without modification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    """Local-first: keep all data under the user's home, never system-wide."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "mechanic"
    return Path.home() / ".local" / "share" / "mechanic"


def _default_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "mechanic"
    return Path.home() / ".config" / "mechanic"


@dataclass
class Config:
    """Runtime configuration. Values are safe defaults; override via env vars."""

    # Storage
    data_dir: Path = field(default_factory=_default_data_dir)
    db_path: Path | None = None  # resolved to data_dir/mechanic.sqlite if None

    # Sampling
    interval_seconds: float = 30.0
    retention_days: int = 30

    # Baseline
    window_size: int = 2880  # ~24h @ 30s
    ewma_alpha: float = 0.1
    z_threshold: float = 3.0
    min_samples: int = 30

    # Server / misc
    enabled_sensors: list[str] = field(default_factory=list)  # empty == all available

    def resolve(self) -> Config:
        """Apply env overrides and resolve derived paths. Returns self."""
        if env := os.environ.get("MECHANIC_DATA_DIR"):
            self.data_dir = Path(env)
        if env := os.environ.get("MECHANIC_DB_PATH"):
            self.db_path = Path(env)
        if env := os.environ.get("MECHANIC_INTERVAL"):
            self.interval_seconds = float(env)
        if env := os.environ.get("MECHANIC_RETENTION_DAYS"):
            self.retention_days = int(env)
        if env := os.environ.get("MECHANIC_WINDOW_SIZE"):
            self.window_size = int(env)
        if env := os.environ.get("MECHANIC_Z_THRESHOLD"):
            self.z_threshold = float(env)
        if env := os.environ.get("MECHANIC_MIN_SAMPLES"):
            self.min_samples = int(env)
        if env := os.environ.get("MECHANIC_ENABLED_SENSORS"):
            self.enabled_sensors = [s.strip() for s in env.split(",") if s.strip()]

        if self.db_path is None:
            self.db_path = self.data_dir / "mechanic.sqlite"
        return self

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


SCHEMA_VERSION = 1
