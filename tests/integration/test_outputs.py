import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]


def _ingest(run, copy_log, key, name):
    copy_log(key, name)
    code, env = run("ingest", name)
    assert code == 0, env
    return env


def test_chart_writes_png(run, copy_log, ws):
    _ingest(run, copy_log, "faulty", "f.csv")
    for kind, params in [("timeseries", "engine_speed,ect"), ("scatter", "engine_speed,map"), ("histogram", "ect")]:
        code, env = run("chart", "f.csv", "--type", kind, "--params", params)
        assert code == 0, env
        p = Path(env["data"]["path"])
        assert p.exists() and p.parent == ws / "obd-reports" / "charts" and p.read_bytes()[:4] == b"\x89PNG"
        assert env["data"]["caption"]


def test_chart_validation_errors(run, copy_log):
    _ingest(run, copy_log, "clean", "c.csv")
    code, env = run("chart", "c.csv", "--type", "scatter", "--params", "ect")
    assert code == 2
    code, env = run("chart", "c.csv", "--params", "does_not_exist")
    assert env["error"]["code"] == "PARAMETER_NOT_AVAILABLE"


def test_pdf_report_sections(run, copy_log, ws):
    _ingest(run, copy_log, "faulty", "f.csv")
    code, env = run("report", "f.csv", "--title", "Test Report")
    assert code == 0, env
    pdf = ws / env["data"]["reports"][0]["relative_path"]
    text = "\n".join(p.extract_text() for p in PdfReader(pdf).pages)
    for section in ["Executive summary", "Data quality assessment", "Recommendations", "Limitations and confidence", "Appendix A", "Test Report"]:
        assert section in text, section
    assert "not a substitute for physical inspection" in text
    assert "ect exceeded 120" in text and "generic" in text
    assert "Engine RPM(rpm)" in text                         # mapping appendix
    assert "Performance and behavior observations" in text


def test_report_omits_unsupported_sections(run, ws):
    (ws / "tiny.csv").write_text("Time,Banana\n" + "\n".join(f"{i},{i%7}" for i in range(100)), encoding="utf-8")
    run("ingest", "tiny.csv")
    code, env = run("report", "tiny.csv", "--no-charts")
    assert code == 0, env
    text = "\n".join(p.extract_text() for p in PdfReader(ws / env["data"]["reports"][0]["relative_path"]).pages)
    assert "Anomalies" not in text                           # nothing to show -> no empty section
    assert "correlation: not performed" in text              # skipped analysis listed under limitations


def test_report_all(run, copy_log):
    _ingest(run, copy_log, "clean", "a.csv")
    _ingest(run, copy_log, "generic", "b.csv")
    code, env = run("report", "all", "--no-charts")
    assert code == 0 and len(env["data"]["reports"]) == 2


def test_selftest_command(ws, capsys):
    from obd_engine.cli.main import main

    code = main(["selftest", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert code == 0 and env["status"] == "ok", env
    assert [s["stage"] for s in env["data"]["stages"]] == ["dictionary", "inspect", "ingest", "analyze", "chart", "report"]


def _bash():
    gitbash = [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]
    for cand in [*gitbash, shutil.which("bash")]:
        if cand and Path(cand).exists() and "System32" not in cand:   # System32 bash.exe is the WSL launcher
            return cand
    return None


@pytest.mark.skipif(_bash() is None, reason="a POSIX bash (Git Bash on Windows) is required")
def test_launcher_setup_required_paths(tmp_path):
    launcher = (ROOT / "scripts" / "obd").as_posix()
    r = subprocess.run([_bash(), launcher, "list", "--json"], capture_output=True, text=True,
                       env={"PATH": __import__("os").environ["PATH"], "CLAUDE_PLUGIN_DATA": str(tmp_path / "none"), "CLAUDE_PLUGIN_ROOT": str(ROOT)})
    assert r.returncode == 4
    assert json.loads(r.stdout)["error"]["code"] == "SETUP_REQUIRED"
    # version mismatch
    data = tmp_path / "data"
    (data / "venv" / "Scripts").mkdir(parents=True)
    (data / "venv" / "bin").mkdir(parents=True)
    for p in (data / "venv" / "Scripts" / "python.exe", data / "venv" / "bin" / "python"):
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    (data / "setup.json").write_text('{"engine_version":"0.0.0"}')
    r = subprocess.run([_bash(), launcher, "list", "--json"], capture_output=True, text=True,
                       env={"PATH": __import__("os").environ["PATH"], "CLAUDE_PLUGIN_DATA": str(data), "CLAUDE_PLUGIN_ROOT": str(ROOT)})
    assert r.returncode == 4 and "does not match" in json.loads(r.stdout)["error"]["message"]
