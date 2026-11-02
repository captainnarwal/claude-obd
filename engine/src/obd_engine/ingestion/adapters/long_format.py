"""Long-format logs: one reading per row (time, PID name, value[, unit]).

Some apps (e.g. Car Scanner's SECONDS / PID / VALUE / UNITS export) write every sample as its own row instead of one
column per sensor. This adapter pivots such a file to the usual wide layout at parse time; nothing is resampled or
filled, so a wide row holds only the readings that shared a timestamp.
"""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import polars as pl

from obd_engine.ingestion.adapters.base import Adapter, ParseResult, read_delimited_to_parquet
from obd_engine.ingestion.discovery.sniff import FileProfile

_TIME = ("seconds", "time", "timestamp", "datetime", "elapsed time", "time (s)", "time(s)", "sec")
_NAME = ("pid", "name", "parameter", "sensor", "description", "param", "channel")
_VALUE = ("value", "val", "reading")
_UNIT = ("units", "unit", "uom")
_MAX_NAMES = 500


def _find(low: dict[str, str], options: tuple[str, ...]) -> str | None:
    return next((low[o] for o in options if o in low), None)


def match_long(columns: list[str]) -> dict | None:
    """Column roles of a long layout, or None when the header does not look like one."""
    low = {c.strip().lower(): c for c in columns}
    t, n, v, u = _find(low, _TIME), _find(low, _NAME), _find(low, _VALUE), _find(low, _UNIT)
    if not (t and n and v) or len({t, n, v}) < 3:
        return None
    extras = [c for c in columns if c not in {t, n, v, u}]
    return {"time": t, "name": n, "value": v, "unit": u, "extras": extras}


def _to_float(s: pl.Series, decimal_comma: bool) -> pl.Series:
    if s.dtype == pl.String:
        s = s.str.strip_chars()
        if decimal_comma:
            s = s.str.replace(",", ".", literal=True)
    return s.cast(pl.Float64, strict=False)


def _display_names(raw: pl.DataFrame, layout: dict) -> dict[str, str]:
    """PID name -> 'Name (unit)' using each PID's most common unit."""
    n, u = layout["name"], layout["unit"]
    names = raw[n].drop_nulls().unique(maintain_order=True).to_list()
    if not u:
        return {x: x for x in names}
    units = (
        raw.filter(pl.col(n).is_not_null() & pl.col(u).is_not_null() & (pl.col(u).cast(pl.String).str.strip_chars() != ""))
        .group_by(n).agg(pl.col(u).cast(pl.String).str.strip_chars().mode().first().alias("u"))
    )
    by_name = dict(zip(units[n].to_list(), units["u"].to_list()))
    return {x: (f"{x} ({by_name[x]})" if by_name.get(x) else x) for x in names}


def _pivot(raw: pl.DataFrame, layout: dict, decimal_comma: bool) -> tuple[pl.DataFrame, int]:
    t, n, v = layout["time"], layout["name"], layout["value"]
    raw = raw.drop("__row_id", strict=False).with_columns(pl.col(n).cast(pl.String).str.strip_chars())
    disp = _display_names(raw, layout)
    val = _to_float(raw[v], decimal_comma)
    dropped = int((val.is_null() & raw[v].is_not_null()).sum())
    time_col = raw[t]
    if time_col.dtype == pl.String:
        as_num = _to_float(time_col, decimal_comma)
        if as_num.drop_nulls().len() >= 0.9 * max(1, time_col.drop_nulls().len()):
            time_col = as_num
    frame = raw.select([t, *layout["extras"], n]).with_columns(time_col.alias(t), val.alias("__val")).filter(pl.col(n).is_not_null())
    wide = frame.pivot(on=n, index=[t, *layout["extras"]], values="__val", aggregate_function="first", maintain_order=True)
    wide = wide.rename({k: v_ for k, v_ in disp.items() if k in wide.columns})
    return wide.with_row_index("__row_id", offset=1), dropped


def detect_long(profile: FileProfile) -> FileProfile | None:
    """Return a long-format profile (wide column list, format 'csv_long') or None when the file is not long-format."""
    layout = match_long(profile.columns)
    if layout is None or profile.format != "csv":
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "sniff.parquet"
            read_delimited_to_parquet(profile, tmp)
            raw = pl.read_parquet(tmp)
    except Exception:
        return None
    names = raw[layout["name"]]
    if names.dtype != pl.String:
        return None
    uniq = names.drop_nulls().n_unique()
    if uniq < 1 or uniq > _MAX_NAMES or uniq > max(1, raw.height // 2):
        return None
    decimal_comma = profile.decimal == ","
    wide, _ = _pivot(raw, layout, decimal_comma)
    cols = [c for c in wide.columns if c != "__row_id"]
    layout = {**layout, "raw_columns": list(profile.columns), "decimal": profile.decimal}
    note = f"Long-format log (one reading per row, {uniq} distinct PIDs) pivoted to one column per PID."
    return dataclasses.replace(profile, format="csv_long", columns=cols, units={}, decimal=".", long_layout=layout,
                               notes=[*profile.notes, note])


class LongFormatAdapter(Adapter):
    id = "long_format"

    def sniff(self, profile: FileProfile) -> float:
        return 0.9 if profile.format == "csv_long" else 0.0

    def parse(self, profile: FileProfile, out_path: Path) -> ParseResult:
        layout = profile.long_layout or {}
        raw_profile = dataclasses.replace(profile, format="csv", columns=layout["raw_columns"], decimal=layout["decimal"])
        tmp = out_path.with_suffix(".raw.tmp")
        read_delimited_to_parquet(raw_profile, tmp)
        try:
            raw = pl.read_parquet(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        wide, dropped = _pivot(raw, layout, layout["decimal"] == ",")
        wide.write_parquet(out_path, row_group_size=100_000, statistics=True)
        notes = [f"{dropped} non-numeric reading(s) were left empty."] if dropped else []
        return ParseResult(rows=wide.height, columns=profile.columns, notes=notes)
