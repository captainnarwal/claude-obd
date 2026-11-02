# ADR 0001: the MCP server ships but is not auto-declared by the plugin

**Status:** accepted (M10)

## Context
The design doc (section 14.5) says the plugin adds an MCP server declaration so other Claude surfaces can use the engine.
For Claude Code the skills already call the same command layer through the launcher, so an auto-declared MCP server adds
a second path to the same operations. A plugin MCP server that exits at start-up (for example before `/obd setup` has
created the environment) is shown as failed in `/mcp`, and Claude Code skips a plugin stdio server that failed its first
connection for 15 minutes in new sessions.

## Decision
- Ship the server (`obd-mcp`, or `scripts/obd mcp`) and document how to enable it, but do **not** put `mcpServers` in `plugin.json`.
- Claude Desktop users add it to `claude_desktop_config.json` (see the user guide). Claude Code users who want the tools can opt in with `claude mcp add`.
- The server fixes its workspace and readable folders at start-up (`--workspace`, `--allow-dir`, enforced through `OBD_ALLOWED_DIRS`), because MCP clients have no per-path permission prompt.

## Consequences
- The first-run experience in Claude Code (marketplace add, install, `/obd setup`) cannot be broken by a server that starts too early.
- Claude Code sessions are not given 20 extra tool definitions by default.
- Revisit once setup can run before the first connection, or if usage shows people want the tools in Claude Code.
