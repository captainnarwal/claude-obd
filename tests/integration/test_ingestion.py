import polars as pl
import pytest


def _ds(env):
    return env["data"]["dataset_id"]


def test_torque_ingest(run, copy_log):
    copy_log("clean", "drive.csv")
    code, env = run("ingest", "drive.csv")
    assert code == 0 and env["status"] == "ok", env
    assert env["data"]["adapter"] == "torque_pro"
    names = {p["name"] for p in env["data"]["parameters"]}
    assert {"engine_speed", "ect", "stft_b1", "ltft_b1", "map"} <= names
    assert env["data"]["segments"] == 1


def test_imperial_units_converted_to_metric(run, copy_log, ws):
    copy_log("generic", "g.csv")
    _, env = run("ingest", "g.csv")
    ds = _ds(env)
    df = pl.read_parquet(ws / ".obd" / "datasets" / ds / "normalized.parquet")
    ect = df["ect"].drop_nulls()
    assert 15 < ect.median() < 100   # source column is in F (68..200); canonical is C
    spd = df["vehicle_speed"].max()
    assert 100 < spd < 120           # source mph (~69) -> km/h (~111)


def test_reset_creates_segments_and_monotonic_time(run, copy_log, ws):
    copy_log("generic", "g.csv")
    _, env = run("ingest", "g.csv")
    assert env["data"]["segments"] >= 2
    assert any("reset" in c for c in env["caveats"])
    df = pl.read_parquet(ws / ".obd" / "datasets" / _ds(env) / "normalized.parquet")
    assert df["t"].is_sorted()


def test_sentinels_flagged_not_deleted(run, copy_log, ws):
    copy_log("generic", "g.csv")
    _, env = run("ingest", "g.csv")
    ect = next(p for p in env["data"]["parameters"] if p["name"] == "ect")
    assert ect["sentinel"] > 0
    df = pl.read_parquet(ws / ".obd" / "datasets" / _ds(env) / "normalized.parquet")
    assert "ect__q" in df.columns
    assert (df["ect__q"] > 0).sum() == ect["sentinel"] + ect["oor"] - 0 or (df["ect__q"] > 0).sum() >= ect["sentinel"]


def test_idempotent_by_hash(run, copy_log):
    copy_log("clean", "drive.csv")
    _, a = run("ingest", "drive.csv")
    _, b = run("ingest", "drive.csv")
    assert _ds(a) == _ds(b)
    assert "Already ingested" in b["summary"]
    _, lst = run("list")
    assert len(lst["data"]["datasets"]) == 1


def test_ambiguous_column_flow(run, ws):
    (ws / "amb.csv").write_text("Time,TEMP,RPM\n" + "\n".join(f"{i},{90+i%3},{800+i}" for i in range(60)), encoding="utf-8")
    _, ins = run("inspect", "amb.csv")
    assert ins["status"] == "needs_input" and ins["needs_input"][0]["column"] == "TEMP"
    code, env = run("ingest", "amb.csv")
    assert env["status"] == "needs_input"
    assert "TEMP" in env["data"]["pending_confirmation"]
    names = {p["name"]: p for p in env["data"]["parameters"]}
    assert not names["TEMP"]["mapped"]                       # not silently assumed
    code, env2 = run("map", "amb.csv", "--set", "TEMP=ect:C")
    assert env2["status"] == "ok" and env2["data"]["mapping_version"] == 2
    assert any(p["name"] == "ect" and p["mapped"] for p in env2["data"]["parameters"])


def test_map_unknown_column_and_parameter(run, copy_log):
    copy_log("clean", "drive.csv")
    run("ingest", "drive.csv")
    code, env = run("map", "drive.csv", "--set", "Nope=ect")
    assert env["error"]["code"] == "COLUMN_NOT_FOUND"
    code, env = run("map", "drive.csv", "--set", "Bearing=not_a_param")
    assert env["error"]["code"] == "UNKNOWN_PARAMETER"


def test_semicolon_decimal_comma_and_preamble(run, ws):
    rows = ["# Logger v2", "# device: test", "", "Zeit;Drehzahl;Speed (km/h)"]
    rows += [f"{i/2:.1f}".replace(".", ",") + f";{800+i*3},5;{i*0.5:.1f}".replace(".", ",", 1) for i in range(80)]
    (ws / "de.csv").write_text("\n".join(rows), encoding="utf-8")
    code, env = run("inspect", "de.csv")
    assert code == 0, env
    assert env["data"]["delimiter"] == ";"
    assert any("preamble" in n for n in env["data"]["notes"])


def test_utf16_file(run, ws):
    text = "Time (s),Engine RPM,Speed (km/h)\n" + "\n".join(f"{i*0.5},{900+i},{i}" for i in range(60))
    (ws / "u16.csv").write_bytes(text.encode("utf-16"))
    code, env = run("ingest", "u16.csv")
    assert code == 0 and env["data"]["rows"] == 60


def test_xlsx_ingest(run, ws):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws0 = wb.active
    ws0.title = "Log"
    ws0.append(["Time (s)", "Engine RPM", "Coolant Temp (°C)"])
    for i in range(80):
        ws0.append([i * 0.5, 800 + i, 70 + i * 0.1])
    wb.create_sheet("Notes").append(["hello"])
    wb.save(ws / "log.xlsx")
    code, env = run("ingest", "log.xlsx")
    assert code == 0, env
    names = {p["name"] for p in env["data"]["parameters"]}
    assert {"engine_speed", "ect"} <= names


def test_no_time_column_uses_index_with_caveat(run, ws):
    (ws / "nt.csv").write_text("Engine RPM,Speed (km/h)\n" + "\n".join(f"{800+i},{i}" for i in range(50)), encoding="utf-8")
    code, env = run("ingest", "nt.csv")
    assert env["data"]["time_axis"] == "index"
    assert any("time column" in c for c in env["caveats"])


def test_delete_requires_confirmation(run, copy_log, ws):
    copy_log("clean", "drive.csv")
    _, env = run("ingest", "drive.csv")
    code, e = run("delete", "drive.csv")
    assert e["error"]["code"] == "CONFIRMATION_REQUIRED" and code == 2
    code, e = run("delete", "drive.csv", "--yes")
    assert code == 0
    assert (ws / "drive.csv").exists()                        # original untouched
    assert run("list")[1]["data"]["datasets"] == []
    code, e = run("clean", "--yes")
    assert not (ws / ".obd").exists()


def test_time_of_day_gps_only_log(run, ws):
    """Regression: a date-less time column (17:44:24.917) from a GPS-only app, with trailing comma and typo'd header."""
    rows = ["time,Altitude (GPS) (m),Average speed (GPS) (km/h),Speed (GPS) (km/h),Latitude,Longtitude,"]
    for i in range(30):
        rows.append(f"17:44:{24 + i // 2:02d}.{(i % 2) * 500:03d},{-75.4 + i * 0.01},{i % 5},{i % 7},12.85{i},80.23{i},")
    (ws / "gps.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    code, env = run("ingest", "gps.csv")
    assert code == 0, env
    assert env["data"]["time_axis"] == "time_of_day" and env["data"]["segments"] == 1
    names = {p["name"] for p in env["data"]["parameters"]}
    assert {"gps_speed", "gps_speed_avg", "gps_latitude", "gps_longitude", "gps_altitude"} <= names
    assert env["status"] == "ok"                                 # the "Longtitude" typo and "Altitude (GPS)" map confidently


def test_unparseable_time_format_candidates_do_not_crash():
    import polars as pl

    from obd_engine.ingestion.normalize.time_axis import detect_time

    df = pl.DataFrame({"__row_id": [1, 2, 3], "time": ["00:01.5", "00:02.5", "00:03.5"]})
    spec = detect_time(df, ["time"], {}, {}, False)
    assert spec.kind in ("index", "numeric_s", "datetime", "time_of_day")
