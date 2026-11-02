"""M8: reference lookups (PID / parameter / DTC structure) and the deterministic diagnostic rule engine."""

import re

import pytest

from obd_engine.analysis.diagnostics.engine import evaluate, load_rules
from obd_engine.reference.loader import load_dictionary, reference_dir

BANNED = re.compile(r"\b(is|are|has|have) (faulty|broken|defective|failed|dead|bad)\b|\bfaulty\b|\bdefective\b|\bbroken\b", re.I)


# ---- reference data integrity -------------------------------------------------
def test_reference_ids_exist_in_dictionary():
    import yaml

    d = load_dictionary()
    pids = yaml.safe_load((reference_dir() / "obd_pids.yaml").read_text(encoding="utf-8"))
    for e in pids["mode_01"]:
        assert e["canonical_id"] in d.params, e["pid"]
    assert set(pids["parameters"]) <= set(d.params)
    dtc = yaml.safe_load((reference_dir() / "dtc_structure.yaml").read_text(encoding="utf-8"))
    for fam in dtc["families"]:
        assert set(fam["params"]) <= set(d.params), fam["topic"]
    # no official SAE description text is shipped
    assert "J2012" in (reference_dir() / "dtc_structure.yaml").read_text(encoding="utf-8")


def test_rules_are_well_formed_and_use_careful_language():
    version, rules = load_rules()
    assert 10 <= len(rules) <= 20 and version
    assert len({r["id"] for r in rules}) == len(rules)
    for r in rules:
        assert r["when"] and len(r["hypotheses"]) >= 2 and r["tests"], r["id"]
        assert all(h["weight"] > 0 for h in r["hypotheses"])
        text = " ".join([r["title"], *r["tests"], *[h["cause"] for h in r["hypotheses"]]])
        assert not BANNED.search(text), (r["id"], BANNED.search(text).group(0))
        assert r["category"] in {"fuel", "thermal", "ignition", "intake", "drivetrain"}


# ---- rule engine on hand-made facts ------------------------------------------
def _rule(rid):
    return next(r for r in load_rules()[1] if r["id"] == rid)


IDLE = "fuel_system.trims.ltft_b1.by_condition.idle.median"
WOT = "fuel_system.trims.ltft_b1.by_condition.wot.median"


def test_lean_idle_ranks_unmetered_air_first_and_load_evidence_changes_ranking():
    hit = evaluate(_rule("lean_at_idle"), {IDLE: 14.0, WOT: 2.0})
    assert hit and "Unmetered air" in hit["ranked"][0]["cause"] and hit["supports"]
    both = evaluate(_rule("lean_at_idle"), {IDLE: 14.0, WOT: 12.0})
    assert both and "Unmetered air" not in both["ranked"][0]["cause"]
    assert both["ranked"][0]["favoring"]  # delivery shortfall is favored by the high trim under load


def test_unknown_or_unmet_facts_do_not_fire():
    assert evaluate(_rule("lean_at_idle"), {}) is None
    assert evaluate(_rule("lean_at_idle"), {IDLE: 3.0}) is None
    assert evaluate(_rule("bank_imbalance"), {"fuel_system.bank_difference.long": -2.0}) is None
    assert evaluate(_rule("bank_imbalance"), {"fuel_system.bank_difference.long": -7.0}) is not None  # abs>=


# ---- end to end ----------------------------------------------------------------
def _diag(run, copy_log, key):
    p = copy_log(key)
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    code, env = run("analyze", ds)
    assert code == 0
    mods = {m["module"]: m for m in env["data"]["modules"]}
    return ds, mods["diagnostics"], env


def test_injected_faults_map_to_their_patterns(ws, run, copy_log):
    _, diag, _ = _diag(run, copy_log, "perf_faulty")
    titles = " | ".join(f["title"] for f in diag["findings"])
    for expect in ("high at idle", "differ noticeably between the two banks", "removed timing because of knock", "Boost stays below",
                   "Misfire counter increased", "lean at wide-open throttle"):
        assert expect in titles, (expect, titles)
    for f in diag["findings"]:
        assert f["kind"] == "hypothesis"
        assert f["confidence"] in ("low", "medium")  # never high: thresholds are generic
        assert f["threshold"]["tier"] == "generic" and "cap" in f["confidence_reason"]
        assert f["alternatives"] and f["next_steps"]


def test_clean_log_produces_no_hypotheses(ws, run, copy_log):
    _, diag, _ = _diag(run, copy_log, "perf_clean")
    assert diag["findings_total"] == 0


def test_requesting_diagnostics_alone_runs_the_modules_it_needs(ws, run, copy_log):
    p = copy_log("perf_faulty")
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    code, env = run("analyze", ds, "--modules", "diagnostics")
    assert code == 0
    ran = [m["module"] for m in env["data"]["modules"]]
    assert ran[-1] == "diagnostics" and "fuel_system" in ran


def test_report_contains_ranked_hypotheses_tests_and_missing_evidence(ws, run, copy_log):
    from pypdf import PdfReader

    p = copy_log("perf_faulty")
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    code, env = run("report", ds, "--no-charts")
    assert code == 0
    pdf = PdfReader(env["data"]["reports"][0]["path"])
    text = " ".join(" ".join(pg.extract_text().split()) for pg in pdf.pages)  # table cells wrap
    assert "Possible explanation" in text and "Relative support" in text
    assert "Physical tests that would discriminate" in text
    assert "not a diagnosis" in text
    assert BANNED.search(text) is None


# ---- lookup ----------------------------------------------------------------------
def test_lookup_dtc_decodes_structure_without_claiming_presence(ws, run):
    code, env = run("lookup", "dtc", "p0171")
    d = env["data"]
    assert code == 0 and d["code"] == "P0171" and d["system"].startswith("Powertrain") and "generic" in d["scope"]
    assert "fuel correction" in d["topic"] and "ltft_b1" in {p["id"] for p in d["related_parameters"]}
    assert any("does not say the code is stored" in c for c in env["caveats"])
    assert not (ws / ".obd").exists()  # pure reference lookup creates no workspace
    code, env = run("lookup", "dtc", "P0304")
    assert env["data"]["cylinder"] == 4 and "misfire" in env["data"]["topic"]
    code, env = run("lookup", "dtc", "P1234")
    assert "manufacturer" in env["data"]["scope"] and any("Manufacturer-specific" in c for c in env["caveats"])
    assert run("lookup", "dtc", "X1234")[0] == 2


def test_lookup_pid_and_parameter(ws, run):
    for ref in ("0C", "01 0C", "0x0c"):
        code, env = run("lookup", "pid", ref)
        assert code == 0 and env["data"]["formula"] == "(256 * A + B) / 4" and env["data"]["canonical_id"] == "engine_speed"
    assert run("lookup", "pid", "FF")[0] == 3
    code, env = run("lookup", "parameter", "ltft_b1")
    assert code == 0 and env["data"]["pid"] == "01 07" and "limits_used_by_this_tool" not in env["data"]
    code, env = run("lookup", "parameter", "coolant temp")
    assert env["data"]["id"] == "ect" and env["data"]["limits_used_by_this_tool"][0]["tier"] == "generic"
    code, env = run("lookup", "parameter", "knock_retard")
    assert "no standard Mode 01 PID" in env["data"]["pid"]
    assert run("lookup", "parameter", "flux capacitor")[0] == 3


def test_lookup_parameter_shows_vehicle_tier(ws, run):
    run("vehicle", "add", "--nickname", "t", "--aspiration", "turbo", "--fuel", "petrol")
    code, env = run("lookup", "parameter", "ect", "--vehicle", "t")
    assert env["data"]["limits_used_by_this_tool"][0]["tier"] == "engine-family"


@pytest.mark.parametrize("cmd", ["lookup", "vehicle", "session"])
def test_new_commands_registered(cmd):
    from obd_engine.cli.commands import COMMANDS

    assert cmd in COMMANDS
