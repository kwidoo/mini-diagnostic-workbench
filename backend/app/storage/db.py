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


#: columns added after the first release (session events with origin and evidence)
_ANNOTATION_EXTRA = {
    "event_type": "TEXT",
    "origin": "TEXT NOT NULL DEFAULT 'manual'",
    "confidence": "TEXT",
    "evidence": "TEXT NOT NULL DEFAULT '{}'",
}


#: sample provenance columns added for manufacturer readings (DDE-05). NULL on every older row and on
#: generic OBD / adapter samples; returned by samples() only when set, so older exports stay identical.
_SAMPLE_EXTRA = {
    "source": "TEXT",  # bmw_dde (NULL: generic OBD, or adapter for mode 0)
    "signal_key": "TEXT",  # stable manufacturer signal key, e.g. dpf_soot_mass
    "request_id": "TEXT",
    "decoder_version": "TEXT",
    "catalog_version": "TEXT",
    "status_json": "TEXT",  # decoded status semantics (verified layouts only)
}


def chronological(readings: list[Reading]) -> list[Reading]:
    """One timeline: order by monotonic time, then wall time; at identical timestamps generic /
    adapter readings precede manufacturer readings, then the original order (stable, deterministic).
    ``Store.samples()`` returns rows in insertion order, so writers that merge several sources
    (transcript replay) insert through this."""
    return [
        r
        for _, r in sorted(
            enumerate(readings), key=lambda ir: (ir[1].t_mono, ir[1].t_wall, ir[1].source is not None, ir[0])
        )
    ]


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
        self._migrate()

    def _migrate(self) -> None:
        """Additive, idempotent schema changes. Existing rows keep their meaning
        (old annotations become origin 'manual' with no event type)."""
        with self._lock:
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(annotations)").fetchall()}
            for name, ddl in _ANNOTATION_EXTRA.items():
                if name not in cols:
                    self._db.execute(f"ALTER TABLE annotations ADD COLUMN {name} {ddl}")
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(samples)").fetchall()}
            for name, ddl in _SAMPLE_EXTRA.items():
                if name not in cols:
                    self._db.execute(f"ALTER TABLE samples ADD COLUMN {name} {ddl}")

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

    def delete_setting(self, key: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM settings WHERE key=?", (key,))

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
        cols = (*_SAMPLE_COLS, *_SAMPLE_EXTRA)

        def extra(r: Reading) -> tuple:
            return (
                r.source,
                r.signal_key,
                r.request_id,
                r.decoder_version,
                r.catalog_version,
                json.dumps(r.status, sort_keys=True) if r.status is not None else None,
            )

        with self.tx() as db:
            db.executemany(
                f"INSERT INTO samples(session_id,{','.join(cols)}) VALUES(?{',?' * len(cols)})",
                [(sid, *(getattr(r, c) for c in _SAMPLE_COLS), *extra(r)) for r in readings],
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
                "INSERT INTO annotations(session_id,t_wall,t_mono,label,note,event_type,origin,confidence,evidence)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    a.t_wall,
                    a.t_mono,
                    a.label,
                    a.note,
                    a.event_type,
                    a.origin,
                    a.confidence,
                    json.dumps(a.evidence, sort_keys=True),
                ),
            )

    # -- reads -----------------------------------------------------------------
    def samples(self, sid: str) -> list[dict[str, Any]]:
        """Stored readings in insertion order. Provenance columns (DDE-05) appear only on rows that
        have them, so generic-only sessions keep exactly their original shape."""
        rows = self._rows(
            f"SELECT {','.join((*_SAMPLE_COLS, *_SAMPLE_EXTRA))} FROM samples WHERE session_id=? ORDER BY id",
            (sid,),
        )
        for r in rows:
            for k in _SAMPLE_EXTRA:
                if r[k] is None:
                    del r[k]
        return rows

    def exchanges(self, sid: str) -> list[dict[str, Any]]:
        return self._rows(
            f"SELECT {','.join(_EX_COLS)} FROM exchanges WHERE session_id=? ORDER BY id", (sid,)
        )

    def scans(self, sid: str) -> list[DtcScan]:
        rows = self._rows("SELECT data FROM dtc_scans WHERE session_id=? ORDER BY id", (sid,))
        return [DtcScan.model_validate_json(r["data"]) for r in rows]

    def annotations(self, sid: str) -> list[Annotation]:
        rows = self._rows(
            "SELECT t_wall,t_mono,label,note,event_type,origin,confidence,evidence FROM annotations "
            "WHERE session_id=? ORDER BY t_wall, id",
            (sid,),
        )
        out = []
        for r in rows:
            r["evidence"] = json.loads(r["evidence"] or "{}")
            r["origin"] = r["origin"] or "manual"
            out.append(Annotation(**r))
        return out

    def last_activity(self, sid: str) -> float | None:
        """Wall time of the newest stored sample, exchange or event of a session."""
        rows = self._rows(
            "SELECT MAX(t) AS t FROM (SELECT MAX(t_wall) AS t FROM samples WHERE session_id=? "
            "UNION ALL SELECT MAX(t_wall) FROM exchanges WHERE session_id=? "
            "UNION ALL SELECT MAX(t_wall) FROM annotations WHERE session_id=?)",
            (sid, sid, sid),
        )
        return rows[0]["t"] if rows else None

    def open_sessions(self) -> list[dict[str, Any]]:
        return [self._session(r) for r in self._rows("SELECT * FROM sessions WHERE ended_at IS NULL")]
