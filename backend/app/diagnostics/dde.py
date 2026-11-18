"""DDE-06: analysis of manufacturer (BMW DDE) readings inside a monitoring session.

Kept apart from the generic analysis on purpose:

* every number here comes from ``source == "bmw_dde"`` samples (provenance kept per signal),
  never mixed with generic PIDs as if they were the same measurement;
* a regeneration is ``confirmed`` only from a fresh, decoded regeneration flag of an entry
  whose catalog semantics say it IS the ECU's regeneration flag; stale or missing flag data
  leaves the state ``unknown`` (never ``false``). The generic "possible regeneration"
  heuristic stays a separate, inferred signal;
* commanded-vs-actual deltas pair two sequentially polled signals only when they were read
  close together, and say so: polling order says nothing about cause.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

GOOD = ("ok",)
PAIR_MAX_DT_S = 2.5  # commanded / actual samples further apart than this are not paired
REGEN_STALE_FACTOR = 3.0  # flag older than this x its planned interval (min 10 s) -> unknown
REGEN_STALE_MIN_S = 10.0
PAIRS = (
    ("egr", "egr_commanded", "egr_actual"),
    ("air_mass", "air_mass_target", "air_mass_actual"),
    ("boost", "boost_commanded", "boost_actual"),
    ("rail", "rail_pressure_commanded", "rail_pressure_actual_mfr"),
)

HEURISTICS = {
    "dde_pair_max_dt_s": PAIR_MAX_DT_S,
    "dde_regen_stale_factor": REGEN_STALE_FACTOR,
    "dde_regen_stale_min_s": REGEN_STALE_MIN_S,
}


def dde_samples(samples: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        if s.get("source") == "bmw_dde":
            out.setdefault(s["signal_key"], []).append(s)
    return out


def _stats(xs: list[float]) -> dict[str, Any] | None:
    if not xs:
        return None
    return {
        "n": len(xs),
        "min": round(min(xs), 3),
        "max": round(max(xs), 3),
        "median": round(statistics.median(xs), 3),
        "first": round(xs[0], 3),
        "last": round(xs[-1], 3),
    }


LINK_GAP_EVENTS = {
    "ecu_communication_lost": "ecu_communication_restored",
    "adapter_connection_lost": "adapter_connection_restored",
}


def link_gaps(annotations: list[Any] | None) -> list[tuple[float, float | None, str]]:
    """(lost_wall, restored_wall or None, event type) for ECU-silence / adapter-loss periods."""
    out: list[tuple[float, float | None, str]] = []
    open_: dict[str, float] = {}
    for a in sorted(annotations or [], key=lambda a: a.t_wall):
        et = a.event_type
        if et in LINK_GAP_EVENTS:
            open_.setdefault(et, a.t_wall)
        for lost, restored in LINK_GAP_EVENTS.items():
            if et == restored and lost in open_:
                out.append((open_.pop(lost), a.t_wall, lost))
    out += [(t, None, et) for et, t in open_.items()]
    return out


def freshness_window(attempt_times: list[float], planned_interval_s: float) -> tuple[float, float]:
    """-> (cadence_s, window_s). The cadence is the median spacing of ATTEMPTED reads (every quality:
    the scheduler's effective interval), never of successful reads only - so failed reads cannot make
    the cadence look slower and stretch what counts as fresh."""
    times = sorted(attempt_times)
    cadence = (
        statistics.median([b - a for a, b in zip(times, times[1:], strict=False)])
        if len(times) > 2
        else planned_interval_s
    )
    return cadence, max(REGEN_STALE_FACTOR * cadence, REGEN_STALE_MIN_S)


def regeneration_episodes(
    rows: list[dict[str, Any]],
    interval_s: float,
    t0_wall: float | None,
    annotations: list[Any] | None = None,
    session_end_wall: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """-> (confirmed episodes, unknown gaps).

    Each decoded ('ok') flag reading confirms the state at its own instant. The state between two
    consecutive ok readings is known only when they are at most one freshness window apart (window
    from the ATTEMPTED-read cadence) AND no ECU-silence / adapter-loss period lies between them.
    Otherwise that span is an unknown gap: never 'inactive', never part of a confirmed duration. A
    confirmed episode is the span from its first to its last active reading inside one known run;
    starts / ends between readings are reported as bounds, not guessed."""
    attempts = sorted(rows, key=lambda r: (r["t_mono"], r["t_wall"]))
    cadence, window = freshness_window([r["t_mono"] for r in attempts], interval_s)
    link = link_gaps(annotations)
    episodes: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    prev: dict[str, Any] | None = None  # previous ok reading: {"active", "t_wall", "t_mono"}

    def rel(t: float | None) -> float | None:
        return round(t - t0_wall, 2) if t is not None and t0_wall is not None else None

    def between(a_mono: float, b_mono: float | None) -> list[dict[str, Any]]:
        hi = b_mono if b_mono is not None else float("inf")
        return [r for r in attempts if a_mono < r["t_mono"] < hi]

    def why_unknown(a: dict[str, Any], b_wall: float | None, b_mono: float | None) -> str | None:
        """Why the span after ok reading ``a`` up to b is NOT known (None: it is known)."""
        hi = b_wall if b_wall is not None else float("inf")
        hits = [et for lost, back, et in link if lost <= hi and (back is None or back >= a["t_wall"])]
        if "ecu_communication_lost" in hits:
            return "ECU communication lost"
        if hits:
            return "adapter connection lost"
        if b_mono is None or b_mono - a["t_mono"] > window:
            failed = sum(1 for r in between(a["t_mono"], b_mono) if r["quality"] not in GOOD)
            if failed:
                return f"{failed} failed / unusable read(s), no fresh reading"
            return "no read attempt (polling suspended, paused or stopped)"
        return None

    def gap(a: dict[str, Any], b_wall: float | None, b_mono: float | None, reason: str) -> None:
        gaps.append(
            {
                "from_rel_s": rel(a["t_wall"]),
                "to_rel_s": rel(b_wall),
                "gap_s": round(b_wall - a["t_wall"], 2) if b_wall is not None else None,
                "failed_reads": sum(1 for r in between(a["t_mono"], b_mono) if r["quality"] not in GOOD),
                "reason": reason,
                "state_before": "active" if a["active"] else "inactive",
            }
        )

    def close(ended: str, end_bounds: list[float | None]) -> None:
        nonlocal cur
        assert cur is not None
        cur["t_end_wall"] = cur["last_confirmed_wall"]
        cur["t_end_rel_s"] = rel(cur["last_confirmed_wall"])
        cur["duration_s"] = round(cur["last_confirmed_wall"] - cur["t_start_wall"], 2)  # measured span only
        cur["ended"] = ended
        cur["ended_between_rel_s"] = [rel(x) for x in end_bounds]
        episodes.append(cur)
        cur = None

    for r in attempts:
        if r["quality"] not in GOOD or not r.get("status_json"):
            continue
        active = bool(json.loads(r["status_json"]).get("active"))
        reason = why_unknown(prev, r["t_wall"], r["t_mono"]) if prev is not None else None
        if prev is not None and reason:
            gap(prev, r["t_wall"], r["t_mono"], reason)
            if cur is not None:
                close(f"unknown ({reason})", [cur["last_confirmed_wall"], None])
        if active and cur is None:
            if prev is None:
                started, bounds = (
                    "active at the first decoded reading (start not observed)",
                    [None, r["t_wall"]],
                )
            elif reason:
                started = "active at the first fresh reading after an unknown gap (start not observed)"
                bounds = [prev["t_wall"], r["t_wall"]]
            else:
                started, bounds = (
                    "flag became active between two fresh readings",
                    [prev["t_wall"], r["t_wall"]],
                )
            cur = {
                "type": "regeneration",
                "origin": "measured",
                "status": "confirmed",
                "confidence": "measured",
                "source": "bmw_dde",
                "t_wall": r["t_wall"],
                "t_rel_s": rel(r["t_wall"]),
                "t_start_wall": r["t_wall"],
                "t_start_rel_s": rel(r["t_wall"]),
                "started": started,
                "started_between_rel_s": [rel(x) for x in bounds],
                "samples": 0,
                "decoder_version": r.get("decoder_version"),
                "catalog_version": r.get("catalog_version"),
                "request_id": r.get("request_id"),
            }
        if active and cur is not None:
            cur["samples"] += 1
            cur["last_confirmed_wall"] = r["t_wall"]
        if not active and cur is not None:
            close("flag inactive between two fresh readings", [cur["last_confirmed_wall"], r["t_wall"]])
        prev = {"active": active, "t_wall": r["t_wall"], "t_mono": r["t_mono"]}
    if prev is not None:
        last_attempt = attempts[-1]
        end_wall = max(session_end_wall or last_attempt["t_wall"], last_attempt["t_wall"])
        end_mono = prev["t_mono"] + (end_wall - prev["t_wall"])
        reason = why_unknown(prev, end_wall, end_mono)
        if reason and end_wall > prev["t_wall"]:
            gap(prev, end_wall, end_mono, reason)
        if cur is not None:
            close(
                f"unknown after the last fresh reading ({reason})" if reason else "still active at the end",
                [cur["last_confirmed_wall"], None],
            )
    for e in episodes:
        e["freshness_window_s"] = round(window, 2)
        e["read_cadence_s"] = round(cadence, 2)
    return episodes, gaps


def _pair(cmd: list[dict[str, Any]], act: list[dict[str, Any]]) -> dict[str, Any] | None:
    a_ok = [r for r in act if r["quality"] in GOOD and r["value"] is not None]
    deltas = []
    for c in cmd:
        if c["quality"] not in GOOD or c["value"] is None or not a_ok:
            continue
        near = min(a_ok, key=lambda r: abs(r["t_mono"] - c["t_mono"]))
        if abs(near["t_mono"] - c["t_mono"]) <= PAIR_MAX_DT_S:
            deltas.append(c["value"] - near["value"])
    if not deltas:
        return None
    absd = [abs(d) for d in deltas]
    return {
        "pairs": len(deltas),
        "mean_delta": round(statistics.fmean(deltas), 3),
        "mean_abs_delta": round(statistics.fmean(absd), 3),
        "max_abs_delta": round(max(absd), 3),
        "note": f"commanded minus actual, paired within {PAIR_MAX_DT_S} s; sequential polling, no causal order",
    }


def dde_summary(
    session: dict[str, Any],
    samples: list[dict[str, Any]],
    t0_wall: float | None,
    annotations: list[Any] | None = None,
) -> dict[str, Any]:
    meta = session.get("meta") or {}
    plan_meta = meta.get("dde")
    by = dde_samples(samples)
    if plan_meta is None and not by:
        return {
            "status": "not_recorded",
            "reason": "no DDE plan in this session (older session, other vehicle, or not a DDE-capable recording)",
        }
    plan = {p["metric"]: p for p in (plan_meta or {}).get("plan") or []}
    synthetic = bool((plan_meta or {}).get("synthetic") or session.get("synthetic"))
    signals = []
    for metric in list(dict.fromkeys([*plan, *by])):
        rows = by.get(metric, [])
        ok = [r for r in rows if r["quality"] in GOOD]
        vals = [r["value"] for r in ok if r["value"] is not None]
        fails: dict[str, int] = {}
        for r in rows:
            if r["quality"] not in GOOD:
                fails[r["quality"]] = fails.get(r["quality"], 0) + 1
        p = plan.get(metric, {})
        signals.append(
            {
                "signal_key": metric,
                "title": p.get("title") or (rows[0]["name"] if rows else metric),
                "provenance": "bmw_dde",
                "unit": p.get("unit") or (ok[0]["unit"] if ok else None),
                "tier": p.get("tier"),
                "base_interval_s": p.get("base_interval_s"),
                "samples_ok": len(ok),
                "failed": fails,
                "status": "measured" if ok else ("failed" if rows else "not_read"),
                "stats": _stats(vals),
                "decoder_versions": sorted(
                    {r.get("decoder_version") for r in ok if r.get("decoder_version")}
                ),
            }
        )
    regen_key = next((m for m, p in plan.items() if p.get("semantics") == "regeneration_active_flag"), None)
    episodes, gaps = ([], [])
    if regen_key:
        episodes, gaps = regeneration_episodes(
            by.get(regen_key, []),
            float(plan[regen_key].get("base_interval_s") or 5.0),
            t0_wall,
            annotations,
            session.get("ended_at"),
        )
    for e in episodes:
        e["synthetic"] = synthetic
        e["text"] = (
            ("SYNTHETIC: " if synthetic else "")
            + f"DPF regeneration confirmed by the DDE regeneration flag over a measured span of {e['duration_s']:.0f} s "
            f"({e['samples']} active reading(s), first to last; unknown gaps are never included). "
            f"Start: {e['started']}; end: {e['ended']}."
        )
    pairs = {name: _pair(by.get(c, []), by.get(a, [])) for name, c, a in PAIRS if c in by or a in by}
    limitations = [
        "Manufacturer values are kept as their own signals; they are never merged with generic PIDs (e.g. "
        "DDE air mass is not compared as if it were PID 10).",
        "Signals are polled one after another; deltas pair samples taken close together and show "
        "association in time, not cause.",
        "A regeneration counts as confirmed only from a fresh, decoded DDE regeneration flag; missing or stale "
        "flag data is 'unknown', never 'inactive'.",
    ]
    if synthetic:
        limitations.insert(0, "SYNTHETIC: simulator data with invented layouts; says nothing about the car.")
    return {
        "status": (plan_meta or {}).get("status") or ("polling" if by else "not_polled"),
        "reason": (plan_meta or {}).get("reason"),
        "synthetic": synthetic,
        "catalog_version": (plan_meta or {}).get("catalog_version"),
        "opted_in": (plan_meta or {}).get("opted_in"),
        "confirmed_variant": (plan_meta or {}).get("confirmed_variant"),
        "adapter_verdict": (plan_meta or {}).get("adapter_verdict"),
        "availability": (plan_meta or {}).get("availability") or [],
        "signals": signals,
        "regeneration": {
            "signal_key": regen_key,
            "confirmed": episodes,
            "unknown_gaps": gaps,
            "status": "measured" if regen_key and by.get(regen_key) else "not_available",
        },
        "commanded_vs_actual": {k: v for k, v in pairs.items() if v},
        "heuristics": HEURISTICS,
        "limitations": limitations,
    }
