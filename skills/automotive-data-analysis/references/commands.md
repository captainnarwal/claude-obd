# Engine command cheatsheet

Always: `"${CLAUDE_PLUGIN_ROOT}/scripts/obd" <command> ... --json`. One JSON envelope on stdout. Exit codes: 0 ok/partial/needs_input, 2 usage, 3 data error, 4 environment (setup needed).

| Command | Purpose | Key arguments |
|---|---|---|
| `scan [folder]` | list telemetry-looking files | `--recursive` |
| `inspect <path>` | read-only profile, proposed mappings, questions | `--sheet <name>` (XLSX) |
| `ingest <path>` | parse, map, normalize, store in `.obd/` (idempotent by file hash) | `--set "Col=canonical:unit"` (repeat), `--map-file x.json`, `--label`, `--force`, `--sheet` |
| `map <dataset>` | confirm/correct mappings, re-normalizes | `--set "Col=canonical:unit"` (`custom` = no parameter), `--list-parameters` |
| `list` | datasets in this folder | |
| `overview <dataset>` | parameters, units, rates, completeness, quality caveats | |
| `query <dataset>` | bounded excerpt | `--params a,b` `--t0 --t1` (s) `--max-points` (default 200, max 1000) `--agg mean|min|max|median` `--include-flagged` |
| `stats <dataset>` | per-parameter statistics | `--params`, `--by-condition` |
| `correlate <dataset>` | Pearson/Spearman on as-of aligned samples | `--params a,b[,c]` `--max-lag <s>` |
| `anomalies <dataset>` | spikes, flatlines, impossible combinations | `--params`, `--sensitivity low|medium|high` |
| `analyze <dataset>` | run modules | `--modules auto|id,id` `--list-modules` `--no-cache` `--detail` |
| `chart <dataset>` | PNG into `obd-reports/charts/` | `--type timeseries|scatter|histogram|comparison` `--params` `--t0 --t1` `--name` `--compare ds2,ds3` |
| `report <dataset|all>` | PDF and/or Excel into `obd-reports/` | `--format pdf|xlsx|both` (default pdf), `--title`, `--no-charts` |
| `compare <a> <b> [<c>...]` | condition-matched deltas of each run vs the first, with 95% intervals and comparability warnings | `--params a,b` |
| `delete <dataset>` / `clean` | remove processed data (needs `--yes`) | `--all`, `--yes` |
| `vehicle <add|set|show|list|assign|delete>` | vehicle/engine profile; selects threshold tier (vehicle > engine-family > generic) | `--nickname --make --model --year --vin`, engine: `--displacement --cylinders --aspiration na|turbo|supercharged --fuel --redline`, drivetrain: `--transmission --mass-kg`, `--limit "ect:>:112:°C:attention"`, `assign <vehicle> <dataset>`, `delete --yes` |
| `session <add|list|assign>` | group datasets from one outing | `--label --vehicle --condition key=value`, `assign <session> <dataset>` |
| `lookup <dtc|parameter|pid> <id>` | reference: code structure, parameter meaning and limits, PID formula (no dataset needed) | `--vehicle` (shows that vehicle's limit tier) |
| `selftest` | pipeline check on the bundled sample | |

## Envelope fields
`status` (ok / partial / needs_input / error), `summary`, `data`, `units`, `method`, `evidence` (dataset, parameter, time range, row spans, n), `caveats`, `needs_input` (questions to ask the user), `truncated`, `versions`, `error {code, message, hint}`.

Common error codes: `FILE_NOT_FOUND`, `FILE_TOO_LARGE`, `UNSUPPORTED_FORMAT`, `MACRO_FILE_REJECTED`, `DATASET_NOT_FOUND`, `PARAMETER_NOT_AVAILABLE` (see `data.near_matches`), `COLUMN_NOT_FOUND`, `UNKNOWN_PARAMETER`, `CONFIRMATION_REQUIRED`, `INVALID_DTC`, `PID_NOT_FOUND`, `PARAMETER_NOT_FOUND`, `AMBIGUOUS_PARAMETER`, `VEHICLE_NOT_FOUND`, `SESSION_NOT_FOUND`, `SETUP_REQUIRED`.
