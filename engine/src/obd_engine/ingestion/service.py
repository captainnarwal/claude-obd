"""Ingestion service: inspect (read-only), ingest, remap."""

from __future__ import annotations

import io
import json
import shutil
import time
from pathlib import Path
from typing import Any

import polars as pl

from obd_engine.cli.envelope import Envelope, ObdError, clean_text
from obd_engine.ingestion.adapters.base import Adapter
from obd_engine.ingestion.adapters.registry import get_adapter, select_adapter
from obd_engine.ingestion.discovery.sniff import NULL_TOKENS, FileProfile, file_hash, sniff_file
from obd_engine.ingestion.mapping.scorer import column_stats, map_columns
from obd_engine.ingestion.normalize.normalize import normalize
from obd_engine.ingestion.normalize.time_axis import detect_time
from obd_engine.reference.loader import load_dictionary
from obd_engine.security.checks import resolve_input
from obd_engine.storage.catalog import Catalog, new_id
from obd_engine.storage.workspace import Workspace

SAMPLE_ROWS = 50_000
MAX_SHOWN_COLUMNS = 60


def read_sample(profile: FileProfile, adapter: Adapter, tmp_dir: Path) -> pl.DataFrame:
    if profile.format == "csv":
        raw = Path(profile.path).read_bytes()[: 8 * 1024 * 1024] if profile.size > 8 * 1024 * 1024 else Path(profile.path).read_bytes()
        text = raw.decode(profile.encoding, errors="replace")
        if profile.size > 8 * 1024 * 1024:
            text = text[: text.rfind("\n")]
        skip = profile.header_row + 1 + (1 if profile.unit_row else 0)
        lines = text.splitlines()
        body = "\n".join(lines[skip:])
        kwargs = dict(
            has_header=False, new_columns=profile.columns, separator=profile.delimiter, null_values=NULL_TOKENS,
            decimal_comma=(profile.decimal == ","), truncate_ragged_lines=True, n_rows=SAMPLE_ROWS,
            infer_schema_length=SAMPLE_ROWS,
        )
        try:
            df = pl.read_csv(io.BytesIO(body.encode("utf-8")), **kwargs)
        except Exception:
            df = pl.read_csv(io.BytesIO(body.encode("utf-8")), infer_schema=False, **{k: v for k, v in kwargs.items() if k != "infer_schema_length"})
        return df.with_row_index("__row_id", offset=1)
    out = tmp_dir / "sample.parquet"
    adapter.parse(profile, out)
    return pl.read_parquet(out).head(SAMPLE_ROWS)


def _mapping_view(m: dict[str, Any]) -> dict[str, Any]:
    v = {
        "column": m["display_name"] if "display_name" in m else clean_text(m["original_name"]),
        "unit": m.get("source_unit"), "maps_to": m.get("canonical_id"), "confidence": m.get("confidence"),
        "status": m.get("status"),
    }
    if m.get("note"):
        v["note"] = clean_text(m["note"], 160)
    return v


def _questions(mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    d = load_dictionary()
    qs = []
    for m in mappings:
        if m["status"] != "needs_confirmation":
            continue
        qs.append({
            "column": clean_text(m["original_name"]), "question": f"What does column '{clean_text(m['original_name'])}' measure, and in what unit?",
            "suggested": {"canonical_id": m["canonical_id"], "name": d.params[m["canonical_id"]].name if m["canonical_id"] in d.params else None,
                          "canonical_unit": m["canonical_unit"]},
            "candidates": m.get("candidates", []), "reason": clean_text(m.get("note", ""), 160),
            "how_to_answer": f"obd map <dataset> --set \"{m['original_name']}=<canonical_id>:<unit>\"",
        })
    return qs


def _prepare(path_str: str, sheet: str | None, ws: Workspace):
    path = resolve_input(path_str, ws.root)
    profile = sniff_file(path, sheet)
    adapter, score = select_adapter(profile)
    return path, profile, adapter, score


def _build_mappings(profile, adapter, df, overrides):
    policy = adapter.time_policy()
    decimal_comma = profile.decimal == ","
    spec = detect_time(df, profile.columns, policy, profile.units, decimal_comma)
    stats = column_stats(df.select([c for c in df.columns if c != "__row_id"]))
    maps = map_columns(
        [c for c in profile.columns], profile.units, stats, adapter.column_hints(),
        {spec.column} if spec.column else set(), overrides,
    )
    return maps, spec, stats


def inspect_file(ws: Workspace, path_str: str, sheet: str | None = None) -> Envelope:
    path, profile, adapter, score = _prepare(path_str, sheet, ws)
    tmp = ws.make_tmp()
    try:
        df = read_sample(profile, adapter, tmp)
        maps, spec, stats = _build_mappings(profile, adapter, df, None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    counts: dict[str, int] = {}
    for m in maps:
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    shown = [_mapping_view(m) for m in maps][:MAX_SHOWN_COLUMNS]
    data = {
        "file": clean_text(profile.file_name, 120), "format": profile.format, "size_mb": round(profile.size / 1024**2, 2),
        "adapter": adapter.id, "adapter_confidence": round(score, 2), "encoding": profile.encoding,
        "delimiter": profile.delimiter if profile.format == "csv" else None, "sheet": profile.sheet, "sheets": profile.sheets or None,
        "columns": len(profile.columns), "est_rows": profile.est_rows, "time_axis": spec.to_dict(),
        "mapping_counts": counts, "mappings": shown, "notes": profile.notes,
    }
    env = Envelope(
        status="needs_input" if counts.get("needs_confirmation") else "ok",
        summary=f"{clean_text(profile.file_name)}: {len(profile.columns)} columns, adapter {adapter.id}, time axis {spec.kind}; "
                f"{counts.get('confirmed', 0)} confirmed, {counts.get('needs_confirmation', 0)} need confirmation, "
                f"{counts.get('unmapped', 0)} custom.",
        data=data, needs_input=_questions(maps),
        method="sampled read of up to 50k rows; deterministic alias/unit/plausibility scoring",
    )
    if len(maps) > MAX_SHOWN_COLUMNS:
        env.truncated = {"reason": f"showing {MAX_SHOWN_COLUMNS} of {len(maps)} columns"}
    if spec.kind == "index":
        env.caveats.append(spec.note)
    return env


def _write_profile(dd: Path, profile: FileProfile, adapter: Adapter, spec_dict: dict[str, Any]) -> None:
    payload = {"profile": profile.to_dict(), "adapter": adapter.id, "time_policy": adapter.time_policy(), "time_spec": spec_dict}
    payload["profile"].pop("sample_rows", None)
    (dd / "profile.json").write_text(json.dumps(payload, default=str), encoding="utf-8")


def _load_profile(dd: Path) -> tuple[FileProfile, Adapter]:
    payload = json.loads((dd / "profile.json").read_text(encoding="utf-8"))
    prof = FileProfile(**{**payload["profile"], "sample_rows": []})
    return prof, get_adapter(payload["adapter"])


def _finish(ws, cat, ds_id, dd, profile, adapter, overrides, label, file_hash_, path, adapter_notes, existing=False):
    raw_path = dd / "raw.parquet"
    sample = pl.scan_parquet(raw_path).head(200_000).collect()
    maps, spec, _ = _build_mappings(profile, adapter, sample, overrides)
    summary = normalize(raw_path, dd / "normalized.parquet", profile, maps, adapter.time_policy())
    _write_profile(dd, profile, adapter, spec.to_dict())
    cat.replace_mappings(ds_id, [
        {**m, "confirmed_by": m.get("confirmed_by", "auto")} for m in maps
    ])
    cat.set_quality(ds_id, summary)
    return maps, summary


def ingest_file(ws: Workspace, cat: Catalog, path_str: str, overrides: dict[str, dict[str, Any]] | None = None,
                label: str | None = None, force: bool = False, sheet: str | None = None) -> Envelope:
    path, profile, adapter, score = _prepare(path_str, sheet, ws)
    h = file_hash(path)
    existing = cat.by_hash(h)
    if existing and not force:
        return dataset_overview_envelope(ws, cat, existing["id"], note="Already ingested; returning existing dataset (use --force to rebuild).")
    ds_id = new_id("ds")
    dd = ws.dataset_dir(ds_id)
    dd.mkdir(parents=True)
    t0 = time.time()
    try:
        pr = adapter.parse(profile, dd / "raw.parquet")
        maps, summary = _finish(ws, cat, ds_id, dd, profile, adapter, overrides, label, h, path, pr.notes)
        cat.add_dataset({
            "id": ds_id, "file_name": path.name, "file_path": str(path), "file_hash": h, "file_size": profile.size,
            "adapter": adapter.id, "label": label, "status": "ready", "row_count": summary["rows"],
            "duration_s": summary["duration_s"], "mapping_version": 1,
            "summary_json": json.dumps({"notes": pr.notes + profile.notes}), "created_at": time.time(),
        })
    except Exception:
        shutil.rmtree(dd, ignore_errors=True)
        raise
    env = dataset_overview_envelope(ws, cat, ds_id, note=f"Ingested in {time.time() - t0:.1f}s.")
    return env


def remap(ws: Workspace, cat: Catalog, ds_ref: str, overrides: dict[str, dict[str, Any]]) -> Envelope:
    row = resolve_dataset(cat, ds_ref)
    dd = ws.dataset_dir(row["id"])
    profile, adapter = _load_profile(dd)
    prior = {
        m["original_name"]: {"canonical_id": m["canonical_id"], "unit": m["source_unit"]}
        for m in cat.get_mappings(row["id"]) if m["confirmed_by"] == "user"
    }
    prior.update(overrides)
    bad = [k for k in overrides if k not in profile.columns]
    if bad:
        raise ObdError("COLUMN_NOT_FOUND", f"No such column(s): {', '.join(clean_text(b) for b in bad)}", f"Columns: {', '.join(clean_text(c) for c in profile.columns[:30])}", data={"columns": [clean_text(c) for c in profile.columns[:60]]})
    d = load_dictionary()
    for k, v in overrides.items():
        if v.get("canonical_id") and v["canonical_id"] not in d.params:
            raise ObdError("UNKNOWN_PARAMETER", f"Unknown canonical id '{v['canonical_id']}'.", "Use `overview` or the dictionary ids (e.g. engine_speed, ect, stft_b1).")
    _finish(ws, cat, row["id"], dd, profile, adapter, prior, row["label"], row["file_hash"], Path(row["file_path"]), [])
    cat.update_dataset(row["id"], mapping_version=row["mapping_version"] + 1)
    cat.conn.execute("DELETE FROM results WHERE dataset_id=?", (row["id"],))
    cat.conn.commit()
    return dataset_overview_envelope(ws, cat, row["id"], note="Mappings updated; data re-normalized from raw and earlier analyses invalidated.")


def resolve_dataset(cat: Catalog, ref: str):
    rows = cat.find_dataset(ref)
    if not rows:
        names = [r["file_name"] for r in cat.list_datasets()][:10]
        raise ObdError("DATASET_NOT_FOUND", f"No dataset matches '{clean_text(ref)}'.", "Run `list`, or `ingest` the file first.", data={"datasets": names})
    return rows[0]


def stale_warning(row) -> str | None:
    p = Path(row["file_path"])
    if not p.exists():
        return "Original file is no longer at its recorded path."
    if p.stat().st_size != row["file_size"] or file_hash(p) != row["file_hash"]:
        return "The original file has changed since ingestion; re-ingest with --force to refresh."
    return None


def dataset_overview_envelope(ws: Workspace, cat: Catalog, ds_id: str, note: str = "") -> Envelope:
    row = cat.get_dataset(ds_id)
    maps = cat.get_mappings(ds_id)
    q = cat.get_quality(ds_id) or {}
    params = []
    for name, info in (q.get("parameters") or {}).items():
        params.append({
            "name": clean_text(name), "mapped": info.get("mapped"), "unit": info.get("unit"), "n": info.get("n"),
            "completeness": info.get("completeness"), "rate_hz": info.get("rate_hz"),
            "oor": info.get("out_of_range", 0), "sentinel": info.get("sentinels", 0), "dropouts": info.get("dropouts", 0),
        })
    params.sort(key=lambda p: (not p["mapped"], p["name"]))
    ti = q.get("time", {})
    pend = [m for m in maps if m["status"] == "needs_confirmation"]
    mapping_dicts = pend
    caveats = []
    if ti.get("spec", {}).get("kind") == "index":
        caveats.append(ti["spec"].get("note", "No time column; row order used."))
    elif ti.get("spec", {}).get("note"):
        caveats.append(ti["spec"]["note"])
    if ti.get("resets"):
        caveats.append(f"{ti['resets']} timestamp reset(s) detected; time continued across resets and segments split.")
    if ti.get("gaps"):
        caveats.append(f"{ti['gaps']} large gap(s) in the data; analyses treat segments separately where it matters.")
    if q.get("duplicates_dropped"):
        caveats.append(f"{q['duplicates_dropped']} duplicate row(s) dropped from normalized data.")
    if ti.get("rows_without_time"):
        caveats.append(f"{ti['rows_without_time']} row(s) had no usable timestamp and were excluded.")
    for p in params:
        if p["oor"] or p["sentinel"]:
            caveats.append(f"{p['name']}: {p['oor']} out-of-range / {p['sentinel']} suspected sentinel samples flagged (kept, excluded from analysis).")
    stale = stale_warning(row)
    if stale:
        caveats.append(stale)
    data = {
        "dataset_id": ds_id, "file": clean_text(row["file_name"], 120), "adapter": row["adapter"], "rows": row["row_count"],
        "duration_s": row["duration_s"], "segments": ti.get("segments"), "median_interval_s": ti.get("median_interval_s"),
        "time_axis": ti.get("spec", {}).get("kind"), "mapping_version": row["mapping_version"], "parameters": params,
        "pending_confirmation": [clean_text(m["original_name"]) for m in pend],
    }
    env = Envelope(
        status="needs_input" if pend else "ok",
        summary=(note + " " if note else "") + f"{clean_text(row['file_name'])} -> {ds_id}: {row['row_count']} rows over {row['duration_s']:.0f}s, "
                f"{sum(1 for p in params if p['mapped'])} canonical + {sum(1 for p in params if not p['mapped'])} custom parameters"
                + (f"; {len(pend)} column(s) await confirmation." if pend else "."),
        data=data, caveats=caveats[:12], needs_input=_questions(mapping_dicts),
        evidence=[{"dataset_id": ds_id, "rows": row["row_count"], "file_hash": row["file_hash"][:12]}],
        units={"duration_s": "s", "rate_hz": "Hz"},
    )
    return env
