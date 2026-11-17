"""Rules for the MINI diesel incident. Generic-OBD evidence only; BMW specifics are described, never read."""

from __future__ import annotations

from app.diagnostics.engine import AnalysisContext, SessionData
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

# SAE J2012 generic code families used only to say "a generic code in this family is present".
AIR_PATH_CODES = (
    {f"P010{i}" for i in range(10)}
    | {f"P040{i}" for i in range(10)}
    | {"P0234", "P0235", "P0236", "P0237", "P0238", "P0299", "P2413"}
)
DPF_CODES = {
    "P2002",
    "P2003",
    "P242F",
    "P244A",
    "P244B",
    "P2452",
    "P2453",
    "P2454",
    "P2455",
    "P2456",
    "P2458",
    "P2459",
    "P2463",
    "P246C",
}

EGR_ERR_SUSTAINED = 15.0  # % mean |EGR error| (PID 2D) treated as "sustained" - tool heuristic
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


def air_path_evidence(sd: SessionData) -> tuple[list[Evidence], list[Evidence], list[str], bool, bool]:
    """-> (supporting, contradicting, conditions, has_dtc, has_signal_evidence)."""
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

    codes = sorted(sd.dtc_codes() & AIR_PATH_CODES)
    if codes:
        sup.append(
            Evidence(
                text=f"Generic DTC(s) in the air-mass / manifold-pressure / EGR / boost families present: {', '.join(codes)}.",
                values={"dtcs": codes},
            )
        )
    elif sd.scans:
        con.append(
            Evidence(
                text="No generic DTC in the air-mass / manifold-pressure / EGR / boost families in this session's scans."
            )
        )

    signal = False
    err = [v for _, v in sd.series(EGR_ERR)]
    if len(err) >= 20:
        mean = sum(err) / len(err)
        ev = Evidence(
            text=f"Generic EGR error (PID 2D) averaged {mean:+.1f} % over {len(err)} samples.",
            values={"egr_error_mean": round(mean, 2), "n": len(err)},
        )
        if abs(mean) >= EGR_ERR_SUSTAINED:
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
    return sup, con, cond, bool(codes), signal


AIR_PATH_STEPS = [
    "Workshop smoke test of the intake / charge-air path after the turbocharger, including joints disturbed by earlier work.",
    "Inspect the MAF sensor connector and wiring; compare the engine-off MAF reading with zero.",
    "Inspect the hoses and connectors of the exhaust pressure sensor path for cracks or blockage.",
    "Have EGR valve travel and target/actual values checked with a BMW-capable tester (this tool cannot actuate or read them).",
]


def air_path_egr_maf(ctx: AnalysisContext) -> list[Finding]:
    sd = ctx.current
    sup, con, cond, has_dtc, has_signal = air_path_evidence(sd)
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
    sup, con, _cond, has_dtc, has_signal = air_path_evidence(sd)
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
