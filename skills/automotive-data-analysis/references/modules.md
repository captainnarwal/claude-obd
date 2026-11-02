# Analysis modules (v0.1)

`analyze <dataset> --modules auto` runs every applicable module and lists skipped ones with reasons. `analyze --list-modules` prints this set.

| Module id | Needs | Produces |
|---|---|---|
| `data_quality` | nothing | completeness, effective rates, dropouts, sentinels, out-of-range counts, time resets/gaps, "supported by the available parameters" list |
| `statistics` | any mapped numeric parameter | n/mean/std/min/p5/median/p95/max per parameter, and means by operating condition (idle / cruise / moderate / high_load / wot) |
| `time_series` | any mapped parameter | trend per minute, rate-of-change, steady-state fraction, coolant warm-up time, WOT/high-load/idle spans, generic limit checks (e.g. coolant > 110 °C) |
| `correlation` | two or more mapped parameters | strongest pairs with n, Pearson/Spearman, optional lag |
| `anomalies` | any mapped continuous parameter | isolated spikes (robust z vs rolling median/MAD), flatlines, impossible combinations (moving with RPM ~0) |

## Operating conditions (generic classifier)
idle = stopped and < 1200 rpm; wot = throttle or load >= 90 %; high_load = throttle >= 60 % or load >= 70 %; cruise = moving >= 5 km/h with throttle < 35 %; warm = coolant >= 80 °C. These are generic - say "generic condition thresholds" when it matters.

## Thresholds
Only generic sanity limits exist in this version. A finding that uses one carries `threshold.tier = "generic"` and is capped at medium confidence. Prefer relative statements (change over time, idle vs cruise) over absolute limits.
