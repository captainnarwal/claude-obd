"""Shared helpers for the system modules: condition-labelled loading, per-condition stats, sustained episodes."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import polars as pl

from obd_engine.analysis.conditions.classifier import GENERIC_NOTE, LOAD_STATES, classify
from obd_engine.analysis.registry.base import Context
from obd_engine.query.service import load

MIN_COND_N = 8
CARRY_S = 5.0  # a carried-forward value older than this is treated as missing
CONDITION_NOTE = f"Conditions use {GENERIC_NOTE}."

STATE_TEXT = {"idle": "idle", "cruise": "cruise", "moderate": "moderate load", "high_load": "high load", "wot": "wide-open throttle",
              "off": "engine off/cranking", "unknown": "unknown conditions"}


def labelled(ctx: Context, params: list[str], extra: Iterable[str] = ()) -> pl.DataFrame:
    """Rows of `params` with load_state / thermal_state attached (as-of), and `<extra>__c` columns carried forward <= 5 s."""
    extra = [e for e in extra if ctx.has(e) and e not in ("t",)]
    df = load(ctx.ws, ctx.dataset_id, list(dict.fromkeys([*params, *extra]))).sort("t")
    labels = classify(ctx).select("t", "load_state", "thermal_state").sort("t")
    df = df.join_asof(labels, on="t", strategy="backward")
    for e in extra:
        right = df.filter(pl.col(e).is_not_null()).select("t", pl.col(e).alias(f"{e}__c")).sort("t")
        df = df.join_asof(right, on="t", strategy="backward", tolerance=CARRY_S)
    return df


def series(df: pl.DataFrame, p: str) -> pl.DataFrame:
    """Non-null rows of one parameter (keeps t, __row_id and the label / carried columns)."""
    return df.filter(pl.col(p).is_not_null() & pl.col(p).is_not_nan())


def by_condition(sub: pl.DataFrame, p: str, col: str = "load_state", states: Iterable[str] = LOAD_STATES, min_n: int = MIN_COND_N) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for st in states:
        x = sub.filter(pl.col(col) == st)[p].to_numpy()
        x = x[np.isfinite(x)]
        if x.size >= min_n:
            out[st] = {"n": int(x.size), "mean": round(float(x.mean()), 2), "median": round(float(np.median(x)), 2),
                       "p5": round(float(np.percentile(x, 5)), 2), "p95": round(float(np.percentile(x, 95)), 2)}
    return out


def runs(t: np.ndarray, mask: np.ndarray, min_len_s: float = 3.0, max_gap_s: float = 5.0) -> list[tuple[int, int]]:
    """Index ranges (i0, i1 inclusive) of consecutive true samples lasting >= min_len_s; a time gap > max_gap_s splits a run."""
    out: list[tuple[int, int]] = []
    start = last = None
    for i in range(len(t)):
        if mask[i]:
            if start is not None and t[i] - t[last] > max_gap_s:  # type: ignore[index]
                if t[last] - t[start] >= min_len_s:  # type: ignore[index]
                    out.append((start, last))  # type: ignore[arg-type]
                start = None
            if start is None:
                start = i
            last = i
        elif start is not None:
            if t[last] - t[start] >= min_len_s:  # type: ignore[index]
                out.append((start, last))  # type: ignore[arg-type]
            start = None
    if start is not None and t[last] - t[start] >= min_len_s:  # type: ignore[index]
        out.append((start, last))  # type: ignore[arg-type]
    return out


def counter_increases(t: np.ndarray, x: np.ndarray) -> tuple[float, np.ndarray, int]:
    """(total increase, indices where the counter went up, number of resets). Decreases are resets, not negative counts."""
    if x.size < 2:
        return 0.0, np.array([], dtype=int), 0
    d = np.diff(x)
    up = np.where(d > 0)[0] + 1
    return float(d[d > 0].sum()), up, int((d < 0).sum())


def generic_threshold(limit: float, unit: str, what: str) -> dict[str, Any]:
    return {"limit": limit, "unit": unit, "tier": "generic", "source": f"generic rule of thumb for {what}; may not apply to your engine"}


def state_counts(labels: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in labels:
        out[s] = out.get(s, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
