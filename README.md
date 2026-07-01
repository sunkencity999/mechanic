# Mechanic

> A local-first baseline & anomaly daemon that gives your AI assistants **memory of your machine** — over MCP.

```
            ┌──────────────────────────────┐
            │   you ask your AI assistant    │
            │   "is 95% CPU normal right now?"│
            └───────────────┬──────────────┘
                            │ MCP (stdio)
                ┌───────────▼───────────┐
                │   mechanic server     │  ← thin reader; spawns on demand
                │  (is_this_normal,      │
                │   what_changed_since,  │
                │   baseline_for, ...)   │
                └───────────┬───────────┘
                            │ reads
                ┌───────────▼───────────┐
                │   mechanic.sqlite      │  ← one small local file
                └───────────▲───────────┘
                            │ writes
                ┌───────────┴───────────┐
                │   mechanic sampler     │  ← long-running daemon
                │   (os / docker /       │     (launchd · systemd --user)
                │    ollama sensors)     │
                └───────────────────────┘
```

## What & why

Every AI terminal assistant — Claude Code, `aichat`, `mods`, your own agent — starts **blind about your box.** Ask it "why is this slow?" and it has no idea what *your* normal is, so it falls back to generic advice pulled from training data. `btop` and `glances` show you numbers in the moment but don't *remember* them, and they don't talk to your AI.

Mechanic closes that loop. It quietly samples your machine into a local SQLite file, learns a per-metric baseline, and exposes that history to any MCP-speaking client. Now your assistant can answer **"is this normal *for me*?"** — and actually know.

It is:

- **Local-first.** Everything stays in `~/.local/share/mechanic-data/`. No cloud, no egress, no telemetry, no account.
- **Private.** Container names, loaded model names, process counts never leave your box.
- **MCP-native.** Not a new assistant. It slots into the tools you already use.
- **User-level.** No `sudo`. Runs under your own launchd / systemd --user.
- **Runs anywhere.** macOS and Linux. Sensors no-op gracefully when their backend isn't installed.

## The gap it fills

| Tool | What it does | What it doesn't |
|---|---|---|
| `btop` / `glances` | live view, right now | forgets the moment you close it; can't talk to your AI |
| Datadog / New Relic | cloud, dashboards, alerting | $$, ships data off-box, not AI-native |
| `etckeeper` / AIDE | file integrity | just files; no runtime state |
| (nothing) | "what's normal *for this box*, queryable by an LLM" | **this is what Mechanic is** |

## Quickstart

```bash
# from the repo root
bash scripts/install.sh
```

That installs Mechanic into a venv under `~/.local/share/mechanic`, starts the sampler
under your user supervisor (launchd on macOS, systemd --user on Linux), and offers to
wire it into Claude Code's MCP config. When it's done, restart your AI client and ask:

> *"Use the mechanic tools — is the current memory pressure normal for this machine?"*

Or, without any AI client:

```bash
mechanic once      # sample every available sensor once
mechanic status    # show the most recent sample per sensor
mechanic doctor    # sensor availability + storage health
```

## Install

### One-liner (from a checkout)

```bash
bash scripts/install.sh
```

### Overridable knobs (env vars)

| Variable | Default | What |
|---|---|---|
| `MECHANIC_PREFIX` | `$HOME/.local` | where the venv + bin shim live |
| `MECHANIC_INSTALL_DIR` | `$PREFIX/share/mechanic` | venv parent |
| `MECHANIC_CONFIG_DIR` | `$HOME/.config/mechanic` | `mechanic.ini` location |
| `MECHANIC_DATA_DIR` | `$PREFIX/share/mechanic-data` | SQLite + logs |
| `MECHANIC_SKIP_CLAUDE_WIRE` | `0` | set `1` to skip editing `~/.claude.json` |

### Uninstall

```bash
bash scripts/uninstall.sh          # stops daemon, removes install, keeps data + config
bash scripts/uninstall.sh --purge  # also removes data + config
```

## The MCP tools

These are what your AI client sees. All read-only, all return JSON designed to be reasoned over.

### `is_this_normal(metric, value)`
Is `value` within the learned baseline for `metric`? Returns `normal`, `z_score`, `mean`, `std`, `n`, `cold_start`.
```json
{"metric": "os.cpu_pct", "value": 95.0, "normal": false, "z_score": 12.3, "mean": 11.2, "std": 6.1, "n": 2880, "cold_start": false}
```
Metric names are `<sensor>.<key>`, e.g. `os.cpu_pct`, `docker.n_containers`, `ollama.n_models_loaded`.

### `what_changed_since(minutes_ago)`
Set-differences in sensor state vs N minutes ago. For set-valued metrics (container names, loaded models) reports `added` / `removed`; for scalars reports `from` / `to` / `delta`.
```json
{"minutes_ago": 60, "changes": [
  {"sensor": "docker", "container_names": {"added": ["db"], "removed": []}},
  {"sensor": "ollama", "loaded_models": {"added": ["qwen3:32b"], "removed": ["llama3.2:3b"]}}
]}
```

### `baseline_for(target)`
Stats for one metric (`os.cpu_pct`) or a whole sensor (`os`). Returns `mean`, `std`, `min`, `max`, `last`, `n`, `ewma`.

### `recent(sensor, limit)`
The last `limit` samples for a sensor, newest first, with age.

### `doctor()`
What's installed and working on this box: sensor availability flags, storage path, total sample count, schema version. The single source of truth for "is Mechanic healthy here?"

### Example prompts to try

These are written for an AI client (Claude Code, etc.) that has the `mechanic` MCP server connected. Copy them verbatim or adapt — they're grouped by what you're actually trying to do.

**First run / "is this thing on?"**
- *"Run the mechanic doctor tool and tell me what's available on this machine."*
- *"Is mechanic healthy? Show me storage status and which sensors are active."*
- *"Use mechanic's recent tool to show me the last few os samples — is the sampler actually running?"*

**"Is this normal?" — the core question**
- *"Is the current CPU usage normal for this machine, or should I be worried?"*
- *"Check mechanic — is my memory pressure right now within the normal baseline?"*
- *"Is 90% disk usage normal here, or has it crept up compared to usual?"*
- *"Is the load average I'm seeing unusual for this box?"*
- *"Is the number of running Docker containers normal, or did something spin up?"*
- *"Is the amount of VRAM Ollama is using right now typical for me?"*

**"What changed?" — the diff question**
- *"What changed on this box in the last hour?"*
- *"Use mechanic to tell me what's different now versus 30 minutes ago."*
- *"Did any new Docker containers start, or any stop, since I left for lunch?"*
- *"Have the loaded Ollama models changed since this morning?"*
- *"What changed in the last 24 hours? Summarize just the differences."*

**"Give me the numbers" — baselines and history**
- *"What's the baseline for memory usage? Give me the mean and standard deviation."*
- *"Show me the baseline for every os metric — cpu, mem, disk, load."*
- *"What's the usual CPU range on this machine? Min and max over the baseline window."*
- *"Show me the recent ollama samples — how many models are usually loaded?"*
- *"Give me the last 20 docker samples so I can see the pattern."*

**Investigating a slowdown or spike**
- *"Something feels slow. Use mechanic to check whether CPU, memory, and load are all within normal range right now."*
- *"I just noticed a fan spin up. Did anything change on the box in the last 15 minutes?"*
- *"Is the current memory usage anomalous? If so, by how many standard deviations?"*
- *"Pull the recent os samples and tell me if anything looks outside the baseline."*

**Ollama / model-loading workflows** (a real differentiator — no one else instruments this)
- *"Is the VRAM Ollama's using right now normal for me, or is something bigger loaded than usual?"*
- *"What Ollama models are loaded right now, and is that the usual set? What changed recently?"*
- *"I loaded a big model and things feel tight — is memory pressure anomalous because of it?"*

**Docker workflows**
- *"How many containers are running, and is that a normal number for this box?"*
- *"Did a container stop or start in the last hour? Use what_changed_since."*
- *"Show me the baseline for container count — am I running way more than usual?"*

**Multi-tool / "investigate for me"**
- *"Something's off with this box. Run doctor, then check whether CPU, memory, and load are all within their baselines, and tell me what changed in the last hour. Give me a one-paragraph summary."*
- *"Am I in a healthy state right now? Check the baselines for the os sensor and flag anything anomalous."*
- *"I'm about to run a big job. Snapshot the current state with mechanic so I can ask 'what changed?' afterwards."*

**Tip:** you don't have to know which tool to call — just describe the situation in your own words and let the AI pick `is_this_normal`, `what_changed_since`, `baseline_for`, or `recent` as needed. The tool names are there for when you want to be explicit.

## Configuration

`~/.config/mechanic/mechanic.ini` (created by the installer with defaults if absent):

```ini
[sampler]
interval_seconds = 30      # how often the daemon samples
retention_days    = 30      # how long samples live in SQLite

[baseline]
window_size   = 2880       # rolling window (2880 ≈ 24h @ 30s)
ewma_alpha    = 0.1         # recent-bias weight
z_threshold   = 3.0          # |value-mean|/effective_std above this = anomaly
min_samples   = 30          # cold-start: never flag until this many samples
```

All values are also overridable via env vars (`MECHANIC_INTERVAL`, `MECHANIC_RETENTION_DAYS`, `MECHANIC_WINDOW_SIZE`, `MECHANIC_Z_THRESHOLD`, `MECHANIC_MIN_SAMPLES`).

## Adding a sensor

A sensor is one file satisfying a tiny protocol. Drop this in `mechanic/plugins/`:

```python
# mechanic/plugins/mything_sensor.py
from mechanic.plugins.base import SensorError

class MythingSensor:
    name = "mything"

    def is_available(self) -> bool:
        # cheap probe — return False when the backend isn't here; the
        # sampler will skip you and doctor() will report "not available"
        import shutil
        return shutil.which("mything") is not None

    def sample(self) -> dict:
        if not self.is_available():
            raise SensorError("mything not installed")
        # return a FLAT, JSON-serializable dict; keys become metric names
        return {"widgets": 42, "frobnicated_pct": 12.5}

sensor = MythingSensor()
```

That's it. The registry auto-discovers it on import; `mechanic doctor` lists it; the sampler calls it every cycle; `mything.widgets` becomes a baseline-able metric the AI can ask about. Three rules:

1. **Flat dict.** No nested dicts or lists-as-metrics (lists are for set-valued keys like `container_names` that `what_changed_since` knows how to diff).
2. **JSON-serializable.** The store persists payloads with `json.dumps`.
3. **Fail soft.** Raise `SensorError` on a real failure; the sampler isolates it and logs a warning. One sensor's error never stops the loop.

## Architecture

**Two cooperating processes, one file.**

- **`mechanic sampler`** — the long-running daemon (managed by launchd / systemd --user). The *single writer*. Walks the available sensors once per interval, persists each sample to SQLite. One sensor failing is isolated; SIGTERM/SIGINT trigger a clean exit after the current cycle.
- **`mechanic server`** — the MCP stdio server, spawned on demand by the AI client. A *reader only* — no sensor calls happen in-process, so it's cheap and there's no concurrency on the store.

Keeping writer and reader in separate processes (rather than one daemon that also serves MCP) means the AI client lifecycle and the sampling lifecycle are decoupled: the sampler keeps recording whether or not any client is connected, and a freshly-spawned server answers from the full recorded history immediately (it's stateless — see Design notes).

**Why SQLite?** One file, zero config, portable, good enough for tens of thousands of rows per sensor, and it's already on every machine. The schema is intentionally tiny: `samples(id, ts, sensor, payload)` plus indexes. If you outgrow it, the store is one module — swap it for DuckDB or SQLite-WAL or a real TSDB without touching the sensors or the server.

**Why Welford + a rolling window?** A bounded rolling window means a single spike can't permanently poison the mean (the outlier evicts out after `window_size` samples). Running sum/sum-of-squares keep updates O(1). The anomaly decision floors the std at 5% of the metric's scale so a rock-steady stream (std ~0) doesn't make the z-score explode on harmless jitter — but a genuine jump (10 → 999) is still flagged.

## Project layout

```
mechanic/
  README.md
  LICENSE                       # MIT
  pyproject.toml
  mechanic/                     # import package
    config.py                   # Config dataclass + env/ini loader
    store.py                    # SQLite schema + read/write (pure I/O)
    baseline.py                 # rolling stats + EWMA + anomaly (pure math)
    sampler.py                  # the daemon loop (glue)
    server.py                   # FastMCP stdio server (reader)
    cli.py                      # `mechanic` entrypoint
    plugins/
      base.py                   # SensorPlugin protocol + registry
      os_sensor.py             # psutil: cpu/mem/disk/net/load/procs
      docker_sensor.py         # `docker ps` (no SDK dep)
      ollama_sensor.py         # /api/ps (loaded models)
  scripts/
    install.sh                  # cross-platform, user-level installer
    uninstall.sh
  tests/                        # pytest; ~60 tests, bottom-up TDD
```

## Security

- No network egress, ever. The only socket Mechanic opens is to `127.0.0.1:11434` (your local Ollama).
- All data lives in `~/.local/share/mechanic-data/mechanic.sqlite`. Delete the file, delete the history.
- The MCP server is a reader; it cannot mutate the store or run sensors.
- The installer never asks for root. It edits your own `~/.claude.json` with a timestamped backup.

## Roadmap

Mechanic is the first of two companion tools. The second — **Drift** — is a `diff` for live systems: snapshot a box's operational state (ports, services, packages, cron, containers, users), then days later ask "what changed, in plain English?" Drift pairs naturally with Mechanic: Mechanic tells you the *numbers* are off; Drift tells you *what configuration* moved. Drift is next.

Other v1.1+ ideas: more sensors (network connections by host, systemd units, cron drift), alerting hooks, remote aggregation across a homelab, a richer time-series backend.

## Design notes

**Stateless server.** The MCP server holds no in-memory baseline state. Every call to `is_this_normal` or `baseline_for` rebuilds a `Baseline` from the store's recorded history and judges the queried value with a non-mutating `evaluate()`. This means: a spike you ask about never pollutes the baseline for the next query; `n` always reflects recorded history, not the value being tested; and a freshly-spawned server answers with the full history immediately — no cold-start, no warmup, no drift between what the sampler recorded and what the server reports. The cost is a bounded read (≤ `window_size` rows) per call against local SQLite, which is cheap.

**Effective-std floor.** The anomaly decision uses `z = |value − mean| / max(std, 0.05 · scale)` where `scale = max(|mean|, 1.0)`. The `max(std, floor)` keeps a rock-steady stream (std ≈ 0) from making the z-score explode on harmless jitter — a CPU that's sat at 50.0 for an hour won't scream "anomaly!" when it ticks to 50.5 — while still flagging genuine jumps (10 → 999 is caught). Tunable via `z_threshold` and the `_MIN_STD_FRACTION` constant.

## License

MIT — see `LICENSE`.
