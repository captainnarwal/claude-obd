"""ReportLab PDF renderer for the report model (design doc 11.2). Pure Python, no system dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_SUBS = {"λ": "lambda", "≥": ">=", "≤": "<=", "→": "->", "×": "x", "–": "-", "—": "-", "…": "...", "’": "'", "“": '"', "”": '"'}
SEV_COLOR = {"warning": "#c0392b", "attention": "#d9822b", "info": "#5b6b7a"}
KIND_LABEL = {"observation": "OBSERVATION", "inference": "INFERENCE", "hypothesis": "HYPOTHESIS"}


def T(s: Any) -> str:
    """Escape for Paragraph and force into the base-14 font's latin-1 range."""
    s = "" if s is None else str(s)
    for k, v in _SUBS.items():
        s = s.replace(k, v)
    s = "".join(ch for ch in s if ch == "\n" or ch >= " ")
    return escape(s.encode("latin-1", "replace").decode("latin-1"))


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("Small", parent=ss["BodyText"], fontSize=8, leading=10))
    ss.add(ParagraphStyle("Cell", parent=ss["BodyText"], fontSize=8, leading=9.5))
    ss.add(ParagraphStyle("Cap", parent=ss["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#5b6b7a")))
    ss["Heading1"].fontSize = 15
    ss["Heading2"].fontSize = 12
    return ss


def _table(rows: list[list[Any]], widths: list[float] | None = None, head: bool = True) -> Table:
    ss = _styles()
    data = [[Paragraph(T(c), ss["Cell"]) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1 if head else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9d1d9")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    if head:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")))
    t.setStyle(TableStyle(style))
    return t


def _finding_block(f: dict[str, Any], ss) -> list:
    sev = f["severity"]
    head = (f'<font color="{SEV_COLOR[sev]}"><b>[{sev.upper()}]</b></font> <font size="7" color="#5b6b7a">{KIND_LABEL[f["kind"]]} | '
            f'confidence {T(f["confidence"]["level"])}</font><br/>{T(f["title"])}')
    parts = [Paragraph(head, ss["BodyText"])]
    detail = []
    if f.get("conditions"):
        detail.append(f"Conditions: {f['conditions']}")
    for e in f.get("evidence", [])[:2]:
        bits = [e.get("parameter") or ""]
        if e.get("time_range_s"):
            bits.append(f"t = {e['time_range_s'][0]}-{e['time_range_s'][1]} s")
        if e.get("row_span"):
            bits.append(f"rows {e['row_span'][0]}-{e['row_span'][1]}")
        if e.get("n") is not None:
            bits.append(f"n = {e['n']}")
        if e.get("statistic"):
            bits.append(f"{e['statistic']} = {e.get('value')} {e.get('unit') or ''}")
        detail.append("Evidence: " + ", ".join(b for b in bits if b))
    if f.get("threshold"):
        th = f["threshold"]
        detail.append(f"Threshold: {th['limit']} {th['unit']} ({th['tier']}: {th['source']})")
    if f["confidence"].get("reason"):
        detail.append(f"Confidence basis: {f['confidence']['reason']}")
    if f.get("alternatives"):
        detail.append("Alternative explanations: " + "; ".join(f["alternatives"]))
    if f.get("next_steps"):
        detail.append("Next steps: " + "; ".join(f["next_steps"]))
    if detail:
        parts.append(Paragraph("<br/>".join(T(x) for x in detail), ss["Small"]))
    parts.append(Spacer(1, 4))
    return [KeepTogether(parts)]


def render_pdf(model: dict[str, Any], out: Path) -> Path:
    ss = _styles()
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=model["title"], author="claude-obd")
    W = A4[0] - 36 * mm
    s: list = []
    ds = model["dataset"]

    # 1 cover
    s += [Spacer(1, 40), Paragraph(T(model["title"]), ss["Title"]), Spacer(1, 8),
          Paragraph(T(f"Source file: {ds['file']}  |  Generated: {model['generated']}"), ss["BodyText"]),
          Paragraph(T(f"Engine {model['versions']['engine']}  |  Parameter dictionary {model['versions']['dictionary']}  |  Dataset {ds['id']}"), ss["Small"]),
          Spacer(1, 10), Paragraph(T(model["disclaimer"]), ss["Small"]), Spacer(1, 14)]

    # 2 executive summary
    s.append(Paragraph("Executive summary", ss["Heading1"]))
    if model["summary_points"]:
        for f in model["summary_points"]:
            s += _finding_block(f, ss)
    else:
        s.append(Paragraph("No notable findings were produced from this log.", ss["BodyText"]))

    # 3 vehicle & session
    v = model["vehicle"]
    s += [Paragraph("Vehicle and session", ss["Heading1"]),
          _table([["Make", "Model", "Year", "Engine"], [T(str(v["make"])), T(str(v["model"])), T(str(v["year"])), T(str(v["engine"]))]], [W / 4] * 4),
          Paragraph(T("Thresholds used: " + v["threshold_tier"] + "." + ("" if v.get("known") else " Vehicle details were not provided and are shown as unknown.")), ss["Cap"])]

    # 4 dataset overview
    q = model["quality"]
    s.append(Paragraph("Dataset overview", ss["Heading1"]))
    s.append(_table([["File", "Adapter", "Rows", "Duration", "Segments", "Median interval"],
                     [ds["file"], ds["adapter"], ds["rows"], f"{ds['duration_s']:.0f} s", q["time"].get("segments", ""), f"{q['time'].get('median_interval_s', '')} s"]]))
    params = q["parameters"]
    if params:
        rows = [["Parameter", "Unit", "Samples", "Completeness", "Rate (Hz)", "Mapped"]]
        for p in params[:40]:
            rows.append([p["parameter"], p["unit"] or "", p["n"], f"{(p['completeness'] or 0) * 100:.0f}%", p["rate_hz"], "yes" if p["mapped"] else "custom"])
        s += [Spacer(1, 6), _table(rows, [W * .32, W * .1, W * .14, W * .16, W * .14, W * .14])]

    # 5 data quality
    s.append(Paragraph("Data quality assessment", ss["Heading1"]))
    for f in q["findings"]:
        s += _finding_block(f, ss)
    if not q["findings"]:
        s.append(Paragraph("No data-quality issues were detected.", ss["BodyText"]))
    for n in q["notes"]:
        s.append(Paragraph(T(n), ss["Small"]))
    if q["usable_for"]:
        s.append(Paragraph("<b>Supported by the available parameters:</b> " + T(", ".join(k for k, ok in q["usable_for"].items() if ok) or "none of the standard groups"), ss["Small"]))

    # 6 key parameter analysis
    if model["stats"]:
        s.append(Paragraph("Key parameter analysis", ss["Heading1"]))
        rows = [["Parameter", "Unit", "n", "Mean", "Std", "Min", "Median", "P95", "Max"]]
        for p, st in list(model["stats"].items())[:30]:
            f = lambda x: f"{x:.2f}" if isinstance(x, (int, float)) else x  # noqa: E731
            rows.append([p, st.get("unit") or "", st["n"], f(st["mean"]), f(st["std"]), f(st["min"]), f(st["median"]), f(st["p95"]), f(st["max"])])
        s.append(_table(rows))
        bc = model["by_condition"]
        if bc:
            rows = [["Parameter", "idle", "cruise", "moderate", "high load", "wot"]]
            for p, cs in bc.items():
                if cs:
                    rows.append([p] + [f"{cs[k]['mean']:.1f} (n={cs[k]['n']})" if k in cs else "-" for k in ("idle", "cruise", "moderate", "high_load", "wot")])
            if len(rows) > 1:
                s += [Spacer(1, 6), Paragraph("Mean by operating condition (generic classifier: idle &lt;1200 rpm and stopped, WOT &gt;=90% throttle/load)", ss["Small"]), _table(rows)]
    for c in model["charts"]:
        p = Path(c["path"])
        if p.exists():
            from PIL import Image as PILImage  # reportlab dependency

            w, h = PILImage.open(p).size
            width = min(W, 170 * mm)
            s += [Spacer(1, 6), KeepTogether([Image(str(p), width=width, height=width * h / w), Paragraph(T(c["caption"]), ss["Cap"])])]

    # 7 performance observations
    if model["performance"]:
        s.append(Paragraph("Performance and behavior observations", ss["Heading1"]))
        for f in model["performance"]:
            s += _finding_block(f, ss)

    # 7b system analysis (fuel, thermal, ignition, intake/boost, performance, drivetrain) - only modules the data supports
    if model.get("systems"):
        s.append(Paragraph("System analysis", ss["Heading1"]))
        for sysmod in model["systems"]:
            s.append(Paragraph(T(sysmod["title"]), ss["Heading2"]))
            for f in sysmod["findings"]:
                s += _finding_block(f, ss)
            if not sysmod["findings"]:
                s.append(Paragraph("No findings from this module.", ss["Small"]))
            for n in sysmod["notes"]:
                if not n.startswith("Conditions use"):
                    s.append(Paragraph(T(n), ss["Small"]))
        s.append(Paragraph("Condition labels use generic thresholds (idle below 1200 rpm and stopped, wide-open throttle at 90% throttle/load or more, warm at 80 C or more).", ss["Cap"]))

    # 8 anomalies
    if model["anomalies"]:
        s.append(Paragraph("Anomalies", ss["Heading1"]))
        rows = [["Parameter", "Type", "Time (s)", "Value", "Robust z"]]
        for e in model["anomalies"]:
            rows.append([e["parameter"], e["type"], f"{e.get('t_start_s')}-{e.get('t_end_s')}", f"{e.get('value', e.get('n', ''))} {e.get('unit') or ''}", e.get("robust_z", "")])
        s.append(_table(rows))
        for f in model["anomaly_findings"]:
            if f["severity"] != "info":
                s += _finding_block(f, ss)

    # 9 diagnostic findings
    s.append(Paragraph("Diagnostic findings", ss["Heading1"]))
    dg = model.get("diagnostics")
    if dg and dg["ranked"]:
        s.append(Paragraph(T(f"Pattern matches from {dg['rules_evaluated']} deterministic rules (rule base {dg['rules_version']}). These are ranked possibilities, not a diagnosis; "
                             "relative support is not a probability, and the rules use generic thresholds."), ss["Small"]))
        for item in dg["ranked"]:
            f = next((x for x in dg["findings"] if x["title"].startswith(item["title"])), None)
            if f:
                s += _finding_block(f, ss)
            rows = [["Possible explanation", "Relative support", "Evidence for / against"]]
            for h in item["ranked"]:
                rows.append([h["cause"], f"{h['score']:.2f}", "; ".join([*h["favoring"], *[f"against: {a}" for a in h["against"]]]) or "prior only"])
            s += [_table(rows, [W * .5, W * .14, W * .36]), Spacer(1, 4)]
            if item["tests"]:
                s.append(Paragraph("<b>Physical tests that would discriminate:</b> " + T("; ".join(item["tests"])), ss["Small"]))
            if item["missing_evidence"]:
                s.append(Paragraph("<b>Missing evidence:</b> " + T("; ".join(item["missing_evidence"])), ss["Small"]))
            s.append(Spacer(1, 8))
    else:
        s.append(Paragraph("No diagnostic patterns matched the measured behavior in this log." if dg else
                           "Diagnostic patterns were not evaluated: the log lacks the system data (fuel, thermal, ignition, intake/boost) they need.", ss["BodyText"]))

    # 10 recommendations
    s.append(Paragraph("Recommendations", ss["Heading1"]))
    if model["recommendations"]:
        for r in model["recommendations"]:
            s.append(Paragraph("- " + T(r), ss["BodyText"]))
    else:
        s.append(Paragraph("No specific follow-up tests are suggested by these findings.", ss["BodyText"]))
    if model["log_next_time"]:
        s.append(Paragraph("<b>To get more from future logs, record:</b>", ss["BodyText"]))
        for r in model["log_next_time"]:
            s.append(Paragraph("- " + T(r), ss["Small"]))

    # 11 limitations
    s.append(Paragraph("Limitations and confidence", ss["Heading1"]))
    for l in model["limitations"]:
        s.append(Paragraph("- " + T(l), ss["Small"]))

    # 12 appendix
    s += [PageBreak(), Paragraph("Appendix A - Parameter mappings", ss["Heading1"])]
    rows = [["Column (as in file)", "Maps to", "Source unit", "Confidence", "Method", "Status", "Confirmed by"]]
    for m in model["mappings"]:
        rows.append([m["original_name"], m["canonical_id"] or "-", m["source_unit"] or "", f"{m['confidence']:.2f}" if m["confidence"] is not None else "", m["method"], m["status"], m["confirmed_by"]])
    s.append(_table(rows, [W * .27, W * .17, W * .1, W * .1, W * .1, W * .15, W * .11]))
    s += [Paragraph("Appendix B - Methods and versions", ss["Heading1"]),
          Paragraph(T("Deterministic analysis modules; same input and configuration produce identical results. Module versions: " +
                      ", ".join(f"{k} {v}" for k, v in model["module_versions"].items())), ss["Small"]),
          Paragraph(T("Analysis run id(s): " + ", ".join(model["run_ids"])), ss["Small"]),
          Paragraph(T(f"Source file SHA-256 (first 16): {ds['file_hash']}"), ss["Small"])]
    doc.build(s, onFirstPage=_footer, onLaterPages=_footer)
    return out


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#5b6b7a"))
    canvas.drawString(18 * mm, 9 * mm, "claude-obd - analytical assistance only; not a substitute for physical inspection")
    canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()
