# Analysis modules

`analyze <dataset> --modules auto` runs every applicable module and lists skipped ones with reasons. `analyze --list-modules` prints this set.

| Module id | Needs | Produces |
|---|---|---|
| `data_quality` | nothing | completeness, effective rates, dropouts, sentinels, out-of-range counts, time resets/gaps, "supported by the available parameters" list |
| `statistics` | any mapped numeric parameter | n/mean/std/min/p5/median/p95/max per parameter, and means by operating condition (idle / cruise / moderate / high_load / wot) |
| `time_series` | any mapped parameter | trend per minute, rate-of-change, steady-state fraction, coolant warm-up time, WOT/high-load/idle spans, generic limit checks (e.g. coolant > 110 °C) |
| `correlation` | two or more mapped parameters | strongest pairs with n, Pearson/Spearman, optional lag |
| `anomalies` | any mapped continuous parameter | isolated spikes (robust z vs rolling median/MAD), flatlines, impossible combinations (moving with RPM ~0) |
| `fuel_system` | any fuel trim, or lambda / AFR | trims by condition and bank (warm engine only), combined trim, bank imbalance, lean/rich episodes, lean at wide-open throttle |
| `thermal` | coolant temperature | warm-up time, steady-state temperature, overshoot, fall-back after warm, idle vs load, intake heat soak (with IAT) |
| `ignition` | timing, knock retard, knock or misfire counter | timing by condition, knock-retard events, knock / misfire counter increases with the load state they happened in |
| `engine_performance` | engine speed (throttle and speed improve it) | throttle tip-in response, wide-open-throttle runs, wheel-power lower bound (needs `vehicle set --mass-kg`) |
| `intake_boost` | MAP, boost or MAF | idle manifold pressure, peak boost, boost at light load, spool time, boost vs target |
| `drivetrain` | gear, transmission temp, converter slip, or RPM + speed | inferred rpm-per-km/h relationships (gears), shifts, transmission temperature, converter slip at cruise |

These system modules report measurements and condition-dependent behavior (observation, sometimes inference). They do not name causes; say so, and do not turn a finding into "your X is faulty".

## Operating conditions (generic classifier)
idle = stopped and < 1200 rpm; wot = throttle or load >= 90 %; high_load = throttle >= 60 % or load >= 70 %; cruise = moving >= 5 km/h with throttle < 35 %; warm = coolant >= 80 °C. These are generic - say "generic condition thresholds" when it matters.

## Thresholds
Limits resolve vehicle-specific > engine-family > generic (see `vehicle`). A finding carries `threshold.tier`; generic ones are capped at medium confidence and `confidence_reason` says so. Prefer relative statements (change over time, idle vs cruise, bank vs bank) over absolute limits. Findings also carry `alternatives`: mention at least one when explaining a finding.
