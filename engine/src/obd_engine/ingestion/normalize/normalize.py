"""Stage 5 - validation and normalization from raw.parquet to normalized.parquet (design doc 5.6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from obd_engine.ingestion.discovery.sniff import FileProfile, time_candidates
from obd_engine.ingestion.normalize.time_axis import TimeSpec, build_time, detect_time
from obd_engine.reference.loader import conversion, load_dictionary

RESERVED = {"__row_id", "t", "t_abs", "segment_id"}
FLAG_OOR = 1
FLAG_SENTINEL = 2
GAP_FACTOR = 20.0
MIN_GAP_S = 10.0
_SENTINELS = (255.0, 65535.0)


def _numeric(s: pl.Series, decimal_comma: bool) -> pl.Series:
    if s.dtype == pl.String:
        s = s.str.strip_chars()
        if decimal_comma:
            s = s.str.replace(",", ".", literal=True)
    return s.cast(pl.Float64, strict=False)


def _runs(mask: np.ndarray) -> int:
    if mask.size == 0:
        return 0
    return int(np.count_nonzero(np.diff(np.concatenate(([0], mask.astype(np.int8)))) == 1))


def normalize(raw_path: Path, out_path: Path, profile: FileProfile, mappings: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    d = load_dictionary()
    decimal_comma = profile.decimal == ","
    schema = pl.read_parquet_schema(raw_path)
    header_units = profile.units

    # memory: read only the time candidates first, then only the numeric columns we will keep
    time_cands = [c for c in dict.fromkeys([policy.get("column"), *time_candidates(profile.columns)]) if c and c in schema]
    time_raw = pl.read_parquet(raw_path, columns=["__row_id", *time_cands])
    n_raw = time_raw.height
    spec: TimeSpec = detect_time(time_raw, profile.columns, policy, header_units, decimal_comma)
    secs, t_abs = build_time(time_raw, spec, decimal_comma)
    row_ids = time_raw["__row_id"]
    del time_raw
    keep_src = [m["original_name"] for m in mappings
                if m["original_name"] in schema and m.get("status") != "time" and m["original_name"] != spec.column
                and "non-numeric" not in (m.get("note") or "")]
    keep_set = set(keep_src)

    # --- value columns
    cols: dict[str, pl.Series] = {}
    qual_params: dict[str, dict[str, Any]] = {}
    flags: dict[str, np.ndarray] = {}
    used: set[str] = set(RESERVED)
    for m in mappings:
        src = m["original_name"]
        if src not in keep_set:
            continue
        col = pl.read_parquet(raw_path, columns=[src])[src]  # one column at a time keeps peak memory flat
        s = _numeric(col, decimal_comma)
        raw_nn = col.is_not_null().sum()
        del col
        failures = int(raw_nn - s.is_not_null().sum())
        if raw_nn and failures > 0.5 * raw_nn:
            continue  # text column
        cid = m.get("canonical_id") if m.get("status") == "confirmed" else None
        info: dict[str, Any] = {"source_column": src, "mapped": bool(cid), "parse_failures": failures}
        if cid and cid in d.params:
            p = d.params[cid]
            conv = conversion(m.get("source_unit") or p.unit, p.unit) or (1.0, 0.0)
            if conv != (1.0, 0.0):
                s = s * conv[0] + conv[1]
            name = cid
            if p.kind not in ("enum", "flag"):
                arr = s.to_numpy()
                lo, hi = p.range
                with np.errstate(invalid="ignore"):
                    oor = (arr < lo - 1e-9) | (arr > hi + 1e-9)
                    sent = np.isin(arr, _SENTINELS) | ((cid in {"ect", "iat", "oil_temp"}) & (arr == -40.0))
                f = (oor.astype(np.uint8) * FLAG_OOR) | (sent.astype(np.uint8) * FLAG_SENTINEL)
                f = np.where(np.isnan(arr), 0, f).astype(np.uint8)
                if f.any():
                    flags[name] = f
                info["out_of_range"] = int(np.count_nonzero(f & FLAG_OOR))
                info["sentinels"] = int(np.count_nonzero(f & FLAG_SENTINEL))
            info["unit"] = p.unit
            info["source_unit"] = m.get("source_unit")
        else:
            name = src if src not in used else f"{src}_col"
        used.add(name)
        cols[name] = s.alias(name)
        qual_params[name] = info

    # --- time validation (done on the bare time array, so the wide frame is built exactly once)
    secs_np = secs.to_numpy()
    valid = np.isfinite(secs_np)
    null_time = int(valid.size - np.count_nonzero(valid))
    series: dict[str, pl.Series] = {"__row_id": row_ids}
    if t_abs is not None:
        series["t_abs"] = t_abs
    series.update(cols)
    for name, f in flags.items():
        series[f"{name}__q"] = pl.Series(f"{name}__q", f)
    del cols, flags
    if null_time:
        mask = pl.Series(valid)
        series = {k: v.filter(mask) for k, v in series.items()}
        secs_np = secs_np[valid]
    time_issues: dict[str, Any] = {"rows_without_time": null_time, "spec": spec.to_dict()}
    s_arr = secs_np
    dt = np.diff(s_arr) if s_arr.size > 1 else np.array([])
    pos = dt[dt > 0]
    med = float(np.median(pos)) if pos.size else 1.0
    resets = np.where(dt < 0)[0] + 1
    dt_adj = np.where(dt < 0, med, dt)
    gap_thr = max(GAP_FACTOR * med, MIN_GAP_S)
    breaks = (dt_adj > gap_thr)
    if resets.size:
        breaks[resets - 1] = True
    t = np.concatenate(([0.0], np.cumsum(dt_adj))) if s_arr.size else np.array([])
    seg = np.concatenate(([0], np.cumsum(breaks))).astype(np.int32) if s_arr.size else np.array([], dtype=np.int32)
    time_issues.update(
        median_interval_s=round(med, 6), resets=int(resets.size), gaps=int(np.count_nonzero(dt_adj > gap_thr)),
        duplicate_timestamps=int(np.count_nonzero(dt == 0)), segments=int(seg.max() + 1) if seg.size else 0,
        non_monotonic=bool(resets.size),
    )
    lead = {"__row_id": series.pop("__row_id"), "t": pl.Series("t", t)}
    if "t_abs" in series:
        lead["t_abs"] = series.pop("t_abs")
    lead["segment_id"] = pl.Series("segment_id", seg)
    df = pl.DataFrame({**lead, **series})
    del lead, series, secs, secs_np

    # --- duplicate rows: scanners repeat a row verbatim, so a row identical to its predecessor (same t, same values) is dropped.
    # Only rows sharing a timestamp with the previous row can qualify, so compare just those (keeps memory flat on big files).
    before = df.height
    t_arr = df["t"].to_numpy()
    cand = np.where(t_arr[1:] == t_arr[:-1])[0] + 1
    dup_dropped = 0
    if cand.size:
        value_cols = [c for c in df.columns if c not in ("__row_id", "segment_id")]
        cur, prev = df[cand.tolist()], df[(cand - 1).tolist()]
        same = np.ones(cand.size, dtype=bool)
        for c in value_cols:
            a_, b_ = cur[c], prev[c]
            same &= (a_.eq_missing(b_)).to_numpy()
        if same.any():
            drop = np.zeros(df.height, dtype=bool)
            drop[cand[same]] = True
            df = df.filter(pl.Series(~drop))
            dup_dropped = before - df.height

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out_path, row_group_size=100_000, statistics=True, compression="zstd")

    duration = float(df["t"].max() - df["t"].min()) if df.height > 1 else 0.0
    # --- per-parameter quality
    for name, info in qual_params.items():
        nn = df.select("t", name).filter(pl.col(name).is_not_null())
        n = nn.height
        info["n"] = int(n)
        info["completeness"] = round(n / df.height, 4) if df.height else 0.0
        info["rate_hz"] = round(n / duration, 4) if duration > 0 else None
        if n > 1:
            gaps = np.diff(nn["t"].to_numpy())
            mg = float(gaps.max())
            thr = max(10 * float(np.median(gaps)), 5.0 * med, 5.0)
            info["max_gap_s"] = round(mg, 3)
            info["dropouts"] = int(np.count_nonzero(gaps > thr))
            if nn[name].dtype.is_float():
                vals = nn[name].to_numpy()
                same = np.diff(vals) == 0
                longest = 0
                if same.any():
                    d = np.diff(np.concatenate(([0], same.astype(np.int8), [0])))
                    longest = int((np.where(d == -1)[0] - np.where(d == 1)[0]).max())
                info["longest_flat_samples"] = longest
    summary = {
        "rows_raw": n_raw, "rows": df.height, "duplicates_dropped": dup_dropped, "duration_s": round(duration, 3),
        "time": time_issues, "parameters": qual_params,
    }
    return summary
