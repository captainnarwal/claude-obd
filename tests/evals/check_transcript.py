"""Score a `claude -p --output-format stream-json` transcript against a case from cases.yaml.

    python tests/evals/check_transcript.py first-look transcript.jsonl [--folder path/to/run/folder]

Prints one line per failed check and exits 1 if any failed. Importable: `score(case, events, folder)`.
"""

from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

CASES = Path(__file__).with_name("cases.yaml")
TELEMETRY = (".csv", ".tsv", ".txt", ".xlsx", ".xls", ".log")
ENGINE_CALL = re.compile(r"scripts/obd[\"']?\s+([a-z]+)")


def load_cases() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}


def parse(lines: list[str]) -> list[dict[str, Any]]:
    out = []
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def _blocks(events: list[dict[str, Any]]):
    for e in events:
        msg = e.get("message") or {}
        if e.get("type") == "assistant" and isinstance(msg.get("content"), list):
            yield from msg["content"]


def engine_commands(events: list[dict[str, Any]]) -> list[str]:
    cmds = []
    for b in _blocks(events):
        if b.get("type") == "tool_use" and b.get("name") == "Bash":
            cmds += ENGINE_CALL.findall(b.get("input", {}).get("command", ""))
    return cmds


def read_telemetry(events: list[dict[str, Any]]) -> list[str]:
    bad = []
    for b in _blocks(events):
        if b.get("type") != "tool_use":
            continue
        inp = b.get("input", {})
        if b.get("name") == "Read" and str(inp.get("file_path", "")).lower().endswith(TELEMETRY):
            bad.append(f"Read {inp['file_path']}")
        if b.get("name") == "Bash" and re.search(r"\b(cat|head|tail|python3?|pandas)\b.*\.(csv|tsv|xlsx)", inp.get("command", "")) and "scripts/obd" not in inp.get("command", ""):
            bad.append("Bash " + inp["command"][:80])
    return bad


def answer_text(events: list[dict[str, Any]]) -> str:
    texts = [b.get("text", "") for b in _blocks(events) if b.get("type") == "text"]
    final = [e.get("result") for e in events if e.get("type") == "result" and isinstance(e.get("result"), str)]
    return "\n".join(texts + final)


def score(case: dict[str, Any], events: list[dict[str, Any]], folder: Path | None = None) -> list[str]:
    fails: list[str] = []
    expect, forbid = case.get("expect", {}), case.get("forbid", {})
    used = engine_commands(events)
    for c in expect.get("engine_commands", []):
        if c not in used:
            fails.append(f"engine command '{c}' was not used (used: {used})")
    ans = answer_text(events)
    anyof = expect.get("answer_mentions_any")
    if anyof and not any(a.lower() in ans.lower() for a in anyof):
        fails.append(f"answer mentions none of {anyof}")
    for pat in expect.get("files_created_glob", []):
        if folder is None or not glob.glob(str(folder / pat)):
            fails.append(f"no file matching {pat}")
    if forbid.get("read_telemetry_directly"):
        fails += [f"read a telemetry file directly: {x}" for x in read_telemetry(events)]
    for pat in forbid.get("answer_regex", []):
        if re.search(pat, ans, re.I | re.M):
            fails.append(f"answer matches forbidden pattern {pat!r}")
    return fails


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    case = load_cases()[argv[1]]
    folder = Path(argv[argv.index("--folder") + 1]) if "--folder" in argv else None
    events = parse(Path(argv[2]).read_text(encoding="utf-8").splitlines())
    fails = score(case, events, folder)
    for f in fails:
        print("FAIL:", f)
    print(f"{argv[1]}: {'PASS' if not fails else 'FAIL'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
