"""query / stats / correlate / anomalies / analyze / chart / report commands."""

from __future__ import annotations

import argparse
from typing import Any, Callable


from obd_engine.analysis.generic.correlation import correlate_pair
from obd_engine.analysis.generic.statistics import compute_stats, select_params
from obd_engine.analysis.registry.base import make_context, registry, run_modules
from obd_engine.cli.envelope import Envelope, ObdError, clean_text
from obd_engine.ingestion import service as ing
from obd_engine.query import service as q
from obd_engine.storage.catalog import Catalog
from obd_engine.storage.workspace import Workspace

MAX_FINDINGS_SHOWN = 12  # when a single module is requested
MAX_ATTENTION_SHOWN = 8  # warning/attention findings shown per module
MAX_INFO_MULTI = 2  # info findings shown per module when several modules run, so the answer fits the token budget


def _open(ns_dataset: str):
    ws = Workspace.current()
    if not ws.catalog_path.exists():
        raise ObdError("DATASET_NOT_FOUND", "Nothing has been ingested in this folder yet.", "Run `ingest <file>` first.")
    cat = Catalog(ws)
    return ws, cat, ing.resolve_dataset(cat, ns_dataset)


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _units(ctx_params: dict, names: list[str]) -> dict[str, str]:
    return {n: ctx_params.get(n, {}).get("unit") or "" for n in names}


# ---- query ----
def query_setup(sp):
    """Bounded excerpt of parameters over a time window."""
    sp.add_argument("dataset")
    sp.add_argument("--params", required=True)
    sp.add_argument("--t0", type=float)
    sp.add_argument("--t1", type=float)
    sp.add_argument("--max-points", type=int, default=200)
    sp.add_argument("--agg", choices=["mean", "min", "max", "median"], default="mean")
    sp.add_argument("--include-flagged", action="store_true")


def query_cmd(ns) -> Envelope:
    ws, cat, row = _open(ns.dataset)
    try:
        params = q.resolve_params(ws, row["id"], _csv(ns.params))
        maxp = max(1, min(ns.max_points, 1000))
        df = q.load(ws, row["id"], params, ns.t0, ns.t1, ns.include_flagged)
        total = df.height
        out = q.bucket(df, params, maxp, ns.agg)
        quality = cat.get_quality(row["id"]) or {}
        rows = [[round(float(r[0]), 3) if r[0] is not None else None] + [None if v is None else round(float(v), 3) for v in r[2:]]
                for r in out.select("t", "__row_id", *params).iter_rows()]
        tbl = {"columns": ["t", *params], "rows": rows, "row_ids": out["__row_id"].to_list()}
        cav = []
        if total > maxp:
            cav.append(f"Downsampled {total} rows to {out.height} time buckets ({ns.agg} per bucket).")
        if not ns.include_flagged:
            cav.append("Out-of-range and sentinel samples are excluded.")
        return Envelope(
            summary=f"{out.height} row(s) of {', '.join(params)}" + (f" between t={ns.t0} and t={ns.t1} s" if ns.t0 is not None or ns.t1 is not None else "") + ".",
            data=tbl, units={"t": "s", **_units(quality.get("parameters", {}), params)}, caveats=cav,
            method=f"column-pruned DuckDB read; {'bucketed ' + ns.agg if total > maxp else 'raw rows'}",
            evidence=[{"dataset_id": row["id"], "parameters": params, "rows_in_window": total}],
            truncated={"reason": "downsampled", "hint": "narrow --t0/--t1 for full resolution"} if total > maxp else None,
        )
    finally:
        cat.close()


# ---- stats ----
def stats_setup(sp):
    """Statistics per parameter, optionally by operating condition."""
    sp.add_argument("dataset")
    sp.add_argument("--params")
    sp.add_argument("--by-condition", action="store_true")


def stats_cmd(ns) -> Envelope:
    ws, cat, row = _open(ns.dataset)
    try:
        ctx = make_context(ws, cat, row)
        params = q.resolve_params(ws, row["id"], _csv(ns.params)) if ns.params else select_params(ctx)
        stats, cond = compute_stats(ctx, params, ns.by_condition)
        rounded = {p: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()} for p, s in stats.items()}
        return Envelope(
            summary=f"Statistics for {len(rounded)} parameter(s)" + (" split by operating condition." if ns.by_condition else "."),
            data={"stats": rounded, **({"by_condition": cond} if ns.by_condition else {})}, units=_units(ctx.params, list(rounded)),
            method="mean/std/percentiles over unflagged samples" + ("; condition = generic load-state classifier" if ns.by_condition else ""),
            caveats=["Out-of-range and sentinel samples excluded (counts in excluded_flagged)."],
            evidence=[{"dataset_id": row["id"], "parameters": list(rounded)}],
        )
    finally:
        cat.close()


# ---- correlate ----
def correlate_setup(sp):
    """Pairwise correlation of parameters on as-of aligned samples."""
    sp.add_argument("dataset")
    sp.add_argument("--params", required=True, help="two or more, comma separated")
    sp.add_argument("--max-lag", type=float, default=0.0, help="seconds")


def correlate_cmd(ns) -> Envelope:
    ws, cat, row = _open(ns.dataset)
    try:
        params = q.resolve_params(ws, row["id"], _csv(ns.params))
        if len(params) < 2:
            raise ObdError("USAGE", "Give at least two parameters.", exit_code=2)
        df, thinned = q.thin_blocks(q.load(ws, row["id"], params))
        from itertools import combinations

        pairs = [r for a, b in combinations(params, 2) if (r := correlate_pair(df, a, b, ns.max_lag))]
        return Envelope(
            status="ok" if pairs else "partial",
            summary=f"{len(pairs)} pair(s) with enough overlapping samples." if pairs else "Not enough time-aligned samples to correlate these parameters.",
            data={"pairs": pairs}, method="Pearson and Spearman on as-of aligned samples (staleness = 2x median interval, min 1 s)" + (f"; lag search +/-{ns.max_lag}s" if ns.max_lag else ""),
            caveats=["Correlation is not causation; load changes move many parameters together."]
            + (["Large dataset: evenly spaced contiguous blocks (about 150k rows) were used."] if thinned else []),
            evidence=[{"dataset_id": row["id"], "parameters": params}],
        )
    finally:
        cat.close()


# ---- analyze / anomalies ----
def _findings_view(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in results:
        fs = sorted(r.get("findings", []), key=lambda f: {"warning": 0, "attention": 1, "info": 2}[f["severity"]])
        if len(results) == 1:
            fs = fs[:MAX_FINDINGS_SHOWN]
        else:
            loud = [f for f in fs if f["severity"] != "info"][:MAX_ATTENTION_SHOWN]
            fs = loud + [f for f in fs if f["severity"] == "info"][:MAX_INFO_MULTI]
        shown = [{
            "title": clean_text(f["title"], 200), "kind": f["kind"], "severity": f["severity"], "confidence": f["confidence"]["level"],
            **({"confidence_reason": clean_text(f["confidence"]["reason"], 160)} if f["confidence"].get("reason") else {}),
            **({"alternatives": [clean_text(a, 120) for a in f["alternatives"][:2]]} if f.get("alternatives") else {}),
            "evidence": f["evidence"][:1 if len(results) > 1 else 2], **({"threshold": f["threshold"]} if f.get("threshold") else {}),
            **({"next_steps": f["next_steps"][:2 if len(results) == 1 else 1]} if f.get("next_steps") else {}),
        } for f in fs]
        out.append({"module": r["module"], "status": r["status"], "summary": r.get("summary"), "findings": shown,
                    "findings_total": len(r.get("findings", [])), "notes": r.get("notes", [])})
    return out


def analyze_setup(sp):
    """Run analysis modules (default: all applicable)."""
    sp.add_argument("dataset", nargs="?")
    sp.add_argument("--modules", default="auto")
    sp.add_argument("--list-modules", action="store_true")
    sp.add_argument("--no-cache", action="store_true")
    sp.add_argument("--detail", action="store_true", help="include metrics (can be large)")


def analyze_cmd(ns) -> Envelope:
    if ns.list_modules:
        reg = registry()
        return Envelope(summary=f"{len(reg)} module(s).", data={"modules": [
            {"id": m.id, "title": m.title, "version": m.version, "required": m.required_all or m.required_any} for m in reg.values()]})
    if not ns.dataset:
        raise ObdError("USAGE", "Give a dataset (or --list-modules).", exit_code=2)
    ws, cat, row = _open(ns.dataset)
    try:
        ids = None if ns.modules in ("auto", "all") else _csv(ns.modules)
        run = run_modules(ws, cat, row, ids, use_cache=not ns.no_cache)
        view = _findings_view(run["results"])
        n_f = sum(v["findings_total"] for v in view)
        warn = sum(1 for r in run["results"] for f in r.get("findings", []) if f["severity"] != "info")
        data: dict[str, Any] = {"run_id": run["run_id"], "modules": view, "skipped": run["skipped"]}
        if ns.detail:
            data["metrics"] = {r["module"]: r.get("metrics") for r in run["results"]}
        return Envelope(
            summary=f"Ran {len(run['results'])} module(s) on {row['file_name']}: {n_f} finding(s), {warn} needing attention; "
                    f"{len(run['skipped'])} skipped.", data=data,
            method="deterministic modules; results cached by (file hash, mapping version, module version, config)",
            evidence=[{"dataset_id": row["id"], "run_id": run["run_id"]}],
            caveats=[f"{run['cached']} module result(s) served from cache."] if run["cached"] else [],
        )
    finally:
        cat.close()


def anomalies_setup(sp):
    """Outliers, spikes, flatlines and impossible combinations."""
    sp.add_argument("dataset")
    sp.add_argument("--params")
    sp.add_argument("--sensitivity", choices=["low", "medium", "high"], default="medium")


def anomalies_cmd(ns) -> Envelope:
    ws, cat, row = _open(ns.dataset)
    try:
        params = q.resolve_params(ws, row["id"], _csv(ns.params)) if ns.params else None
        z = {"low": 8.0, "medium": 6.0, "high": 4.0}[ns.sensitivity]
        run = run_modules(ws, cat, row, ["anomalies"], {"anomalies": {"z_threshold": z, "params": params}})
        if not run["results"]:
            return Envelope(status="partial", summary="Anomaly detection not applicable.", data={"skipped": run["skipped"]})
        r = run["results"][0]
        ev = r["metrics"]["events"][:20]
        return Envelope(summary=r["summary"], data={"events": ev, "total_events": r["metrics"]["total_events"], "run_id": run["run_id"]},
                        method=" ".join(r.get("notes", [])), evidence=[{"dataset_id": row["id"], "run_id": run["run_id"]}],
                        truncated={"reason": "top 20 events by robust z"} if r["metrics"]["total_events"] > 20 else None)
    finally:
        cat.close()


# ---- chart ----
def chart_setup(sp):
    """Render a PNG chart into obd-reports/charts/."""
    sp.add_argument("dataset")
    sp.add_argument("--type", default="timeseries", choices=["timeseries", "scatter", "histogram", "comparison"])
    sp.add_argument("--params", required=True)
    sp.add_argument("--t0", type=float)
    sp.add_argument("--t1", type=float)
    sp.add_argument("--name")
    sp.add_argument("--compare", help="other datasets for --type comparison (comma separated)")


def chart_cmd(ns) -> Envelope:
    from obd_engine.charts.render import render_chart

    ws, cat, row = _open(ns.dataset)
    try:
        others = [ing.resolve_dataset(cat, d) for d in _csv(ns.compare)]
        res = render_chart(ws, cat, row, ns.type, _csv(ns.params), ns.t0, ns.t1, ns.name, others)
        return Envelope(summary=f"Chart written to {res['relative_path']}.", data=res, method="Matplotlib; series downsampled with LTTB to <= 5000 points",
                        evidence=[{"dataset_id": row["id"], "parameters": res["parameters"]}],
                        caveats=["Flagged (out-of-range / sentinel) samples are not plotted."])
    finally:
        cat.close()


# ---- report ----
def report_setup(sp):
    """Build a PDF report from the analysis of a dataset."""
    sp.add_argument("dataset", help="dataset id, file name, or 'all'")
    sp.add_argument("--title")
    sp.add_argument("--format", default="pdf", choices=["pdf"])
    sp.add_argument("--no-charts", action="store_true")


KEY_CHART_PARAMS = ["engine_speed", "vehicle_speed", "throttle_position", "ect", "engine_load", "map"]


def _report_one(ws, cat, row, title, charts_on) -> dict[str, Any]:
    import datetime as dt

    from obd_engine.charts.render import render_chart
    from obd_engine.reports.model import build_model
    from obd_engine.reports.pdf import render_pdf
    from obd_engine.storage.catalog import new_id

    run = run_modules(ws, cat, row, None)
    ctx = make_context(ws, cat, row)
    charts = []
    if charts_on:
        keys = [p for p in KEY_CHART_PARAMS if ctx.has(p)][:4]
        if keys:
            stem = row["file_name"].rsplit(".", 1)[0]
            charts.append(render_chart(ws, cat, row, "timeseries", keys, name=f"{stem}-overview".lower().replace(" ", "-"), overwrite_name=True))
    model = build_model(cat, row, title, charts, [run["run_id"]])
    ws.ensure_reports()
    stem = row["file_name"].rsplit(".", 1)[0].replace(" ", "-")
    out = ws.reports_dir / f"{stem}-report-{dt.date.today().isoformat()}.pdf"
    render_pdf(model, out)
    rid = new_id("rp")
    cat.add_report(rid, [row["id"]], [run["run_id"]], "pdf", str(out))
    n_find = len(model["summary_points"])
    return {"report_id": rid, "path": str(out), "relative_path": str(out.relative_to(ws.root)), "dataset_id": row["id"], "summary_findings": n_find,
            "charts": [c["relative_path"] for c in charts], "sections_skipped": [l for l in model["limitations"] if "not performed" in l]}


def report_cmd(ns) -> Envelope:
    ws = Workspace.current()
    if not ws.catalog_path.exists():
        raise ObdError("DATASET_NOT_FOUND", "Nothing has been ingested in this folder yet.", "Run `ingest <file>` first.")
    cat = Catalog(ws)
    try:
        rows = cat.list_datasets() if ns.dataset == "all" else [ing.resolve_dataset(cat, ns.dataset)]
        if not rows:
            raise ObdError("DATASET_NOT_FOUND", "No datasets to report on.")
        outs = [_report_one(ws, cat, r, ns.title, not ns.no_charts) for r in rows]
        pending = [r["file_name"] for r in rows if any(m["status"] == "needs_confirmation" for m in cat.get_mappings(r["id"]))]
        return Envelope(
            status="ok", summary=f"Wrote {len(outs)} PDF report(s): " + ", ".join(o["relative_path"] for o in outs) + ".", data={"reports": outs},
            caveats=([f"Unconfirmed column mappings exist for: {', '.join(pending)}; those columns are excluded from the report."] if pending else []),
            method="report built from cached deterministic module results; sections appear only when supported by the data",
        )
    finally:
        cat.close()


COMMANDS: dict[str, tuple[Callable, Callable[[argparse.Namespace], Envelope]]] = {
    "query": (query_setup, query_cmd),
    "stats": (stats_setup, stats_cmd),
    "correlate": (correlate_setup, correlate_cmd),
    "anomalies": (anomalies_setup, anomalies_cmd),
    "analyze": (analyze_setup, analyze_cmd),
    "chart": (chart_setup, chart_cmd),
    "report": (report_setup, report_cmd),
}
