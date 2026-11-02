"""Query service: windowed loads (DuckDB over Parquet), downsampling, as-of alignment (design doc 8.3)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import duckdb
import numpy as np
import polars as pl

from obd_engine.cli.envelope import ObdError, clean_text
from obd_engine.storage.workspace import Workspace

META_COLS = ("__row_id", "t", "t_abs", "segment_id")


def normalized_path(ws: Workspace, dataset_id: str) -> Path:
    p = ws.dataset_dir(dataset_id) / "normalized.parquet"
    if not p.exists():
        raise ObdError("DATASET_NOT_FOUND", f"No normalized data for {dataset_id}.")
    return p


def available_columns(ws: Workspace, dataset_id: str) -> list[str]:
    return [c for c in pl.read_parquet_schema(normalized_path(ws, dataset_id)) if c not in META_COLS and not c.endswith("__q")]


def resolve_params(ws: Workspace, dataset_id: str, wanted: Iterable[str]) -> list[str]:
    cols = available_columns(ws, dataset_id)
    lower = {c.lower(): c for c in cols}
    out = []
    for w in wanted:
        if w in cols:
            out.append(w)
        elif w.lower() in lower:
            out.append(lower[w.lower()])
        else:
            near = [c for c in cols if w.lower() in c.lower()][:8] or cols[:8]
            raise ObdError("PARAMETER_NOT_AVAILABLE", f"Parameter '{clean_text(w)}' is not in this dataset.",
                           "Use one of the available names.", data={"near_matches": [clean_text(c) for c in near], "available": [clean_text(c) for c in cols[:60]]})
    return out


def load(ws: Workspace, dataset_id: str, params: list[str], t0: float | None = None, t1: float | None = None,
         include_flagged: bool = False, with_meta: tuple[str, ...] = ("__row_id", "t", "segment_id")) -> pl.DataFrame:
    """Column-pruned, time-pruned load. Flagged (out-of-range/sentinel) samples become null unless include_flagged."""
    path = normalized_path(ws, dataset_id)
    schema = pl.read_parquet_schema(path)
    cols = list(dict.fromkeys([*with_meta, *params]))
    qcols = [f"{p}__q" for p in params if f"{p}__q" in schema] if not include_flagged else []
    sel = ", ".join('"' + c.replace('"', '""') + '"' for c in [*cols, *qcols] if c in schema)  # column names come from untrusted headers
    where, args = [], [str(path)]
    if t0 is not None:
        where.append("t >= ?")
        args.append(t0)
    if t1 is not None:
        where.append("t <= ?")
        args.append(t1)
    q = f"SELECT {sel} FROM read_parquet(?)" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY t"
    df = duckdb.connect(":memory:").sql(q, params=args).pl()
    for p in params:
        if f"{p}__q" in df.columns:
            df = df.with_columns(pl.when(pl.col(f"{p}__q") == 0).then(pl.col(p)).otherwise(None).alias(p))
    return df.drop([c for c in df.columns if c.endswith("__q")])


def lttb(x: np.ndarray, y: np.ndarray, n_out: int) -> np.ndarray:
    """Largest-Triangle-Three-Buckets; returns selected indices."""
    n = x.size
    if n_out >= n or n_out < 3:
        return np.arange(n)
    sel = [0]
    every = (n - 2) / (n_out - 2)
    a = 0
    for i in range(n_out - 2):
        lo = int((i + 1) * every) + 1
        hi = min(int((i + 2) * every) + 1, n)
        avg_x, avg_y = x[lo:hi].mean(), y[lo:hi].mean()
        r_lo, r_hi = int(i * every) + 1, int((i + 1) * every) + 1
        px, py = x[a], y[a]
        area = np.abs((px - avg_x) * (y[r_lo:r_hi] - py) - (px - x[r_lo:r_hi]) * (avg_y - py))
        a = r_lo + int(np.argmax(area))
        sel.append(a)
    sel.append(n - 1)
    return np.array(sel)


def lttb_series(t: np.ndarray, v: np.ndarray, n_out: int) -> tuple[np.ndarray, np.ndarray]:
    m = np.isfinite(v)
    t, v = t[m], v[m]
    idx = lttb(t, v, n_out)
    return t[idx], v[idx]


def bucket(df: pl.DataFrame, params: list[str], max_points: int, agg: str = "mean") -> pl.DataFrame:
    """Min/max/mean per time bucket for analytical excerpts. Returns bucket mid-time + aggregated values."""
    if df.height <= max_points:
        return df
    t0, t1 = float(df["t"].min()), float(df["t"].max())
    width = (t1 - t0) / max_points or 1.0
    fn = {"mean": pl.mean, "min": pl.min, "max": pl.max, "median": pl.median}[agg]
    return (
        df.with_columns(((pl.col("t") - t0) // width).cast(pl.Int64).clip(upper_bound=max_points - 1).alias("_b"))
        .group_by("_b", maintain_order=True)
        .agg([pl.col("t").mean().alias("t"), pl.col("__row_id").min().alias("__row_id")] + [fn(p).alias(p) for p in params])
        .drop("_b")
    )


def effective_interval(df: pl.DataFrame, p: str) -> float:
    t = df.filter(pl.col(p).is_not_null())["t"].to_numpy()
    if t.size < 2:
        return 1.0
    return float(np.median(np.diff(t))) or 1.0


def asof_align(df: pl.DataFrame, params: list[str], max_staleness: float | None = None) -> tuple[pl.DataFrame, dict[str, int]]:
    """Anchor on the sparsest parameter; pair others with their most recent value within staleness. Returns (frame, dropped)."""
    nn = {p: df.filter(pl.col(p).is_not_null()).select("t", "__row_id", p, "segment_id") for p in params}
    anchor = min(params, key=lambda p: nn[p].height if nn[p].height else 1 << 60)
    base = nn[anchor].sort("t")
    dropped: dict[str, int] = {}
    for p in params:
        if p == anchor:
            continue
        tol = max_staleness if max_staleness is not None else max(1.0, 2.0 * effective_interval(df, p))
        right = nn[p].select("t", p).sort("t")
        base = base.join_asof(right, on="t", strategy="backward", tolerance=tol)
        dropped[p] = int(base[p].is_null().sum())
    out = base.drop_nulls(subset=params)
    return out, {**dropped, "_rows_before": base.height, "_rows_after": out.height}


def state_frame(df: pl.DataFrame, params: list[str], stale_s: float = 5.0) -> pl.DataFrame:
    """All normalized rows with each param carried forward (as-of) up to stale_s seconds."""
    base = df.select("__row_id", "t", "segment_id").sort("t")
    for p in params:
        if p not in df.columns:
            continue
        right = df.filter(pl.col(p).is_not_null()).select("t", p).sort("t")
        base = base.join_asof(right, on="t", strategy="backward", tolerance=stale_s)
    return base


def thin_blocks(df: pl.DataFrame, target_rows: int = 150_000, n_blocks: int = 300) -> tuple[pl.DataFrame, bool]:
    """Keep evenly spaced contiguous blocks (preserving local polling structure) so pairwise work stays bounded."""
    n = df.height
    if n <= target_rows:
        return df, False
    block_len = max(int(round(n / n_blocks)), 1)
    keep_len = max(int(round(block_len * target_rows / n)), 1)
    mask = (np.arange(n) % block_len) < keep_len
    return df.filter(pl.Series(mask)), True
