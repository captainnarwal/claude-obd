---
name: obd-parameter-interpretation
description: Explain what an OBD-II parameter, PID or trouble code means in plain language - "what is LTFT", "what does MAP show", "what is PID 0C", "what does P0171 mean", "is this value normal". Use for reference questions about vehicle data, with or without a log.
---

# OBD parameter interpretation

Answer reference questions from the engine's reference data, not from memory. It is local, versioned and written to avoid overclaiming. Word answers with `communicating-findings`.

## Commands (no log needed)
```
"${CLAUDE_PLUGIN_ROOT}/scripts/obd" lookup parameter <canonical id or alias> [--vehicle <nickname>] --json
"${CLAUDE_PLUGIN_ROOT}/scripts/obd" lookup pid <0C | "01 0C"> --json
"${CLAUDE_PLUGIN_ROOT}/scripts/obd" lookup dtc <P0171> --json
```

## Parameters ("what is LTFT?")
`lookup parameter` returns the meaning, tips for reading it, the unit, the physical plausibility range and (if the analysis uses any) the limits with their **tier**. Explain in plain language first (one or two sentences), then offer detail. Plausibility range is "values the sensor can physically report", **not** "healthy range" - never present it as normal. If the user gives a value, relate it to the tips, not to a pass/fail verdict.

"Is this value normal?" - the honest answer depends on engine, condition and temperature. Use the tier: vehicle-specific limits are the user's own; engine-family and generic limits are rules of thumb (say so). Prefer relative reading: compare idle vs cruise, bank vs bank, this run vs an earlier run.

## PIDs
`lookup pid` returns the standard Mode 01 formula (bytes A, B), unit and meaning. Offer this when the user is decoding raw scanner bytes.

## Trouble codes ("what does P0171 mean?")
`lookup dtc` decodes the **structure** (system, generic vs manufacturer-specific, subsystem) and the monitored topic and related parameters for common generic ranges. Rules:
- Say it describes the code's family; it does **not** say the code is stored in the user's car, and a code names a monitored condition, not a failed part.
- Official descriptions are copyrighted and are not shipped; do not recite a code's official title from memory as if it came from the tool. Explain the topic the tool returned.
- Manufacturer-specific codes (second character 1, or P3xxx below P3400) mean different things per make: say a make-specific source is needed.
- Then connect to data: the `related_parameters` are what to look at in a log (`overview`, `stats`, `analyze`).

## If the lookup fails
`PARAMETER_NOT_FOUND` lists near matches; `AMBIGUOUS_PARAMETER` lists candidates (ask which); `INVALID_DTC` gives the expected format; `PID_NOT_FOUND` lists the PIDs in the reference. Do not guess a meaning that is not in the reference.
