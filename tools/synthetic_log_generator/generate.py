"""Deterministic synthetic drive-log generator with injectable faults (design doc 13.3).

    python -m tools.synthetic_log_generator.generate out.csv --duration 1200 --style torque --faults lean,dropout --seed 1
    python -m tools.synthetic_log_generator.generate big.csv --mb 100
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

FAULTS = {"lean", "overheat", "dropout", "reset", "sentinel", "spike",
          # "perf" style only: knock events, misfires, boost leak, bank imbalance, lean at WOT; also slow warm-up and idle heat soak
          "knock", "misfire", "boost_leak", "bank_imbalance", "lean_wot", "slow_warmup", "heatsoak"}
CYCLE_S = 600.0
# (time in cycle, speed km/h)
_SPEED_KEYS = [(0, 0), (30, 0), (90, 50), (200, 58), (215, 110), (330, 105), (380, 0), (420, 0), (480, 60), (560, 55), (600, 0)]
_WOT = (200.0, 215.0)


def _cycle(t: np.ndarray, rng: np.random.Generator, faults: set[str], t_total: float) -> dict[str, np.ndarray]:
    c = np.mod(t, CYCLE_S)
    kt, ks = zip(*_SPEED_KEYS)
    clean_speed = np.interp(c, kt, ks)
    speed = np.clip(clean_speed + rng.normal(0, 0.4, t.size), 0, None)
    gear = np.digitize(speed, [1, 20, 40, 65, 90])
    ratio = np.array([0, 60, 40, 30, 23, 19.0])[gear]
    rpm = 800 + speed * ratio * 0.85 + rng.normal(0, 18, t.size)
    wot = (c >= _WOT[0]) & (c <= _WOT[1])
    accel = np.gradient(clean_speed, t) if t.size > 1 else np.zeros_like(t)
    throttle = np.clip(6 + speed * 0.28 + np.clip(accel, 0, None) * 6, 0, 100)
    throttle = np.where(wot, 96 + rng.normal(0, 1, t.size), throttle) + rng.normal(0, 0.3, t.size)
    throttle = np.clip(throttle, 0, 100)
    rpm = np.where(wot, np.maximum(rpm, 4200 + (c - _WOT[0]) * 150), rpm)
    load = np.clip(18 + throttle * 0.7 + rng.normal(0, 1, t.size), 0, 100)
    mapk = np.clip(32 + throttle * 0.68 + rng.normal(0, 1, t.size), 15, 250)
    maf = np.clip(rpm / 60.0 * load / 100.0 * 0.9 + rng.normal(0, 0.2, t.size), 0.5, None)
    tau = 700.0 if "slow_warmup" in faults else 280.0
    ect = 20 + 72 * (1 - np.exp(-t / tau)) + rng.normal(0, 0.15, t.size)
    if "overheat" in faults:
        late = t > 0.6 * t_total
        ect = np.where(late, ect + (t - 0.6 * t_total) * 0.12, ect)
    iat = 24 + 9 * (1 - np.exp(-t / 500.0)) + rng.normal(0, 0.2, t.size)
    if "heatsoak" in faults:
        iat = iat + np.where((speed < 2) & (t > 150), 12.0, 0.0)
    timing = np.clip(10 + rpm / 400 - load / 9 + rng.normal(0, 0.4, t.size), -5, 45)
    stft = rng.normal(0, 1.6, t.size)
    ltft = 1.0 + rng.normal(0, 0.25, t.size)
    if "lean" in faults:
        idle = speed < 2
        cruise = (~idle) & (~wot)
        ltft = ltft + np.where(idle, 14.0, np.where(cruise, 5.5, 0.0))
        stft = stft + np.where(idle, 4.0, 0.0)
    volt = 14.1 + rng.normal(0, 0.05, t.size)
    return dict(rpm=rpm, speed=speed, throttle=throttle, load=load, map=mapk, maf=maf, ect=ect, iat=iat,
                timing=timing, stft=stft, ltft=ltft, volt=volt)


def _lag(target: np.ndarray, dt: float, tau: float) -> np.ndarray:
    out = np.empty_like(target)
    y, a = 0.0, dt / (tau + dt)
    for i, x in enumerate(target):
        y += a * (x - y)
        out[i] = y
    return out


def _perf(t: np.ndarray, v: dict[str, np.ndarray], faults: set[str], rng: np.random.Generator, hz: float) -> dict[str, np.ndarray]:
    """Extra channels for the dense "perf" style: turbo boost, lambda, bank 2 trims, knock and misfire counters."""
    n = t.size
    c = np.mod(t, CYCLE_S)
    wot = (c >= _WOT[0]) & (c <= _WOT[1])
    idle = v["speed"] < 2
    thr, rpm = v["throttle"], v["rpm"]
    target = 130.0 * np.clip((thr - 30) / 66, 0, 1) * (rpm > 2200)
    boost = _lag(target, 1.0 / hz, 1.2)
    if "boost_leak" in faults:
        boost = np.where(target > 0, boost * 0.55, boost)
    boost = boost + rng.normal(0, 0.5, n)
    mapk = np.clip(30 + 0.7 * np.minimum(thr, 70), 20, 101) + boost + rng.normal(0, 0.5, n)
    lam = 1.0 + rng.normal(0, 0.008, n)
    lam = np.where(wot, (1.04 if "lean_wot" in faults else 0.86) + rng.normal(0, 0.008, n), lam)
    if "lean" in faults:
        lam = np.where(idle, 1.08 + rng.normal(0, 0.008, n), lam)
    knock_zone = wot & (c >= 205) & (c <= 209)
    kr = np.where(knock_zone, -4.0 + rng.normal(0, 0.2, n), 0.0) if "knock" in faults else np.zeros(n)
    kcount = np.cumsum((kr < -1.0) & (np.roll(kr, 1) >= -1.0))
    mis = (wot & (rng.random(n) < 0.03)) if "misfire" in faults else np.zeros(n, dtype=bool)
    ltft2 = v["ltft"] + (7.0 if "bank_imbalance" in faults else 0.0)
    return dict(boost=boost, boost_target=target, map=mapk, lam=lam, kr=kr, kcount=kcount, mcount=np.cumsum(mis),
                baro=101.3 + rng.normal(0, 0.1, n), stft2=v["stft"] + rng.normal(0, 0.5, n), ltft2=ltft2 + rng.normal(0, 0.2, n),
                timing=v["timing"] + kr)


def _apply_faults(v: dict[str, np.ndarray], t: np.ndarray, faults: set[str], rng: np.random.Generator, t_total: float):
    n = t.size
    if "sentinel" in faults:
        idx = rng.choice(n, size=max(3, n // 5000), replace=False)
        v["ect"][idx] = -40.0
    if "spike" in faults:
        idx = rng.choice(n, size=max(2, n // 8000), replace=False)
        v["rpm"][idx] = 9800.0
    drop = np.zeros(n, dtype=bool)
    if "dropout" in faults:
        drop = (t > 0.45 * t_total) & (t < 0.45 * t_total + 25)
    return drop


def generate(path: str | Path, duration_s: float = 1200.0, hz: float = 2.5, style: str = "torque",
             faults: set[str] | None = None, seed: int = 1, start: datetime | None = None, chunk_rows: int = 200_000) -> Path:
    faults = set(faults or ())
    bad = faults - FAULTS
    if bad:
        raise ValueError(f"unknown faults {bad}")
    start = start or datetime(2026, 11, 1, 10, 0, 0)
    path = Path(path)
    n_total = int(duration_s * hz)
    rng = np.random.default_rng(seed)
    first = True
    for lo in range(0, n_total, chunk_rows):
        hi = min(n_total, lo + chunk_rows)
        t = np.arange(lo, hi) / hz
        v = _cycle(t, rng, faults, duration_s)
        if style == "perf":
            v.update(_perf(t, v, faults, rng, hz))
        drop = _apply_faults(v, t, faults, rng, duration_s)
        df = _frame(t, v, drop, style, start, rng, faults, duration_s, lo)
        with open(path, "ab" if not first else "wb") as f:
            df.write_csv(f, include_header=first, quote_style="necessary")
        first = False
    return path


def _fmt(a: np.ndarray, nd: int, mask: np.ndarray | None = None) -> pl.Series:
    s = pl.Series([f"{x:.{nd}f}" for x in a])
    if mask is not None:
        s = pl.Series(np.where(mask, "-", s.to_numpy()))
    return s


def _frame(t, v, drop, style, start, rng, faults, t_total, offset) -> pl.DataFrame:
    n = t.size
    if style == "torque":
        idx = np.arange(offset, offset + n)
        grp = idx % 3
        def sparse(key, nd, g):
            m = (grp != g) | drop
            return _fmt(v[key], nd, m)
        times = [(start + timedelta(seconds=float(x))) for x in t]
        if "reset" in faults:
            half = int(0.5 * t_total * 2.5)
            times = [tm if (offset + i) < half else tm - timedelta(seconds=float(0.5 * t_total - 5)) for i, tm in enumerate(times)]
        dev = pl.Series([tm.strftime("%d-%b-%Y %H:%M:%S.") + f"{tm.microsecond // 1000:03d}" for tm in times])
        return pl.DataFrame({
            "GPS Time": dev, "Device Time": dev, "Longitude": _fmt(np.full(n, -0.1278), 6), "Latitude": _fmt(np.full(n, 51.5074), 6),
            "GPS Speed (Meters/second)": _fmt(v["speed"] / 3.6, 2), "Horizontal Dilution of Precision": _fmt(np.full(n, 1.2), 1),
            "Altitude": _fmt(np.full(n, 35.0), 1), "Bearing": _fmt(np.full(n, 90.0), 1),
            "G(x)": _fmt(rng.normal(0, 0.02, n), 3), "G(y)": _fmt(rng.normal(0, 0.02, n), 3), "G(z)": _fmt(rng.normal(1, 0.02, n), 3),
            "G(calibrated)": _fmt(rng.normal(0, 0.02, n), 3),
            "Engine RPM(rpm)": _fmt(v["rpm"], 0, drop), "Speed (OBD)(km/h)": _fmt(v["speed"], 0, drop),
            "Throttle Position(Manifold)(%)": _fmt(v["throttle"], 1, drop), "Engine Load(%)": _fmt(v["load"], 1, drop),
            "Engine Coolant Temperature(°C)": sparse("ect", 0, 1), "Intake Air Temperature(°C)": sparse("iat", 0, 1),
            "Intake Manifold Pressure(kpa)": sparse("map", 0, 1), "Mass Air Flow Rate(g/s)": sparse("maf", 2, 1),
            "Fuel Trim Bank 1 Short Term(%)": sparse("stft", 1, 2), "Fuel Trim Bank 1 Long Term(%)": sparse("ltft", 1, 2),
            "Timing Advance(°)": sparse("timing", 1, 2), "Voltage (Control Module)(V)": sparse("volt", 2, 2),
        })
    if style == "perf":
        return pl.DataFrame({
            "Time (s)": _fmt(t, 3), "Engine RPM": _fmt(v["rpm"], 0), "Vehicle Speed (km/h)": _fmt(v["speed"], 1),
            "Throttle Position (%)": _fmt(v["throttle"], 1), "Engine Load (%)": _fmt(v["load"], 1),
            "Coolant Temp (°C)": _fmt(v["ect"], 1), "Intake Air Temp (°C)": _fmt(v["iat"], 1),
            "MAP (kPa)": _fmt(v["map"], 1), "Boost (kPa)": _fmt(v["boost"], 1), "Boost Target (kPa)": _fmt(v["boost_target"], 1),
            "Barometric Pressure (kPa)": _fmt(v["baro"], 1), "MAF (g/s)": _fmt(v["maf"], 2),
            "Short Term Fuel Trim Bank 1 (%)": _fmt(v["stft"], 1), "Long Term Fuel Trim Bank 1 (%)": _fmt(v["ltft"], 1),
            "Short Term Fuel Trim Bank 2 (%)": _fmt(v["stft2"], 1), "Long Term Fuel Trim Bank 2 (%)": _fmt(v["ltft2"], 1),
            "Lambda": _fmt(v["lam"], 3), "Timing Advance (°)": _fmt(v["timing"], 1), "Knock Retard (°)": _fmt(v["kr"], 1),
            "Knock Count": _fmt(v["kcount"], 0), "Misfire Count": _fmt(v["mcount"], 0),
        })
    # generic: dense, imperial units, unit-in-header
    tt = t.copy()
    if "reset" in faults:
        half = int(0.5 * t_total * 2.5)
        tt = np.where(np.arange(offset, offset + n) < half, t, t - (0.5 * t_total - 5))
    return pl.DataFrame({
        "Time (s)": _fmt(tt, 3), "Engine RPM": _fmt(v["rpm"], 0, drop), "Vehicle Speed (mph)": _fmt(v["speed"] / 1.609344, 1, drop),
        "Coolant Temp (°F)": _fmt(v["ect"] * 9 / 5 + 32, 1), "Intake Air Temp (°F)": _fmt(v["iat"] * 9 / 5 + 32, 1),
        "MAF (g/s)": _fmt(v["maf"], 2), "MAP (kPa)": _fmt(v["map"], 1), "Throttle (%)": _fmt(v["throttle"], 1, drop),
        "Engine Load (%)": _fmt(v["load"], 1), "STFT B1 (%)": _fmt(v["stft"], 1), "LTFT B1 (%)": _fmt(v["ltft"], 1),
        "Timing Advance (°)": _fmt(v["timing"], 1),
    })


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--duration", type=float, default=1200.0)
    ap.add_argument("--hz", type=float, default=2.5)
    ap.add_argument("--style", choices=["torque", "generic", "perf"], default="torque")
    ap.add_argument("--faults", default="")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--mb", type=float, help="approximate size; overrides --duration")
    a = ap.parse_args()
    dur = a.duration
    if a.mb:
        bytes_per_row = {"torque": 155, "perf": 210}.get(a.style, 110)
        dur = a.mb * 1024 * 1024 / bytes_per_row / a.hz
    p = generate(a.out, dur, a.hz, a.style, set(filter(None, a.faults.split(","))), a.seed)
    print(f"{p} {p.stat().st_size / 1024**2:.1f} MB")


if __name__ == "__main__":
    main()
