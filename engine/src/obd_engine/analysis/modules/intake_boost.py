"""Module F - Intake & Boost (design doc 7.4): manifold pressure, boost, target vs actual, spool time, boost at light load."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from obd_engine.analysis.conditions.classifier import classify, spans
from obd_engine.analysis.modules._common import CONDITION_NOTE, STATE_TEXT, generic_threshold, labelled, runs, series
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register
from obd_engine.query.service import asof_align

STD_BARO = 101.3


class IntakeBoost(Module):
    id = "intake_boost"
    version = "1.0.1"
    title = "Intake & Boost"
    required_any = ["map", "boost", "maf"]
    optional = ["baro", "boost_target", "throttle_position", "engine_speed"]
    config_defaults = {"boost_min_kpa": 10.0, "overshoot_pct": 15.0, "overshoot_min_kpa": 10.0, "undershoot_kpa": 15.0, "episode_s": 3.0,
                       "light_load_boost_kpa": 10.0, "spool_fraction": 0.9, "min_span_s": 3.0}

    def run(self, ctx: Context) -> dict[str, Any]:
        cfg = ctx.config
        fs: list[dict[str, Any]] = []
        metrics: dict[str, Any] = {}
        notes = [CONDITION_NOTE]
        cols = [p for p in ("map", "boost", "maf", "boost_target", "baro", "throttle_position") if ctx.has(p)]
        df = labelled(ctx, cols)

        baro_ref, baro_note = STD_BARO, f"No barometric channel: boost = MAP - {STD_BARO:g} kPa (standard sea level); altitude and weather shift this."
        if ctx.has("baro"):
            baro_ref = float(np.median(series(df, "baro")["baro"].to_numpy()))
            baro_note = f"Boost = MAP - barometric pressure (median {baro_ref:.1f} kPa from the log)."

        boost = None  # (t, kPa gauge, __row_id, load_state)
        if ctx.has("boost"):
            b = series(df, "boost")
            boost = (b["t"].to_numpy(), b["boost"].to_numpy(), b["__row_id"].to_numpy(), b["load_state"].to_list())
        elif ctx.has("map"):
            m = series(df, "map")
            if m.height and float(m["map"].max()) > baro_ref + 5.0:
                boost = (m["t"].to_numpy(), m["map"].to_numpy() - baro_ref, m["__row_id"].to_numpy(), m["load_state"].to_list())
                notes.append(baro_note)

        if ctx.has("map"):
            m = series(df, "map")
            idle = m.filter((pl.col("load_state") == "idle") & pl.col("thermal_state").is_in(["warm", "unknown"]))["map"].to_numpy()
            x = m["map"].to_numpy()
            metrics["map"] = {"unit": "kPa", "n": int(x.size), "min": round(float(x.min()), 1), "max": round(float(x.max()), 1)}
            if idle.size >= 8:
                metrics["map"]["idle_median"] = round(float(np.median(idle)), 1)
                t, rid = m["t"].to_numpy(), m["__row_id"].to_numpy()
                fs.append(finding(f"Manifold pressure at warm idle: median {np.median(idle):.0f} kPa absolute (n={idle.size})", "intake", "observation",
                                  reason="direct measurement; idle MAP depends on engine, cam and altitude so no absolute limit is applied", conditions="idle, warm engine",
                                  ev=[evidence(ctx, "map", t[0], t[-1], idle.size, "median", float(np.median(idle)), "kPa", (rid[0], rid[-1]))]))
        if ctx.has("maf"):
            f_ = series(df, "maf").filter(pl.col("load_state") == "idle")["maf"].to_numpy()
            if f_.size >= 8:
                metrics["maf_idle_median_gs"] = round(float(np.median(f_)), 2)

        if boost is not None:
            t, y, rid, state = boost
            pk = int(np.argmax(y))
            metrics["boost"] = {"unit": "kPa", "peak": round(float(y[pk]), 1), "peak_state": state[pk], "baro_ref_kpa": round(baro_ref, 1)}
            if y[pk] >= cfg["boost_min_kpa"]:
                fs.append(finding(f"Peak boost {y[pk]:.0f} kPa gauge ({y[pk] * 0.145:.1f} psi) at {STATE_TEXT[state[pk]]}", "intake", "observation",
                                  reason=baro_note if not ctx.has("boost") else "direct boost channel", conditions=STATE_TEXT[state[pk]],
                                  ev=[evidence(ctx, "boost" if ctx.has("boost") else "map", float(t[pk]), float(t[pk]), int(y.size), "max", float(y[pk]), "kPa", (rid[pk], rid[pk]))]))
            # boost at light load (creep)
            light = np.array([s in ("idle", "cruise") for s in state])
            thr = series(df, "throttle_position") if ctx.has("throttle_position") else None
            if thr is not None and thr.height >= 4:
                tt, xt = thr["t"].to_numpy(), thr["throttle_position"].to_numpy()
                light &= np.interp(t, tt, xt) < 15.0
            creep = runs(t, light & (y > cfg["light_load_boost_kpa"]), cfg["episode_s"])
            metrics["boost"]["light_load_episodes"] = len(creep)
            if creep:
                i0, i1 = max(creep, key=lambda e: y[e[0]:e[1] + 1].max())
                fs.append(finding(f"Boost above {cfg['light_load_boost_kpa']:g} kPa at light load ({len(creep)} episode(s), up to {y[i0:i1 + 1].max():.0f} kPa)", "intake", "observation", "attention",
                                  reason="sustained pressure above the limit while idle/cruising with low throttle (where little boost is normally commanded)",
                                  conditions="idle or light cruise", ev=[evidence(ctx, "boost" if ctx.has("boost") else "map", float(t[i0]), float(t[i1]), i1 - i0 + 1, "max", float(y[i0:i1 + 1].max()), "kPa", (rid[i0], rid[i1]))],
                                  threshold=generic_threshold(cfg["light_load_boost_kpa"], "kPa", "light-load boost"),
                                  alternatives=["Normal on some engines or with downhill coasting under a load", "Possible boost control issue - cannot be told from this log alone"]))
            # spool time on high-load spans
            labels = classify(ctx)
            spool = []
            for a, b_, r0, r1 in spans(labels, "load_state", "wot", cfg["min_span_s"]) + spans(labels, "load_state", "high_load", cfg["min_span_s"]):
                mk = (t >= a) & (t <= b_)
                if mk.sum() >= 3 and y[mk].max() >= 2 * cfg["boost_min_kpa"]:
                    ts, ys = t[mk], y[mk]
                    reach = np.where(ys >= cfg["spool_fraction"] * ys.max())[0]
                    spool.append({"t0": round(a, 1), "spool_s": round(float(ts[reach[0]] - a), 2), "peak_kpa": round(float(ys.max()), 1)})
            if spool:
                metrics["spool"] = spool[:10]
                med = float(np.median([s["spool_s"] for s in spool]))
                metrics["spool_median_s"] = round(med, 2)
                fs.append(finding(f"Boost reached {cfg['spool_fraction'] * 100:.0f}% of its peak a median {med:.1f} s after high load began ({len(spool)} run(s))", "intake", "observation",
                                  reason="measured from the start of the high-load span; limited by the boost sampling interval", conditions="high load / wide-open throttle",
                                  ev=[evidence(ctx, "boost" if ctx.has("boost") else "map", spool[0]["t0"], spool[0]["t0"], len(spool), "median_spool", med, "s")]))
            # target vs actual
            if ctx.has("boost_target"):
                src = "boost" if ctx.has("boost") else "map"
                al, dropped = asof_align(df, [src, "boost_target"])
                if al.height >= 8:
                    actual = al[src].to_numpy() - (0.0 if src == "boost" else baro_ref)
                    tgt = al["boost_target"].to_numpy()
                    err = actual - tgt
                    ta = al["t"].to_numpy()
                    metrics["boost_vs_target"] = {"n": int(err.size), "mean_error_kpa": round(float(err.mean()), 1), "max_overshoot_kpa": round(float(err.max()), 1), "max_undershoot_kpa": round(float(err.min()), 1)}
                    over = runs(ta, (err > cfg["overshoot_min_kpa"]) & (err > tgt * cfg["overshoot_pct"] / 100.0), cfg["episode_s"])
                    under = runs(ta, (tgt > cfg["boost_min_kpa"]) & (err < -cfg["undershoot_kpa"]), cfg["episode_s"])
                    metrics["boost_vs_target"].update(overshoot_episodes=len(over), shortfall_episodes=len(under))
                    for name, eps, sev in (("overshoot", over, "attention"), ("shortfall", under, "attention")):
                        if eps:
                            i0, i1 = max(eps, key=lambda e: abs(err[e[0]:e[1] + 1]).max())
                            seg = err[i0:i1 + 1]
                            ext = float(np.median(seg))
                            fs.append(finding(f"Boost {name} versus target: {len(eps)} episode(s), typically {ext:+.0f} kPa (worst {seg[np.argmax(np.abs(seg))]:+.0f} kPa, includes spool-up)", "intake", "observation", sev,
                                              reason=f"actual vs. commanded boost paired by time proximity; episodes of at least {cfg['episode_s']:g} s", conditions="boost target above zero",
                                              ev=[evidence(ctx, src, float(ta[i0]), float(ta[i1]), i1 - i0 + 1, "median_error", ext, "kPa", (int(al['__row_id'][i0]), int(al['__row_id'][i1])))],
                                              threshold=generic_threshold(cfg["undershoot_kpa"] if name == "shortfall" else cfg["overshoot_min_kpa"], "kPa", "boost control error"),
                                              alternatives=["Boost control, leak, wastegate or sensor calibration - this log cannot separate them"]))
                else:
                    notes.append("Boost target and actual could not be paired in enough samples.")
        elif ctx.has("map"):
            notes.append("MAP never exceeded barometric pressure by more than 5 kPa: no boost to analyse (normal for a naturally aspirated engine).")
        return {"summary": "Intake: " + ((f"peak boost {metrics['boost']['peak']:.0f} kPa; " if metrics["boost"]["peak"] > 0 else f"no positive boost (gauge pressure never above {metrics['boost']['peak']:.0f} kPa); ") if "boost" in metrics else "") + f"{len(fs)} finding(s).", "metrics": metrics, "findings": fs, "notes": notes}


register(IntakeBoost())
