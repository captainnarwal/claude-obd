"""Module D - Thermal (design doc 7.4): warm-up, steady-state temperature, overshoot, fall-back, intake heat-soak."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from obd_engine.analysis.modules._common import CONDITION_NOTE, STATE_TEXT, generic_threshold, labelled, runs, series
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register

WARM_C = 80.0  # generic "operating temperature reached" marker (same as the condition classifier)


class Thermal(Module):
    id = "thermal"
    version = "1.0.0"
    title = "Thermal"
    required_all = ["ect"]
    optional = ["iat", "vehicle_speed"]
    config_defaults = {"slow_warmup_s": 900.0, "overshoot_c": 8.0, "overshoot_s": 10.0, "fallback_c": 10.0, "fallback_s": 20.0,
                       "heatsoak_c": 8.0, "load_rise_c": 6.0}

    def run(self, ctx: Context) -> dict[str, Any]:
        cfg = ctx.config
        df = labelled(ctx, ["ect", *(["iat"] if ctx.has("iat") else [])])
        e = series(df, "ect")
        if e.height < 3:
            return {"summary": "Thermal: too few valid coolant samples.", "metrics": {"n": e.height}, "findings": [], "notes": []}
        t, x, rid = e["t"].to_numpy(), e["ect"].to_numpy(), e["__row_id"].to_numpy()
        state = e["load_state"].to_list()
        fs: list[dict[str, Any]] = []
        metrics: dict[str, Any] = {"ect_start_c": round(float(x[:5].mean()), 1), "ect_max_c": round(float(x.max()), 1), "n": int(x.size)}
        notes = [CONDITION_NOTE, f"{WARM_C:g} C is a generic 'warmed up' marker, not an engine specification."]
        cold_start = bool(x[:5].mean() < 60)
        metrics["cold_start"] = cold_start
        k_warm = int(np.argmax(x >= WARM_C)) if (x >= WARM_C).any() else None

        if cold_start and x.size >= 10:
            if k_warm is not None and k_warm > 0:
                dur = float(t[k_warm] - t[0])
                lo = np.where(x >= 40)[0]
                rate = None
                if lo.size and lo[0] < k_warm and t[k_warm] > t[lo[0]]:
                    rate = (WARM_C - x[lo[0]]) / ((t[k_warm] - t[lo[0]]) / 60.0)
                metrics["warmup"] = {"time_to_80c_s": round(dur, 1), "rate_c_per_min_40_to_80": round(float(rate), 2) if rate is not None else None}
                if dur > cfg["slow_warmup_s"]:
                    fs.append(finding(
                        f"Slow warm-up: coolant took {dur / 60:.0f} min to reach {WARM_C:g} C (from {x[0]:.0f} C)", "thermal", "observation", "attention",
                        reason="measured time to a generic marker; ambient temperature and driving style change this a lot",
                        conditions="warm-up from cold start", ev=[evidence(ctx, "ect", t[0], t[k_warm], k_warm + 1, "time_to_80c", dur, "s", (rid[0], rid[k_warm]))],
                        threshold=generic_threshold(cfg["slow_warmup_s"], "s", "warm-up time"),
                        alternatives=["Cold ambient temperature or mostly light-load driving", "Thermostat that opens early (cannot be told from this log alone)"],
                        next_steps=["Repeat in similar conditions and compare; check the thermostat only if it is consistently slow"]))
            elif k_warm is None and t[-1] - t[0] >= 600:
                fs.append(finding(f"Coolant never reached {WARM_C:g} C in {(t[-1] - t[0]) / 60:.0f} min (peak {x.max():.0f} C)", "thermal", "observation", "attention",
                                  reason="measured; may simply be a short or gentle trip", conditions="warm-up from cold start",
                                  ev=[evidence(ctx, "ect", t[0], t[-1], x.size, "max", float(x.max()), "°C", (rid[0], rid[-1]))],
                                  threshold=generic_threshold(WARM_C, "°C", "operating temperature")))

        if k_warm is not None:
            tw, xw, rw, sw = t[k_warm:], x[k_warm:], rid[k_warm:], state[k_warm:]
            if xw.size >= 10:
                steady = float(np.median(xw))
                p5, p95 = float(np.percentile(xw, 5)), float(np.percentile(xw, 95))
                metrics["steady_state"] = {"median_c": round(steady, 1), "p5_c": round(p5, 1), "p95_c": round(p95, 1), "n": int(xw.size)}
                fs.append(finding(f"Steady-state coolant temperature about {steady:.0f} C (5th-95th percentile {p5:.0f}-{p95:.0f} C, n={xw.size})", "thermal", "observation",
                                  reason="median of samples after first reaching the warm marker", conditions="warm engine",
                                  ev=[evidence(ctx, "ect", tw[0], tw[-1], xw.size, "median", steady, "°C", (rw[0], rw[-1]))]))
                over = runs(tw, xw > steady + cfg["overshoot_c"], cfg["overshoot_s"])
                if over:
                    i0, i1 = max(over, key=lambda r: xw[r[0]:r[1] + 1].max())
                    pk = float(xw[i0:i1 + 1].max())
                    metrics["overshoot"] = {"episodes": len(over), "peak_c": round(pk, 1), "above_median_c": round(pk - steady, 1)}
                    fs.append(finding(
                        f"Coolant rose {pk - steady:.0f} C above its steady level (peak {pk:.0f} C) in {len(over)} episode(s)", "thermal", "observation", "attention",
                        reason=f"sustained >= {cfg['overshoot_s']:g} s above median + {cfg['overshoot_c']:g} C; relative to this log's own steady state",
                        conditions=", ".join(sorted({STATE_TEXT[s] for s in sw[i0:i1 + 1]})),
                        ev=[evidence(ctx, "ect", float(tw[i0]), float(tw[i1]), i1 - i0 + 1, "max", pk, "°C", (rw[i0], rw[i1]))],
                        alternatives=["Expected rise under sustained load or heat soak", "Cooling-system limitation (cannot be told apart from this log alone)"],
                        next_steps=["Check whether the rise lines up with load or with standing still (idle heat soak)"]))
                early = float(np.median(xw[:max(10, xw.size // 4)]))  # level when it first warmed up
                back = runs(tw, xw < early - cfg["fallback_c"], cfg["fallback_s"])
                if back:
                    i0, i1 = back[0]
                    fs.append(finding(
                        f"Coolant fell {early - xw[i0:i1 + 1].min():.0f} C below its early warm level for {tw[i1] - tw[i0]:.0f} s after warming up", "thermal", "observation", "attention",
                        reason="sustained drop after the engine had been warm; relative to the level shortly after first warming up",
                        ev=[evidence(ctx, "ect", float(tw[i0]), float(tw[i1]), i1 - i0 + 1, "min", float(xw[i0:i1 + 1].min()), "°C", (rw[i0], rw[i1]))],
                        alternatives=["Long downhill or low-load cruise in cold air", "Thermostat cycling or a sensor dropout"]))
                hi = np.array([s in ("high_load", "wot") for s in sw])
                idle = np.array([s == "idle" for s in sw])
                if hi.sum() >= 8 and idle.sum() >= 8:
                    rise = float(np.median(xw[hi]) - np.median(xw[idle]))
                    metrics["load_vs_idle_c"] = round(rise, 1)
                    if rise >= cfg["load_rise_c"]:
                        fs.append(finding(f"Coolant ran {rise:.0f} C hotter under high load than at idle (median {np.median(xw[hi]):.0f} vs {np.median(xw[idle]):.0f} C)", "thermal", "inference",
                                          confidence="medium", reason=f"medians of warm samples (n={int(hi.sum())} / {int(idle.sum())})", conditions="warm engine, high load vs idle",
                                          ev=[evidence(ctx, "ect", n=int(hi.sum()), stat="median", value=float(np.median(xw[hi])), unit="°C")]))

        if ctx.has("iat"):
            i = series(df, "iat")
            warm = i.filter(pl.col("thermal_state").is_in(["warm", "unknown"]))
            a = warm.filter(pl.col("load_state") == "idle")["iat"].to_numpy()
            b = warm.filter(pl.col("load_state") == "cruise")["iat"].to_numpy()
            if a.size >= 8 and b.size >= 8:
                d = float(np.median(a) - np.median(b))
                metrics["iat_idle_minus_cruise_c"] = round(d, 1)
                if d >= cfg["heatsoak_c"]:
                    fs.append(finding(f"Intake air was {d:.0f} C hotter at idle than at cruise (median {np.median(a):.0f} vs {np.median(b):.0f} C)", "thermal", "observation", "attention",
                                      reason=f"medians of warm samples (n={a.size} / {b.size}); typical of heat soak when the car is stationary", conditions="idle vs cruise, warm engine",
                                      ev=[evidence(ctx, "iat", n=int(a.size), stat="median_idle", value=float(np.median(a)), unit="°C"),
                                          evidence(ctx, "iat", n=int(b.size), stat="median_cruise", value=float(np.median(b)), unit="°C")],
                                      threshold=generic_threshold(cfg["heatsoak_c"], "°C", "idle heat-soak difference")))
        return {"summary": f"Thermal: start {metrics['ect_start_c']} C, peak {metrics['ect_max_c']} C; {len(fs)} finding(s).", "metrics": metrics, "findings": fs, "notes": notes}


register(Thermal())
