"""Engine CLI entry point: one JSON envelope on stdout, logs on stderr, exit codes 0/2/3/4."""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from obd_engine import __version__
from obd_engine.cli import commands
from obd_engine.cli.envelope import EXIT_DATA, EXIT_OK, EXIT_USAGE, Envelope, ObdError, error_envelope


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise ObdError("USAGE", message, "Run with --help for usage.", exit_code=EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="obd", description="Automotive Intelligence engine")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="command", parser_class=_Parser)
    for name, (setup, _handler) in commands.COMMANDS.items():
        sp = sub.add_parser(name, help=setup.__doc__)
        sp.add_argument("--json", action="store_true", help="(default) emit JSON envelope")
        setup(sp)
    return p


def execute(argv: list[str]) -> tuple[Envelope, int]:
    """Run one command and return (envelope, exit code). Never prints and never raises: shared by the CLI and the MCP server."""
    try:
        parser = build_parser()
        ns = parser.parse_args(argv)
        if ns.version:
            return Envelope(summary=f"engine {__version__}", data={"engine": __version__}), EXIT_OK
        if not ns.command:
            raise ObdError("USAGE", "No command given.", f"Commands: {', '.join(commands.COMMANDS)}", exit_code=EXIT_USAGE)
        env = commands.COMMANDS[ns.command][1](ns)
        return env, EXIT_DATA if env.status == "error" else EXIT_OK
    except ObdError as e:
        return error_envelope(e), e.exit_code
    except Exception as e:  # never leak a raw stack trace to Claude
        print(traceback.format_exc(), file=sys.stderr)
        return error_envelope(ObdError("INTERNAL_ERROR", f"Unexpected {type(e).__name__}", "See stderr / logs; please report.", exit_code=EXIT_DATA)), EXIT_DATA


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"]:  # plain-text compatibility with earlier versions
        print(json.dumps({"engine": __version__}))
        return EXIT_OK
    env, code = execute(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.write(env.render() + chr(10))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
