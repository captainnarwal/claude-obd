"""M6: vehicle / session profiles and layered thresholds."""

from obd_engine.analysis.thresholds import family_for, limits_for


def _tiers(env):
    return {f["threshold"]["tier"] for m in env["data"]["modules"] for f in m["findings"] if f.get("threshold")}


def test_limits_resolve_vehicle_then_family_then_generic():
    assert {lim["tier"] for lim in limits_for("ect")} == {"generic"}
    turbo = {"engine": {"aspiration": "turbo", "fuel": "petrol"}}
    assert {lim["tier"] for lim in limits_for("ect", turbo)} == {"engine-family"}
    assert min(lim["limit"] for lim in limits_for("ect", turbo)) == 108.0
    own = {**turbo, "limits": {"ect": [{"op": ">", "limit": 99.0, "unit": "°C", "severity": "warning"}]}}
    lims = limits_for("ect", own)
    assert [(lim["tier"], lim["limit"]) for lim in lims] == [("vehicle", 99.0)]
    # a parameter the family does not list falls through to generic
    assert {lim["tier"] for lim in limits_for("control_module_voltage", turbo)} == {"generic"}


def test_family_selection():
    assert family_for({"fuel": "Diesel"}) == "diesel"
    assert family_for({"aspiration": "supercharged", "fuel": "petrol"}) == "turbo_petrol"
    assert family_for({"aspiration": "na"}) == "na_petrol"
    assert family_for({}) is None and family_for(None) is None


def test_vehicle_lifecycle_and_vin_masking(ws, run):
    code, env = run("vehicle", "add", "--nickname", "daily", "--make", "Honda", "--vin", "1HGCM82633A004352", "--aspiration", "na", "--fuel", "petrol")
    assert code == 0
    v = env["data"]["vehicle"]
    assert v["vin"].endswith("004352") and v["vin"].startswith("*") and "1HGCM" not in v["vin"]
    assert v["threshold_family"] == "na_petrol"
    code, env = run("vehicle", "set", "daily", "--displacement", "1.8", "--limit", "ect:>:101:°C:warning")
    assert code == 0 and env["data"]["vehicle"]["engine"]["displacement"] == 1.8
    assert env["data"]["vehicle"]["engine"]["aspiration"] == "na"  # merged, not replaced
    assert env["data"]["vehicle"]["limits"]["ect"][0]["limit"] == 101.0
    assert run("vehicle", "list")[1]["data"]["vehicles"][0]["nickname"] == "daily"
    assert run("vehicle", "delete", "daily")[0] == 2  # needs --yes
    assert run("vehicle", "delete", "daily", "--yes")[0] == 0
    assert run("vehicle", "show", "daily")[0] == 3


def test_vehicle_input_validation(ws, run):
    assert run("vehicle", "add")[0] == 2
    assert run("vehicle", "add", "--nickname", "x", "--aspiration", "warp")[0] == 2
    assert run("vehicle", "add", "--nickname", "x", "--limit", "ect:~:100")[0] == 2


def test_assigned_vehicle_changes_thresholds_and_cache(ws, run, copy_log):
    p = copy_log("faulty")
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    base = run("analyze", ds, "--modules", "time_series")[1]
    assert _tiers(base) == {"generic"}  # the faulty log overheats, so a limit finding exists
    run("vehicle", "add", "--nickname", "t", "--aspiration", "turbo", "--fuel", "petrol")
    assert run("vehicle", "assign", "t", ds)[0] == 0
    fam = run("analyze", ds, "--modules", "time_series")[1]
    assert _tiers(fam) == {"engine-family"}
    run("vehicle", "set", "t", "--limit", "ect:>:100:°C:warning")
    own = run("analyze", ds, "--modules", "time_series")[1]
    assert _tiers(own) == {"vehicle"}


def test_session_groups_dataset_and_inherits_vehicle(ws, run, copy_log):
    p = copy_log("clean")
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    run("vehicle", "add", "--nickname", "car")
    code, env = run("session", "add", "--label", "pull-1", "--vehicle", "car", "--condition", "ambient=30C")
    assert code == 0 and env["data"]["session"]["conditions"] == {"ambient": "30C"}
    code, env = run("session", "assign", "pull-1", ds)
    assert code == 0 and env["data"]["vehicle_id"]
    assert run("session", "assign", "nope", ds)[0] == 3


def test_report_shows_vehicle_and_tier(ws, run, copy_log):
    from obd_engine.reports.model import build_model
    from obd_engine.storage.catalog import Catalog
    from obd_engine.storage.workspace import Workspace

    p = copy_log("clean")
    ds = run("ingest", p.name)[1]["data"]["dataset_id"]
    run("vehicle", "add", "--nickname", "t", "--make", "Mazda", "--model", "3", "--year", "2019", "--aspiration", "turbo", "--fuel", "petrol", "--displacement", "2.5")
    run("vehicle", "assign", "t", ds)
    run("analyze", ds)
    cat = Catalog(Workspace.current())
    try:
        m = build_model(cat, cat.get_dataset(ds), None, [])
    finally:
        cat.close()
    assert m["vehicle"]["make"] == "Mazda" and "2.5 L" in m["vehicle"]["engine"] and "turbo_petrol" in m["vehicle"]["threshold_tier"]
    code, env = run("report", ds)
    assert code == 0
