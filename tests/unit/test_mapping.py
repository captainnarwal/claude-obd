import pytest

from obd_engine.ingestion.mapping.scorer import ColStats, map_columns
from obd_engine.ingestion.adapters.base import split_header
from obd_engine.reference.loader import conversion, load_dictionary, norm_name


def _map(name, lo, hi, unit=None, hints=None):
    st = {name: ColStats(n=100, min=lo, max=hi, p5=lo, p95=hi, numeric=True)}
    units = {name: unit} if unit else {}
    return map_columns([name], units, st, hints or {}, set())[0]


@pytest.mark.parametrize("header,cid,status", [
    ("Engine RPM(rpm)", "engine_speed", "confirmed"),
    ("RPM", "engine_speed", "confirmed"),
    ("Vehicle Speed (mph)", "vehicle_speed", "confirmed"),
    ("Coolant Temp (°F)", "ect", "confirmed"),
    ("Engine Coolant Temperature(°C)", "ect", "confirmed"),
    ("MAF (g/s)", "maf", "confirmed"),
    ("Throttle Position (%)", "throttle_position", "confirmed"),
    ("STFT B1 (%)", "stft_b1", "confirmed"),
    ("LTFT B2 (%)", "ltft_b2", "confirmed"),
    ("Intake Air Temp (°C)", "iat", "confirmed"),
])
def test_clear_headers_confirmed(header, cid, status):
    r = _map(header, 10, 90)
    assert (r["canonical_id"], r["status"]) == (cid, status), r


@pytest.mark.parametrize("header", ["TEMP", "Temperature", "Pressure", "Voltage", "Sensor1"])
def test_generic_names_need_confirmation(header):
    r = _map(header, 10, 90)
    assert r["status"] == "needs_confirmation"


def test_temperature_without_unit_needs_confirmation():
    r = _map("Coolant Temp", 20, 95)
    assert r["canonical_id"] == "ect" and r["status"] == "needs_confirmation"
    assert "unit" in r["note"]


def test_map_without_unit_needs_confirmation():
    assert _map("MAP", 30, 100)["status"] == "needs_confirmation"


def test_fuel_trim_without_bank_needs_confirmation():
    r = _map("Short Term Fuel Trim", -5, 5)
    assert r["status"] == "needs_confirmation"
    assert "bank" in r["note"]


def test_incompatible_unit_lowers_confidence():
    r = _map("Engine RPM (V)", 700, 3000)
    assert r["status"] != "confirmed"


def test_implausible_values_lower_confidence():
    good = _map("Engine RPM", 700, 3000)["confidence"]
    bad = _map("Engine RPM", 70000, 90000)["confidence"]
    assert bad < good


def test_unknown_column_unmapped():
    r = _map("Banana Level", 0, 5)
    assert r["status"] == "unmapped" and r["canonical_id"] is None


def test_adapter_hint_blank_means_custom():
    r = _map("Barometer (on Android device)", 95, 101, hints={"barometer on android device": ""})
    assert r["status"] == "unmapped"


def test_collision_flags_both():
    st = {c: ColStats(n=10, min=1, max=9, p5=1, p95=9, numeric=True) for c in ("RPM", "Engine RPM")}
    rows = map_columns(["RPM", "Engine RPM"], {}, st, {}, set())
    assert all(r["status"] == "needs_confirmation" for r in rows)


def test_user_override_wins():
    st = {"TEMP": ColStats(n=10, min=1, max=9, p5=1, p95=9, numeric=True)}
    r = map_columns(["TEMP"], {}, st, {}, set(), {"TEMP": {"canonical_id": "ect", "unit": "°F"}})[0]
    assert r["status"] == "confirmed" and r["confirmed_by"] == "user" and r["source_unit"] == "°F"


def test_split_header_units():
    assert split_header("Speed (OBD)(km/h)") == ("Speed (OBD)", "km/h")
    assert split_header("Engine Load(%)") == ("Engine Load", "%")
    assert split_header("Speed (OBD)") == ("Speed (OBD)", "")
    assert split_header("Name", "°F") == ("Name", "°F")


def test_conversions():
    s, o = conversion("°F", "°C")
    assert round(212 * s + o, 6) == 100
    assert round(conversion("psi", "kPa")[0] * 14.5, 1) == 100.0
    assert conversion("V", "rpm") is None


def test_dictionary_aliases_unique_ids():
    d = load_dictionary()
    assert len(d.params) >= 55
    assert len({p.id for p in d.params.values()}) == len(d.params)
    assert norm_name("Engine_RPM (rpm)") == "engine rpm rpm"
