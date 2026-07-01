"""Mechanic command-line interface.

Subcommands:
  sampler   — run the sampling daemon (foreground; supervisors manage this)
  server    — run the MCP stdio server (the AI client spawns this)
  once      — sample every available sensor once, then exit (debug / smoke test)
  doctor    — report sensor availability + storage health
  status    — show the most recent sample per sensor

Exit codes: 0 ok · 2 missing-deps · 3 storage-error
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence

from mechanic.config import Config
from mechanic.plugins import registry as _sensor_registry
from mechanic.sampler import Sampler
from mechanic.store import Store

EXIT_OK = 0
EXIT_MISSING_DEPS = 2
EXIT_STORAGE_ERROR = 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mechanic",
        description="Local-first baseline & anomaly daemon with an MCP interface.",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("sampler", help="run the sampling daemon (foreground)")
    sub.add_parser("server", help="run the MCP stdio server")
    sub.add_parser("once", help="sample every available sensor once, then exit")
    sub.add_parser("doctor", help="report sensor availability + storage health")
    sub.add_parser("status", help="show the most recent sample per sensor")
    return p


def _config_from_env() -> Config:
    cfg = Config().resolve()
    cfg.ensure_dirs()
    return cfg


def _open_store(cfg: Config) -> Store:
    store = Store(cfg)
    store.open()
    return store


def cmd_doctor(cfg: Config) -> int:
    print(f"Mechanic v{_version()}")
    print(f"  data dir : {cfg.data_dir}")
    print(f"  db path  : {cfg.db_path}")
    print(f"  interval : {cfg.interval_seconds}s · retention: {cfg.retention_days}d")
    print(f"  baseline : window={cfg.window_size} alpha={cfg.ewma_alpha} "
          f"z={cfg.z_threshold} min_samples={cfg.min_samples}")
    print()
    print("Sensors:")
    for s in _sensor_registry.all():
        try:
            avail = bool(s.is_available())
        except Exception:  # noqa: BLE001
            avail = False
        mark = "✓" if avail else "✗"
        state = "available" if avail else "not available (will be skipped)"
        print(f"  {mark} {s.name:<10} {state}")
        if not avail:
            hint = _SENSOR_HINTS.get(s.name)
            if hint:
                print(f"             {hint}")
    print()
    try:
        store = _open_store(cfg)
        n = store.count()
        store.close()
        print(f"Storage: OK ({n} samples)")
    except Exception as exc:  # noqa: BLE001
        print(f"Storage: ERROR — {exc}")
        return EXIT_STORAGE_ERROR
    # An absent optional sensor (ollama/docker not installed) is NOT an error —
    # Mechanic runs fine on whatever sensors are available. Only return nonzero if
    # storage itself is broken (handled above).
    return EXIT_OK


# Hints shown by `mechanic doctor` when an optional sensor's backend isn't installed.
# Mechanic never requires these — they're just more things it can watch. The hint
# points the user at the optional install without ever forcing it.
_SENSOR_HINTS = {
    "ollama": "install ollama if you want model-load monitoring (optional)",
    "docker": "install docker if you want container monitoring (optional)",
}


def cmd_once(cfg: Config) -> int:
    store = _open_store(cfg)
    sampler = Sampler(store)
    try:
        n = sampler.run_once()
        print(f"sampled {n} sensor(s) -> {cfg.db_path}")
    finally:
        store.close()
    return EXIT_OK


def cmd_status(cfg: Config) -> int:
    store = _open_store(cfg)
    try:
        sensors = store.sensors()
        if not sensors:
            print("no samples yet — run `mechanic once` or start the sampler")
            return EXIT_OK
        for name in sensors:
            rows = store.read_samples(name, limit=1)
            if not rows:
                continue
            r = rows[0]
            age = __import__("time").time() - r["ts"]
            print(f"[{name}] {age:.1f}s ago: {json.dumps(r['payload'], default=str)}")
    finally:
        store.close()
    return EXIT_OK


def cmd_sampler(cfg: Config) -> int:
    store = _open_store(cfg)
    sampler = Sampler(store)
    try:
        sampler.run()
    except KeyboardInterrupt:
        sampler.stop()
    finally:
        store.close()
    return EXIT_OK


def cmd_server(cfg: Config) -> int:
    # Import here so `mechanic doctor` etc. don't require the MCP SDK at import time
    # (it's a hard dep, but this keeps the import graph lazy and the server path clean).
    from mechanic.server import build_server

    store = _open_store(cfg)
    mcp = build_server(store, cfg)
    try:
        mcp.run()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    return EXIT_OK


def _version() -> str:
    from mechanic import __version__

    return __version__


_DISPATCH = {
    "doctor": cmd_doctor,
    "once": cmd_once,
    "status": cmd_status,
    "sampler": cmd_sampler,
    "server": cmd_server,
}


def run(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse calls sys.exit(2) on bad input. Translate to a return code so
        # library callers of run() don't have to catch SystemExit.
        code = exc.code
        return code if isinstance(code, int) else EXIT_MISSING_DEPS
    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = _config_from_env()
    handler = _DISPATCH.get(args.command)
    if handler is None:
        parser.print_help()
        return EXIT_MISSING_DEPS
    return handler(cfg)


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
