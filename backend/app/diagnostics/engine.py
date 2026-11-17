"""Analysis engine: loads session data, runs the rules a vehicle profile selects.

The engine is vehicle-independent. Rules live in app/diagnostics/rules and are
looked up by id; a profile lists which rule ids apply to it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from app.acquisition.recorder import GOOD
from app.models.domain import Annotation, DtcScan, Finding
from app.storage.db import Store
from app.vehicles.profiles import VehicleProfile, get_profile

WINDOW_S = 5.0


@dataclass
class SessionData:
    session: dict[str, Any]
    samples: list[dict[str, Any]]
    scans: list[DtcScan]
    annotations: list[Annotation]
    _series: dict[int, list[tuple[float, float]]] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.session["id"]

    @property
    def caps(self) -> dict[str, Any] | None:
        return self.session["meta"].get("capabilities")

    @property
    def primary_ecu(self) -> str | None:
        return (self.caps or {}).get("primary_ecu")

    def supported(self, pid: int) -> bool | None:
        """True/False from capability discovery, None when discovery data is missing."""
        if not self.caps:
            return None
        return any(pid in e["mode01_pids"] for e in self.caps["ecus"])

    @property
    def t0(self) -> float | None:
        rec = [s["t_mono"] for s in self.samples if s["mode"] == 1]
        return min(rec) if rec else None

    def series(self, pid: int) -> list[tuple[float, float]]:
        """(seconds since first sample, value) for good numeric samples of the primary ECU. No interpolation."""
        if pid not in self._series:
            t0 = self.t0 or 0.0
            self._series[pid] = [
                (s["t_mono"] - t0, s["value"])
                for s in self.samples
                if s["mode"] == 1
                and s["pid"] == pid
                and s["quality"] in GOOD
                and s["value"] is not None
                and s["ecu"] in (None, self.primary_ecu or s["ecu"])
            ]
        return self._series[pid]

    def metric(self, pid: int) -> dict[str, Any] | None:
        return next((m for m in self.session["meta"].get("metrics", []) if m["pid"] == pid), None)

    def dtc_codes(self) -> set[str]:
        return {d.code for sc in self.scans for d in sc.dtcs}

    def mil_states(self) -> list[bool]:
        return [sc.mil_on for sc in self.scans if sc.mil_on is not None]

    def annotation_times(self) -> list[tuple[float, Annotation]]:
        t0 = self.t0
        return [(a.t_mono - t0, a) for a in self.annotations if a.t_mono is not None and t0 is not None]


@dataclass
class AnalysisContext:
    profile: VehicleProfile
    current: SessionData
    history: list[SessionData]  # all sessions of this vehicle profile, oldest first (includes current)
    owner_reported: dict[str, Any]
    manufacturer_status: list[dict[str, Any]]


def load_session(store: Store, sid: str) -> SessionData:
    session = store.get_session(sid)
    if session is None:
        raise KeyError(f"no session {sid}")
    return SessionData(session, store.samples(sid), store.scans(sid), store.annotations(sid))


def analyze_session(
    store: Store,
    sid: str,
    owner_reported: dict[str, Any] | None = None,
    manufacturer_status: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    from app.diagnostics.rules import RULES
    from app.manufacturer.extension import default_registry

    current = load_session(store, sid)
    profile = get_profile(current.session["vehicle_profile"])
    history = [load_session(store, s["id"]) for s in store.list_sessions(profile.id)]
    record = store.get_setting(f"vehicle:{profile.id}", None)
    ctx = AnalysisContext(
        profile=profile,
        current=current,
        history=history,
        owner_reported=owner_reported
        if owner_reported is not None
        else (record or {}).get("owner_reported", profile.owner_reported),
        manufacturer_status=manufacturer_status
        if manufacturer_status is not None
        else default_registry().status(profile.ecu_ids),
    )
    findings: list[Finding] = []
    for rule_id in profile.rule_ids:
        findings.extend(RULES[rule_id](ctx))
    return findings


# -- shared numeric helpers (no interpolation anywhere) ---------------------------


def windows(series: list[tuple[float, float]], width: float = WINDOW_S) -> dict[int, list[float]]:
    """Bucket samples into fixed windows by their own timestamps."""
    out: dict[int, list[float]] = {}
    for t, v in series:
        out.setdefault(int(t // width), []).append(v)
    return out


def stats(values: list[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 2),
        "std": round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0,
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def latest_before(series: list[tuple[float, float]], t: float) -> float | None:
    """Most recent actually-measured value at or before t (a held last observation, flagged as such by callers)."""
    prev = None
    for ts, v in series:
        if ts > t:
            break
        prev = v
    return prev
