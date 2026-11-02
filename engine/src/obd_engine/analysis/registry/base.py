"""Module contract, finding helpers and the orchestrator (design doc 7.1, 7.2, 9.3)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from obd_engine import __version__
from obd_engine.analysis.thresholds import profile_key
from obd_engine.cli.envelope import ObdError
from obd_engine.reference.loader import load_dictionary
from obd_engine.storage.catalog import Catalog, new_id
from obd_engine.storage.workspace import Workspace


@dataclass
class Context:
    ws: Workspace
    cat: Catalog
    dataset: Any  # sqlite row
    params: dict[str, dict[str, Any]]  # available parameter -> quality info (mapped, n, unit, ...)
    quality: dict[str, Any]
    config: dict[str, Any] = field(default_factory=dict)
    vehicle: dict[str, Any] | None = None  # assigned vehicle profile (engine, drivetrain, limits) or None

    @property
    def dataset_id(self) -> str:
        return self.dataset["id"]

    def has(self, *names: str) -> bool:
        return all(n in self.params and self.params[n].get("n", 0) > 0 for n in names)

    def mapped(self) -> list[str]:
        return [p for p, i in self.params.items() if i.get("mapped") and i.get("n", 0) > 0]


def evidence(ctx: Context, parameter: str | None, t0: float | None = None, t1: float | None = None, n: int | None = None,
             stat: str | None = None, value: Any = None, unit: str | None = None, rows: tuple[int, int] | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"dataset_id": ctx.dataset_id, "parameter": parameter}
    if t0 is not None:
        e["time_range_s"] = [round(float(t0), 2), round(float(t1 if t1 is not None else t0), 2)]
    if rows:
        e["row_span"] = [int(rows[0]), int(rows[1])]
    if n is not None:
        e["n"] = int(n)
    if stat:
        e["statistic"] = stat
    if value is not None:
        e["value"] = round(float(value), 3) if isinstance(value, float) else value
    if unit:
        e["unit"] = unit
    return e


def finding(title: str, category: str, kind: str, severity: str = "info", confidence: str = "high", reason: str = "",
            conditions: str = "", ev: list[dict[str, Any]] | None = None, threshold: dict[str, Any] | None = None,
            alternatives: list[str] | None = None, missing: list[str] | None = None, next_steps: list[str] | None = None) -> dict[str, Any]:
    assert kind in ("observation", "inference", "hypothesis")
    assert severity in ("info", "attention", "warning")
    if threshold and threshold.get("tier") == "generic" and confidence == "high":
        confidence, reason = "medium", (reason + "; " if reason else "") + "generic threshold, may not apply to this engine"
    return {
        "title": title, "category": category, "kind": kind, "severity": severity,
        "confidence": {"level": confidence, "reason": reason}, "conditions": conditions, "evidence": ev or [],
        "threshold": threshold, "alternatives": alternatives or [], "missing_evidence": missing or [], "next_steps": next_steps or [],
    }


class Module:
    id = "base"
    version = "1.0.0"
    title = ""
    required_any: list[str] = []  # at least one of
    required_all: list[str] = []
    optional: list[str] = []
    config_defaults: dict[str, Any] = {}

    def applicability(self, ctx: Context) -> tuple[str, str]:
        """('applicable' | 'partial' | 'not_applicable', reason)"""
        missing_all = [p for p in self.required_all if not ctx.has(p)]
        if missing_all:
            return "not_applicable", f"missing required parameter(s): {', '.join(missing_all)}"
        if self.required_any and not any(ctx.has(p) for p in self.required_any):
            return "not_applicable", f"needs at least one of: {', '.join(self.required_any)}"
        miss_opt = [p for p in self.optional if not ctx.has(p)]
        if miss_opt:
            return "partial", f"optional parameters absent: {', '.join(miss_opt)}"
        return "applicable", ""

    def run(self, ctx: Context) -> dict[str, Any]:
        raise NotImplementedError


_REGISTRY: dict[str, Module] = {}


def register(mod: Module) -> Module:
    _REGISTRY[mod.id] = mod
    return mod


def registry() -> dict[str, Module]:
    from obd_engine.analysis import modules_index  # noqa: F401  (registers on import; idempotent)

    return _REGISTRY


def make_context(ws: Workspace, cat: Catalog, row, config: dict[str, Any] | None = None) -> Context:
    q = cat.get_quality(row["id"]) or {}
    return Context(ws, cat, row, q.get("parameters", {}), q, config or {}, cat.vehicle_for_dataset(row["id"]))


def cache_key(ctx: Context, mod: Module, cfg: dict[str, Any]) -> str:
    payload = json.dumps({
        "h": ctx.dataset["file_hash"], "mv": ctx.dataset["mapping_version"], "m": mod.id, "v": mod.version,
        "cfg": cfg, "dict": load_dictionary().version, "engine": __version__, "veh": profile_key(ctx.vehicle),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def run_modules(ws: Workspace, cat: Catalog, row, module_ids: list[str] | None, config: dict[str, Any] | None = None,
                use_cache: bool = True) -> dict[str, Any]:
    reg = registry()
    ids = module_ids or [m for m in reg]  # 'auto' = all registered; applicability filters below
    unknown = [m for m in ids if m not in reg]
    if unknown:
        raise ObdError("UNKNOWN_MODULE", f"Unknown module(s): {', '.join(unknown)}", f"Available: {', '.join(reg)}", exit_code=2)
    run_id = new_id("rn")
    ctx = make_context(ws, cat, row, config)
    cat.add_run(run_id, row["id"], ids, config or {}, {"engine": __version__, "dictionary": load_dictionary().version,
                                                        **{m: reg[m].version for m in ids}})
    results, skipped = [], []
    cached_n = 0
    for mid in ids:
        mod = reg[mid]
        status, reason = mod.applicability(ctx)
        if status == "not_applicable":
            skipped.append({"module": mid, "reason": reason})
            cat.add_result(run_id, row["id"], mid, "", "not_applicable", {"module": mid, "version": mod.version, "status": "not_applicable", "reason": reason})
            continue
        cfg = {**mod.config_defaults, **(config or {}).get(mid, {})}
        ctx.config = cfg
        key = cache_key(ctx, mod, cfg)
        res = cat.cached_result(key) if use_cache else None
        if res is None:
            res = mod.run(ctx)
            res.update(module=mid, version=mod.version, status=status, applicability_note=reason, title=mod.title)
        else:
            cached_n += 1
        cat.add_result(run_id, row["id"], mid, key, status, res)
        results.append(res)
    return {"run_id": run_id, "results": results, "skipped": skipped, "cached": cached_n}
