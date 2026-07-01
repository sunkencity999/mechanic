"""The MCP server — the AI-facing surface over the store.

FastMCP (stdio) server. Five tools, all read-only, all STATELESS:
  - is_this_normal(metric, value)    : is this value within the learned baseline?
  - what_changed_since(minutes_ago)  : set-differences in sensor state vs N minutes ago
  - baseline_for(target)             : mean/std/last/min/max for a metric or whole sensor
  - recent(sensor, limit)            : last N samples
  - doctor()                         : what's installed/available on this box + storage health

Stateless means: every tool that needs a baseline reconstructs it fresh from the
store's recorded history on each call. A spike passed to is_this_normal never mutates
the baseline, and the server's view always matches what the sampler actually wrote —
no in-memory state to drift, no cold-start, no per-process warmup. The cost is reading
<= window_size rows per call, which is cheap against local SQLite.

This process is a reader only. The sampler is the single writer.
"""

from __future__ import annotations

import math
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from mechanic.baseline import Baseline
from mechanic.config import SCHEMA_VERSION, Config
from mechanic.plugins import registry as _sensor_registry
from mechanic.store import Store

# Metrics that represent a *set* of names (containers, loaded models). Used by
# what_changed_since to compute added/removed sets rather than scalar diffs.
_SET_METRICS = {
    "docker": ["container_names"],
    "ollama": ["loaded_models"],
}


def _hydrate_baseline(
    store: Store,
    config: Config,
    metric: str,
    window_size: int | None = None,
) -> Baseline:
    """Build a fresh Baseline for one metric by replaying recorded history.

    Reads the last `window_size` samples for the metric's sensor and feeds the metric's
    values into a Baseline in chronological order. Cheap (bounded reads + in-memory
    math) and called per-tool-invocation so the server never holds mutable state.
    """
    ws = window_size if window_size is not None else config.window_size
    b = Baseline(
        window_size=ws,
        ewma_alpha=config.ewma_alpha,
        z_threshold=config.z_threshold,
        min_samples=config.min_samples,
    )
    if "." in metric:
        sensor, key = metric.split(".", 1)
    else:
        sensor, key = metric, None
    rows = store.read_samples(sensor, limit=ws)
    for r in reversed(rows):  # chronological (oldest first)
        if key is None:
            continue
        val = r["payload"].get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            b.update(metric, float(val))
    return b


def build_server(store: Store, config: Config) -> FastMCP:
    """Construct a stateless FastMCP server wired to the given store.

    Only `store` + `config` are required — no pre-built Baseline, because each tool
    hydrates one from history as needed. This is what makes the server cheap to spawn
    (the AI client spawns it on demand) and immune to cross-call state pollution.
    """
    mcp = FastMCP("mechanic")

    @mcp.tool()
    def is_this_normal(metric: str, value: float) -> dict[str, Any]:
        """Is this value normal for this machine? Answers 'is the current CPU/memory/
        disk/load/VRAM/etc. normal?' or 'should I be worried about X?'.

        Pass a metric name ('<sensor>.<key>', e.g. 'os.cpu_pct', 'os.mem_pct',
        'docker.n_running', 'ollama.loaded_vram_gb') and the value to check. Returns
        normal(bool), z_score, mean, std, n, cold_start. The baseline is rebuilt from
        recorded history on every call — querying with a spike does NOT pollute it.
        """
        b = _hydrate_baseline(store, config, metric)
        stats = b.stats_for(metric)
        if stats is None or stats.n == 0:
            return {
                "normal": True,
                "cold_start": True,
                "n": 0,
                "z_score": 0.0,
                "mean": 0.0,
                "std": 0.0,
                "note": f"no recorded samples for metric '{metric}'",
            }
        # evaluate() judges the value against history WITHOUT recording it — so a
        # spike in a query never pollutes the baseline, and n reflects recorded
        # history, not the value being asked about.
        result = b.evaluate(metric, value)
        return {
            "normal": not result.is_anomaly,
            "cold_start": result.cold_start,
            "n": result.n,
            "z_score": round(result.z_score, 4),
            "mean": round(result.mean, 4),
            "std": round(result.std, 4),
            "ewma": round(result.ewma, 4),
        }

    @mcp.tool()
    def what_changed_since(minutes_ago: float = 60.0) -> dict[str, Any]:
        """What changed on this box in the last N minutes? Answers 'what changed
        recently?', 'did anything change in the last hour?', 'did new containers or
        Ollama models appear since I left?'.

        Compares the most recent sample of each sensor against the sample closest to
        `minutes_ago` ago. For set-valued metrics (container_names, loaded_models)
        reports added/removed sets on any change — a new container or model is always
        meaningful. For scalars, only reports the delta when the new value is
        ANOMALOUS against that metric's learned baseline, so routine drift (net bytes
        creeping up, load wobbling 0.2) doesn't flood the result. A cold metric (no
        baseline yet) never surfaces as a scalar change.
        """
        cutoff = time.time() - minutes_ago * 60.0
        changes: list[dict[str, Any]] = []
        for sensor in store.sensors():
            rows = store.read_samples(sensor, limit=1000)[::-1]  # chronological
            if len(rows) < 2:
                continue
            latest = rows[-1]
            past = None
            for r in rows:
                if r["ts"] <= cutoff:
                    past = r
                else:
                    break
            if past is None:
                past = rows[0]
            if past["ts"] == latest["ts"]:
                continue
            diff = _diff_samples(
                sensor, past["payload"], latest["payload"], judge=_make_judge(store, config)
            )
            if diff:
                changes.append(
                    {
                        "sensor": sensor,
                        "minutes_ago": round((time.time() - past["ts"]) / 60.0, 2),
                        **diff,
                    }
                )
        return {"minutes_ago": minutes_ago, "changes": changes}

    @mcp.tool()
    def baseline_for(target: str) -> dict[str, Any]:
        """Give me the baseline numbers for a metric or sensor. Answers 'what's the
        normal range for CPU/memory/etc.?', 'what's the usual value?', 'min and max
        over the baseline window'.

        `target` is either '<sensor>.<key>' (one metric, e.g. 'os.cpu_pct') or
        '<sensor>' (all numeric metrics from that sensor, e.g. 'os'). Returns mean,
        std, min, max, last, n. Statistics are computed from recorded history on each call.
        """
        if "." in target:
            b = _hydrate_baseline(store, config, target)
            stats = b.stats_for(target)
            if stats is None or stats.n == 0:
                return {"metric": target, "n": 0, "note": "no recorded samples"}
            return {
                "metric": target,
                "n": stats.n,
                "mean": round(stats.sum / stats.n, 4) if stats.n else 0.0,
                "std": round(_std_of(stats), 4),
                "ewma": round(stats.ewma, 4),
                "last": stats.values[-1] if stats.values else None,
                "min": min(stats.values) if stats.values else None,
                "max": max(stats.values) if stats.values else None,
            }
        # whole sensor: baseline for every numeric key we've recorded
        rows = store.read_samples(target, limit=config.window_size)
        seen_keys: set[str] = set()
        for r in rows:
            for k, v in r["payload"].items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    seen_keys.add(f"{target}.{k}")
        metrics: dict[str, Any] = {}
        for mname in sorted(seen_keys):
            b = _hydrate_baseline(store, config, mname)
            stats = b.stats_for(mname)
            if stats is None or stats.n == 0:
                continue
            metrics[mname] = {
                "n": stats.n,
                "mean": round(stats.sum / stats.n, 4),
                "std": round(_std_of(stats), 4),
                "last": stats.values[-1] if stats.values else None,
            }
        return {"sensor": target, "metrics": metrics}

    @mcp.tool()
    def recent(sensor: str, limit: int = 10) -> dict[str, Any]:
        """Show me the last few samples for a sensor — newest first, with age. Answers
        'what does it look like right now?', 'show me recent CPU samples', 'how many
        models are loaded right now?'. `sensor` is 'os', 'docker', or 'ollama'.
        """
        rows = store.read_samples(sensor, limit=limit)
        return {
            "sensor": sensor,
            "count": len(rows),
            "samples": [
                {
                    "ts": r["ts"],
                    "age_seconds": round(time.time() - r["ts"], 1),
                    "payload": r["payload"],
                }
                for r in rows
            ],
        }

    @mcp.tool()
    def doctor() -> dict[str, Any]:
        """Is Mechanic healthy on this box? Reports which sensors are available
        (os, docker, ollama) and storage status. Use this for 'is mechanic working?'
        / 'what can it watch here?' / first-run sanity checks.
        """
        sensors = []
        for s in _sensor_registry.all():
            try:
                avail = bool(s.is_available())
            except Exception:  # noqa: BLE001
                avail = False
            sensors.append({"name": s.name, "available": avail})
        store_ok = True
        sample_count = 0
        try:
            sample_count = store.count()
        except Exception:  # noqa: BLE001
            store_ok = False
        return {
            "ok": store_ok,
            "version": _version(),
            "store": {
                "path": str(store.config.db_path),
                "schema_version": SCHEMA_VERSION,
                "total_samples": sample_count,
                "ok": store_ok,
            },
            "sensors": sensors,
        }

    return mcp


def _make_judge(store: Store, config: Config):
    """Build a closure that judges whether a metric value is anomalous against its
    recorded baseline. Used by what_changed_since to gate scalar deltas — only
    values outside the baseline surface, so routine drift doesn't flood the result.
    """

    def judge(metric: str, value: float) -> bool:
        b = _hydrate_baseline(store, config, metric)
        stats = b.stats_for(metric)
        if stats is None or stats.n < config.min_samples:
            return False  # cold-start: can't call it anomalous yet
        return b.evaluate(metric, value).is_anomaly

    return judge


def _diff_samples(
    sensor: str,
    past: dict,
    latest: dict,
    judge=None,
) -> dict[str, Any]:
    """Compute the human-meaningful diff between two samples of one sensor.

    Set-valued keys (containers, models) surface on any add/remove — always
    meaningful. Scalar keys surface only when the new value is anomalous against
    its baseline (via `judge`); without a judge, falls back to any-nonzero-delta
    (used where baseline gating isn't wanted, e.g. tests of the raw diff).
    """
    out: dict[str, Any] = {}
    set_keys = _SET_METRICS.get(sensor, [])
    for key in set_keys:
        old_set = set(past.get(key, []) or [])
        new_set = set(latest.get(key, []) or [])
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        if added or removed:
            out[key] = {"added": added, "removed": removed}
    # scalar deltas for numeric keys (excluding set members and booleans)
    for k, new_v in latest.items():
        if k in set_keys:
            continue
        old_v = past.get(k)
        is_numeric = (
            isinstance(new_v, (int, float))
            and isinstance(old_v, (int, float))
            and not isinstance(new_v, bool)
        )
        if not is_numeric:
            continue
        delta = new_v - old_v
        if abs(delta) <= 1e-9:
            continue
        if judge is not None:
            # Gate on anomaly: only surface if the new value is outside the baseline.
            metric = f"{sensor}.{k}"
            if not judge(metric, float(new_v)):
                continue
        out[k] = {"from": old_v, "to": new_v, "delta": round(delta, 4)}
    return out


def _std_of(stats) -> float:
    if stats.n < 2:
        return 0.0
    mean = stats.sum / stats.n
    var = stats.sumsq / stats.n - mean * mean
    return math.sqrt(max(var, 0.0))


def _version() -> str:
    from mechanic import __version__

    return __version__


def main() -> None:
    """Entry point for `mechanic server` — opens the store and runs the stdio server."""
    cfg = Config().resolve()
    cfg.ensure_dirs()
    store = Store(cfg)
    store.open()
    mcp = build_server(store, cfg)
    mcp.run()  # stdio transport by default in FastMCP


if __name__ == "__main__":
    main()
