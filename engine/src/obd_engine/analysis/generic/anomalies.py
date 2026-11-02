"""Basic anomaly detection: robust z-score vs rolling median/MAD, flatlines, impossible combinations."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from obd_engine.analysis.generic.statistics import SKIP
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register
from obd_engine.query.service import load, state_frame
from obd_engine.reference.loader import load_dictionary

MAX_PARAMS = 20
WINDOW = 31
Z_THRESH = 6.0
MAX_EVENTS_PER_PARAM = 5
MAX_SPIKE_SAMPLES = 4
LARGE_SERIES = 50_000
GAP_S = 5.0


def rolling_robust_z(x: np.ndarray, window: int, floor: float) -> np.ndarray:
    s = pl.Series(x)
    med = s.rolling_median(window, center=True, min_samples=max(3, window // 3)).to_numpy()
    dev = np.abs(x - med)
    mad = pl.Series(dev).rolling_median(window, center=True, min_samples=max(3, window // 3)).to_numpy()
    scale = np.maximum(1.4826 * mad, floor)
    return (x - med) / scale


def robust_z_screened(x: np.ndarray, window: int, floor: float, z_thresh: float) -> np.ndarray:
    """Same z-scores as rolling_robust_z wherever a spike is possible, computed only near candidates for long series.

    A sample with robust z > z_thresh deviates from its local median by > z_thresh*floor, so a cheap 3-point median
    screen finds every candidate; the exact windowed statistic then runs on slices around them (margin 2*window
    covers the nested median-of-deviations window)."""
    n = x.size
    if n <= LARGE_SERIES:
        return np.nan_to_num(rolling_robust_z(x, window, floor))
    med3 = pl.Series(x).rolling_median(3, center=True, min_samples=1).to_numpy()
    cand = np.where(np.abs(x - med3) > 0.7 * z_thresh * floor)[0]
    if cand.size > 0.05 * n:
        return np.nan_to_num(rolling_robust_z(x, window, floor))
    z = np.zeros(n)
    if cand.size == 0:
        return z
    # cluster candidates that are close together
    breaks = np.where(np.diff(cand) > window)[0]
    starts = np.concatenate(([cand[0]], cand[breaks + 1]))
    ends = np.concatenate((cand[breaks], [cand[-1]]))
    for a, b in zip(starts, ends):
        lo, hi = max(0, a - 2 * window), min(n, b + 2 * window + 1)
        zz = np.nan_to_num(rolling_robust_z(x[lo:hi], window, floor))
        z[a : b + 1] = zz[a - lo : b - lo + 1]
    return z


def detect_spikes(t, x, rid, floor, window=WINDOW, z_thresh=Z_THRESH):  # noqa: C901
    z = robust_z_screened(x, window, floor, z_thresh)
    idx = np.where(np.abs(z) > z_thresh)[0]
    events = []
    for i in idx:
        if events and i - events[-1]["i1"] <= 2:
            e = events[-1]
            e["i1"] = int(i)
            if abs(z[i]) > abs(e["z"]):
                e["z"], e["v"] = float(z[i]), float(x[i])
        else:
            events.append({"i0": int(i), "i1": int(i), "z": float(z[i]), "v": float(x[i])})
    for e in events:
        e["t0"], e["t1"], e["r0"], e["r1"] = float(t[e["i0"]]), float(t[e["i1"]]), int(rid[e["i0"]]), int(rid[e["i1"]])
    # a spike is isolated: a level shift or ramp keeps deviating for many samples and is not an anomaly
    events = [e for e in events if e["i1"] - e["i0"] + 1 <= MAX_SPIKE_SAMPLES]
    # samples next to a large time gap are level changes across a dropout, not spikes
    def near_gap(e):
        lo, hi = max(e["i0"] - 1, 0), min(e["i1"] + 1, len(t) - 1)
        return bool(np.any(np.diff(t[lo : hi + 1]) > GAP_S))

    edge = max(3, window // 2)  # rolling statistics are unreliable at the very ends of the series
    events = [e for e in events if not near_gap(e) and edge <= e["i0"] and e["i1"] < len(t) - edge]
    events.sort(key=lambda e: -abs(e["z"]))
    return events


def detect_flatline(t, x, min_s):
    out, start = [], 0
    for i in range(1, len(x) + 1):
        if i == len(x) or x[i] != x[start]:
            if t[i - 1] - t[start] >= min_s and i - start >= 8:
                out.append((start, i - 1))
            start = i
    return out


class Anomalies(Module):
    id = "anomalies"
    version = "1.0.3"
    title = "Anomaly Detection"
    config_defaults = {"z_threshold": Z_THRESH, "window": WINDOW, "flatline_s": 60.0, "params": None}

    def applicability(self, ctx: Context):
        return ("applicable", "") if ctx.mapped() else ("not_applicable", "no mapped parameters")

    def run(self, ctx: Context) -> dict[str, Any]:
        d = load_dictionary()
        zt, win, flat_s = float(ctx.config["z_threshold"]), int(ctx.config["window"]), float(ctx.config["flatline_s"])
        params = [p for p in ctx.mapped() if p not in SKIP and d.params[p].kind == "continuous" and ctx.params[p].get("n", 0) >= win][:MAX_PARAMS]
        if ctx.config.get("params"):
            params = [p for p in params if p in ctx.config["params"]]
        df = load(ctx.ws, ctx.dataset_id, params)
        events_out, fs = [], []
        for p in params:
            sub = df.filter(pl.col(p).is_not_null())
            t, x, rid = sub["t"].to_numpy(), sub[p].to_numpy().astype(float), sub["__row_id"].to_numpy()
            lo, hi = d.params[p].range
            floor = max(0.005 * (hi - lo), 1e-6) if hi < 1e5 else 1.0
            unit = ctx.params[p].get("unit")
            for e in detect_spikes(t, x, rid, floor, win, zt)[:MAX_EVENTS_PER_PARAM]:
                ev = {"parameter": p, "type": "spike", "t_start_s": round(e["t0"], 2), "t_end_s": round(e["t1"], 2), "value": round(e["v"], 3),
                      "robust_z": round(e["z"], 1), "unit": unit, "row_span": [e["r0"], e["r1"]]}
                events_out.append(ev)
            # severity: only a few strong events become findings
            strong = [e for e in events_out if e["parameter"] == p and abs(e["robust_z"]) >= 2 * zt]
            if strong:
                e = strong[0]
                fs.append(finding(
                    f"{p}: isolated spike to {e['value']} {unit or ''} ({abs(e['robust_z']):.1f} robust SDs from the rolling median)",
                    "anomaly", "observation", "attention", reason="rolling median/MAD, window %d samples" % win,
                    ev=[evidence(ctx, p, e["t_start_s"], e["t_end_s"], 1, "robust_z", e["robust_z"], unit, tuple(e["row_span"]))],
                    alternatives=["Electrical noise or a bad read", "A real transient"],
                    next_steps=["Check whether neighbouring parameters moved at the same moment"]))
            fl = detect_flatline(t, x, flat_s)
            if fl and np.ptp(x) > 0:
                i0, i1 = max(fl, key=lambda r: t[r[1]] - t[r[0]])
                events_out.append({"parameter": p, "type": "flatline", "t_start_s": round(float(t[i0]), 2), "t_end_s": round(float(t[i1]), 2),
                                   "value": round(float(x[i0]), 3), "unit": unit, "row_span": [int(rid[i0]), int(rid[i1])]})
                fs.append(finding(f"{p}: constant at {x[i0]:.3g} {unit or ''} for {t[i1] - t[i0]:.0f} s", "anomaly", "observation", "info",
                                  reason="value did not change although it varies elsewhere in the log",
                                  ev=[evidence(ctx, p, t[i0], t[i1], i1 - i0 + 1, "constant_value", float(x[i0]), unit, (rid[i0], rid[i1]))],
                                  alternatives=["Genuinely steady condition", "Stuck or stale sensor value"]))
        # impossible combination: moving but engine speed ~ 0
        if ctx.has("vehicle_speed", "engine_speed"):
            sdf = load(ctx.ws, ctx.dataset_id, ["vehicle_speed", "engine_speed"])
            sf = state_frame(sdf, ["vehicle_speed", "engine_speed"], 3.0).drop_nulls(subset=["vehicle_speed", "engine_speed"])
            bad = sf.filter((pl.col("vehicle_speed") > 15) & (pl.col("engine_speed") < 300))
            if bad.height >= 3:
                events_out.append({"parameter": "vehicle_speed/engine_speed", "type": "impossible_combination",
                                   "t_start_s": round(float(bad["t"].min()), 2), "t_end_s": round(float(bad["t"].max()), 2), "n": bad.height})
                fs.append(finding("Vehicle moving above 15 km/h while engine speed reads under 300 rpm", "anomaly", "observation", "attention",
                                  ev=[evidence(ctx, "vehicle_speed/engine_speed", float(bad["t"].min()), float(bad["t"].max()), bad.height, "count", bad.height,
                                               rows=(int(bad["__row_id"].min()), int(bad["__row_id"].max())))],
                                  alternatives=["Dropped/stale RPM readings", "Mapping error (wrong column)", "Engine-off coasting"]))
        events_out.sort(key=lambda e: -abs(e.get("robust_z", 0)))
        return {"summary": f"{len(events_out)} anomaly event(s) across {len(params)} parameter(s).",
                "metrics": {"events": events_out[:60], "total_events": len(events_out)}, "findings": fs,
                "notes": [f"Spikes: |robust z| > {zt:g} vs rolling median/MAD over {win} samples; flatlines: >= {flat_s:g} s."]}


register(Anomalies())
