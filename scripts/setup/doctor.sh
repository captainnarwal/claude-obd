#!/usr/bin/env bash
# /obd doctor: check the environment and print fixes. Always exits 0; read the JSON.
set -u
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SELF_DIR/../.." && pwd)}"
DATA="${CLAUDE_PLUGIN_DATA:-$ROOT/.obd-plugin-data}"
checks=""; ok=1
add() { # name ok detail fix
  checks="$checks{\"check\":\"$1\",\"ok\":$2,\"detail\":\"$3\",\"fix\":\"$4\"},"; [ "$2" = false ] && ok=0; }

if command -v uv >/dev/null 2>&1; then add uv true "$(uv --version 2>/dev/null)" ""
else add uv false "uv not on PATH (optional if the venv exists)" "setup.sh --install-uv, or install from https://docs.astral.sh/uv/"; fi
VPY="$DATA/venv/bin/python"; [ -x "$DATA/venv/Scripts/python.exe" ] && VPY="$DATA/venv/Scripts/python.exe"
if [ -x "$VPY" ]; then
  add venv true "engine environment present" ""
  PLUGIN_VERSION="$(grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' "$ROOT/.claude-plugin/plugin.json" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
  SETUP_VERSION="$(grep -o '"engine_version"[[:space:]]*:[[:space:]]*"[^"]*"' "$DATA/setup.json" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
  if [ "$PLUGIN_VERSION" = "$SETUP_VERSION" ]; then add engine_version true "engine $SETUP_VERSION matches plugin" ""
  else add engine_version false "engine ${SETUP_VERSION:-none} vs plugin $PLUGIN_VERSION" "Run /obd setup to refresh the environment"; fi
  CLAUDE_PLUGIN_ROOT="$ROOT" CLAUDE_PLUGIN_DATA="$DATA" "$ROOT/scripts/obd" selftest --json >/dev/null 2>&1; RC=$?
  if [ $RC -eq 0 ]; then add selftest true "pipeline passed on the bundled sample" ""
  else add selftest false "self-test failed" "Run /obd setup; if it persists run scripts/obd selftest and read the stage detail"; fi
  CLAUDE_PLUGIN_ROOT="$ROOT" CLAUDE_PLUGIN_DATA="$DATA" "$ROOT/scripts/obd" list --json >/dev/null 2>&1; RC=$?
  if [ $RC -eq 0 ]; then add workspace true "current folder readable" ""
  else add workspace false "could not read .obd/ in the current folder" "Run /obd clean in this folder, then re-analyze"; fi
else
  add venv false "no engine environment found" "Run /obd setup"
fi
if [ $ok = 1 ]; then ST=ok; MSG="Everything looks healthy."; else ST=partial; MSG="Some checks failed; see fixes."; fi
printf '{"status":"%s","summary":"%s","data":{"checks":[%s]}}\n' "$ST" "$MSG" "${checks%,}"
exit 0
