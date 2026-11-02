"""Module A - Data Quality (design doc 7.4): completeness, rates, dropouts, sentinels, time issues, usable-for."""

from __future__ import annotations

from typing import Any

from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register

USABLE_FOR = {
    "fuel trims": ["stft_b1", "ltft_b1"],
    "mixture (lambda / AFR)": ["lambda"],
    "knock / misfire": ["knock_retard"],
    "thermal behavior": ["ect"],
    "engine speed / load behavior": ["engine_speed", "engine_load", "throttle_position"],
    "intake / boost": ["map", "maf"],
    "ignition timing": ["timing_advance"],
    "speed / gearing": ["vehicle_speed", "engine_speed"],
}


class DataQuality(Module):
    id = "data_quality"
    version = "1.0.2"
    title = "Data Quality"

    def applicability(self, ctx: Context):
        return "applicable", ""

    def run(self, ctx: Context) -> dict[str, Any]:
        q = ctx.quality
        t = q.get("time", {})
        fs: list[dict[str, Any]] = []
        eds = lambda p=None: [evidence(ctx, p, n=q.get("rows"))]  # noqa: E731

        if t.get("resets"):
            fs.append(finding(f"{t['resets']} timestamp reset(s) in the log", "data_quality", "observation", "attention",
                              reason="direct count from the time axis", ev=eds(),
                              next_steps=["Check whether the logger restarted or the log was concatenated"]))
        if t.get("gaps"):
            fs.append(finding(f"{t['gaps']} large gap(s) between samples (segments: {t.get('segments')})", "data_quality", "observation", "attention",
                              reason=f"gap threshold = max(20 x median interval {t.get('median_interval_s')} s, 10 s)", ev=eds()))
        if t.get("rows_without_time"):
            fs.append(finding(f"{t['rows_without_time']} row(s) without a usable timestamp were excluded", "data_quality", "observation", "attention", ev=eds()))
        if q.get("duplicates_dropped"):
            fs.append(finding(f"{q['duplicates_dropped']} duplicate row(s) removed", "data_quality", "observation", ev=eds()))
        spec = t.get("spec", {})
        if spec.get("kind") == "index":
            fs.append(finding("No time column: sample order was used instead of real time", "data_quality", "observation", "warning",
                              reason="rates and durations in this dataset are not in seconds", ev=eds()))

        rows = []
        for name, info in ctx.params.items():
            row = {"parameter": name, "unit": info.get("unit"), "n": info.get("n"), "completeness": info.get("completeness"),
                   "rate_hz": info.get("rate_hz"), "max_gap_s": info.get("max_gap_s"), "dropouts": info.get("dropouts", 0),
                   "out_of_range": info.get("out_of_range", 0), "sentinels": info.get("sentinels", 0),
                   "parse_failures": info.get("parse_failures", 0), "mapped": info.get("mapped")}
            rows.append(row)
            if info.get("parse_failures"):
                fs.append(finding(f"{name}: {info['parse_failures']} value(s) could not be parsed as numbers", "data_quality", "observation", "attention",
                                  ev=[evidence(ctx, name, n=info["parse_failures"], stat="parse_failures")]))
        for key, label, stat, why in (
            ("sentinels", "suspected 'no data' sentinel value(s)", "sentinel_count", "values like -40 C, 255 or 65535 usually mean the sensor did not respond; kept but excluded from analysis"),
            ("out_of_range", "sample(s) outside the physical plausibility range", "out_of_range_count", "range check against the parameter dictionary; kept but excluded from analysis"),
        ):
            hit = [(r["parameter"], r[key], r["unit"]) for r in rows if r[key]]
            if hit:
                fs.append(finding(f"{sum(h[1] for h in hit)} {label} in {', '.join(h[0] for h in hit[:6])}", "data_quality", "observation", "attention",
                                  reason=why, ev=[evidence(ctx, p, n=n, stat=stat, unit=u) for p, n, u in hit[:6]]))
        const = [(n, i["constant_value"], i.get("unit"), i["n"]) for n, i in ctx.params.items() if "constant_value" in i and i.get("mapped") and not n.startswith("gps_") and n != "odometer"]
        if const:
            fs.append(finding(
                f"{len(const)} parameter(s) never change in the whole log: " + ", ".join(f"{n} = {v:g} {u or ''}".strip() for n, v, u, _ in const[:6]),
                "data_quality", "observation", "attention",
                reason="identical value in every sample; typical of a stuck, unsupported or not-refreshing PID",
                ev=[evidence(ctx, n, n=k, stat="constant_value", value=v, unit=u) for n, v, u, k in const[:6]],
                alternatives=["The vehicle or logger does not actually supply this value", "A genuinely fixed quantity"],
                next_steps=["Check the PID is supported and polled by the logging app before relying on it"]))
        dro = [(r["parameter"], r["dropouts"], r["max_gap_s"]) for r in rows if r["dropouts"]]
        if dro:
            worst = max(d[2] or 0 for d in dro)
            fs.append(finding(f"Dropouts in {len(dro)} parameter(s) (longest gap {worst:g} s): {', '.join(d[0] for d in dro[:6])}", "data_quality", "observation",
                              "attention" if worst > 20 else "info", reason="gap between consecutive samples of a parameter exceeds max(10x its median interval, 5 s)",
                              ev=[evidence(ctx, p, n=n, stat="dropouts", value=g, unit="s") for p, n, g in dro[:6]],
                              next_steps=["Check the adapter connection / logging app for the time of the gap"]))

        usable = {k: (all(ctx.has(p) for p in v)) for k, v in USABLE_FOR.items()}
        sparse = [r["parameter"] for r in rows if r["mapped"] and (r["completeness"] or 1) < 0.5]
        notes = []
        if sparse:
            notes.append("Sparse polling: " + ", ".join(sparse[:8]) + " are present in under half of the rows; analyses pair them by time proximity.")
        unmapped = [m["original_name"] for m in ctx.cat.get_mappings(ctx.dataset_id) if m["status"] in ("needs_confirmation",)]
        if unmapped:
            fs.append(finding(f"{len(unmapped)} column(s) await mapping confirmation and are excluded from automotive analysis", "data_quality", "observation", "attention",
                              ev=eds(), next_steps=["Confirm the mappings with `map`"]))
        return {
            "summary": f"{q.get('rows')} rows, {q.get('duration_s')} s, {len(rows)} parameters; {len(fs)} data-quality finding(s).",
            "metrics": {"parameters": rows, "time": t, "usable_for": usable, "rows": q.get("rows"), "duration_s": q.get("duration_s")},
            "findings": fs, "notes": notes,
        }


register(DataQuality())
