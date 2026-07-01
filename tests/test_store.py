"""Tests for the SQLite store.

Pure I/O: schema creation, sample write/read round-trip, retention pruning, and
schema-version migration. No sensor logic lives here.
"""

import json
import sqlite3
import time

from mechanic.store import Store


def test_store_creates_schema_and_version_table(config):
    s = Store(config)
    s.open()
    with sqlite3.connect(config.db_path) as cx:
        rows = cx.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in rows}
    assert "samples" in names
    assert "schema_version" in names
    assert cx  # connection closed by context manager
    s.close()


def test_write_and_read_sample_round_trip(config):
    s = Store(config)
    s.open()
    payload = {"cpu_pct": 42.0, "mem_pct": 60.0}
    sid = s.write_sample("os", payload)
    assert isinstance(sid, int) and sid > 0

    rows = s.read_samples("os")
    assert len(rows) == 1
    assert rows[0]["sensor"] == "os"
    assert rows[0]["payload"] == payload
    assert rows[0]["ts"] > 0


def test_read_samples_filters_by_sensor(config):
    s = Store(config)
    s.open()
    s.write_sample("os", {"cpu_pct": 1})
    s.write_sample("docker", {"containers": 2})
    s.write_sample("os", {"cpu_pct": 3})

    os_rows = s.read_samples("os")
    docker_rows = s.read_samples("docker")
    assert len(os_rows) == 2
    assert len(docker_rows) == 1


def test_read_samples_since_filters_by_timestamp(config):
    s = Store(config)
    s.open()
    t0 = time.time()
    s.write_sample("os", {"v": 1})
    s.write_sample("os", {"v": 2})
    # only rows at/after t0 should come back (both are >= t0)
    rows = s.read_samples("os", since=t0)
    assert len(rows) == 2
    # a future cutoff returns nothing
    rows_future = s.read_samples("os", since=time.time() + 100)
    assert rows_future == []


def test_read_samples_limit(config):
    s = Store(config)
    s.open()
    for i in range(5):
        s.write_sample("os", {"i": i})
    rows = s.read_samples("os", limit=3)
    assert len(rows) == 3
    # most-recent-first ordering: limit returns the newest 3
    assert [r["payload"]["i"] for r in rows] == [4, 3, 2]


def test_payload_is_json_serializable_round_trip(config):
    s = Store(config)
    s.open()
    nested = {"containers": [{"name": "x", "ports": ["80:80", "443:443"]}], "n": 2}
    s.write_sample("docker", nested)
    rows = s.read_samples("docker")
    assert rows[0]["payload"] == nested


def test_open_is_idempotent(config):
    s = Store(config)
    s.open()
    s.open()  # second open must not error or wipe data
    s.write_sample("os", {"a": 1})
    s.open()
    assert len(s.read_samples("os")) == 1
    s.close()


def test_retention_prune_removes_old_rows(config):
    config.retention_days = 1
    s = Store(config)
    s.open()
    # insert an "old" row directly via raw connection to backdate timestamp
    with sqlite3.connect(config.db_path) as cx:
        old_ts = time.time() - 60 * 60 * 24 * 3  # 3 days ago, > 1-day retention
        cx.execute(
            "INSERT INTO samples (ts, sensor, payload) VALUES (?, ?, ?)",
            (old_ts, "os", json.dumps({"old": True})),
        )
        cx.commit()
    # a fresh write should trigger pruning of rows older than retention window
    s.write_sample("os", {"fresh": True})
    rows = s.read_samples("os")
    assert len(rows) == 1
    assert rows[0]["payload"] == {"fresh": True}


def test_schema_version_is_recorded(config):
    from mechanic.config import SCHEMA_VERSION

    s = Store(config)
    s.open()
    with sqlite3.connect(config.db_path) as cx:
        v = cx.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == SCHEMA_VERSION
    s.close()


def test_existing_db_is_not_recreated(config):
    s1 = Store(config)
    s1.open()
    s1.write_sample("os", {"x": 1})
    s1.close()

    s2 = Store(config)
    s2.open()
    rows = s2.read_samples("os")
    assert len(rows) == 1
    s2.close()
