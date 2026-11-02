"""Stage 4 - deterministic, explainable semantic mapping (design doc 5.5)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import polars as pl

from obd_engine.cli.envelope import clean_text
from obd_engine.ingestion.adapters.base import split_header
from obd_engine.reference.loader import Dictionary, Param, canon_unit, conversion, load_dictionary, norm_name

CONFIRMED_MIN = 0.9
UNMAPPED_MAX = 0.5
_BANK_TOKEN = re.compile(r"\b(b[12]|bank ?[12]|s[12]|sensor ?[12]|b[12]s[12])\b")
_TEMP_IDS = {"ect", "iat", "oil_temp", "ambient_temp", "trans_temp", "catalyst_temp_b1s1", "diesel_particulate_temp"}
# parameters whose unit is genuinely ambiguous without a header unit
_UNIT_SENSITIVE_CATEGORIES = {"thermal"}
_UNIT_SENSITIVE_IDS = _TEMP_IDS | {"map", "baro", "boost", "boost_target", "fuel_pressure", "fuel_rail_pressure", "vehicle_speed", "maf"}


_AMBIGUOUS_GUESSES = {
    "temp": ["ect", "iat", "oil_temp", "ambient_temp"], "temperature": ["ect", "iat", "oil_temp", "ambient_temp"],
    "pressure": ["map", "baro", "boost", "fuel_pressure"], "voltage": ["control_module_voltage", "o2_b1s1_voltage"],
    "trim": ["stft_b1", "ltft_b1"], "fuel": ["stft_b1", "ltft_b1"], "o2": ["o2_b1s1_voltage", "lambda"], "oxygen": ["o2_b1s1_voltage", "lambda"],
}


@dataclass
class ColStats:
    n: int = 0
    min: float | None = None
    max: float | None = None
    p5: float | None = None
    p95: float | None = None
    numeric: bool = False


def column_stats(df: pl.DataFrame, exclude: set[str] | None = None) -> dict[str, ColStats]:
    out: dict[str, ColStats] = {}
    for c in df.columns:
        if c == "__row_id" or (exclude and c in exclude):
            continue
        s = df[c]
        if s.dtype == pl.String:
            s = s.str.strip_chars().str.replace(",", ".", literal=True).cast(pl.Float64, strict=False)
            nn = df[c].is_not_null().sum()
            ok = s.is_not_null().sum()
            if nn == 0 or ok < 0.8 * nn:
                out[c] = ColStats(n=int(nn), numeric=False)
                continue
        elif not s.dtype.is_numeric():
            out[c] = ColStats(n=int(s.is_not_null().sum()), numeric=False)
            continue
        s = s.cast(pl.Float64, strict=False).drop_nulls()
        s = s.filter(s.is_finite())
        if s.len() == 0:
            out[c] = ColStats(n=0, numeric=True)
            continue
        out[c] = ColStats(
            n=int(s.len()), min=float(s.min()), max=float(s.max()),
            p5=float(s.quantile(0.05)), p95=float(s.quantile(0.95)), numeric=True,
        )
    return out


def _plausible(p: Param, st: ColStats, scale: float, offset: float) -> float | None:
    """Return +1 / 0 / -1 style evidence: fraction-like score in [0,1], or None if unknown."""
    if not st.numeric or st.n == 0 or st.p5 is None or st.p95 is None:
        return None
    lo, hi = sorted((st.p5 * scale + offset, st.p95 * scale + offset))
    pr_lo, pr_hi = p.range
    if lo >= pr_lo - 1e-9 and hi <= pr_hi + 1e-9:
        return 1.0
    # partial overlap
    ov = max(0.0, min(hi, pr_hi) - max(lo, pr_lo))
    span = max(hi - lo, 1e-9)
    return ov / span


def _fuzzy(name: str, d: Dictionary) -> list[tuple[float, str]]:
    toks = set(name.split())
    best: dict[str, float] = {}
    for alias, ids in d.alias_index.items():
        ratio = SequenceMatcher(None, name, alias).ratio()
        score = 0.0
        if ratio >= 0.84:
            score = 0.55 + (ratio - 0.84) * 1.6  # 0.55 .. 0.80
        atoks = set(alias.split())
        if len(atoks) >= 2 and len(toks) >= 2 and (atoks <= toks or toks <= atoks):
            score = max(score, 0.62)
        if score:
            for pid in ids:
                best[pid] = max(best.get(pid, 0.0), min(score, 0.8))
    return sorted(((s, pid) for pid, s in best.items()), key=lambda x: -x[0])


def map_columns(
    columns: list[str],
    header_units: dict[str, str],
    stats: dict[str, ColStats],
    hints: dict[str, str],
    time_columns: set[str],
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    d = load_dictionary()
    overrides = overrides or {}
    rows: list[dict[str, Any]] = []
    claims: dict[str, list[int]] = {}

    for col in columns:
        shown = clean_text(col)
        base: dict[str, Any] = {
            "original_name": col, "display_name": shown, "canonical_id": None, "source_unit": None,
            "canonical_unit": None, "confidence": 0.0, "method": "none", "status": "unmapped",
            "candidates": [], "note": "", "confirmed_by": "auto",
        }
        st = stats.get(col, ColStats())
        name, unit = split_header(col, header_units.get(col))
        base["source_unit"] = unit or None
        nname = norm_name(name)

        if col in time_columns:
            base.update(status="time", method="time", confidence=1.0, note="time axis")
            rows.append(base)
            continue
        if col in overrides:
            ov = overrides[col]
            cid = ov.get("canonical_id")
            base.update(method="user", confirmed_by="user", confidence=1.0)
            if cid and cid in d.params:
                p = d.params[cid]
                su = canon_unit(ov.get("unit")) if ov.get("unit") else (unit or p.unit)
                base.update(canonical_id=cid, canonical_unit=p.unit, source_unit=su or None, status="confirmed")
            else:
                base.update(status="unmapped", note="user marked as custom column")
            rows.append(base)
            continue
        if not st.numeric:
            base.update(note="non-numeric column kept as custom")
            rows.append(base)
            continue

        # candidate scoring
        cand: list[tuple[float, str, str]] = []  # (score, id, method)
        if nname in hints:
            hid = hints[nname]
            if hid == "":
                base.update(status="unmapped", method="hint", confidence=0.95, note="source-specific non-vehicle column")
                rows.append(base)
                continue
            cand.append((0.95, hid, "hint"))
        for pid in d.alias_index.get(nname, []):
            cand.append((0.95 if len(d.alias_index[nname]) == 1 else 0.7, pid, "alias"))
        if not cand:
            for sc, pid in _fuzzy(nname, d)[:3]:
                cand.append((sc, pid, "fuzzy"))
        if not cand and nname in d.ambiguous_names:
            guesses = _AMBIGUOUS_GUESSES.get(nname.split()[0], [])
            base.update(status="needs_confirmation", method="ambiguous", confidence=0.4,
                        note="generic column name; say what it measures and its unit",
                        candidates=[{"canonical_id": g, "score": 0.0} for g in guesses])
            rows.append(base)
            continue
        if not cand:
            rows.append(base)
            continue
        cand.sort(key=lambda x: -x[0])
        score, pid, method = cand[0]
        p = d.params[pid]
        notes: list[str] = []

        # unit evidence
        cu = canon_unit(unit) if unit else ""
        conv = conversion(cu, p.unit) if cu else None
        if cu and conv is None and not (cu == p.unit):
            score -= 0.35
            notes.append(f"unit {cu} incompatible with {p.unit}")
            conv = (1.0, 0.0)
        elif cu:
            score = min(1.0, score + 0.03)
        scale, offset = conv or (1.0, 0.0)

        # plausibility evidence
        pl_ev = _plausible(p, st, scale, offset)
        if pl_ev is not None:
            if pl_ev >= 0.95:
                score = min(1.0, score + 0.02)
            elif pl_ev < 0.5:
                score -= 0.3
                notes.append("values fall outside the plausible range for this parameter")

        needs_confirm = False
        if not cu and (pid in _UNIT_SENSITIVE_IDS or p.category in _UNIT_SENSITIVE_CATEGORIES):
            needs_confirm = True
            notes.append("no unit in header; unit must be confirmed")
        if nname in d.ambiguous_names:
            needs_confirm = True
            notes.append("generic column name")
            score = min(score, 0.7)
        if p.bank and not _BANK_TOKEN.search(nname) and not any(_BANK_TOKEN.search(a) for a in [nname]) and method != "hint":
            needs_confirm = True
            notes.append("bank/sensor not specified")
        if pid in ("afr", "lambda") and method != "hint":
            notes.append(f"fuel assumption for {p.id}: stoichiometric AFR depends on fuel type")
        if pid == "map" and not cu:
            notes.append("MAP: absolute kPa vs gauge boost unclear")

        score = max(0.0, min(1.0, score))
        if score < UNMAPPED_MAX:
            status = "unmapped"
        elif score >= CONFIRMED_MIN and not needs_confirm:
            status = "confirmed"
        else:
            status = "needs_confirmation"
        base.update(
            canonical_id=pid if status != "unmapped" else None, canonical_unit=p.unit, confidence=round(score, 3),
            method=method, status=status, note="; ".join(notes),
            candidates=[{"canonical_id": c[1], "score": round(c[0], 2)} for c in cand[:3]],
        )
        if status == "unmapped":
            base["candidates"] = base["candidates"]
        rows.append(base)
        if base["canonical_id"]:
            claims.setdefault(base["canonical_id"], []).append(len(rows) - 1)

    # collisions: best score keeps the id, others need confirmation
    for pid, idxs in claims.items():
        if len(idxs) > 1:
            idxs.sort(key=lambda i: -rows[i]["confidence"])
            for i in idxs:
                r = rows[i]
                if r["method"] != "user":
                    r["status"] = "needs_confirmation"
                    r["note"] = (r["note"] + "; " if r["note"] else "") + f"multiple columns compete for {pid}"
    return rows


def parse_set_args(items: list[str]) -> dict[str, dict[str, Any]]:
    """--set "Original Name=canonical_id[:unit]" ; canonical_id may be 'custom'."""
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        if "=" not in it:
            continue
        k, v = it.rsplit("=", 1)
        cid, _, unit = v.partition(":")
        out[k.strip()] = {"canonical_id": None if cid.strip() in ("", "custom", "none") else cid.strip(), "unit": unit.strip() or None}
    return out
