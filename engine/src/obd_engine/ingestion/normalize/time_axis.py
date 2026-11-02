"""Time-axis detection and construction (design doc 5.6). Never resamples."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import polars as pl

from obd_engine.ingestion.adapters.base import split_header
from obd_engine.ingestion.discovery.sniff import time_candidates

DT_FORMATS = [
    "%d-%b-%Y %H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S%.f", "%Y/%m/%d %H:%M:%S%.f",
    "%m/%d/%Y %H:%M:%S%.f", "%d/%m/%Y %H:%M:%S%.f", "%d.%m.%Y %H:%M:%S%.f", "%m/%d/%Y %I:%M:%S%.f %p",
    "%H:%M:%S%.f", "%M:%S%.f",
]
TIME_OF_DAY = {"%H:%M:%S%.f", "%M:%S%.f"}


@dataclass
class TimeSpec:
    kind: str  # numeric_s | numeric_ms | epoch_s | epoch_ms | datetime | time_of_day | index
    column: str | None = None
    fmt: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_float(s: pl.Series, decimal_comma: bool) -> pl.Series:
    if s.dtype == pl.String:
        s = s.str.strip_chars()
        if decimal_comma:
            s = s.str.replace(",", ".", literal=True)
        return s.cast(pl.Float64, strict=False)
    return s.cast(pl.Float64, strict=False)


def _best_datetime_format(s: pl.Series, preferred: list[str]) -> tuple[str, float] | None:
    s = s.drop_nulls().head(2000).str.strip_chars()
    if s.len() == 0:
        return None
    best: tuple[str, float] | None = None
    for fmt in [*preferred, *[f for f in DT_FORMATS if f not in preferred]]:
        try:
            parsed = s.str.to_time(fmt, strict=False) if fmt in TIME_OF_DAY else s.str.to_datetime(fmt, strict=False)
            ok = parsed.is_not_null().sum() / s.len()
        except Exception:  # a format polars cannot compile is simply not a candidate
            continue
        if ok >= 0.9 and (best is None or ok > best[1]):
            best = (fmt, ok)
            if ok > 0.99:
                break
    return best


def detect_time(df: pl.DataFrame, columns: list[str], policy: dict[str, Any], header_units: dict[str, str], decimal_comma: bool) -> TimeSpec:
    cands = []
    if policy.get("column") in df.columns:
        cands.append(policy["column"])
    cands += [c for c in time_candidates(columns) if c in df.columns and c not in cands]
    for col in cands:
        s = df[col]
        _, unit = split_header(col, header_units.get(col))
        if s.dtype == pl.String:
            f = _best_datetime_format(s, list(policy.get("formats", [])))
            if f:
                kind = "time_of_day" if f[0] in TIME_OF_DAY else "datetime"
                return TimeSpec(kind, col, f[0])
            num = _as_float(s, decimal_comma)
        elif s.dtype in (pl.Datetime, pl.Date):
            return TimeSpec("datetime", col, None)
        else:
            num = s.cast(pl.Float64, strict=False)
        nn = num.drop_nulls()
        if nn.len() < max(2, 0.8 * s.drop_nulls().len()):
            continue
        mx = float(nn.max())
        if mx > 1e11:
            return TimeSpec("epoch_ms", col)
        if mx > 1e9:
            return TimeSpec("epoch_s", col)
        d = nn.diff().drop_nulls()
        d = d.filter(d > 0)
        med = float(d.median()) if d.len() else 0.0
        if unit == "ms":
            return TimeSpec("numeric_ms", col)
        if not unit and med >= 20:
            return TimeSpec("numeric_ms", col, note="no unit in header; median step >= 20 so milliseconds assumed")
        return TimeSpec("numeric_s", col)
    return TimeSpec("index", None, note="no usable time column; row order used (1 row = 1 time unit)")


def build_time(df: pl.DataFrame, spec: TimeSpec, decimal_comma: bool) -> tuple[pl.Series, pl.Series | None]:
    """Return (seconds, absolute datetime or None); values may be non-monotonic - callers handle resets."""
    if spec.kind == "index":
        return df["__row_id"].cast(pl.Float64), None
    s = df[spec.column]
    if spec.kind == "time_of_day":
        tm = s.str.strip_chars().str.to_time(spec.fmt, strict=False)
        return tm.cast(pl.Int64).cast(pl.Float64) / 1e9, None  # seconds since midnight; a rollover shows up as a reset
    if spec.kind == "datetime":
        if s.dtype == pl.String:
            dt = s.str.strip_chars().str.to_datetime(spec.fmt, strict=False)
        else:
            dt = s.cast(pl.Datetime("us"))
        secs = dt.dt.epoch("us").cast(pl.Float64) / 1e6
        return secs, dt
    num = _as_float(s, decimal_comma)
    if spec.kind == "numeric_ms":
        return num / 1000.0, None
    if spec.kind == "epoch_ms":
        return num / 1000.0, (num * 1000).cast(pl.Int64).cast(pl.Datetime("us"))
    if spec.kind == "epoch_s":
        return num, (num * 1e6).cast(pl.Int64).cast(pl.Datetime("us"))
    return num, None
