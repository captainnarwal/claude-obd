"""Exports from other OBD apps (long format, BlueDriver, OBDLink, Car Scanner variants)."""

import polars as pl


def _norm(ws, env):
    return pl.read_parquet(ws / ".obd" / "datasets" / env["data"]["dataset_id"] / "normalized.parquet")


def _long_csv(sep=","):
    rows = ["SECONDS,PID,VALUE,UNITS,LATITUDE,LONGITUDE".replace(",", sep)]
    t = 49700.5
    for i in range(40):
        t += 0.4
        rows.append(sep.join([f"{t:.6f}", "Engine RPM", str(800 + 10 * i), "rpm", "12.82", "80.21"]))
        rows.append(sep.join([f"{t + 0.1:.6f}", "Vehicle speed", str(i), "km/h", "12.82", "80.21"]))
        rows.append(sep.join([f"{t + 0.2:.6f}", "Engine coolant temperature", str(80 + i // 10), "C", "12.82", "80.21"]))
    return "\n".join(rows) + "\n"


def test_long_format_is_pivoted_and_mapped(run, ws):
    (ws / "long.csv").write_text(_long_csv(), encoding="utf-8")
    code, env = run("ingest", "long.csv")
    assert code == 0, env
    assert env["data"]["adapter"] == "long_format"
    names = {p["name"] for p in env["data"]["parameters"] if p["mapped"]}
    assert {"engine_speed", "vehicle_speed", "ect"} <= names
    df = _norm(ws, env)
    assert df["engine_speed"].drop_nulls().min() == 800


def test_long_format_semicolon_decimal_comma(run, ws):
    text = _long_csv(";").replace("12.82", "12,82").replace("80.21", "80,21")
    text = "\n".join(l.replace(".", ",", 1) if i else l for i, l in enumerate(text.splitlines())) + "\n"
    (ws / "long2.csv").write_text(text, encoding="utf-8")
    code, env = run("ingest", "long2.csv")
    assert code == 0, env
    assert env["data"]["adapter"] == "long_format"
    assert {"engine_speed", "vehicle_speed"} <= {p["name"] for p in env["data"]["parameters"] if p["mapped"]}


def test_wide_log_with_time_and_value_columns_is_not_long(run, ws):
    (ws / "w.csv").write_text("time,value,Engine RPM (rpm)\n" + "\n".join(f"{i},{i * 2},{900 + i}" for i in range(30)) + "\n", encoding="utf-8")
    _, env = run("inspect", "w.csv")
    assert env["data"]["adapter"] == "generic_delimited"


def test_bluedriver_utf16_preamble(run, ws):
    body = "BlueDriver Export\nDate: 2026-01-05\nTime(s),Engine RPM (rpm),Voltage (V),Vehicle Speed (km/h)\n"
    body += "\n".join(f"{i * 0.5},{900 + i},{13.9},{i}" for i in range(40)) + "\n"
    (ws / "bd.csv").write_bytes(b"\xff\xfe" + body.encode("utf-16-le"))
    code, env = run("ingest", "bd.csv")
    assert code == 0, env
    assert env["data"]["time_axis"] == "numeric_s"
    assert "engine_speed" in {p["name"] for p in env["data"]["parameters"] if p["mapped"]}


def test_obdlink_elapsed_seconds_and_unit_names(run, ws):
    body = "Time (sec),Engine RPM (RPM),Vehicle Speed (km/h),Coolant Temperature (°F)\n"
    body += "\n".join(f"{i * 0.25},{1000 + i},{i},{180 + i % 3}" for i in range(60)) + "\n"
    (ws / "ol.csv").write_text(body, encoding="utf-8")
    code, env = run("ingest", "ol.csv")
    assert code == 0, env
    assert env["data"]["time_axis"] == "numeric_s"
    df = _norm(ws, env)
    assert 80 < df["ect"].drop_nulls().median() < 90  # F converted to C


def test_car_scanner_wide_export(run, ws):
    head = 'time,"Engine RPM (rpm)","Vehicle speed (km/h)","Engine coolant temperature (℃)",Latitude,Longtitude,\n'
    rows = [f"16:43:{i:02d}.100,{800 + i},{i},{85 + i % 2},12.8253,80.2194," for i in range(50)]
    (ws / "cs.csv").write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    code, env = run("ingest", "cs.csv")
    assert code == 0, env
    assert env["data"]["time_axis"] == "time_of_day"
    mapped = {p["name"] for p in env["data"]["parameters"] if p["mapped"]}
    assert {"engine_speed", "vehicle_speed", "ect", "gps_latitude", "gps_longitude"} <= mapped


def test_obd_source_tag_does_not_hide_unit(run, ws):
    body = "Time,Engine Speed (RPM) *OBD,Vehicle Speed (km/h) *OBD,Engine Coolant Temperature (°C) *OBD\n"
    body += "\n".join(f"{i * 0.5},{900 + i},{i},{85 + i % 3}" for i in range(40)) + "\n"
    (ws / "tag.csv").write_text(body, encoding="utf-8")
    _, env = run("inspect", "tag.csv")
    got = {m["maps_to"]: m["status"] for m in env["data"]["mappings"]}
    assert got["engine_speed"] == got["vehicle_speed"] == got["ect"] == "confirmed", got
