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

# --- remove MCP client wiring (Claude, Codex, Antigravity — whichever exist) -
PY="$VENV_DIR/bin/python"
[[ -x "$PY" ]] || PY=""

# Claude + Antigravity: JSON, drop the key
for path in "$HOME/.claude.json" "$HOME/.gemini/antigravity/mcp_config.json"; do
  if [[ -f "$path" ]] && [[ -n "$PY" ]]; then
    "$PY" - "$path" <<'PYEOF' 2>/dev/null || true
import json, sys
path = sys.argv[1]
try:
    with open(path) as f: cfg = json.load(f)
except Exception:
    raise SystemExit(0)
mcp = cfg.get("mcpServers", {})
if "mechanic" in mcp:
    del mcp["mechanic"]
    with open(path, "w") as f: json.dump(cfg, f, indent=2)
    print(f"✓ removed 'mechanic' from {path}")
PYEOF
  fi
done

# Codex: TOML — delete the [mcp_servers.mechanic] + [mcp_servers.mechanic.env] blocks.
CODEX_TOML="$HOME/.codex/config.toml"
if [[ -f "$CODEX_TOML" ]]; then
  # awk: drop lines from a '# Mechanic' comment line through the end of the
  # [mcp_servers.mechanic.env] block (the next blank line that's followed by a
  # non-continuation line). Simplest robust approach: delete from the marker
  # comment to the next top-level table that isn't mcp_servers.mechanic.* .
  if grep -q "mcp_servers.mechanic" "$CODEX_TOML"; then
    cp "$CODEX_TOML" "$CODEX_TOML.mechanic-uninstall-bak.$(date +%Y%m%d%H%M%S)"
    "$PY" - "$CODEX_TOML" <<'PYEOF' 2>/dev/null || true
import sys, re
path = sys.argv[1]
s = open(path).read()
# Remove the installer-added block: from the '# Mechanic' marker line (if present)
# or the [mcp_servers.mechanic] header, through the end of its env sub-table.
# The block is contiguous: [mcp_servers.mechanic] ... [mcp_servers.mechanic.env] ... <blank>
pattern = re.compile(
    r"\n*(?:# Mechanic[^\n]*\n)?\[mcp_servers\.mechanic\]\n.*?\n\[mcp_servers\.mechanic\.env\]\n.*?(?=\n\[|\Z)",
    re.DOTALL,
)
s2 = pattern.sub("\n", s)
if s2 != s:
    open(path, "w").write(s2)
    print(f"✓ removed 'mechanic' from {path}")
PYEOF
  fi
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
