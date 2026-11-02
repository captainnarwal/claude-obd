"""Operating-condition classifier (design doc 7.3). Generic fallback thresholds are labeled as such."""

from __future__ import annotations

import polars as pl

from obd_engine.analysis.registry.base import Context
from obd_engine.query.service import load, state_frame

NEEDS = ["engine_speed", "vehicle_speed", "throttle_position", "engine_load", "ect", "map"]
GENERIC_NOTE = "generic condition thresholds (idle <1200 rpm, WOT >=90% throttle/load, warm >=80C)"
LOAD_STATES = ["idle", "cruise", "moderate", "high_load", "wot"]


def available(ctx: Context) -> list[str]:
    return [p for p in NEEDS if ctx.has(p)]


def classify(ctx: Context, df: pl.DataFrame | None = None) -> pl.DataFrame:
    """Per-row labels: engine_state, thermal_state, load_state. Requires nothing, degrades to 'unknown'."""
    params = available(ctx)
    if df is None:
        df = load(ctx.ws, ctx.dataset_id, params) if params else load(ctx.ws, ctx.dataset_id, [])
    sf = state_frame(df, params)
    cols = sf.columns

    def c(name):
        return pl.col(name) if name in cols else pl.lit(None, dtype=pl.Float64)

    rpm, spd, thr, load_, ect = c("engine_speed"), c("vehicle_speed"), c("throttle_position"), c("engine_load"), c("ect")
    engine_state = (
        pl.when(rpm.is_null()).then(pl.lit("unknown"))
        .when(rpm < 50).then(pl.lit("off"))
        .when(rpm < 400).then(pl.lit("cranking"))
        .otherwise(pl.lit("running"))
    )
    thermal = (
        pl.when(ect.is_null()).then(pl.lit("unknown"))
        .when(ect < 50).then(pl.lit("cold"))
        .when(ect < 80).then(pl.lit("warming"))
        .otherwise(pl.lit("warm"))
    )
    stopped = pl.when(spd.is_not_null()).then(spd < 2).otherwise(thr < 10)
    load_state = (
        pl.when(rpm.is_not_null() & (rpm < 400)).then(pl.lit("off"))
        .when((thr >= 90) | (load_ >= 90)).then(pl.lit("wot"))
        .when((thr >= 60) | (load_ >= 70)).then(pl.lit("high_load"))
        .when(stopped & ((rpm < 1200) | rpm.is_null())).then(pl.lit("idle"))
        .when(spd.is_not_null() & (spd >= 5) & (thr < 35)).then(pl.lit("cruise"))
        .when(rpm.is_null() & thr.is_null() & load_.is_null() & spd.is_null()).then(pl.lit("unknown"))
        .otherwise(pl.lit("moderate"))
    )
    return sf.with_columns(
        engine_state.alias("engine_state"), thermal.alias("thermal_state"), load_state.alias("load_state"),
    ).select("__row_id", "t", "segment_id", "engine_state", "thermal_state", "load_state")


def spans(labels: pl.DataFrame, col: str, value: str, min_len_s: float = 2.0) -> list[tuple[float, float, int, int]]:
    """Contiguous (t0, t1, row0, row1) runs where labels[col]==value and lasting >= min_len_s."""
    if labels.height == 0:
        return []
    m = (labels[col] == value).to_numpy()
    t = labels["t"].to_numpy()
    rid = labels["__row_id"].to_numpy()
    out, start = [], None
    for i, v in enumerate(m):
        if v and start is None:
            start = i
        if (not v or i == len(m) - 1) and start is not None:
            end = i if v else i - 1
            if t[end] - t[start] >= min_len_s:
                out.append((float(t[start]), float(t[end]), int(rid[start]), int(rid[end])))
            start = None
    return out
