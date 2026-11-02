"""Repository hygiene checks (design doc 18): versions match, skills exist, plugin layout is marketplace-safe."""

import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _json(p):
    return json.loads((ROOT / p).read_text(encoding="utf-8"))


def test_versions_match():
    plugin = _json(".claude-plugin/plugin.json")["version"]
    market = _json(".claude-plugin/marketplace.json")
    assert market["plugins"][0]["version"] == plugin
    assert market["metadata"]["version"] == plugin
    py = tomllib.loads((ROOT / "engine/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert py == plugin
    from obd_engine import __version__

    assert __version__ == plugin


def test_plugin_name_not_reserved():
    name = _json(".claude-plugin/plugin.json")["name"]
    assert not name.startswith(("claude-", "anthropic-", "cc-plugin-"))
    assert _json(".claude-plugin/marketplace.json")["plugins"][0]["name"] == name


def test_no_top_level_bin_directory():
    assert not (ROOT / "bin").exists()          # the hosted marketplace rejects it; the launcher lives in scripts/
    assert (ROOT / "scripts" / "obd").exists()


def test_skills_have_frontmatter_and_router_targets_exist():
    for d in ("obd", "automotive-data-analysis", "vehicle-report-generation", "communicating-findings", "engine-diagnostics", "obd-parameter-interpretation", "performance-ecu-log-analysis"):
        text = (ROOT / "skills" / d / "SKILL.md").read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n", text, re.S)
        assert m, d
        assert f"name: {d}" in m.group(1) and "description:" in m.group(1)
    router = (ROOT / "skills/obd/SKILL.md").read_text(encoding="utf-8")
    for referenced in re.findall(r"`([a-z-]+)` skill", router):
        assert (ROOT / "skills" / referenced / "SKILL.md").exists(), referenced


def test_skills_do_not_use_allowed_tools_frontmatter():
    # Claude Code 2.1.316 fails to load plugin skills that carry allowed-tools (verified with `claude -p "/obd doctor"`);
    # the launcher is pre-approved through a user permission rule instead (README).
    for p in (ROOT / "skills").glob("*/SKILL.md"):
        assert "allowed-tools:" not in p.read_text(encoding="utf-8"), p


def test_commands_documented_in_cheatsheet():
    from obd_engine.cli.commands import COMMANDS

    sheet = (ROOT / "skills/automotive-data-analysis/references/commands.md").read_text(encoding="utf-8")
    for c in COMMANDS:
        if c != "clean":
            assert f"`{c}" in sheet, c


def test_launchers_are_executable_text():
    for p in ("scripts/obd", "scripts/setup/setup.sh", "scripts/setup/doctor.sh", "scripts/hooks/read-guard.sh"):
        b = (ROOT / p).read_bytes()
        assert b.startswith(b"#!/bin/sh") and b"\r\n" not in b, p     # CRLF would break sh on Linux/macOS


def test_launchers_have_exec_bit_in_git():
    import shutil
    import subprocess

    import pytest

    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    files = ["scripts/obd", "scripts/obd.py", "scripts/setup/setup.sh", "scripts/setup/doctor.sh",
             "scripts/hooks/read-guard.sh", "scripts/hooks/read_guard.py"]
    out = subprocess.run(["git", "ls-files", "-s", *files], cwd=ROOT, capture_output=True, text=True).stdout
    modes = {line.split("\t")[1]: line.split()[0] for line in out.splitlines()}
    assert modes and all(m == "100755" for m in modes.values()), modes     # 644 => "Permission denied" on Linux/macOS


def test_reference_data_ships_and_samples_exist():
    assert (ROOT / "reference/parameters.yaml").exists()
    assert (ROOT / "samples/sample-drive.csv").stat().st_size > 10_000
