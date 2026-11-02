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

## What it does (v0.1)

- **Ingestion:** CSV/TSV/TXT and XLSX, Torque Pro adapter, encoding/delimiter/decimal/preamble detection, 50–500 MB files via streaming Parquet.
- **Mapping:** ~60 canonical parameters with a deterministic, explainable scorer. Ambiguous columns (`TEMP`, `MAP` without unit, fuel trims without a bank…) are never guessed; Claude asks you.
- **Normalization:** unit conversion to metric, sentinel and plausibility flags, gap/reset segmentation, no resampling.
- **Analysis:** data quality, statistics (also by operating condition), time-series behavior, correlation, anomaly detection (robust rolling statistics). Findings are tagged observation / inference / hypothesis with evidence references.
- **Output:** PNG charts and PDF reports containing only what the data supports.

Not yet: fuel/thermal/ignition system diagnosis, DTC lookup, ranked hypotheses, comparisons, Excel export, MCP server for other Claude apps (see the roadmap in the design doc).

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

Torque Pro support follows the documented export format and synthetic fixtures; it has not yet been validated against real exported files — contributions of anonymized samples are welcome.

## License

MIT
