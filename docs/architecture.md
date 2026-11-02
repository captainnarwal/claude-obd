# Architecture (implementation notes)

The full design is in [../automotive-intelligence-design-doc.md](../automotive-intelligence-design-doc.md). This page records how v0.2 maps onto it and where it deliberately differs.

## Layout
```
.claude-plugin/        plugin.json, marketplace.json (plugin name obd-analyst; "claude-" names are reserved)
skills/                obd (router), automotive-data-analysis, vehicle-report-generation, communicating-findings,
                       engine-diagnostics, obd-parameter-interpretation, performance-ecu-log-analysis
hooks/                 hooks.json + scripts/hooks/read_guard.py (non-blocking Read-size steer)
scripts/obd(.py)       python launcher (obd = sh wrapper; falls back to uv when no Python): venv lookup, version check, SETUP_REQUIRED envelope (exit 4)
scripts/setup/         setup.sh (uv sync, selftest, setup.json; uv is required and installed on request), doctor.sh
engine/src/obd_engine/
  cli/                 envelope.py (JSON envelope, budget thinning), commands.py, analysis_commands.py, vehicle_commands.py,
                       lookup_commands.py, main.py (execute() is shared by the CLI and the MCP server)
  mcp/                 schema.py (tools derived from the argparse registry), server.py (stdio, official SDK)
  security/            path/size/zip-bomb/macro checks
  ingestion/           discovery -> adapters -> mapping -> normalize; service.py wires inspect/ingest/remap
  storage/             workspace (.obd), catalog (SQLite + migrations)
  query/               DuckDB windowed loads, LTTB, bucket downsampling, as-of alignment, block thinning
  analysis/            registry (module contract, orchestrator, cache, post-pass), conditions, generic/, modules/ (data quality +
                       fuel, thermal, ignition, engine performance, intake/boost, drivetrain), diagnostics/ (rule engine),
                       thresholds.py (vehicle > engine-family > generic), compare.py
  charts/  reports/    Matplotlib PNG, report model, ReportLab PDF, XlsxWriter Excel (both render the same model)
  reference/           dictionary loader, lookup (PIDs, parameters, DTC structure)
reference/             parameters.yaml, obd_pids.yaml, dtc_structure.yaml, thresholds/, diagnostic_rules/ (data, not code)
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

## Phase 2 additions
- **Vehicles and sessions** live in the per-folder catalog (migration 2); the engine profile and drivetrain are JSON columns. The global cross-folder vehicle library is not built.
- **Threshold tiers**: `limits_for(param, vehicle)` returns vehicle > engine-family > generic from `reference/thresholds/*.yaml`; the vehicle's threshold profile is part of every analysis cache key.
- **System modules** report per operating condition and never name a cause. `diagnostics` is a *post-pass* module: the orchestrator runs it after the others, hands it their results, and does not cache it. Requesting it alone also runs the modules it needs.
- **Rule base**: YAML rules match wildcard keys over the modules' flattened metrics; hypotheses are ranked by prior weight + favoring - against evidence. Confidence is capped at medium because rule thresholds are generic. DTC support is structure plus paraphrased topics; no copyrighted description text ships.
- **MCP**: tools are generated from the argparse registry (`obd_<command>`), run through `execute()`, and return the same envelope. The workspace and readable folders are fixed at server start (`--workspace`, `--allow-dir`, enforced by `OBD_ALLOWED_DIRS`). See `docs/adr/0001-mcp-is-opt-in.md`.
- **Analyze output** is compact when several modules run (loud findings kept, info findings capped, shorter evidence); ask for one module to get full detail. Budget thinning never drops a whole module.

## Known gaps vs the design doc
- Torque Pro adapter built from documented format + synthetic fixtures, not real exports (open question Q2).
- 500 MB ingest memory: ~2.8 GB peak working set vs the 2 GB target (the streaming CSV->Parquet step retains ~1.2 GB; normalization adds the rest). Time is well within target (~10 s vs 120 s).
- Report PDF visual layout was verified by text extraction and structure tests, not by eye (no PDF rasterizer in the dev environment).
- Install tests for macOS/Linux exist as CI jobs only; they have not run locally (dev machine is Windows/Git Bash).
- Claude-in-the-loop evals exist as cases plus a transcript scorer (`tests/evals`) but are not run in CI; perf benchmarks run nightly, not on PRs.
- The MCP server has been tested over stdio with the SDK client here, not yet with Claude Desktop or on macOS/Linux.
- Diagnostic rules are a starter set of 13 with generic thresholds and no automotive-expert review yet; the lazy-O2 pattern needs a response-time analysis that is not built.
- No DTC *presence* data: `lookup dtc` decodes structure only; reading stored codes from a file is not implemented.
- Phase 3 (ECU logger adapters, advanced comparison, ML anomaly detection, engine-family knowledge packs) is not started.
