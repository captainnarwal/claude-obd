"""Thin command handlers. Each validates args, calls one service, returns an Envelope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from obd_engine.cli.envelope import Envelope, ObdError, clean_text
from obd_engine.ingestion import service as ing
from obd_engine.ingestion.mapping.scorer import parse_set_args
from obd_engine.reference.loader import load_dictionary
from obd_engine.security.checks import TELEMETRY_EXT
from obd_engine.storage.catalog import Catalog
from obd_engine.storage.workspace import Workspace

Handler = Callable[[argparse.Namespace], Envelope]


def _ws() -> Workspace:
    return Workspace.current()


def _overrides(ns: argparse.Namespace) -> dict[str, dict[str, Any]]:
    ov = parse_set_args(getattr(ns, "set", None) or [])
    mf = getattr(ns, "map_file", None)
    if mf:
        data = json.loads(Path(mf).read_text(encoding="utf-8"))
        for k, v in data.items():
            ov[k] = {"canonical_id": v.get("canonical_id") if isinstance(v, dict) else v, "unit": v.get("unit") if isinstance(v, dict) else None}
    return ov


# ---- scan -------------------------------------------------------------------
def scan_setup(sp):
    """List telemetry-looking files in a folder."""
    sp.add_argument("folder", nargs="?", default=".")
    sp.add_argument("--recursive", action="store_true")


def scan(ns) -> Envelope:
    from obd_engine.ingestion.discovery.sniff import detect_format

    ws = _ws()
    folder = Path(ns.folder).expanduser()
    if not folder.is_absolute():
        folder = ws.root / folder
    if not folder.is_dir():
        raise ObdError("FILE_NOT_FOUND", f"Folder not found: {ns.folder}")
    ingested = {}
    if ws.catalog_path.exists():
        cat = Catalog(ws)
        ingested = {r["file_hash"][:0] + r["file_name"]: r["id"] for r in cat.list_datasets()}
        cat.close()
    it = folder.rglob("*") if ns.recursive else folder.iterdir()
    files = []
    for p in sorted(it):
        if ".obd" in p.parts or "obd-reports" in p.parts or not p.is_file() or p.suffix.lower() not in TELEMETRY_EXT:
            continue
        if p.name.lower() in ("readme.txt", "license.txt", "requirements.txt"):
            continue
        files.append({
            "name": clean_text(str(p.relative_to(folder)), 120), "size_mb": round(p.stat().st_size / 1024**2, 2),
            "format": detect_format(p), "dataset_id": ingested.get(p.name),
        })
        if len(files) >= 100:
            break
    return Envelope(
        summary=f"{len(files)} telemetry-looking file(s) in {folder.name or folder}." + (" None yet - drop an export here or give a path." if not files else ""),
        data={"files": files, "folder": str(folder)}, truncated={"reason": "first 100 files"} if len(files) >= 100 else None,
    )


# ---- inspect / ingest / map -------------------------------------------------
def inspect_setup(sp):
    """Read-only profile and proposed mappings for a file."""
    sp.add_argument("path")
    sp.add_argument("--sheet")


def inspect_cmd(ns) -> Envelope:
    return ing.inspect_file(_ws(), ns.path, ns.sheet)


def ingest_setup(sp):
    """Parse, map, normalize and store a file in .obd/."""
    sp.add_argument("path")
    sp.add_argument("--sheet")
    sp.add_argument("--label")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--set", action="append", help='"Column=canonical_id[:unit]" (repeatable)')
    sp.add_argument("--map-file", help="JSON file of overrides")


def ingest_cmd(ns) -> Envelope:
    ws = _ws()
    cat = Catalog(ws)
    try:
        return ing.ingest_file(ws, cat, ns.path, _overrides(ns) or None, ns.label, ns.force, ns.sheet)
    finally:
        cat.close()


def map_setup(sp):
    """Confirm or correct parameter mappings (re-normalizes from raw)."""
    sp.add_argument("dataset")
    sp.add_argument("--set", action="append", help='"Column=canonical_id[:unit]" ; use custom for none')
    sp.add_argument("--map-file")
    sp.add_argument("--list-parameters", action="store_true", help="list canonical ids")


def map_cmd(ns) -> Envelope:
    if ns.list_parameters:
        d = load_dictionary()
        return Envelope(summary=f"{len(d.params)} canonical parameters.", data={
            "parameters": [{"id": p.id, "name": p.name, "unit": p.unit, "category": p.category} for p in d.params.values()]})
    ov = _overrides(ns)
    if not ov:
        raise ObdError("USAGE", "Nothing to set.", 'Use --set "Column=canonical_id:unit".', exit_code=2)
    ws = _ws()
    cat = Catalog(ws)
    try:
        return ing.remap(ws, cat, ns.dataset, ov)
    finally:
        cat.close()


# ---- list / overview / delete / clean --------------------------------------
def list_setup(sp):
    """List datasets in this folder."""


def list_cmd(ns) -> Envelope:
    ws = _ws()
    if not ws.catalog_path.exists():
        return Envelope(summary="No datasets yet.", data={"datasets": []})
    cat = Catalog(ws)
    rows = [{
        "dataset_id": r["id"], "file": clean_text(r["file_name"], 80), "rows": r["row_count"],
        "duration_s": r["duration_s"], "label": r["label"],
    } for r in cat.list_datasets()]
    cat.close()
    return Envelope(summary=f"{len(rows)} dataset(s).", data={"datasets": rows}, units={"duration_s": "s"})


def overview_setup(sp):
    """Dataset metadata, parameters and quality."""
    sp.add_argument("dataset")


def overview_cmd(ns) -> Envelope:
    ws = _ws()
    cat = Catalog(ws)
    try:
        row = ing.resolve_dataset(cat, ns.dataset)
        return ing.dataset_overview_envelope(ws, cat, row["id"])
    finally:
        cat.close()


def delete_setup(sp):
    """Delete one dataset (or --all)."""
    sp.add_argument("dataset", nargs="?")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--yes", action="store_true")


def delete_cmd(ns) -> Envelope:
    import shutil

    ws = _ws()
    if ns.all:
        if not ns.yes:
            raise ObdError("CONFIRMATION_REQUIRED", "This deletes all processed data in .obd/ (originals and reports untouched).", "Re-run with --yes after the user confirms.", exit_code=2)
        existed = ws.exists()
        ws.wipe()
        return Envelope(summary="Removed .obd/ workspace." if existed else "Nothing to clean.", data={"deleted": ".obd"})
    if not ns.dataset:
        raise ObdError("USAGE", "Give a dataset or --all.", exit_code=2)
    cat = Catalog(ws)
    try:
        row = ing.resolve_dataset(cat, ns.dataset)
        if not ns.yes:
            raise ObdError("CONFIRMATION_REQUIRED", f"This deletes processed data for {row['file_name']} ({row['id']}).", "Re-run with --yes after the user confirms.", exit_code=2)
        shutil.rmtree(ws.dataset_dir(row["id"]), ignore_errors=True)
        cat.delete_dataset(row["id"])
        return Envelope(summary=f"Deleted {row['id']}.", data={"deleted": row["id"]})
    finally:
        cat.close()


# ---- selftest ---------------------------------------------------------------
def selftest_setup(sp):
    """Run the full pipeline on the bundled sample log."""


def selftest_cmd(ns) -> Envelope:
    from obd_engine.selftest import run_selftest

    return run_selftest()


COMMANDS: dict[str, tuple[Callable, Handler]] = {
    "scan": (scan_setup, scan),
    "inspect": (inspect_setup, inspect_cmd),
    "ingest": (ingest_setup, ingest_cmd),
    "map": (map_setup, map_cmd),
    "list": (list_setup, list_cmd),
    "overview": (overview_setup, overview_cmd),
    "delete": (delete_setup, delete_cmd),
    "clean": (lambda sp: (sp.add_argument("--yes", action="store_true"), sp.set_defaults(all=True, dataset=None)) and None, delete_cmd),
    "selftest": (selftest_setup, selftest_cmd),
}

# analysis / chart / report commands are registered by their modules (see bottom of obd_engine.cli.analysis_commands)
from obd_engine.cli import analysis_commands  # noqa: E402

COMMANDS.update(analysis_commands.COMMANDS)

from obd_engine.cli import vehicle_commands  # noqa: E402

COMMANDS.update(vehicle_commands.COMMANDS)
