"""Air-path analysis for stored sessions: MAF anomalies, EGR-error excursions, MAP / RPM context,
generic DTCs, and per-session statistics that can be compared across weeks of drives.

Derived only from what a session already stored (samples with their own timestamps, quality and
raw bytes; fault scans; timeline events). It sends nothing to the vehicle and adds no polling: the
recording profile and its sampling tiers are unchanged, and any old session can be re-analysed.

Rules this module keeps:

* A MAF sample counts as "while running" only if measured RPM on BOTH sides of it (each at most
  ``AIRPATH_RPM_MAX_AGE_S`` away) is at or above ``AIRPATH_RUNNING_RPM``. A zero after the engine
  stopped, or next to a stale / missing RPM, is never a running anomaly.
* A failed read (NO DATA, timeout, disconnect, negative answer) has no value and is never a zero.
  Unsupported or unrecorded signals are ``not_available``; their statistics are ``None``, never 0.
* PIDs are polled one after another. Every associated value carries its own age; values older than
  the stated limit are marked stale and are not used for an association.
* Thresholds are screening heuristics (``monitoring.HEURISTICS``), not BMW / MINI specifications.
  A temporal association is reported as such; nothing here names a faulty component.
"""

from __future__ import annotations

import bisect
import statistics
from typing import Any

from app.diagnostics import monitoring as M
from app.diagnostics.dtc_info import classify, confirmed_ecu_variant
from app.diagnostics.engine import SessionData
from app.vehicles.profiles import get_profile

RPM, SPEED, LOAD, COOLANT, MAP, MAF, EGR_CMD, EGR_ERR = 0x0C, 0x0D, 0x04, 0x05, 0x0B, 0x10, 0x2C, 0x2D

#: manual events and free-text labels treated as drivability symptoms worth relating to the air path
SYMPTOM_EVENTS = frozenset({"rough_idle", "power_loss", "unusual_engine_noise"})
SYMPTOM_WORDS = ("rough idle", "vibration", "jerk", "noise", "rattl", "power loss", "hesitat", "stall")

AIRPATH_KEYS = tuple(k for k in M.HEURISTICS if k.startswith(("airpath_", "maf_", "egr_error")))

SIGNALS = {
    "rpm": (RPM, "Engine speed (0C)"),
    "speed": (SPEED, "Vehicle speed (0D)"),
    "load": (LOAD, "Calculated load (04)"),
    "coolant": (COOLANT, "Coolant temperature (05)"),
    "map": (MAP, "Intake manifold absolute pressure (0B)"),
    "maf": (MAF, "MAF air flow rate (10)"),
    "egr_cmd": (EGR_CMD, "Commanded EGR (2C)"),
    "egr_error": (EGR_ERR, "EGR error (2D)"),
}


# -- helpers ---------------------------------------------------------------------------------------


class _Lookup:
    """Time-ordered measured values of one signal (held values only, never interpolated)."""

    def __init__(self, series: list[Any]) -> None:
        self.s = series
        self.t = [p.t for p in series]

    def __bool__(self) -> bool:
        return bool(self.s)

    def before(self, t: float):
        i = bisect.bisect_right(self.t, t) - 1
        return self.s[i] if i >= 0 else None

    def after(self, t: float):
        i = bisect.bisect_left(self.t, t)
        return self.s[i] if i < len(self.s) else None

    def held(self, t: float, max_age: float) -> dict[str, Any] | None:
        p = self.before(t)
        if p is None:
            return None
        age = t - p.t
        return {"value": round(p.v, 2), "age_s": round(age, 2), "fresh": age <= max_age}

    def between(self, a: float, b: float) -> list[Any]:
        return self.s[bisect.bisect_left(self.t, a) : bisect.bisect_right(self.t, b)]


def _fresh(h: dict[str, Any] | None) -> float | None:
    return h["value"] if h and h["fresh"] else None


def _per_1k(n: int | None, d: int | None) -> float | None:
    return None if n is None or not d else round(1000.0 * n / d, 2)


def _per_h(n: int | None, hours: float | None) -> float | None:
    return None if n is None or not hours else round(n / hours, 2)


def _response(pid: int, raw_hex: str | None) -> str | None:
    """The service-01 answer as the ECU sent it (data bytes are stored as raw_hex)."""
    if not raw_hex:
        return None
    data = " ".join(raw_hex[i : i + 2] for i in range(0, len(raw_hex), 2))
    return f"41 {pid:02X} {data}".strip()


def _signal_status(sd: SessionData, key: str) -> dict[str, Any]:
    pid, name = SIGNALS[key]
    n = len(M._series(sd, pid))
    failed = sum(
        1
        for s in sd.samples
        if s["mode"] == 1 and s["pid"] == pid and (s["quality"] != "ok" or s["value"] is None)
    )
    sup = sd.supported(pid)
    if n:
        status, reason = "measured", None
    elif sup is False:
        status, reason = "not_available", "not advertised by this ECU via generic OBD; never polled"
    elif failed:
        status, reason = "no_valid_answer", f"{failed} read(s), none with a value"
    elif sup is None:
        status, reason = "not_available", "no capability data and no samples in this session"
    else:
        status, reason = "not_available", "advertised but not recorded in this session"
    return {
        "name": name,
        "hex": f"{pid:02X}",
        "status": status,
        "samples": n,
        "failed_reads": failed,
        "reason": reason,
    }


def _running_state(rpm: _Lookup, t: float) -> tuple[str, dict[str, Any]]:
    """'running' / 'not_running' / 'unknown' from the measured RPM right before AND right after t."""
    b, a = rpm.before(t), rpm.after(t)
    ev = {
        "rpm_before": None if b is None else {"value": round(b.v, 1), "age_s": round(t - b.t, 2)},
        "rpm_after": None if a is None else {"value": round(a.v, 1), "age_s": round(a.t - t, 2)},
    }
    if b is None or a is None or t - b.t > M.AIRPATH_RPM_MAX_AGE_S or a.t - t > M.AIRPATH_RPM_MAX_AGE_S:
        return "unknown", ev
    if b.v >= M.AIRPATH_RUNNING_RPM and a.v >= M.AIRPATH_RUNNING_RPM:
        return "running", ev
    if b.v < M.RUNNING_RPM and a.v < M.RUNNING_RPM:
        return "not_running", ev
    return "unknown", ev  # cranking, stalling, or a transition between the two samples


def _temp_state(cool: _Lookup, t: float) -> str:
    v = _fresh(cool.held(t, M.AIRPATH_COOLANT_MAX_AGE_S))
    return "unknown" if v is None else ("warm" if v >= M.AIRPATH_WARM_COOLANT_C else "cold")


def _motion_state(speed: _Lookup, t: float) -> str:
    v = _fresh(speed.held(t, M.AIRPATH_CONTEXT_MAX_AGE_S))
    return "unknown" if v is None else ("idle" if v <= M.IDLE_MAX_SPEED else "moving")


def _load_band(load: _Lookup, t: float) -> str:
    v = _fresh(load.held(t, M.AIRPATH_CONTEXT_MAX_AGE_S))
    if v is None:
        return "unknown"
    return "low" if v < 30 else "medium" if v < 60 else "high"


def _evidence_row(s: dict[str, Any], clk: M._Clock) -> dict[str, Any]:
    return {
        "t_rel_s": clk.rel(s["t_wall"]),
        "value_gs": s["value"],
        "raw_hex": s["raw_hex"],
        "response": _response(MAF, s["raw_hex"]),
        "ecu": s["ecu"],
        "quality": s["quality"],
        "latency_ms": s["latency_ms"],
    }


def _change(series: _Lookup, t0: float, t1: float, after_s: float) -> dict[str, Any] | None:
    """Median during [t0 - 1 s, t1 + after_s] minus median in the baseline window before t0."""
    before = [p.v for p in series.between(t0 - M.AIRPATH_BASELINE_WINDOW_S, t0 - 1.0)]
    during = [p.v for p in series.between(t0 - 1.0, t1 + after_s)]
    if len(before) < 2 or not during:
        return None
    b, d = statistics.median(before), statistics.median(during)
    return {
        "before": round(b, 2),
        "during": round(d, 2),
        "change": round(d - b, 2),
        "n_before": len(before),
        "n_during": len(during),
    }


# -- MAF ---------------------------------------------------------------------------------------------


def _maf(
    sd: SessionData, clk: M._Clock, L: dict[str, _Lookup], idle_rpm_std_ref: float | None
) -> dict[str, Any]:
    prim = sd.primary_ecu
    rows = [
        s for s in sd.samples if s["mode"] == 1 and s["pid"] == MAF and s["ecu"] in (None, prim or s["ecu"])
    ]
    ok = sorted(
        (s for s in rows if s["quality"] == "ok" and s["value"] is not None), key=lambda s: s["t_wall"]
    )
    failed: dict[str, int] = {}
    for s in rows:
        if s["quality"] != "ok" or s["value"] is None:
            failed[s["quality"]] = failed.get(s["quality"], 0) + 1
    out: dict[str, Any] = {
        "samples_ok": len(ok),
        "failed_reads": {"n": sum(failed.values()), "by_quality": failed},
        "failed_reads_note": "Failed reads carry no value; they are never counted as zero MAF.",
    }
    if not ok:
        return out
    rpm = L["rpm"]
    states: list[tuple[dict[str, Any], str, dict[str, Any]]] = [
        (s, *_running_state(rpm, s["t_wall"])) for s in ok
    ]
    n_state = {"running": 0, "not_running": 0, "unknown": 0}
    zero_off = low_off = low_unknown = 0
    groups = {
        g: {"running_samples": 0, "zero": 0, "low": 0, "episodes": 0} for g in ("cold", "warm", "unknown")
    }
    motion = {
        g: {"running_samples": 0, "zero": 0, "low": 0, "episodes": 0} for g in ("idle", "moving", "unknown")
    }
    run_ok: list[tuple[dict[str, Any], dict[str, Any]]] = []
    zero_run = low_run = 0
    for s, st, ev in states:
        n_state[st] += 1
        low = s["value"] < M.MAF_LOW_GS
        zero = s["value"] <= M.MAF_ZERO_GS
        if st == "running":
            run_ok.append((s, ev))
            tg, mg = _temp_state(L["coolant"], s["t_wall"]), _motion_state(L["speed"], s["t_wall"])
            for grp in (groups[tg], motion[mg]):
                grp["running_samples"] += 1
                grp["zero"] += int(zero)
                grp["low"] += int(low)
            zero_run += int(zero)
            low_run += int(low)
        elif st == "not_running":
            zero_off += int(zero)
            low_off += int(low)
        elif low:
            low_unknown += 1

    # -- episodes: running low-MAF samples, closed by a normal MAF sample, a non-running / unknown one, or a gap
    episodes_raw: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
    cur: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for s, st, ev in states:
        cand = st == "running" and s["value"] < M.MAF_LOW_GS
        if cand and cur and s["t_wall"] - cur[-1][0]["t_wall"] <= M.AIRPATH_EPISODE_GAP_S:
            cur.append((s, ev))
            continue
        if cur:
            episodes_raw.append(cur)
            cur = []
        if cand:
            cur = [(s, ev)]
    if cur:
        episodes_raw.append(cur)

    maf_l = _Lookup(M._series(sd, MAF))
    episodes = []
    cluster = -1
    prev_end: float | None = None
    for ep in episodes_raw:
        t0, t1 = ep[0][0]["t_wall"], ep[-1][0]["t_wall"]
        if prev_end is None or t0 - prev_end > M.AIRPATH_REPEAT_WINDOW_S:
            cluster += 1
        prev_end = t1
        vals = [s["value"] for s, _ in ep]
        dur = round(t1 - t0, 2)
        persistent = dur >= M.AIRPATH_PERSISTENT_S or len(ep) >= 3
        tg, mg = _temp_state(L["coolant"], t0), _motion_state(L["speed"], t0)
        groups[tg]["episodes"] += 1
        motion[mg]["episodes"] += 1
        before = maf_l.before(t0 - 1e-6)
        after = maf_l.after(t1 + 1e-6)
        # RPM in the window around the episode
        rw = [p.v for p in rpm.between(t0 - M.AIRPATH_ASSOC_WINDOW_S, t1 + M.AIRPATH_ASSOC_WINDOW_S)]
        rstd = round(statistics.pstdev(rw), 1) if len(rw) >= 3 else None
        unstable: bool | None = None
        if rstd is not None and mg == "idle":
            limit = max(M.AIRPATH_RPM_UNSTABLE_STD, M.AIRPATH_RPM_UNSTABLE_FACTOR * (idle_rpm_std_ref or 0.0))
            unstable = rstd >= limit
        ctx = {
            k: L[k].held(t0, M.AIRPATH_COOLANT_MAX_AGE_S if k == "coolant" else M.AIRPATH_CONTEXT_MAX_AGE_S)
            for k in ("speed", "coolant", "map", "load", "egr_cmd", "egr_error")
        }
        limits = [
            f"{k}: stale ({v['age_s']} s old), not associated" for k, v in ctx.items() if v and not v["fresh"]
        ]
        limits += [f"{k}: not measured" for k, v in ctx.items() if v is None]
        kind = "maf_zero_while_running" if min(vals) <= M.MAF_ZERO_GS else "maf_low_while_running"
        episodes.append(
            {
                "type": kind,
                "origin": "inferred",
                "status": "candidate",
                "confidence": M.MODERATE_CONFIDENCE if persistent else M.LOW_CONFIDENCE,
                "t_start_wall": t0,
                "t_end_wall": t1,
                "t_start_rel_s": clk.rel(t0),
                "t_end_rel_s": clk.rel(t1),
                "duration_s": dur,
                "samples": len(ep),
                "zero_samples": sum(1 for v in vals if v <= M.MAF_ZERO_GS),
                "min_maf_gs": min(vals),
                "pattern": "persistent" if persistent else "single_transient",
                "cluster": cluster,
                "maf_before": None
                if before is None
                else {"value_gs": round(before.v, 2), "age_s": round(t0 - before.t, 2)},
                "maf_recovered": None
                if after is None
                else {"value_gs": round(after.v, 2), "after_s": round(after.t - t1, 2)},
                "rpm": {
                    "first": ep[0][1],
                    "last": ep[-1][1],
                    "window": {"n": len(rw), "min": round(min(rw), 1), "max": round(max(rw), 1), "std": rstd}
                    if rw
                    else None,
                    "unstable": unstable,
                    "idle_window_std_reference": idle_rpm_std_ref,
                },
                "context": ctx,
                "temperature_state": tg,
                "motion": mg,
                "ecu": ep[0][0]["ecu"],
                "evidence": [_evidence_row(s, clk) for s, _ in ep[:20]],
                "evidence_truncated": max(0, len(ep) - 20),
                "limitations": limits,
            }
        )
    sizes: dict[int, int] = {}
    for e in episodes:
        sizes[e["cluster"]] = sizes.get(e["cluster"], 0) + 1
    for e in episodes:
        e["repeated"] = sizes[e["cluster"]] >= 2
        if e["repeated"] and e["confidence"] == M.LOW_CONFIDENCE:
            e["confidence"] = M.MODERATE_CONFIDENCE
        e["text"] = (
            f"MAF {e['min_maf_gs']:.2f} g/s reported by ECU "
            f"{e['ecu'] or '?'} while measured RPM was >= {M.AIRPATH_RUNNING_RPM:.0f} on both sides "
            f"({e['samples']} sample(s), {e['duration_s']:.1f} s, {e['pattern'].replace('_', ' ')}"
            f"{', repeated' if e['repeated'] else ''}; {e['motion']}, {e['temperature_state']}). Candidate anomaly "
            "from a screening threshold; it does not identify a faulty part."
        )

    # -- abrupt dips with recovery (steady RPM)
    dips = []
    i = 0
    while i < len(run_ok) - 1:
        a, b = run_ok[i][0], run_ok[i + 1][0]
        if (
            b["t_wall"] - a["t_wall"] <= M.AIRPATH_DIP_RECOVERY_S
            and b["value"] <= a["value"] * M.AIRPATH_DIP_FRACTION
            and a["value"] - b["value"] >= M.AIRPATH_DIP_MIN_GS
        ):
            j, low_s = i + 1, b
            rec = None
            while (
                j + 1 < len(run_ok) and run_ok[j + 1][0]["t_wall"] - b["t_wall"] <= M.AIRPATH_DIP_RECOVERY_S
            ):
                j += 1
                c = run_ok[j][0]
                if c["value"] < low_s["value"]:
                    low_s = c
                if c["value"] >= a["value"] * M.AIRPATH_DIP_RECOVERED_FRACTION:
                    rec = c
                    break
            if rec is not None:
                ra = _fresh(rpm.held(a["t_wall"], M.AIRPATH_RPM_MAX_AGE_S))
                rc = _fresh(rpm.held(rec["t_wall"], M.AIRPATH_RPM_MAX_AGE_S))
                if ra and rc and abs(rc - ra) / ra <= M.AIRPATH_DIP_RPM_STEADY:
                    overl = any(e["t_start_wall"] <= low_s["t_wall"] <= e["t_end_wall"] for e in episodes)
                    dips.append(
                        {
                            "type": "maf_abrupt_dip",
                            "origin": "inferred",
                            "status": "candidate",
                            "confidence": M.LOW_CONFIDENCE,
                            "t_start_wall": a["t_wall"],
                            "t_end_wall": rec["t_wall"],
                            "t_start_rel_s": clk.rel(a["t_wall"]),
                            "t_end_rel_s": clk.rel(rec["t_wall"]),
                            "from_gs": a["value"],
                            "min_gs": low_s["value"],
                            "recovered_gs": rec["value"],
                            "recovery_s": round(rec["t_wall"] - low_s["t_wall"], 2),
                            "rpm_before": ra,
                            "rpm_after": rc,
                            "overlaps_low_maf_episode": overl,
                            "motion": _motion_state(L["speed"], a["t_wall"]),
                            "evidence": [_evidence_row(x, clk) for x in (a, low_s, rec)],
                            "text": f"MAF fell from {a['value']:.1f} to {low_s['value']:.1f} g/s and recovered to "
                            f"{rec['value']:.1f} g/s within {rec['t_wall'] - low_s['t_wall']:.1f} s while RPM stayed "
                            f"within {100 * M.AIRPATH_DIP_RPM_STEADY:.0f} % ({ra:.0f} -> {rc:.0f} rpm). Driver inputs "
                            "can also cause short drops; candidate only.",
                        }
                    )
                    i = j
                    continue
        i += 1

    n_run = n_state["running"]
    enough = n_run >= M.AIRPATH_MIN_RUNNING_MAF_SAMPLES
    for grp in (*groups.values(), *motion.values()):
        grp["low_per_1k"] = _per_1k(grp["low"], grp["running_samples"]) if enough else None
        grp["zero_per_1k"] = _per_1k(grp["zero"], grp["running_samples"]) if enough else None
    temps = [_fresh(L["coolant"].held(e["t_start_wall"], M.AIRPATH_COOLANT_MAX_AGE_S)) for e in episodes]
    bins: dict[int, int] = {}
    for c in temps:
        if c is not None:
            bins[int(c // 10) * 10] = bins.get(int(c // 10) * 10, 0) + 1
    gaps = [b["t_wall"] - a["t_wall"] for a, b in zip(ok, ok[1:], strict=False)]
    out.update(
        {
            "running_state": {
                "running_confirmed": n_run,
                "engine_not_running": n_state["not_running"],
                "unknown": n_state["unknown"],
                "note": f"Running = measured RPM >= {M.AIRPATH_RUNNING_RPM:.0f} right before AND right after the MAF "
                f"sample, each at most {M.AIRPATH_RPM_MAX_AGE_S:.0f} s away.",
            },
            "zero_running": zero_run,
            "low_running": low_run,
            "zero_per_1k_running": _per_1k(zero_run, n_run) if enough else None,
            "low_per_1k_running": _per_1k(low_run, n_run) if enough else None,
            "zero_engine_not_running": zero_off,
            "low_engine_not_running": low_off,
            "low_running_state_unknown": low_unknown,
            "zero_all_samples": sum(1 for s in ok if s["value"] <= M.MAF_ZERO_GS),
            "low_all_samples": sum(1 for s in ok if s["value"] < M.MAF_LOW_GS),
            "median_interval_s": round(statistics.median(gaps), 2) if gaps else None,
            "episodes": episodes,
            "episode_count": len(episodes),
            "episode_total_s": round(sum(e["duration_s"] for e in episodes), 2),
            "episode_max_s": max((e["duration_s"] for e in episodes), default=None),
            "persistent_episodes": sum(1 for e in episodes if e["pattern"] == "persistent"),
            "transient_episodes": sum(1 for e in episodes if e["pattern"] == "single_transient"),
            "repeated_clusters": sum(1 for v in sizes.values() if v >= 2),
            "dips": dips,
            "by_temperature": groups,
            "by_motion": motion,
            "episodes_by_coolant_c": [
                {"from_c": k, "to_c": k + 10, "episodes": v} for k, v in sorted(bins.items())
            ],
            "rates_computed": enough,
        }
    )
    return out


# -- EGR error excursions --------------------------------------------------------------------------------


def _egr(
    sd: SessionData, clk: M._Clock, L: dict[str, _Lookup], runs: list[dict[str, Any]], run_h: float | None
):
    ser = L["egr_error"].s
    if not ser:
        return {"samples": 0, "excursions": [], "count": None, "per_running_h": None}
    running = M._running_test(runs)
    run_s = [p for p in ser if running(p.t)]
    # excursion samples closer than AIRPATH_EGR_EXCURSION_GAP_S form one excursion (an oscillation
    # through zero stays one excursion, reported with direction "both")
    ex_raw: list[list[Any]] = []
    for p in run_s:
        if abs(p.v) < M.EGR_ERROR_SCREEN_PCT:
            continue
        if ex_raw and p.t - ex_raw[-1][-1].t <= M.AIRPATH_EGR_EXCURSION_GAP_S:
            ex_raw[-1].append(p)
        else:
            ex_raw.append([p])
    exc = []
    by_motion = {"idle": 0, "moving": 0, "unknown": 0}
    by_load = {"low": 0, "medium": 0, "high": 0, "unknown": 0}
    for e in ex_raw:
        peak = max(e, key=lambda p: abs(p.v))
        mo, lb = _motion_state(L["speed"], peak.t), _load_band(L["load"], peak.t)
        by_motion[mo] += 1
        by_load[lb] += 1
        signs = {("+" if p.v > 0 else "-") for p in e}
        exc.append(
            {
                "t_start_wall": e[0].t,
                "t_end_wall": e[-1].t,
                "t_start_rel_s": clk.rel(e[0].t),
                "t_end_rel_s": clk.rel(e[-1].t),
                "samples": len(e),
                "peak_pct": round(peak.v, 1),
                "max_abs_pct": round(abs(peak.v), 1),
                "direction": "both" if len(signs) > 1 else ("positive" if "+" in signs else "negative"),
                "at_peak": {
                    k: L[k].held(peak.t, M.AIRPATH_CONTEXT_MAX_AGE_S)
                    for k in ("rpm", "speed", "load", "egr_cmd")
                },
                "motion": mo,
                "load_band": lb,
            }
        )
    vals = [p.v for p in run_s]
    return {
        "samples": len(ser),
        "running_samples": len(run_s),
        "signed_mean_pct": round(statistics.fmean(vals), 2) if vals else None,
        "mean_abs_pct": round(statistics.fmean(abs(v) for v in vals), 2) if vals else None,
        "max_abs_pct": round(max(abs(v) for v in vals), 1) if vals else None,
        "excursion_sample_share": round(
            sum(1 for v in vals if abs(v) >= M.EGR_ERROR_SCREEN_PCT) / len(vals), 3
        )
        if vals
        else None,
        "excursions": exc,
        "count": len(exc),
        "positive": sum(1 for e in exc if e["direction"] == "positive"),
        "negative": sum(1 for e in exc if e["direction"] == "negative"),
        "both_directions": sum(1 for e in exc if e["direction"] == "both"),
        "per_running_h": _per_h(len(exc), run_h),
        "by_motion": by_motion,
        "by_load": by_load,
        "screen_pct": M.EGR_ERROR_SCREEN_PCT,
    }


# -- correlations ---------------------------------------------------------------------------------------------


def _is_symptom(a: Any) -> bool:
    if a.origin != "manual":
        return False
    if a.event_type in SYMPTOM_EVENTS:
        return True
    text = f"{a.label} {a.note}".lower()
    return a.event_type is None and any(w in text for w in SYMPTOM_WORDS)


def _correlations(
    sd: SessionData, clk: M._Clock, L: dict[str, _Lookup], maf: dict[str, Any], egr: dict[str, Any]
) -> dict[str, Any]:
    W = M.AIRPATH_ASSOC_WINDOW_S
    episodes = maf.get("episodes", [])
    exc_times = sorted(p.t for p in L["egr_error"].s if abs(p.v) >= M.EGR_ERROR_SCREEN_PCT)

    def exc_near(a: float, b: float) -> bool:
        i = bisect.bisect_left(exc_times, a)
        return i < len(exc_times) and exc_times[i] <= b

    out: dict[str, Any] = {"window_s": W, "episodes_evaluated": len(episodes)}
    # EGR error excursion near a MAF episode, against a rough chance level
    if L["egr_error"]:
        w = wo = ne = 0
        for e in episodes:
            if not L["egr_error"].between(e["t_start_wall"] - W, e["t_end_wall"] + W):
                ne += 1
                e["associations"] = {**e.get("associations", {}), "egr_excursion_nearby": None}
                continue
            near = exc_near(e["t_start_wall"] - W, e["t_end_wall"] + W)
            e["associations"] = {**e.get("associations", {}), "egr_excursion_nearby": near}
            w += int(near)
            wo += int(not near)
        rpm = L["rpm"]
        run_ts = [p.t for p in M._series(sd, MAF) if _running_state(rpm, p.t)[0] == "running"]
        chance = round(sum(1 for t in run_ts if exc_near(t - W, t + W)) / len(run_ts), 3) if run_ts else None
        out["egr_excursion_nearby"] = {"with": w, "without": wo, "not_evaluable": ne, "chance_share": chance}
    else:
        out["egr_excursion_nearby"] = None
    # MAP and commanded-EGR change, RPM instability
    for key, after_s, limit, name in (
        ("map", 1.0, M.AIRPATH_MAP_CHANGE_KPA, "map_change"),
        ("egr_cmd", M.AIRPATH_CONTEXT_MAX_AGE_S, M.AIRPATH_EGR_CMD_CHANGE_PCT, "egr_cmd_change"),
    ):
        if not L[key]:
            out[name] = None
            continue
        w = wo = ne = 0
        for e in episodes:
            ch = _change(L[key], e["t_start_wall"], e["t_end_wall"], after_s)
            e.setdefault("associations", {})[name] = ch
            if ch is None:
                ne += 1
            elif abs(ch["change"]) >= limit:
                w += 1
            else:
                wo += 1
        out[name] = {"changed": w, "unchanged": wo, "not_evaluable": ne, "screen": limit}
    uns = [e["rpm"]["unstable"] for e in episodes]
    out["rpm_unstable"] = {
        "unstable": sum(1 for u in uns if u is True),
        "stable": sum(1 for u in uns if u is False),
        "not_evaluated": sum(1 for u in uns if u is None),
        "note": "Evaluated at stationary idle only; RPM varies with driving otherwise.",
    }
    # manual symptom markers vs air-path anomalies
    marks = []
    anomalies = (
        [(e["t_start_wall"], e["t_end_wall"], e["type"]) for e in episodes]
        + [(d["t_start_wall"], d["t_end_wall"], d["type"]) for d in maf.get("dips", [])]
        + [(x["t_start_wall"], x["t_end_wall"], "egr_error_excursion") for x in egr.get("excursions", [])]
    )
    maf_ts = [p.t for p in M._series(sd, MAF)]
    for a in sd.annotations:
        if not _is_symptom(a):
            continue
        t = a.t_wall
        dist = [(0.0 if s <= t <= e else min(abs(t - s), abs(t - e)), kind) for s, e, kind in anomalies]
        near = sorted((d, k) for d, k in dist if d <= M.AIRPATH_ANNOTATION_WINDOW_S)
        lo = bisect.bisect_left(maf_ts, t - M.AIRPATH_ANNOTATION_WINDOW_S)
        hi = bisect.bisect_right(maf_ts, t + M.AIRPATH_ANNOTATION_WINDOW_S)
        marks.append(
            {
                "event": a.event_type or a.label,
                "t_rel_s": clk.rel(t),
                "note": a.note,
                "maf_samples_in_window": hi - lo,
                "evaluable": hi - lo > 0,
                "anomalies_nearby": [{"type": k, "distance_s": round(d, 1)} for d, k in near[:5]],
                "anomalies_nearby_total": len(near),
                "near_air_path_anomaly": bool(near) if hi - lo > 0 else None,
            }
        )
    out["symptom_markers"] = marks
    sup, con = [], []
    ex = out["egr_excursion_nearby"]
    if episodes and ex:
        line = (
            f"{ex['with']} of {len(episodes)} MAF episode(s) had an EGR-error excursion (|2D| >= "
            f"{M.EGR_ERROR_SCREEN_PCT:.0f} %) within +/-{W:.0f} s; across all running MAF samples "
            f"{100 * (ex['chance_share'] or 0):.0f} % had one that close (rough chance level)."
        )
        (sup if ex["with"] else con).append(line)
        if ex["without"] and ex["with"]:
            con.append(f"{ex['without']} MAF episode(s) had no EGR-error excursion nearby.")
    for name, what, unit in (("map_change", "MAP", "kPa"), ("egr_cmd_change", "Commanded EGR", "%")):
        c = out.get(name)
        if episodes and c:
            if c["changed"]:
                sup.append(
                    f"{what} changed by >= {c['screen']:g} {unit} during {c['changed']} MAF episode(s)."
                )
            if c["unchanged"]:
                con.append(
                    f"{what} stayed within +/-{c['screen']:g} {unit} during {c['unchanged']} MAF episode(s)."
                )
    r = out["rpm_unstable"]
    if r["unstable"]:
        sup.append(f"Idle RPM was unstable around {r['unstable']} MAF episode(s).")
    if r["stable"]:
        con.append(f"Idle RPM stayed stable around {r['stable']} MAF episode(s).")
    for m in marks:
        if m["near_air_path_anomaly"]:
            sup.append(
                f"Marker '{m['event']}' at {m['t_rel_s']:.0f} s: air-path anomaly within "
                f"{M.AIRPATH_ANNOTATION_WINDOW_S:.0f} s ({', '.join(x['type'] for x in m['anomalies_nearby'][:3])})."
            )
        elif m["near_air_path_anomaly"] is False:
            con.append(
                f"Marker '{m['event']}' at {m['t_rel_s']:.0f} s: no air-path anomaly within "
                f"{M.AIRPATH_ANNOTATION_WINDOW_S:.0f} s although MAF was measured."
            )
        else:
            con.append(
                f"Marker '{m['event']}' at {m['t_rel_s']:.0f} s: not evaluable (no MAF measured nearby)."
            )
    out["supporting"] = sup
    out["contradicting"] = con
    out["note"] = (
        "These are associations in time between separately polled signals, not causal relationships. "
        "They do not identify an EGR valve, MAF sensor, turbocharger or intake component as faulty."
    )
    return out


# -- DTCs -------------------------------------------------------------------------------------------------------


class _ScanClock:
    def __init__(self, t0: float) -> None:
        self.t0 = t0

    def rel(self, t: float | None) -> float | None:
        return None if t is None else round(t - self.t0, 2)


def _scan_clock(sd: SessionData) -> Any:
    """The session clock (first sample); tolerant of minimal / legacy session structures."""
    walls = [s["t_wall"] for s in sd.samples if s.get("t_wall") is not None]
    return _ScanClock(min(walls) if walls else float(sd.session.get("started_at") or 0.0))


def has_timeline(sd: SessionData) -> bool:
    """Enough structure for the time-based air-path analysis (every stored session has it)."""
    return "vehicle_profile" in sd.session and all(s.get("t_wall") is not None for s in sd.samples)


def dtc_summary(
    sd: SessionData, clk: M._Clock | None = None, owner_reported: dict[str, Any] | None = None
) -> dict[str, Any]:
    clk = clk or _scan_clock(sd)
    meta = sd.session.get("meta") or {}
    prof_id = sd.session.get("vehicle_profile")
    variant = confirmed_ecu_variant(meta)
    detected = []
    for sc in sd.scans:
        for d in sc.dtcs:
            detected.append(
                {
                    "code": d.code,
                    "ecu": d.ecu,
                    "status": d.kind,
                    "raw_hex": d.raw_hex,
                    "scan_phase": sc.phase,
                    "scan_t_rel_s": clk.rel(sc.t_wall),
                    "mil_on_at_scan": sc.mil_on,
                    "source": "read by this tool (generic OBD)",
                    **classify(d.code, prof_id, variant),
                }
            )
    phases_present = {sc.phase for sc in sd.scans}
    by_code: dict[str, dict[str, Any]] = {}
    for d in detected:
        b = by_code.setdefault(
            d["code"],
            {
                "code": d["code"],
                "family": d["family"],
                "air_path_relevance": d["air_path_relevance"],
                "description": d["description"],
                "description_status": d["description_status"],
                "note": d["note"],
                "ecus": [],
                "statuses": [],
                "scan_phases": [],
            },
        )
        for k, v in (("ecus", d["ecu"]), ("statuses", d["status"]), ("scan_phases", d["scan_phase"])):
            if v not in b[k]:
                b[k].append(v)
    for b in by_code.values():
        ph = b["scan_phases"]
        if "pre" in ph:
            b["timing"] = (
                "present in the pre-scan at the start of the recording: set before this session; when it was set is "
                "unknown, and it cannot be tied to any anomaly recorded in this session"
            )
        elif "post" in ph and "pre" in phases_present:
            b["timing"] = (
                "not in the pre-scan, reported in the post-scan: appeared during this session; the exact time is "
                "unknown (no timestamped evidence), so it is not tied to any single anomaly"
            )
        else:
            b["timing"] = "reported in a scan; when it was set is unknown"
    owner = (
        owner_reported
        if owner_reported is not None
        else (get_profile(prof_id).owner_reported if prof_id else {})
    )
    owner_codes = [
        {"code": c["code"], "system": c.get("system"), "source": c.get("source") or "owner report"}
        for c in (owner or {}).get("manufacturer_fault_codes", [])
    ]
    mil_scans = [sc.mil_on for sc in sd.scans if sc.mil_on is not None]
    mil_events = [
        {"event": a.event_type, "t_rel_s": clk.rel(a.t_wall), "origin": a.origin}
        for a in sd.annotations
        if a.event_type in ("mil_on", "mil_off")
    ]
    notes = []
    if not sd.scans:
        notes.append("No fault scan in this session: DTC status is unknown (not 'no faults').")
    if any(b["air_path_relevance"] == "unknown" for b in by_code.values()):
        notes.append(
            "Manufacturer-range code(s) present without a verified description for this ECU variant: they are "
            "neither counted as air-path faults nor as evidence that there is no air-path fault."
        )
    return {
        "scans": [
            {
                "phase": sc.phase,
                "t_rel_s": clk.rel(sc.t_wall),
                "mil_on": sc.mil_on,
                "mode_status": sc.mode_status,
                "dtc_count_reported": sc.dtc_count_reported,
            }
            for sc in sd.scans
        ],  # fmt: skip
        "detected": detected,
        "codes": sorted(by_code),
        "by_code": [by_code[c] for c in sorted(by_code)],
        "generic_air_path_codes": sorted(
            c for c, b in by_code.items() if b["air_path_relevance"] == "air_path_family"
        ),
        "unverified_manufacturer_codes": sorted(
            c for c, b in by_code.items() if b["air_path_relevance"] == "unknown"
        ),
        "mil_on": (any(mil_scans) if mil_scans else None),
        "mil_events": mil_events,
        "ecu_variant_confirmed": variant,
        "owner_reported_manufacturer_codes": owner_codes,
        "owner_reported_note": "Owner-reported BMW fault codes were read elsewhere (BMW tester); this tool cannot read "
        "them, and whether they are stored now is unknown. They are kept apart from the generic codes above.",
        "notes": notes,
        "no_extra_polling": "Fault codes come only from the existing pre/post scans and PID 01; nothing is cleared.",
    }


# -- entry points ---------------------------------------------------------------------------------------------------


def _idle_rpm_std_ref(L: dict[str, _Lookup], runs: list[dict[str, Any]]) -> float | None:
    running = M._running_test(runs)
    buckets: dict[int, list[float]] = {}
    for p in L["rpm"].s:
        if p.v >= M.AIRPATH_RUNNING_RPM and running(p.t) and _motion_state(L["speed"], p.t) == "idle":
            buckets.setdefault(int(p.t // 10), []).append(p.v)
    stds = [statistics.pstdev(v) for v in buckets.values() if len(v) >= 3]
    return round(statistics.median(stds), 1) if stds else None


def _core(sd: SessionData) -> dict[str, Any]:
    clk = M._Clock(sd)
    L = {k: _Lookup(M._series(sd, pid)) for k, (pid, _) in SIGNALS.items()}
    runs = M.engine_runs(sd)
    run_s = M._running_s(runs)
    run_h = run_s / 3600 if run_s >= 60 else None
    signals = {k: _signal_status(sd, k) for k in SIGNALS}
    ref = _idle_rpm_std_ref(L, runs)
    maf = (
        _maf(sd, clk, L, ref)
        if signals["maf"]["status"] == "measured"
        else {
            "samples_ok": 0,
            "failed_reads": {"n": signals["maf"]["failed_reads"], "by_quality": {}},
            "episodes": [],
            "dips": [],
        }
    )
    egr = _egr(sd, clk, L, runs, run_h)
    corr = _correlations(sd, clk, L, maf, egr)
    limits = [
        "Signals are polled one after another; each associated value is the latest measurement with its own age, "
        "never a simultaneous value.",
        "Zero / low MAF and every other threshold here are screening values of this tool, not manufacturer "
        "specifications; no component is diagnosed.",
        "BMW-specific values (MAF target, EGR target / actual, boost target, DDE fault memory) are not read, so a "
        "target-versus-actual comparison is not possible.",
    ]
    if signals["maf"]["status"] != "measured":
        limits.append(
            f"MAF not measured in this session ({signals['maf']['status']}): MAF statistics are unavailable, not zero."
        )
    else:
        rs = maf["running_state"]
        if rs["unknown"]:
            limits.append(
                f"{rs['unknown']} MAF sample(s) had no fresh RPM on both sides (or RPM in transition): engine state "
                f"unknown, not judged ({maf['low_running_state_unknown']} of them below {M.MAF_LOW_GS:g} g/s)."
            )
        if maf["failed_reads"]["n"]:
            limits.append(f"{maf['failed_reads']['n']} MAF read(s) failed: no value, not counted as zero.")
        if not maf["rates_computed"]:
            limits.append(
                f"Only {rs['running_confirmed']} MAF sample(s) with the engine confirmed running (need "
                f"{M.AIRPATH_MIN_RUNNING_MAF_SAMPLES}): rates not computed, counts only."
            )
    if signals["egr_error"]["status"] != "measured":
        limits.append(
            f"EGR error (2D) not measured ({signals['egr_error']['status']}): no excursion statistics."
        )
    if signals["map"]["status"] != "measured":
        limits.append("MAP (0B) not measured: no MAP association.")
    # dips that are part of a low-MAF episode are kept under maf.dips but not listed twice as events
    detected = [
        *maf.get("episodes", []),
        *(d for d in maf.get("dips", []) if not d["overlaps_low_maf_episode"]),
    ]
    return {
        "status": "evaluated" if signals["maf"]["status"] == "measured" else "maf_not_available",
        "signals": signals,
        "running_s": round(run_s, 1),
        "maf": maf,
        "egr_error": egr,
        "correlations": corr,
        "limitations": limits,
        "detected_events": detected,
        "heuristics": {k: M.HEURISTICS[k] for k in AIRPATH_KEYS},
        "provenance": {
            "measured": "MAF, RPM, speed, load, MAP, coolant, commanded EGR and EGR error values decoded from ECU answers; "
            "DTCs and MIL from the ECU's generic fault scans",
            "inferred": "running state, zero/low-MAF episodes, dips, excursions, cold/warm and idle/moving labels, "
            "associations and rates (screening heuristics)",
            "manual": "symptom markers (rough idle, noise, vibration, jerk, power loss)",
            "not_available": "BMW DDE fault memory, MAF / EGR / boost targets, EGR valve position (manufacturer values)",
        },
    }


def air_path_summary(sd: SessionData, owner_reported: dict[str, Any] | None = None) -> dict[str, Any]:
    """Air-path section of a session (memoised per loaded session; the DTC part follows ``owner_reported``)."""
    c = M._cache(sd)
    if "air_path" not in c:
        c["air_path"] = _core(sd)
    return {**c["air_path"], "dtcs": dtc_summary(sd, owner_reported=owner_reported)}


# -- longitudinal -----------------------------------------------------------------------------------------------------


def _bucket(g: dict[str, Any] | None) -> dict[str, Any] | None:
    if not g:
        return None
    return {k: g.get(k) for k in ("running_samples", "zero", "low", "low_per_1k", "zero_per_1k", "episodes")}


def air_path_row(sd: SessionData, ap: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compact, comparable statistics of one session. ``None`` means unavailable, never zero."""
    ap = ap or air_path_summary(sd)
    maf, egr, dt = ap["maf"], ap["egr_error"], ap["dtcs"]
    measured = ap["signals"]["maf"]["status"] == "measured"
    run_h = ap["running_s"] / 3600 if ap["running_s"] >= 60 else None
    rs = maf.get("running_state") or {}
    marks = ap["correlations"]["symptom_markers"]
    return {
        "id": sd.id,
        "started_at": sd.session["started_at"],
        "drive_type": (sd.session["meta"].get("user_labels") or {}).get("drive_type"),
        "maf_status": ap["signals"]["maf"]["status"],
        "running_h": round(run_h, 3) if run_h else None,
        "maf_running_samples": rs.get("running_confirmed") if measured else None,
        "maf_zero_running": maf.get("zero_running") if measured else None,
        "maf_low_running": maf.get("low_running") if measured else None,
        "maf_zero_per_1k": maf.get("zero_per_1k_running") if measured else None,
        "maf_low_per_1k": maf.get("low_per_1k_running") if measured else None,
        "maf_episodes": maf.get("episode_count") if measured else None,
        "maf_episodes_per_h": _per_h(maf.get("episode_count"), run_h) if measured else None,
        "maf_episode_total_s": maf.get("episode_total_s") if measured else None,
        "maf_episode_max_s": maf.get("episode_max_s") if measured else None,
        "maf_persistent_episodes": maf.get("persistent_episodes") if measured else None,
        "maf_repeated_clusters": maf.get("repeated_clusters") if measured else None,
        "maf_dips": len(maf.get("dips", [])) if measured else None,
        "maf_failed_reads": maf["failed_reads"]["n"],
        "cold": _bucket((maf.get("by_temperature") or {}).get("cold")) if measured else None,
        "warm": _bucket((maf.get("by_temperature") or {}).get("warm")) if measured else None,
        "idle": _bucket((maf.get("by_motion") or {}).get("idle")) if measured else None,
        "moving": _bucket((maf.get("by_motion") or {}).get("moving")) if measured else None,
        "episodes_by_coolant_c": maf.get("episodes_by_coolant_c") if measured else None,
        "rpm_unstable_episodes": ap["correlations"]["rpm_unstable"]["unstable"] if measured else None,
        "egr_status": ap["signals"]["egr_error"]["status"],
        "egr_excursions": egr["count"],
        "egr_excursions_per_h": egr["per_running_h"],
        "egr_max_abs_pct": egr.get("max_abs_pct"),
        "dtc_scanned": bool(dt["scans"]),
        "dtcs": dt["codes"] if dt["scans"] else None,
        "generic_air_path_dtcs": dt["generic_air_path_codes"] if dt["scans"] else None,
        "unverified_manufacturer_dtcs": dt["unverified_manufacturer_codes"] if dt["scans"] else None,
        "mil_on": dt["mil_on"],
        "symptom_markers": len(marks),
        "symptom_markers_near_anomaly": sum(1 for m in marks if m["near_air_path_anomaly"]),
    }


COMPARE_KEYS = ("maf_zero_per_1k", "maf_low_per_1k", "maf_episodes_per_h", "egr_excursions_per_h")


def _med(xs: list[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return round(statistics.median(vals), 2) if vals else None


def compare_to_cohort(sid: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """This session's rates against the median of the OTHER sessions of its cohort."""
    others = [r for r in rows if r["id"] != sid and r["maf_status"] == "measured"]
    if len(others) < M.AIRPATH_MIN_HISTORY_SESSIONS:
        return {
            "other_sessions": len(others),
            "medians": None,
            "note": f"Needs at least {M.AIRPATH_MIN_HISTORY_SESSIONS} other sessions of this vehicle and data origin "
            "with MAF measured before comparing.",
        }
    return {
        "other_sessions": len(others),
        "medians": {k: _med([r[k] for r in others]) for k in COMPARE_KEYS},
        "note": "Median over this vehicle's other sessions of the same data origin (real / synthetic never mixed).",
    }


def trends(rows: list[dict[str, Any]], cohort: dict[str, Any] | None) -> dict[str, Any]:
    """Multi-session air-path trend of ONE cohort (rows of one vehicle identity and data origin)."""
    rows = sorted(rows, key=lambda r: r["started_at"])
    measured = [r for r in rows if r["maf_status"] == "measured"]
    scanned = [r for r in rows if r["dtc_scanned"]]
    rec: dict[str, dict[str, Any]] = {}
    for r in scanned:
        for c in r["dtcs"] or []:
            x = rec.setdefault(
                c, {"code": c, "sessions": 0, "first_seen": r["started_at"], "last_seen": r["started_at"]}
            )
            x["sessions"] += 1
            x["last_seen"] = r["started_at"]
    for x in rec.values():
        x["share_of_scanned_sessions"] = round(x["sessions"] / len(scanned), 3)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in measured:
        by_type.setdefault(r["drive_type"] or "unlabelled", []).append(r)

    def pooled(key: str) -> dict[str, Any] | None:
        gs = [r[key] for r in measured if r.get(key)]
        if not gs:
            return None
        n = sum(g["running_samples"] or 0 for g in gs)
        low = sum(g["low"] or 0 for g in gs)
        zero = sum(g["zero"] or 0 for g in gs)
        return {"running_samples": n, "low": low, "zero": zero, "low_per_1k": _per_1k(low, n), "zero_per_1k": _per_1k(zero, n),
                "episodes": sum(g["episodes"] or 0 for g in gs)}  # fmt: skip

    halves = None
    if len(measured) >= 4:
        h = len(measured) // 2
        halves = {
            k: {
                "earlier_median": _med([r[k] for r in measured[:h]]),
                "later_median": _med([r[k] for r in measured[h:]]),
            }
            for k in COMPARE_KEYS
        }
    temp_bins: dict[int, int] = {}
    for r in measured:
        for b in r["episodes_by_coolant_c"] or []:
            temp_bins[b["from_c"]] = temp_bins.get(b["from_c"], 0) + b["episodes"]
    return {
        "cohort": cohort,
        "sessions": len(rows),
        "sessions_with_maf": len(measured),
        "sessions_scanned": len(scanned),
        "series": [
            {
                k: r[k]
                for k in (
                    "id",
                    "started_at",
                    "drive_type",
                    *COMPARE_KEYS,
                    "maf_episodes",
                    "egr_excursions",
                    "dtcs",
                    "mil_on",
                )
            }
            for r in rows
        ],
        "medians": {k: _med([r[k] for r in measured]) for k in COMPARE_KEYS},
        "by_drive_type": {
            t: {"sessions": len(rs), **{k: _med([r[k] for r in rs]) for k in COMPARE_KEYS}}
            for t, rs in sorted(by_type.items())
        },
        "cold": pooled("cold"),
        "warm": pooled("warm"),
        "idle": pooled("idle"),
        "moving": pooled("moving"),
        "episodes_by_coolant_c": [
            {"from_c": k, "to_c": k + 10, "episodes": v} for k, v in sorted(temp_bins.items())
        ],
        "sessions_with_low_maf_running": sum(1 for r in measured if (r["maf_low_running"] or 0) > 0),
        "dtc_recurrence": sorted(rec.values(), key=lambda x: (-x["sessions"], x["code"])),
        "mil_on_sessions": sum(1 for r in rows if r["mil_on"]),
        "earlier_vs_later": halves,
        "note": "Rates are normalised per 1000 MAF samples with the engine confirmed running, or per engine-running "
        "hour. Sessions without a signal are left out of its statistics (unavailable, not zero).",
    }
