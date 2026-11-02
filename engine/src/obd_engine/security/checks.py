"""Path, size and zip-bomb checks (design doc §12)."""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path

from obd_engine.cli.envelope import ObdError

MAX_CSV_BYTES = 2 * 1024**3
MAX_XLSX_BYTES = 200 * 1024**2
MAX_COLUMNS = 2000
MAX_ZIP_RATIO = 200
MAX_ZIP_UNCOMPRESSED = 2 * 1024**3
TELEMETRY_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".log"}


def resolve_input(path: str | os.PathLike[str], cwd: Path | None = None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (cwd or Path.cwd()) / p
    try:
        real = p.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise ObdError("FILE_NOT_FOUND", f"File not found: {path}", "Check the path or run `scan` to list files.")
    require_allowed(real)
    mode = real.stat().st_mode
    if stat.S_ISDIR(mode):
        raise ObdError("NOT_A_FILE", f"{path} is a directory, not a file.")
    if not stat.S_ISREG(mode):
        raise ObdError("NOT_A_FILE", f"{path} is not a regular file.")
    return real


def check_size(path: Path, kind: str) -> None:
    size = path.stat().st_size
    limit = MAX_XLSX_BYTES if kind in ("xlsx", "xls") else MAX_CSV_BYTES
    if size > limit:
        hint = "Export to CSV for files this large." if kind in ("xlsx", "xls") else ""
        raise ObdError("FILE_TOO_LARGE", f"File is {size/1024**2:.0f} MB; limit for {kind} is {limit/1024**2:.0f} MB.", hint)


def check_xlsx_zip(path: Path) -> None:
    if path.suffix.lower() == ".xlsm":
        raise ObdError("MACRO_FILE_REJECTED", "Macro-enabled workbooks (.xlsm) are not supported.", "Save as .xlsx or export CSV.")
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise ObdError("MACRO_FILE_REJECTED", "Workbook contains macros; refusing to open.")
            comp = sum(i.compress_size for i in zf.infolist()) or 1
            total = sum(i.file_size for i in zf.infolist())
            if total > MAX_ZIP_UNCOMPRESSED or total / comp > MAX_ZIP_RATIO:
                raise ObdError("ZIP_BOMB_SUSPECTED", "Workbook decompresses to an implausible size; refusing to open.")
    except zipfile.BadZipFile:
        raise ObdError("BAD_FILE", "File is not a valid .xlsx archive.")


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def allowed_roots() -> list[Path] | None:
    """Folders the engine may read from. None = unrestricted (Claude Code's permission prompts govern paths there).
    The MCP server sets OBD_ALLOWED_DIRS to the workspace plus any --allow-dir, because MCP clients have no such prompt."""
    raw = os.environ.get("OBD_ALLOWED_DIRS")
    return [Path(x).resolve() for x in raw.split(os.pathsep) if x] if raw else None


def require_allowed(real: Path) -> None:
    roots = allowed_roots()
    if roots is not None and not any(inside(real, r) for r in roots):
        raise ObdError("PATH_NOT_ALLOWED", f"{real.name} is outside the folders this server may read.",
                       "Move or copy the file into the working folder, or start the server with --allow-dir <folder>.", exit_code=3)
