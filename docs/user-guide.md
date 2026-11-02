# User guide

## Install (once per machine)

Inside Claude Code:

```
/plugin marketplace add captainnarwal/claude-obd
/plugin install obd-analyst@captainnarwal-obd-tools
/obd setup
```

`/obd setup` creates an isolated Python environment in the plugin's data folder, installs the engine from a locked dependency set, and runs a self-test on a bundled sample log. If `uv` is not installed, Claude explains what it is and asks before installing it. With no `uv` but Python 3.11+, setup falls back to `venv` + `pip` (dependencies are then not hash-pinned).

`/obd doctor` checks uv, the environment, engine/plugin version agreement, the self-test and the `.obd/` workspace in the current folder, and prints a fix for anything that fails. After a plugin update, `doctor` (or any command returning `SETUP_REQUIRED`) tells you to re-run `/obd setup`.

## Everyday use

```
cd ~/car-logs
claude
```

Put exports in the folder, or give any path. Examples:

- "What's in this folder?" / `/obd scan`
- "Look at run1.csv" / `/obd analyze run1.csv`
- "Check my coolant temperature" - statistics, warm-up and limit checks
- "Show me RPM and throttle during the pulls" - chart with wide-open-throttle spans shaded
- "Anything weird in this log?" - anomalies
- `/obd report run1.csv` - PDF in `obd-reports/`
- `/obd clean` - removes `.obd/` (originals and reports untouched)

### Ambiguous columns
Columns the engine cannot interpret safely (`TEMP`, `MAP` without a unit, fuel trims without a bank, temperatures without °C/°F) are never guessed. Claude asks, you answer, and the data is re-normalized from the raw copy. A column you mark as `custom` stays available for queries and charts but is left out of automotive analysis.

### Findings
Each finding is tagged **observation** (measured), **inference** or **hypothesis**, with severity, confidence, the time range, rows and sample count behind it. Findings that rest on a generic threshold are capped at medium confidence and say so. If you describe your vehicle (`/obd vehicle`), engine-family limits (naturally aspirated / turbo petrol / diesel) replace the generic ones, and you can set your own limits for any parameter; each finding names the tier it used. This version produces no hypotheses yet.

### What the files are
```
~/car-logs/
├── run1.csv            your original, never modified
├── obd-reports/        charts/*.png and *.pdf
└── .obd/               Parquet + SQLite catalog + cache; safe to delete
```

## Supported inputs
CSV/TSV/TXT (any common delimiter and encoding, decimal comma, preamble lines, unit rows or units in headers) and XLSX (cached values only; macro-enabled files are refused; large workbooks should be exported to CSV). A Torque Pro adapter is included. Long-format logs (one reading per row: time, PID, value, optional unit) are pivoted to one column per PID automatically. Other apps work through the generic adapters.

## Limits and honesty
- Not a diagnosis. Analytical assistance, not a substitute for physical inspection.
- No live vehicle connection, no ECU writes, no cloud, no telemetry.
- Not in this version: fuel/thermal/ignition/boost system modules, DTC lookup, ranked hypotheses, session comparison, Excel export, Claude Desktop/MCP.
- Large logs (500 MB, about 3.4 M rows): ingest took ~10 s here, but peak memory was about 2.8 GB, above the 2 GB design target. Correlation uses evenly spaced blocks (about 150k rows) on very large logs and says so.

## Troubleshooting
| Symptom | What to do |
|---|---|
| `SETUP_REQUIRED` | run `/obd setup` |
| Setup fails downloading packages | check internet/proxy, re-run `/obd setup` |
| "File too large" for XLSX | export the sheet to CSV |
| Columns missing from the report | they await confirmation - answer Claude's questions or `map` them |
| Results look stale after editing the file | re-ingest with `--force`; the engine warns when the hash changed |
