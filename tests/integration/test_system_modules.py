"""M7: fuel / thermal / ignition / performance / intake-boost / drivetrain modules on logs with injected faults."""

import numpy as np
import pytest

from obd_engine.analysis.modules.drivetrain import ratio_clusters

SYSTEM = "fuel_system,thermal,ignition,engine_performance,intake_boost,drivetrain"


def _analyze(run, copy_log, key, modules=SYSTEM, *extra):
    p = copy_log(key)
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    for e in extra:
        run(*e.replace("{ds}", ds).split())
    code, env = run("analyze", ds, "--modules", modules)
    assert code == 0, env
    return ds, {m["module"]: m for m in env["data"]["modules"]}, env


def _titles(mods, module):
    return [f["title"] for f in mods[module]["findings"]]


def _has(mods, module, text):
    return any(text in t for t in _titles(mods, module))


def test_clean_log_has_no_findings_needing_attention(ws, run, copy_log):
    _, mods, env = _analyze(run, copy_log, "perf_clean")
    assert set(mods) == set(SYSTEM.split(","))
    bad = [(m, f["title"]) for m, v in mods.items() for f in v["findings"] if f["severity"] != "info"]
    assert not bad, bad


def test_injected_faults_are_found_with_evidence(ws, run, copy_log):
    _, mods, _ = _analyze(run, copy_log, "perf_faulty")
    assert _has(mods, "fuel_system", "lean at wide-open throttle")
    assert _has(mods, "fuel_system", "sustained lean episode")
    assert _has(mods, "fuel_system", "differ between banks")
    assert _has(mods, "fuel_system", "depends on operating condition")
    assert _has(mods, "ignition", "knock-retard event")
    assert _has(mods, "ignition", "Misfire counter rose")
    assert _has(mods, "intake_boost", "Boost shortfall")
    assert _has(mods, "thermal", "hotter at idle than at cruise")
    for m in ("fuel_system", "ignition", "intake_boost"):
        for f in mods[m]["findings"]:
            assert f["evidence"], f["title"]
            assert f["kind"] in ("observation", "inference")
    # generic-threshold findings are capped at medium confidence and say so
    lean = next(f for f in mods["fuel_system"]["findings"] if "lean at wide-open" in f["title"])
    assert lean["severity"] == "warning" and lean["threshold"]["tier"] == "generic"


def test_slow_warmup_and_overheat(ws, run, copy_log):
    _, mods, _ = _analyze(run, copy_log, "perf_slow", "thermal")
    assert _has(mods, "thermal", "Slow warm-up")
    assert _has(mods, "thermal", "above its steady level")


def test_vehicle_mass_enables_wheel_power_lower_bound(ws, run, copy_log):
    p = copy_log("perf_clean")
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    _, a = run("analyze", ds, "--modules", "engine_performance")
    assert not any("Wheel power" in f["title"] for m in a["data"]["modules"] for f in m["findings"])
    run("vehicle", "add", "--nickname", "car", "--mass-kg", "1400")
    run("vehicle", "assign", "car", ds)
    _, a = run("analyze", ds, "--modules", "engine_performance")
    f = next(f for m in a["data"]["modules"] for f in m["findings"] if "Wheel power" in f["title"])
    assert f["kind"] == "inference" and f["confidence"] == "low" and "lower bound" in f["confidence_reason"]


def test_modules_skip_cleanly_when_parameters_are_missing(ws, run, copy_log):
    # the torque-style log has no lambda / knock / boost channels
    p = copy_log("generic")
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    code, env = run("analyze", ds)
    assert code == 0
    skipped = {s["module"] for s in env["data"]["skipped"]}
    assert "ignition" not in skipped  # timing advance alone is enough to run
    ran = {m["module"] for m in env["data"]["modules"]}
    assert {"fuel_system", "thermal", "intake_boost"} <= ran


def test_all_modules_run_on_every_synthetic_style(ws, run, copy_log):
    for key in ("clean", "faulty", "generic_lean"):
        p = copy_log(key)
        ds = run("ingest", p.name)[1]["data"]["dataset_id"]
        code, env = run("analyze", ds)
        assert code == 0, (key, env)


def test_results_are_cached_and_reproducible(ws, run, copy_log):
    ds, mods, _ = _analyze(run, copy_log, "perf_faulty", "fuel_system,ignition,intake_boost")
    _, env2 = run("analyze", ds, "--modules", "fuel_system,ignition,intake_boost")
    assert any("from cache" in c for c in env2["caveats"])
    again = {m["module"]: m for m in env2["data"]["modules"]}
    assert [f["title"] for f in again["fuel_system"]["findings"]] == _titles(mods, "fuel_system")


def test_ratio_clusters_find_distinct_gears():
    rng = np.random.default_rng(0)
    ratio = np.concatenate([rng.normal(m, m * 0.01, 400) for m in (60.0, 38.0, 27.0)])
    cl = ratio_clusters(ratio)
    assert len(cl) == 3
    assert [round(c["rpm_per_kmh"]) for c in cl] == [60, 38, 27]
    assert ratio_clusters(np.array([1.0, 2.0])) == []


@pytest.mark.parametrize("module", SYSTEM.split(","))
def test_modules_listed(ws, run, module):
    _, env = run("analyze", "--list-modules")
    assert module in {m["id"] for m in env["data"]["modules"]}
