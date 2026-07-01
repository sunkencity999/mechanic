"""Tests for Config — env vars, ini file, and precedence.

Env vars override the ini file; the ini file overrides defaults. This is what makes
the install coherent: the installer writes the chosen data_dir into mechanic.ini, so
the user's interactive `mechanic status` (no env var in the shell) reads the same DB
the daemon writes.
"""

from pathlib import Path

from mechanic.config import Config


def write_ini(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_defaults_when_no_env_no_ini(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # no config dir → no ini
    cfg = Config().resolve()
    assert cfg.data_dir == tmp_path / ".local" / "share" / "mechanic"
    assert cfg.interval_seconds == 30.0
    assert cfg.retention_days == 30
    assert cfg.window_size == 2880


def test_ini_sets_data_dir(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ini = tmp_path / ".config" / "mechanic" / "mechanic.ini"
    write_ini(ini, "[storage]\ndata_dir = /opt/mechanic-data\n")
    cfg = Config(config_path=ini).resolve()
    assert str(cfg.data_dir) == "/opt/mechanic-data"
    assert str(cfg.db_path) == "/opt/mechanic-data/mechanic.sqlite"


def test_ini_sets_sampling_and_baseline(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ini = tmp_path / ".config" / "mechanic" / "mechanic.ini"
    write_ini(
        ini,
        "[sampler]\ninterval_seconds = 15\nretention_days = 7\n"
        "[baseline]\nwindow_size = 1000\nz_threshold = 4.5\nmin_samples = 20\n",
    )
    cfg = Config(config_path=ini).resolve()
    assert cfg.interval_seconds == 15.0
    assert cfg.retention_days == 7
    assert cfg.window_size == 1000
    assert cfg.z_threshold == 4.5
    assert cfg.min_samples == 20


def test_env_overrides_ini(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MECHANIC_INTERVAL", "5")
    ini = tmp_path / ".config" / "mechanic" / "mechanic.ini"
    write_ini(ini, "[sampler]\ninterval_seconds = 15\n")
    cfg = Config(config_path=ini).resolve()
    assert cfg.interval_seconds == 5.0  # env wins


def test_env_data_dir_overrides_ini(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MECHANIC_DATA_DIR", "/from-env")
    ini = tmp_path / ".config" / "mechanic" / "mechanic.ini"
    write_ini(ini, "[storage]\ndata_dir = /from-ini\n")
    cfg = Config(config_path=ini).resolve()
    assert str(cfg.data_dir) == "/from-env"


def test_missing_ini_falls_back_to_defaults(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config(config_path=tmp_path / "nonexistent.ini").resolve()
    assert cfg.interval_seconds == 30.0


def test_enabled_sensors_from_ini(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ini = tmp_path / ".config" / "mechanic" / "mechanic.ini"
    write_ini(ini, "[sampler]\nenabled_sensors = os, ollama\n")
    cfg = Config(config_path=ini).resolve()
    assert cfg.enabled_sensors == ["os", "ollama"]


def test_default_config_path_is_xdg(isolated_env, tmp_path, monkeypatch):
    # when no config_path is given, Config locates ~/.config/mechanic/mechanic.ini
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config()
    assert str(cfg.config_path).endswith(".config/mechanic/mechanic.ini")


def test_mechanic_config_env_overrides_config_path(isolated_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    custom = tmp_path / "custom.ini"
    write_ini(custom, "[sampler]\ninterval_seconds = 99\n")
    monkeypatch.setenv("MECHANIC_CONFIG", str(custom))
    cfg = Config().resolve()
    assert cfg.interval_seconds == 99.0
