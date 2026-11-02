"""M9: Excel report and condition-matched comparison."""

import openpyxl

from obd_engine.reports.excel import SHEET_NAMES, safe_text


def _ingest(run, copy_log, key, name=None):
    p = copy_log(key, name)
    return run("ingest", p.name)[1]["data"]["dataset_id"]


def test_safe_text_escapes_formula_leaders():
    for bad in ("=1+1", "+cmd", "-2+3", "@SUM(A1)", "\t=x"):
        assert safe_text(bad).startswith("'")
    assert safe_text("normal") == "normal" and safe_text(None) == "" and safe_text(12) == "12"


def test_excel_report_has_all_sheets_and_matches_findings(ws, run, copy_log):
    ds = _ingest(run, copy_log, "perf_faulty")
    code, env = run("report", ds, "--format", "both", "--no-charts")
    assert code == 0
    files = env["data"]["reports"][0]["files"]
    assert set(files) == {"pdf", "xlsx"}
    wb = openpyxl.load_workbook(ws / files["xlsx"], read_only=False)
    assert wb.sheetnames == SHEET_NAMES
    findings = list(wb["Findings"].iter_rows(min_row=2, values_only=True))
    assert any("knock-retard" in (r[6] or "") for r in findings)
    assert {r[2] for r in findings} >= {"observation", "hypothesis"}
    diag = " ".join(str(c) for r in wb["Diagnostics"].iter_rows(values_only=True) for c in r if c)
    assert "Possible explanation" in diag and "not a diagnosis" in diag.lower()
    stats = list(wb["Parameter Statistics"].iter_rows(min_row=2, values_only=True))
    assert stats and isinstance(stats[0][3], float)  # numbers stay numeric
    data = list(wb["Normalized Data"].iter_rows(values_only=True))
    assert len(data) > 3000 and "All 3500 normalized rows" in str(data[0][0])
    ref = " ".join(str(c) for r in wb["Raw Data Reference"].iter_rows(values_only=True) for c in r if c)
    assert "SHA-256" in ref


def test_pdf_only_is_still_the_default(ws, run, copy_log):
    ds = _ingest(run, copy_log, "perf_clean")
    code, env = run("report", ds, "--no-charts")
    assert code == 0 and set(env["data"]["reports"][0]["files"]) == {"pdf"}


def test_formula_injection_in_headers_and_values_is_neutralised(ws, run):
    rows = ["Time (s),Engine RPM,Vehicle Speed (km/h),=HYPERLINK(\"http://evil\"),+cmd|calc"]
    rows += [f"{i * 0.5},{900 + (i % 7)},{(i % 40)},{i},{i % 5}" for i in range(400)]
    (ws / "evil.csv").write_text("\n".join(rows), encoding="utf-8")
    code, env = run("ingest", "evil.csv", "--set", "=HYPERLINK(\"http://evil\")=custom", "--set", "+cmd|calc=custom")
    ds = env["data"]["dataset_id"]
    code, env = run("report", ds, "--format", "xlsx", "--no-charts")
    assert code == 0
    wb = openpyxl.load_workbook(ws / env["data"]["reports"][0]["files"]["xlsx"])
    seen = 0
    for sheet in wb:
        for row in sheet.iter_rows():
            for c in row:
                assert c.data_type != "f", (sheet.title, c.coordinate, c.value)  # nothing is stored as a formula
                if isinstance(c.value, str) and ("HYPERLINK" in c.value or "cmd|calc" in c.value):
                    seen += 1
                    assert c.value.startswith("'") or not c.value.startswith(("=", "+", "-", "@")), c.value
    assert seen >= 2


def test_compare_finds_condition_matched_differences(ws, run, copy_log):
    a = _ingest(run, copy_log, "perf_clean")
    b = _ingest(run, copy_log, "perf_faulty")
    code, env = run("compare", a, b, "--params", "ltft_b2,ect")
    assert code == 0
    c = env["data"]["comparisons"][0]
    d = next(x for x in c["deltas"] if x["parameter"] == "ltft_b2" and x["condition"] == "idle")
    assert d["clear"] and d["delta"] > 10 and d["baseline"]["n"] >= 20
    assert any("different vehicles" in w or "Session conditions" in w for w in c["warnings"])
    assert any("autocorrelated" in n for n in env["data"]["notes"])


def test_compare_warns_when_vehicles_differ_and_validates_input(ws, run, copy_log):
    a = _ingest(run, copy_log, "perf_clean")
    b = _ingest(run, copy_log, "perf_faulty")
    run("vehicle", "add", "--nickname", "one")
    run("vehicle", "assign", "one", a)
    code, env = run("compare", a, b, "--params", "ect")
    assert any("different vehicles" in w for w in env["data"]["comparisons"][0]["warnings"])
    assert run("compare", a)[0] == 2
    assert run("compare", a, "nope")[0] != 0
