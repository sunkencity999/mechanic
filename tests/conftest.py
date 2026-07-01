"""Shared pytest fixtures for Mechanic.

Everything is hermetic: each test gets its own temp database and data dir, so tests
never touch the user's real ~/.local/share/mechanic. Sensor backends are faked via
monkeypatch so no real psutil/docker/ollama state is required.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """A clean per-test data directory under pytest's tmp_path."""
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def isolated_env(monkeypatch):
    """Strip MECHANIC_* env vars so tests start from defaults, not the real shell."""
    for k in list(os.environ):
        if k.startswith("MECHANIC_"):
            monkeypatch.delenv(k, raising=False)
    yield
    # monkeypatch restores env on teardown automatically


@pytest.fixture
def config(tmp_data_dir, isolated_env):
    """A Config pointing at a temp DB — the canonical test config."""
    from mechanic.config import Config

    cfg = Config(data_dir=tmp_data_dir, db_path=tmp_data_dir / "mechanic.sqlite")
    cfg.ensure_dirs()
    return cfg
