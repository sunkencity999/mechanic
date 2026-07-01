"""SQLite-backed sample store.

Single writer (the sampler daemon), many readers (the MCP server process). Schema is
deliberately simple: one row per sample, payload as JSON. Indexed on (sensor, ts) so
the common "recent samples for sensor X" and "samples since time T" queries are cheap.

No sensor or baseline logic here — this module knows only about persistence.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from mechanic.config import SCHEMA_VERSION, Config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    sensor  TEXT    NOT NULL,
    payload TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_sensor_ts ON samples (sensor, ts DESC);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts);
"""

# Prune every N writes so we don't run a DELETE on every sample.
_PRUNE_EVERY = 64


class Store:
    """Thin wrapper over a sqlite3 connection."""

    def __init__(self, config: Config):
        self.config = config
        self._cx: sqlite3.Connection | None = None
        self._write_count = 0

    def open(self) -> Store:
        """Open the DB (creating the schema if needed) idempotently."""
        if self._cx is not None:
            return self
        self.config.ensure_dirs()
        assert self.config.db_path is not None
        # check_same_thread=False: the MCP server may read from a different thread
        # than the one that opened it; sqlite3 with a single connection is fine here
        # because we guard writes via the connection lock and use short transactions.
        cx = sqlite3.connect(self.config.db_path, check_same_thread=False)
        cx.row_factory = sqlite3.Row
        cx.executescript(_SCHEMA)
        # Record schema version exactly once.
        existing = cx.execute(
            "SELECT COUNT(*) FROM schema_version"
        ).fetchone()[0]
        if existing == 0:
            cx.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, time.time()),
            )
        cx.commit()
        self._cx = cx
        return self

    def close(self) -> None:
        if self._cx is not None:
            self._cx.commit()
            self._cx.close()
            self._cx = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._cx is None:
            raise RuntimeError("Store is not open; call open() first")
        return self._cx

    # ---- writes -----------------------------------------------------------

    def write_sample(self, sensor: str, payload: dict[str, Any]) -> int:
        """Insert one sample. Returns the row id."""
        ts = time.time()
        blob = json.dumps(payload, default=str, sort_keys=True)
        cur = self.conn.execute(
            "INSERT INTO samples (ts, sensor, payload) VALUES (?, ?, ?)",
            (ts, sensor, blob),
        )
        self.conn.commit()
        self._write_count += 1
        # Prune on the first write after open (cheap, catches backlog from a long
        # gap) and then every _PRUNE_EVERY writes thereafter. Avoids a DELETE per
        # write while keeping retention bounded in a fresh process.
        if self._write_count == 1 or self._write_count % _PRUNE_EVERY == 0:
            self._prune()
        return cur.lastrowid

    def _prune(self) -> None:
        """Delete rows older than retention_days. Called periodically from write_sample."""
        cutoff = time.time() - self.config.retention_days * 86400
        self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.conn.commit()

    def prune_now(self) -> int:
        """Force a prune; returns number of rows deleted."""
        cutoff = time.time() - self.config.retention_days * 86400
        cur = self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    # ---- reads ------------------------------------------------------------

    def read_samples(
        self,
        sensor: str,
        since: float | None = None,
        until: float | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read samples, newest first. Each row: {id, ts, sensor, payload(dict)}."""
        sql = "SELECT id, ts, sensor, payload FROM samples WHERE sensor = ?"
        params: list[Any] = [sensor]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "sensor": r["sensor"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    def last_sample_ts(self, sensor: str) -> float | None:
        row = self.conn.execute(
            "SELECT ts FROM samples WHERE sensor = ? ORDER BY ts DESC LIMIT 1",
            (sensor,),
        ).fetchone()
        return row["ts"] if row else None

    def sensors(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT sensor FROM samples ORDER BY sensor"
        ).fetchall()
        return [r["sensor"] for r in rows]

    def count(self, sensor: str | None = None) -> int:
        if sensor is None:
            return self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM samples WHERE sensor = ?", (sensor,)
        ).fetchone()[0]
