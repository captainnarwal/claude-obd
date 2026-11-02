# Architecture (implementation notes)

The full design is in [../automotive-intelligence-design-doc.md](../automotive-intelligence-design-doc.md). This page records how v0.1 maps onto it and where it deliberately differs.

## Layout
```
.claude-plugin/        plugin.json, marketplace.json (plugin name obd-analyst; "claude-" names are reserved)
skills/                obd (router), automotive-data-analysis, vehicle-report-generation, communicating-findings
scripts/obd            bash launcher: venv lookup, version check, SETUP_REQUIRED envelope (exit 4)
scripts/setup/         setup.sh (uv or venv+pip, selftest, setup.json), doctor.sh
engine/src/obd_engine/
  cli/                 envelope.py (JSON envelope, budget thinning), commands.py, analysis_commands.py, main.py
  security/            path/size/zip-bomb/macro checks
  ingestion/           discovery -> adapters -> mapping -> normalize; service.py wires inspect/ingest/remap
  storage/             workspace (.obd), catalog (SQLite + migrations)
  query/               DuckDB windowed loads, LTTB, bucket downsampling, as-of alignment, block thinning
  analysis/            registry (module contract, orchestrator, cache), conditions, generic/, modules/
  charts/  reports/    Matplotlib PNG, report model, ReportLab PDF
reference/parameters.yaml   canonical parameter dictionary (data, not code)
tools/synthetic_log_generator   seeded drive cycles with injectable faults
```

## Data flow
`inspect` (sample, read-only) -> `ingest`: adapter `parse` streams the file into `raw.parquet` (original column names + `__row_id`; typed when inference succeeds, else text) -> mapping runs on the first 200k raw rows -> `normalize` builds `normalized.parquet` (`t`, `t_abs`, `segment_id`, canonical columns in canonical units, custom numeric columns, `<param>__q` flag columns only where samples were flagged). `map` re-runs the last two steps from the raw file. Only mappings with status `confirmed` produce canonical columns; everything else stays under its original name until confirmed.

## Decisions that differ from or refine the design doc
- **Plugin name** `obd-analyst` (Claude Code rejects third-party plugin names starting with `claude-`); repo and docs keep `claude-obd`.
- **Quality flags** are stored as a `UInt8` bitmask column per flagged parameter (1 = out of range, 2 = sentinel); `query`/analysis load them as nulls unless `--include-flagged`.
- **Duplicate rows** are dropped only when identical to the previous row with the same timestamp (the realistic scanner failure mode, and constant-memory).
- **Time resets** keep time monotonic (the step after a reset is the median interval) and start a new segment; large gaps (> max(20 x median, 10 s)) also start a segment.
- **Anomaly spikes** must be isolated (<= 4 samples), away from gaps and series edges; level shifts and ramps are not anomalies. Long series use a 3-point-median screen and compute the exact rolling robust z only near candidates (same results, ~100x cheaper).
- **Correlation** on > 150k rows uses evenly spaced contiguous blocks and states it.
- **Analysis cache** key: file hash, mapping version, module id + version, config, dictionary version, engine version. Bump a module's `version` when its logic changes.
- **Report** is built from stored module results, so it is reproducible and shares the cache with conversational `analyze`.

## Known gaps vs the design doc
- Torque Pro adapter built from documented format + synthetic fixtures, not real exports (open question Q2).
- 500 MB ingest memory: ~2.8 GB peak working set vs the 2 GB target (the streaming CSV->Parquet step retains ~1.2 GB; normalization adds the rest). Time is well within target (~10 s vs 120 s).
- Report PDF visual layout was verified by text extraction and structure tests, not by eye (no PDF rasterizer in the dev environment).
- Install tests for macOS/Linux exist as CI jobs only; they have not run locally (dev machine is Windows/Git Bash).
- Perf CI, Claude-in-the-loop evals, the Read-size hook, fuel/thermal modules, vehicles/sessions, Excel, MCP: Phase 2+.
