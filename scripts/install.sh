#!/usr/bin/env bash
# Mechanic — cross-platform, user-level installer.
#
# What it does:
#   1. Detects macOS / Linux, checks prerequisites (python3.11+, pip, git).
#   2. Creates a venv under the install prefix and pip-installs Mechanic (-e).
#   3. Writes a default config (~/.config/mechanic/mechanic.ini) if none exists.
#   4. Installs a USER-LEVEL supervisor for the sampler:
#        macOS  -> ~/Library/LaunchAgents/dev.mechanic.sampler.plist  (launchctl load)
#        Linux  -> ~/.config/systemd/user/mechanic-sampler.service    (systemctl --user enable --now)
#      (The MCP *server* is launched on-demand by the AI client — no supervisor needed.)
#   5. Offers to wire Mechanic into the Claude Code MCP config (~/.claude.json),
#      idempotently, with a timestamped backup first.
#   6. Runs `mechanic doctor` to summarize what's working.
#
# No sudo required. Everything lives under the user's home / XDG dirs so the same
# installer runs on anyone's box.

set -euo pipefail

# --- pretty printing ---------------------------------------------------------
if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
  BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; BOLD=''; DIM=''; NC=''
fi

log()  { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*" >&2; }
err()  { printf "${RED}✗${NC} %s\n" "$*" >&2; }
info() { printf "${DIM}%s${NC}\n" "$*"; }
hdr()  { printf "\n${BLUE}╶─${NC} %s\n" "$*"; }

# --- config knobs (overridable via env) --------------------------------------
PREFIX="${MECHANIC_PREFIX:-$HOME/.local}"
INSTALL_DIR="${MECHANIC_INSTALL_DIR:-$PREFIX/share/mechanic}"
VENV_DIR="${MECHANIC_VENV_DIR:-$INSTALL_DIR/.venv}"
BIN_LINK="$PREFIX/bin/mechanic"
CONFIG_DIR="${MECHANIC_CONFIG_DIR:-$HOME/.config/mechanic}"
DATA_DIR="${MECHANIC_DATA_DIR:-$PREFIX/share/mechanic-data}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_CLAUDE_WIRE="${MECHANIC_SKIP_CLAUDE_WIRE:-0}"

# --- preflight ---------------------------------------------------------------
hdr "Mechanic installer"

OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM="macos";;
  Linux)   PLATFORM="linux";;
  *) err "Unsupported OS: $OS (need macOS or Linux)"; exit 1;;
esac
log "Detected platform: $PLATFORM"

PYOK=0
if command -v python3 >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo 0)"
  PYMAJOR="${PYV%%.*}"; PYMINOR="${PYV#*.}"
  if [[ "$PYMAJOR" -gt 3 || ( "$PYMAJOR" -eq 3 && "$PYMINOR" -ge 11 ) ]]; then
    PYOK=1
  fi
fi
if [[ "$PYOK" -ne 1 ]]; then
  err "Python 3.11+ required (found: ${PYV:-none})"
  info "Install from https://python.org or: brew install python@3.12 / apt install python3.12"
  exit 2
fi
log "Python: $PYV"

for dep in git; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    err "Missing required command: $dep"
    exit 2
  fi
done
log "git present"

# pip: accept either a `pip` binary or `python3 -m pip` (newer pythons ship no `pip` shim)
if ! command -v pip >/dev/null 2>&1; then
  if ! python3 -m pip --version >/dev/null 2>&1; then
    err "pip not found (need `pip` or `python3 -m pip`)"
    info "Install: python3 -m ensurepip --upgrade"
    exit 2
  fi
fi
log "pip present"

# --- install -----------------------------------------------------------------
hdr "Installing into $VENV_DIR"

mkdir -p "$INSTALL_DIR" "$PREFIX/bin"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -e "$REPO_ROOT"
log "Mechanic installed into venv"

# Put a `mechanic` shim on PATH via $PREFIX/bin
mkdir -p "$PREFIX/bin"
cat > "$BIN_LINK" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/mechanic" "\$@"
EOF
chmod +x "$BIN_LINK"
log "CLI shim: $BIN_LINK"
if [[ ":$PATH:" != *":$PREFIX/bin:"* ]]; then
  warn "$PREFIX/bin is not on your PATH — add it to your shell rc, or use $BIN_LINK directly."
fi

# --- config ------------------------------------------------------------------
hdr "Configuration"
mkdir -p "$CONFIG_DIR"
INI="$CONFIG_DIR/mechanic.ini"
if [[ ! -f "$INI" ]]; then
  cat > "$INI" <<EOF
# Mechanic configuration. All values optional; defaults shown.
[sampler]
interval_seconds = 30
retention_days    = 30

[baseline]
window_size   = 2880
ewma_alpha    = 0.1
z_threshold   = 3.0
min_samples   = 30

[storage]
# data_dir defaults to $DATA_DIR
EOF
  log "Wrote default config: $INI"
else
  log "Existing config preserved: $INI"
fi
mkdir -p "$DATA_DIR"
log "Data dir: $DATA_DIR"

# Export the data dir for the supervisor so the daemon and CLI agree on storage.
export MECHANIC_DATA_DIR="$DATA_DIR"

# --- supervisor --------------------------------------------------------------
hdr "Supervisor (sampler daemon)"
SVC_NAME="mechanic-sampler"
if [[ "$PLATFORM" == "macos" ]]; then
  PLIST="$HOME/Library/LaunchAgents/dev.mechanic.sampler.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.mechanic.sampler</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV_DIR/bin/mechanic</string>
    <string>sampler</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MECHANIC_DATA_DIR</key><string>$DATA_DIR</string>
    <key>PATH</key><string>$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DATA_DIR/sampler.log</string>
  <key>StandardErrorPath</key><string>$DATA_DIR/sampler.err.log</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  launchctl load   "$PLIST" >/dev/null 2>&1
  log "launchd agent installed + loaded: $PLIST"
  info "(logs: $DATA_DIR/sampler.log)"
else
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  UNIT="$UNIT_DIR/$SVC_NAME.service"
  cat > "$UNIT" <<EOF
[Unit]
Description=Mechanic sampling daemon
After=network.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/mechanic sampler
Environment=MECHANIC_DATA_DIR=$DATA_DIR
Restart=on-failure
RestartSec=10
StandardOutput=append:$DATA_DIR/sampler.log
StandardError=append:$DATA_DIR/sampler.err.log

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$SVC_NAME" >/dev/null 2>&1
  log "systemd --user unit installed + enabled: $UNIT"
  info "(logs: $DATA_DIR/sampler.log)"
  # Keep the user service alive after logout (lingering).
  loginctl enable-linger "$USER" 2>/dev/null || true
fi

# --- wire Claude Code MCP entry ---------------------------------------------
hdr "AI client wiring"
CLAUDE_JSON="$HOME/.claude.json"
wire_claude() {
  if [[ ! -f "$CLAUDE_JSON" ]]; then
    warn "~/.claude.json not found — skipping Claude Code wiring."
    info "When you install Claude Code, re-run this script or add the server manually:"
    info "  mechanic server  (as an stdio MCP server)"
    return
  fi
  # Timestamped backup
  cp "$CLAUDE_JSON" "$CLAUDE_JSON.mechanic-bak.$(date +%Y%m%d%H%M%S)"
  # Idempotent insert via python (json-safe)
  "$VENV_DIR/bin/python" - <<PYEOF
import json, sys, os
path = os.path.expanduser("$CLAUDE_JSON")
with open(path) as f:
    cfg = json.load(f)
mcp = cfg.setdefault("mcpServers", {})
venv = "$VENV_DIR"
mcp["mechanic"] = {
    "type": "stdio",
    "command": f"{venv}/bin/mechanic",
    "args": ["server"],
    "env": {"MECHANIC_DATA_DIR": "$DATA_DIR"},
}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("✓ wired 'mechanic' server into ~/.claude.json")
PYEOF
}

if [[ "$SKIP_CLAUDE_WIRE" == "1" ]]; then
  info "Claude wiring skipped (MECHANIC_SKIP_CLAUDE_WIRE=1)"
else
  wire_claude
fi

# --- doctor ------------------------------------------------------------------
hdr "Health check"
"$VENV_DIR/bin/mechanic" doctor || true

printf "\n${BOLD}Done.${NC} Mechanic is sampling in the background.\n"
info "Next: restart your AI client so it picks up the 'mechanic' MCP server, then ask it:"
info "  \"run the mechanic doctor tool\" / \"is 95%% CPU normal right now?\""
info "Uninstall: scripts/uninstall.sh"
