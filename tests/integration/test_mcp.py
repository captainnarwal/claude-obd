"""M10: the MCP server is a thin layer over the command registry."""

import json
import shutil
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from obd_engine.cli.commands import COMMANDS
from obd_engine.cli.envelope import ObdError
from obd_engine.mcp.schema import build_argv, tool_defs
from obd_engine.mcp.server import list_tools, run_tool


def _env(result):
    return json.loads(result.content[0].text)


def test_every_command_becomes_a_tool_with_a_valid_schema():
    defs = {d["name"]: d for d in tool_defs()}
    assert set(defs) == {f"obd_{c}" for c in COMMANDS}
    for d in defs.values():
        s = d["input_schema"]
        assert s["type"] == "object" and s["additionalProperties"] is False
        assert "json" not in s["properties"] and "help" not in s["properties"]
    assert defs["obd_ingest"]["input_schema"]["required"] == ["path"]
    assert defs["obd_vehicle"]["input_schema"]["properties"]["action"]["enum"][0] == "add"
    assert defs["obd_ingest"]["input_schema"]["properties"]["set"]["type"] == "array"
    assert defs["obd_report"]["input_schema"]["properties"]["format"]["enum"] == ["pdf", "xlsx", "both"]
    assert defs["obd_query"]["input_schema"]["properties"]["t0"]["type"] == "number"
    assert defs["obd_scan"]["read_only"] and defs["obd_delete"]["destructive"] and not defs["obd_ingest"]["read_only"]
    tools = {t.name: t for t in list_tools()}
    assert tools["obd_scan"].annotations.read_only_hint is True


def test_build_argv_maps_arguments_and_rejects_the_unknown():
    assert build_argv("lookup", {"kind": "dtc", "id": "P0171"}) == ["lookup", "dtc", "P0171", "--json"]
    argv = build_argv("ingest", {"path": "a b.csv", "set": ["X=ect", "Y=map"], "force": True, "label": "run"})
    assert argv[:2] == ["ingest", "a b.csv"] and argv.count("--set") == 2 and "--force" in argv and argv[-1] == "--json"
    assert "--force" not in build_argv("ingest", {"path": "a.csv", "force": False})
    assert "--tire-size" in build_argv("vehicle", {"action": "add", "nickname": "x", "tire_size": "205/55R16"})
    for bad in ({"path": "a.csv", "evil": "1"}, {"path": "a.csv", "json": True}):
        with pytest.raises(ObdError) as e:
            build_argv("ingest", bad)
        assert e.value.code == "USAGE"
    with pytest.raises(ObdError):
        build_argv("session", {"action": "assign", "dataset": "x"})  # 'target' skipped but 'dataset' given
    with pytest.raises(ObdError):
        build_argv("nope", {})


def test_run_tool_returns_the_cli_envelope_and_flags_errors(ws):
    r = run_tool("obd_lookup", {"kind": "dtc", "id": "P0171"})
    assert not r.is_error and _env(r)["data"]["code"] == "P0171"
    r = run_tool("obd_lookup", {"kind": "dtc", "id": "ZZZ"})
    assert r.is_error and _env(r)["error"]["code"] == "INVALID_DTC"
    r = run_tool("obd_ingest", {"path": "missing.csv"})
    assert r.is_error and _env(r)["error"]["code"] == "FILE_NOT_FOUND"
    for name, args in (("other_tool", {}), ("obd_ingest", {"bogus": 1})):
        r = run_tool(name, args)
        assert r.is_error and "error" in _env(r)


def test_readable_folders_are_confined_when_configured(ws, tmp_path_factory, monkeypatch):
    outside = tmp_path_factory.mktemp("elsewhere") / "x.csv"
    outside.write_text("Time (s),Engine RPM\n" + "\n".join(f"{i},{800 + i}" for i in range(50)))
    inside = ws / "ok.csv"
    shutil.copy(outside, inside)
    monkeypatch.setenv("OBD_ALLOWED_DIRS", str(ws))
    r = run_tool("obd_ingest", {"path": str(outside)})
    assert r.is_error and _env(r)["error"]["code"] == "PATH_NOT_ALLOWED"
    assert run_tool("obd_scan", {"folder": str(outside.parent)}).is_error
    assert not run_tool("obd_ingest", {"path": "ok.csv"}).is_error
    monkeypatch.setenv("OBD_ALLOWED_DIRS", str(ws) + __import__("os").pathsep + str(outside.parent))
    assert not run_tool("obd_inspect", {"path": str(outside)}).is_error


def test_stdio_round_trip_with_a_real_client(tmp_path, logs, monkeypatch):
    shutil.copy(logs["perf_faulty"], tmp_path / "pf.csv")
    outside = tmp_path.parent / "outside_pf.csv"
    shutil.copy(logs["perf_clean"], outside)

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "obd_engine.mcp", "--workspace", str(tmp_path)])
        with anyio.fail_after(120):
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert {"obd_ingest", "obd_analyze", "obd_lookup", "obd_compare"} <= {t.name for t in tools}
                    ing = await session.call_tool("obd_ingest", {"path": "pf.csv"})
                    ds = _env(ing)["data"]["dataset_id"]
                    ana = _env(await session.call_tool("obd_analyze", {"dataset": ds}))
                    assert ana["status"] == "ok" and any(m["module"] == "diagnostics" for m in ana["data"]["modules"])
                    bad = await session.call_tool("obd_ingest", {"path": str(outside)})
                    assert bad.is_error and _env(bad)["error"]["code"] == "PATH_NOT_ALLOWED"
                    return ana

    ana = anyio.run(go)
    # the MCP path gives the same modules as the CLI path
    from obd_engine.cli.main import execute

    monkeypatch.setenv("OBD_WORKSPACE", str(tmp_path))
    env, _ = execute(["analyze", ana["evidence"][0]["dataset_id"], "--json"])
    assert [m["module"] for m in env.data["modules"]] == [m["module"] for m in ana["data"]["modules"]]


def test_stdio_through_the_cross_platform_launcher(tmp_path, monkeypatch):
    """The launcher other MCP clients use (python scripts/obd.py mcp), from a clean env (no bash). The plugin itself uses .mcp.json (uv run)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("OBD_PYTHON", sys.executable)  # the test interpreter has the engine installed

    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(root / "scripts" / "obd.py"), "mcp", "--workspace", str(tmp_path)],
                                       env={**__import__("os").environ})
        with anyio.fail_after(120):
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    names = {t.name for t in (await session.list_tools()).tools}
                    assert {"obd_ingest", "obd_analyze", "obd_lookup"} <= names
                    res = await session.call_tool("obd_lookup", {"kind": "dtc", "id": "P0171"})
                    assert _env(res)["data"]["code"] == "P0171"

    anyio.run(go)


def test_launcher_reports_setup_required_on_stderr_in_mcp_mode(tmp_path):
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in __import__("os").environ.items() if k != "OBD_PYTHON"}
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "empty")
    r = subprocess.run([sys.executable, str(root / "scripts" / "obd.py"), "mcp"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 4 and r.stdout == "" and "not set up" in r.stderr   # stdout is the protocol channel: stay silent
