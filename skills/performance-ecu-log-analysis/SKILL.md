---
name: performance-ecu-log-analysis
description: Tuner-level review of performance logs - full-throttle pulls, boost vs target, spool, knock retard, lambda/AFR at load, timing, before/after comparison of two runs or a modification. Use when the user talks about pulls, dyno-style runs, tunes, boost, timing, wideband readings or "did my mod help".
---

# Performance and ECU log analysis

Same rules as `automotive-data-analysis` (everything through the engine, bounded outputs) and `communicating-findings` (observation / inference / hypothesis). This skill adds the review order a tuner would follow. It is analysis, not tuning: never suggest specific map changes, timing numbers or boost targets.

## 1. Set up
- Ask once for the vehicle's aspiration and fuel and the mass if known (`vehicle add ... --aspiration turbo --fuel petrol --mass-kg 1400`, then `vehicle assign`). That selects engine-family limits and enables the wheel-power lower bound.
- Record conditions if the user cares about comparing: `session add --label "pull-1" --vehicle <v> --condition ambient=30C --condition fuel=95RON --condition mods=intake`, then `session assign`.
- Confirm column mappings (boost vs MAP, AFR vs lambda, unit of boost). A wrong boost unit ruins every comparison.

## 2. Review order for a pull
Run `analyze <dataset>`; for detail use `--modules <id>` on one module.
1. **Run detection** - `engine_performance`: how many wide-open-throttle runs, their length, speed range, peak RPM. If there is no run, say so; do not judge power from cruise data.
2. **Mixture** - `fuel_system`: lambda/AFR at wide-open throttle (lean readings under full load are the first safety flag), lean or rich episodes, trims by condition.
3. **Knock and timing** - `ignition`: knock-retard events and the load they occurred at, misfire counter, timing by condition. Repeated or deep retard under load matters more than a single blip.
4. **Boost** - `intake_boost`: peak boost, boost vs target (overshoot / shortfall), spool time (to 90 % of peak), boost at light load.
5. **Thermals** - `thermal`: coolant overshoot and intake-air heat soak during or just before the pull; heat changes everything above.
6. **Hypotheses** - `diagnostics`: read them as a ranked list of possibilities with the physical tests; see `engine-diagnostics`.

## 3. Comparing runs ("did my mod help?")
`compare <baseline> <run> [<run>...] [--params ...]` compares warm-engine samples within the same load state and reports each difference with a 95 % interval and whether it is clear.
- Read the `warnings` first: different vehicle, unrecorded conditions (ambient, fuel, mods), very different lengths. If conditions are not matched, say the comparison is indicative only.
- Report clear differences in matched conditions, with units and sample counts. A difference inside its interval is "no clear difference", not "no effect".
- The interval assumes independent samples; real logs are autocorrelated, so treat it as optimistic.
- Never claim a gain in power from a log of RPM and boost alone. Wheel power is only a lower bound from speed and mass (ignores drag), and crank power needs a dyno.

## 4. What not to do
- No tune advice (timing, boost, fuel numbers). Describe what the log shows and what to check; a tuner owns the changes.
- Do not call a run "safe". Absence of knock in a log does not prove a tune is safe; say what the log covers.
- Generic limits are rules of thumb; say which tier a limit came from.
