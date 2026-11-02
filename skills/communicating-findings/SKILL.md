---
name: communicating-findings
description: How to word vehicle-data findings - observation vs inference vs hypothesis, units and conditions, uncertainty language, and what never to say. Use whenever explaining results from the claude-obd engine or any OBD/ECU log analysis.
---

# Communicating findings

Every engine finding has a `kind`. Keep the distinction visible in your wording.

| Kind | Meaning | How to say it |
|---|---|---|
| observation | a measured fact | state it with value, unit, condition and sample count: "Long-term fuel trim averaged +14.2 % at warm idle (n = 4,120)." |
| inference | an interpretation of observations | signal it: "That means the ECU was adding fuel at idle." |
| hypothesis | a possible cause | always hedged, with alternatives: "This pattern is consistent with unmetered air or a contaminated MAF; it is also consistent with ..." (version 0.1 produces no hypotheses - do not invent them) |

## Always include
- **Units and conditions** with every number (idle / cruise / WOT / warm-up; time window).
- **How many samples** (`n`) when the claim rests on few.
- **Data-quality caveats that change the meaning**: excluded sentinel/out-of-range samples, dropouts, sparse polling, unconfirmed mappings, an index-based time axis.
- **Threshold tier.** If `threshold.tier` is `generic`: "above a generic limit of 110 °C; the right limit depends on your engine." Confidence is capped at medium.
- **Next step**: a physical check or a parameter to log next time (use the finding's `next_steps` / `missing_evidence`).

## Uncertainty language
Use "consistent with", "suggests", "can't rule out", "the log doesn't show". Match strength to `confidence.level` and its `reason`. Prefer relative statements (changed over the run, differs between idle and cruise) over absolute judgments when thresholds are generic.

## Never say
- "Your X is broken / failed / needs replacing." The engine ranks nothing as faulty and neither do you.
- Numbers that are not in the engine output, or rounded estimates of missing parameters.
- Anything about safety-critical systems (brakes, steering, airbags) - out of scope; recommend a qualified technician.
- Causation from correlation.

## Close the loop
End analyses with what the data can and cannot support, and one concrete next action. The standing disclaimer: this is analytical assistance, not a substitute for physical inspection.
