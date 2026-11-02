"""Parameter dictionary loader and unit conversion (data lives in <repo>/reference/*.yaml)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from obd_engine.cli.envelope import ObdError


def reference_dir() -> Path:
    env = os.environ.get("OBD_REFERENCE_DIR")
    candidates = [Path(env)] if env else []
    here = Path(__file__).resolve()
    candidates += [here.parent.parent / "_reference"]  # installed wheel
    candidates += [p / "reference" for p in here.parents]  # repo checkout / plugin root
    for c in candidates:
        if (c / "parameters.yaml").exists():
            return c
    raise ObdError("REFERENCE_MISSING", "parameters.yaml not found", "Reinstall with /obd setup.", exit_code=4)


@dataclass(frozen=True)
class Param:
    id: str
    name: str
    category: str
    unit: str
    range: tuple[float, float]
    aliases: tuple[str, ...]
    pid: str = ""
    kind: str = "continuous"
    bank: bool = False
    ambiguous_unitless: bool = False


@dataclass
class Dictionary:
    version: str
    params: dict[str, Param] = field(default_factory=dict)
    alias_index: dict[str, list[str]] = field(default_factory=dict)
    ambiguous_names: set[str] = field(default_factory=set)


def norm_name(s: str) -> str:
    """Lowercase, strip punctuation to single spaces."""
    s = s.lower().replace("_", " ")
    s = re.sub(r"[^a-z0-9/λ%.\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=1)
def load_dictionary() -> Dictionary:
    raw: dict[str, Any] = yaml.safe_load((reference_dir() / "parameters.yaml").read_text(encoding="utf-8"))
    d = Dictionary(version=str(raw["version"]), ambiguous_names=set(raw.get("ambiguous_names", [])))
    for e in raw["parameters"]:
        p = Param(
            id=e["id"], name=e["name"], category=e["category"], unit=e.get("unit", ""),
            range=(float(e["range"][0]), float(e["range"][1])),
            aliases=tuple(norm_name(a) for a in e.get("aliases", [])), pid=e.get("pid", ""),
            kind=e.get("kind", "continuous"), bank=bool(e.get("bank", False)),
            ambiguous_unitless=bool(e.get("ambiguous_unitless", False)),
        )
        d.params[p.id] = p
        for a in {*p.aliases, norm_name(p.name), norm_name(p.id)}:
            d.alias_index.setdefault(a, [])
            if p.id not in d.alias_index[a]:
                d.alias_index[a].append(p.id)
    return d


# ---- units -----------------------------------------------------------------

_UNIT_ALIASES = {
    "c": "°C", "°c": "°C", "degc": "°C", "deg c": "°C", "celsius": "°C", "℃": "°C",
    "f": "°F", "°f": "°F", "degf": "°F", "deg f": "°F", "fahrenheit": "°F", "℉": "°F",
    "meters/second": "m/s", "hp": "hp", "nm": "Nm", "kph": "km/h", "kmh": "km/h", "km/h": "km/h", "mph": "mph", "m/s": "m/s",
    "kpa": "kPa", "psi": "psi", "bar": "bar", "mbar": "mbar", "hpa": "hPa", "inhg": "inHg", "mmhg": "mmHg", "pa": "Pa",
    "g/s": "g/s", "g/sec": "g/s", "gps": "g/s", "lb/min": "lb/min", "kg/h": "kg/h",
    "l/h": "L/h", "lph": "L/h", "gal/h": "gal/h", "gph": "gal/h",
    "mi": "mi", "miles": "mi", "km": "km", "ft": "ft", "m": "m", "meters": "m",
    "v": "V", "mv": "mV", "volts": "V", "%": "%", "pct": "%", "percent": "%",
    "rpm": "rpm", "s": "s", "sec": "s", "seconds": "s", "ms": "ms", "min": "min",
    "deg": "°", "°": "°", "degrees": "°", "λ": "λ", "lambda": "λ", ":1": ":1", "afr": ":1",
    "g": "g", "a": "A", "amps": "A",
}


def canon_unit(u: str | None) -> str:
    if not u:
        return ""
    key = u.strip().lower().replace("deg ", "deg")
    return _UNIT_ALIASES.get(key, u.strip())


# (from, to) -> (scale, offset): to_value = from_value * scale + offset
_CONV: dict[tuple[str, str], tuple[float, float]] = {
    ("°F", "°C"): (5 / 9, -32 * 5 / 9),
    ("mph", "km/h"): (1.609344, 0.0),
    ("m/s", "km/h"): (3.6, 0.0),
    ("psi", "kPa"): (6.894757, 0.0),
    ("bar", "kPa"): (100.0, 0.0),
    ("mbar", "kPa"): (0.1, 0.0),
    ("inHg", "kPa"): (3.386389, 0.0),
    ("mmHg", "kPa"): (0.133322, 0.0),
    ("Pa", "kPa"): (0.001, 0.0),
    ("hPa", "kPa"): (0.1, 0.0),
    ("lb/min", "g/s"): (7.559873, 0.0),
    ("kg/h", "g/s"): (1000 / 3600, 0.0),
    ("gal/h", "L/h"): (3.785411784, 0.0),
    ("mi", "km"): (1.609344, 0.0),
    ("ft", "m"): (0.3048, 0.0),
    ("mV", "V"): (0.001, 0.0),
    ("ms", "s"): (0.001, 0.0),
    ("min", "s"): (60.0, 0.0),
}


def conversion(src: str, dst: str) -> tuple[float, float] | None:
    """Return (scale, offset) converting src->dst, (1,0) if same, None if incompatible."""
    if src == dst or not src or not dst:
        return (1.0, 0.0)
    return _CONV.get((src, dst))


def convertible_units(dst: str) -> set[str]:
    return {dst, *(a for (a, b) in _CONV if b == dst)}
