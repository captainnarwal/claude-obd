"""Derive MCP tool definitions from the CLI's argparse registry, so there is exactly one place where a command is defined."""

from __future__ import annotations

import argparse
from typing import Any

from obd_engine.cli.envelope import ObdError
from obd_engine.cli.main import build_parser

READ_ONLY = {"scan", "inspect", "list", "overview", "query", "stats", "correlate", "anomalies", "lookup", "compare", "selftest"}
DESTRUCTIVE = {"delete", "clean"}
SKIP_DESTS = {"help", "json", "version", "command"}


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    parser = build_parser()
    for a in parser._actions:  # noqa: SLF001 - argparse has no public accessor for the subcommand table
        if isinstance(a, argparse._SubParsersAction):  # noqa: SLF001
            return dict(a.choices)
    return {}


def _actions(sp: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in sp._actions if a.dest not in SKIP_DESTS and not isinstance(a, argparse._HelpAction)]  # noqa: SLF001


def _prop(a: argparse.Action) -> dict[str, Any]:
    scalar = {int: "integer", float: "number"}.get(a.type, "string")  # type: ignore[arg-type]
    if isinstance(a, (argparse._StoreTrueAction, argparse._StoreFalseAction)):  # noqa: SLF001
        p: dict[str, Any] = {"type": "boolean"}
    elif isinstance(a, argparse._AppendAction) or a.nargs in ("+", "*"):  # noqa: SLF001
        p = {"type": "array", "items": {"type": scalar}}
    else:
        p = {"type": scalar}
        if a.choices:
            p["enum"] = list(a.choices)
    if a.help:
        p["description"] = a.help
    return p


def tool_defs() -> list[dict[str, Any]]:
    """[{name, description, input_schema, read_only, destructive}] - one per engine command, named obd_<command>."""
    out = []
    for name, sp in _subparsers().items():
        props, required = {}, []
        for a in _actions(sp):
            props[a.dest] = _prop(a)
            if not a.option_strings and a.nargs not in ("?", "*"):
                required.append(a.dest)
        out.append({
            "name": f"obd_{name}", "command": name, "description": (sp.description or sp.format_usage()).strip(),
            "input_schema": {"type": "object", "properties": props, **({"required": required} if required else {}), "additionalProperties": False},
            "read_only": name in READ_ONLY, "destructive": name in DESTRUCTIVE,
        })
    return out


def build_argv(command: str, arguments: dict[str, Any] | None) -> list[str]:
    """Turn tool arguments into the argv the CLI would receive. Unknown keys are rejected, never passed through."""
    sp = _subparsers().get(command)
    if sp is None:
        raise ObdError("UNKNOWN_TOOL", f"No such tool: obd_{command}", exit_code=2)
    args = dict(arguments or {})
    acts = {a.dest: a for a in _actions(sp)}
    unknown = sorted(set(args) - set(acts))
    if unknown:
        raise ObdError("USAGE", f"Unknown argument(s): {', '.join(unknown)}", f"Valid: {', '.join(acts)}", exit_code=2)
    argv = [command]
    gap = False
    for a in (x for x in acts.values() if not x.option_strings):
        v = args.pop(a.dest, None)
        if v is None:
            gap = gap or True
            continue
        if gap:
            raise ObdError("USAGE", f"Give '{a.dest}' only together with the arguments before it.", exit_code=2)
        argv += [str(x) for x in v] if isinstance(v, list) else [str(v)]
    for dest, v in args.items():
        a = acts[dest]
        flag = a.option_strings[0]
        if isinstance(a, (argparse._StoreTrueAction, argparse._StoreFalseAction)):  # noqa: SLF001
            if v:
                argv.append(flag)
        elif isinstance(a, argparse._AppendAction):  # noqa: SLF001
            for x in (v if isinstance(v, list) else [v]):
                argv += [flag, str(x)]
        elif isinstance(v, list):
            argv += [flag, *map(str, v)]
        elif v is not None:
            argv += [flag, str(v)]
    return [*argv, "--json"]
