"""Module G - Transmission & Drivetrain (design doc 7.4, minimal): gear inference, transmission temperature, converter slip."""

from __future__ import annotations

from typing import Any

import numpy as np

from obd_engine.analysis.modules._common import CONDITION_NOTE, STATE_TEXT, generic_threshold, labelled, runs, series
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register
from obd_engine.query.service import asof_align

MIN_KMH = 15.0
MIN_RPM = 800.0


def ratio_clusters(ratio: np.ndarray, min_share: float = 0.04, width: float = 0.04) -> list[dict[str, float]]:
    """Peaks of the log-ratio histogram: each peak is a stable rpm / (km/h) relationship (a gear or a locked converter)."""
    if ratio.size < 50:
        return []
    lr = np.log(ratio)
    lo, hi = float(np.percentile(lr, 1)), float(np.percentile(lr, 99))
    if hi - lo < width:
        edges = np.array([lo - width, hi + width])
    else:
        edges = np.arange(lo, hi + width, width)
    hist, edges = np.histogram(lr, bins=edges)
    padded = np.pad(hist.astype(float), 1)
    window = padded[:-2] + padded[1:-1] + padded[2:]  # samples within one bin either side of each bin
    out: list[dict[str, float]] = []
    last = -3
    for i in range(hist.size):
        left = window[i - 1] if i > 0 else -1.0
        right = window[i + 1] if i + 1 < hist.size else -1.0
        if window[i] >= left and window[i] > right and window[i] >= min_share * ratio.size and i - last > 1:
            m = (lr >= edges[max(i - 1, 0)]) & (lr < edges[min(i + 2, edges.size - 1)] + (1e-9 if i + 2 >= edges.size - 1 else 0))
            out.append({"rpm_per_kmh": round(float(np.median(ratio[m])), 2), "share": round(float(m.sum() / ratio.size), 3), "n": int(m.sum())})
            last = i
    return sorted(out, key=lambda c: -c["rpm_per_kmh"])


class Drivetrain(Module):
    id = "drivetrain"
    version = "1.0.0"
    title = "Transmission & Drivetrain"
    required_any = ["gear", "trans_temp", "tcc_slip", "vehicle_speed"]
    optional = ["engine_speed", "vehicle_speed"]
    config_defaults = {"slip_rpm": 100.0, "slip_s": 5.0, "min_share": 0.04}

    def applicability(self, ctx: Context):
        if ctx.has("vehicle_speed") and not (ctx.has("engine_speed") or ctx.has("gear") or ctx.has("trans_temp") or ctx.has("tcc_slip")):
            return "not_applicable", "vehicle_speed alone is not enough; needs engine_speed, a gear, transmission temperature or converter slip"
        return super().applicability(ctx)

    def run(self, ctx: Context) -> dict[str, Any]:
        cfg = ctx.config
        fs: list[dict[str, Any]] = []
        metrics: dict[str, Any] = {}
        notes = [CONDITION_NOTE]
        use = [p for p in ("gear", "trans_temp", "tcc_slip", "engine_speed", "vehicle_speed") if ctx.has(p)]
        df = labelled(ctx, use)

        if ctx.has("engine_speed") and ctx.has("vehicle_speed"):
            al, _ = asof_align(df, ["engine_speed", "vehicle_speed"])
            al = al.filter((al["vehicle_speed"] >= MIN_KMH) & (al["engine_speed"] >= MIN_RPM))
            if al.height >= 50:
                ratio = (al["engine_speed"] / al["vehicle_speed"]).to_numpy()
                cl = ratio_clusters(ratio, cfg["min_share"])
                metrics["ratio_clusters"] = cl
                if cl:
                    fs.append(finding(
                        f"{len(cl)} distinct engine-speed / road-speed relationship(s) while moving: " + ", ".join(f"{c['rpm_per_kmh']:g} rpm per km/h" for c in cl[:8]),
                        "drivetrain", "inference", confidence="medium",
                        reason=f"peaks in the rpm / (km/h) ratio for {al.height} paired samples above {MIN_KMH:g} km/h; each peak is consistent with one gear (or a locked torque converter)",
                        conditions="moving, speed above 15 km/h", ev=[evidence(ctx, "engine_speed", n=al.height, stat="ratio_clusters", value=len(cl))],
                        alternatives=["A slipping clutch or converter blurs the peaks", "A gear used only briefly may be missed"]))
            else:
                notes.append("Too few paired RPM and speed samples above 15 km/h to infer gears.")

        if ctx.has("gear"):
            g = series(df, "gear")
            x = g["gear"].to_numpy()
            if x.size:
                shifts = int((np.diff(np.round(x)) != 0).sum())
                share = {int(v): round(float((np.round(x) == v).mean()), 3) for v in np.unique(np.round(x))}
                metrics["gear"] = {"shifts": shifts, "time_share_by_gear": share}
                fs.append(finding(f"{shifts} gear change(s) reported by the ECU; gears used: {', '.join(str(k) for k in share)}", "drivetrain", "observation",
                                  ev=[evidence(ctx, "gear", float(g['t'][0]), float(g['t'][-1]), x.size, "changes", shifts)]))

        if ctx.has("trans_temp"):
            s = series(df, "trans_temp")
            t, x, rid = s["t"].to_numpy(), s["trans_temp"].to_numpy(), s["__row_id"].to_numpy()
            if x.size >= 3:
                metrics["trans_temp"] = {"unit": "°C", "start": round(float(x[0]), 1), "max": round(float(x.max()), 1), "rise": round(float(x.max() - x[0]), 1)}
                fs.append(finding(f"Transmission temperature went from {x[0]:.0f} to a peak of {x.max():.0f} C", "drivetrain", "observation",
                                  reason="direct measurement; no limit is applied because transmission limits vary widely by design", ev=[evidence(ctx, "trans_temp", t[0], t[-1], x.size, "max", float(x.max()), "°C", (rid[0], rid[-1]))]))

        if ctx.has("tcc_slip"):
            s = series(df, "tcc_slip")
            t, x, rid = s["t"].to_numpy(), s["tcc_slip"].to_numpy(), s["__row_id"].to_numpy()
            cruise = np.array([v == "cruise" for v in s["load_state"].to_list()])
            eps = runs(t, cruise & (np.abs(x) > cfg["slip_rpm"]), cfg["slip_s"])
            metrics["tcc_slip"] = {"unit": "rpm", "cruise_median": round(float(np.median(x[cruise])), 1) if cruise.any() else None, "slip_episodes": len(eps)}
            if eps:
                i0, i1 = max(eps, key=lambda e: t[e[1]] - t[e[0]])
                fs.append(finding(f"Converter slip above {cfg['slip_rpm']:g} rpm for {t[i1] - t[i0]:.0f} s at cruise ({len(eps)} episode(s))", "drivetrain", "observation", "attention",
                                  reason="sustained slip while cruising, when the converter is normally locked", conditions=STATE_TEXT["cruise"],
                                  ev=[evidence(ctx, "tcc_slip", float(t[i0]), float(t[i1]), i1 - i0 + 1, "median", float(np.median(x[i0:i1 + 1])), "rpm", (rid[i0], rid[i1]))],
                                  threshold=generic_threshold(cfg["slip_rpm"], "rpm", "locked-converter slip"),
                                  alternatives=["Unlocked by design at low speed or on a grade", "Cannot separate a worn clutch from a calibration choice using this log"]))
        return {"summary": f"Drivetrain: {len(fs)} finding(s).", "metrics": metrics, "findings": fs, "notes": notes}


register(Drivetrain())
