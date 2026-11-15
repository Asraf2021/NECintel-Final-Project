"""SQLite verilənlər bazası: sxem, ilkin məlumat (seed) və nazik sorğu qatı."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS shelters (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, district_id TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, capacity INTEGER NOT NULL, occupancy INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open', updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sos_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, citizen_id TEXT NOT NULL, type TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, district_id TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', brigade_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sos_created ON sos_signals(created_at);
CREATE INDEX IF NOT EXISTS idx_sos_status ON sos_signals(status);
CREATE INDEX IF NOT EXISTS idx_sos_citizen ON sos_signals(citizen_id);
CREATE TABLE IF NOT EXISTS citizen_status (
    citizen_id TEXT PRIMARY KEY, status TEXT NOT NULL, shelter_id TEXT, lat REAL, lon REAL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evacuation_plans (
    citizen_id TEXT PRIMARY KEY, origin_lat REAL NOT NULL, origin_lon REAL NOT NULL,
    shelter_id TEXT NOT NULL, edge_ids TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS brigades (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, district_id TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, status TEXT NOT NULL DEFAULT 'available',
    target_lat REAL, target_lon REAL, assigned_alert_id INTEGER, assigned_sos_id INTEGER, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, level TEXT NOT NULL DEFAULT 'red',
    district_id TEXT, shelter_id TEXT, lat REAL, lon REAL, details TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending', actions TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, decided_at REAL, decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE TABLE IF NOT EXISTS road_closures (
    edge_id TEXT PRIMARY KEY, source TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
    action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL, display_name TEXT NOT NULL
);
"""

DATA_TABLES = ["sos_signals", "citizen_status", "evacuation_plans", "alerts", "road_closures", "shelters", "brigades"]


class Database:
    """Tək bağlantı + kilid. Bütün sorğular parametrlidir (SQL injection-a qarşı)."""

    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: tuple = ()):
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return row[0] if row else None

    @contextmanager
    def transaction(self):
        """Atomar əməliyyat: ya hamısı yazılır, ya heç biri."""
        with self._lock:
            if self._depth > 0:  # iç-içə çağırış xarici əməliyyata qoşulur
                yield self
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    def close(self):
        with self._lock:
            self._conn.close()

    # ----- Seed -----
    def seed(self, city: dict, users: list[dict], force: bool = False) -> None:
        now = time.time()
        with self.transaction():
            if force:
                for t in DATA_TABLES:
                    self._conn.execute(f"DELETE FROM {t}")
            if self._conn.execute("SELECT COUNT(*) FROM shelters").fetchone()[0] == 0:
                for s in city["shelters"]:
                    self._conn.execute(
                        "INSERT INTO shelters (id,name,type,district_id,lat,lon,capacity,occupancy,status,updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (s["id"], s["name"], s["type"], s["district_id"], s["lat"], s["lon"],
                         s["capacity"], s["occupancy"], s["status"], now),
                    )
            if self._conn.execute("SELECT COUNT(*) FROM brigades").fetchone()[0] == 0:
                for b in city["brigades"]:
                    self._conn.execute(
                        "INSERT INTO brigades (id,name,type,district_id,lat,lon,status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                        (b["id"], b["name"], b["type"], b["district_id"], b["lat"], b["lon"], b["status"], now),
                    )
            for u in users:
                self._conn.execute(
                    "INSERT INTO users (username,password_hash,role,display_name) VALUES (?,?,?,?)"
                    " ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,"
                    " role=excluded.role, display_name=excluded.display_name",
                    (u["username"], u["password_hash"], u["role"], u["display_name"]),
                )

    def audit(self, actor: str, role: str, action: str, details: dict | None = None) -> None:
        self.execute(
            "INSERT INTO audit_log (ts,actor,role,action,details) VALUES (?,?,?,?,?)",
            (time.time(), actor, role, action, json.dumps(details or {}, ensure_ascii=False)),
        )
