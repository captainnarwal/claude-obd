"""Generic time-series: trends, warm-up, steady state, rate of change, events, generic limit checks."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from obd_engine.analysis.conditions.classifier import classify, spans
from obd_engine.analysis.generic.statistics import SKIP
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register
from obd_engine.analysis.thresholds import limits_for
from obd_engine.query.service import load

MAX_PARAMS = 12


def _slope_per_min(t: np.ndarray, x: np.ndarray) -> float:
    if t.size < 3 or np.ptp(t) <= 0:
        return 0.0
    return float(np.polyfit(t, x, 1)[0] * 60.0)


class TimeSeries(Module):
    id = "time_series"
    version = "1.0.0"
    title = "Time-Series Behavior"

    def applicability(self, ctx: Context):
        return ("applicable", "") if ctx.mapped() else ("not_applicable", "no mapped parameters")

    def run(self, ctx: Context) -> dict[str, Any]:
        params = [p for p in ctx.mapped() if p not in SKIP and ctx.params[p].get("n", 0) >= 5][:MAX_PARAMS]
        df = load(ctx.ws, ctx.dataset_id, params)
        metrics: dict[str, Any] = {}
        fs: list[dict[str, Any]] = []
        for p in params:
            sub = df.filter(pl.col(p).is_not_null())
            t, x, rid = sub["t"].to_numpy(), sub[p].to_numpy(), sub["__row_id"].to_numpy()
            if x.size < 5:
                continue
            unit = ctx.params[p].get("unit")
            dt = np.diff(t)
            ok = dt > 0
            roc = np.abs(np.diff(x)[ok] / dt[ok]) if ok.any() else np.array([0.0])
            p99 = float(np.percentile(roc, 99))
            slope = _slope_per_min(t, x)
            rng = float(np.ptp(x))
            steady = float(np.mean(roc <= max(0.02 * rng, 1e-9) / max(float(np.median(dt)), 1e-9))) if rng > 0 else 1.0
            metrics[p] = {"unit": unit, "trend_per_min": round(slope, 4), "p99_rate_of_change_per_s": round(p99, 4),
                          "steady_state_fraction": round(steady, 3), "n": int(x.size)}
            for lim in limits_for(p):
                hit = (x > lim["limit"]) if lim["op"] == ">" else (x < lim["limit"])
                if hit.any():
                    # require the condition to persist for >= 3 samples to avoid isolated blips
                    run = np.convolve(hit.astype(int), np.ones(3, dtype=int), mode="valid")
                    if (run >= 3).any():
                        i0 = int(np.argmax(hit))
                        extreme = float(x.max() if lim["op"] == ">" else x.min())
                        fs.append(finding(
                            f"{p} {'exceeded' if lim['op'] == '>' else 'fell below'} {lim['limit']:g} {lim['unit']} "
                            f"(peak {extreme:.1f} {lim['unit']}, {int(hit.sum())} samples)",
                            "thermal" if "temp" in lim["name"] or p in ("ect", "iat", "oil_temp") else "electrical", "observation", lim["severity"],
                            reason="direct measurement against a limit", conditions="whole log",
                            ev=[evidence(ctx, p, t[i0], t[hit].max(), int(hit.sum()), "max" if lim["op"] == ">" else "min", extreme, lim["unit"], (rid[i0], rid[np.where(hit)[0][-1]]))],
                            threshold={"limit": lim["limit"], "unit": lim["unit"], "tier": lim["tier"], "source": lim["source"]},
                            alternatives=["Sensor fault or sentinel value", "Normal for a high-output or hot-climate engine"],
                            next_steps=["Verify with a second temperature source (e.g., infrared reading) before concluding anything"]))
        # warm-up (ect)
        if "ect" in params:
            sub = df.filter(pl.col("ect").is_not_null())
            t, x, rid = sub["t"].to_numpy(), sub["ect"].to_numpy(), sub["__row_id"].to_numpy()
            if x.size >= 10 and x[:5].mean() < 60 and x.max() >= 70:
                hot = np.where(x >= 80)[0]
                if hot.size:
                    k = int(hot[0])
                    metrics["ect_warmup"] = {"start_c": round(float(x[0]), 1), "time_to_80c_s": round(float(t[k] - t[0]), 1)}
                    fs.append(finding(f"Coolant warmed from {x[0]:.0f} to 80 C in {(t[k] - t[0]) / 60:.1f} min", "thermal", "observation",
                                      conditions="warm-up from cold start", ev=[evidence(ctx, "ect", t[0], t[k], k + 1, "time_to_80c", float(t[k] - t[0]), "s", (rid[0], rid[k]))],
                                      reason="measured; 80 C is a generic marker"))
        # event spans
        labels = classify(ctx)
        events = {st: spans(labels, "load_state", st, 2.0) for st in ("wot", "high_load", "idle")}
        metrics["events"] = {st: {"count": len(v), "total_s": round(sum(b - a for a, b, _, _ in v), 1)} for st, v in events.items()}
        if events["wot"]:
            a, b, r0, r1 = events["wot"][0]
            fs.append(finding(f"{len(events['wot'])} wide-open-throttle / max-load span(s), longest {max(e[1] - e[0] for e in events['wot']):.0f} s", "engine", "observation",
                              conditions="throttle >= 90% or load >= 90% (generic)", ev=[evidence(ctx, "throttle_position", a, b, rows=(r0, r1))]))
        return {"summary": f"Time-series behavior for {len(metrics)} item(s).", "metrics": metrics, "findings": fs, "notes": []}


register(TimeSeries())
