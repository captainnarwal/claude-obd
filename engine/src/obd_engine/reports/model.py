"""Format-independent report model (design doc 11.1). Sections exist only when the data supports them."""

from __future__ import annotations

import datetime as dt
from typing import Any

from obd_engine import __version__
from obd_engine.analysis.modules.data_quality import USABLE_FOR
from obd_engine.analysis.thresholds import family_for
from obd_engine.reference.loader import load_dictionary
from obd_engine.storage.catalog import Catalog

SYSTEM_MODULES = ("fuel_system", "thermal", "ignition", "intake_boost", "engine_performance", "drivetrain")
SEV_ORDER = {"warning": 0, "attention": 1, "info": 2}
DISCLAIMER = ("This report is analytical assistance based solely on the supplied log. It is not a substitute for physical inspection; "
              "consult a qualified technician before acting on any finding, particularly for safety-critical systems.")


def _vehicle_block(v: dict[str, Any] | None) -> dict[str, Any]:
    if not v:
        return {"known": False, "make": "unknown", "model": "unknown", "year": "unknown", "engine": "unknown", "threshold_tier": "generic"}
    e = v.get("engine") or {}
    eng = " ".join(str(x) for x in (f"{e['displacement']:g} L" if e.get("displacement") else None, e.get("cylinders") and f"{e['cylinders']}-cyl",
                                    e.get("aspiration"), e.get("fuel")) if x) or "unknown"
    fam = family_for(e)
    tier = "vehicle-specific limits where set; " if v.get("limits") else ""
    tier += f"engine-family ({fam}) limits" if fam else "generic limits"
    return {"known": True, "nickname": v.get("nickname"), "make": v.get("make") or "unknown", "model": v.get("model") or "unknown",
            "year": v.get("year") or "unknown", "engine": eng, "threshold_tier": tier}


def build_model(cat: Catalog, row, title: str | None, charts: list[dict[str, Any]], run_ids: list[str] | None = None) -> dict[str, Any]:
    ds = row["id"]
    results = {r["module"]: r for r in cat.latest_results(ds)}
    q = cat.get_quality(ds) or {}
    maps = cat.get_mappings(ds)
    d = load_dictionary()
    ok = {k: v for k, v in results.items() if v.get("status") != "not_applicable"}

    findings = []
    for mod, r in ok.items():
        for f in r.get("findings", []):
            findings.append({**f, "module": mod})
    findings.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["module"] == "correlation"))
    exec_pick = [f for f in findings if f["module"] not in ("correlation",)]
    summary_points = (exec_pick[:6])

    quality = results.get("data_quality", {})
    usable = quality.get("metrics", {}).get("usable_for", {})
    recs: list[str] = []
    for f in findings:
        for s in f.get("next_steps", []):
            if s not in recs:
                recs.append(s)
    present = {m["canonical_id"] for m in maps if m["status"] == "confirmed" and m["canonical_id"]}
    log_next = []
    for group, pids in USABLE_FOR.items():
        miss = [p for p in pids if p not in present]
        if miss:
            log_next.append(f"{group}: log {', '.join(d.params[p].name for p in miss if p in d.params)}")

    limitations = [f"{mod}: not performed - {r.get('reason')}" for mod, r in results.items() if r.get("status") == "not_applicable"]
    limitations += [
        "Ranked diagnostic hypotheses are not part of this version; the system sections report measurements and condition-dependent behavior, not causes.",
        "Thresholds are generic sanity ranges, not specifications for your engine; findings that use them are capped at medium confidence.",
    ]
    low_conf = [m["original_name"] for m in maps if m["status"] == "needs_confirmation"]
    if low_conf:
        limitations.append(f"{len(low_conf)} column(s) were not confirmed and are excluded from analysis: {', '.join(low_conf[:8])}")
    time_note = (q.get("time", {}).get("spec") or {}).get("note")
    if time_note:
        limitations.append(time_note)

    stats = results.get("statistics", {}).get("metrics", {})
    return {
        "title": title or f"Vehicle Log Report - {row['file_name']}",
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "versions": {"engine": __version__, "dictionary": d.version},
        "dataset": {"id": ds, "file": row["file_name"], "adapter": row["adapter"], "rows": row["row_count"], "duration_s": row["duration_s"],
                    "file_hash": row["file_hash"][:16]},
        "vehicle": _vehicle_block(cat.vehicle_for_dataset(ds)),
        "summary_points": summary_points,
        "quality": {"findings": quality.get("findings", []), "parameters": quality.get("metrics", {}).get("parameters", []),
                    "time": quality.get("metrics", {}).get("time", {}), "usable_for": usable, "notes": quality.get("notes", [])},
        "stats": stats.get("stats", {}), "by_condition": stats.get("by_condition", {}),
        "performance": [f for f in results.get("time_series", {}).get("findings", [])],
        "systems": [{"module": m, "title": ok[m].get("title") or m, "summary": ok[m].get("summary"), "findings": ok[m].get("findings", []), "notes": ok[m].get("notes", [])}
                    for m in SYSTEM_MODULES if m in ok],
        "time_series": results.get("time_series", {}).get("metrics", {}),
        "anomalies": results.get("anomalies", {}).get("metrics", {}).get("events", [])[:15],
        "anomaly_findings": results.get("anomalies", {}).get("findings", []),
        "correlations": results.get("correlation", {}).get("metrics", {}).get("pairs", [])[:8],
        "diagnostics": [],  # Phase 2: ranked hypotheses
        "recommendations": recs[:10], "log_next_time": log_next,
        "limitations": limitations, "charts": charts,
        "mappings": [{k: m.get(k) for k in ("original_name", "canonical_id", "source_unit", "canonical_unit", "confidence", "method", "status", "confirmed_by")} for m in maps],
        "run_ids": run_ids or sorted({r.get("run_id") for r in results.values() if r.get("run_id")}),
        "module_versions": {m: r.get("version") for m, r in results.items()},
        "disclaimer": DISCLAIMER,
    }
