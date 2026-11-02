"""Generic correlation on as-of aligned data, with lag search and condition split."""

from __future__ import annotations

from itertools import combinations
from typing import Any

import numpy as np
import polars as pl

from obd_engine.analysis.generic.statistics import SKIP
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register
from obd_engine.query.service import asof_align, load, thin_blocks

MAX_PARAMS = 10
MIN_N = 30
STRONG = 0.7


def _pearson(a: np.ndarray, b: np.ndarray) -> float | None:
    if a.size < 3 or a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    ra, rb = a.argsort().argsort().astype(float), b.argsort().argsort().astype(float)
    return _pearson(ra, rb)


def correlate_pair(df: pl.DataFrame, a: str, b: str, max_lag_s: float = 0.0) -> dict[str, Any] | None:
    al, info = asof_align(df, [a, b])
    if al.height < MIN_N:
        return None
    x, y = al[a].to_numpy(), al[b].to_numpy()
    res: dict[str, Any] = {"a": a, "b": b, "n": int(al.height), "pearson": _pearson(x, y), "spearman": _spearman(x, y),
                           "row_span": [int(al["__row_id"].min()), int(al["__row_id"].max())],
                           "time_range_s": [float(al["t"].min()), float(al["t"].max())]}
    if res["pearson"] is None:
        return None
    if max_lag_s > 0:
        # shift b relative to a by lag seconds on the aligned timeline
        best = (res["pearson"], 0.0)
        step = max(max_lag_s / 10, 0.1)
        for lag in np.arange(-max_lag_s, max_lag_s + step / 2, step):
            sh = df.select("t", "__row_id", "segment_id", a, b).with_columns(pl.when(pl.col(b).is_not_null()).then(pl.col("t") + lag).otherwise(None).alias("_tb"))
            right = sh.filter(pl.col("_tb").is_not_null()).select(pl.col("_tb").alias("t"), b).sort("t")
            left = df.filter(pl.col(a).is_not_null()).select("t", a).sort("t")
            j = left.join_asof(right, on="t", strategy="nearest", tolerance=max(1.0, step * 2)).drop_nulls()
            if j.height >= MIN_N:
                r = _pearson(j[a].to_numpy(), j[b].to_numpy())
                if r is not None and abs(r) > abs(best[0]) + 1e-6:
                    best = (r, float(lag))
        res["best_lag_s"], res["best_lag_pearson"] = round(best[1], 3), round(best[0], 4)
    res["pearson"] = round(res["pearson"], 4)
    res["spearman"] = None if res["spearman"] is None else round(res["spearman"], 4)
    return res


class Correlation(Module):
    id = "correlation"
    version = "1.0.1"
    title = "Parameter Correlations"
    config_defaults = {"max_lag_s": 0.0}

    def applicability(self, ctx: Context):
        n = len([p for p in ctx.mapped() if p not in SKIP])
        return ("applicable", "") if n >= 2 else ("not_applicable", "needs at least two mapped parameters")

    def run(self, ctx: Context) -> dict[str, Any]:
        params = [p for p in ctx.mapped() if p not in SKIP and ctx.params[p].get("n", 0) >= MIN_N][:MAX_PARAMS]
        df, thinned = thin_blocks(load(ctx.ws, ctx.dataset_id, params))
        pairs = []
        for a, b in combinations(params, 2):
            r = correlate_pair(df, a, b, float(ctx.config.get("max_lag_s", 0.0)))
            if r:
                pairs.append(r)
        pairs.sort(key=lambda r: -abs(r["pearson"]))
        fs = []
        for r in pairs[:5]:
            if abs(r["pearson"]) >= STRONG:
                fs.append(finding(
                    f"{r['a']} and {r['b']} move together (Pearson r = {r['pearson']:+.2f}, n = {r['n']})", "correlation", "observation",
                    reason="correlation is not causation; many parameters rise together under load",
                    ev=[evidence(ctx, f"{r['a']}~{r['b']}", r["time_range_s"][0], r["time_range_s"][1], r["n"], "pearson", r["pearson"], rows=tuple(r["row_span"]))]))
        return {
            "summary": f"{len(pairs)} pair(s) analysed; {sum(abs(p['pearson']) >= STRONG for p in pairs)} strong.",
            "metrics": {"pairs": pairs[:40]},
            "findings": fs, "notes": ["Method: Pearson/Spearman on as-of aligned samples (staleness = 2x the parameter's median interval, min 1 s)."]
            + (["Large dataset: evenly spaced contiguous blocks (about 150k rows) were used."] if thinned else []),
        }


register(Correlation())
