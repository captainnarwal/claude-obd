# claude-obd — Automotive Intelligence for Claude Code

Point Claude Code at a folder of OBD-II scanner exports or ECU logs and get a conversational analysis, charts and a PDF report. Everything runs **locally**: a Python engine does the data work and hands Claude only compact, evidence-linked summaries, so a 300 MB log never floods the conversation and nothing is uploaded.

## Install

Inside Claude Code:

```
/plugin marketplace add captainnarwal/claude-obd
/plugin install obd-analyst@captainnarwal-obd-tools
/obd setup
```

`/obd setup` builds an isolated Python environment in the plugin's data folder (about 1–3 minutes, internet needed once) and runs a self-test on a bundled sample log. Nothing is installed globally and your PATH is untouched. `/obd doctor` re-checks the environment any time.

> The Claude plugin name is `obd-analyst` because third-party plugin names starting with `claude-` are reserved; the repository keeps its name.

## Use

```
cd ~/car-logs      # any folder with OBD/ECU exports
claude
```

Then talk naturally ("look at run1.csv", "check my fuel trims", "show RPM and boost during the pulls") or use the command:

| Command | What it does |
|---|---|
| `/obd scan` | lists telemetry-looking files in this folder |
| `/obd analyze run1.csv` | inspect → confirm ambiguous columns → ingest → quality check → findings → charts |
| `/obd report run1.csv` | PDF report in `obd-reports/` |
| `/obd clean` | deletes processed data in `.obd/` (your originals are never touched) |
| `/obd setup`, `/obd doctor` | install / health check |

Your folder ends up with `obd-reports/` (charts, PDFs) and a hidden `.obd/` workspace (Parquet + SQLite; safe to delete).

### Fewer permission prompts
Each engine call is a Bash command, so Claude Code asks before the first one. To approve only the launcher and setup scripts, add this to `.claude/settings.json` (project) or `~/.claude/settings.json` (user):

```json
{ "permissions": { "allow": ["Bash(*/scripts/obd:*)", "Bash(*/scripts/setup/setup.sh:*)", "Bash(*/scripts/setup/doctor.sh:*)"] } }
```

(The skills do not use the `allowed-tools` frontmatter: Claude Code 2.1.316 failed to load plugin skills that carry it.)

## What it does

- **Ingestion:** CSV/TSV/TXT and XLSX, Torque Pro adapter, long-format (one reading per row) logs, encoding/delimiter/decimal/preamble detection, 50–500 MB files via streaming Parquet.
- **Mapping:** ~65 canonical parameters (bank/sensor aware) with a deterministic, explainable scorer. Ambiguous columns (`TEMP`, `MAP` without unit, fuel trims without a bank…) are never guessed; Claude asks you.
- **Normalization:** unit conversion to metric, sentinel and plausibility flags, gap/reset segmentation, no resampling.
- **Analysis:** data quality, statistics (also by operating condition), time-series behavior, correlation, anomaly detection (robust rolling statistics), plus fuel-system, thermal, ignition/knock, intake/boost, performance and drivetrain modules that report behavior per operating condition. Vehicle profiles (`/obd vehicle`) select engine-family or your own thresholds. Findings are tagged observation / inference / hypothesis with evidence references.
- **Output:** PNG charts and PDF reports containing only what the data supports.

- **Reference & diagnostics:** `lookup` explains parameters, Mode 01 PIDs and trouble-code structure (no copyrighted code descriptions); a deterministic rule base (13 patterns) turns module findings into ranked hypotheses with alternatives, missing evidence and physical tests. It never says a part is faulty.

- **Excel & compare:** `report --format xlsx|both` writes an Excel workbook from the same report model (formula-injection safe); `compare` gives condition-matched differences between runs with 95% intervals and comparability warnings.
- **Read guard:** a plugin hook nudges Claude to use the engine instead of reading big telemetry files directly.

- **MCP server:** `scripts/obd mcp` exposes every command as a tool for Claude Desktop and other MCP clients (opt-in; see the user guide and `docs/adr/0001-mcp-is-opt-in.md`).

Not yet: ECU-logger formats, ML anomaly detection (see the roadmap in the design doc).

*Analytical assistance only — not a substitute for physical inspection by a qualified technician.*

## Development

```
cd engine
uv sync
uv run pytest                                   # whole suite (tests live in ../tests)
uv run python -m obd_engine selftest            # full pipeline on samples/sample-drive.csv
cd .. && PYTHONPATH=. engine/.venv/Scripts/python -m tools.synthetic_log_generator.generate out.csv --faults lean,spike --duration 1200
```

The engine CLI is `scripts/obd <command> --json` (see `skills/automotive-data-analysis/references/commands.md`). Design: [automotive-intelligence-design-doc.md](automotive-intelligence-design-doc.md). User guide: [docs/user-guide.md](docs/user-guide.md).

App support status:

| App | Status |
|---|---|
| Car Scanner ELM OBD2 (wide CSV) | Validated on real exports |
| Car Scanner (long `SECONDS/PID/VALUE/UNITS` style) | Synthetic fixtures, header names from forum reports |
| Torque Pro | Documented format and synthetic fixtures |
| BlueDriver, OBDLink app | Documented format traits (UTF-16, preamble, `Time(s)`; elapsed seconds or date-time); synthetic fixtures |
| OBD Fusion, OBDwiz, OBD Auto Doctor, Dash Command | Best effort through the generic adapter; exact headers not documented online |

Anonymized sample exports from any of these apps are welcome.

## License

MIT
