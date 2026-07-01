"""Configuration for Mechanic.

Resolved with this precedence (highest wins):
  1. MECHANIC_* environment variables
  2. ~/.config/mechanic/mechanic.ini  (or $MECHANIC_CONFIG)
  3. built-in defaults

The ini file is what makes a real install coherent: the installer writes the chosen
data_dir into mechanic.ini, so a user's interactive `mechanic status` (run in a shell
with no MECHANIC_* vars) reads the same database the launchd/systemd daemon writes to.
Without it, the CLI and the daemon would silently use different files.
"""

from __future__ import annotations

import configparser
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


def _default_config_path() -> Path:
    return _default_config_dir() / "mechanic.ini"


@dataclass
class Config:
    """Runtime configuration. Values are safe defaults; override via env vars or ini."""

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

    # Config file location (resolved, not loaded, until .resolve())
    config_path: Path | None = None

    def __post_init__(self) -> None:
        if self.config_path is None:
            self.config_path = _default_config_path()

    def _load_ini(self) -> dict[str, str]:
        """Read the ini file into a flat key->value dict. Returns {} if missing/broken."""
        path = self.config_path
        if path is None or not Path(path).exists():
            return {}
        parser = configparser.ConfigParser()
        try:
            parser.read(path)
        except configparser.Error:
            return {}
        flat: dict[str, str] = {}
        for section in parser.sections():
            for key, value in parser.items(section):
                flat[key] = value
        return flat

    def resolve(self) -> Config:
        """Apply ini then env overrides and resolve derived paths. Returns self.

        Env vars win over ini; ini wins over defaults. This order matters: the
        installer writes the chosen data_dir to the ini, but a user who exports
        MECHANIC_DATA_DIR in their shell should still be able to override it.
        """
        # MECHANIC_CONFIG points at a specific ini file; honor it before reading.
        if env_cfg := os.environ.get("MECHANIC_CONFIG"):
            self.config_path = Path(env_cfg)
        ini = self._load_ini()

        def pick(key: str, env_name: str) -> str | None:
            env = os.environ.get(env_name)
            if env is not None:
                return env
            return ini.get(key)

        if (v := pick("data_dir", "MECHANIC_DATA_DIR")) is not None:
            self.data_dir = Path(v)
        if (v := pick("db_path", "MECHANIC_DB_PATH")) is not None:
            self.db_path = Path(v)
        if (v := pick("interval_seconds", "MECHANIC_INTERVAL")) is not None:
            self.interval_seconds = float(v)
        if (v := pick("retention_days", "MECHANIC_RETENTION_DAYS")) is not None:
            self.retention_days = int(v)
        if (v := pick("window_size", "MECHANIC_WINDOW_SIZE")) is not None:
            self.window_size = int(v)
        if (v := pick("ewma_alpha", "MECHANIC_EWMA_ALPHA")) is not None:
            self.ewma_alpha = float(v)
        if (v := pick("z_threshold", "MECHANIC_Z_THRESHOLD")) is not None:
            self.z_threshold = float(v)
        if (v := pick("min_samples", "MECHANIC_MIN_SAMPLES")) is not None:
            self.min_samples = int(v)
        if (v := pick("enabled_sensors", "MECHANIC_ENABLED_SENSORS")) is not None:
            self.enabled_sensors = [s.strip() for s in v.split(",") if s.strip()]

        if self.db_path is None:
            self.db_path = self.data_dir / "mechanic.sqlite"
        return self

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


SCHEMA_VERSION = 1
