"""Deterministic diagnostic reasoning (design doc 7.5): rules over module metrics -> ranked hypotheses.

The rule base is data (reference/diagnostic_rules/*.yaml). Rules never name a part as faulty; they rank possible
explanations, say what evidence is missing and which physical tests would discriminate between them.
"""

from __future__ import annotations

import math
from fnmatch import fnmatchcase
from functools import lru_cache
from typing import Any, Callable

import yaml

from obd_engine.analysis.registry.base import Context, Module, evidence, finding, register
from obd_engine.reference.loader import reference_dir

SYSTEM_MODULES = ["fuel_system", "thermal", "ignition", "intake_boost", "engine_performance", "drivetrain"]
OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
    "abs>=": lambda a, b: abs(a) >= b,
}
FAVOR_STEP, AGAINST_STEP = 0.15, 0.20
THRESHOLD = {"limit": None, "unit": "", "tier": "generic", "source": "diagnostic rule thresholds are generic rules of thumb, not specifications for this engine"}


@lru_cache(maxsize=1)
def load_rules() -> tuple[str, list[dict[str, Any]]]:
    version, rules = [], []
    for p in sorted((reference_dir() / "diagnostic_rules").glob("*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        version.append(f"{p.stem}:{raw.get('version', '0')}")
        rules.extend(raw["rules"])
    return ",".join(version), rules


def flatten(d: Any, prefix: str, out: dict[str, float]) -> None:
    """Numeric leaves of nested metric dicts as dotted keys. Booleans become 0/1; lists are skipped."""
    if isinstance(d, dict):
        for k, v in d.items():
            flatten(v, f"{prefix}.{k}", out)
    elif isinstance(d, bool):
        out[prefix] = float(d)
    elif isinstance(d, (int, float)) and math.isfinite(d):
        out[prefix] = float(d)


def facts_from(results: list[dict[str, Any]]) -> dict[str, float]:
    facts: dict[str, float] = {}
    for r in results:
        if r.get("status") != "not_applicable" and r.get("metrics"):
            flatten(r["metrics"], r["module"], facts)
    return facts


def _pool(facts: dict[str, float], patterns: list[str]) -> list[tuple[str, float]]:
    return [(k, v) for k, v in facts.items() if any(fnmatchcase(k, p) for p in patterns)]


def check(cond: dict[str, Any], facts: dict[str, float]) -> dict[str, Any] | None:
    """None = unknown (no matching fact). Otherwise {met, facts, note}."""
    pool = _pool(facts, cond["keys"])
    if not pool:
        return None
    if "ref" in cond:
        ref = _pool(facts, cond["ref"])
        if not ref:
            return None
        limit = max(v for _, v in ref) + float(cond.get("delta", 0.0))
    else:
        limit = float(cond["value"])
    op = OPS[cond["op"]]
    hits = [(k, v) for k, v in pool if op(v, limit)]
    return {"met": bool(hits), "facts": hits or pool[:1], "note": cond.get("note", ""), "limit": limit}


def evaluate(rule: dict[str, Any], facts: dict[str, float]) -> dict[str, Any] | None:
    """The triggered rule with ranked hypotheses, or None."""
    when = [check(c, facts) for c in rule["when"]]
    if any(w is None or not w["met"] for w in when):
        return None
    supports = [r for r in (check(c, facts) for c in rule.get("supports", [])) if r and r["met"]]
    ranked = []
    for h in rule["hypotheses"]:
        score = float(h["weight"])
        fav = [r for r in (check(c, facts) for c in h.get("favors", [])) if r and r["met"]]
        ag = [r for r in (check(c, facts) for c in h.get("against", [])) if r and r["met"]]
        score = min(0.95, max(0.02, score + FAVOR_STEP * len(fav) - AGAINST_STEP * len(ag)))
        ranked.append({"cause": h["cause"], "score": round(score, 2), "prior": float(h["weight"]),
                       "favoring": [f["note"] for f in fav], "against": [a["note"] for a in ag]})
    ranked.sort(key=lambda h: -h["score"])
    return {"rule": rule, "when": when, "supports": supports, "ranked": ranked}


class Diagnostics(Module):
    id = "diagnostics"
    version = "1.0.0"
    title = "Diagnostic Reasoning"
    post_pass = True
    needs = SYSTEM_MODULES

    def applicability(self, ctx: Context):
        if not any(r.get("module") in SYSTEM_MODULES and r.get("status") != "not_applicable" for r in ctx.prior):
            return "not_applicable", "no system-module results to reason over (needs fuel, thermal, ignition, intake/boost or drivetrain data)"
        return "applicable", ""

    def run(self, ctx: Context) -> dict[str, Any]:
        rules_version, rules = load_rules()
        facts = facts_from(ctx.prior)
        fs, listing = [], []
        for rule in rules:
            hit = evaluate(rule, facts)
            if hit is None:
                continue
            ranked = hit["ranked"]
            n_support = len(hit["supports"])
            level = "medium" if n_support >= 1 else "low"
            reason = (f"{n_support} supporting condition(s) met; " if rule.get("supports") else "") + "capped at medium: rule thresholds are generic rules of thumb"
            ev = [evidence(ctx, k, stat="fact", value=round(v, 3)) for c in [*hit["when"], *hit["supports"]] for k, v in c["facts"][:2]]
            missing = [f"{m['param']}: {m['why']}" for m in rule.get("missing_evidence", []) if not ctx.has(m["param"])]
            top = ranked[0]
            fs.append(finding(
                f"{rule['title']}: most consistent with {top['cause'][0].lower() + top['cause'][1:]}", rule["category"], "hypothesis", rule.get("severity", "attention"),
                level, reason, conditions="; ".join(c["note"] for c in [*hit["when"], *hit["supports"]] if c["note"]), ev=ev, threshold=dict(THRESHOLD),
                alternatives=[f"{h['cause']} (relative support {h['score']:.2f})" for h in ranked[1:4]],
                missing=missing, next_steps=list(rule.get("tests", []))))
            listing.append({"rule": rule["id"], "title": rule["title"], "category": rule["category"], "confidence": level, "ranked": ranked,
                            "supporting_conditions": [s["note"] for s in hit["supports"]], "missing_evidence": missing, "tests": list(rule.get("tests", []))})
        notes = ["Hypotheses are ranked possibilities from deterministic rules; they are not a diagnosis. Relative support is not a probability.",
                 f"Rule base {rules_version}; {len(rules)} rule(s) evaluated, {len(fs)} matched."]
        return {"summary": f"{len(fs)} pattern(s) matched of {len(rules)} diagnostic rule(s).",
                "metrics": {"rules_version": rules_version, "rules_evaluated": len(rules), "matched": [h["rule"] for h in listing], "hypotheses": listing},
                "findings": fs, "notes": notes}


register(Diagnostics())
