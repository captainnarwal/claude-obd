"""Layered thresholds (design doc 9.4): vehicle-specific > engine-family > generic.

Data lives in reference/thresholds/*.yaml. For each parameter the first tier that has any limit wins, so a vehicle
limit replaces the family limits, which replace the generic ones.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

from obd_engine.reference.loader import reference_dir

TURBO_WORDS = ("turbo", "supercharg", "forced", "boost")


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, Any], dict[str, Any]]:
    d = reference_dir() / "thresholds"
    generic = yaml.safe_load((d / "generic.yaml").read_text(encoding="utf-8"))
    families = yaml.safe_load((d / "engine_families.yaml").read_text(encoding="utf-8"))
    return generic, families


def family_for(engine: dict[str, Any] | None) -> str | None:
    """Map an engine profile (fuel, aspiration) to a family id, or None when it cannot be told."""
    if not engine:
        return None
    fuel = str(engine.get("fuel") or "").lower()
    asp = str(engine.get("aspiration") or "").lower()
    if "diesel" in fuel:
        return "diesel"
    if any(w in asp for w in TURBO_WORDS):
        return "turbo_petrol"
    if asp in ("na", "n/a", "naturally aspirated", "natural"):
        return "na_petrol"
    return None


def threshold_version() -> str:
    g, f = _load()
    return f"{g['version']}.{f['version']}"


def profile_key(vehicle: dict[str, Any] | None) -> dict[str, Any]:
    """The parts of a vehicle that change thresholds - part of analysis cache keys."""
    if not vehicle:
        return {}
    return {"family": family_for(vehicle.get("engine")), "limits": vehicle.get("limits") or {}, "tv": threshold_version()}


def _entries(items: list[dict[str, Any]], tier: str, source: str) -> list[dict[str, Any]]:
    return [{"name": e["name"], "op": e["op"], "limit": float(e["limit"]), "unit": e.get("unit", ""),
             "severity": e.get("severity", "attention"), "tier": tier, "source": source} for e in items]


def limits_for(param: str, vehicle_profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    generic, families = _load()
    if vehicle_profile:
        own = (vehicle_profile.get("limits") or {}).get(param)
        if own:
            name = vehicle_profile.get("nickname") or vehicle_profile.get("id") or "vehicle"
            return _entries([{"name": f"{param} limit", **e} for e in own], "vehicle", f"user-entered limit for {name}")
        fam = families["families"].get(family_for(vehicle_profile.get("engine")) or "")
        if fam and param in fam["limits"]:
            return _entries(fam["limits"][param], "engine-family", fam["source"])
    return _entries(generic["limits"].get(param, []), "generic", generic["source"])
