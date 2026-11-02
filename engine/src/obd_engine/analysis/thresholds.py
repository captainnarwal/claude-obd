"""Layered thresholds (design doc 9.4). Phase 1 ships only the generic tier; the lookup order is already in place."""

from __future__ import annotations

from typing import Any

# parameter -> list of (name, op, limit, unit, severity)
GENERIC: dict[str, list[tuple[str, str, float, str, str]]] = {
    "ect": [("coolant temperature high", ">", 110.0, "°C", "attention"), ("coolant temperature very high", ">", 120.0, "°C", "warning")],
    "oil_temp": [("oil temperature high", ">", 140.0, "°C", "attention")],
    "control_module_voltage": [("system voltage low while running", "<", 11.5, "V", "attention")],
    "iat": [("intake air temperature high", ">", 70.0, "°C", "attention")],
}


def limits_for(param: str, vehicle_profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Vehicle-specific > engine-family > generic. Phase 1 has no vehicle/engine profiles, so only generic applies."""
    out = []
    for name, op, lim, unit, sev in GENERIC.get(param, []):
        out.append({"name": name, "op": op, "limit": lim, "unit": unit, "severity": sev, "tier": "generic",
                    "source": "generic sanity range; may not apply to your engine"})
    return out
