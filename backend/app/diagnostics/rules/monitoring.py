"""Findings for continuous-monitoring sessions (mini_extended_monitoring).

They return nothing for other sessions, so existing analyses are unchanged.
Every finding here rests on generic OBD values, the adapter voltage and manual
annotations. Heuristic results are 'possible'; the strength never goes above
'suggestive' because no manufacturer (BMW DDE) value confirms them.
"""

from __future__ import annotations

from typing import Any

from app.diagnostics.engine import AnalysisContext
from app.diagnostics.monitoring import (
    VOLT_LOW_RUNNING_V,
    is_monitoring_session,
    monitoring_summary,
)
from app.models.domain import Evidence, Finding

HEURISTIC_CAVEAT = (
    "Thresholds are screening heuristics of this tool, mostly relative to this vehicle's own history; "
    "they are not BMW / MINI limits."
)
NO_ECU_CONFIRMATION = (
    "No manufacturer value (BMW DDE regeneration status, soot load, regeneration history) is read, so nothing "
    "here is confirmed by the ECU."
)


def _summary(ctx: AnalysisContext) -> dict[str, Any] | None:
    """One monitoring summary per analysis run, shared by the four rules."""
    if not is_monitoring_session(ctx.current.session):
        return None
    cached = getattr(ctx, "_monitoring_summary", None)
    if cached is None:
        cached = monitoring_summary(ctx.current, ctx.history)
        ctx._monitoring_summary = cached  # type: ignore[attr-defined]
    return cached


def monitoring_dpf_regeneration(ctx: AnalysisContext) -> list[Finding]:
    m = _summary(ctx)
    if m is None:
        return []
    dpf = m["dpf"]
    f = Finding(
        id="monitoring_dpf_regeneration",
        title="DPF regeneration: possible and possibly interrupted regenerations (monitoring)",
        hypothesis="DPF regenerations are started but interrupted by engine stops on short trips.",
        explanation=(
            "Looks for indirect signs of a regeneration at warm stationary idle (commanded EGR nearly closed, idle "
            "RPM raised, MAF raised - relative to this car's usual warm idle) and for such a regeneration, or your "
            "own 'suspected regeneration' marker, still active when the engine stopped. Correlates that with fan "
            "runs you marked after the stop and with the driving conditions."
        ),
        strength="insufficient",
        caveats=[NO_ECU_CONFIRMATION, HEURISTIC_CAVEAT, *dpf["limitations"]],
    )
    avail = {i["key"]: i for g in dpf["availability"] if g["group"] == "wanted" for i in g["items"]}
    f.conditions += [f"{i['name']}: {i['status'].replace('_', ' ')} ({i['via']})." for i in avail.values()]
    raw = [
        i
        for g in dpf["availability"]
        if g["group"] == "standard_pids"
        for i in g["items"]
        if i["status"] == "raw_only"
    ]
    if raw:
        f.conditions.append(
            "Raw bytes stored (no verified decoder, no value): "
            + ", ".join(f"{i['name']} ({i['hex']})" for i in raw)
        )
    for r in dpf["possible_regenerations"]:
        f.supporting.append(
            Evidence(
                text=r["text"],
                values={k: r[k] for k in ("t_start_rel_s", "t_end_rel_s", "indicators", "confidence")},
            )
        )
    for r in dpf["possible_interrupted_regenerations"]:
        f.supporting.append(
            Evidence(
                text=r["text"], values={"t_rel_s": r["t_rel_s"], "fan_runs_after_s": r["fan_runs_after_s"]}
            )
        )
    for u in dpf["user_suspected"]:
        f.conditions.append(f"You marked 'suspected DPF regeneration' at {u['t_rel_s']:.0f} s.")
    d = m["drive"]
    if d["running_s"]:
        f.conditions.append(
            f"Driving: engine running {d['running_s'] / 60:.1f} min, moving {100 * (d['moving_share'] or 0):.0f} % of "
            f"speed samples, max {d['max_speed_kmh']} km/h, label: {d['drive_type_user_label'] or 'none'}."
        )
    if dpf["possible_interrupted_regenerations"] and any(
        r["fan_runs_after_s"] for r in dpf["possible_interrupted_regenerations"]
    ):
        f.strength = "suggestive"
    elif not dpf["possible_regenerations"] and not dpf["limitations"]:
        f.contradicting.append(Evidence(text="No possible regeneration was inferred in this session."))
    f.next_steps = [
        "Keep recording daily drives; mark fan_running / fan_stopped after every stop and the drive type (city / highway).",
        "When a regeneration seems to be running (fan after stop, smell, raised idle), a longer drive lets it finish.",
        "Have a BMW-capable tester read soot load, regeneration history and the last regeneration status to confirm.",
    ]
    return [f]


def monitoring_cooling(ctx: AnalysisContext) -> list[Finding]:
    m = _summary(ctx)
    if m is None:
        return []
    c = m["coolant"]
    f = Finding(
        id="monitoring_cooling",
        title="Cooling system behaviour (monitoring)",
        hypothesis="Coolant temperature behaves unusually for this car (warm-up, drops after warm-up, high peaks).",
        explanation=(
            "Compares this session's coolant temperature (PID 05) with the same car's usual warm temperature, "
            "derived from earlier monitoring sessions. No fixed temperature limit is assumed for this engine."
        ),
        strength="insufficient",
        caveats=[HEURISTIC_CAVEAT, "PID 05 is the ECU's coolant reading; it says nothing about the fan."],
    )
    if c["status"] != "measured":
        f.caveats.append("Coolant temperature was not measured in this session.")
        return [f]
    f.conditions.append(
        f"Coolant: first {c['first']['value']:.0f} °C, max while running {c['max_running']} °C, running "
        f"{c['running_s'] / 60:.1f} min; usual warm temperature: "
        + (
            f"{c['usual_warm_c']} °C ({c['usual_warm_sessions']} session(s))"
            if c["usual_warm_c"] is not None
            else "unknown yet"
        )
        + (f"; ambient ~{c['ambient_c']} °C." if c["ambient_c"] is not None else ".")
    )
    f.caveats += c["limitations"]
    for d in c["detected"]:
        f.supporting.append(Evidence(text=d["text"] + " (possible; heuristic)", values=d["values"]))
    for p in c["after_shutdown"]:
        if p["after"]:
            f.conditions.append(
                f"After the engine stop at {p['t_rel_s']:.0f} s the ECU still reported coolant for {p['measured_for_s']:.0f} s "
                f"(max rise {p['max_rise_c']} °C)."
            )
    if c["detected"]:
        f.strength = "suggestive"
    elif c["usual_warm_c"] is not None:
        f.contradicting.append(
            Evidence(text="Coolant stayed within the usual range for this car in this session.")
        )
    f.next_steps = [
        "If coolant repeatedly stays low or drops while driving, have the thermostat checked.",
        "If it is repeatedly high, or you see a temperature warning, stop safely and have the cooling system inspected.",
    ]
    return [f]


def monitoring_electrical(ctx: AnalysisContext) -> list[Finding]:
    m = _summary(ctx)
    if m is None:
        return []
    v = m["voltage"]
    f = Finding(
        id="monitoring_electrical",
        title="Electrical system: observed voltage (monitoring)",
        hypothesis="Observed supply voltage is low or unstable around the battery warnings.",
        explanation=(
            "ECU-reported voltage (PID 42) and the adapter's own supply measurement (ATRV) are kept apart. "
            "Reports the start voltage, the voltage while the engine runs, the last value before each stop, the "
            "values after the stop where they could be read, large drops, and voltages around your battery-warning markers."
        ),
        strength="insufficient",
        caveats=[
            v["caveat"],
            "Modern alternator control can deliberately vary the charging voltage, so a lower running voltage is not by "
            "itself a fault. This tool does not diagnose the battery, the alternator or the battery sensor.",
            HEURISTIC_CAVEAT,
        ],
    )
    suggestive = False
    for key in ("ecu", "adapter"):
        src = v[key]
        if src["status"] != "measured":
            f.conditions.append(f"{src['provenance']}: not measured.")
            continue
        run = src["engine_running"]
        off = src["engine_off"]
        f.conditions.append(
            f"{src['provenance']}: at start {src['at_start']['value']:.2f} V"
            + (f"; engine running median {run['median']} V (min {run['min']})" if run else "")
            + (f"; engine off median {off['median']} V" if off else "")
            + "."
        )
        low = src["low_while_running"]
        if low["samples"]:
            f.supporting.append(
                Evidence(
                    text=f"{key.upper()} voltage below {VOLT_LOW_RUNNING_V} V while the engine ran in {low['samples']} sample(s) (min {low['min_v']} V).",
                    values=low,
                )
            )
            suggestive = True
        if src["no_charging_rise_seen"]:
            f.supporting.append(
                Evidence(
                    text=f"{key.upper()} voltage: no rise above the engine-off level while running (no charging rise seen)."
                )
            )
        drops = [d for d in src["drops"] if d["context"] is None]
        if drops:
            f.supporting.append(
                Evidence(
                    text=f"{key.upper()} voltage: {len(drops)} large drop(s) outside engine start/stop.",
                    values={"drops": drops[:10]},
                )
            )
        for st in src["shutdowns"]:
            if st["last_before"] or st["after"]:
                f.conditions.append(
                    f"{key.upper()} around the stop at {st['t_rel_s']:.0f} s: last before "
                    f"{st['last_before']['value'] if st['last_before'] else 'n/a'} V, after: "
                    + (
                        f"{st['after']['min']}-{st['after']['max']} V ({st['after']['n']} reads)"
                        if st["after"]
                        else "not readable"
                    )
                    + (f", trend {st['trend_v_per_h']} V/h" if st["trend_v_per_h"] is not None else "")
                    + "."
                )
    for w in v["battery_warnings"]:
        near = (
            ", ".join(f"{k.replace('_', ' ')} {x['min']}-{x['max']} V" for k, x in w["nearby"].items() if x)
            or "no voltage measured within +/-60 s"
        )
        f.supporting.append(
            Evidence(
                text=f"'{w['event']}' marked at {w['t_rel_s']:.0f} s (engine {'running' if w['engine_running'] else 'not running'}): {near}.",
                values=w,
            )
        )
        if any(x and x["min"] < VOLT_LOW_RUNNING_V for x in w["nearby"].values()):
            suggestive = True
    if suggestive:
        f.strength = "suggestive"
    f.next_steps = [
        "Mark every battery warning (and when it clears) so voltages can be compared across drives.",
        "Have the battery tested (load test) and the charging system checked by a workshop before replacing parts.",
    ]
    return [f]


def monitoring_shutdown_fan(ctx: AnalysisContext) -> list[Finding]:
    m = _summary(ctx)
    if m is None:
        return []
    fans = m["fan"]["runs"]
    after = [r for r in fans if r["after_shutdown"]]
    f = Finding(
        id="monitoring_shutdown_fan",
        title="Engine shutdown and radiator fan after-run (monitoring)",
        hypothesis="The radiator fan runs for a long time after the engine is switched off.",
        explanation=(
            "Fan activity is not readable through generic OBD; durations come only from your fan_running / "
            "fan_stopped markers. Engine stops come from measured RPM where the ECU still answered, otherwise "
            "from your engine_stopped / ignition_off markers."
        ),
        strength="suggestive" if any(r["complete"] for r in after) else "insufficient",
        caveats=[
            "Fan durations are as marked by a person; they are approximate.",
            "Losing ECU communication is not proof that the ignition was switched off.",
        ],
    )
    for st in m["shutdowns"]:
        f.conditions.append(f"Engine stop at {st['t_rel_s']:.0f} s ({st['origin']}: {st['basis']}).")
    for r in fans:
        dur = f"{r['duration_s']:.0f} s" if r["duration_s"] is not None else "fan_stopped not marked"
        where = (
            f"{r['seconds_after_shutdown']:.0f} s after a {r['shutdown_origin']} engine stop"
            if r["after_shutdown"]
            else "not after a recorded engine stop"
        )
        f.supporting.append(Evidence(text=f"Fan run marked: {dur}, starting {where}.", values=r))
    oc = m.get("observed_capabilities") or {}
    if oc:
        f.conditions.append(
            "Observed in this session (not assumed): ECU answered "
            f"{oc.get('ecu_answers_after_engine_stop', 0)} request(s) for {oc.get('ecu_answered_for_s_after_engine_stop', 0)} s "
            f"after an engine stop; adapter voltage read {oc.get('adapter_voltage_reads_while_ecu_unresponsive', 0)} time(s) "
            "while the ECU was unresponsive."
        )
    if not fans:
        f.caveats.append("No fan markers in this session.")
    f.next_steps = [
        "After every stop, mark fan_running when you hear it and fan_stopped when it stops (the session stays open).",
        "Compare fan run times between city and highway drives in the history view.",
    ]
    return [f]
