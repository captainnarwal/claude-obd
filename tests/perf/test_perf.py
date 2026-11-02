"""Performance benchmarks (design doc 13). Skipped unless OBD_PERF=1 (nightly CI job); sizes via OBD_PERF_MB="10,100,500".

Targets scale linearly from the design's 500 MB figures (ingest < 120 s and < 2 GB RAM; analyze/stats/chart in seconds).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.synthetic_log_generator.generate import generate

pytestmark = pytest.mark.skipif(os.environ.get("OBD_PERF") != "1", reason="set OBD_PERF=1 to run benchmarks")
SIZES = [float(x) for x in os.environ.get("OBD_PERF_MB", "10,100").split(",") if x]


def _run(ws: Path, *args):
    env = {**os.environ, "OBD_WORKSPACE": str(ws)}
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "obd_engine", *map(str, args), "--json"], cwd=ws, env=env, capture_output=True, text=True, encoding="utf-8")
    dt = time.perf_counter() - t0
    out = json.loads(r.stdout.strip().splitlines()[-1])
    return dt, out


def _peak_child_rss_mb():
    try:
        import resource

        r = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return r / (1024 * 1024 if sys.platform == "darwin" else 1024)
    except ImportError:  # Windows: no cheap child-RSS measure
        return None


@pytest.mark.parametrize("mb", SIZES)
def test_ingest_and_analysis_scale(tmp_path, mb):
    # a dense 5 Hz "perf" log so every system module has data
    dur = mb * 1024 * 1024 / 105 / 5
    path = generate(tmp_path / "big.csv", dur, 5, "perf", {"lean", "knock", "boost_leak"}, seed=7)
    size_mb = path.stat().st_size / 1024**2

    t_inspect, env = _run(tmp_path, "inspect", path.name)
    assert env["status"] in ("ok", "needs_input")
    t_ingest, env = _run(tmp_path, "ingest", path.name)
    assert env["data"]["dataset_id"], env
    ds = env["data"]["dataset_id"]
    rss = _peak_child_rss_mb()
    t_stats, _ = _run(tmp_path, "stats", ds, "--params", "engine_speed,ect,ltft_b1")
    t_analyze, env = _run(tmp_path, "analyze", ds)
    assert env["status"] == "ok"
    t_cached, _ = _run(tmp_path, "analyze", ds)
    t_chart, _ = _run(tmp_path, "chart", ds, "--params", "engine_speed,boost")
    t_report, _ = _run(tmp_path, "report", ds, "--format", "both", "--no-charts")

    scale = size_mb / 500.0
    print(f"\n{size_mb:.0f} MB: inspect {t_inspect:.1f}s ingest {t_ingest:.1f}s stats {t_stats:.1f}s analyze {t_analyze:.1f}s "
          f"cached {t_cached:.1f}s chart {t_chart:.1f}s report {t_report:.1f}s peak-rss {rss}")
    assert t_inspect < 5 + 5 * scale
    assert t_ingest < 120 * scale + 10
    assert t_stats < 3 + 3 * scale
    assert t_chart < 5 + 5 * scale
    assert t_report < 60 * scale + 20
    if rss is not None:
        assert rss < 2048 * max(scale, 0.25) + 400
