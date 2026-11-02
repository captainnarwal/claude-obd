"""Module E - Ignition & Combustion (design doc 7.4): timing by condition, knock retard events, knock / misfire counters."""

from __future__ import annotations

from typing import Any

from obd_engine.analysis.modules._common import (CONDITION_NOTE, STATE_TEXT, by_condition, counter_increases, generic_threshold, labelled,
                                                   runs, series, state_counts)
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register


class Ignition(Module):
    id = "ignition"
    version = "1.0.0"
    title = "Ignition & Combustion"
    required_any = ["timing_advance", "knock_retard", "knock_count", "misfire_count"]
    optional = ["engine_speed", "engine_load"]
    config_defaults = {"knock_retard_deg": 1.0, "knock_warning_deg": 5.0, "event_gap_s": 2.0}

    def run(self, ctx: Context) -> dict[str, Any]:
        cfg = ctx.config
        present = [p for p in ("timing_advance", "knock_retard", "knock_count", "misfire_count") if ctx.has(p)]
        df = labelled(ctx, present)
        fs: list[dict[str, Any]] = []
        metrics: dict[str, Any] = {}
        notes = [CONDITION_NOTE]

        if "timing_advance" in present:
            s = series(df, "timing_advance")
            cond = by_condition(s, "timing_advance")
            x = s["timing_advance"].to_numpy()
            if x.size:
                metrics["timing_advance"] = {"unit": "°", "n": int(x.size), "min": round(float(x.min()), 1), "max": round(float(x.max()), 1), "by_condition": cond}
                bits = ", ".join(f"{STATE_TEXT[k]} {v['median']:.0f}°" for k, v in cond.items())
                t, rid = s["t"].to_numpy(), s["__row_id"].to_numpy()
                fs.append(finding(f"Ignition timing advance ranged {x.min():.0f}° to {x.max():.0f}°" + (f" (median by condition: {bits})" if bits else ""), "ignition", "observation",
                                  reason="direct measurement; timing is ECU-commanded and engine-specific, so no absolute limit is applied",
                                  ev=[evidence(ctx, "timing_advance", t[0], t[-1], x.size, "range", float(x.max() - x.min()), "°", (rid[0], rid[-1]))]))

        if "knock_retard" in present:
            s = series(df, "knock_retard")
            t, x, rid = s["t"].to_numpy(), s["knock_retard"].to_numpy(), s["__row_id"].to_numpy()
            state = s["load_state"].to_list()
            eps = runs(t, x <= -cfg["knock_retard_deg"], 0.0, cfg["event_gap_s"])
            metrics["knock_retard"] = {"unit": "°", "n": int(x.size), "events": len(eps), "max_retard_deg": round(float(-x.min()), 1) if x.size else 0}
            if eps:
                worst = min(eps, key=lambda e: x[e[0]:e[1] + 1].min())
                i0, i1 = worst
                deepest = float(x[i0:i1 + 1].min())
                where = state_counts(state[a] for a, _ in eps)
                metrics["knock_retard"]["events_by_condition"] = where
                fs.append(finding(
                    f"{len(eps)} knock-retard event(s); deepest {-deepest:.1f}° of retard, mostly at {STATE_TEXT[next(iter(where))]}", "ignition", "observation",
                    "warning" if -deepest >= cfg["knock_warning_deg"] else "attention",
                    reason=f"consecutive samples with at least {cfg['knock_retard_deg']:g}° of knock correction", conditions=", ".join(STATE_TEXT[k] for k in where),
                    ev=[evidence(ctx, "knock_retard", float(t[i0]), float(t[i1]), i1 - i0 + 1, "min", deepest, "°", (rid[i0], rid[i1]))],
                    threshold=generic_threshold(cfg["knock_retard_deg"], "°", "knock correction"),
                    alternatives=["Low-octane or contaminated fuel", "High intake temperature", "A noisy-sensor false trigger"],
                    next_steps=["Note whether it repeats under the same load; compare with a tank of known-good fuel"]))
        elif "timing_advance" in present and ctx.has("engine_speed"):
            notes.append("No knock-retard channel: knock cannot be assessed from timing alone, so no knock finding is made.")

        for p, label, sev in (("knock_count", "knock", "attention"), ("misfire_count", "misfire", "attention")):
            if p not in present:
                continue
            s = series(df, p)
            t, x, rid = s["t"].to_numpy(), s[p].to_numpy(), s["__row_id"].to_numpy()
            total, up, resets = counter_increases(t, x)
            state = s["load_state"].to_list()
            where = state_counts(state[i] for i in up)
            metrics[p] = {"increase": int(total), "events": int(up.size), "counter_resets": resets, "by_condition": where}
            if total > 0:
                fs.append(finding(
                    f"{label.capitalize()} counter rose by {int(total)} over the log" + (f", mostly at {STATE_TEXT[next(iter(where))]}" if where else ""), "ignition", "observation", sev,
                    reason="sum of increases of a cumulative counter" + (f"; {resets} reset(s) ignored" if resets else ""), conditions=", ".join(STATE_TEXT[k] for k in where),
                    ev=[evidence(ctx, p, float(t[up[0]]), float(t[up[-1]]), int(up.size), "increase", total, rows=(rid[up[0]], rid[up[-1]]))],
                    alternatives=["Counters can include a few counts from a normal start", "A single bad event versus a repeating pattern: look at the timing of the increases"],
                    next_steps=[f"Check whether the {label} counts line up with a specific load or RPM range"]))
            elif x.size:
                fs.append(finding(f"{label.capitalize()} counter did not rise (stayed at {int(x[0])})", "ignition", "observation", ev=[evidence(ctx, p, t[0], t[-1], x.size, "increase", 0.0)]))
        return {"summary": f"Ignition: {', '.join(present)}; {len(fs)} finding(s).", "metrics": metrics, "findings": fs, "notes": notes}


register(Ignition())
