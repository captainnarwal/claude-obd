"""Reference lookups (design doc 8.1 `lookup`): PID info, parameter explanations, DTC structure decoding.

These describe things. They never say a code is stored in the vehicle or that a value in a log is abnormal.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import yaml

from obd_engine.analysis.thresholds import limits_for
from obd_engine.cli.envelope import ObdError, clean_text
from obd_engine.reference.loader import load_dictionary, norm_name, reference_dir

DTC_NOT_A_DIAGNOSIS = ("This describes what the code's structure and range mean. It does not say the code is stored in your vehicle, "
                       "and a code names a monitored condition, not a failed part.")


@lru_cache(maxsize=1)
def _pids() -> dict[str, Any]:
    return yaml.safe_load((reference_dir() / "obd_pids.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _dtc() -> dict[str, Any]:
    return yaml.safe_load((reference_dir() / "dtc_structure.yaml").read_text(encoding="utf-8"))


def reference_versions() -> dict[str, str]:
    return {"pids": str(_pids()["version"]), "dtc_structure": str(_dtc()["version"])}


def decode_dtc(code: str) -> dict[str, Any]:
    ref = _dtc()
    c = re.sub(r"[\s-]", "", code or "").upper()
    if not re.match(ref["format"], c):
        raise ObdError("INVALID_DTC", f"'{clean_text(code, 20)}' is not a valid trouble code.", "Codes look like P0171: a letter P/C/B/U, then four hex digits (second digit 0-3).", exit_code=2)
    system, second, third = c[0], c[1], c[2]
    out: dict[str, Any] = {"code": c, "system": ref["systems"][system], "scope": ref["scope"][system][second]}
    if system == "P":
        out["subsystem"] = ref["p_subsystem"].get(third, "unspecified")
        # families are written for the generic P0xxx block; the numeric part is the last three hex digits
        if second in ("0", "2") and re.match(r"^[0-9]{3}$", c[2:]):
            n = int(c[2:])
            fam = next((f for f in ref["families"] if f["from"] <= n <= f["to"]), None) if second == "0" else None
            if fam:
                out["topic"] = fam["topic"]
                out["related_parameters"] = _params_view(fam["params"])
            if second == "0" and 301 <= n <= 312:
                out["cylinder"] = n - 300
    out["notes"] = [DTC_NOT_A_DIAGNOSIS]
    if out["scope"].startswith("manufacturer"):
        out["notes"].append("Manufacturer-specific codes mean different things on different makes; look up the code for this make before drawing conclusions.")
    out["source"] = f"code structure per the J2012/J1979 numbering scheme; topic text paraphrased (reference dtc_structure v{ref['version']})"
    return out


def _params_view(ids: list[str]) -> list[dict[str, str]]:
    d = load_dictionary()
    return [{"id": i, "name": d.params[i].name} for i in ids if i in d.params]


def lookup_pid(pid: str) -> dict[str, Any]:
    p = re.sub(r"^(0x|01\s*)", "", (pid or "").strip(), flags=re.I).replace(" ", "").upper()
    if len(p) == 4 and p.startswith("01"):
        p = p[2:]
    p = p.zfill(2)
    hit = next((e for e in _pids()["mode_01"] if e["pid"] == p), None)
    if not hit:
        known = ", ".join(e["pid"] for e in _pids()["mode_01"])
        raise ObdError("PID_NOT_FOUND", f"Mode 01 PID {clean_text(pid, 12)} is not in the reference.", f"Known PIDs: {known}", exit_code=3)
    out = {"mode": "01", **hit}
    if hit.get("canonical_id") in load_dictionary().params:
        out["parameter"] = load_dictionary().params[hit["canonical_id"]].name
    out["source"] = "SAE J1979 Mode 01 definition (formula); description written for this project"
    return out


def lookup_parameter(ref: str, vehicle: dict[str, Any] | None = None) -> dict[str, Any]:
    d = load_dictionary()
    key = norm_name(ref or "")
    pid = ref if ref in d.params else None
    if pid is None:
        cands = d.alias_index.get(key, [])
        if len(cands) == 1:
            pid = cands[0]
        elif len(cands) > 1:
            raise ObdError("AMBIGUOUS_PARAMETER", f"'{clean_text(ref, 40)}' matches several parameters.", "Use a canonical id.", exit_code=2, data={"candidates": cands})
    if pid is None:
        near = [i for i, p in d.params.items() if key and (key in norm_name(p.name) or key in i)][:8]
        raise ObdError("PARAMETER_NOT_FOUND", f"No parameter '{clean_text(ref, 40)}' in the dictionary.", "Try `map --list-parameters`.", exit_code=3, data={"near_matches": near})
    p = d.params[pid]
    out: dict[str, Any] = {"id": p.id, "name": p.name, "category": p.category, "canonical_unit": p.unit, "kind": p.kind,
                           "physical_plausibility_range": list(p.range), "bank_aware": p.bank,
                           "also_known_as": list(p.aliases[:8])}
    pids = _pids()
    entry = next((e for e in pids["mode_01"] if e.get("canonical_id") == pid), None)
    note = pids["parameters"].get(pid)
    if entry:
        out.update(pid=f"01 {entry['pid']}", formula=entry["formula"], meaning=entry["meaning"], tips=entry["tips"])
    elif note:
        out.update(meaning=note["meaning"], tips=note["tips"], pid="no standard Mode 01 PID (enhanced or logger channel)")
    else:
        out["meaning"] = f"{p.name} ({p.category})."
    lims = limits_for(pid, vehicle)
    if lims:
        out["limits_used_by_this_tool"] = [{"name": x["name"], "op": x["op"], "limit": x["limit"], "unit": x["unit"], "tier": x["tier"], "source": x["source"]} for x in lims]
        out["limits_note"] = "These are the limits the analysis modules compare against; a generic tier may not apply to your engine."
    out["source"] = "parameter dictionary; plain-language text written for this project"
    return out
