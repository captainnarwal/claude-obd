# ADR 0001: the MCP server is bundled with the plugin via `uv run`

**Status:** accepted (M10); revised in 0.2.x - supersedes the original "opt-in only" decision

## Context
The original decision kept the server opt-in because a plugin stdio server that exits at start-up (before `/obd setup` has
created the environment) is shown as failed in `/mcp`, and Claude Code skips a failed plugin stdio server for 15 minutes.
It also started through a shell wrapper, which an MCP command cannot rely on (no shell, no per-OS override).

## Decision
- The plugin ships `.mcp.json` declaring server `obd` with command `uv run --frozen --no-dev --no-editable --project ${CLAUDE_PLUGIN_ROOT}/engine obd-mcp`.
  Claude Code spawns MCP commands without a shell and has no per-OS override, so the only portable choice is a real executable present on
  Linux, macOS and Windows; `uv` is the plugin's installer and is that executable.
- `UV_PROJECT_ENVIRONMENT=${CLAUDE_PLUGIN_DATA}/venv` makes the server reuse the environment `/obd setup` builds, and `uv run` creates it
  from the lock file if it is missing, so the server no longer depends on `/obd setup` having run first (the original failure mode).
- Claude Desktop and other clients use the same command (see the user guide).
- The server still fixes its workspace and readable folders at start-up (`--workspace`, `--allow-dir`, `OBD_ALLOWED_DIRS`).

## Consequences
- Requires `uv` on PATH (`/obd setup` offers to install it; run `/mcp` and reconnect `obd` afterwards). The first start needs internet once.
- With no `--workspace` the server uses the folder Claude Code was started in (see `obd_engine/mcp/server.py` `configure`); reads stay limited to that folder.
- Claude Code sessions get 20 extra tool definitions; users can disable the server with `/mcp`.
