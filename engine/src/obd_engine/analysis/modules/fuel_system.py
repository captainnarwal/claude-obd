"""Module C - Fuel System (design doc 7.4): trims by condition and bank, combined trim, bank imbalance, lambda/AFR tracking."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from obd_engine.analysis.modules._common import CONDITION_NOTE, STATE_TEXT, by_condition, generic_threshold, labelled, runs, series
from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register
from obd_engine.query.service import asof_align

TRIMS = {"stft_b1": "Short-term fuel trim bank 1", "ltft_b1": "Long-term fuel trim bank 1",
         "stft_b2": "Short-term fuel trim bank 2", "ltft_b2": "Long-term fuel trim bank 2"}
STOICH = {"petrol": 14.7, "diesel": 14.5, "e85": 9.76, "lpg": 15.5}


def _stoich(ctx: Context) -> tuple[float, str]:
    fuel = str(((ctx.vehicle or {}).get("engine") or {}).get("fuel") or "").lower()
    for k, v in STOICH.items():
        if k in fuel:
            return v, f"stoichiometric AFR {v} assumed from the vehicle's fuel ({k})"
    return 14.7, "stoichiometric AFR 14.7 assumed (petrol); set the vehicle's fuel with `vehicle set` if different"


class FuelSystem(Module):
    id = "fuel_system"
    version = "1.0.0"
    title = "Fuel System"
    required_any = ["stft_b1", "ltft_b1", "stft_b2", "ltft_b2", "lambda", "afr"]
    optional = ["engine_speed", "throttle_position", "ect"]
    config_defaults = {"trim_attention": 10.0, "trim_warning": 20.0, "spread_points": 8.0, "bank_diff": 5.0,
                       "lean_lambda": 1.05, "rich_lambda": 0.93, "episode_s": 3.0}

    def run(self, ctx: Context) -> dict[str, Any]:
        cfg = ctx.config
        trims = [p for p in TRIMS if ctx.has(p)]
        lam_src = "lambda" if ctx.has("lambda") else ("afr" if ctx.has("afr") else None)
        fs: list[dict[str, Any]] = []
        metrics: dict[str, Any] = {"trims": {}}
        notes = [CONDITION_NOTE, "Trim statistics use warm-engine samples (closed loop); cold-start rows are excluded."]
        df = labelled(ctx, [*trims, *([lam_src] if lam_src else [])], extra=["throttle_position", "engine_speed"])

        warm_means: dict[str, float] = {}
        for p in trims:
            sub = series(df, p)
            warm = sub.filter(pl.col("thermal_state").is_in(["warm", "unknown"]))
            if warm.height < 5:
                continue
            x = warm[p].to_numpy()
            mean = float(x.mean())
            warm_means[p] = mean
            cond = by_condition(warm, p)
            metrics["trims"][p] = {"unit": "%", "warm_mean": round(mean, 2), "warm_n": int(x.size), "by_condition": cond}
            t, rid = warm["t"].to_numpy(), warm["__row_id"].to_numpy()
            ev = [evidence(ctx, p, t[0], t[-1], x.size, "mean", mean, "%", (rid[0], rid[-1]))]
            mag = abs(mean)
            if mag >= cfg["trim_attention"]:
                sev = "warning" if mag >= cfg["trim_warning"] else "attention"
                fs.append(finding(
                    f"{TRIMS[p]} averaged {mean:+.1f}% when warm (n={x.size})", "fuel", "observation", sev,
                    reason="direct measurement of the ECU's fuel correction", conditions="warm engine, all load states", ev=ev,
                    threshold=generic_threshold(cfg["trim_attention"], "%", "sustained fuel-trim magnitude"),
                    alternatives=["A correction that is large but stable can still be normal for a modified or high-mileage engine"],
                    next_steps=["Compare idle vs. cruise vs. load trims (see the condition breakdown) to narrow down the cause"]))
            if len(cond) >= 2:
                hi = max(cond, key=lambda s: cond[s]["median"])
                lo = min(cond, key=lambda s: cond[s]["median"])
                spread = cond[hi]["median"] - cond[lo]["median"]
                if spread >= cfg["spread_points"]:
                    fs.append(finding(
                        f"{TRIMS[p]} depends on operating condition: {cond[hi]['median']:+.1f}% at {STATE_TEXT[hi]} vs {cond[lo]['median']:+.1f}% at {STATE_TEXT[lo]}",
                        "fuel", "inference", "attention", confidence="medium",
                        reason=f"median trim differs by {spread:.1f} points between conditions (n={cond[hi]['n']} / {cond[lo]['n']})",
                        conditions=f"{STATE_TEXT[hi]} vs {STATE_TEXT[lo]}",
                        ev=[evidence(ctx, p, n=cond[hi]["n"], stat="median", value=cond[hi]["median"], unit="%"),
                            evidence(ctx, p, n=cond[lo]["n"], stat="median", value=cond[lo]["median"], unit="%")],
                        alternatives=["Condition-dependent correction has several possible causes; this module does not pick one"],
                        next_steps=["Run `analyze` with all modules so the diagnostic rules can weigh this against other evidence"]))
            if p not in {f["evidence"][0]["parameter"] for f in fs if f["evidence"]}:
                fs.append(finding(f"{TRIMS[p]} averaged {mean:+.1f}% when warm (n={x.size}) - within +/-{cfg['trim_attention']:g}%", "fuel", "observation",
                                  conditions="warm engine", ev=ev, threshold=generic_threshold(cfg["trim_attention"], "%", "sustained fuel-trim magnitude")))

        # bank imbalance (relative, no absolute limit)
        for a, b, what in (("ltft_b1", "ltft_b2", "long-term"), ("stft_b1", "stft_b2", "short-term")):
            if a in warm_means and b in warm_means:
                d = warm_means[a] - warm_means[b]
                metrics.setdefault("bank_difference", {})[what] = round(d, 2)
                if abs(d) >= cfg["bank_diff"]:
                    fs.append(finding(f"{what.capitalize()} trims differ between banks by {d:+.1f} points (bank 1 {warm_means[a]:+.1f}%, bank 2 {warm_means[b]:+.1f}%)",
                                      "fuel", "observation", "attention", reason="difference of warm-engine means",
                                      ev=[evidence(ctx, a, stat="mean", value=warm_means[a], unit="%"), evidence(ctx, b, stat="mean", value=warm_means[b], unit="%")],
                                      alternatives=["Natural bank-to-bank variation", "A fault affecting only one bank"]))

        # combined trim
        for bank in ("b1", "b2"):
            s, l_ = f"stft_{bank}", f"ltft_{bank}"
            if s in warm_means and l_ in warm_means:
                al, _ = asof_align(df.filter(pl.col("thermal_state").is_in(["warm", "unknown"])), [s, l_])
                if al.height >= 5:
                    comb = (al[s] + al[l_]).to_numpy()
                    metrics.setdefault("combined_trim", {})[bank] = {"mean": round(float(comb.mean()), 2), "peak": round(float(comb[np.argmax(np.abs(comb))]), 2), "n": int(comb.size)}
                    if abs(comb.mean()) >= cfg["trim_attention"] + 5:
                        fs.append(finding(f"Combined fuel correction bank {bank[1]} averaged {comb.mean():+.1f}% (short + long term, n={comb.size})", "fuel", "observation", "attention",
                                          reason="short-term and long-term trims paired by time proximity",
                                          ev=[evidence(ctx, s, n=comb.size, stat="mean_combined", value=float(comb.mean()), unit="%")],
                                          threshold=generic_threshold(cfg["trim_attention"] + 5, "%", "combined fuel-trim magnitude")))

        if lam_src:
            self._lambda(ctx, df, lam_src, fs, metrics, notes)
        return {"summary": f"Fuel system: {len(metrics['trims'])} trim channel(s)" + (f", mixture from {lam_src}" if lam_src else "") + f"; {len(fs)} finding(s).",
                "metrics": metrics, "findings": fs, "notes": notes}

    def _lambda(self, ctx: Context, df: pl.DataFrame, src: str, fs: list, metrics: dict, notes: list) -> None:
        cfg = ctx.config
        sub = series(df, src)
        if sub.height < 10:
            return
        stoich = 1.0
        if src == "afr":
            stoich, why = _stoich(ctx)
            notes.append(why)
        lam = sub[src].to_numpy() / stoich
        t, rid = sub["t"].to_numpy(), sub["__row_id"].to_numpy()
        state = sub["load_state"].to_list()
        thr = sub["throttle_position__c"].to_numpy() if "throttle_position__c" in sub.columns else np.full(lam.size, np.nan)
        rpm = sub["engine_speed__c"].to_numpy() if "engine_speed__c" in sub.columns else np.full(lam.size, np.nan)
        decel = (thr < 3) & (rpm > 1200)  # fuel cut: lean readings are expected and not a finding
        excluded = np.array([s in ("wot", "off") for s in state]) | decel
        metrics["lambda"] = {"source": src, "median": round(float(np.median(lam)), 3), "n": int(lam.size), "excluded_decel_wot_or_off": int(excluded.sum())}
        wot = np.array([s == "wot" for s in state])
        if wot.sum() >= 5:
            w = float(np.median(lam[wot]))
            metrics["lambda"]["wot_median"] = round(w, 3)
            if w > 1.0:
                fs.append(finding(f"Mixture was lean at wide-open throttle (median lambda {w:.2f}, n={int(wot.sum())})", "fuel", "observation", "warning",
                                  reason="lambda above 1.0 under full load is normally avoided by the ECU; measured directly", conditions=STATE_TEXT["wot"],
                                  ev=[evidence(ctx, src, float(t[wot][0]), float(t[wot][-1]), int(wot.sum()), "median", w)],
                                  threshold=generic_threshold(1.0, "lambda", "full-load mixture"),
                                  alternatives=["Wideband sensor reading error or calibration offset", "A log-time sensor lag during transients"],
                                  next_steps=["Confirm with a second reading (e.g. a wideband gauge) before drawing conclusions"]))
        for name, mask, sev in (("lean", (lam > cfg["lean_lambda"]) & ~excluded, "attention"), ("rich", (lam < cfg["rich_lambda"]) & ~excluded, "info")):
            eps = runs(t, mask, cfg["episode_s"])
            metrics["lambda"][f"{name}_episodes"] = len(eps)
            if eps:
                longest = max(eps, key=lambda e: t[e[1]] - t[e[0]])
                i0, i1 = longest
                fs.append(finding(
                    f"{len(eps)} sustained {name} episode(s) (lambda {'>' if name == 'lean' else '<'} {cfg[name + '_lambda']:g}); longest {t[i1] - t[i0]:.0f} s, mean lambda {lam[i0:i1 + 1].mean():.2f}",
                    "fuel", "observation", sev, reason=f"episodes of at least {cfg['episode_s']:g} s outside wide-open throttle and fuel cut",
                    conditions=", ".join(sorted({STATE_TEXT[s] for s in state[i0:i1 + 1]})),
                    ev=[evidence(ctx, src, float(t[i0]), float(t[i1]), i1 - i0 + 1, "mean", float(lam[i0:i1 + 1].mean()), rows=(rid[i0], rid[i1]))],
                    threshold=generic_threshold(cfg[name + "_lambda"], "lambda", "mixture deviation")))


register(FuelSystem())
