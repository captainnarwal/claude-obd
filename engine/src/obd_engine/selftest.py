"""`selftest`: run the whole pipeline on the bundled sample in a throwaway workspace (used by setup/doctor)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from obd_engine import __version__
from obd_engine.cli.envelope import Envelope, ObdError
from obd_engine.reference.loader import load_dictionary


def sample_path() -> Path:
    env = os.environ.get("OBD_SAMPLE")
    here = Path(__file__).resolve()
    cands = ([Path(env)] if env else []) + [here.parent / "_samples" / "sample-drive.csv"] + [p / "samples" / "sample-drive.csv" for p in here.parents]
    for c in cands:
        if c.exists():
            return c
    raise ObdError("SAMPLE_MISSING", "Bundled sample-drive.csv not found.", "Reinstall the plugin.", exit_code=4)


def run_selftest() -> Envelope:
    from obd_engine.analysis.registry.base import run_modules
    from obd_engine.charts.render import render_chart
    from obd_engine.ingestion import service as ing
    from obd_engine.reports.model import build_model
    from obd_engine.reports.pdf import render_pdf
    from obd_engine.storage.catalog import Catalog
    from obd_engine.storage.workspace import Workspace

    stages: list[dict] = []
    tmp = Path(tempfile.mkdtemp(prefix="obd-selftest-"))
    sample = sample_path()
    work = tmp / sample.name
    shutil.copy(sample, work)
    ws = Workspace(tmp)
    cat = None
    ctx_state: dict = {}

    def stage(name, fn):
        t0 = time.time()
        try:
            detail = fn()
            stages.append({"stage": name, "ok": True, "seconds": round(time.time() - t0, 2), "detail": detail})
        except Exception as e:  # report, don't crash
            stages.append({"stage": name, "ok": False, "seconds": round(time.time() - t0, 2), "error": f"{type(e).__name__}: {str(e)[:200]}"})
            raise

    try:
        stage("dictionary", lambda: f"{len(load_dictionary().params)} parameters")
        stage("inspect", lambda: ing.inspect_file(ws, str(work)).summary)

        def _ingest():
            nonlocal cat
            cat = Catalog(ws)
            env = ing.ingest_file(ws, cat, str(work))
            ctx_state["row"] = cat.get_dataset(env.data["dataset_id"])
            return env.summary

        stage("ingest", _ingest)
        row = ctx_state["row"]
        stage("analyze", lambda: f"{len(run_modules(ws, cat, row, None)['results'])} module(s)")
        stage("chart", lambda: render_chart(ws, cat, row, "timeseries", ["engine_speed", "vehicle_speed"])["relative_path"])

        def _pdf():
            model = build_model(cat, row, "Selftest", [])
            ws.ensure_reports()
            out = render_pdf(model, ws.reports_dir / "selftest.pdf")
            assert out.stat().st_size > 2000, "PDF suspiciously small"
            return f"{out.stat().st_size} bytes"

        stage("report", _pdf)
        ok = True
    except Exception:
        ok = False
    finally:
        if cat:
            cat.close()
        shutil.rmtree(tmp, ignore_errors=True)
    versions = {"engine": __version__, "python": sys.version.split()[0], "dictionary": load_dictionary().version}
    try:
        import duckdb, matplotlib, polars, reportlab  # noqa: E401

        versions.update(polars=polars.__version__, duckdb=duckdb.__version__, matplotlib=matplotlib.__version__, reportlab=reportlab.Version)
    except Exception:
        pass
    env = Envelope(status="ok" if ok else "error", summary="Selftest passed." if ok else "Selftest failed at stage " + next(s["stage"] for s in stages if not s["ok"]),
                   data={"stages": stages}, versions=versions)
    if not ok:
        env.error = {"code": "SELFTEST_FAILED", "message": env.summary, "hint": "Run /obd doctor; see stage detail."}
    return env
