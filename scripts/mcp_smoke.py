#!/usr/bin/env python3
"""Smoke test: start the MCP server exactly as .mcp.json declares it and call a tool. Needs `mcp` importable (e.g. the engine venv)."""
import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
DATA = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or ROOT / ".obd-plugin-data")
CFG = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["obd"]


def sub(s: str) -> str:
    return s.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT)).replace("${CLAUDE_PLUGIN_DATA}", str(DATA))


async def main() -> None:
    params = StdioServerParameters(command=CFG["command"], args=[sub(a) for a in CFG["args"]],
                                   env={**os.environ, **{k: sub(v) for k, v in CFG["env"].items()}})
    with anyio.fail_after(300):
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                n = len((await s.list_tools()).tools)
                res = await s.call_tool("obd_lookup", {"kind": "dtc", "id": "P0171"})
                assert n >= 20 and json.loads(res.content[0].text)["data"]["code"] == "P0171", (n, res)
                print(f"ok: {n} tools")


anyio.run(main)
