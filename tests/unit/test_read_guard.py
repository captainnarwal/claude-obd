"""The Read-size hook steers Claude away from reading big telemetry files directly and never blocks."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hooks" / "read_guard.py"


def _run(path, mb_limit="1"):
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": str(path)}})
    env = {**os.environ, "OBD_READ_GUARD_MB": mb_limit, "CLAUDE_PLUGIN_ROOT": "/plugin"}
    r = subprocess.run([sys.executable, str(SCRIPT)], input=payload, capture_output=True, text=True, env=env, timeout=30)
    return r.returncode, r.stdout.strip()


def test_large_telemetry_file_gets_steering_context(tmp_path):
    big = tmp_path / "run 1.csv"
    big.write_bytes(b"a,b\n" + b"1,2\n" * (700_000))  # ~2.7 MB
    code, out = _run(big)
    assert code == 0
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "PreToolUse" and "permissionDecision" not in data  # steers, never blocks
    assert "/plugin/scripts/obd" in data["additionalContext"] and "inspect" in data["additionalContext"]


def test_small_files_other_types_and_missing_files_pass_silently(tmp_path):
    small = tmp_path / "small.csv"
    small.write_text("a,b\n1,2\n")
    other = tmp_path / "big.py"
    other.write_bytes(b"x = 1\n" * 500_000)
    for p in (small, other, tmp_path / "missing.csv"):
        assert _run(p) == (0, "")
    assert subprocess.run([sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True).returncode == 0


def test_hook_manifest_is_valid_and_script_is_lf():
    cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entry = cfg["hooks"]["PreToolUse"][0]
    cmd = entry["hooks"][0]["command"]
    # shell-form hooks run under Git Bash on Windows (PowerShell 5.1 has no `||`), so call the sh wrapper, which picks python3/python/py
    assert entry["matcher"] == "Read" and "read-guard.sh" in cmd and "CLAUDE_PLUGIN_ROOT" in cmd
    assert "||" not in cmd and "&&" not in cmd
    assert b"\r\n" not in SCRIPT.read_bytes()
