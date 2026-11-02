"""Torque Pro (Android) CSV export adapter.

Format notes (from the app's documented export): header cells like "Engine RPM(rpm)" or "Speed (OBD)(km/h)",
a "Device Time" column such as "01-Nov-2026 10:15:32.123", a "GPS Time" column, "-" for PIDs not yet polled,
phone-sensor columns (G(x), Barometer (on Android device)). Not yet validated against real exports (open question Q2).
"""

from __future__ import annotations

from obd_engine.ingestion.adapters.base import GenericDelimitedAdapter, ParseResult, read_delimited_to_parquet
from obd_engine.ingestion.discovery.sniff import FileProfile

_HINTS = {
    "engine rpm": "engine_speed",
    "speed obd": "vehicle_speed",
    "engine coolant temperature": "ect",
    "intake air temperature": "iat",
    "ambient air temp": "ambient_temp",
    "intake manifold pressure": "map",
    "mass air flow rate": "maf",
    "throttle position manifold": "throttle_position",
    "engine load": "engine_load",
    "engine load absolute": "absolute_load",
    "timing advance": "timing_advance",
    "fuel trim bank 1 short term": "stft_b1",
    "fuel trim bank 1 long term": "ltft_b1",
    "fuel trim bank 2 short term": "stft_b2",
    "fuel trim bank 2 long term": "ltft_b2",
    "air fuel ratio measured": "afr",
    "air fuel ratio commanded": "commanded_afr",
    "voltage control module": "control_module_voltage",
    "fuel level from tank": "fuel_level",
    "run time since engine start": "engine_run_time",
    "accelerator pedal position d": "accelerator_pedal",
    "latitude": "gps_latitude",
    "longitude": "gps_longitude",
    "altitude": "gps_altitude",
    "g x": "accel_x",
    "g y": "accel_y",
    "g z": "accel_z",
    # phone / derived columns that must not be confused with vehicle PIDs
    "barometer on android device": "",
    "gps speed meters second": "",
    "gps time": "",
    "horsepower at the wheels": "",
    "torque": "",
    "bearing": "",
    "gps accuracy": "",
    "horizontal dilution of precision": "",
    "g calibrated": "",
}


class TorqueProAdapter(GenericDelimitedAdapter):
    id = "torque_pro"

    def sniff(self, profile: FileProfile) -> float:
        names = {c.strip().lower() for c in profile.columns}
        score = 0.0
        if "device time" in names:
            score += 0.45
        if "gps time" in names:
            score += 0.25
        if any(n.startswith("g(x)") for n in names):
            score += 0.15
        if any(n.startswith("barometer (on android device)") for n in names):
            score += 0.15
        return min(score, 0.99)

    def column_hints(self) -> dict[str, str]:
        return dict(_HINTS)

    def time_policy(self) -> dict:
        return {"kind": "datetime", "column": "Device Time", "formats": ["%d-%b-%Y %H:%M:%S%.f"]}

    def parse(self, profile: FileProfile, out_path) -> ParseResult:
        return read_delimited_to_parquet(profile, out_path)
