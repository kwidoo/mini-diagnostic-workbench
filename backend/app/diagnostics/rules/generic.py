"""Vehicle-independent rules. Thresholds are this tool's screening heuristics, not manufacturer limits."""

from __future__ import annotations

from typing import Any

from app.diagnostics.engine import WINDOW_S, AnalysisContext, SessionData, latest_before, stats, windows
from app.models.domain import Evidence, Finding
from app.parameters.standard_pids import PIDS

RPM, SPEED, COOLANT = 0x0C, 0x0D, 0x05
IDLE_P2P_RPM = 100.0  # peak-to-peak within one 5 s window that counts as "unstable"
MIN_RPM_SAMPLES = 30
POLLING_CAVEAT = (
    "Signals are polled one after another, not simultaneously. Co-variation within a 5 s window is a temporal "
    "association only; it cannot show which signal moved first or what caused what."
)
COVARIATION_PIDS = (0x10, 0x0B, 0x23, 0x59, 0x22, 0x06, 0x08)


def idle_windows(sd: SessionData) -> dict[str, Any]:
    """Per-window idle RPM statistics for a session. Returns {} when not analysable."""
    rpm = sd.series(RPM)
    if len(rpm) < MIN_RPM_SAMPLES:
        return {"reason": f"only {len(rpm)} usable RPM samples (need {MIN_RPM_SAMPLES})"}
    speed = sd.series(SPEED)
    running = [(t, v) for t, v in rpm if v >= 300]
    if len(running) < MIN_RPM_SAMPLES:
        return {"reason": "engine was not running for most of the session"}
    if speed:
        idle = [(t, v) for t, v in running if (latest_before(speed, t) or 0) == 0]
    else:
        idle = running
    if len(idle) < MIN_RPM_SAMPLES:
        return {
            "reason": "vehicle was moving for most of the session; this playbook only looks at stationary idle"
        }
    wins = {k: v for k, v in windows(idle).items() if len(v) >= 3}
    unstable = sorted(k for k, v in wins.items() if max(v) - min(v) > IDLE_P2P_RPM)
    return {"idle": idle, "windows": wins, "unstable": unstable, "speed_known": bool(speed)}


def idle_rpm_stability(ctx: AnalysisContext) -> list[Finding]:
    sd = ctx.current
    f = Finding(
        id="idle_rpm_stability",
        title="Idle RPM stability",
        hypothesis="Engine speed is unstable at stationary idle.",
        explanation=(
            "Quantifies how much the reported engine speed varies while the vehicle is stationary, and notes which "
            "other recorded signals varied more in the same time windows. It describes what was measured; it does "
            "not name a faulty part."
        ),
        strength="insufficient",
        conditions=[
            f"Unstable window = RPM peak-to-peak > {IDLE_P2P_RPM:.0f} rpm inside one {WINDOW_S:.0f} s window (tool heuristic)."
        ],
        caveats=[POLLING_CAVEAT],
    )
    if sd.session["kind"] != "record" and not sd.series(RPM):
        f.caveats.append("This session has no recording; run a recording at idle.")
        f.next_steps.append(
            "Record at least 120 s at idle, engine warm and cold, and mark events when the idle feels rough."
        )
        return [f]
    iw = idle_windows(sd)
    if "reason" in iw:
        f.caveats.append(iw["reason"])
        f.next_steps.append("Record at least 120 s with the engine idling and the vehicle stationary.")
        return [f]
    vals = [v for _, v in iw["idle"]]
    st = stats(vals)
    n_win, unstable = len(iw["windows"]), iw["unstable"]
    m = sd.metric(RPM) or {}
    hz = m.get("actual_hz", 0.0)
    f.conditions.append(f"{st['n']} idle RPM samples in {n_win} windows; observed RPM sample rate {hz} Hz.")
    summary = Evidence(
        text=f"Idle RPM mean {st['mean']}, std {st['std']}, min {st['min']}, max {st['max']} rpm.", values=st
    )
    if not iw["speed_known"]:
        f.caveats.append("Vehicle speed was not recorded, so 'stationary' could not be confirmed from data.")
    if m.get("dropped"):
        f.caveats.append(f"{m['dropped']} of {m['attempts']} RPM reads failed; gaps are left as gaps.")
    if hz and hz < 2:
        f.caveats.append(
            f"RPM was sampled at only {hz} Hz; oscillations faster than about {hz / 2:.1f} Hz cannot be resolved."
        )

    if unstable:
        f.supporting.append(summary)
        spans = ", ".join(f"{k * WINDOW_S:.0f}-{(k + 1) * WINDOW_S:.0f} s" for k in unstable[:8])
        worst = max(max(iw["windows"][k]) - min(iw["windows"][k]) for k in unstable)
        f.supporting.append(
            Evidence(
                text=f"{len(unstable)} of {n_win} windows exceeded {IDLE_P2P_RPM:.0f} rpm peak-to-peak (worst {worst:.0f} rpm) at {spans}.",
                values={
                    "unstable_windows": len(unstable),
                    "windows": n_win,
                    "worst_p2p_rpm": round(worst, 1),
                },
            )
        )
        f.strength = "strong" if len(unstable) >= 2 and hz >= 2 else "suggestive"
        for pid in COVARIATION_PIDS:
            ser = sd.series(pid)
            if not ser:
                continue
            w = {k: v for k, v in windows(ser).items() if len(v) >= 3}
            rough = [max(w[k]) - min(w[k]) for k in unstable if k in w]
            calm = [max(v) - min(v) for k, v in w.items() if k not in unstable and k in iw["windows"]]
            if len(rough) < 1 or len(calm) < 2:
                continue
            r, c = sum(rough) / len(rough), sum(calm) / len(calm)
            spec = PIDS[pid]
            ev = Evidence(
                text=f"{spec.name}: mean in-window range {r:.2f} {spec.unit} during unstable RPM windows vs {c:.2f} {spec.unit} otherwise.",
                values={"pid": f"{pid:02X}", "range_unstable": round(r, 3), "range_stable": round(c, 3)},
            )
            (f.supporting if r >= 2 * max(c, 1e-9) else f.contradicting).append(ev)
        marks = [a.label for t, a in sd.annotation_times() if int(t // WINDOW_S) in unstable]
        if marks:
            f.supporting.append(
                Evidence(text="Event markers inside unstable windows: " + ", ".join(sorted(set(marks))) + ".")
            )
        f.next_steps += [
            "Repeat the recording cold and fully warm to see whether the instability depends on temperature.",
            "Have a workshop inspect the intake tract for leaks (smoke test) and check sensor connectors before replacing parts.",
        ]
    else:
        f.contradicting.append(summary)
        f.contradicting.append(
            Evidence(text=f"No window exceeded {IDLE_P2P_RPM:.0f} rpm peak-to-peak in this session.")
        )
        f.caveats.append(
            "The reported fault is intermittent: a steady recording does not show the fault is absent."
        )
        f.next_steps.append(
            "Record again when the symptom is present and mark the moment with an event marker."
        )
    return [f]


def fuel_trim_bank_balance(ctx: AnalysisContext) -> list[Finding]:
    sd = ctx.current
    f = Finding(
        id="fuel_trim_bank_balance",
        title="Fuel trim balance between cylinder banks",
        hypothesis="One cylinder bank is being corrected much more than the other.",
        explanation=(
            "Compares generic long-term fuel trims of bank 1 and bank 2. A large one-sided correction points to "
            "something affecting only that bank; equal large corrections point to something common to both."
        ),
        strength="insufficient",
        conditions=[
            "Heuristic: bank difference > 8 percentage points, or |long-term trim| > 10 % on a bank."
        ],
        caveats=[
            "Generic OBD trims only; manufacturer adaptation values are not available through this tool."
        ],
    )
    b1, b2 = [v for _, v in sd.series(0x07)], [v for _, v in sd.series(0x09)]
    if len(b1) < 5 or len(b2) < 5:
        missing = [n for n, p in (("bank 1", 0x07), ("bank 2", 0x09)) if sd.supported(p) is False]
        f.caveats.append(
            ("Not supported by this ECU: long-term trim " + ", ".join(missing) + ".")
            if missing
            else "Not enough long-term fuel trim samples in this session."
        )
        f.next_steps.append("Record an idle session that includes long-term fuel trims for both banks.")
        return [f]
    m1, m2 = sum(b1) / len(b1), sum(b2) / len(b2)
    ev = Evidence(
        text=f"Long-term fuel trim mean: bank 1 {m1:+.1f} %, bank 2 {m2:+.1f} % (difference {abs(m1 - m2):.1f} points).",
        values={"ltft_b1": round(m1, 2), "ltft_b2": round(m2, 2)},
    )
    if abs(m1 - m2) > 8 or max(abs(m1), abs(m2)) > 10:
        f.supporting.append(ev)
        f.strength = "suggestive"
        side = "bank 2" if abs(m2) > abs(m1) else "bank 1"
        f.next_steps += [
            f"Inspect {side} for intake leaks and exhaust leaks ahead of the oxygen sensor.",
            "Compare oxygen sensor behaviour between banks with a workshop tester before replacing parts.",
        ]
    else:
        f.contradicting.append(ev)
    return [f]


def intermittent_progression(ctx: AnalysisContext) -> list[Finding]:
    f = Finding(
        id="intermittent_progression",
        title="Fault progression across sessions",
        hypothesis="The fault picture changes between sessions in a repeatable way.",
        explanation="Compares DTC snapshots, MIL state, event markers and idle stability across all stored sessions of this vehicle.",
        strength="insufficient",
        caveats=["Synthetic (simulator) and real sessions are compared separately; they are never mixed."],
    )
    synthetic = ctx.current.session["synthetic"]
    hist = [s for s in ctx.history if s.session["synthetic"] == synthetic and (s.scans or s.samples)]
    if len(hist) < 2:
        f.caveats.append(f"Only {len(hist)} comparable session(s) stored; progression needs at least two.")
        f.next_steps.append(
            "Capture sessions on different days and at different temperatures (cold start, fully warm)."
        )
        return [f]
    prev_codes: set[str] | None = None
    rows = []
    for s in hist:
        codes = s.dtc_codes()
        iw = idle_windows(s)
        mil = s.mil_states()
        row = {
            "session": s.id,
            "dtcs": sorted(codes),
            "mil_on": any(mil) if mil else None,
            "unstable_windows": len(iw["unstable"]) if "unstable" in iw else None,
            "markers": [a.label for a in s.annotations],
        }
        if prev_codes is not None and (s.scans):
            row["appeared"], row["disappeared"] = sorted(codes - prev_codes), sorted(prev_codes - codes)
        if s.scans:
            prev_codes = codes
        rows.append(row)
    changed = [r for r in rows if r.get("appeared") or r.get("disappeared")]
    for r in changed:
        f.supporting.append(
            Evidence(
                text=f"Session {r['session']}: DTCs appeared {r['appeared'] or 'none'}, disappeared {r['disappeared'] or 'none'}.",
                values=r,
            )
        )
    unstable_sessions = [r for r in rows if r["unstable_windows"]]
    if unstable_sessions:
        f.supporting.append(
            Evidence(
                text=f"Idle instability was captured in {len(unstable_sessions)} of {len(rows)} sessions.",
                values={"sessions": [r["session"] for r in unstable_sessions]},
            )
        )
    temps = []
    for s in hist:
        cool = s.series(COOLANT)
        for t, a in s.annotation_times():
            v = latest_before(cool, t)
            if v is not None:
                temps.append((a.label, v))
    if temps:
        lo, hi = min(v for _, v in temps), max(v for _, v in temps)
        f.supporting.append(
            Evidence(
                text=f"{len(temps)} event marker(s) had a coolant reading; last measured coolant temperature before them ranged {lo:.0f}-{hi:.0f} °C.",
                values={"events": [{"label": lab, "coolant_c": v} for lab, v in temps]},
            )
        )
    if not changed and not unstable_sessions:
        f.contradicting.append(
            Evidence(text=f"No DTC changes and no captured idle instability across {len(rows)} sessions.")
        )
    elif len(hist) >= 3 and (changed or len(unstable_sessions) >= 2):
        f.strength = "suggestive"
    f.conditions.append(f"{len(rows)} comparable sessions.")
    f.next_steps.append(
        "Keep adding sessions with event markers; note ambient temperature and whether the engine was cold."
    )
    return [f]


def repair_history(ctx: AnalysisContext) -> list[Finding]:
    repairs = ctx.owner_reported.get("repairs") or []
    if not repairs:
        return []
    f = Finding(
        id="repair_history",
        title="Previous repair (historical observation)",
        hypothesis="Earlier repair work is relevant context for the current symptoms.",
        explanation=(
            "Recorded as history only. It is not proof that the work was carried out, and not evidence that it was "
            "not; this tool cannot verify either."
        ),
        strength="insufficient",
    )
    for r in repairs:
        f.conditions.append(
            f"{r['description']} - {'verified' if r.get('verified') else 'owner-reported, not independently verified'}."
        )
    if ctx.owner_reported.get("notes"):
        f.conditions.append(ctx.owner_reported["notes"])
    f.caveats.append(
        "Symptoms persisting after a repair are compatible with several explanations, including a different root cause."
    )
    f.next_steps += [
        "Ask the workshop for the invoice with part numbers and, if possible, the replaced parts.",
        "Have joints and gaskets disturbed during that work checked for leaks (smoke test) before any further parts are bought.",
    ]
    return [f]


def manufacturer_data_availability(ctx: AnalysisContext) -> list[Finding]:
    out = []
    for ext in ctx.manufacturer_status:
        names = [p["name"] for p in ext["parameters"] if p["status"] != "available"]
        f = Finding(
            id=f"manufacturer_data_{ext['id']}",
            title="Manufacturer-specific data: not available",
            hypothesis="Measurements that would narrow this down further exist in the ECU but are out of reach of this tool.",
            explanation=ext["access_requirements"],
            strength="insufficient",
            conditions=[f"Not measured: {n}." for n in names],
            caveats=[
                "Nothing in this report is derived from manufacturer-specific data. No such request was sent."
            ],
            next_steps=[
                "If these values are needed, have them read with a manufacturer-capable tester and note them alongside these sessions."
            ],
        )
        out.append(f)
    return out
