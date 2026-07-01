#!/usr/bin/env bash
# Mechanic — uninstaller. User-level: stops the supervisor, removes the install.
# Data and config are kept by default (pass --purge to remove them too).

set -euo pipefail

if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; DIM='\033[2m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; DIM=''; NC=''
fi

log()  { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*" >&2; }
info() { printf "${DIM}%s${NC}\n" "$*"; }

PREFIX="${MECHANIC_PREFIX:-$HOME/.local}"
INSTALL_DIR="${MECHANIC_INSTALL_DIR:-$PREFIX/share/mechanic}"
VENV_DIR="${MECHANIC_VENV_DIR:-$INSTALL_DIR/.venv}"
BIN_LINK="$PREFIX/bin/mechanic"
CONFIG_DIR="${MECHANIC_CONFIG_DIR:-$HOME/.config/mechanic}"
DATA_DIR="${MECHANIC_DATA_DIR:-$PREFIX/share/mechanic-data}"
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1;;
    -h|--help)
      echo "Usage: uninstall.sh [--purge]   (default keeps data + config)"; exit 0;;
    *) warn "Unknown arg: $arg";;
  esac
done

# --- stop + remove supervisor ------------------------------------------------
OS="$(uname -s)"
if [[ "$OS" == "Darwin" ]]; then
  PLIST="$HOME/Library/LaunchAgents/dev.mechanic.sampler.plist"
  if [[ -f "$PLIST" ]]; then
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    log "Removed launchd agent"
  fi
else
  UNIT="mechanic-sampler.service"
  if systemctl --user list-unit-files 2>/dev/null | grep -q "$UNIT"; then
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/$UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    log "Removed systemd --user unit"
  fi
fi

# --- remove Claude wiring ----------------------------------------------------
CLAUDE_JSON="$HOME/.claude.json"
if [[ -f "$CLAUDE_JSON" ]] && [[ "$VENV_DIR/bin/python" != "" ]] && [[ -x "$VENV_DIR/bin/python" ]]; then
  "$VENV_DIR/bin/python" - <<PYEOF 2>/dev/null || true
import json, os
path = os.path.expanduser("$CLAUDE_JSON")
try:
    with open(path) as f: cfg = json.load(f)
except Exception:
    raise SystemExit(0)
mcp = cfg.get("mcpServers", {})
if "mechanic" in mcp:
    del mcp["mechanic"]
    with open(path, "w") as f: json.dump(cfg, f, indent=2)
    print("✓ removed 'mechanic' from ~/.claude.json")
PYEOF
fi

# --- remove install ----------------------------------------------------------
rm -f "$BIN_LINK"
if [[ "$PURGE" == "1" ]]; then
  rm -rf "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR"
  log "Purged install dir, config, and data"
else
  rm -rf "$INSTALL_DIR"
  log "Removed install dir (kept data: $DATA_DIR and config: $CONFIG_DIR)"
  info "Run with --purge to also remove data + config."
fi

log "Uninstalled."
