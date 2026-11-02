---
name: engine-diagnostics
description: Symptom-driven diagnosis from a vehicle log - rough idle, hesitation, losing power, running lean or rich, overheating, knock, misfires, check-engine light, boost problems. Use when the user describes a symptom or asks "what's wrong with my car" and has (or can make) an OBD/ECU log; turns the engine's ranked hypotheses into plain advice with physical tests.
---

# Engine diagnostics

Work through the engine, never around it (see `automotive-data-analysis`). Word everything with `communicating-findings`. The engine returns **measurements** (system modules) and **ranked hypotheses** (the `diagnostics` module); your job is to connect them to the symptom, say what is uncertain, and say what to check physically.

## 1. Symptom intake (ask only what you do not already know, max 3 questions)
- What is happening, and when? (cold start / warm idle / light cruise / hard acceleration / hot day)
- Is the check-engine light on, or were codes read? If the user has codes, decode their structure with `lookup dtc <code>` - it describes the code family, it does **not** prove the code is stored.
- Any recent work, fuel change or modification? Offer to record a vehicle (`vehicle add ...`, then `vehicle assign`) so thresholds fit the engine.

If there is no log yet, say which parameters would help for that symptom (below) and how to record them; do not diagnose from the symptom alone.

## 2. Which analysis to run
Run `analyze <dataset>` (all applicable modules, includes `diagnostics`). Use `--modules <id>` for a single module to see all its findings. Pick by symptom:

| Symptom | Look at | Useful extra parameters |
|---|---|---|
| rough idle, hesitation | `fuel_system` (idle vs load trims), `ignition` (misfire counter), `intake_boost` (idle MAP) | lambda, misfire count, fuel pressure |
| lean / rich running | `fuel_system` (by bank and condition), `diagnostics` | lambda per bank, fuel pressure, MAF |
| overheating, slow warm-up | `thermal` | oil temp, ambient temp |
| knock, pinging, power loss under load | `ignition`, `intake_boost`, `fuel_system` (lambda at WOT) | knock retard, lambda, intake air temp |
| boost problems | `intake_boost` (target vs actual, spool, boost at light load) | boost target, wastegate duty, barometric pressure |
| slipping / harsh shifts | `drivetrain` | transmission temp, converter slip, gear |

## 3. Reading the result
1. Start with `summary` and `caveats`; mention any `needs_input` (unconfirmed column mappings) first.
2. Present **measurements first**, in the order of importance: warnings, then attention items. Give value, unit, operating condition and sample count (`evidence`).
3. Then the **hypotheses**. For each pattern: say what was seen, then the ranked possible explanations. Relative support is *not a probability* - say "the top possibility, with the data we have, is ..., but ... and ... fit too". Always mention the `alternatives` and the `confidence_reason` (confidence is capped at medium because the rule thresholds are generic).
4. Say what **evidence is missing** (`missing_evidence`) as "log these parameters next time".
5. Give the **physical tests** (`next_steps`) that would tell the explanations apart, in the order a careful owner would do them (cheap and non-invasive first).

## 4. What not to do
- Never state that a part is faulty, broken or needs replacing. Rank possibilities and recommend tests.
- Never invent values, codes or thresholds that the engine did not return. If a parameter is missing (`PARAMETER_NOT_AVAILABLE`), say so.
- Do not diagnose safety-critical systems (brakes, steering, airbags); say it is out of scope and advise a qualified technician.
- If the log looks fine, say that plainly: "no pattern matched" is a valid result, not a failure.
- A single log is a snapshot. If a finding matters, suggest repeating the run under the same conditions, or logging again after a fix to confirm the change (comparison across runs is planned).

## 5. Closing
Offer a PDF (`report <dataset>`): it contains the ranked hypotheses, tests and missing evidence in one document to show a mechanic.
