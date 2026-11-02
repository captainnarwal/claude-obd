#!/usr/bin/env bash
# PreToolUse hook for the Read tool: when Claude is about to read a large telemetry-looking file directly, add context
# steering it to the obd engine. Never blocks (exit 0 always); any problem simply lets the Read proceed.
# Threshold in MB: OBD_READ_GUARD_MB (default 2).

input="$(cat)"
file="$(printf '%s' "$input" | sed -n 's/.*"file_path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
[ -n "$file" ] || exit 0
file="$(printf '%s' "$file" | sed 's/\\\\/\//g')"   # JSON-escaped Windows backslashes -> forward slashes (Git Bash)

case "$(printf '%s' "$file" | tr 'A-Z' 'a-z')" in
  *.csv|*.tsv|*.txt|*.log|*.xlsx|*.xls) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

limit_mb="${OBD_READ_GUARD_MB:-2}"
case "$limit_mb" in *[!0-9]*|'') limit_mb=2 ;; esac
size="$(wc -c < "$file" 2>/dev/null | tr -d '[:space:]')"
[ -n "$size" ] || exit 0
[ "$size" -ge $((limit_mb * 1024 * 1024)) ] || exit 0

mb=$((size / 1024 / 1024))
launcher="${CLAUDE_PLUGIN_ROOT:-<plugin>}/scripts/obd"
msg="This file is about ${mb} MB. If it is a vehicle log (OBD/ECU export), do not read it directly: it would flood the context and skip the engine's validation. Use \`${launcher} inspect <path> --json\`, then \`ingest\`, \`overview\`, \`query\`, \`stats\` or \`analyze\`. If it is not a vehicle log, carry on and read only the part you need (offset/limit)."
msg="$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')"
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}\n' "$msg"
exit 0
