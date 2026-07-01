"""Tests for the MCP server surface.

The server is a STATELESS reader over the store. Every tool that needs a baseline
(is_this_normal, baseline_for) reconstructs it fresh from store history on each call —
so a spike in an is_this_normal query never pollutes the baseline, and the server's
view always matches what the sampler actually recorded. Tests populate a real (temp)
store and invoke tools in-process via FastMCP.call_tool.
"""

import asyncio
import json

import pytest

from mechanic.config import SCHEMA_VERSION
from mechanic.server import build_server
from mechanic.store import Store


@pytest.fixture
def populated_store(config):
    """A store seeded with os + docker + ollama samples for query tests."""
    s = Store(config)
    s.open()
    s.write_sample("os", {"cpu_pct": 10.0, "mem_pct": 40.0})
    s.write_sample("os", {"cpu_pct": 11.0, "mem_pct": 41.0})
    s.write_sample("os", {"cpu_pct": 12.0, "mem_pct": 42.0})
    s.write_sample("docker", {"n_containers": 1, "n_running": 1, "container_names": ["web"]})
    s.write_sample("ollama", {"n_models_loaded": 0, "loaded_models": []})
    return s


@pytest.fixture
def server(populated_store, config):
    """A stateless server wired to the store. No pre-warmed baseline is needed."""
    return build_server(populated_store, config)


def _call(server, tool, args):
    """Run a tool in-process and return the structured result (dict) or text."""
    result = asyncio.run(server.call_tool(tool, args))
    if isinstance(result, tuple):
        blocks, structured = result
        if isinstance(structured, dict):
            return structured
        return json.loads(blocks[0].text) if blocks else None
    return result


def test_list_tools_exposes_five(server):
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {"is_this_normal", "what_changed_since", "baseline_for", "recent", "doctor"} <= names


def test_doctor_reports_storage_and_sensors(server, populated_store):
    r = _call(server, "doctor", {})
    assert r["ok"] is True
    assert "store" in r
    assert r["store"]["path"] is not None
    assert r["store"]["schema_version"] == SCHEMA_VERSION
    assert "sensors" in r
    sensor_names = {s["name"] for s in r["sensors"]}
    assert {"os", "docker", "ollama"} <= sensor_names


def test_is_this_normal_with_no_history_returns_cold_start(server):
    # a metric with no recorded samples is cold-start
    r = _call(server, "is_this_normal", {"metric": "disk.used_gb", "value": 99.0})
    assert r["normal"] is True
    assert r["cold_start"] is True
    assert r["n"] == 0


def test_is_this_normal_is_stateless_spike_does_not_pollute(config):
    """The key v1.1 property: querying is_this_normal with a spike must NOT mutate the
    baseline. A subsequent normal query is still judged against the recorded history,
    not poisoned by the spike we just asked about."""
    s = Store(config)
    s.open()
    for _ in range(50):
        s.write_sample("os", {"cpu_pct": 10.0})
    srv = build_server(s, config)

    # ask about a huge spike — it should be flagged
    spike = _call(srv, "is_this_normal", {"metric": "os.cpu_pct", "value": 999.0})
    assert spike["normal"] is False

    # immediately ask about a normal value — must NOT be poisoned by the 999 query
    normal = _call(srv, "is_this_normal", {"metric": "os.cpu_pct", "value": 10.0})
    assert normal["normal"] is True
    assert normal["cold_start"] is False
    # and the mean reflects the recorded history (~10), not the 999 query
    assert normal["mean"] < 15.0


def test_is_this_normal_after_warmup_detects_anomaly(config):
    """With enough recorded samples, a genuine spike is flagged — even off a constant
    baseline, because the effective-std floor keeps the z-score from exploding on
    jitter while still catching large jumps."""
    s = Store(config)
    s.open()
    for _ in range(50):
        s.write_sample("os", {"cpu_pct": 10.0, "mem_pct": 40.0})
    srv = build_server(s, config)
    r = _call(srv, "is_this_normal", {"metric": "os.cpu_pct", "value": 10.0})
    assert r["normal"] is True
    # a large jump (10 → 999) is flagged: |999-10| / max(std, 0.05*10) >> 3
    r2 = _call(srv, "is_this_normal", {"metric": "os.cpu_pct", "value": 999.0})
    assert r2["normal"] is False
    assert r2["cold_start"] is False
    # a tiny jitter off the constant baseline (10 -> 10.5) is NOT flagged
    r3 = _call(srv, "is_this_normal", {"metric": "os.cpu_pct", "value": 10.5})
    assert r3["normal"] is True


def test_is_this_normal_warm_baseline_from_history_not_in_memory(config):
    """A freshly-built server with no in-memory state still answers with the full
    recorded history — the baseline is hydrated from the store on each call."""
    s = Store(config)
    s.open()
    for _ in range(100):
        s.write_sample("os", {"cpu_pct": 50.0})
    srv = build_server(s, config)
    r = _call(srv, "is_this_normal", {"metric": "os.cpu_pct", "value": 50.0})
    assert r["n"] == 100
    assert r["cold_start"] is False


def test_baseline_for_metric(server):
    r = _call(server, "baseline_for", {"target": "os.cpu_pct"})
    assert "mean" in r and "std" in r and "n" in r
    assert r["n"] == 3


def test_baseline_for_sensor_returns_all_its_metrics(server):
    r = _call(server, "baseline_for", {"target": "os"})
    assert "metrics" in r
    assert "os.cpu_pct" in r["metrics"]


def test_recent_returns_last_n(server, populated_store):
    r = _call(server, "recent", {"sensor": "os", "limit": 2})
    assert len(r["samples"]) == 2
    assert r["sensor"] == "os"
    # newest first
    assert r["samples"][0]["payload"]["cpu_pct"] == 12.0


def test_recent_unknown_sensor_returns_empty(server):
    r = _call(server, "recent", {"sensor": "nonexistent", "limit": 5})
    assert r["samples"] == []
    assert r["sensor"] == "nonexistent"


def test_what_changed_since_detects_new_container(config):
    s = Store(config)
    s.open()
    s.write_sample("docker", {"n_containers": 1, "n_running": 1, "container_names": ["web"]})
    s.write_sample("os", {"cpu_pct": 10.0})
    # simulate time passing, then a new container appears
    s.write_sample("docker", {"n_containers": 2, "n_running": 2, "container_names": ["web", "db"]})
    s.write_sample("os", {"cpu_pct": 12.0})
    srv = build_server(s, config)
    r = _call(srv, "what_changed_since", {"minutes_ago": 60})
    assert "changes" in r
    docker_changes = [c for c in r["changes"] if c["sensor"] == "docker"]
    assert docker_changes, "expected a docker change entry"
    assert "db" in docker_changes[0]["container_names"]["added"]
    assert docker_changes[0]["container_names"]["removed"] == []


def test_what_changed_since_with_no_changes_returns_empty(config):
    """A store whose values are unchanged between the past and latest sample."""
    s = Store(config)
    s.open()
    s.write_sample("os", {"cpu_pct": 10.0, "mem_pct": 40.0})
    s.write_sample("os", {"cpu_pct": 10.0, "mem_pct": 40.0})
    s.write_sample("docker", {"n_containers": 1, "n_running": 1, "container_names": ["web"]})
    s.write_sample("docker", {"n_containers": 1, "n_running": 1, "container_names": ["web"]})
    srv = build_server(s, config)
    r = _call(srv, "what_changed_since", {"minutes_ago": 60})
    assert r["changes"] == []


def test_what_changed_since_filters_scalar_drift_within_baseline(config):
    """A scalar that drifted but stays within its baseline should NOT surface — that's
    the fix for the 'noisy 9 trivial deltas' problem. Only anomalous scalars report."""
    s = Store(config)
    s.open()
    # Warm a tight cpu baseline around 50, and a wide mem baseline.
    for _ in range(40):
        s.write_sample("os", {"cpu_pct": 50.0, "mem_pct": 40.0})
    # past sample: cpu 50, mem 40
    # latest sample: cpu 50.3 (jitter, within baseline), mem 40.2 (jitter, within)
    s.write_sample("os", {"cpu_pct": 50.3, "mem_pct": 40.2})
    srv = build_server(s, config)
    r = _call(srv, "what_changed_since", {"minutes_ago": 60})
    os_changes = [c for c in r["changes"] if c["sensor"] == "os"]
    # cpu_pct and mem_pct drifted but within baseline → not surfaced
    assert os_changes == [], f"expected no os changes for within-baseline drift, got {os_changes}"


def test_what_changed_since_surfaces_anomalous_scalar(config):
    """A scalar that jumps outside its baseline SHOULD surface, with the delta."""
    s = Store(config)
    s.open()
    for _ in range(40):
        s.write_sample("os", {"cpu_pct": 50.0, "mem_pct": 40.0})
    # latest: cpu spikes to 500 (anomalous), mem stays put
    s.write_sample("os", {"cpu_pct": 500.0, "mem_pct": 40.0})
    srv = build_server(s, config)
    r = _call(srv, "what_changed_since", {"minutes_ago": 60})
    os_changes = [c for c in r["changes"] if c["sensor"] == "os"]
    assert os_changes, "expected an os change for the cpu spike"
    assert "cpu_pct" in os_changes[0]
    assert "mem_pct" not in os_changes[0]  # mem didn't spike


def test_what_changed_since_always_surfaces_set_changes(config):
    """Set-valued metrics (containers, models) surface on any add/remove, regardless
    of baseline — a new container is always meaningful, never 'drift'."""
    s = Store(config)
    s.open()
    for _ in range(40):
        s.write_sample("docker", {"n_containers": 1, "n_running": 1, "container_names": ["web"]})
    s.write_sample("docker", {"n_containers": 2, "n_running": 2, "container_names": ["web", "db"]})
    srv = build_server(s, config)
    r = _call(srv, "what_changed_since", {"minutes_ago": 60})
    docker_changes = [c for c in r["changes"] if c["sensor"] == "docker"]
    assert docker_changes
    assert "db" in docker_changes[0]["container_names"]["added"]


def test_what_changed_since_cold_metric_filters_out(config):
    """A scalar with no baseline history (cold-start) doesn't surface drift — we
    can't call it anomalous if we don't know what's normal yet."""
    s = Store(config)
    s.open()
    s.write_sample("os", {"cpu_pct": 10.0})
    s.write_sample("os", {"cpu_pct": 12.0})  # only 2 samples, < min_samples
    srv = build_server(s, config)
    r = _call(srv, "what_changed_since", {"minutes_ago": 60})
    os_changes = [c for c in r["changes"] if c["sensor"] == "os"]
    assert os_changes == []


def test_tools_return_json_serializable(server):
    """Every tool's structured result must be JSON-serializable (it crosses stdio)."""
    for tool, args in [
        ("doctor", {}),
        ("is_this_normal", {"metric": "os.cpu_pct", "value": 10.0}),
        ("what_changed_since", {"minutes_ago": 60}),
        ("baseline_for", {"target": "os"}),
        ("recent", {"sensor": "os", "limit": 5}),
    ]:
        r = _call(server, tool, args)
        json.dumps(r)  # raises if not serializable
