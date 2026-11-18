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


def regeneration_episodes(
    rows: list[dict[str, Any]], interval_s: float, t0_wall: float | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """-> (confirmed episodes, unknown gaps). Only decoded 'ok' flag samples count; a failed or
    stale read ends knowledge (unknown), it never says 'inactive'."""
    # judge freshness against how often the flag was ACTUALLY read (the scheduler stretches DDE
    # intervals on a slow link), never against an optimistic planned interval
    ok_t = [r["t_wall"] for r in rows if r["quality"] in GOOD and r.get("status_json")]
    spacing = (
        statistics.median([b - a for a, b in zip(ok_t, ok_t[1:], strict=False)])
        if len(ok_t) > 2
        else interval_s
    )
    stale = max(REGEN_STALE_FACTOR * max(interval_s, spacing), REGEN_STALE_MIN_S)
    episodes: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    last_ok: dict[str, Any] | None = None

    def rel(t: float) -> float | None:
        return round(t - t0_wall, 2) if t0_wall is not None else None

    def close(end_t: float, how: str) -> None:
        nonlocal cur
        if cur is not None:
            cur["t_end_wall"], cur["t_end_rel_s"], cur["ended"] = end_t, rel(end_t), how
            cur["duration_s"] = round(end_t - cur["t_start_wall"], 2)
            episodes.append(cur)
            cur = None

    for r in rows:
        if r["quality"] not in GOOD or not r.get("status_json"):
            continue
        active = bool(json.loads(r["status_json"]).get("active"))
        t = r["t_wall"]
        if last_ok is not None and t - last_ok["t_wall"] > stale:
            gaps.append({"from_rel_s": rel(last_ok["t_wall"]), "to_rel_s": rel(t),
                         "gap_s": round(t - last_ok["t_wall"], 2)})  # fmt: skip
            # the state during the gap is unknown: an open episode ends at its last confirmation
            close(last_ok["t_wall"], "unknown (data gap)")
        if active and cur is None:
            cur = {
                "type": "regeneration",
                "origin": "measured",
                "status": "confirmed",
                "confidence": "measured",
                "source": "bmw_dde",
                "t_wall": t,
                "t_rel_s": rel(t),
                "t_start_wall": t,
                "t_start_rel_s": rel(t),
                "samples": 0,
                "decoder_version": r.get("decoder_version"),
                "catalog_version": r.get("catalog_version"),
                "request_id": r.get("request_id"),
            }
        if active and cur is not None:
            cur["samples"] += 1
            cur["last_confirmed_wall"] = t
        if not active and cur is not None:
            # the flag changed somewhere between the last active and this inactive reading: the
            # confirmed span ends at the last active reading; the bound is kept, not guessed
            cur["ended_between_rel_s"] = [rel(cur["last_confirmed_wall"]), rel(t)]
            close(cur["last_confirmed_wall"], "flag inactive")
        last_ok = r
    if cur is not None:
        close(cur["last_confirmed_wall"], "still active at the last fresh reading")
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
    session: dict[str, Any], samples: list[dict[str, Any]], t0_wall: float | None
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
            by.get(regen_key, []), float(plan[regen_key].get("base_interval_s") or 5.0), t0_wall
        )
    for e in episodes:
        e["synthetic"] = synthetic
        e["text"] = (
            ("SYNTHETIC: " if synthetic else "")
            + f"DPF regeneration confirmed by the DDE regeneration flag for {e['duration_s']:.0f} s "
            f"({e['samples']} reading(s), span between the first and last active reading); ended: {e['ended']}"
            + (
                f" between {e['ended_between_rel_s'][0]} s and {e['ended_between_rel_s'][1]} s"
                if e.get("ended_between_rel_s")
                else ""
            )
            + "."
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
