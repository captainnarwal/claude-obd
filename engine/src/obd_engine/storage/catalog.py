"""SQLite catalog with versioned migrations."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from obd_engine.storage.workspace import Workspace

MIGRATIONS: list[str] = [
    """
    CREATE TABLE datasets (
      id TEXT PRIMARY KEY, file_name TEXT NOT NULL, file_path TEXT NOT NULL, file_hash TEXT NOT NULL,
      file_size INTEGER, adapter TEXT, label TEXT, status TEXT NOT NULL DEFAULT 'ready',
      row_count INTEGER, duration_s REAL, mapping_version INTEGER NOT NULL DEFAULT 1,
      summary_json TEXT, created_at REAL NOT NULL
    );
    CREATE TABLE mappings (
      dataset_id TEXT NOT NULL, original_name TEXT NOT NULL, canonical_id TEXT, source_unit TEXT,
      canonical_unit TEXT, confidence REAL, method TEXT, status TEXT, confirmed_by TEXT, confirmed_at REAL, note TEXT, candidates_json TEXT,
      PRIMARY KEY (dataset_id, original_name)
    );
    CREATE TABLE quality (dataset_id TEXT PRIMARY KEY, report_json TEXT NOT NULL);
    CREATE TABLE runs (
      id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, modules TEXT, config_json TEXT, versions_json TEXT,
      cache_key TEXT, created_at REAL NOT NULL
    );
    CREATE TABLE results (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, dataset_id TEXT NOT NULL, module TEXT NOT NULL,
      cache_key TEXT, status TEXT, result_json TEXT NOT NULL
    );
    CREATE INDEX idx_results_cache ON results(cache_key);
    CREATE TABLE reports (
      id TEXT PRIMARY KEY, dataset_ids TEXT, run_ids TEXT, format TEXT, path TEXT, created_at REAL NOT NULL
    );
    """,
    # 2: vehicles (engine / drivetrain profiles inline as JSON) and logging sessions
    """
    CREATE TABLE vehicles (
      id TEXT PRIMARY KEY, nickname TEXT, make TEXT, model TEXT, year INTEGER, vin TEXT, notes TEXT,
      engine_json TEXT NOT NULL DEFAULT '{}', drivetrain_json TEXT NOT NULL DEFAULT '{}',
      limits_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
    );
    CREATE TABLE sessions (
      id TEXT PRIMARY KEY, vehicle_id TEXT, label TEXT, conditions_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
    );
    ALTER TABLE datasets ADD COLUMN vehicle_id TEXT;
    ALTER TABLE datasets ADD COLUMN session_id TEXT;
    """,
]


def new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time()):x}{secrets.token_hex(2)}"


class Catalog:
    def __init__(self, ws: Workspace):
        ws.ensure()
        self.conn = sqlite3.connect(ws.catalog_path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version):
            self.conn.executescript(sql)
            self.conn.execute(f"PRAGMA user_version = {i + 1}")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    # ---- datasets
    def add_dataset(self, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self.tx() as c:
            c.execute(f"INSERT INTO datasets ({cols}) VALUES ({qs})", list(row.values()))

    def update_dataset(self, dataset_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE datasets SET {sets} WHERE id=?", [*fields.values(), dataset_id])

    def get_dataset(self, dataset_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()

    def find_dataset(self, ref: str) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM datasets WHERE id=?", (ref,)).fetchall()
        if rows:
            return rows
        return self.conn.execute(
            "SELECT * FROM datasets WHERE file_name=? OR label=? ORDER BY created_at DESC", (ref, ref)
        ).fetchall()

    def by_hash(self, file_hash: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM datasets WHERE file_hash=?", (file_hash,)).fetchone()

    def list_datasets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()

    def delete_dataset(self, dataset_id: str) -> None:
        with self.tx() as c:
            for t in ("mappings", "quality", "results", "runs"):
                c.execute(f"DELETE FROM {t} WHERE dataset_id=?", (dataset_id,))
            c.execute("DELETE FROM datasets WHERE id=?", (dataset_id,))

    # ---- vehicles / sessions
    _VEHICLE_JSON = ("engine", "drivetrain", "limits")

    @classmethod
    def _vehicle_dict(cls, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for k in cls._VEHICLE_JSON:
            d[k] = json.loads(d.pop(f"{k}_json") or "{}")
        return d

    def add_vehicle(self, vehicle_id: str, fields: dict[str, Any]) -> None:
        row = {"id": vehicle_id, "created_at": time.time()}
        for k in ("nickname", "make", "model", "year", "vin", "notes"):
            row[k] = fields.get(k)
        for k in self._VEHICLE_JSON:
            row[f"{k}_json"] = json.dumps(fields.get(k) or {})
        with self.tx() as c:
            c.execute(f"INSERT INTO vehicles ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))

    def update_vehicle(self, vehicle_id: str, fields: dict[str, Any]) -> None:
        """Scalar fields replace; engine/drivetrain/limits merge key-by-key."""
        cur = self.get_vehicle(vehicle_id)
        if cur is None:
            return
        sets: dict[str, Any] = {k: v for k, v in fields.items() if k in ("nickname", "make", "model", "year", "vin", "notes")}
        for k in self._VEHICLE_JSON:
            if k in fields:
                sets[f"{k}_json"] = json.dumps({**cur[k], **fields[k]})
        if sets:
            with self.tx() as c:
                c.execute(f"UPDATE vehicles SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", [*sets.values(), vehicle_id])

    def get_vehicle(self, vehicle_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
        return self._vehicle_dict(r) if r else None

    def find_vehicle(self, ref: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM vehicles WHERE id=? OR nickname=? ORDER BY created_at DESC", (ref, ref)).fetchone()
        return self._vehicle_dict(r) if r else None

    def list_vehicles(self) -> list[dict[str, Any]]:
        return [self._vehicle_dict(r) for r in self.conn.execute("SELECT * FROM vehicles ORDER BY created_at")]

    def delete_vehicle(self, vehicle_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE datasets SET vehicle_id=NULL WHERE vehicle_id=?", (vehicle_id,))
            c.execute("UPDATE sessions SET vehicle_id=NULL WHERE vehicle_id=?", (vehicle_id,))
            c.execute("DELETE FROM vehicles WHERE id=?", (vehicle_id,))

    def add_session(self, session_id: str, vehicle_id: str | None, label: str | None, conditions: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (session_id, vehicle_id, label, json.dumps(conditions), time.time()))

    def find_session(self, ref: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM sessions WHERE id=? OR label=? ORDER BY created_at DESC", (ref, ref)).fetchone()
        return self._session_dict(r) if r else None

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self._session_dict(r) for r in self.conn.execute("SELECT * FROM sessions ORDER BY created_at")]

    @staticmethod
    def _session_dict(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["conditions"] = json.loads(d.pop("conditions_json") or "{}")
        return d

    def assign_dataset(self, dataset_id: str, vehicle_id: str | None = None, session_id: str | None = None) -> None:
        sets = {k: v for k, v in (("vehicle_id", vehicle_id), ("session_id", session_id)) if v is not None}
        if session_id is not None and vehicle_id is None:
            s = self.conn.execute("SELECT vehicle_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if s and s["vehicle_id"]:
                sets["vehicle_id"] = s["vehicle_id"]
        if sets:
            self.update_dataset(dataset_id, **sets)

    def vehicle_for_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT vehicle_id FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        return self.get_vehicle(r["vehicle_id"]) if r and r["vehicle_id"] else None

    # ---- mappings / quality
    def replace_mappings(self, dataset_id: str, rows: list[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM mappings WHERE dataset_id=?", (dataset_id,))
            for r in rows:
                c.execute(
                    "INSERT INTO mappings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dataset_id, r["original_name"], r.get("canonical_id"), r.get("source_unit"),
                     r.get("canonical_unit"), r.get("confidence"), r.get("method"), r.get("status"),
                     r.get("confirmed_by", "auto"), r.get("confirmed_at", time.time()), r.get("note", ""),
                     json.dumps(r.get("candidates", []))),
                )

    def get_mappings(self, dataset_id: str) -> list[dict[str, Any]]:
        out = []
        for r in self.conn.execute("SELECT * FROM mappings WHERE dataset_id=?", (dataset_id,)):
            d = dict(r)
            d["candidates"] = json.loads(d.pop("candidates_json") or "[]")
            out.append(d)
        return out

    def set_quality(self, dataset_id: str, report: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO quality VALUES (?,?)", (dataset_id, json.dumps(report, default=str)))

    def get_quality(self, dataset_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT report_json FROM quality WHERE dataset_id=?", (dataset_id,)).fetchone()
        return json.loads(r[0]) if r else None

    # ---- analysis
    def cached_result(self, cache_key: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT result_json FROM results WHERE cache_key=? LIMIT 1", (cache_key,)).fetchone()
        return json.loads(r[0]) if r else None

    def add_run(self, run_id: str, dataset_id: str, modules: list[str], config: dict, versions: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                (run_id, dataset_id, ",".join(modules), json.dumps(config), json.dumps(versions), None, time.time()),
            )

    def add_result(self, run_id: str, dataset_id: str, module: str, cache_key: str, status: str, result: dict) -> str:
        rid = new_id("ar")
        with self.tx() as c:
            c.execute(
                "INSERT INTO results VALUES (?,?,?,?,?,?,?)",
                (rid, run_id, dataset_id, module, cache_key, status, json.dumps(result, default=str)),
            )
        return rid

    def latest_results(self, dataset_id: str) -> list[dict[str, Any]]:
        """Most recent result per module for a dataset."""
        rows = self.conn.execute(
            """SELECT r.module, r.result_json, r.run_id FROM results r JOIN runs u ON u.id = r.run_id
               WHERE r.dataset_id=? ORDER BY u.created_at DESC""",
            (dataset_id,),
        ).fetchall()
        seen: dict[str, dict] = {}
        for row in rows:
            if row["module"] not in seen:
                d = json.loads(row["result_json"])
                d["run_id"] = row["run_id"]
                seen[row["module"]] = d
        return list(seen.values())

    def add_report(self, report_id: str, dataset_ids: list[str], run_ids: list[str], fmt: str, path: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO reports VALUES (?,?,?,?,?,?)",
                (report_id, ",".join(dataset_ids), ",".join(run_ids), fmt, path, time.time()),
            )
