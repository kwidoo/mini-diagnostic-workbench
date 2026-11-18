"""Rules for the MINI diesel incident. Generic-OBD evidence only; BMW specifics are described, never read."""

from __future__ import annotations

from typing import Any

from app.diagnostics.air_path import air_path_summary, dtc_summary, has_timeline
from app.diagnostics.dtc_info import AIR_PATH_CODES, DPF_CODES
from app.diagnostics.engine import WINDOW_S, AnalysisContext, SessionData, windows
from app.diagnostics.monitoring import (
    AIRPATH_MIN_HISTORY_SESSIONS,
    AIRPATH_RUNNING_RPM,
    EGR_ERROR_SCREEN_PCT,
    MAF_LOW_GS,
    baseline_cohort,
    baseline_origin,
)
from app.models.domain import Evidence, Finding

RPM, MAP, MAF, BARO, EGR_CMD, EGR_ERR = 0x0C, 0x0B, 0x10, 0x33, 0x2C, 0x2D
DPF_PIDS = {0x7A: "DPF bank 1", 0x7B: "DPF bank 2", 0x7C: "DPF temperature"}
EXTENDED_AIR_PIDS = {
    0x69: "Commanded EGR and EGR error (extended)",
    0x6B: "EGR temperature",
    0x70: "Boost pressure control",
    0x73: "Exhaust pressure",
    0x87: "Intake manifold absolute pressure (extended)",
}

# AIR_PATH_CODES / DPF_CODES (SAE J2012 families) live in app.diagnostics.dtc_info and stay importable here.
__all__ = ["AIR_PATH_CODES", "DPF_CODES", "air_path_egr_maf", "dde_4862", "dpf_480a"]

# Screening heuristics of this tool, NOT vehicle-specific pass/fail limits (none are published
# for this ECU through generic OBD):
EGR_ERR_SCREEN = EGR_ERROR_SCREEN_PCT  # % |EGR error| (PID 2D) worth a closer look (monitoring.HEURISTICS)
EGR_MIN_SAMPLES = 20  # fewer good samples: no statement about the EGR error at all
EGR_MIN_SPAN_S = 10.0  # shorter recording: no statement
EGR_MIN_WINDOW_SAMPLES = 3  # a window/event needs at least this many samples to be evaluated
EGR_LOW_RATE_HZ = 1.0  # below this, short transients can fall between samples
KOEO_MAP_BARO_KPA = 5.0


def _owner_codes(ctx: AnalysisContext) -> set[str]:
    return {c["code"].upper() for c in ctx.owner_reported.get("manufacturer_fault_codes", [])}


def _support_line(sd: SessionData, pid: int, name: str) -> str:
    sup = sd.supported(pid)
    state = (
        "supported"
        if sup
        else "not supported by this ECU via generic OBD"
        if sup is False
        else "support unknown (no discovery data)"
    )
    return f"{name} (PID {pid:02X}): {state}."


def summarize_egr_error(
    series: list[tuple[float, float]], events: list[tuple[float, str]] | None = None
) -> dict[str, Any]:
    """Statistics of the generic EGR error PID that do not let +/- deviations cancel out.

    -> {"enough": bool, "limitations": [...], plus n, span_s, rate_hz, signed_mean,
    mean_abs, max_abs, windows (fixed WINDOW_S windows with mean |err| >= screen),
    events (+/-WINDOW_S around each annotation)}. Uses measured samples only.
    """
    out: dict[str, Any] = {"enough": False, "limitations": [], "n": len(series)}
    if not series:
        out["limitations"].append("EGR error (PID 2D): no good samples in this session; not measured.")
        return out
    ts = [t for t, _ in series]
    vals = [v for _, v in series]
    span = max(ts) - min(ts)
    rate = (len(series) - 1) / span if span > 0 else 0.0
    out.update(n=len(vals), span_s=round(span, 2), rate_hz=round(rate, 3))
    if len(vals) < EGR_MIN_SAMPLES or span < EGR_MIN_SPAN_S:
        out["limitations"].append(
            f"EGR error (PID 2D): only {len(vals)} samples over {span:.1f} s; at least {EGR_MIN_SAMPLES} samples "
            f"over {EGR_MIN_SPAN_S:.0f} s are needed before this tool says anything about it."
        )
        return out
    absv = [abs(v) for v in vals]
    out.update(
        enough=True,
        signed_mean=round(sum(vals) / len(vals), 2),
        mean_abs=round(sum(absv) / len(absv), 2),
        max_abs=round(max(absv), 2),
    )
    if rate < EGR_LOW_RATE_HZ:
        out["limitations"].append(
            f"EGR error (PID 2D) was sampled at only {rate:.2f} Hz; deviations shorter than "
            f"~{1 / rate if rate else 0:.0f} s can fall between samples and be missed."
        )
    flagged = []
    for k, w in sorted(windows(series).items()):
        if len(w) >= EGR_MIN_WINDOW_SAMPLES:
            m = sum(abs(v) for v in w) / len(w)
            if m >= EGR_ERR_SCREEN:
                flagged.append(
                    {
                        "t_start": k * WINDOW_S,
                        "t_end": (k + 1) * WINDOW_S,
                        "n": len(w),
                        "mean_abs": round(m, 2),
                    }
                )
    out["windows"] = flagged
    ev_out = []
    for t_ev, label in events or []:
        near = [abs(v) for t, v in series if abs(t - t_ev) <= WINDOW_S]
        if len(near) >= EGR_MIN_WINDOW_SAMPLES:
            ev_out.append(
                {
                    "t": round(t_ev, 2),
                    "label": label,
                    "n": len(near),
                    "mean_abs": round(sum(near) / len(near), 2),
                }
            )
        else:
            ev_out.append({"t": round(t_ev, 2), "label": label, "n": len(near), "mean_abs": None})
            out["limitations"].append(
                f"EGR error around annotation '{label}' at {t_ev:.1f} s: only {len(near)} sample(s) within "
                f"+/-{WINDOW_S:.0f} s; not evaluated."
            )
    out["events"] = ev_out
    return out


def egr_error_evidence(sd: SessionData) -> tuple[Evidence | None, bool, list[str]]:
    """-> (evidence or None, counts as supporting, limitation lines)."""
    events = [(t, a.label) for t, a in sd.annotation_times()]
    st = summarize_egr_error(sd.series(EGR_ERR), events)
    if not st["enough"]:
        return None, False, st["limitations"]
    flagged_events = [
        e for e in st["events"] if e["mean_abs"] is not None and e["mean_abs"] >= EGR_ERR_SCREEN
    ]
    sustained = abs(st["signed_mean"]) >= EGR_ERR_SCREEN
    intermittent = not sustained and (st["mean_abs"] >= EGR_ERR_SCREEN or bool(st["windows"]))
    parts = [
        f"Generic EGR error (PID 2D) over {st['n']} samples ({st['span_s']:.0f} s, {st['rate_hz']:.2f} Hz): "
        f"signed mean {st['signed_mean']:+.1f} %, mean |error| {st['mean_abs']:.1f} %, max |error| {st['max_abs']:.1f} %."
    ]
    if sustained:
        parts.append("The deviation is sustained in one direction.")
    elif intermittent:
        parts.append(
            "Large deviations in both directions cancel in the signed mean: the error is intermittent/oscillating, "
            f"with {len(st['windows'])} {WINDOW_S:.0f}-s window(s) at mean |error| >= {EGR_ERR_SCREEN:.0f} %."
        )
    if flagged_events:
        parts.append(
            "Around annotated events: "
            + "; ".join(
                f"'{e['label']}' at {e['t']:.0f} s mean |error| {e['mean_abs']:.1f} %" for e in flagged_events
            )
            + "."
        )
    values = {
        k: st[k]
        for k in ("n", "span_s", "rate_hz", "signed_mean", "mean_abs", "max_abs", "windows", "events")
    }
    values["egr_error_mean"] = st["signed_mean"]  # kept for existing exports/consumers
    values["screen_threshold_pct"] = EGR_ERR_SCREEN
    ev = Evidence(text=" ".join(parts), values=values)
    return ev, sustained or intermittent or bool(flagged_events), st["limitations"]


def air_path_evidence(
    sd: SessionData,
    history: list[SessionData] | None = None,
    owner_reported: dict[str, Any] | None = None,
) -> tuple[list[Evidence], list[Evidence], list[str], bool, bool]:
    """-> (supporting, contradicting, conditions, has_dtc, has_signal_evidence).

    ``history``: other sessions of the vehicle (only those of the same cohort are used for the
    cross-session lines). ``owner_reported``: the vehicle record (BMW codes stay apart from generic ones).
    """
    sup: list[Evidence] = []
    con: list[Evidence] = []
    cond = [
        _support_line(sd, p, n)
        for p, n in (
            (MAF, "MAF"),
            (MAP, "MAP absolute"),
            (BARO, "Barometric pressure"),
            (EGR_CMD, "Commanded EGR"),
            (EGR_ERR, "EGR error"),
        )
    ]
    for pid, name in EXTENDED_AIR_PIDS.items():
        if sd.supported(pid):
            cond.append(
                f"{name} (PID {pid:02X}): advertised by the ECU; raw bytes only, no verified decoder in this tool."
            )

    ap = air_path_summary(sd, owner_reported) if has_timeline(sd) else None
    dt = ap["dtcs"] if ap else dtc_summary(sd, owner_reported=owner_reported)
    codes = sorted(sd.dtc_codes() & AIR_PATH_CODES)
    if codes:
        sup.append(
            Evidence(
                text=f"Generic DTC(s) in the air-mass / manifold-pressure / EGR / boost families present: {', '.join(codes)}.",
                values={"dtcs": codes},
            )
        )
    for b in dt["by_code"]:
        if b["air_path_relevance"] != "unknown":
            continue
        rows = [d for d in dt["detected"] if d["code"] == b["code"]]
        where = "; ".join(
            f"ECU {d['ecu'] or '?'}, {d['status']}, {d['scan_phase']} scan (raw {d['raw_hex']})" for d in rows
        )
        cond.append(
            f"Generic OBD DTC {b['code']} read by this tool ({where}): {b['note']} Timing: {b['timing']}."
        )
    if dt["mil_on"]:
        cond.append("MIL reported ON in this session's fault scan(s).")
    if not codes and sd.scans and not dt["unverified_manufacturer_codes"]:
        con.append(
            Evidence(
                text="No generic DTC in the air-mass / manifold-pressure / EGR / boost families in this session's scans."
            )
        )
    elif not codes and dt["unverified_manufacturer_codes"]:
        cond.append(
            "No DTC from the SAE air-path families, but "
            + ", ".join(dt["unverified_manufacturer_codes"])
            + " (meaning unverified) is present, so this is NOT evidence that there is no air-path fault."
        )
    if dt["owner_reported_manufacturer_codes"]:
        cond.append(
            "Owner-reported BMW DDE fault(s) "
            + ", ".join(c["code"] for c in dt["owner_reported_manufacturer_codes"])
            + ": history from a BMW tester, not read by this tool; kept apart from the generic codes above."
        )

    signal = False
    ev, egr_supports, limits = egr_error_evidence(sd)
    if sd.supported(EGR_ERR) is not False:
        cond.extend(limits)
    if ev is not None:
        if egr_supports:
            sup.append(ev)
            signal = True
        else:
            con.append(ev)

    # key-on / engine-off plausibility: only from samples actually taken with RPM == 0
    rpm = sd.series(RPM)
    off_times = [t for t, v in rpm if v == 0]
    if off_times:
        lo, hi = min(off_times), max(off_times)
        mp = [v for t, v in sd.series(MAP) if lo <= t <= hi]
        ba = [v for t, v in sd.series(BARO) if lo <= t <= hi]
        mf = [v for t, v in sd.series(MAF) if lo <= t <= hi]
        if mp and ba:
            d = sum(mp) / len(mp) - sum(ba) / len(ba)
            ev = Evidence(
                text=f"Engine off: MAP minus barometric pressure = {d:+.1f} kPa (expected near 0).",
                values={"map_minus_baro_kpa": round(d, 2)},
            )
            if abs(d) > KOEO_MAP_BARO_KPA:
                sup.append(ev)
                signal = True
            else:
                con.append(ev)
        if mf:
            m = sum(mf) / len(mf)
            ev = Evidence(
                text=f"Engine off: MAF reads {m:.2f} g/s (expected near 0).",
                values={"maf_engine_off": round(m, 2)},
            )
            if m > 1.0:
                sup.append(ev)
                signal = True
            else:
                con.append(ev)
    mp, ba = sd.series(MAP), sd.series(BARO)
    if mp and ba:
        boost = sum(v for _, v in mp) / len(mp) - sum(v for _, v in ba) / len(ba)
        cond.append(
            f"Derived (not measured): boost gauge pressure = MAP - barometric = {boost:+.1f} kPa on average. MAP itself is absolute pressure."
        )
    elif mp:
        cond.append(
            "Boost gauge pressure not derived: barometric pressure was not recorded, and MAP alone is absolute pressure."
        )
    if ap is not None:
        signal = _maf_evidence(sd, ap, history, sup, con, cond) or signal
    return sup, con, cond, bool(codes), signal


def _maf_evidence(
    sd: SessionData,
    ap: dict[str, Any],
    history: list[SessionData] | None,
    sup: list[Evidence],
    con: list[Evidence],
    cond: list[str],
) -> bool:
    """MAF anomalies while running, their associations, and the cross-session picture. -> signal evidence?"""
    maf = ap["maf"]
    if ap["signals"]["maf"]["status"] != "measured":
        cond.append(
            f"MAF anomaly screening: MAF {ap['signals']['maf']['status'].replace('_', ' ')} (no statistics, not zero)."
        )
        return False
    rs = maf["running_state"]
    signal = False
    eps = maf["episodes"]
    values = {
        k: maf[k]
        for k in (
            "zero_running", "low_running", "zero_per_1k_running", "low_per_1k_running", "episode_count",
            "episode_total_s", "persistent_episodes", "repeated_clusters", "zero_engine_not_running",
            "low_running_state_unknown",
        )
    }  # fmt: skip
    values["running_confirmed_samples"] = rs["running_confirmed"]
    values["failed_reads"] = maf["failed_reads"]
    if eps:
        signal = maf["zero_running"] > 0 or maf["persistent_episodes"] > 0 or maf["repeated_clusters"] > 0
        sup.append(
            Evidence(
                text=f"MAF below {MAF_LOW_GS:g} g/s in {maf['low_running']} of {rs['running_confirmed']} samples taken while "
                f"measured RPM was >= {AIRPATH_RUNNING_RPM:.0f} on both sides ({maf['zero_running']} exactly 0.00 g/s, "
                f"present in the ECU's raw answer); {len(eps)} episode(s), {maf['persistent_episodes']} persistent, "
                f"{maf['repeated_clusters']} repeated cluster(s). Screening values, not a sensor diagnosis.",
                values={**values, "episodes": [
                    {k: e[k] for k in ("t_start_rel_s", "duration_s", "samples", "min_maf_gs", "pattern", "repeated",
                                       "motion", "temperature_state", "ecu")}
                    for e in eps[:20]
                ]},
            )
        )  # fmt: skip
    elif rs["running_confirmed"] >= 20:
        con.append(
            Evidence(
                text=f"No MAF value below {MAF_LOW_GS:g} g/s in {rs['running_confirmed']} samples taken with the engine "
                "confirmed running.",
                values=values,
            )
        )
    if maf["dips"]:
        sup.append(
            Evidence(
                text=f"{len(maf['dips'])} abrupt MAF dip(s) with recovery while RPM stayed steady (candidate; driver "
                "inputs can also cause short drops).",
                values={"dips": [{k: d[k] for k in ("t_start_rel_s", "from_gs", "min_gs", "recovered_gs", "recovery_s")}
                                 for d in maf["dips"][:20]]},
            )
        )  # fmt: skip
    if maf["zero_engine_not_running"]:
        cond.append(
            f"{maf['zero_engine_not_running']} MAF sample(s) at 0 g/s with the engine measured not running: expected, not counted."
        )
    corr = ap["correlations"]
    if eps:
        for line in corr["supporting"]:
            cond.append("Association (not causation): " + line)
        for line in corr["contradicting"]:
            con.append(Evidence(text=line))
    else:
        for m in corr["symptom_markers"]:
            if m["near_air_path_anomaly"]:
                cond.append(
                    f"Association (not causation): marker '{m['event']}' near an EGR-error excursion."
                )
    cohort = baseline_cohort(sd.session)
    same = [h for h in history or [] if h.id != sd.id and baseline_cohort(h.session) == cohort]
    if len(same) >= AIRPATH_MIN_HISTORY_SESSIONS:
        from app.diagnostics.air_path import air_path_row

        rows = [air_path_row(h) for h in same]
        measured = [r for r in rows if r["maf_status"] == "measured"]
        low = sum(1 for r in measured if (r["maf_low_running"] or 0) > 0)
        scanned = [r for r in rows if r["dtc_scanned"]]
        here = set(ap["dtcs"]["codes"])
        recur = {c: sum(1 for r in scanned if c in (r["dtcs"] or [])) for c in sorted(here)}
        cond.append(
            f"Other {baseline_origin(sd.session)} sessions of this vehicle: MAF measured in {len(measured)}, low MAF while "
            f"running in {low} of them"
            + (
                "; " + ", ".join(f"{c} present in {n} of {len(scanned)} scanned" for c, n in recur.items())
                if recur and scanned
                else ""
            )
            + "."
        )
    return signal


AIR_PATH_STEPS = [
    "Workshop smoke test of the intake / charge-air path after the turbocharger, including joints disturbed by earlier work.",
    "Inspect the MAF sensor connector and wiring; compare the engine-off MAF reading with zero.",
    "Inspect the hoses and connectors of the exhaust pressure sensor path for cracks or blockage.",
    "Have EGR valve travel and target/actual values checked with a BMW-capable tester (this tool cannot actuate or read them).",
]


def air_path_egr_maf(ctx: AnalysisContext) -> list[Finding]:
    sd = ctx.current
    sup, con, cond, has_dtc, has_signal = air_path_evidence(sd, ctx.history, ctx.owner_reported)
    f = Finding(
        id="air_path_egr_maf",
        title="Air path: EGR / MAF plausibility (generic OBD)",
        hypothesis="Measured air mass and EGR behaviour are not plausible together.",
        explanation=(
            "Uses only generic OBD values. EGR target/actual and air-mass target are manufacturer values and are "
            "not available, so a proper target-versus-actual comparison is not possible here."
        ),
        strength="strong"
        if has_dtc and has_signal
        else "suggestive"
        if has_dtc or has_signal
        else "insufficient",
        supporting=sup,
        contradicting=con,
        conditions=cond,
        caveats=[
            "The generic EGR error PID is defined loosely and implemented differently between ECUs; the ECU variant of this car is unconfirmed.",
            "A generic DTC names a monitored system, not a failed part.",
            "No reference MAF value is asserted: the correct idle air mass depends on the engine variant and EGR state.",
            f"The {EGR_ERR_SCREEN:.0f} % EGR-error level is a screening heuristic of this tool, not a BMW pass/fail threshold.",
            "Signals are polled one after another, never at the same instant; values of different PIDs are not simultaneous.",
            "MAF 0.00 g/s with the engine running is not a plausible air mass, but this tool cannot tell whether the "
            "sensor, its wiring, the ECU's signal processing or the generic-OBD reporting produced it.",
            "Manufacturer-range codes such as P115D are shown as raw codes: their meaning is not verified for this ECU variant.",
            "Associations between MAF, EGR, MAP and RPM are temporal, not causal; no component is identified as faulty.",
        ],
        next_steps=list(AIR_PATH_STEPS),
    )
    return [f]


def dpf_480a(ctx: AnalysisContext) -> list[Finding]:
    sd = ctx.current
    f = Finding(
        id="dpf_480a",
        title="DPF status / owner-reported fault 480A",
        hypothesis="The particulate filter system is involved in the symptoms.",
        explanation=(
            "480A is a BMW-specific DDE fault code reported by the owner. Its exact wording depends on the DDE "
            "variant, which is unconfirmed for this car, so this tool does not interpret it beyond 'reported'. "
            "BMW hex fault codes cannot be read through generic OBD, so whether 480A is stored now is unknown."
        ),
        strength="insufficient",
        conditions=[
            "DPF soot load: not measured (manufacturer value, unavailable).",
            "DPF regeneration status and history: not measured (manufacturer value, unavailable).",
            "DPF differential pressure / exhaust back-pressure in physical units: not measured.",
        ],
        caveats=["This tool never triggers a DPF regeneration and offers no way to do so."],
        next_steps=[
            "Have soot/ash load, differential pressure at idle and at 2000 rpm, and the regeneration history read with a BMW-capable tester.",
            "Inspect the DPF differential-pressure sensor hoses for cracks, kinks or blockage.",
        ],
    )
    if "480A" in _owner_codes(ctx):
        f.conditions.insert(0, "Owner-reported BMW fault 480A (not read by this tool).")
    advertised = [f"{n} (PID {p:02X})" for p, n in DPF_PIDS.items() if sd.supported(p)]
    if advertised:
        f.conditions.append(
            "ECU advertises standard DPF PID(s): "
            + ", ".join(advertised)
            + ". Raw bytes are stored; no verified decoder, so no physical value is shown."
        )
    elif sd.caps:
        f.conditions.append("ECU advertises no standard DPF PIDs (7A/7B/7C) via generic OBD.")
    codes = sorted(sd.dtc_codes() & DPF_CODES)
    if codes:
        f.supporting.append(
            Evidence(
                text=f"Generic DTC(s) in the SAE particulate-filter family present: {', '.join(codes)}.",
                values={"dtcs": codes},
            )
        )
        f.strength = "suggestive"
        f.caveats.append(
            "A generic DPF-family code shows the ECU flagged that system; it does not show whether the filter, a sensor, a hose or an upstream fault is responsible."
        )
    elif sd.scans:
        f.contradicting.append(
            Evidence(text="No generic DTC in the particulate-filter family in this session's scans.")
        )
    return [f]


def dde_4862(ctx: AnalysisContext) -> list[Finding]:
    if "4862" not in _owner_codes(ctx):
        return []
    sd = ctx.current
    sup, con, _cond, has_dtc, has_signal = air_path_evidence(sd, ctx.history, ctx.owner_reported)
    f = Finding(
        id="dde_4862",
        title="Owner-reported BMW DDE fault 4862",
        hypothesis="The earlier 4862 entry and the current symptoms share an air-mass / EGR plausibility cause.",
        explanation=(
            "On another BMW diesel (E70 X5 xDrive35d) BMW describes 4862 as 'Air system, air to EGR mass flow, "
            "plausibility'. That bulletin is for a different engine and vehicle; for this MINI the meaning is "
            "subject to confirming the DDE variant. Treat it as: the ECU judged measured air mass implausible "
            "against its EGR model."
        ),
        strength="suggestive" if has_dtc or has_signal else "insufficient",
        supporting=sup,
        contradicting=con,
        conditions=[
            "Owner-reported BMW fault 4862 (not read by this tool; whether it is stored now is unknown)."
        ],
        caveats=[
            "A plausibility fault names a mismatch between signals. On its own it does not identify the EGR valve, the MAF sensor, a leak or the exhaust pressure path as the cause.",
            "The referenced BMW bulletin is context only and is not a repair procedure for this car.",
        ],
        next_steps=[
            "Confirm the DDE variant (calibration ID from a scan, or vehicle documents) before relying on any code description.",
            *[
                s
                for s in (
                    "Workshop smoke test of the intake / charge-air path, including the EGR cooler and intake manifold joints.",
                    "Check the charge-air temperature sensor and MAF sensor signals with a BMW-capable tester.",
                    "Check EGR valve operation and the exhaust pressure measurement path (sensor hoses).",
                )
            ],
        ],
    )
    return [f]
