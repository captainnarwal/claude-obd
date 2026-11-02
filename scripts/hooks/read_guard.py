#!/usr/bin/env python3
"""PreToolUse hook for the Read tool: when Claude is about to read a large telemetry-looking file directly, add context
steering it to the obd engine. Never blocks (exit 0 always); any problem simply lets the Read proceed.
Threshold in MB: OBD_READ_GUARD_MB (default 2)."""
import json
import os
import sys
from pathlib import Path

EXTS = {".csv", ".tsv", ".txt", ".log", ".xlsx", ".xls"}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        file = Path(str(json.loads(raw).get("tool_input", {}).get("file_path", "")))
        if file.suffix.lower() not in EXTS or not file.is_file():
            return
        mb_limit = os.environ.get("OBD_READ_GUARD_MB", "2")
        limit = int(mb_limit) if mb_limit.isdigit() else 2
        size = file.stat().st_size
    except (OSError, ValueError, AttributeError):
        return
    if size < limit * 1024 * 1024:
        return
    launcher = f'python "{os.environ.get("CLAUDE_PLUGIN_ROOT", "<plugin>")}/scripts/obd.py"'
    msg = (f"This file is about {size // 1024 // 1024} MB. If it is a vehicle log (OBD/ECU export), do not read it directly: "
           "it would flood the context and skip the engine's validation. "
           f"Use `{launcher} inspect <path> --json`, then `ingest`, `overview`, `query`, `stats` or `analyze`. "
           "If it is not a vehicle log, carry on and read only the part you need (offset/limit).")
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}) + "\n")


if __name__ == "__main__":
    main()
