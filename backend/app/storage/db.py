"""SQLite (WAL) session store. All multi-row writes are transactional."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.models.domain import Annotation, DtcScan, Exchange, Reading

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, vehicle_profile TEXT NOT NULL, source TEXT NOT NULL,
  synthetic INTEGER NOT NULL DEFAULT 0, label TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL, ended_at REAL, meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS exchanges (
  id INTEGER PRIMARY KEY, session_id TEXT REFERENCES sessions(id), seq INTEGER, t_wall REAL, t_mono REAL,
  request TEXT, request_hex TEXT, response_hex TEXT, response_text TEXT, latency_ms REAL, status TEXT, phase TEXT
);
CREATE INDEX IF NOT EXISTS ix_exchanges_session ON exchanges(session_id, id);
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), t_wall REAL, t_mono REAL,
  mode INTEGER, pid INTEGER, ecu TEXT, name TEXT, unit TEXT, value REAL, text TEXT, raw_hex TEXT,
  quality TEXT, detail TEXT, latency_ms REAL
);
CREATE INDEX IF NOT EXISTS ix_samples_session ON samples(session_id, id);
CREATE TABLE IF NOT EXISTS dtc_scans (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), t_wall REAL, phase TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), t_wall REAL, t_mono REAL,
  label TEXT, note TEXT
);
"""

_SAMPLE_COLS = (
    "t_wall",
    "t_mono",
    "mode",
    "pid",
    "ecu",
    "name",
    "unit",
    "value",
    "text",
    "raw_hex",
    "quality",
    "detail",
    "latency_ms",
)
_EX_COLS = (
    "seq",
    "t_wall",
    "t_mono",
    "request",
    "request_hex",
    "response_hex",
    "response_text",
    "latency_ms",
    "status",
    "phase",
)


def new_session_id(t_wall: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(t_wall or time.time())) + "-" + secrets.token_hex(2)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def tx(self):
        with self._lock:
            self._db.execute("BEGIN")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # -- settings ------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # -- sessions ------------------------------------------------------------
    def create_session(
        self,
        kind: str,
        vehicle_profile: str,
        source: str,
        synthetic: bool,
        label: str = "",
        started_at: float | None = None,
        meta: dict | None = None,
        session_id: str | None = None,
    ) -> str:
        started = started_at if started_at is not None else time.time()
        sid = session_id or new_session_id()
        with self.tx() as db:
            db.execute(
                "INSERT INTO sessions(id,kind,vehicle_profile,source,synthetic,label,started_at,meta) VALUES(?,?,?,?,?,?,?,?)",
                (sid, kind, vehicle_profile, source, int(synthetic), label, started, json.dumps(meta or {})),
            )
        return sid

    def update_session(self, sid: str, ended_at: float | None = None, meta: dict | None = None) -> None:
        with self.tx() as db:
            if meta:
                cur = json.loads(
                    db.execute("SELECT meta FROM sessions WHERE id=?", (sid,)).fetchone()["meta"]
                )
                cur.update(meta)
                db.execute("UPDATE sessions SET meta=? WHERE id=?", (json.dumps(cur), sid))
            if ended_at is not None:
                db.execute("UPDATE sessions SET ended_at=? WHERE id=?", (ended_at, sid))

    @staticmethod
    def _session(row: dict[str, Any]) -> dict[str, Any]:
        row["meta"] = json.loads(row["meta"])
        row["synthetic"] = bool(row["synthetic"])
        return row

    def get_session(self, sid: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM sessions WHERE id=?", (sid,))
        return self._session(rows[0]) if rows else None

    def list_sessions(
        self, vehicle_profile: str | None = None, order: str = "started_at"
    ) -> list[dict[str, Any]]:
        """order: 'started_at' (when the data was measured) or 'rowid' (when it was added to this database)."""
        col = "rowid" if order == "rowid" else "started_at, rowid"
        if vehicle_profile:
            rows = self._rows(
                f"SELECT * FROM sessions WHERE vehicle_profile=? ORDER BY {col}", (vehicle_profile,)
            )
        else:
            rows = self._rows(f"SELECT * FROM sessions ORDER BY {col}")
        return [self._session(r) for r in rows]

    # -- bulk writes -----------------------------------------------------------
    def add_exchanges(self, sid: str | None, exchanges: list[Exchange]) -> None:
        if not exchanges:
            return
        with self.tx() as db:
            db.executemany(
                f"INSERT INTO exchanges(session_id,{','.join(_EX_COLS)}) VALUES(?{',?' * len(_EX_COLS)})",
                [(sid, *(getattr(e, c) for c in _EX_COLS)) for e in exchanges],
            )

    def add_samples(self, sid: str, readings: list[Reading]) -> None:
        if not readings:
            return
        with self.tx() as db:
            db.executemany(
                f"INSERT INTO samples(session_id,{','.join(_SAMPLE_COLS)}) VALUES(?{',?' * len(_SAMPLE_COLS)})",
                [(sid, *(getattr(r, c) for c in _SAMPLE_COLS)) for r in readings],
            )

    def add_scan(self, sid: str, scan: DtcScan) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO dtc_scans(session_id,t_wall,phase,data) VALUES(?,?,?,?)",
                (sid, scan.t_wall, scan.phase, scan.model_dump_json()),
            )

    def add_annotation(self, sid: str, a: Annotation) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO annotations(session_id,t_wall,t_mono,label,note) VALUES(?,?,?,?,?)",
                (sid, a.t_wall, a.t_mono, a.label, a.note),
            )

    # -- reads -----------------------------------------------------------------
    def samples(self, sid: str) -> list[dict[str, Any]]:
        return self._rows(
            f"SELECT {','.join(_SAMPLE_COLS)} FROM samples WHERE session_id=? ORDER BY id", (sid,)
        )

    def exchanges(self, sid: str) -> list[dict[str, Any]]:
        return self._rows(
            f"SELECT {','.join(_EX_COLS)} FROM exchanges WHERE session_id=? ORDER BY id", (sid,)
        )

    def scans(self, sid: str) -> list[DtcScan]:
        rows = self._rows("SELECT data FROM dtc_scans WHERE session_id=? ORDER BY id", (sid,))
        return [DtcScan.model_validate_json(r["data"]) for r in rows]

    def annotations(self, sid: str) -> list[Annotation]:
        rows = self._rows(
            "SELECT t_wall,t_mono,label,note FROM annotations WHERE session_id=? ORDER BY t_wall", (sid,)
        )
        return [Annotation(**r) for r in rows]
