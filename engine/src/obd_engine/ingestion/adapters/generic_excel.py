"""XLSX adapter: calamine via fastexcel, cached values only, never evaluates formulas."""

from __future__ import annotations

from pathlib import Path

import fastexcel
import polars as pl

from obd_engine.cli.envelope import ObdError
from obd_engine.ingestion.adapters.base import Adapter, ParseResult
from obd_engine.ingestion.discovery.sniff import FileProfile, NULL_TOKENS, _KNOWN_UNITS, _is_num
from obd_engine.reference.loader import canon_unit


def _cell(v) -> str:
    return "" if v is None else str(v)


def _header_index(rows: list[list[str]]) -> int:
    for i, r in enumerate(rows):
        filled = [c for c in r if c.strip()]
        if len(filled) >= 2 and sum(1 for c in filled if not _is_num(c)) >= 0.6 * len(filled):
            return i
    return 0


def _load_all_strings(path: Path, sheet: str, n_rows: int | None = None) -> pl.DataFrame:
    wb = fastexcel.read_excel(str(path))
    return wb.load_sheet(sheet, header_row=None, dtypes="string", n_rows=n_rows).to_polars()


def sniff_excel(path: Path, sheet: str | None) -> FileProfile:
    wb = fastexcel.read_excel(str(path))
    sheets = list(wb.sheet_names)
    if not sheets:
        raise ObdError("EMPTY_FILE", "Workbook has no sheets.")
    best, best_n = sheet or sheets[0], -1
    if sheet is None:
        for s in sheets:
            try:
                df = wb.load_sheet(s, header_row=None, dtypes="string", n_rows=500).to_polars()
            except Exception:
                continue
            n = df.height * df.width
            if n > best_n:
                best, best_n = s, n
    elif sheet not in sheets:
        raise ObdError("SHEET_NOT_FOUND", f"Sheet '{sheet}' not found.", f"Available: {', '.join(sheets)}")
    df = _load_all_strings(path, best, 500)
    if df.height == 0:
        raise ObdError("EMPTY_FILE", f"Sheet '{best}' is empty.")
    rows = [[_cell(v) for v in r] for r in df.rows()]
    h = _header_index(rows)
    header = [c.strip() for c in rows[h]]
    unit_row = False
    units: dict[str, str] = {}
    if h + 1 < len(rows):
        nxt = rows[h + 1]
        known = sum(1 for c in nxt if c.strip().strip("()[]").lower() in _KNOWN_UNITS)
        if known >= max(2, int(0.4 * len(nxt))) and all(not _is_num(c) for c in nxt):
            unit_row = True
            units = {n: canon_unit(u.strip().strip("()[]")) for n, u in zip(header, nxt) if u.strip()}
    cols, seen = [], {}
    for i, name in enumerate(header):
        name = name or f"column_{i+1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)
    start = h + (2 if unit_row else 1)
    p = FileProfile(
        path=str(path), file_name=path.name, size=path.stat().st_size, format="xlsx", header_row=h,
        unit_row=unit_row, preamble_lines=h, columns=cols, units=units, sheet=best, sheets=sheets,
        sample_rows=rows[start : start + 200],
    )
    if len(sheets) > 1:
        p.notes.append(f"Workbook has {len(sheets)} sheets; using '{best}'.")
    return p


class GenericExcelAdapter(Adapter):
    id = "generic_excel"

    def sniff(self, profile: FileProfile) -> float:
        return 0.3 if profile.format == "xlsx" else 0.0

    def parse(self, profile: FileProfile, out_path: Path) -> ParseResult:
        df = _load_all_strings(Path(profile.path), profile.sheet or "")  # all text; typed below
        start = profile.header_row + (2 if profile.unit_row else 1)
        df = df.slice(start)
        if df.width != len(profile.columns):
            df = df.select(df.columns[: len(profile.columns)])
        df.columns = profile.columns[: df.width]
        # type every column that is fully numeric (or null); keep the rest as text
        out = []
        for c in df.columns:
            s = df[c].str.strip_chars().replace(NULL_TOKENS, None)
            num = s.cast(pl.Float64, strict=False)
            ok = (num.is_null() == s.is_null()).all()
            out.append(num.alias(c) if ok else s.alias(c))
        df = pl.DataFrame(out).with_row_index("__row_id", offset=1)
        if df.height == 0:
            raise ObdError("EMPTY_FILE", "Sheet has a header but no data rows.")
        df.write_parquet(out_path, row_group_size=100_000, statistics=True)
        return ParseResult(rows=df.height, columns=profile.columns)
