"""XlsxWriter renderer for the report model (design doc 11.3). Same model as the PDF, so the two never disagree.

Every text cell is written as a string (never a formula) and a leading = + - @ is escaped, because headers and values come
from untrusted files.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable

import xlsxwriter

from obd_engine.query import service as q
from obd_engine.storage.workspace import Workspace

MAX_DATA_ROWS = 20_000
MAX_DATA_COLS = 60
FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")
SHEET_NAMES = ["Summary", "Findings", "Recommendations", "Parameter Statistics", "Anomalies", "Correlations", "Data Quality", "Mappings",
               "Diagnostics", "Normalized Data", "Raw Data Reference"]


def safe_text(v: Any) -> str:
    """Escape spreadsheet-formula leaders; None -> empty. Long text is cut to Excel's 32,767-character cell limit."""
    s = "" if v is None else str(v)
    if s[:1] in FORMULA_LEADERS:
        s = "'" + s
    return s[:32_000]


class _Sheet:
    def __init__(self, wb: xlsxwriter.Workbook, name: str, fmts: dict[str, Any]):
        self.ws = wb.add_worksheet(name)
        self.f = fmts
        self.row = 0

    def put(self, r: int, c: int, v: Any, fmt: str | None = None) -> None:
        f = self.f.get(fmt) if fmt else None
        if isinstance(v, bool) or v is None:
            self.ws.write_string(r, c, safe_text(v), f)
        elif isinstance(v, (int, float)):
            if isinstance(v, float) and not math.isfinite(v):
                self.ws.write_blank(r, c, None, f)
            else:
                self.ws.write_number(r, c, v, f)
        else:
            self.ws.write_string(r, c, safe_text(v), f)

    def title(self, text: str) -> None:
        self.put(self.row, 0, text, "h1")
        self.row += 2

    def kv(self, k: str, v: Any) -> None:
        self.put(self.row, 0, k, "bold")
        self.put(self.row, 1, v, "wrap")
        self.row += 1

    def table(self, headers: list[str], rows: Iterable[list[Any]], widths: list[int] | None = None) -> int:
        for c, h in enumerate(headers):
            self.put(self.row, c, h, "head")
        r0, n = self.row + 1, 0
        for i, r in enumerate(rows):
            for c, v in enumerate(r):
                self.put(r0 + i, c, v, "wrap" if isinstance(v, str) and len(v) > 40 else None)
            n += 1
        if widths:
            for c, w in enumerate(widths):
                self.ws.set_column(c, c, w)
        self.ws.freeze_panes(self.row + 1, 0) if self.row == 0 else None
        self.row = r0 + n + 1
        return n

    def note(self, text: str) -> None:
        self.put(self.row, 0, text, "note")
        self.row += 1


def _ev(e: dict[str, Any]) -> str:
    bits = [str(e.get("parameter") or "")]
    if e.get("n") is not None:
        bits.append(f"n={e['n']}")
    if e.get("statistic"):
        bits.append(f"{e['statistic']}={e.get('value')}{(' ' + e['unit']) if e.get('unit') else ''}")
    if e.get("time_range_s"):
        bits.append(f"t={e['time_range_s'][0]}-{e['time_range_s'][1]} s")
    if e.get("row_span"):
        bits.append(f"rows {e['row_span'][0]}-{e['row_span'][1]}")
    return " ".join(b for b in bits if b)


def render_xlsx(model: dict[str, Any], out: Path, ws: Workspace | None = None, dataset_id: str | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = xlsxwriter.Workbook(str(out), {"nan_inf_to_errors": False, "strings_to_formulas": False, "strings_to_urls": False})
    fmts = {
        "h1": wb.add_format({"bold": True, "font_size": 14}), "bold": wb.add_format({"bold": True, "valign": "top"}),
        "head": wb.add_format({"bold": True, "bg_color": "#EEF2F6", "border": 1, "text_wrap": True, "valign": "top"}),
        "wrap": wb.add_format({"text_wrap": True, "valign": "top"}), "note": wb.add_format({"italic": True, "font_color": "#5B6B7A"}),
    }
    sheets = {n: _Sheet(wb, n, fmts) for n in SHEET_NAMES}
    ds, veh = model["dataset"], model["vehicle"]

    s = sheets["Summary"]
    s.ws.set_column(0, 0, 24)
    s.ws.set_column(1, 1, 110)
    s.title(model["title"])
    s.kv("Generated", model["generated"])
    s.kv("Source file", ds["file"])
    s.kv("Dataset", f"{ds['id']} - {ds['rows']} rows, {ds['duration_s']:.0f} s, adapter {ds['adapter']}")
    s.kv("Vehicle", f"{veh['make']} {veh['model']} {veh['year']}; engine: {veh['engine']}")
    s.kv("Thresholds used", veh["threshold_tier"])
    s.kv("Versions", f"engine {model['versions']['engine']}, dictionary {model['versions']['dictionary']}")
    s.kv("Analysis runs", ", ".join(model["run_ids"]))
    s.kv("Disclaimer", model["disclaimer"])
    s.row += 1
    s.put(s.row, 0, "Key findings", "h1")
    s.row += 1
    for f in model["summary_points"]:
        s.kv(f"{f['severity']} / {f['kind']}", f["title"])
    if not model["summary_points"]:
        s.kv("", "No notable findings were produced from this log.")

    s = sheets["Findings"]
    rows = []
    for f in model["all_findings"]:
        rows.append([f["module"], f["severity"], f["kind"], f["confidence"]["level"], f["confidence"].get("reason", ""), f["category"], f["title"], f.get("conditions", ""),
                     (f.get("threshold") or {}).get("tier", ""), "; ".join(f.get("alternatives", [])), "; ".join(f.get("missing_evidence", [])),
                     "; ".join(f.get("next_steps", [])), " | ".join(_ev(e) for e in f.get("evidence", [])[:3])])
    s.table(["Module", "Severity", "Kind", "Confidence", "Confidence reason", "Category", "Title", "Conditions", "Threshold tier", "Alternatives", "Missing evidence", "Next steps", "Evidence"],
            rows, [14, 10, 12, 10, 34, 10, 60, 26, 12, 44, 44, 44, 60])

    s = sheets["Recommendations"]
    s.ws.set_column(0, 0, 120)
    s.title("Recommended physical tests and checks")
    for r in model["recommendations"] or ["No specific follow-up tests are suggested by these findings."]:
        s.put(s.row, 0, r, "wrap")
        s.row += 1
    s.row += 1
    s.put(s.row, 0, "Parameters to record next time", "h1")
    s.row += 1
    for r in model["log_next_time"] or ["Nothing further is suggested."]:
        s.put(s.row, 0, r, "wrap")
        s.row += 1
    s.row += 1
    s.put(s.row, 0, "Limitations and confidence", "h1")
    s.row += 1
    for r in model["limitations"]:
        s.put(s.row, 0, r, "wrap")
        s.row += 1

    s = sheets["Parameter Statistics"]
    s.table(["Parameter", "Unit", "n", "Mean", "Std", "Min", "P5", "Median", "P95", "Max", "Flagged samples excluded"],
            [[p, st.get("unit"), st["n"], st["mean"], st["std"], st["min"], st["p5"], st["median"], st["p95"], st["max"], st.get("excluded_flagged")] for p, st in model["stats"].items()],
            [28, 8, 8, 12, 12, 12, 12, 12, 12, 12, 14])
    if model["by_condition"]:
        s.put(s.row, 0, "Mean by operating condition (generic classifier)", "h1")
        s.row += 1
        s.table(["Parameter", "Condition", "n", "Mean", "P5", "P95"],
                [[p, st_name, v["n"], v["mean"], v["p5"], v["p95"]] for p, cs in model["by_condition"].items() for st_name, v in cs.items()])

    s = sheets["Anomalies"]
    ev = model["anomalies"]
    cols = ["parameter", "type", "t_start_s", "t_end_s", "value", "unit", "robust_z", "n"]
    s.table([c.replace("_", " ") for c in cols], [[e.get(c) for c in cols] for e in ev], [24, 18, 12, 12, 12, 8, 10, 8])
    if not ev:
        s.note("No anomaly events.")

    s = sheets["Correlations"]
    pairs = model["correlations"]
    if pairs:
        cols = [k for k, v in pairs[0].items() if isinstance(v, (str, int, float))]
        s.table(cols, [[p.get(c) for c in cols] for p in pairs], [22] * len(cols))
    else:
        s.note("No correlation pairs were analysed.")

    s = sheets["Data Quality"]
    s.table(["Parameter", "Unit", "Samples", "Completeness", "Rate (Hz)", "Max gap (s)", "Dropouts", "Out of range", "Sentinels", "Parse failures", "Mapped"],
            [[p["parameter"], p["unit"], p["n"], p["completeness"], p["rate_hz"], p["max_gap_s"], p["dropouts"], p["out_of_range"], p["sentinels"], p["parse_failures"], "yes" if p["mapped"] else "custom"]
             for p in model["quality"]["parameters"]], [28, 8, 10, 12, 10, 12, 10, 12, 10, 12, 8])
    for n in model["quality"]["notes"]:
        s.note(n)

    s = sheets["Mappings"]
    s.table(["Column (as in file)", "Maps to", "Source unit", "Canonical unit", "Confidence", "Method", "Status", "Confirmed by"],
            [[m["original_name"], m["canonical_id"] or "-", m["source_unit"], m["canonical_unit"], m["confidence"], m["method"], m["status"], m["confirmed_by"]] for m in model["mappings"]],
            [36, 24, 12, 14, 10, 12, 18, 12])

    s = sheets["Diagnostics"]
    dg = model.get("diagnostics")
    if dg and dg["ranked"]:
        s.note(f"Ranked possibilities from {dg['rules_evaluated']} deterministic rules (rule base {dg['rules_version']}). Not a diagnosis; relative support is not a probability.")
        s.row += 1
        s.table(["Pattern", "Rank", "Possible explanation", "Relative support", "Evidence for", "Evidence against", "Pattern confidence"],
                [[item["title"], i + 1, h["cause"], h["score"], "; ".join(h["favoring"]), "; ".join(h["against"]), item["confidence"]] for item in dg["ranked"] for i, h in enumerate(item["ranked"])],
                [46, 6, 60, 12, 40, 40, 12])
        s.table(["Pattern", "Physical tests", "Missing evidence"], [[item["title"], "; ".join(item["tests"]), "; ".join(item["missing_evidence"])] for item in dg["ranked"]])
    else:
        s.note("No diagnostic patterns matched." if dg else "Diagnostic patterns were not evaluated: the log lacks the system data they need.")

    s = sheets["Normalized Data"]
    if ws is not None and dataset_id is not None:
        cols = q.available_columns(ws, dataset_id)[:MAX_DATA_COLS]
        df = q.load(ws, dataset_id, cols)
        total = df.height
        if total > MAX_DATA_ROWS:
            df = q.bucket(df, cols, MAX_DATA_ROWS, "mean")
            s.note(f"Downsampled from {total} to {df.height} rows (mean of each time bucket, ratio about {total / max(df.height, 1):.1f}:1). Flagged samples are blank. Full data stays in .obd/.")
        else:
            s.note(f"All {total} normalized rows. Flagged (out-of-range / sentinel) samples are blank.")
        s.row += 1
        s.table(["t (s)", *cols], ([r["t"], *[r[c] for c in cols]] for r in df.iter_rows(named=True)), [10] + [16] * len(cols))
    else:
        s.note("Normalized data is not available for this export.")

    s = sheets["Raw Data Reference"]
    s.ws.set_column(0, 0, 24)
    s.ws.set_column(1, 1, 90)
    s.title("Raw data reference")
    s.kv("File", ds["file"])
    s.kv("SHA-256 (first 16)", ds["file_hash"])
    s.kv("Rows", ds["rows"])
    s.kv("Duration (s)", ds["duration_s"])
    s.kv("Adapter", ds["adapter"])
    s.kv("Note", "The original file is not copied or modified. Raw rows are kept immutably in .obd/ as Parquet; this workbook holds the reference, not the rows.")
    wb.close()
    return out
