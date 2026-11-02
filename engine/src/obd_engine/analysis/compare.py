"""Condition-matched comparison of two or more datasets (design doc 8.1 `compare`, 3 basic).

Each run is compared with the first (the baseline) parameter by parameter, inside the same operating condition and
warm-engine state, so a difference in how the runs were driven is not mistaken for a difference in the car.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from obd_engine.analysis.generic.statistics import SKIP
from obd_engine.analysis.modules._common import CONDITION_NOTE, STATE_TEXT, labelled
from obd_engine.analysis.registry.base import make_context

MIN_N = 20
MAX_PARAMS = 24
MAX_DELTAS = 25
STATES = ("idle", "cruise", "moderate", "high_load", "wot")
CI_NOTE = ("Uncertainty is a 95% interval on the difference of means from the samples' spread (1.96 x standard error). Samples in a log are "
           "autocorrelated, so the true uncertainty is larger; treat 'clear' as 'worth checking', not as proof.")


def _cond_stats(ctx, params: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    df = labelled(ctx, params)
    out: dict[str, dict[str, dict[str, float]]] = {}
    warm = df.filter(df["thermal_state"].is_in(["warm", "unknown"]))
    for p in params:
        out[p] = {}
        for st in STATES:
            x = warm.filter((warm["load_state"] == st) & warm[p].is_not_null())[p].to_numpy()
            x = x[np.isfinite(x)]
            if x.size >= MIN_N:
                out[p][st] = {"n": int(x.size), "mean": float(x.mean()), "se": float(x.std(ddof=1) / np.sqrt(x.size))}
    return out


def _warnings(a, b, ca, cb) -> list[str]:
    w = []
    if a["vehicle_id"] != b["vehicle_id"]:
        w.append("The runs are assigned to different vehicles (or one is unassigned); differences may reflect the vehicles, not a change.")
    sa, sb = a["session_id"] and ca.cat.conn.execute("SELECT conditions_json FROM sessions WHERE id=?", (a["session_id"],)).fetchone(), \
        b["session_id"] and cb.cat.conn.execute("SELECT conditions_json FROM sessions WHERE id=?", (b["session_id"],)).fetchone()
    if not sa or not sb:
        w.append("Session conditions (ambient temperature, fuel, modifications) are not recorded for both runs, so they cannot be matched.")
    if a["duration_s"] and b["duration_s"] and max(a["duration_s"], b["duration_s"]) > 3 * min(a["duration_s"], b["duration_s"]):
        w.append("The runs differ a lot in length; fewer samples mean wider uncertainty.")
    return w


def compare(ws, cat, rows: list, params: list[str] | None = None) -> dict[str, Any]:
    base = rows[0]
    cb = make_context(ws, cat, base)
    base_params = {p for p in cb.mapped() if p not in SKIP}
    out_cmp, skipped = [], []
    for other in rows[1:]:
        co = make_context(ws, cat, other)
        common = [p for p in (params or sorted(base_params)) if p in base_params and co.has(p) and p not in SKIP][: (len(params) if params else MAX_PARAMS)]
        missing = [p for p in (params or []) if p not in common]
        if not common:
            skipped.append({"dataset": other["id"], "reason": "no parameters in common"})
            continue
        sa, sb = _cond_stats(cb, common), _cond_stats(co, common)
        deltas = []
        for p in common:
            for st in STATES:
                if st in sa[p] and st in sb[p]:
                    a, b = sa[p][st], sb[p][st]
                    d = b["mean"] - a["mean"]
                    ci = 1.96 * float(np.hypot(a["se"], b["se"]))
                    deltas.append({"parameter": p, "unit": cb.params[p].get("unit"), "condition": st, "baseline": {"n": a["n"], "mean": round(a["mean"], 3)},
                                   "other": {"n": b["n"], "mean": round(b["mean"], 3)}, "delta": round(d, 3), "ci95": round(ci, 3), "clear": bool(abs(d) > ci > 0),
                                   "z": round(abs(d) / ci, 2) if ci > 0 else None})
        deltas.sort(key=lambda x: (-int(x["clear"]), -(x["z"] or 0)))
        w = _warnings(base, other, cb, co)
        unmatched = [p for p in common if not (sa[p] and sb[p] and set(sa[p]) & set(sb[p]))]
        if unmatched:
            w.append("No operating condition with enough samples in both runs for: " + ", ".join(unmatched[:8]) + ".")
        if missing:
            w.append("Not available in both runs: " + ", ".join(missing) + ".")
        out_cmp.append({"dataset": other["id"], "file": other["file_name"], "warnings": w, "compared": len(deltas),
                        "clear_differences": sum(d["clear"] for d in deltas), "deltas": deltas[:MAX_DELTAS],
                        "truncated": len(deltas) > MAX_DELTAS})
    return {"baseline": {"dataset": base["id"], "file": base["file_name"]}, "comparisons": out_cmp, "skipped": skipped,
            "notes": [CONDITION_NOTE, "Only warm-engine samples are compared, within the same load state.", CI_NOTE,
                      "States: " + ", ".join(f"{k} = {v}" for k, v in STATE_TEXT.items() if k in STATES)]}
