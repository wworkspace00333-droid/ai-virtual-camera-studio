"""SQLite store for sessions, watchdog/recovery events and benchmark results."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    ended REAL,
    app_version TEXT NOT NULL,
    profile TEXT,
    clean_exit INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    level TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    name TEXT NOT NULL,
    hardware TEXT NOT NULL,
    result TEXT NOT NULL
);
"""
SCHEMA_VERSION = "1"


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        self.session_id: int | None = None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
            self._closed = True

    _closed = False

    # sessions
    def unclean_previous_sessions(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE clean_exit=0 AND ended IS NULL").fetchone()
        return int(row[0])

    def begin_session(self, app_version: str, profile: str | None) -> int:
        with self._lock:
            # mark stale sessions (crashes) as ended without clean exit
            self._conn.execute("UPDATE sessions SET ended=started WHERE ended IS NULL")
            cur = self._conn.execute(
                "INSERT INTO sessions(started, app_version, profile) VALUES (?,?,?)",
                (time.time(), app_version, profile))
            self.session_id = int(cur.lastrowid)
        return self.session_id

    def end_session(self) -> None:
        if self.session_id is None:
            return
        with self._lock:
            self._conn.execute("UPDATE sessions SET ended=?, clean_exit=1 WHERE id=?",
                               (time.time(), self.session_id))

    # events
    def log_event(self, level: str, component: str, message: str, data: dict | None = None) -> None:
        if self._closed:   # late events during shutdown are dropped, never raised
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(ts, session_id, level, component, message, data) VALUES (?,?,?,?,?,?)",
                (time.time(), self.session_id, level, component, message,
                 json.dumps(data) if data is not None else None))

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, level, component, message, data FROM events ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(ts=r[0], level=r[1], component=r[2], message=r[3],
                     data=json.loads(r[4]) if r[4] else None) for r in rows]

    def prune_events(self, keep_days: float = 30) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - keep_days * 86400,))
        return cur.rowcount

    # benchmarks
    def add_benchmark(self, name: str, hardware: dict, result: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO benchmarks(ts, name, hardware, result) VALUES (?,?,?,?)",
                               (time.time(), name, json.dumps(hardware), json.dumps(result)))

    def benchmarks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, name, hardware, result FROM benchmarks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(ts=r[0], name=r[1], hardware=json.loads(r[2]), result=json.loads(r[3])) for r in rows]
