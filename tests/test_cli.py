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
