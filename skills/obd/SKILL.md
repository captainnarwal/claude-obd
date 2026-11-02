---
name: obd
description: Entry point for the claude-obd plugin. Use for "/obd" commands - setup, doctor, scan, analyze, report, clean - and whenever the user asks to set up or check the OBD analysis engine, list log files in a folder, or build a report from a vehicle log.
argument-hint: setup | doctor | scan [folder] | analyze <file|all> | report <file|all> | clean [file|all]
---

# /obd - command router

You run an analysis engine on the user's machine. Every engine call is:

```
"${CLAUDE_PLUGIN_ROOT}/scripts/obd" <command> [arguments] --json
```

It prints one JSON envelope (`status`, `summary`, `data`, `caveats`, `needs_input`, ...). Read `summary` and `caveats` first. Exit code 4 or an error code `SETUP_REQUIRED` means the environment is missing: tell the user to run `/obd setup`.

Follow the rules in the `automotive-data-analysis` skill for everything about reading data, and the `communicating-findings` skill for how to word results. Never open telemetry files yourself (no Read/cat/head, no ad-hoc pandas): always go through the engine.

With no argument, show this list in one short block and run `scan` on the current folder.

## Subcommands

### `/obd setup`
1. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/setup/setup.sh"` (takes 1-3 minutes; needs internet once).
2. If the result has error code `NEEDS_UV`, explain that `uv` (a Python environment manager from astral.sh) is needed and nothing is installed globally; **ask the user for permission**, then run the same script with `--install-uv`.
3. On `SETUP_FAILED`, relay the `hint` (usually network/proxy) and offer to retry.
4. On success, print the `summary` (it includes the next step).

### `/obd doctor`
Run `"${CLAUDE_PLUGIN_ROOT}/scripts/setup/doctor.sh"`. List each failed check with its `fix`. If everything is ok, say so in one line.

### `/obd scan [folder]`
Run `scan [folder]` (add `--recursive` if asked). Show the files (name, size, whether already processed). If none, say where to put exports (this folder) or that any path can be given.

### `/obd analyze <file|all>`
Run the full first-look workflow from `automotive-data-analysis` (inspect, resolve ambiguous columns with the user, ingest, quality and overview, key findings, 1-2 charts). For `all`, do it for each file from `scan`, summarising each in 2-3 lines.

### `/obd report <file|all>`
Follow `vehicle-report-generation`: make sure the file is ingested and its mappings confirmed, then run `report <file|all>` and present the PDF path and a short summary.

### `/obd clean [file|all]`
Ask the user to confirm: this deletes processed data in `.obd/` (originals and `obd-reports/` are untouched). Only after a yes, run `delete <file> --yes` or `clean --yes` for all.

## Not in this version
`/obd compare`, `/obd vehicle`, Excel reports, DTC lookup and fuel/thermal system diagnosis are planned for a later release. If asked, say so and offer what is available (statistics, anomalies, correlations, charts, PDF).
