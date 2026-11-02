"""Matplotlib (Agg) chart renderers writing PNGs into obd-reports/charts (design doc 8.4)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from obd_engine.analysis.conditions.classifier import classify, spans  # noqa: E402
from obd_engine.analysis.registry.base import make_context  # noqa: E402
from obd_engine.analysis.thresholds import limits_for  # noqa: E402
from obd_engine.cli.envelope import ObdError  # noqa: E402
from obd_engine.query import service as q  # noqa: E402
from obd_engine.storage.catalog import Catalog  # noqa: E402
from obd_engine.storage.workspace import Workspace  # noqa: E402

MAX_POINTS = 5000
KINDS = ("timeseries", "scatter", "histogram", "comparison")
PALETTE = ["#2a6fbb", "#d9822b", "#2e9e6b", "#a64ca6", "#c0392b", "#6c7a89"]


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower()[:60]


def _unit(ctx, p: str) -> str:
    u = ctx.params.get(p, {}).get("unit")
    return f" ({u})" if u else ""


def _unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    i = 2
    while True:
        cand = base.with_name(f"{base.stem}-{i}{base.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _style(ax) -> None:
    ax.grid(True, alpha=0.25, linewidth=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def render_chart(ws: Workspace, cat: Catalog, row, kind: str, params: list[str], t0: float | None = None, t1: float | None = None,
                 name: str | None = None, other_rows: list | None = None, overwrite_name: bool = False) -> dict[str, Any]:
    if kind not in KINDS:
        raise ObdError("USAGE", f"Unknown chart type '{kind}'.", f"Use one of: {', '.join(KINDS)}", exit_code=2)
    params = q.resolve_params(ws, row["id"], params)
    if kind == "scatter" and len(params) != 2:
        raise ObdError("USAGE", "scatter needs exactly two parameters (x,y).", exit_code=2)
    if kind in ("histogram", "comparison") and len(params) != 1:
        raise ObdError("USAGE", f"{kind} needs exactly one parameter.", exit_code=2)
    if kind == "timeseries" and not 1 <= len(params) <= 6:
        raise ObdError("USAGE", "timeseries supports 1-6 parameters.", exit_code=2)
    ctx = make_context(ws, cat, row)
    df = q.load(ws, row["id"], params, t0, t1)
    if df.height == 0:
        raise ObdError("NO_DATA", "No samples in that window.")
    ws.ensure_reports()
    stem = Path(row["file_name"]).stem
    fname = (name or f"{_slug(stem)}-{_slug('-'.join(params))}-{kind}") + ".png"
    out = ws.charts_dir / fname if overwrite_name else _unique_path(ws.charts_dir / fname)
    caption, shown = "", {}

    if kind == "timeseries":
        n = len(params)
        fig, axes = plt.subplots(n, 1, figsize=(10, 1.9 * n + 0.9), sharex=True, squeeze=False)
        labels = None
        try:
            labels = classify(ctx, None) if any(ctx.has(p) for p in ("throttle_position", "engine_load")) else None
        except Exception:
            labels = None
        wot = spans(labels, "load_state", "wot", 1.0) if labels is not None else []
        anomalies = _anomaly_marks(cat, row["id"])
        for ax, p, color in zip(axes[:, 0], params, PALETTE):
            sub = df.filter(pl.col(p).is_not_null())
            if sub.height:
                tt, vv = q.lttb_series(sub["t"].to_numpy(), sub[p].to_numpy().astype(float), MAX_POINTS)
                ax.plot(tt, vv, color=color, linewidth=1.0)
                shown[p] = {"min": float(np.nanmin(vv)), "max": float(np.nanmax(vv)), "n": int(sub.height)}
                for a, b, _, _ in wot:
                    if t1 is None or a <= t1:
                        ax.axvspan(a, b, color="#d9822b", alpha=0.12, linewidth=0)
                for m in anomalies.get(p, []):
                    if (t0 is None or m[0] >= t0) and (t1 is None or m[0] <= t1):
                        ax.axvline(m[0], color="#c0392b", alpha=0.5, linewidth=0.8, linestyle=":")
                for lim in limits_for(p):
                    ax.axhline(lim["limit"], color="#c0392b", linestyle="--", linewidth=0.8, alpha=0.7)
                    ax.text(0.995, lim["limit"], f" {lim['tier']} limit {lim['limit']:g}", transform=ax.get_yaxis_transform(), ha="right", va="bottom", fontsize=7, color="#c0392b")
            ax.set_ylabel(f"{p}{_unit(ctx, p)}", fontsize=8)
            _style(ax)
        axes[-1, 0].set_xlabel("time (s)")
        fig.suptitle(f"{row['file_name']}", fontsize=10, x=0.01, ha="left")
        caption = f"Time series of {', '.join(params)}; orange bands = wide-open-throttle/max-load spans, dotted red = detected anomalies, dashed = generic limits."
    elif kind == "scatter":
        al, _ = q.asof_align(df, params)
        if al.height < 3:
            raise ObdError("NO_DATA", "Not enough time-aligned samples for a scatter.")
        step = max(1, al.height // MAX_POINTS)
        fig, ax = plt.subplots(figsize=(6.5, 5))
        sc = ax.scatter(al[params[0]][::step], al[params[1]][::step], c=al["t"][::step], s=6, cmap="viridis")
        fig.colorbar(sc, label="time (s)")
        ax.set_xlabel(f"{params[0]}{_unit(ctx, params[0])}")
        ax.set_ylabel(f"{params[1]}{_unit(ctx, params[1])}")
        _style(ax)
        caption = f"{params[1]} vs {params[0]} on {al.height} time-aligned samples (colour = time)."
        shown = {"n": int(al.height)}
    elif kind == "histogram":
        x = df[params[0]].drop_nulls().to_numpy()
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(x, bins=min(60, max(10, int(np.sqrt(x.size)))), color=PALETTE[0], alpha=0.85)
        ax.set_xlabel(f"{params[0]}{_unit(ctx, params[0])}")
        ax.set_ylabel("samples")
        _style(ax)
        caption = f"Distribution of {params[0]} (n={x.size})."
        shown = {"n": int(x.size)}
    else:  # comparison: same parameter across datasets
        rows = [row, *(other_rows or [])]
        fig, ax = plt.subplots(figsize=(10, 4))
        for r, color in zip(rows, PALETTE):
            d = q.load(ws, r["id"], params, t0, t1).filter(pl.col(params[0]).is_not_null())
            if d.height:
                tt, vv = q.lttb_series(d["t"].to_numpy(), d[params[0]].to_numpy().astype(float), MAX_POINTS)
                ax.plot(tt, vv, color=color, linewidth=1.0, label=r["file_name"])
        ax.set_xlabel("time (s)")
        ax.set_ylabel(f"{params[0]}{_unit(ctx, params[0])}")
        ax.legend(fontsize=8)
        _style(ax)
        caption = f"{params[0]} across {len(rows)} dataset(s); time axes are each log's own seconds from start (not condition-matched)."
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return {"path": str(out), "relative_path": str(out.relative_to(ws.root)), "caption": caption, "kind": kind, "parameters": params, "shown": shown,
            "window_s": [t0, t1]}


def _anomaly_marks(cat: Catalog, dataset_id: str) -> dict[str, list[tuple[float]]]:
    for res in cat.latest_results(dataset_id):
        if res.get("module") == "anomalies":
            out: dict[str, list] = {}
            for e in res.get("metrics", {}).get("events", []):
                if e.get("type") == "spike":
                    out.setdefault(e["parameter"], []).append((e["t_start_s"],))
            return out
    return {}
