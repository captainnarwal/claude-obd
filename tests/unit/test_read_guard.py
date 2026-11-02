"""The Read-size hook steers Claude away from reading big telemetry files directly and never blocks."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hooks" / "read-guard.sh"


def _bash():
    if sys.platform == "win32":
        for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if os.path.exists(c):
                return c
        return None
    return shutil.which("bash")


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash available")


def _run(path, mb_limit="1"):
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": str(path)}})
    env = {**os.environ, "OBD_READ_GUARD_MB": mb_limit, "CLAUDE_PLUGIN_ROOT": "/plugin"}
    r = subprocess.run([BASH, str(SCRIPT)], input=payload, capture_output=True, text=True, env=env, timeout=30)
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
    assert subprocess.run([BASH, str(SCRIPT)], input="not json", capture_output=True, text=True).returncode == 0


def test_hook_manifest_is_valid_and_script_is_lf():
    cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entry = cfg["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Read" and "read-guard.sh" in entry["hooks"][0]["command"]
    assert "CLAUDE_PLUGIN_ROOT" in entry["hooks"][0]["command"]
    b = SCRIPT.read_bytes()
    assert b.startswith(b"#!/usr/bin/env bash") and b"\r\n" not in b
