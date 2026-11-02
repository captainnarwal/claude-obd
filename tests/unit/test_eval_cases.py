"""The eval cases are well-formed and the transcript scorer works (the evals themselves need a live Claude session)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import check_transcript as ct  # noqa: E402


def _tool(name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _text(t):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}


def test_cases_are_well_formed():
    cases = ct.load_cases()
    assert len(cases) >= 6
    for cid, c in cases.items():
        assert c["prompt"] and (c.get("expect") or c.get("forbid")), cid
        for k in c.get("expect", {}):
            assert k in {"engine_commands", "answer_mentions_any", "files_created_glob"}, (cid, k)
        for k in c.get("forbid", {}):
            assert k in {"read_telemetry_directly", "answer_regex"}, (cid, k)
        for cmd in c.get("expect", {}).get("engine_commands", []):
            from obd_engine.cli.commands import COMMANDS

            assert cmd in COMMANDS, (cid, cmd)


def test_scorer_passes_a_good_run_and_flags_bad_ones():
    case = ct.load_cases()["first-look"]
    good = [_tool("Bash", command='"${CLAUDE_PLUGIN_ROOT}/scripts/obd" inspect perf_faulty.csv --json'),
            _tool("Bash", command='"${CLAUDE_PLUGIN_ROOT}/scripts/obd" ingest perf_faulty.csv --json'),
            _tool("Bash", command='"${CLAUDE_PLUGIN_ROOT}/scripts/obd" analyze ds_1 --json'),
            _text("Knock events under load; one possible explanation is fuel quality.")]
    assert ct.score(case, good) == []
    bad = [_tool("Read", file_path="/x/perf_faulty.csv"), _text("The turbo is faulty.")]
    fails = ct.score(case, bad)
    assert any("was not used" in f for f in fails)
    assert any("read a telemetry file directly" in f for f in fails)
    assert any("forbidden pattern" in f for f in fails)  # "is faulty" is banned wording


def test_parse_ignores_noise():
    assert ct.parse(["", "not json", '{"type": "result", "result": "ok"}']) == [{"type": "result", "result": "ok"}]
