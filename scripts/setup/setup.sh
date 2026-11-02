#!/usr/bin/env bash
# /obd setup: build an isolated engine environment in ${CLAUDE_PLUGIN_DATA}/venv, then self-test.
# Usage: setup.sh [--install-uv]   (--install-uv only after the user has agreed to install uv)
set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SELF_DIR/../.." && pwd)}"
DATA="${CLAUDE_PLUGIN_DATA:-$ROOT/.obd-plugin-data}"
INSTALL_UV=0; [ "${1:-}" = "--install-uv" ] && INSTALL_UV=1
log() { echo "[obd setup] $*" >&2; }
fail() { # code message hint
  printf '{"status":"error","summary":"%s","error":{"code":"%s","message":"%s","hint":"%s"}}\n' "$2" "$1" "$2" "$3"; exit 4; }

mkdir -p "$DATA" || fail SETUP_FAILED "Cannot create $DATA" "Check folder permissions."
ENGINE_VERSION="$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "$ROOT/.claude-plugin/plugin.json" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"

# 1. find a Python manager
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ] && [ "$INSTALL_UV" = 1 ]; then
  log "Installing uv (official installer from astral.sh)..."
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" >&2 ;;
    *) curl -LsSf https://astral.sh/uv/install.sh | sh >&2 ;;
  esac
  UV="$(command -v uv || true)"; [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
fi

VENV="$DATA/venv"
INSTALLER=""
if [ -n "$UV" ]; then
  log "Using uv: $UV"
  # 2+3. isolated environment, locked dependency set
  # uv is a native Windows program under Git Bash: give it Windows-style paths
  WENGINE="$ROOT/engine"; WVENV="$VENV"
  if command -v cygpath >/dev/null 2>&1; then WENGINE="$(cygpath -m "$ROOT/engine")"; WVENV="$(cygpath -m "$VENV")"; fi
  UV_PROJECT_ENVIRONMENT="$WVENV" "$UV" sync --frozen --no-dev --no-editable --reinstall-package obd-engine --python 3.12 --project "$WENGINE" >&2 \
    || fail SETUP_FAILED "uv could not build the engine environment" "Check your internet connection or proxy, then re-run /obd setup. Details are in the output above."
  INSTALLER="uv"
else
  PYBIN=""
  for c in python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then PYBIN="$c"; break; fi
  done
  if [ -z "$PYBIN" ]; then
    fail NEEDS_UV "uv is not installed and no Python 3.11+ was found" "Ask the user for permission, then run setup.sh --install-uv (installs uv from astral.sh)."
  fi
  log "uv not found; falling back to $PYBIN -m venv + pip (dependencies are not hash-pinned in this mode)"
  "$PYBIN" -m venv "$VENV" >&2 || fail SETUP_FAILED "python -m venv failed" "Install the python venv package or install uv."
  VPY="$VENV/bin/python"; [ -x "$VENV/Scripts/python.exe" ] && VPY="$VENV/Scripts/python.exe"
  "$VPY" -m pip install --quiet "$ROOT/engine" >&2 || fail SETUP_FAILED "pip install failed" "Check your internet connection or proxy, then re-run /obd setup."
  INSTALLER="pip"
fi

VPY="$VENV/bin/python"; [ -x "$VENV/Scripts/python.exe" ] && VPY="$VENV/Scripts/python.exe"
PYV="$("$VPY" -c 'import sys; print(sys.version.split()[0])')"
# 4. self-test through the launcher so the real call path is exercised
log "Running self-test..."
SELFTEST="$(CLAUDE_PLUGIN_DATA="$DATA" CLAUDE_PLUGIN_ROOT="$ROOT" OBD_PYTHON="$VPY" "$ROOT/scripts/obd" selftest --json)"; RC=$?
if [ $RC -ne 0 ]; then
  printf '%s\n' "$SELFTEST"; log "Self-test failed (see JSON on stdout)."; exit 4
fi
# 5. record
printf '{"engine_version":"%s","python_version":"%s","installer":"%s","setup_at":"%s"}\n' "$ENGINE_VERSION" "$PYV" "$INSTALLER" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DATA/setup.json"
printf '{"status":"ok","summary":"Setup complete (engine %s, Python %s, via %s). Put your scanner exports in a folder and say: analyze my log - or run /obd scan.","data":{"engine_version":"%s","python_version":"%s","installer":"%s","venv":"%s"}}\n' \
  "$ENGINE_VERSION" "$PYV" "$INSTALLER" "$ENGINE_VERSION" "$PYV" "$INSTALLER" "$VENV"
