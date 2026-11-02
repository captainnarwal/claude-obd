"""`vehicle` and `session` commands (design doc 6.2, 10.2): profiles that select thresholds and group datasets."""

from __future__ import annotations

import argparse
from typing import Any, Callable

from obd_engine.analysis.thresholds import family_for
from obd_engine.cli.envelope import Envelope, ObdError, clean_text
from obd_engine.ingestion import service as ing
from obd_engine.storage.catalog import Catalog, new_id
from obd_engine.storage.workspace import Workspace

ENGINE_FIELDS = {"displacement": float, "cylinders": int, "layout": str, "aspiration": str, "fuel": str, "injection": str,
                 "redline": float, "engine_state": str}
DRIVETRAIN_FIELDS = {"transmission": str, "drivetrain": str, "tire_size": str, "final_drive": float, "mass_kg": float}
SCALARS = ("nickname", "make", "model", "year", "vin", "notes")
ASPIRATION = ("na", "turbo", "supercharged", "twin-charged")


def _mask_vin(vin: str | None) -> str | None:
    return None if not vin else "*" * max(0, len(vin) - 6) + vin[-6:]


def _public(v: dict[str, Any]) -> dict[str, Any]:
    out = {k: (clean_text(str(v[k]), 80) if isinstance(v.get(k), str) else v.get(k)) for k in ("id", "nickname", "make", "model", "year", "notes")}
    out.update(vin=_mask_vin(v.get("vin")), engine=v["engine"], drivetrain=v["drivetrain"], limits=v["limits"],
               threshold_family=family_for(v["engine"]) or "none (generic limits apply)")
    return out


def _add_fields(sp: argparse.ArgumentParser) -> None:
    for k in SCALARS:
        sp.add_argument(f"--{k}", type=int if k == "year" else str)
    for k, typ in {**ENGINE_FIELDS, **DRIVETRAIN_FIELDS}.items():
        sp.add_argument(f"--{k.replace('_', '-')}", dest=k, type=typ)
    sp.add_argument("--limit", action="append", help='"param:op:value[:unit[:severity]]", e.g. "ect:>:112:°C:attention" (repeatable)')
    sp.add_argument("--clear-limit", action="append", help="remove vehicle-specific limits for a parameter")


def _parse_limit(spec: str) -> tuple[str, dict[str, Any]]:
    parts = spec.split(":")
    if len(parts) < 3 or parts[1] not in (">", "<"):
        raise ObdError("USAGE", f"Bad --limit {spec!r}.", 'Use "param:op:value[:unit[:severity]]" with op > or <.', exit_code=2)
    try:
        value = float(parts[2])
    except ValueError:
        raise ObdError("USAGE", f"Limit value is not a number: {parts[2]!r}", exit_code=2) from None
    sev = parts[4] if len(parts) > 4 else "attention"
    if sev not in ("attention", "warning"):
        raise ObdError("USAGE", f"Severity must be attention or warning, got {sev!r}.", exit_code=2)
    return parts[0], {"op": parts[1], "limit": value, "unit": parts[3] if len(parts) > 3 else "", "severity": sev}


def _collect(ns: argparse.Namespace, current: dict[str, Any] | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {k: getattr(ns, k) for k in SCALARS if getattr(ns, k, None) is not None}
    eng = {k: getattr(ns, k) for k in ENGINE_FIELDS if getattr(ns, k, None) is not None}
    if "aspiration" in eng:
        eng["aspiration"] = eng["aspiration"].lower()
        if eng["aspiration"] not in ASPIRATION:
            raise ObdError("USAGE", f"aspiration must be one of {', '.join(ASPIRATION)}.", exit_code=2)
    drv = {k: getattr(ns, k) for k in DRIVETRAIN_FIELDS if getattr(ns, k, None) is not None}
    if eng:
        f["engine"] = eng
    if drv:
        f["drivetrain"] = drv
    limits = dict((current or {}).get("limits") or {})
    for spec in ns.limit or []:
        p, lim = _parse_limit(spec)
        limits.setdefault(p, [])
        limits[p] = [e for e in limits[p] if e["op"] != lim["op"]] + [lim]
    for p in ns.clear_limit or []:
        limits.pop(p, None)
    if ns.limit or ns.clear_limit:
        f["limits"] = limits
    return f


def vehicle_setup(sp):
    """Manage vehicle / engine profiles (they select threshold tiers)."""
    sp.add_argument("action", choices=["add", "set", "show", "list", "assign", "delete"])
    sp.add_argument("target", nargs="?", help="vehicle id/nickname (set, show, assign, delete)")
    sp.add_argument("dataset", nargs="?", help="dataset to assign (assign)")
    sp.add_argument("--yes", action="store_true")
    _add_fields(sp)


def _need_vehicle(cat: Catalog, ref: str | None) -> dict[str, Any]:
    if not ref:
        raise ObdError("USAGE", "Give a vehicle id or nickname.", exit_code=2)
    v = cat.find_vehicle(ref)
    if not v:
        raise ObdError("VEHICLE_NOT_FOUND", f"No vehicle {ref!r}.", "Run `vehicle list`.", exit_code=3)
    return v


def vehicle_cmd(ns) -> Envelope:
    ws = Workspace.current()
    cat = Catalog(ws)
    try:
        if ns.action == "list":
            vs = [_public(v) for v in cat.list_vehicles()]
            return Envelope(summary=f"{len(vs)} vehicle(s).", data={"vehicles": vs})
        if ns.action == "add":
            fields = _collect(ns)
            if not any(fields.get(k) for k in ("nickname", "make", "model")):
                raise ObdError("USAGE", "Give at least --nickname, --make or --model.", exit_code=2)
            vid = new_id("veh")
            cat.add_vehicle(vid, fields)
            v = _public(cat.get_vehicle(vid))  # type: ignore[arg-type]
            return Envelope(summary=f"Added vehicle {vid}. Thresholds: {v['threshold_family']}.", data={"vehicle": v},
                            caveats=["Set --aspiration and --fuel to select engine-family limits instead of generic ones."] if v["threshold_family"].startswith("none") else [])
        v = _need_vehicle(cat, ns.target)
        if ns.action == "show":
            return Envelope(summary=f"Vehicle {v['id']}.", data={"vehicle": _public(v)})
        if ns.action == "set":
            fields = _collect(ns, v)
            if not fields:
                raise ObdError("USAGE", "Nothing to change.", exit_code=2)
            cat.update_vehicle(v["id"], fields)
            nv = _public(cat.get_vehicle(v["id"]))  # type: ignore[arg-type]
            return Envelope(summary=f"Updated vehicle {v['id']}. Cached analyses that depend on it will be recomputed.", data={"vehicle": nv})
        if ns.action == "assign":
            if not ns.dataset:
                raise ObdError("USAGE", "Give a dataset to assign.", exit_code=2)
            row = ing.resolve_dataset(cat, ns.dataset)
            cat.assign_dataset(row["id"], vehicle_id=v["id"])
            return Envelope(summary=f"Assigned {row['file_name']} to vehicle {v['id']}.", data={"dataset_id": row["id"], "vehicle_id": v["id"]})
        if not ns.yes:  # delete
            raise ObdError("CONFIRMATION_REQUIRED", f"This deletes vehicle {v['id']}; its datasets become unassigned (data is kept).",
                           "Re-run with --yes after the user confirms.", exit_code=2)
        cat.delete_vehicle(v["id"])
        return Envelope(summary=f"Deleted vehicle {v['id']}.", data={"deleted": v["id"]})
    finally:
        cat.close()


def session_setup(sp):
    """Group datasets from one outing/test into a logging session."""
    sp.add_argument("action", choices=["add", "list", "assign"])
    sp.add_argument("target", nargs="?", help="session id/label (assign)")
    sp.add_argument("dataset", nargs="?")
    sp.add_argument("--label")
    sp.add_argument("--vehicle")
    sp.add_argument("--condition", action="append", help='"key=value" e.g. ambient=30C, fuel=95RON, mods=intake (repeatable)')


def session_cmd(ns) -> Envelope:
    ws = Workspace.current()
    cat = Catalog(ws)
    try:
        if ns.action == "list":
            ss = cat.list_sessions()
            return Envelope(summary=f"{len(ss)} session(s).", data={"sessions": ss})
        if ns.action == "add":
            veh = _need_vehicle(cat, ns.vehicle) if ns.vehicle else None
            cond = {}
            for c in ns.condition or []:
                k, _, val = c.partition("=")
                if not val:
                    raise ObdError("USAGE", f"Bad --condition {c!r}; use key=value.", exit_code=2)
                cond[k.strip()] = val.strip()
            sid = new_id("ses")
            cat.add_session(sid, veh["id"] if veh else None, ns.label, cond)
            return Envelope(summary=f"Added session {sid}.", data={"session": cat.find_session(sid)})
        s = cat.find_session(ns.target) if ns.target else None
        if not s:
            raise ObdError("SESSION_NOT_FOUND", f"No session {ns.target!r}.", "Run `session list`.", exit_code=3)
        if not ns.dataset:
            raise ObdError("USAGE", "Give a dataset to assign.", exit_code=2)
        row = ing.resolve_dataset(cat, ns.dataset)
        cat.assign_dataset(row["id"], session_id=s["id"])
        return Envelope(summary=f"Assigned {row['file_name']} to session {s['id']}.", data={"dataset_id": row["id"], "session_id": s["id"], "vehicle_id": s["vehicle_id"]})
    finally:
        cat.close()


COMMANDS: dict[str, tuple[Callable, Callable[[argparse.Namespace], Envelope]]] = {
    "vehicle": (vehicle_setup, vehicle_cmd),
    "session": (session_setup, session_cmd),
}
