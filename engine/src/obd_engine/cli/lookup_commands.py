"""`lookup` command: PID info, parameter explanations and DTC structure decoding (no dataset needed)."""

from __future__ import annotations

import argparse
from typing import Callable

from obd_engine.cli.envelope import Envelope
from obd_engine.reference import lookup as ref
from obd_engine.storage.catalog import Catalog
from obd_engine.storage.workspace import Workspace


def lookup_setup(sp):
    """Explain a parameter, a Mode 01 PID, or the structure of a trouble code."""
    sp.add_argument("kind", choices=["dtc", "parameter", "pid"])
    sp.add_argument("id", help="P0171 | ltft_b1 or an alias | 0C or '01 0C'")
    sp.add_argument("--vehicle", help="vehicle id/nickname, to show that vehicle's limits")


def lookup_cmd(ns) -> Envelope:
    if ns.kind == "dtc":
        d = ref.decode_dtc(ns.id)
        return Envelope(summary=f"{d['code']}: {d['system']}; {d['scope']}" + (f"; topic: {d['topic']}" if d.get("topic") else "") + ".", data=d,
                        caveats=d["notes"], method="structure decoding only; no official description text is shipped")
    if ns.kind == "pid":
        d = ref.lookup_pid(ns.id)
        return Envelope(summary=f"Mode 01 PID {d['pid']}: {d['name']} ({d['unit'] or 'no unit'}).", data=d, method="SAE J1979 formula")
    veh = None
    if ns.vehicle:
        ws = Workspace.current()
        if ws.catalog_path.exists():
            cat = Catalog(ws)
            try:
                veh = cat.find_vehicle(ns.vehicle)
            finally:
                cat.close()
    d = ref.lookup_parameter(ns.id, veh)
    return Envelope(summary=f"{d['name']} ({d['canonical_unit'] or 'no unit'}): {d['meaning']}", data=d, method="parameter dictionary and written explanations")


COMMANDS: dict[str, tuple[Callable, Callable[[argparse.Namespace], Envelope]]] = {"lookup": (lookup_setup, lookup_cmd)}
