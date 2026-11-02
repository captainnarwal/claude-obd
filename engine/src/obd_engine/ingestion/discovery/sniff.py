"""Stage 1 - discovery: format, encoding, delimiter, decimal, header/unit rows (design doc 5.2)."""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes

from obd_engine.cli.envelope import ObdError
from obd_engine.reference.loader import canon_unit, _UNIT_ALIASES
from obd_engine.security.checks import check_size, check_xlsx_zip

SAMPLE_BYTES = 2 * 1024 * 1024
NULL_TOKENS = ["-", "--", "", "NA", "N/A", "NaN", "nan", "null", "NULL", "None", "n/a", "---"]
_KNOWN_UNITS = {u.lower() for u in _UNIT_ALIASES} | {u.lower() for u in _UNIT_ALIASES.values()}
_NUM = re.compile(r"^\s*[-+]?(\d+([.,]\d*)?|[.,]\d+)([eE][-+]?\d+)?\s*$")


@dataclass
class FileProfile:
    path: str
    file_name: str
    size: int
    format: str  # csv | xlsx | xls | unknown
    encoding: str = "utf-8"
    delimiter: str = ","
    decimal: str = "."
    header_row: int = 0  # 0-based line index of the column-name row
    unit_row: bool = False  # line after header holds units
    preamble_lines: int = 0
    columns: list[str] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)  # from unit row (original name -> unit)
    sheet: str | None = None
    sheets: list[str] = field(default_factory=list)
    sample_rows: list[list[str]] = field(default_factory=list)
    est_rows: int | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_format(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:4] == b"PK\x03\x04":
        return "xlsx"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    if head[:4] == b"%PDF" or head[:2] == b"MZ" or head[:4] == b"\x7fELF":
        return "unknown"
    return "csv"


def _decode(sample: bytes) -> tuple[str, str]:
    if sample.startswith(b"\xef\xbb\xbf"):
        return sample.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return sample.decode("utf-16", errors="replace"), "utf-16"
    try:
        return sample.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # sample may cut a multibyte char at the end; retry trimmed
        try:
            return sample[:-3].decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            pass
    best = from_bytes(sample).best()
    enc = best.encoding if best else "latin-1"
    return sample.decode(enc, errors="replace"), enc


def _is_num(s: str) -> bool:
    return bool(_NUM.match(s))


def _split(line: str, delim: str) -> list[str]:
    return next(csv.reader([line], delimiter=delim, quotechar='"'), [])


def _guess_delimiter(lines: list[str]) -> str:
    stats: dict[str, tuple[float, int]] = {}
    for d in [",", ";", "	", "|"]:
        counts = [len(_split(l, d)) for l in lines[:200] if l.strip()]
        if not counts:
            continue
        modal = max(set(counts), key=counts.count)
        if modal >= 2:
            stats[d] = (counts.count(modal) / len(counts), modal)
    # ';' and tab are rarely anything but delimiters (',' is also a decimal mark), so they win when consistent
    for d in ["	", ";", "|"]:
        if d in stats and stats[d][0] >= 0.9:
            return d
    if not stats:
        return ","
    return max(stats, key=lambda d: stats[d][0] * min(stats[d][1], 50))


def _guess_decimal(lines: list[str], delim: str) -> str:
    comma = dot = 0
    for l in lines[1:200]:
        for cell in _split(l, delim):
            c = cell.strip()
            if re.fullmatch(r"[-+]?\d+,\d+", c):
                comma += 1
            elif re.fullmatch(r"[-+]?\d+\.\d+", c):
                dot += 1
    return "," if delim != "," and comma > dot else "."


def sniff_text(path: Path) -> FileProfile:
    size = path.stat().st_size
    with open(path, "rb") as f:
        sample = f.read(SAMPLE_BYTES)
    text, enc = _decode(sample)
    lines = [l for l in text.splitlines()]
    if len(sample) == SAMPLE_BYTES and lines:
        lines = lines[:-1]  # last line is probably cut
    lines_nb = [l for l in lines if l.strip()]
    if not lines_nb:
        raise ObdError("EMPTY_FILE", "File has no readable text rows.")
    delim = _guess_delimiter(lines_nb)
    decimal = _guess_decimal(lines_nb, delim)
    rows = [_split(l, delim) for l in lines]
    counts = [len(r) for r in rows if any(c.strip() for c in r)]
    modal = max(set(counts), key=counts.count)

    # header row: first line with the modal width whose cells are mostly non-numeric and which is followed by data
    header_idx = 0
    for i, r in enumerate(rows):
        if len(r) != modal or not any(c.strip() for c in r):
            continue
        non_num = sum(1 for c in r if c.strip() and not _is_num(c))
        if non_num >= max(1, int(0.6 * len([c for c in r if c.strip()]))):
            header_idx = i
            break
    header = [c.strip() for c in rows[header_idx]]
    unit_row = False
    if header_idx + 1 < len(rows):
        nxt = rows[header_idx + 1]
        if len(nxt) == modal and nxt and all(not _is_num(c) for c in nxt):
            known = sum(1 for c in nxt if c.strip().strip("()[]").lower() in _KNOWN_UNITS)
            if known >= max(2, int(0.4 * modal)):
                unit_row = True
    units: dict[str, str] = {}
    if unit_row:
        for name, u in zip(header, rows[header_idx + 1]):
            u = u.strip().strip("()[]")
            if u:
                units[name] = canon_unit(u)

    # dedupe empty / duplicate names
    seen: dict[str, int] = {}
    cols: list[str] = []
    for i, h in enumerate(header):
        name = h or f"column_{i+1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)

    data_start = header_idx + (2 if unit_row else 1)
    sample_rows = [r for r in rows[data_start : data_start + 200] if len(r) == modal]
    est_rows = None
    if lines_nb and sample:
        avg = len(sample) / max(1, len(sample.splitlines()))
        est_rows = int(size / max(avg, 1)) - data_start
    prof = FileProfile(
        path=str(path), file_name=path.name, size=size, format="csv", encoding=enc, delimiter=delim,
        decimal=decimal, header_row=header_idx, unit_row=unit_row, preamble_lines=header_idx,
        columns=cols, units=units, sample_rows=sample_rows, est_rows=est_rows,
    )
    if header_idx:
        prof.notes.append(f"Skipped {header_idx} preamble line(s) before the header.")
    if modal < 2:
        prof.notes.append("File looks like a single column; check delimiter.")
    return prof


def sniff_file(path: Path, sheet: str | None = None) -> FileProfile:
    fmt = detect_format(path)
    if fmt == "unknown":
        raise ObdError("UNSUPPORTED_FORMAT", f"{path.name} does not look like a telemetry export.", "Supported: CSV/TSV/TXT, XLSX.")
    check_size(path, fmt)
    if fmt == "xls":
        raise ObdError("UNSUPPORTED_FORMAT", "Legacy .xls is not supported in v1.", "Save the sheet as .xlsx or CSV.")
    if fmt == "xlsx":
        check_xlsx_zip(path)
        from obd_engine.ingestion.adapters.generic_excel import sniff_excel

        return sniff_excel(path, sheet)
    return sniff_text(path)


# ---- timestamps ------------------------------------------------------------

TIME_NAME = re.compile(r"(^|\b)(time|timestamp|device time|gps time|datetime|date time|elapsed|seconds|sec|t)(\b|$)", re.I)


def time_candidates(columns: list[str]) -> list[str]:
    ranked = []
    for c in columns:
        n = c.lower().strip()
        score = 0
        if n in ("time", "timestamp", "datetime", "t", "time (s)", "time(s)", "elapsed time", "seconds", "device time"):
            score = 3
        elif TIME_NAME.search(n) and "run time" not in n and "runtime" not in n and "gps" not in n:
            score = 2
        elif "gps time" in n:
            score = 1
        if score:
            ranked.append((score, c))
    ranked.sort(key=lambda x: -x[0])
    return [c for _, c in ranked]
