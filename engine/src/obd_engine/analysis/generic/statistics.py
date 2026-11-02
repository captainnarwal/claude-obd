"""Generic statistics: per-parameter stats overall and by operating condition."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from obd_engine.analysis.conditions.classifier import GENERIC_NOTE, LOAD_STATES, classify
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register
from obd_engine.query.service import load

SKIP = {"gps_latitude", "gps_longitude", "gps_altitude", "odometer"}
MAX_PARAMS = 25
MIN_COND_N = 10


def stat_row(x: np.ndarray) -> dict[str, float]:
    return {
        "n": int(x.size), "mean": float(x.mean()), "std": float(x.std()), "min": float(x.min()), "p5": float(np.percentile(x, 5)),
        "median": float(np.median(x)), "p95": float(np.percentile(x, 95)), "max": float(x.max()),
    }


def select_params(ctx: Context, wanted: list[str] | None = None) -> list[str]:
    if wanted:
        return wanted
    ps = [p for p in ctx.mapped() if p not in SKIP and ctx.params[p].get("n", 0) >= 3]
    return ps[:MAX_PARAMS]


def compute_stats(ctx: Context, params: list[str], by_condition: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    df = load(ctx.ws, ctx.dataset_id, params)
    excluded = {p: int(ctx.params.get(p, {}).get("out_of_range", 0) + ctx.params.get(p, {}).get("sentinels", 0)) for p in params}
    out: dict[str, Any] = {}
    for p in params:
        x = df[p].drop_nulls().to_numpy()
        x = x[np.isfinite(x)]
        if x.size:
            out[p] = {**stat_row(x), "unit": ctx.params.get(p, {}).get("unit"), "excluded_flagged": excluded[p]}
    cond: dict[str, Any] = {}
    if by_condition and out:
        labels = classify(ctx)
        joined = df.sort("t").join_asof(labels.select("t", "load_state", "thermal_state").sort("t"), on="t", strategy="backward")
        for p in out:
            cond[p] = {}
            for st in LOAD_STATES:
                x = joined.filter((pl.col("load_state") == st) & pl.col(p).is_not_null())[p].to_numpy()
                if x.size >= MIN_COND_N:
                    cond[p][st] = {"n": int(x.size), "mean": round(float(x.mean()), 3), "p5": round(float(np.percentile(x, 5)), 3), "p95": round(float(np.percentile(x, 95)), 3)}
    return out, cond


class Statistics(Module):
    id = "statistics"
    version = "1.0.0"
    title = "Parameter Statistics"
    config_defaults = {"by_condition": True}

    def applicability(self, ctx: Context):
        return ("applicable", "") if ctx.mapped() or ctx.params else ("not_applicable", "no numeric parameters")

    def run(self, ctx: Context) -> dict[str, Any]:
        params = select_params(ctx)
        stats, cond = compute_stats(ctx, params, ctx.config.get("by_condition", True))
        fs = []
        if "engine_speed" in stats:
            s = stats["engine_speed"]
            fs.append(finding(f"Engine speed peaked at {s['max']:.0f} rpm (median {s['median']:.0f})", "engine", "observation",
                              ev=[evidence(ctx, "engine_speed", n=s["n"], stat="max", value=s["max"], unit="rpm")]))
        if "vehicle_speed" in stats:
            s = stats["vehicle_speed"]
            fs.append(finding(f"Vehicle speed peaked at {s['max']:.0f} km/h", "vehicle", "observation",
                              ev=[evidence(ctx, "vehicle_speed", n=s["n"], stat="max", value=s["max"], unit="km/h")]))
        notes = [f"Condition split uses {GENERIC_NOTE}."] if cond else []
        return {"summary": f"Statistics for {len(stats)} parameter(s).", "metrics": {"stats": stats, "by_condition": cond}, "findings": fs, "notes": notes}


register(Statistics())
