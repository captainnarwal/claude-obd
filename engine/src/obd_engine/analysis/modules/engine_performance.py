"""Module B - Engine Performance (design doc 7.4): throttle response, wide-open-throttle runs, wheel-power estimate."""

from __future__ import annotations

from typing import Any

import numpy as np

from obd_engine.analysis.conditions.classifier import classify, spans
from obd_engine.analysis.modules._common import CONDITION_NOTE, labelled, series
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register

G = 9.80665


class EnginePerformance(Module):
    id = "engine_performance"
    version = "1.0.0"
    title = "Engine Performance"
    required_all = ["engine_speed"]
    optional = ["throttle_position", "vehicle_speed"]
    config_defaults = {"step_points": 25.0, "step_window_s": 1.0, "response_rpm_pct": 10.0, "min_run_s": 3.0}

    def run(self, ctx: Context) -> dict[str, Any]:
        cfg = ctx.config
        thr_name = next((p for p in ("throttle_position", "accelerator_pedal") if ctx.has(p)), None)
        fs: list[dict[str, Any]] = []
        metrics: dict[str, Any] = {}
        notes = [CONDITION_NOTE]
        df = labelled(ctx, ["engine_speed", *([thr_name] if thr_name else []), *(["vehicle_speed"] if ctx.has("vehicle_speed") else [])])
        r = series(df, "engine_speed")
        tr, xr = r["t"].to_numpy(), r["engine_speed"].to_numpy()

        if thr_name and r.height >= 10:
            th = series(df, thr_name)
            tt, xt, rid = th["t"].to_numpy(), th[thr_name].to_numpy(), th["__row_id"].to_numpy()
            delays, steps, i = [], [], 0
            while i < tt.size - 1:
                j = int(np.searchsorted(tt, tt[i] + cfg["step_window_s"], side="right")) - 1
                if j > i and xt[i] < 40 and xt[j] - xt[i] >= cfg["step_points"]:
                    k0 = max(int(np.searchsorted(tr, tt[i], side="right")) - 1, 0)
                    base = xr[k0]
                    target = base * (1 + cfg["response_rpm_pct"] / 100.0) + 50.0
                    after = np.where((tr > tt[i]) & (tr <= tt[i] + 5.0) & (xr >= target))[0]
                    if after.size:
                        delays.append(float(tr[after[0]] - tt[i]))
                        steps.append((i, j))
                    i = int(np.searchsorted(tt, tt[i] + 5.0))
                else:
                    i += 1
            if delays:
                d = np.array(delays)
                res = float(np.median(np.diff(tr))) if tr.size > 1 else 0.0
                metrics["throttle_response"] = {"tip_ins": int(d.size), "median_s": round(float(np.median(d)), 2), "max_s": round(float(d.max()), 2), "rpm_resolution_s": round(res, 2)}
                a, b = steps[0]
                fs.append(finding(f"Engine speed answered a throttle tip-in in a median {np.median(d):.1f} s (n={d.size} tip-ins, slowest {d.max():.1f} s)", "engine", "observation",
                                  reason=f"time from a >= {cfg['step_points']:g}-point throttle rise until RPM was {cfg['response_rpm_pct']:g}% higher; resolution is limited by the RPM sampling interval (about {res:.1f} s)",
                                  conditions="throttle tip-in from below 40%", ev=[evidence(ctx, thr_name, float(tt[a]), float(tt[b]), int(d.size), "median_delay", float(np.median(d)), "s", (rid[a], rid[b]))]))
            elif r.height:
                notes.append("No clear throttle tip-ins were found, so throttle response was not assessed.")
        elif not thr_name:
            notes.append("No throttle or pedal channel: throttle response was not assessed.")

        labels = classify(ctx)
        wots = spans(labels, "load_state", "wot", cfg["min_run_s"])
        runs_out = []
        sp = series(df, "vehicle_speed") if ctx.has("vehicle_speed") else None
        mass = (((ctx.vehicle or {}).get("drivetrain") or {}).get("mass_kg"))
        for a, b, r0, r1 in wots:
            m = (tr >= a) & (tr <= b)
            run: dict[str, Any] = {"t0": round(a, 1), "t1": round(b, 1), "duration_s": round(b - a, 1), "peak_rpm": int(xr[m].max()) if m.any() else None}
            if sp is not None and sp.height >= 4:
                ts, xs = sp["t"].to_numpy(), sp["vehicle_speed"].to_numpy()
                if ts[0] <= a and ts[-1] >= b and ((ts >= a) & (ts <= b)).sum() >= 2:
                    v0, v1 = float(np.interp(a, ts, xs)), float(np.interp(b, ts, xs))
                    run.update(speed_start_kmh=round(v0, 1), speed_end_kmh=round(v1, 1))
                    if v1 > v0 + 5:
                        acc = (v1 - v0) / 3.6 / (b - a)
                        run["mean_acceleration_ms2"] = round(acc, 2)
                        if mass:
                            vm = (v0 + v1) / 2 / 3.6
                            run["wheel_power_kw_lower_bound"] = round(float(mass) * acc * vm / 1000.0, 1)
            runs_out.append(run)
        if runs_out:
            metrics["wot_runs"] = runs_out[:10]
            best = max(runs_out, key=lambda x: x["duration_s"])
            speed_txt = f", {best['speed_start_kmh']:.0f}->{best['speed_end_kmh']:.0f} km/h" if "speed_end_kmh" in best else ""
            fs.append(finding(f"{len(runs_out)} wide-open-throttle run(s); longest {best['duration_s']:.0f} s{speed_txt}, peak {best['peak_rpm']} rpm", "engine", "observation",
                              reason="runs of at least {:.0f} s at throttle or load >= 90% (generic condition)".format(cfg["min_run_s"]), conditions="wide-open throttle",
                              ev=[evidence(ctx, "engine_speed", best["t0"], best["t1"], stat="peak", value=best["peak_rpm"], unit="rpm")]))
            powered = [x for x in runs_out if "wheel_power_kw_lower_bound" in x]
            if powered:
                p = max(powered, key=lambda x: x["wheel_power_kw_lower_bound"])
                fs.append(finding(f"Wheel power over a wide-open-throttle run was at least about {p['wheel_power_kw_lower_bound']:.0f} kW ({p['wheel_power_kw_lower_bound'] * 1.341:.0f} hp)",
                                  "engine", "inference", confidence="low",
                                  reason="m x a x v from the entered vehicle mass and logged speed; ignores aerodynamic drag, rolling resistance and gradient, so it is a lower bound at the wheels, not engine power",
                                  conditions="wide-open throttle", ev=[evidence(ctx, "vehicle_speed", p["t0"], p["t1"], stat="wheel_power_kw", value=p["wheel_power_kw_lower_bound"], unit="kW")],
                                  alternatives=["Real wheel power is higher; crank power is higher again by drivetrain losses"]))
        if wots and not mass and sp is not None:
            notes.append("Enter the vehicle's mass (`vehicle set --mass-kg`) to get a wheel-power lower bound for wide-open-throttle runs.")
        return {"summary": f"Engine performance: {len(runs_out)} wide-open-throttle run(s)" + (f", {metrics['throttle_response']['tip_ins']} tip-in(s)" if "throttle_response" in metrics else "") + ".",
                "metrics": metrics, "findings": fs, "notes": notes}


register(EnginePerformance())
