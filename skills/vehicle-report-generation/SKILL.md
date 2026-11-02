---
name: vehicle-report-generation
description: Build a PDF report from an analyzed vehicle log. Use when the user asks for a report, PDF, "summary for my mechanic", write-up, or something to share or print from OBD/ECU log data.
---

# Vehicle report generation

Reports come from the engine, never from text you write yourself: the engine assembles a PDF from stored, reproducible analysis results (evidence, methods, versions included). `report <dataset> --format pdf|xlsx|both` produces the PDF and/or an Excel workbook (same content: findings, hypotheses, statistics, data quality, mappings, downsampled normalized data); offer Excel when the user wants to sort, filter or share the numbers, and PDF for a mechanic or a summary.

## Steps
1. Make sure the data is ingested: `list`. If not, run the first-look workflow from `automotive-data-analysis` (`inspect`, ask about ambiguous columns, `ingest`).
2. **Resolve open questions first.** If `overview` shows `pending_confirmation`, ask the user and use `map`. Those columns are excluded from the report otherwise, and the report says so.
3. Scope: ask only if unclear - one file (default), or `all` (one PDF per file). Optional `--title "..."`.
4. Run `"${CLAUDE_PLUGIN_ROOT}/scripts/obd" report <file|all> [--title ...] --json`.
5. Present: the file path(s) (relative to the current folder, under `obd-reports/`), the 2-4 most important findings in your own words per `communicating-findings`, and anything in `caveats`.
6. Offer next steps: more charts (`chart`), a zoom on a time window, or fixing a mapping and regenerating.

## What the PDF contains
Cover and disclaimer; executive summary (findings tagged observation / inference / hypothesis with severity and confidence); vehicle details (unknown unless provided); dataset overview; data-quality assessment; parameter statistics and condition table; performance observations; anomalies; recommendations (physical checks, parameters to log next time); limitations; appendix with every column mapping (confidence, who confirmed it), methods and run ids. Sections appear only when the data supports them; skipped analyses are listed under Limitations.

## Audience tone
- Enthusiast: summarise in plain language, explain what each finding means and what to check next.
- Mechanic / for a workshop: lead with the findings and numbers, units and conditions, and the recommended physical tests; note that the PDF's appendix lists how columns were interpreted.

## Don'ts
Do not add findings that are not in the engine output, do not paraphrase a threshold as a spec, and do not describe the PDF as a diagnosis.
