"""Adapter contract (design doc 5.3). The core only sees what adapters return."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from obd_engine.cli.envelope import ObdError
from obd_engine.ingestion.discovery.sniff import FileProfile, NULL_TOKENS, _KNOWN_UNITS
from obd_engine.reference.loader import canon_unit

_SOURCE_TAG = re.compile(r"\s*\*\s*obd\s*$", re.I)  # "Engine Speed (RPM) *OBD": marks a PID read from the ECU
_TRAILING_GROUP = re.compile(r"^(?P<name>.*?)\s*[\(\[](?P<unit>[^()\[\]]{1,16})[\)\]]\s*$")


def split_header(header: str, unit_row_unit: str | None = None) -> tuple[str, str]:
    """'Coolant Temp (°F)' -> ('Coolant Temp', '°F'). Only known units are peeled off."""
    name, unit = _SOURCE_TAG.sub("", header.strip()), canon_unit(unit_row_unit) if unit_row_unit else ""
    m = _TRAILING_GROUP.match(name)
    if m and m.group("unit").strip().lower() in _KNOWN_UNITS:
        name, hu = m.group("name").strip(), canon_unit(m.group("unit"))
        unit = unit or hu
    return name, unit


@dataclass
class ParseResult:
    rows: int
    columns: list[str]
    notes: list[str] = field(default_factory=list)


class Adapter:
    id = "base"
    version = "1"

    def sniff(self, profile: FileProfile) -> float:
        return 0.0

    def parse(self, profile: FileProfile, out_path: Path) -> ParseResult:
        raise NotImplementedError

    def column_hints(self) -> dict[str, str]:
        """normalized header name -> canonical id ('' = explicitly a custom/device column)."""
        return {}

    def time_policy(self) -> dict[str, Any]:
        return {"kind": "auto"}


def _transcode_to_utf8(src: Path, encoding: str, dst: Path) -> None:
    with open(src, "r", encoding=encoding, errors="replace", newline="") as r, open(dst, "w", encoding="utf-8", newline="") as w:
        while chunk := r.read(1 << 20):
            w.write(chunk)


def read_delimited_to_parquet(profile: FileProfile, out_path: Path) -> ParseResult:
    """Streaming CSV -> Parquet. Typed when inference succeeds, all-string otherwise (nothing is lost)."""
    src = Path(profile.path)
    tmp: Path | None = None
    if profile.encoding.lower().replace("_", "-") not in ("utf-8", "ascii"):
        tmp = out_path.with_suffix(".utf8.tmp")
        _transcode_to_utf8(src, profile.encoding, tmp)
        src = tmp
    skip = profile.header_row + 1 + (1 if profile.unit_row else 0)
    notes: list[str] = []

    def scan(all_strings: bool) -> pl.LazyFrame:
        return pl.scan_csv(
            src, has_header=False, skip_rows=skip, new_columns=profile.columns, separator=profile.delimiter,
            quote_char='"', null_values=NULL_TOKENS, infer_schema=not all_strings,
            infer_schema_length=20000, decimal_comma=(profile.decimal == ","), truncate_ragged_lines=True,
            encoding="utf8-lossy", low_memory=False, raise_if_empty=False,
        ).with_row_index("__row_id", offset=1)

    try:
        scan(False).sink_parquet(out_path, row_group_size=100_000, statistics=True)
    except Exception:  # type inference failed late in the file: keep strings
        notes.append("Type inference failed on some rows; columns stored as text and converted during normalization.")
        try:
            scan(True).sink_parquet(out_path, row_group_size=100_000, statistics=True)
        except Exception as exc2:
            raise ObdError("PARSE_FAILED", f"Could not parse file: {type(exc2).__name__}", str(exc2)[:200])
    finally:
        if tmp and tmp.exists():
            tmp.unlink()
    meta = pl.scan_parquet(out_path)
    rows = meta.select(pl.len()).collect().item()
    if rows == 0:
        raise ObdError("EMPTY_FILE", "File has a header but no data rows.")
    return ParseResult(rows=rows, columns=profile.columns, notes=notes)


class GenericDelimitedAdapter(Adapter):
    id = "generic_delimited"

    def sniff(self, profile: FileProfile) -> float:
        return 0.3 if profile.format == "csv" else 0.0

    def parse(self, profile: FileProfile, out_path: Path) -> ParseResult:
        return read_delimited_to_parquet(profile, out_path)
