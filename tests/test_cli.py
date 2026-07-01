"""Tests for the CLI entrypoint.

argparse dispatch to the subcommands. We test the wiring (dispatch + exit codes), not
the full daemon/MCP runtime — those are exercised in their own modules. Long-running
subcommands (sampler, server) are invoked via short-circuited stubs.
"""



from mechanic import cli


def test_cli_has_subcommands():
    parser = cli.build_parser()
    # Each known subcommand parses to an args object with .command set.
    for sub in ["doctor", "once", "status", "sampler", "server"]:
        args = parser.parse_args([sub])
        assert args.command == sub


def test_doctor_returns_zero_when_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("MECHANIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MECHANIC_DB_PATH", str(tmp_path / "m.db"))
    rc = cli.run(["doctor"])
    assert rc == 0


def test_doctor_returns_zero_even_when_optional_sensors_missing(tmp_path, monkeypatch):
    """An absent optional sensor (e.g. ollama not installed) is not an error —
    Mechanic runs on whatever sensors are available. doctor must return 0."""
    monkeypatch.setenv("MECHANIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MECHANIC_DB_PATH", str(tmp_path / "m.db"))
    # Force the ollama sensor to report unavailable, simulating a box without it.
    from mechanic.plugins.ollama_sensor import OllamaSensor

    orig = OllamaSensor.is_available
    OllamaSensor.is_available = lambda self: False
    try:
        rc = cli.run(["doctor"])
    finally:
        OllamaSensor.is_available = orig
    assert rc == 0  # not EXIT_MISSING_DEPS


def test_doctor_prints_hint_when_optional_sensor_missing(tmp_path, monkeypatch, capsys):
    """When an optional sensor is unavailable, doctor prints a one-line install hint
    so the user knows what they'd get — without forcing the install."""
    monkeypatch.setenv("MECHANIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MECHANIC_DB_PATH", str(tmp_path / "m.db"))
    from mechanic.plugins.ollama_sensor import OllamaSensor

    orig = OllamaSensor.is_available
    OllamaSensor.is_available = lambda self: False
    try:
        cli.run(["doctor"])
    finally:
        OllamaSensor.is_available = orig
    out = capsys.readouterr().out
    assert "ollama" in out
    assert "optional" in out  # the nudge appears, clearly marked optional


def test_once_writes_a_sample_and_returns_zero(tmp_path):
    import os

    os.environ["MECHANIC_DATA_DIR"] = str(tmp_path)
    os.environ["MECHANIC_DB_PATH"] = str(tmp_path / "m.db")
    try:
        rc = cli.run(["once"])
    finally:
        del os.environ["MECHANIC_DATA_DIR"]
        del os.environ["MECHANIC_DB_PATH"]
    assert rc == 0
    # the os sensor should have written at least one sample (it's always available)
    from mechanic.config import Config
    from mechanic.store import Store

    cfg = Config(data_dir=tmp_path, db_path=tmp_path / "m.db").resolve()
    s = Store(cfg)
    s.open()
    assert s.count("os") >= 1
    s.close()


def test_status_reports_last_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("MECHANIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MECHANIC_DB_PATH", str(tmp_path / "m.db"))
    # seed the store with one sample so status has something to show
    cli.run(["once"])
    rc = cli.run(["status"])
    assert rc == 0


def test_unknown_command_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("MECHANIC_DATA_DIR", str(tmp_path))
    rc = cli.run(["bogus-command"])
    assert rc != 0


def test_cli_run_accepts_argv_and_args_object(tmp_path, monkeypatch):
    """run() should accept a list of args (as called by the entrypoint)."""
    monkeypatch.setenv("MECHANIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MECHANIC_DB_PATH", str(tmp_path / "m.db"))
    rc = cli.run(["doctor"])
    assert rc == 0
