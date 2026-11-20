"""Longitudinal monitoring analysis: shutdowns, fan runs, voltage, coolant, DPF observations.

Works on stored session data only (live, replayed or imported), so every result
can be recomputed offline from an export.

Vocabulary used in every result (and in the dashboard):

* ``measured``  - a value the ECU (or, for ATRV, the adapter) reported, decoded with a
  verified formula;
* ``raw_only``  - the ECU answered a supported PID, but no verified decoder exists:
  raw bytes are stored, no value is shown or used;
* ``not_available`` - the ECU does not offer it via generic OBD, or it is a
  manufacturer-only value this tool cannot read;
* ``manual``    - entered by a person;
* ``observed``  - a transition seen directly in measured data (e.g. RPM fell to 0);
* ``inferred``  - a heuristic interpretation of indirect evidence. Inferred results are
  always called *possible*; nothing here is ever reported as *confirmed*.

Thresholds below are screening heuristics of this tool, chosen to be conservative
and, where possible, relative to the same vehicle's own history. They are not BMW /
MINI limits; no such limits are published for this ECU through generic OBD.
"""

from __future__ import annotations

import bisect
import hashlib
import statistics
from dataclasses import dataclass
from typing import Any

from app.diagnostics.engine import SessionData
from app.ecus.definitions import ECU_DEFINITIONS
from app.parameters.standard_pids import ADAPTER_MODE, ADAPTER_VOLTAGE_PID, PIDS, RAW_ONLY_PIDS, pid_name
from app.vehicles.profiles import DPF_STANDARD_PIDS, get_profile

GOOD = ("ok", "raw_only")
RPM, SPEED, LOAD, COOLANT, MAF, EGR_CMD, ECU_V, AMBIENT = 0x0C, 0x0D, 0x04, 0x05, 0x10, 0x2C, 0x42, 0x46

# -- screening heuristics (tool values, not vehicle limits) -----------------------------------
RUNNING_RPM = 300.0
IDLE_MAX_SPEED = 0.0  # km/h: "stationary"
WARM_BAND_C = 10.0  # warm = within this many degC of the vehicle's usual warm plateau
WARM_MIN_PLATEAU_C = 60.0  # below this no plateau is accepted as "warm" (generic guard)
REGEN_WINDOW_S = 30.0
REGEN_MIN_EPISODE_S = 60.0
REGEN_MIN_BASELINE = 60  # warm-idle samples needed for a baseline
REGEN_EGR_CLOSED_FRACTION = 0.25  # commanded EGR at or below this share of its usual warm-idle value
REGEN_EGR_MIN_BASELINE_PCT = 10.0  # the usual warm-idle EGR must be at least this open to judge closure
REGEN_IDLE_RPM_RISE = 80.0  # rpm above the usual warm idle
REGEN_MAF_RISE = 1.25  # x usual warm-idle MAF
REGEN_AT_SHUTDOWN_S = 90.0  # an episode ending this close to a shutdown counts as "active at shutdown"
FAN_AFTER_SHUTDOWN_S = 1800.0  # fan runs starting within 30 min of a shutdown belong to it
SHUTDOWN_EPISODE_S = 300.0  # stop / ignition-off / ECU silence within this span (no restart) are one shutdown
COOLANT_LOW_MARGIN_C = 10.0
COOLANT_HIGH_MARGIN_C = 8.0
COOLANT_DROP_C = 10.0
COOLANT_MIN_RUN_S = 1200.0  # 20 min running before "did not reach usual temperature" is considered
VOLT_DROP_V = 1.0  # sample-to-sample drop within VOLT_DROP_WINDOW_S
VOLT_DROP_WINDOW_S = 5.0
VOLT_LOW_RUNNING_V = 12.2  # running voltage below this is listed (screen only)
VOLT_NO_RISE_V = 0.3  # running median not this much above the engine-off voltage: no charging rise seen
CRANK_WINDOW_S = 15.0
EVENT_VOLTAGE_WINDOW_S = 60.0
LOW_CONFIDENCE, MODERATE_CONFIDENCE = "low", "moderate"

# -- air path (MAF / EGR / MAP) screening heuristics: see app/diagnostics/air_path.py ------------
# None of these is a BMW / MINI specification. They only decide what is listed for a closer look.
AIRPATH_RUNNING_RPM = (
    600.0  # measured RPM on BOTH sides of a MAF sample at or above this: "confirmed running"
)
AIRPATH_RPM_MAX_AGE_S = 3.0  # ... and each of those RPM samples at most this far from the MAF sample
MAF_ZERO_GS = 0.0  # zero-MAF: exactly this value as decoded from the ECU answer
MAF_LOW_GS = 1.0  # low-MAF screening value (g/s): below this while running is listed
AIRPATH_CONTEXT_MAX_AGE_S = 5.0  # MAP / load / speed / EGR values older than this are stale (not associated)
AIRPATH_COOLANT_MAX_AGE_S = 30.0  # coolant changes slowly: older than this is stale
AIRPATH_EPISODE_GAP_S = 5.0  # low-MAF samples closer than this (no normal MAF between) form one episode
AIRPATH_PERSISTENT_S = 3.0  # an episode lasting this long, or with >= 3 samples, is "persistent"
AIRPATH_REPEAT_WINDOW_S = 120.0  # episodes starting within this of the previous one are a repeated cluster
AIRPATH_DIP_FRACTION = 0.5  # abrupt dip: MAF falls to <= this share of the previous running sample ...
AIRPATH_DIP_MIN_GS = 2.0  # ... by at least this many g/s ...
AIRPATH_DIP_RECOVERY_S = 3.0  # ... and is back to >= AIRPATH_DIP_RECOVERED_FRACTION within this time ...
AIRPATH_DIP_RECOVERED_FRACTION = 0.8
AIRPATH_DIP_RPM_STEADY = 0.10  # ... while RPM changed by at most 10 % (excludes most gear changes / lift-off)
AIRPATH_WARM_COOLANT_C = 60.0  # fixed cold / warm split so that drives stay comparable over months
AIRPATH_ASSOC_WINDOW_S = 5.0  # +/- around an episode for EGR / MAP / RPM associations
AIRPATH_BASELINE_WINDOW_S = 15.0  # MAP / commanded EGR before an episode, to measure a change against
AIRPATH_MAP_CHANGE_KPA = 5.0
AIRPATH_EGR_CMD_CHANGE_PCT = 10.0
AIRPATH_RPM_UNSTABLE_STD = 30.0  # rpm std within the window, and ...
AIRPATH_RPM_UNSTABLE_FACTOR = 2.0  # ... at least this many times the session's usual idle-window std
AIRPATH_ANNOTATION_WINDOW_S = 30.0  # a symptom marker this close to an air-path anomaly is "near" it
EGR_ERROR_SCREEN_PCT = (
    15.0  # |EGR error| (PID 2D) at or above this is an excursion (also used by air_path_egr_maf)
)
AIRPATH_EGR_EXCURSION_GAP_S = 10.0  # excursion samples closer than this belong to one excursion
AIRPATH_MIN_RUNNING_MAF_SAMPLES = 50  # fewer confirmed-running MAF samples: no rates are computed
AIRPATH_MIN_HISTORY_SESSIONS = 2  # other sessions of the same cohort needed before comparing against them

#: published with every summary so a reader sees exactly which screening values were used
HEURISTICS: dict[str, float] = {
    "running_rpm": RUNNING_RPM,
    "idle_max_speed": IDLE_MAX_SPEED,
    "warm_band_c": WARM_BAND_C,
    "warm_min_plateau_c": WARM_MIN_PLATEAU_C,
    "regen_window_s": REGEN_WINDOW_S,
    "regen_min_episode_s": REGEN_MIN_EPISODE_S,
    "regen_min_baseline": REGEN_MIN_BASELINE,
    "regen_egr_closed_fraction": REGEN_EGR_CLOSED_FRACTION,
    "regen_egr_min_baseline_pct": REGEN_EGR_MIN_BASELINE_PCT,
    "regen_idle_rpm_rise": REGEN_IDLE_RPM_RISE,
    "regen_maf_rise": REGEN_MAF_RISE,
    "regen_at_shutdown_s": REGEN_AT_SHUTDOWN_S,
    "fan_after_shutdown_s": FAN_AFTER_SHUTDOWN_S,
    "shutdown_episode_s": SHUTDOWN_EPISODE_S,
    "coolant_low_margin_c": COOLANT_LOW_MARGIN_C,
    "coolant_high_margin_c": COOLANT_HIGH_MARGIN_C,
    "coolant_drop_c": COOLANT_DROP_C,
    "coolant_min_run_s": COOLANT_MIN_RUN_S,
    "volt_drop_v": VOLT_DROP_V,
    "volt_drop_window_s": VOLT_DROP_WINDOW_S,
    "volt_low_running_v": VOLT_LOW_RUNNING_V,
    "volt_no_rise_v": VOLT_NO_RISE_V,
    "crank_window_s": CRANK_WINDOW_S,
    "event_voltage_window_s": EVENT_VOLTAGE_WINDOW_S,
    "airpath_running_rpm": AIRPATH_RUNNING_RPM,
    "airpath_rpm_max_age_s": AIRPATH_RPM_MAX_AGE_S,
    "maf_zero_gs": MAF_ZERO_GS,
    "maf_low_gs": MAF_LOW_GS,
    "airpath_context_max_age_s": AIRPATH_CONTEXT_MAX_AGE_S,
    "airpath_coolant_max_age_s": AIRPATH_COOLANT_MAX_AGE_S,
    "airpath_episode_gap_s": AIRPATH_EPISODE_GAP_S,
    "airpath_persistent_s": AIRPATH_PERSISTENT_S,
    "airpath_repeat_window_s": AIRPATH_REPEAT_WINDOW_S,
    "airpath_dip_fraction": AIRPATH_DIP_FRACTION,
    "airpath_dip_min_gs": AIRPATH_DIP_MIN_GS,
    "airpath_dip_recovery_s": AIRPATH_DIP_RECOVERY_S,
    "airpath_dip_recovered_fraction": AIRPATH_DIP_RECOVERED_FRACTION,
    "airpath_dip_rpm_steady": AIRPATH_DIP_RPM_STEADY,
    "airpath_warm_coolant_c": AIRPATH_WARM_COOLANT_C,
    "airpath_assoc_window_s": AIRPATH_ASSOC_WINDOW_S,
    "airpath_baseline_window_s": AIRPATH_BASELINE_WINDOW_S,
    "airpath_map_change_kpa": AIRPATH_MAP_CHANGE_KPA,
    "airpath_egr_cmd_change_pct": AIRPATH_EGR_CMD_CHANGE_PCT,
    "airpath_rpm_unstable_std": AIRPATH_RPM_UNSTABLE_STD,
    "airpath_rpm_unstable_factor": AIRPATH_RPM_UNSTABLE_FACTOR,
    "airpath_annotation_window_s": AIRPATH_ANNOTATION_WINDOW_S,
    "egr_error_screen_pct": EGR_ERROR_SCREEN_PCT,
    "airpath_egr_excursion_gap_s": AIRPATH_EGR_EXCURSION_GAP_S,
    "airpath_min_running_maf_samples": AIRPATH_MIN_RUNNING_MAF_SAMPLES,
    "airpath_min_history_sessions": AIRPATH_MIN_HISTORY_SESSIONS,
}

MANUFACTURER_DPF_KEYS = ("dpf_diff_pressure", "dpf_soot_load", "dpf_regen_status", "exhaust_backpressure")
DPF_WANTED = (
    ("regeneration_state", "DPF regeneration active / inactive", None),
    ("regeneration_request", "Regeneration request / status", None),
    ("soot_mass", "Calculated soot mass", None),
    ("ash_loading", "Ash loading", None),
    ("differential_pressure", "DPF differential pressure", 0x7A),
    ("exhaust_temperatures", "Exhaust gas temperatures", 0x78),
    ("since_last_regeneration", "Distance / time since last successful regeneration", None),
    ("failed_regeneration_indicators", "Failed / interrupted regeneration indicators", None),
)


def is_monitoring_session(session: dict[str, Any]) -> bool:
    meta = session.get("meta") or {}
    return bool(meta.get("sampling_plan") or meta.get("monitoring"))


# -- reference pools (real vs synthetic, one vehicle) ---------------------------------------------


def baseline_origin(session: dict[str, Any]) -> str:
    """'synthetic' (simulator, or a replay of simulator data) or 'real' (a real adapter, or a replay
    of a real recording). Replays carry the origin of the data in the transcript header."""
    return "synthetic" if session.get("synthetic") else "real"


def vehicle_identity(session: dict[str, Any]) -> str | None:
    """A CONFIRMED vehicle identity of the session, or None. Never guessed: only an explicit
    ``meta["vehicle_identity"]`` or a VIN the ECU actually returned in this session counts."""
    meta = session.get("meta") or {}
    ident = meta.get("vehicle_identity")
    if ident:
        return str(ident)
    info = meta.get("vehicle_info") or {}
    if info.get("vin_status") == "read" and info.get("vin"):
        return "vin:" + str(info["vin"]).strip().upper()
    return None


def baseline_cohort(session: dict[str, Any]) -> tuple[str, str, str | None]:
    """Sessions may share diagnostic references only within one cohort: same vehicle profile,
    same data origin (real / synthetic) and the same confirmed identity (or both unconfirmed)."""
    return (session["vehicle_profile"], baseline_origin(session), vehicle_identity(session))


def cohort_label(cohort: tuple[str, str, str | None]) -> dict[str, Any]:
    """Public description of a cohort. A VIN is never exposed: only a short digest of it."""
    profile, origin, ident = cohort
    return {
        "vehicle_profile": profile,
        "origin": origin,
        "vehicle_identity": None if ident is None else hashlib.sha256(ident.encode()).hexdigest()[:10],
    }


def reference_pool(current: SessionData, history: list[SessionData] | None) -> list[SessionData]:
    """Monitoring sessions that may feed the references used to judge ``current``: same cohort
    only (a synthetic recording never moves a real vehicle's baseline, nor the other way round)."""
    key = baseline_cohort(current.session)
    pool = {
        h.id: h
        for h in (history or [])
        if is_monitoring_session(h.session) and baseline_cohort(h.session) == key
    }
    pool[current.id] = current
    return list(pool.values())


# -- small helpers ------------------------------------------------------------------------------


@dataclass
class _S:
    t: float  # wall time
    v: float


def _cache(sd: SessionData) -> dict[Any, Any]:
    """Per-session memo of derived data (stored samples never change once loaded), so the
    same series / runs / warm-idle sets are not rebuilt for every summary and reference."""
    c = sd.__dict__.get("_monitoring_cache")
    if c is None:
        c = sd.__dict__["_monitoring_cache"] = {}
    return c


def _series(sd: SessionData, pid: int, mode: int = 1) -> list[_S]:
    """Measured values of one signal in time order (stable: equal timestamps keep stored order)."""
    key = ("series", pid, mode)
    c = _cache(sd)
    if key not in c:
        prim = sd.primary_ecu
        pts = [
            _S(s["t_wall"], s["value"])
            for s in sd.samples
            if s["mode"] == mode
            and s["pid"] == pid
            and s["quality"] == "ok"
            and s["value"] is not None
            and (mode != 1 or s["ecu"] in (None, prim or s["ecu"]))
        ]
        if any(b.t < a.t for a, b in zip(pts, pts[1:], strict=False)):
            pts.sort(key=lambda p: p.t)
        c[key] = pts
    return c[key]


class _Cursor:
    """Held-value lookup on a time-ordered series: the most recent measured value at or before t.

    Queries in non-decreasing time order (the usual case) advance a pointer, so N lookups
    over a series of M points cost O(N + M); a query that goes back in time falls back to a
    binary search. Values are never interpolated. ``steps`` counts pointer moves and searches.
    """

    def __init__(self, series: list[_S]) -> None:
        self.series = series
        self.times = [s.t for s in series]
        self.i = -1  # index of the last point with t <= the previous query
        self.last_t = float("-inf")
        self.steps = 0

    def at(self, t: float) -> float | None:
        if t < self.last_t:
            self.i = bisect.bisect_right(self.times, t) - 1
            self.steps += 1
        else:
            n = len(self.times)
            while self.i + 1 < n and self.times[self.i + 1] <= t:
                self.i += 1
                self.steps += 1
        self.last_t = t
        return self.series[self.i].v if self.i >= 0 else None


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 2) if xs else None


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {"n": len(xs), "min": round(min(xs), 2), "median": _median(xs), "max": round(max(xs), 2)}


class _Clock:
    def __init__(self, sd: SessionData) -> None:
        walls = [s["t_wall"] for s in sd.samples]
        self.t0 = min(walls) if walls else sd.session["started_at"]

    def rel(self, t: float | None) -> float | None:
        return None if t is None else round(t - self.t0, 2)


def _events(sd: SessionData, *types: str, origin: str | None = None) -> list[Any]:
    return [
        a
        for a in sd.annotations
        if (a.event_type in types or (not a.event_type and a.label in types))
        and (origin is None or a.origin == origin)
    ]


# -- engine runs & shutdowns --------------------------------------------------------------------


def engine_runs(sd: SessionData) -> list[dict[str, Any]]:
    """Contiguous running segments from measured RPM. A start/stop is 'observed' only when a
    non-running RPM sample was measured right before/after it. Returns fresh dicts (callers
    annotate them); the computation itself is memoised per session."""
    c = _cache(sd)
    if "runs" not in c:
        c["runs"] = _engine_runs(sd)
    return [dict(r) for r in c["runs"]]


def _engine_runs(sd: SessionData) -> list[dict[str, Any]]:
    rpm = _series(sd, RPM)
    runs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    prev: _S | None = None
    for s in rpm:
        running = s.v >= RUNNING_RPM
        if running and cur is None:
            cur = {"t_start": s.t, "start_observed": prev is not None and prev.v < RUNNING_RPM, "t_end": s.t}
        elif running and cur is not None:
            cur["t_end"] = s.t
        elif not running and cur is not None:
            cur["stop_observed"] = True
            cur["t_stopped_sample"] = s.t
            runs.append(cur)
            cur = None
        prev = s
    if cur is not None:
        cur["stop_observed"] = False
        runs.append(cur)
    for r in runs:
        r["duration_s"] = round(r["t_end"] - r["t_start"], 1)
    return runs


def _running_at(runs: list[dict[str, Any]], t: float) -> bool:
    return any(r["t_start"] <= t <= r["t_end"] for r in runs)


def _running_test(runs: list[dict[str, Any]]):
    """``_running_at`` for many timestamps: binary search over the (ordered, disjoint) runs."""
    ordered = sorted(runs, key=lambda r: r["t_start"])
    starts = [r["t_start"] for r in ordered]

    def running(t: float) -> bool:
        i = bisect.bisect_right(starts, t) - 1
        return i >= 0 and ordered[i]["t_start"] <= t <= ordered[i]["t_end"]

    return running


def _restart_between(starts: list[float], t0: float, t1: float) -> bool:
    """An engine start (measured or marked) strictly after t0 and at or before t1."""
    i = bisect.bisect_right(starts, t0)
    return i < len(starts) and starts[i] <= t1


def shutdowns(sd: SessionData, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shutdown EPISODES: one entry per physical engine shutdown.

    A shutdown is evidenced by an observed engine stop (measured RPM fell to ~0 while the ECU
    kept answering) and/or a manual ``engine_stopped`` / ``ignition_off`` marker. Markers within
    SHUTDOWN_EPISODE_S of each other belong to one episode unless the engine started again in
    between (measured RPM start or a manual ``engine_started`` marker). An observed
    ``ecu_communication_lost`` is attached to the episode it follows, as context; on its own it
    never creates a shutdown (an ECU going silent is not proof that the engine stopped).

    Every original event stays in the timeline; each episode lists the events it groups with
    their own timestamps and origins. ``t_wall`` / ``origin`` / ``basis`` describe the anchor:
    the observed engine stop when there is one, otherwise the earliest manual marker.
    """
    observed_ev = _events(sd, "engine_stopped", origin="observed")
    marks: list[dict[str, Any]] = []
    for r in runs:
        if r.get("stop_observed"):
            ev = next((a for a in observed_ev if abs(a.t_wall - r["t_stopped_sample"]) < 2.0), None)
            marks.append(
                {
                    "t_wall": r["t_stopped_sample"],
                    "kind": "engine_stopped",
                    "origin": "observed",
                    "basis": "measured RPM fell to ~0 while the ECU kept answering",
                    "snapshot": ev.evidence if ev else None,
                    "event_t_wall": ev.t_wall if ev else None,
                }
            )
    for a in _events(sd, "engine_stopped", "ignition_off", origin="manual"):
        kind = a.event_type or a.label
        marks.append({"t_wall": a.t_wall, "kind": kind, "origin": "manual", "basis": f"marked '{kind}'"})
    marks.sort(key=lambda m: (m["t_wall"], m["origin"] != "observed"))
    starts = sorted(
        [r["t_start"] for r in runs] + [a.t_wall for a in _events(sd, "engine_started", origin="manual")]
    )
    groups: list[list[dict[str, Any]]] = []
    for m in marks:
        if groups:
            last = groups[-1][-1]["t_wall"]
            if m["t_wall"] - last <= SHUTDOWN_EPISODE_S and not _restart_between(starts, last, m["t_wall"]):
                groups[-1].append(m)
                continue
        groups.append([m])

    out: list[dict[str, Any]] = []
    for idx, g in enumerate(groups):
        observed = next((m for m in g if m["kind"] == "engine_stopped" and m["origin"] == "observed"), None)
        manual_stop = next((m for m in g if m["kind"] == "engine_stopped" and m["origin"] == "manual"), None)
        ign = next((m for m in g if m["kind"] == "ignition_off"), None)
        anchor = observed or manual_stop or g[0]
        t_first = g[0]["t_wall"]
        nxt = next((t for t in starts if t > g[-1]["t_wall"]), None)
        events = [
            {"event_type": m["kind"], "origin": m["origin"], "t_wall": m.get("event_t_wall") or m["t_wall"]}
            for m in g
        ]
        basis = anchor["basis"]
        if observed is None and manual_stop is None:
            basis += " (no engine stop observed or marked; ignition-off reported by a person)"
        out.append(
            {
                "episode": idx,
                "t_wall": anchor["t_wall"],
                "origin": anchor["origin"],
                "basis": basis,
                "snapshot": observed["snapshot"] if observed else None,
                "engine_stop": None
                if observed is None and manual_stop is None
                else {
                    "origin": "observed" if observed else "manual",
                    "t_wall": (observed or manual_stop)["t_wall"],  # type: ignore[index]
                },
                "engine_stop_marked": manual_stop["t_wall"] if manual_stop else None,
                "ignition_off": {"origin": "manual", "t_wall": ign["t_wall"]} if ign else None,
                "ecu_communication_lost": None,
                "t_first_wall": t_first,
                "t_last_wall": g[-1]["t_wall"],
                "t_next_start_wall": nxt,
                "events": events,
            }
        )
    # ECU silence: context of the shutdown it follows (never a shutdown by itself)
    for a in _events(sd, "ecu_communication_lost", origin="observed"):
        cands = [
            ep
            for ep in out
            if ep["t_first_wall"] - SHUTDOWN_EPISODE_S <= a.t_wall
            and (ep["t_next_start_wall"] is None or a.t_wall < ep["t_next_start_wall"])
            and not _restart_between(
                starts, min(a.t_wall, ep["t_first_wall"]), max(a.t_wall, ep["t_first_wall"])
            )
        ]
        if not cands:
            continue
        ep = min(cands, key=lambda e: abs(a.t_wall - e["t_wall"]))
        if ep["ecu_communication_lost"] is None:
            ep["ecu_communication_lost"] = {"origin": "observed", "t_wall": a.t_wall}
        ep["events"].append(
            {"event_type": "ecu_communication_lost", "origin": "observed", "t_wall": a.t_wall}
        )
    for ep in out:
        ep["events"].sort(key=lambda e: e["t_wall"])
    return out


def _last_before(series: list[_S], t: float) -> dict[str, Any] | None:
    i = bisect.bisect_right(series, t, key=lambda s: s.t) - 1
    prev = series[i] if i >= 0 else None
    return None if prev is None else {"value": prev.v, "age_s": round(t - prev.t, 1)}


def _after(series: list[_S], t: float, until: float | None = None) -> list[_S]:
    lo = bisect.bisect_right(series, t, key=lambda s: s.t)
    hi = len(series) if until is None else bisect.bisect_right(series, until, key=lambda s: s.t)
    return series[lo:hi]


def fan_runs(sd: SessionData, stops: list[dict[str, Any]]) -> dict[str, Any]:
    """Pairs manual fan_running -> fan_stopped markers (time order). Fan activity is never
    measured by this tool; every duration here comes from what a person marked."""
    marks = sorted(_events(sd, "fan_running", "fan_stopped"), key=lambda a: a.t_wall)
    runs: list[dict[str, Any]] = []
    orphans: list[float] = []
    open_run: Any = None
    for a in marks:
        kind = a.event_type or a.label
        if kind == "fan_running":
            if open_run is None:
                open_run = a
        elif open_run is not None:
            runs.append({"start": open_run, "end": a})
            open_run = None
        else:
            orphans.append(a.t_wall)
    if open_run is not None:
        runs.append({"start": open_run, "end": None})
    loud = [a.t_wall for a in _events(sd, "fan_unusually_loud")]
    out = []
    for r in runs:
        st, en = r["start"], r["end"]
        prior = [
            s
            for s in stops
            if s["t_wall"] <= st.t_wall
            and st.t_wall - s["t_wall"] <= FAN_AFTER_SHUTDOWN_S
            and (s.get("t_next_start_wall") is None or st.t_wall < s["t_next_start_wall"])
        ]
        stop = prior[-1] if prior else None
        out.append(
            {
                "t_start_wall": st.t_wall,
                "t_end_wall": en.t_wall if en else None,
                "duration_s": round(en.t_wall - st.t_wall, 1) if en else None,
                "complete": en is not None,
                "origin": "manual",
                "after_shutdown": stop is not None,
                "seconds_after_shutdown": round(st.t_wall - stop["t_wall"], 1) if stop else None,
                "shutdown_origin": stop["origin"] if stop else None,
                "shutdown_episode": stop.get("episode") if stop else None,
                "marked_unusually_loud": any(
                    st.t_wall <= t <= (en.t_wall if en else st.t_wall + FAN_AFTER_SHUTDOWN_S) for t in loud
                ),
                "notes": [n for n in (st.note, en.note if en else "") if n],
            }
        )
    return {
        "runs": out,
        "fan_stopped_without_running": orphans,
        "source": "manual annotations (fan_running -> fan_stopped); no OBD signal reports the fan",
    }


# -- voltage --------------------------------------------------------------------------------------


def _voltage_source(
    series: list[_S], runs: list[dict[str, Any]], stops: list[dict[str, Any]], clk: _Clock
) -> dict[str, Any]:
    if not series:
        return {"status": "not_measured"}
    is_running = _running_test(runs)
    running = [s.v for s in series if is_running(s.t)]
    off = [s.v for s in series if not is_running(s.t)]
    cranks = [r["t_start"] for r in runs if r["start_observed"]]
    stop_ts = [r["t_stopped_sample"] for r in runs if r.get("stop_observed")] + [st["t_wall"] for st in stops]
    drops = []
    for a, b in zip(series, series[1:], strict=False):
        if b.t - a.t <= VOLT_DROP_WINDOW_S and a.v - b.v >= VOLT_DROP_V:
            context = None
            if any(-2.0 <= c - b.t <= CRANK_WINDOW_S for c in cranks):
                context = "engine_start"  # cranking dip
            elif any(abs(b.t - t) <= CRANK_WINDOW_S for t in stop_ts):
                context = "engine_stop"  # charging ends when the engine stops
            drops.append(
                {
                    "t_rel_s": clk.rel(b.t),
                    "from_v": a.v,
                    "to_v": b.v,
                    "drop_v": round(a.v - b.v, 2),
                    "context": context,
                }
            )
    per_stop = []
    for st in stops:
        nxt = next((r["t_start"] for r in runs if r["t_start"] > st["t_wall"]), None)
        post = _after(series, st["t_wall"], nxt)
        trend = None
        if len(post) >= 3 and post[-1].t - post[0].t >= 300:
            hours = (post[-1].t - post[0].t) / 3600
            trend = round((post[-1].v - post[0].v) / hours, 3)
        per_stop.append(
            {
                "t_rel_s": clk.rel(st["t_wall"]),
                "origin": st["origin"],
                "last_before": _last_before(series, st["t_wall"]),
                "after": _stats([s.v for s in post]),
                "after_last": {"value": post[-1].v, "t_rel_s": clk.rel(post[-1].t)} if post else None,
                "trend_v_per_h": trend,
            }
        )
    low = [s for s in series if is_running(s.t) and s.v < VOLT_LOW_RUNNING_V]
    rest = _median(off)
    run_med = _median(running)
    return {
        "status": "measured",
        "at_start": {"value": series[0].v, "t_rel_s": clk.rel(series[0].t)},
        "engine_running": _stats(running),
        "engine_off": _stats(off),
        "no_charging_rise_seen": (
            run_med is not None and rest is not None and run_med < rest + VOLT_NO_RISE_V
        ),
        "low_while_running": {
            "screen_v": VOLT_LOW_RUNNING_V,
            "samples": len(low),
            "share": round(len(low) / len(running), 3) if running else None,
            "min_v": round(min(s.v for s in low), 2) if low else None,
        },
        "drops": drops,
        "shutdowns": per_stop,
        "points": [[clk.rel(s.t), s.v] for s in series],
    }


def voltage(sd: SessionData, runs, stops, clk: _Clock) -> dict[str, Any]:
    ecu = _series(sd, ECU_V)
    ad = _series(sd, ADAPTER_VOLTAGE_PID, mode=ADAPTER_MODE)
    warnings = []
    for a in _events(sd, "battery_warning", "battery_warning_cleared"):
        near = {}
        for name, ser in (("ecu_voltage", ecu), ("adapter_voltage", ad)):
            pts = [s for s in ser if abs(s.t - a.t_wall) <= EVENT_VOLTAGE_WINDOW_S]
            near[name] = (
                {
                    "min": round(min(p.v for p in pts), 2),
                    "max": round(max(p.v for p in pts), 2),
                    "n": len(pts),
                    "window_s": EVENT_VOLTAGE_WINDOW_S,
                }
                if pts
                else None
            )
        warnings.append(
            {
                "event": a.event_type or a.label,
                "t_rel_s": clk.rel(a.t_wall),
                "origin": a.origin,
                "engine_running": _running_at(runs, a.t_wall),
                "nearby": near,
                "note": a.note,
            }
        )
    return {
        "ecu": {
            **_voltage_source(ecu, runs, stops, clk),
            "provenance": "ECU (PID 42 control module voltage)",
        },
        "adapter": {
            **_voltage_source(ad, runs, stops, clk),
            "provenance": "adapter (ATRV: supply voltage measured by the adapter itself)",
        },
        "battery_warnings": warnings,
        "caveat": (
            "Voltage is not battery state of charge or battery health. The ECU and the adapter measure at "
            "different points with different accuracy; neither is calibrated by this tool."
        ),
    }


# -- coolant ---------------------------------------------------------------------------------------


def _running_s(runs: list[dict[str, Any]]) -> float:
    return sum(r["t_end"] - r["t_start"] for r in runs)


def warm_plateau(history: list[SessionData]) -> dict[str, Any]:
    """The vehicle's own usual warm coolant temperature: median, over monitoring sessions that
    ran at least COOLANT_MIN_RUN_S, of the 90th percentile of running coolant values."""
    vals = []
    for sd in history:
        runs = engine_runs(sd)
        if _running_s(runs) < COOLANT_MIN_RUN_S:
            continue
        is_running = _running_test(runs)
        cool = sorted(s.v for s in _series(sd, COOLANT) if is_running(s.t))
        if len(cool) >= 20:
            vals.append(cool[int(0.9 * (len(cool) - 1))])
    if not vals:
        return {"value": None, "sessions": 0}
    return {"value": round(statistics.median(vals), 1), "sessions": len(vals)}


def coolant(sd: SessionData, runs, stops, clk: _Clock, plateau: dict[str, Any]) -> dict[str, Any]:
    ser = _series(sd, COOLANT)
    if not ser:
        return {"status": "not_measured", "detected": []}
    is_running = _running_test(runs)
    running = [s for s in ser if is_running(s.t)]
    ref = plateau.get("value")
    detected: list[dict[str, Any]] = []
    limits: list[str] = []
    if ref is None:
        limits.append(
            "No usual warm temperature for this vehicle yet (needs monitoring sessions with >= "
            f"{COOLANT_MIN_RUN_S / 60:.0f} min engine running); warm-up and high-temperature checks are relative "
            "to it, so they were not evaluated."
        )
    run_s = _running_s(runs)
    if ref is not None and running:
        mx = max(s.v for s in running)
        reached = next((s for s in running if s.v >= ref - 5), None)
        if run_s >= COOLANT_MIN_RUN_S and mx < ref - COOLANT_LOW_MARGIN_C and plateau.get("sessions", 0) >= 2:
            detected.append(
                {
                    "type": "coolant_below_usual_temperature",
                    "origin": "inferred",
                    "status": "possible",
                    "confidence": LOW_CONFIDENCE,
                    "text": f"Coolant peaked at {mx:.0f} °C after {run_s / 60:.0f} min running; this vehicle usually "
                    f"reaches ~{ref:.0f} °C (median over {plateau['sessions']} sessions).",
                    "values": {"max_c": mx, "usual_c": ref, "running_s": round(run_s)},
                }
            )
        if reached is not None:
            after = [s for s in running if s.t > reached.t]
            if after:
                lo = min(after, key=lambda s: s.v)
                if lo.v <= ref - 5 - COOLANT_DROP_C:
                    detected.append(
                        {
                            "type": "coolant_dropped_after_warmup",
                            "origin": "inferred",
                            "status": "possible",
                            "confidence": LOW_CONFIDENCE,
                            "t_rel_s": clk.rel(lo.t),
                            "text": f"After reaching {reached.v:.0f} °C the coolant fell to {lo.v:.0f} °C with the engine running.",
                            "values": {"reached_c": reached.v, "min_after_c": lo.v, "usual_c": ref},
                        }
                    )
        hi = [s for s in running if s.v >= ref + COOLANT_HIGH_MARGIN_C]
        episodes = []
        for s in hi:
            if not episodes or s.t - episodes[-1][-1].t > 60:
                episodes.append([s])
            else:
                episodes[-1].append(s)
        if len(episodes) >= 2:
            detected.append(
                {
                    "type": "coolant_repeatedly_above_usual",
                    "origin": "inferred",
                    "status": "possible",
                    "confidence": LOW_CONFIDENCE,
                    "text": f"{len(episodes)} separate episodes at or above {ref + COOLANT_HIGH_MARGIN_C:.0f} °C "
                    f"(usual ~{ref:.0f} °C), peak {max(s.v for s in hi):.0f} °C.",
                    "values": {
                        "episodes": len(episodes),
                        "peak_c": max(s.v for s in hi),
                        "screen_c": ref + COOLANT_HIGH_MARGIN_C,
                    },
                }
            )
    post = []
    for st in stops:
        nxt = next((r["t_start"] for r in runs if r["t_start"] > st["t_wall"]), None)
        after = _after(ser, st["t_wall"], nxt)
        before = _last_before(ser, st["t_wall"])
        post.append(
            {
                "t_rel_s": clk.rel(st["t_wall"]),
                "origin": st["origin"],
                "last_before": before,
                "after": _stats([s.v for s in after]),
                "max_rise_c": round(max(s.v for s in after) - before["value"], 1)
                if after and before
                else None,
                "measured_for_s": round(after[-1].t - st["t_wall"], 1) if after else 0.0,
            }
        )
    first = running[0] if running else ser[0]
    return {
        "status": "measured",
        "first": {"value": first.v, "t_rel_s": clk.rel(first.t)},
        "max_running": max((s.v for s in running), default=None),
        "usual_warm_c": ref,
        "usual_warm_sessions": plateau.get("sessions", 0),
        "running_s": round(run_s, 1),
        "after_shutdown": post,
        "ambient_c": _median([s.v for s in _series(sd, AMBIENT)]),
        "detected": detected,
        "limitations": limits,
        "points": [[clk.rel(s.t), s.v] for s in ser],
    }


# -- DPF ---------------------------------------------------------------------------------------------


def dpf_availability(sd: SessionData) -> list[dict[str, Any]]:
    """Each wanted DPF measurement -> measured / raw_only / not_available, with the reason."""
    prof = get_profile(sd.session["vehicle_profile"])
    raw_counts: dict[int, int] = {}
    for s in sd.samples:
        if s["mode"] == 1 and s["pid"] in DPF_STANDARD_PIDS and s["quality"] in GOOD:
            raw_counts[s["pid"]] = raw_counts.get(s["pid"], 0) + 1
    standard = []
    for pid in DPF_STANDARD_PIDS:
        sup = sd.supported(pid)
        decoded = pid in PIDS
        if raw_counts.get(pid):
            status = "measured" if decoded else "raw_only"
            reason = (
                "decoded with a verified formula"
                if decoded
                else "ECU answers; layout not verified for this project, raw bytes stored, no value"
            )
        elif sup:
            status, reason = "not_available", "advertised by the ECU but no usable answer in this session"
        elif sup is False:
            status, reason = "not_available", "not advertised by this ECU via generic OBD"
        else:
            status, reason = "not_available", "no capability data for this session"
        standard.append(
            {
                "pid": pid,
                "hex": f"{pid:02X}",
                "name": RAW_ONLY_PIDS.get(pid) or pid_name(pid),
                "status": status,
                "samples": raw_counts.get(pid, 0),
                "reason": reason,
            }
        )
    mfr = []
    for ecu_id in prof.ecu_ids:
        for p in ECU_DEFINITIONS[ecu_id].manufacturer_parameters:
            if p.key in MANUFACTURER_DPF_KEYS:
                mfr.append(
                    {
                        "key": p.key,
                        "name": p.name,
                        "status": "not_available",
                        "reason": "manufacturer (BMW DDE) value: " + p.reason,
                    }
                )
    by_pid = {d["pid"]: d for d in standard}
    wanted = []
    for key, name, pid in DPF_WANTED:
        if pid is not None and by_pid[pid]["status"] != "not_available":
            wanted.append(
                {"key": key, "name": name, "status": by_pid[pid]["status"], "via": f"PID {pid:02X}"}
            )
        else:
            wanted.append(
                {
                    "key": key,
                    "name": name,
                    "status": "not_available",
                    "via": "manufacturer-only (BMW DDE); no verified request in this tool"
                    if pid is None
                    else f"PID {pid:02X} not available; otherwise manufacturer-only",
                }
            )
    return [
        {"group": "wanted", "items": wanted},
        {"group": "standard_pids", "items": standard},
        {"group": "manufacturer", "items": mfr},
    ]


def _warm_idle(sd: SessionData, plateau_c: float | None) -> dict[int, list[_S]]:
    """RPM / MAF / commanded-EGR samples taken at warm stationary idle. Each sample is judged with
    the most recent measured RPM, speed and coolant at its own timestamp (held values, never
    interpolated). Memoised per session and reference temperature; linear in the sample count."""
    key = ("warm_idle", plateau_c)
    c = _cache(sd)
    if key not in c:
        c[key] = _compute_warm_idle(sd, plateau_c)
    return c[key]


def _compute_warm_idle(sd: SessionData, plateau_c: float | None) -> dict[int, list[_S]]:
    rpm, speed, cool = _series(sd, RPM), _series(sd, SPEED), _series(sd, COOLANT)
    if not rpm or not speed or not cool:
        return {}
    ref = plateau_c if plateau_c is not None else max(s.v for s in cool)
    if ref < WARM_MIN_PLATEAU_C:
        return {}
    out: dict[int, list[_S]] = {}
    for pid in (RPM, MAF, EGR_CMD):
        # one set of cursors per (time-ordered) target series: lookups only move forward
        c_rpm, c_speed, c_cool = _Cursor(rpm), _Cursor(speed), _Cursor(cool)
        keep = []
        for s in _series(sd, pid):
            r, v, cv = c_rpm.at(s.t), c_speed.at(s.t), c_cool.at(s.t)
            if (
                r is not None
                and r >= RUNNING_RPM
                and v is not None
                and v <= IDLE_MAX_SPEED
                and cv is not None
                and cv >= ref - WARM_BAND_C
            ):
                keep.append(s)
        out[pid] = keep
    return out


def regen_baseline(history: list[SessionData], plateau_c: float | None) -> dict[str, Any]:
    pools: dict[int, list[float]] = {RPM: [], MAF: [], EGR_CMD: []}
    for sd in history:
        for pid, pts in _warm_idle(sd, plateau_c).items():
            pools[pid].extend(s.v for s in pts)
    return {
        "rpm": _median(pools[RPM]) if len(pools[RPM]) >= REGEN_MIN_BASELINE else None,
        "maf": _median(pools[MAF]) if len(pools[MAF]) >= REGEN_MIN_BASELINE else None,
        "egr_cmd": _median(pools[EGR_CMD]) if len(pools[EGR_CMD]) >= REGEN_MIN_BASELINE else None,
        "samples": {"rpm": len(pools[RPM]), "maf": len(pools[MAF]), "egr_cmd": len(pools[EGR_CMD])},
    }


def possible_regenerations(
    sd: SessionData, baseline: dict[str, Any], plateau_c: float | None, clk: _Clock
) -> tuple[list[dict[str, Any]], list[str]]:
    """Indirect, heuristic detection of a *possible* regeneration at warm stationary idle.

    A 30-s window counts when at least two of: commanded EGR nearly closed, idle RPM raised,
    MAF raised - each relative to this vehicle's usual warm idle. Episodes of >= 60 s are
    reported as 'possible' with low (2 indicators) or moderate (3) confidence. Never confirmed:
    no verified regeneration-state signal is available through generic OBD here.
    """
    limits: list[str] = []
    usable = {k: baseline.get(k) for k in ("rpm", "maf", "egr_cmd")}
    egr_ok = usable["egr_cmd"] is not None and usable["egr_cmd"] >= REGEN_EGR_MIN_BASELINE_PCT
    n_ind = sum(1 for k in ("rpm", "maf") if usable[k] is not None) + int(egr_ok)
    if n_ind < 2:
        limits.append(
            "Possible-regeneration heuristic not evaluated: it needs a baseline of at least "
            f"{REGEN_MIN_BASELINE} warm stationary-idle samples for at least two of RPM, MAF and commanded EGR "
            f"(have {baseline.get('samples')})."
        )
        return [], limits
    pts = _warm_idle(sd, plateau_c)
    if not pts or not pts.get(RPM):
        limits.append(
            "No warm stationary idle in this session: possible-regeneration heuristic not applicable."
        )
        return [], limits
    t_first = min(s.t for ser in pts.values() for s in ser)
    buckets: dict[int, dict[int, list[float]]] = {}
    for pid, ser in pts.items():
        for s in ser:
            buckets.setdefault(int((s.t - t_first) // REGEN_WINDOW_S), {}).setdefault(pid, []).append(s.v)
    hits: list[tuple[int, list[str], dict[str, float]]] = []
    for k in sorted(buckets):
        b = buckets[k]
        ind: list[str] = []
        vals: dict[str, float] = {}
        if usable["rpm"] is not None and len(b.get(RPM, [])) >= 3:
            m = statistics.median(b[RPM])
            vals["rpm"] = round(m, 1)
            if m >= usable["rpm"] + REGEN_IDLE_RPM_RISE:
                ind.append("idle_rpm_raised")
        if usable["maf"] is not None and len(b.get(MAF, [])) >= 3:
            m = statistics.median(b[MAF])
            vals["maf"] = round(m, 2)
            if m >= usable["maf"] * REGEN_MAF_RISE:
                ind.append("maf_raised")
        if egr_ok and len(b.get(EGR_CMD, [])) >= 2:
            m = statistics.median(b[EGR_CMD])
            vals["egr_cmd"] = round(m, 1)
            if m <= usable["egr_cmd"] * REGEN_EGR_CLOSED_FRACTION:
                ind.append("egr_nearly_closed")
        if len(ind) >= 2:
            hits.append((k, ind, vals))
    episodes: list[list[tuple[int, list[str], dict[str, float]]]] = []
    for h in hits:
        if episodes and h[0] - episodes[-1][-1][0] <= 2:
            episodes[-1].append(h)
        else:
            episodes.append([h])
    out = []
    for ep in episodes:
        t_start = t_first + ep[0][0] * REGEN_WINDOW_S
        t_end = t_first + (ep[-1][0] + 1) * REGEN_WINDOW_S
        if t_end - t_start < REGEN_MIN_EPISODE_S:
            continue
        inds = sorted({i for _, ii, _ in ep for i in ii})
        out.append(
            {
                "type": "possible_dpf_regeneration",
                "origin": "inferred",
                "status": "possible",
                "confidence": MODERATE_CONFIDENCE if len(inds) >= 3 else LOW_CONFIDENCE,
                "t_start_wall": t_start,
                "t_end_wall": t_end,
                "t_start_rel_s": clk.rel(t_start),
                "t_end_rel_s": clk.rel(t_end),
                "indicators": inds,
                "window_values": [v for _, _, v in ep],
                "baseline": {k: usable[k] for k in usable},
                "ecu_evidence": "none: no verified regeneration-state signal is readable through generic OBD here",
                "text": f"Possible DPF regeneration ({len(inds)} indirect indicators: {', '.join(inds)}) for "
                f"{(t_end - t_start):.0f} s at warm stationary idle. Heuristic only; not confirmed by the ECU.",
            }
        )
    return out, limits


def driving_conditions(sd: SessionData, runs: list[dict[str, Any]]) -> dict[str, Any]:
    speed = _series(sd, SPEED)
    run_s = _running_s(runs)
    moving = [s.v for s in speed if s.v > 0]
    span = (speed[-1].t - speed[0].t) if len(speed) > 1 else 0.0
    return {
        "running_s": round(run_s, 1),
        "speed_samples": len(speed),
        "moving_share": round(len(moving) / len(speed), 3) if speed else None,
        "mean_speed_moving_kmh": round(statistics.fmean(moving), 1) if moving else None,
        "max_speed_kmh": max((s.v for s in speed), default=None),
        "speed_span_s": round(span, 1),
        "drive_type_user_label": (sd.session["meta"].get("user_labels") or {}).get("drive_type"),
    }


def interrupted_regenerations(
    sd: SessionData,
    regens: list[dict[str, Any]],
    stops: list[dict[str, Any]],
    fans: dict[str, Any],
    drive: dict[str, Any],
    clk: _Clock,
) -> list[dict[str, Any]]:
    """A possible (inferred) or user-suspected regeneration still active at an engine stop,
    correlated with fan runs after that stop and the driving conditions. Heuristic only."""
    # one result per shutdown EPISODE (engine stop, ignition-off marker and the ECU going silent
    # are one episode). An ECU loss that belongs to no episode is kept as a separate end, but it is
    # never described as a shutdown.
    in_episode = {
        e["t_wall"]
        for ep in stops
        for e in ep.get("events", [])
        if e["event_type"] == "ecu_communication_lost"
    }
    ends: list[tuple[float, str, str, dict[str, Any] | None]] = [
        (s["t_wall"], s["origin"], "engine stop" if s.get("engine_stop") else "ignition-off marker", s)
        for s in stops
    ] + [
        (a.t_wall, "observed", "ECU communication lost (not proof of an engine stop)", None)
        for a in _events(sd, "ecu_communication_lost", origin="observed")
        if a.t_wall not in in_episode
    ]
    suspected = _events(sd, "suspected_dpf_regeneration")
    out = []
    for t_stop, origin, what, episode in sorted(ends, key=lambda e: e[0]):
        active = [
            r
            for r in regens
            if 0 <= t_stop - r["t_end_wall"] <= REGEN_AT_SHUTDOWN_S
            or r["t_start_wall"] <= t_stop <= r["t_end_wall"]
        ]
        manual = [a for a in suspected if 0 <= t_stop - a.t_wall <= 600]
        if not active and not manual:
            continue
        if episode is not None:
            runs_after = [f for f in fans["runs"] if f.get("shutdown_episode") == episode.get("episode")]
        else:
            runs_after = [
                f
                for f in fans["runs"]
                if f["t_start_wall"] >= t_stop and f["t_start_wall"] - t_stop <= FAN_AFTER_SHUTDOWN_S
            ]
        support = []
        if active:
            support.append(
                f"inferred possible regeneration active within {REGEN_AT_SHUTDOWN_S:.0f} s of the {what}"
            )
        if manual:
            support.append("user marked 'suspected DPF regeneration' in the 10 min before")
        if runs_after:
            support.append(
                "fan marked running after the stop"
                + "".join(f" ({f['duration_s']:.0f} s)" for f in runs_after if f["duration_s"] is not None)
            )
        out.append(
            {
                "type": "possible_interrupted_regeneration",
                "origin": "inferred",
                "status": "possible",
                "confidence": LOW_CONFIDENCE,
                "t_wall": t_stop,
                "t_rel_s": clk.rel(t_stop),
                "ended_by": what,
                "ended_by_origin": origin,
                "shutdown_episode": episode.get("episode") if episode else None,
                "supporting": support,
                "fan_runs_after_s": [f["duration_s"] for f in runs_after],
                "driving": drive,
                "exhaust_temperature": "not available as a value (raw-only or not offered); not used",
                "ecu_evidence": "none: regeneration state, soot load and regeneration history are manufacturer values",
                "text": "Possible interrupted regeneration: "
                + "; ".join(support)
                + ". Heuristic interpretation only.",
            }
        )
    return out


# -- link & sampling -----------------------------------------------------------------------------------


def link_events(sd: SessionData, clk: _Clock) -> list[dict[str, Any]]:
    types = (
        "adapter_connection_lost",
        "adapter_connection_restored",
        "ecu_communication_lost",
        "ecu_communication_restored",
    )
    return [
        {
            "type": a.event_type,
            "t_rel_s": clk.rel(a.t_wall),
            "gap_s": a.evidence.get("gap_s"),
            "note": a.note,
        }
        for a in sd.annotations
        if a.event_type in types
    ]


def sampling_quality(sd: SessionData) -> dict[str, Any]:
    meta = sd.session["meta"]
    mon = meta.get("monitoring") or {}
    tier_of = {(it["pid"] if it["pid"] is not None else -1): it for it in mon.get("items", [])}
    rows = []
    for m in meta.get("metrics") or []:
        key = -1 if m.get("mode", 1) == ADAPTER_MODE else m["pid"]
        it = tier_of.get(key)
        rows.append(
            {
                **m,
                "tier": it["tier"] if it else None,
                "target_hz_base": round(1 / it["base_interval_s"], 3) if it else None,
                "target_hz_final": round(1 / it["final_interval_s"], 3)
                if it and it["final_interval_s"]
                else None,
            }
        )
    return {
        "base_intervals": mon.get("base_intervals") or (meta.get("sampling_plan") or {}).get("tiers"),
        "final_effective_intervals": mon.get("final_effective_intervals"),
        "adjustments": mon.get("adjustments", []),
        "suspended_at_end": mon.get("suspended_at_end", {}),
        "time_in_state_s": mon.get("time_in_state_s"),
        "signals": rows,
        "note": "Signals are polled one after another; values of different PIDs are never simultaneous. "
        "A held value is never shown as current: every sample has its own timestamp.",
    }


def signal_availability(sd: SessionData) -> list[dict[str, Any]]:
    meta = sd.session["meta"]
    sel = meta.get("selected") or {}
    counts: dict[tuple[int, int], dict[str, int]] = {}
    for s in sd.samples:
        c = counts.setdefault((s["mode"], s["pid"]), {"ok": 0, "raw": 0, "failed": 0})
        if s["quality"] == "ok":
            c["ok"] += 1
        elif s["quality"] == "raw_only":
            c["raw"] += 1
        else:
            c["failed"] += 1
    out = []
    for tier in ("fast", "medium", "slow"):
        for pid in sel.get(tier, []):
            c = counts.get((1, pid), {"ok": 0, "raw": 0, "failed": 0})
            status = "measured" if c["ok"] else "raw_only" if c["raw"] else "no_valid_answer"
            out.append({"pid": pid, "hex": f"{pid:02X}", "name": pid_name(pid), "tier": tier, "status": status,
                        "provenance": "ECU", **c})  # fmt: skip
    for pid in sel.get("unsupported", []):
        out.append({"pid": pid, "hex": f"{pid:02X}", "name": pid_name(pid), "tier": None, "status": "not_available",
                    "provenance": "ECU", "ok": 0, "raw": 0, "failed": 0,
                    "reason": "not advertised by this ECU via generic OBD; never polled"})  # fmt: skip
    plan = meta.get("sampling_plan") or {}
    c = counts.get((ADAPTER_MODE, ADAPTER_VOLTAGE_PID))
    out.append(
        {
            "pid": None,
            "hex": "ATRV",
            "name": "Adapter supply voltage",
            "tier": "slow",
            "provenance": "adapter",
            "status": ("measured" if c and c["ok"] else "no_valid_answer")
            if plan.get("adapter_voltage") == "polled"
            else "not_available",
            "reason": None if plan.get("adapter_voltage") == "polled" else plan.get("adapter_voltage"),
            **(c or {"ok": 0, "raw": 0, "failed": 0}),
        }
    )
    return out


# -- entry point ----------------------------------------------------------------------------------------


class MixedReferencePoolError(ValueError):
    """References were requested from sessions of different cohorts (e.g. real + synthetic)."""


def references(history: list[SessionData]) -> dict[str, Any]:
    """Vehicle-level references (usual warm coolant, warm-idle baseline) from the given sessions.

    All sessions must belong to ONE cohort (see :func:`baseline_cohort`): real and synthetic data,
    or two different confirmed vehicles, are never pooled. Use :func:`reference_pool` /
    :func:`references_by_cohort` to select them.
    """
    cohorts = {baseline_cohort(h.session) for h in history}
    if len(cohorts) > 1:
        raise MixedReferencePoolError(
            "refusing to pool references across cohorts: "
            + ", ".join(sorted(f"{p}/{o}/{'confirmed' if i else 'unconfirmed'}" for p, o, i in cohorts))
        )
    from app.diagnostics.air_path import air_path_row

    cohort = next(iter(cohorts)) if cohorts else None
    plateau = warm_plateau(history)
    return {
        "plateau": plateau,
        "baseline": regen_baseline(history, plateau.get("value")),
        "cohort": cohort_label(cohort) if cohort else None,
        "_cohort": cohort,
        # compact air-path statistics of every session of this cohort (for cross-session comparison)
        "air_path_rows": [air_path_row(h) for h in sorted(history, key=lambda h: h.session["started_at"])],
    }


def references_by_cohort(sessions: list[SessionData]) -> dict[tuple[str, str, str | None], dict[str, Any]]:
    """References per cohort for a list of (monitoring) sessions."""
    groups: dict[tuple[str, str, str | None], list[SessionData]] = {}
    for sd in sessions:
        groups.setdefault(baseline_cohort(sd.session), []).append(sd)
    return {k: references(v) for k, v in groups.items()}


def _public_refs(refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "usual_warm_coolant": refs["plateau"],
        "warm_idle_baseline": refs["baseline"],
        "cohort": refs.get("cohort"),
    }


def monitoring_summary(
    sd: SessionData,
    history: list[SessionData] | None = None,
    refs: dict[str, Any] | None = None,
    owner_reported: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the monitoring dashboard / report / export needs, recomputable offline.

    ``history``: the vehicle's sessions; only monitoring sessions (plus this one) feed the
    references. ``refs`` (from :func:`references`) skips recomputing them. ``owner_reported``:
    the vehicle record's owner-reported codes (default: the profile's).
    """
    from app.diagnostics.air_path import air_path_summary, compare_to_cohort

    if refs is None:
        refs = references(reference_pool(sd, history))
    elif refs.get("_cohort") is not None and refs["_cohort"] != baseline_cohort(sd.session):
        raise MixedReferencePoolError(
            f"references of {refs.get('cohort')} cannot judge session {sd.id} ({cohort_label(baseline_cohort(sd.session))})"
        )
    clk = _Clock(sd)
    runs = engine_runs(sd)
    stops = shutdowns(sd, runs)
    fans = fan_runs(sd, stops)
    plateau = refs["plateau"]
    base = refs["baseline"]
    regens, regen_limits = possible_regenerations(sd, base, plateau.get("value"), clk)
    drive = driving_conditions(sd, runs)
    interrupted = interrupted_regenerations(sd, regens, stops, fans, drive, clk)
    cool = coolant(sd, runs, stops, clk, plateau)
    volts = voltage(sd, runs, stops, clk)
    events = [
        {
            "t_rel_s": clk.rel(a.t_wall),
            "t_wall": a.t_wall,
            "label": a.label,
            "event_type": a.event_type,
            "origin": a.origin,
            "confidence": a.confidence,
            "note": a.note,
            "evidence": a.evidence,
        }
        for a in sd.annotations
    ]
    for r in runs:
        r["t_start_rel_s"], r["t_end_rel_s"] = clk.rel(r["t_start"]), clk.rel(r["t_end"])
    for st in stops:
        st["t_rel_s"] = clk.rel(st["t_wall"])
    for f in fans["runs"]:
        f["t_start_rel_s"], f["t_end_rel_s"] = clk.rel(f["t_start_wall"]), clk.rel(f["t_end_wall"])
    air = air_path_summary(sd, owner_reported)
    air["history"] = compare_to_cohort(sd.id, refs.get("air_path_rows") or [])
    from app.diagnostics.dde import dde_summary

    dde = dde_summary(sd.session, sd.samples, clk.t0, sd.annotations)
    confirmed = (dde.get("regeneration") or {}).get("confirmed") or []
    detected = [*confirmed, *regens, *interrupted, *cool["detected"], *air["detected_events"]]
    return {
        "session_id": sd.id,
        "is_monitoring_session": is_monitoring_session(sd.session),
        "synthetic": sd.session["synthetic"],
        "t0_wall": clk.t0,
        "drive": drive,
        "engine_runs": runs,
        "shutdowns": stops,
        "fan": fans,
        "voltage": volts,
        "coolant": cool,
        "dpf": {
            "availability": dpf_availability(sd),
            "possible_regenerations": regens,
            "possible_interrupted_regenerations": interrupted,
            "user_suspected": [
                {"t_rel_s": clk.rel(a.t_wall), "note": a.note, "origin": a.origin}
                for a in _events(sd, "suspected_dpf_regeneration")
            ],
            "baseline": base,
            "limitations": regen_limits,
            # only from a fresh, decoded DDE regeneration flag (DDE-06); the inferred list above stays separate
            "confirmed_regenerations": confirmed,
        },
        "detected_events": detected,
        "events": events,
        "link": link_events(sd, clk),
        "signals": signal_availability(sd),
        "sampling": sampling_quality(sd),
        "observed_capabilities": (sd.session["meta"].get("monitoring") or {}).get("observed_capabilities"),
        "air_path": air,
        "dde": dde,
        "references": _public_refs(refs),
        "baseline_origin": baseline_origin(sd.session),
        "heuristics": HEURISTICS,
    }


def history_row(
    sd: SessionData, refs: dict[str, Any], owner_reported: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One line of the historical comparison (several drives side by side)."""
    from app.diagnostics.air_path import air_path_row

    m = monitoring_summary(sd, refs=refs, owner_reported=owner_reported)
    s = sd.session
    after = [f for f in m["fan"]["runs"] if f["after_shutdown"]]
    ecu_run = (
        (m["voltage"]["ecu"].get("engine_running") or {})
        if m["voltage"]["ecu"]["status"] == "measured"
        else {}
    )
    ad_run = (
        (m["voltage"]["adapter"].get("engine_running") or {})
        if m["voltage"]["adapter"]["status"] == "measured"
        else {}
    )
    return {
        "id": s["id"],
        "started_at": s["started_at"],
        "ended_at": s["ended_at"],
        "label": s["label"],
        "synthetic": s["synthetic"],
        "baseline_origin": baseline_origin(s),
        "aborted": s["meta"].get("aborted"),
        "interrupted": bool(s["meta"].get("interrupted")),
        "drive_type": m["drive"]["drive_type_user_label"],
        "running_min": round(m["drive"]["running_s"] / 60, 1),
        "max_speed_kmh": m["drive"]["max_speed_kmh"],
        "moving_share": m["drive"]["moving_share"],
        "engine_stops": len(m["shutdowns"]),
        "fan_runs_after_shutdown": len(after),
        "fan_durations_s": [f["duration_s"] for f in after],
        "battery_warnings": sum(
            1 for w in m["voltage"]["battery_warnings"] if w["event"] == "battery_warning"
        ),
        "possible_regenerations": len(m["dpf"]["possible_regenerations"]),
        # measured by the DDE (never pooled with the inferred count above); None: no DDE data
        "dde_confirmed_regenerations": len(m["dpf"]["confirmed_regenerations"])
        if m["dde"].get("regeneration", {}).get("status") == "measured"
        else None,
        "dde_soot_last": next(
            (
                (x["stats"] or {}).get("last")
                for x in m["dde"].get("signals", [])
                if x["signal_key"] == "dpf_soot_mass"
            ),
            None,
        ),
        "possible_interrupted_regenerations": len(m["dpf"]["possible_interrupted_regenerations"]),
        "user_suspected_regenerations": len(m["dpf"]["user_suspected"]),
        "coolant_max_c": m["coolant"].get("max_running"),
        "ecu_voltage_running_median": ecu_run.get("median"),
        "adapter_voltage_running_median": ad_run.get("median"),
        "link_events": len(m["link"]),
        "air_path": {**air_path_row(sd, m["air_path"]), "vs_cohort": m["air_path"]["history"]},
    }


def summary_for(store: Any, sid: str) -> dict[str, Any]:
    """Monitoring summary of a stored session. Its references come from the vehicle's other
    monitoring sessions of the SAME cohort only (real with real, synthetic with synthetic)."""
    from app.diagnostics.engine import load_session

    current = load_session(store, sid)
    key = baseline_cohort(current.session)
    history = [
        current if s["id"] == sid else load_session(store, s["id"])
        for s in store.list_sessions(current.session["vehicle_profile"])
        if s["id"] == sid or (is_monitoring_session(s) and baseline_cohort(s) == key)
    ]
    return monitoring_summary(current, history, owner_reported=_owner_reported(store, key[0]))


def _owner_reported(store: Any, vehicle_profile: str) -> dict[str, Any] | None:
    """The stored vehicle record's owner-reported data, or None (then the profile's default is used)."""
    record = store.get_setting(f"vehicle:{vehicle_profile}", None)
    return (record or {}).get("owner_reported")


def history_for(store: Any, vehicle_profile: str) -> dict[str, Any]:
    """Historical comparison of a vehicle profile's monitoring sessions. Each row is judged with
    the references of its own cohort; real and synthetic rows never share a baseline."""
    from app.diagnostics.air_path import trends
    from app.diagnostics.engine import load_session

    sessions = [
        load_session(store, s["id"]) for s in store.list_sessions(vehicle_profile) if is_monitoring_session(s)
    ]
    owner = _owner_reported(store, vehicle_profile)
    by_cohort = references_by_cohort(sessions)
    cohorts = [
        {**_public_refs(r), "sessions": sum(1 for sd in sessions if baseline_cohort(sd.session) == k)}
        for k, r in sorted(by_cohort.items(), key=lambda kv: (kv[0][1] != "real", kv[0][2] or ""))
    ]
    # the headline references: the real vehicle's (unconfirmed identity first), else synthetic
    head = cohorts[0] if cohorts else {"usual_warm_coolant": {"value": None, "sessions": 0},
                                       "warm_idle_baseline": None, "cohort": None}  # fmt: skip
    return {
        "vehicle_profile": vehicle_profile,
        "references": head,
        "references_by_cohort": cohorts,
        "sessions": [
            history_row(sd, by_cohort[baseline_cohort(sd.session)], owner) for sd in reversed(sessions)
        ],
        # multi-session air-path statistics, one block per cohort (real first; never pooled across cohorts)
        "air_path_trends": [
            trends(r["air_path_rows"], r["cohort"])
            for _, r in sorted(by_cohort.items(), key=lambda kv: (kv[0][1] != "real", kv[0][2] or ""))
        ],
    }
