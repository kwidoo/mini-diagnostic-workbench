"""Session export: JSON, CSV, raw transcript, Markdown report, bundled as a reproducible ZIP.

The VIN is redacted unless the caller passes include_vin=True (explicit consent).
The same database state always produces byte-identical archives.
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from typing import Any

from app.acquisition.recorder import GOOD
from app.diagnostics.engine import analyze_session
from app.diagnostics.monitoring import is_monitoring_session, summary_for
from app.models.domain import Finding
from app.storage.db import Store
from app.storage.transcript import REDACTED, transcript_lines
from app.vehicles.profiles import get_profile

SAMPLE_COLUMNS = [
    "t_wall",
    "t_rel_s",
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
]


def _iso(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t)) if t else "n/a"


def samples_csv(samples: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(SAMPLE_COLUMNS)
    t0 = min((s["t_mono"] for s in samples), default=0.0)
    for s in samples:
        row = dict(s, t_rel_s=round(s["t_mono"] - t0, 4), pid=f"{s['pid']:02X}")
        w.writerow(["" if row[c] is None else row[c] for c in SAMPLE_COLUMNS])
    return buf.getvalue()


_LINK_COUNTERS = [
    ("resyncs", "resynchronisation(s) after incomplete replies"),
    ("reconnects", "reconnect(s)"),
]


def render_monitoring(m: dict[str, Any]) -> list[str]:
    """Report section for continuous-monitoring sessions (appended; other reports are unchanged)."""

    def t(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.0f} s"

    out = [
        "## Continuous monitoring",
        "",
        "Origins: **measured** (ECU / adapter value), **observed** (transition seen in measured data), "
        "**manual** (marked by a person), **inferred** (heuristic: always 'possible', never confirmed), "
        "**not available** (not offered by the ECU or manufacturer-only).",
        "",
        "### Signals",
        "",
        "| Signal | Tier | Provenance | Status | ok / raw / failed |",
        "|---|---|---|---|---|",
    ]
    for s in m["signals"]:
        out.append(
            f"| {s['name']} ({s['hex']}) | {s['tier'] or '-'} | {s['provenance']} | {s['status']} | "
            f"{s['ok']} / {s['raw']} / {s['failed']} |"
        )
    out += ["", "### Engine stops and fan", ""]
    if not m["shutdowns"]:
        out.append("- No engine stop recorded.")
    for st in m["shutdowns"]:
        out.append(f"- Engine stop at {t(st['t_rel_s'])} ({st['origin']}: {st['basis']})")
    for f in m["fan"]["runs"]:
        dur = f"{f['duration_s']:.0f} s" if f["duration_s"] is not None else "end not marked"
        rel = f", {f['seconds_after_shutdown']:.0f} s after an engine stop" if f["after_shutdown"] else ""
        out.append(f"- Fan run (manual markers) at {t(f['t_start_rel_s'])}: {dur}{rel}")
    out += ["", "### Voltage", "", f"_{m['voltage']['caveat']}_", ""]
    for key in ("ecu", "adapter"):
        v = m["voltage"][key]
        if v["status"] != "measured":
            out.append(f"- {v['provenance']}: not measured")
            continue
        run = v["engine_running"] or {}
        out.append(
            f"- {v['provenance']}: start {v['at_start']['value']} V; running median {run.get('median', 'n/a')} V "
            f"(min {run.get('min', 'n/a')}); large drops outside engine start/stop: "
            f"{sum(1 for d in v['drops'] if d['context'] is None)}"
        )
    for w in m["voltage"]["battery_warnings"]:
        out.append(f"- '{w['event']}' at {t(w['t_rel_s'])} ({w['origin']}); nearby: {w['nearby']}")
    out += ["", "### DPF", ""]
    for g in m["dpf"]["availability"]:
        if g["group"] == "wanted":
            out += [f"- {i['name']}: **{i['status'].replace('_', ' ')}** ({i['via']})" for i in g["items"]]
    for r in m["dpf"]["possible_regenerations"] + m["dpf"]["possible_interrupted_regenerations"]:
        out.append(f"- *{r['type']}* ({r['confidence']} confidence, inferred): {r['text']}")
    out += [f"- {x}" for x in m["dpf"]["limitations"]]
    out += ["", "### Link and sampling", ""]
    for e in m["link"]:
        out.append(f"- {t(e['t_rel_s'])}: {e['type']}" + (f" (gap {e['gap_s']} s)" if e["gap_s"] else ""))
    s = m["sampling"]
    out.append(
        f"- Base intervals {s['base_intervals']}; effective at the end {s['final_effective_intervals']}; "
        f"{len(s['adjustments'])} adjustment(s) to adapter throughput."
    )
    oc = m.get("observed_capabilities")
    if oc:
        out.append(f"- Observed in this session (not assumed): {oc}")
    if m.get("air_path"):
        out += render_air_path(m["air_path"])
    return out + [""]


def render_air_path(ap: dict[str, Any]) -> list[str]:
    """Air-path subsection of the monitoring report: statistics, evidence, limitations."""

    def t(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.0f} s"

    def na(x: Any) -> str:
        return "n/a" if x is None else str(x)

    out = [
        "",
        "### Air path (MAF / EGR / MAP / DTC)",
        "",
        "Measured: values decoded from ECU answers. Inferred: running state, episodes, dips, excursions, "
        "associations and rates (screening heuristics, listed under `heuristics` in monitoring.json; not BMW / MINI "
        "specifications). Not available: BMW DDE fault memory and MAF / EGR / boost targets.",
        "",
        "| Signal | Status | Samples | Failed reads |",
        "|---|---|---|---|",
    ]
    for s in ap["signals"].values():
        out.append(
            f"| {s['name']} | {s['status'].replace('_', ' ')} | {s['samples']} | {s['failed_reads']} |"
        )
    maf = ap["maf"]
    out.append("")
    if ap["signals"]["maf"]["status"] == "measured":
        rs = maf["running_state"]
        out += [
            f"- MAF samples: {maf['samples_ok']} with a value; engine confirmed running for {rs['running_confirmed']}, "
            f"not running for {rs['engine_not_running']}, unknown for {rs['unknown']}; "
            f"{maf['failed_reads']['n']} failed read(s) (no value, never counted as zero).",
            f"- While running: {maf['zero_running']} sample(s) at 0.00 g/s, {maf['low_running']} below the screening "
            f"value (per 1000 running samples: {na(maf['zero_per_1k_running'])} / {na(maf['low_per_1k_running'])}). "
            f"Zero with the engine measured off: {maf['zero_engine_not_running']} (expected, not counted).",
            f"- Episodes: {maf['episode_count']} ({maf['persistent_episodes']} persistent, {maf['transient_episodes']} "
            f"single transient, {maf['repeated_clusters']} repeated cluster(s)), total {maf['episode_total_s']} s; "
            f"abrupt dips with recovery: {len(maf['dips'])}.",
            "- Cold / warm (coolant split at the screening value): "
            + "; ".join(
                f"{k}: {g['low']} low of {g['running_samples']} running samples, {g['episodes']} episode(s)"
                for k, g in maf["by_temperature"].items()
                if g["running_samples"] or g["episodes"]
            ),
            "- Idle / moving: "
            + "; ".join(
                f"{k}: {g['low']} low of {g['running_samples']}, {g['episodes']} episode(s)"
                for k, g in maf["by_motion"].items()
                if g["running_samples"] or g["episodes"]
            ),
        ]
        for e in maf["episodes"][:30]:
            ev = e["evidence"][0] if e["evidence"] else {}
            out.append(
                f"  - {t(e['t_start_rel_s'])}: *{e['type']}* ({e['confidence']} confidence, inferred), {e['samples']} "
                f"sample(s) / {e['duration_s']} s, min {e['min_maf_gs']} g/s, ECU {e['ecu']}, raw answer "
                f"`{ev.get('response')}`; {e['motion']}, {e['temperature_state']}; RPM before "
                f"{(e['rpm']['first']['rpm_before'] or {}).get('value')} / after "
                f"{(e['rpm']['first']['rpm_after'] or {}).get('value')}"
            )
        if len(maf["episodes"]) > 30:
            out.append(f"  - ... {len(maf['episodes']) - 30} more in monitoring.json")
    else:
        out.append(f"- MAF: {ap['signals']['maf']['status'].replace('_', ' ')} (no statistics; not zero).")
    egr = ap["egr_error"]
    if egr["count"] is not None:
        out.append(
            f"- EGR error (2D) excursions (|error| >= {egr['screen_pct']:g} %): {egr['count']} "
            f"({na(egr['per_running_h'])} per running hour), max |error| {na(egr['max_abs_pct'])} %; "
            f"by motion {egr['by_motion']}, by load {egr['by_load']}."
        )
    corr = ap["correlations"]
    out += ["", "**Associations (in time, not causation)**", ""]
    out += [f"- supporting: {x}" for x in corr["supporting"]] or ["- supporting: none"]
    out += [f"- contradicting: {x}" for x in corr["contradicting"]] or ["- contradicting: none"]
    dt = ap["dtcs"]
    out += ["", "**Fault codes**", ""]
    if not dt["scans"]:
        out.append("- No fault scan in this session: DTC status unknown.")
    for b in dt["by_code"]:
        desc = b["description"] or "description not verified for this ECU variant"
        out.append(
            f"- {b['code']} (generic OBD, ECU {', '.join(str(e) for e in b['ecus'])}, {', '.join(b['statuses'])}, "
            f"{', '.join(b['scan_phases'])} scan): {desc}. {b['note']} Timing: {b['timing']}."
        )
    if dt["scans"] and not dt["codes"]:
        out.append("- No generic DTC reported in this session's scans.")
    out.append(f"- MIL: {'ON' if dt['mil_on'] else 'off' if dt['mil_on'] is False else 'unknown'}")
    if dt["owner_reported_manufacturer_codes"]:
        out.append(
            "- Owner-reported BMW codes (not read by this tool): "
            + ", ".join(c["code"] for c in dt["owner_reported_manufacturer_codes"])
            + ". "
            + dt["owner_reported_note"]
        )
    hist = ap.get("history")
    if hist:
        out += ["", "**Compared with this vehicle's other sessions**", ""]
        out.append(
            f"- {hist['note']} Other sessions: {hist['other_sessions']}; medians: {hist['medians'] or 'n/a'}"
        )
    out += ["", "**Limitations**", ""] + [f"- {x}" for x in ap["limitations"]]
    return out


def render_dde_faults(sc: Any) -> list[str]:
    """DDE-04 lines for one scan: measured BMW DDE fault memory, kept apart from generic DTCs."""
    m = getattr(sc, "manufacturer", None)
    if m is None:
        return []
    tag = " [SYNTHETIC]" if m.synthetic else ""
    head = f"- BMW DDE fault memory{tag}: **{m.status.replace('_', ' ')}**"
    if m.status in ("read", "partial", "no_faults"):
        head += f" (ECU {m.ecu}, {m.request_id}, catalog {m.catalog_version}, decoder {m.decoder_version})"
    elif m.detail:
        head += f" - {m.detail}"
    lines = [head]
    lines += [
        f"  - {f.code} (manufacturer code as returned, raw {f.raw_hex}, status byte {f.status_raw}"
        + (
            f": {', '.join(k for k, v in (f.status or {}).items() if v) or 'no status bit set'}"
            if f.status
            else ""
        )
        + ")"
        for f in m.faults
    ]
    return lines


def dde_faults_csv(scans: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        ["scan_phase", "scan_t_wall", "read_status", "synthetic", "code", "raw_hex", "status_raw", "status",
         "ecu", "request_id", "catalog_version", "decoder_version", "detail"]
    )  # fmt: skip
    for sc in scans:
        m = sc.manufacturer
        if m is None:
            continue
        common = [
            m.ecu or "",
            m.request_id or "",
            m.catalog_version or "",
            m.decoder_version or "",
            m.detail or "",
        ]
        if not m.faults:
            w.writerow([sc.phase, sc.t_wall, m.status, m.synthetic, "", "", "", "", *common])
        for f in m.faults:
            st = json.dumps(f.status, sort_keys=True) if f.status else ""
            w.writerow(
                [
                    sc.phase,
                    sc.t_wall,
                    m.status,
                    m.synthetic,
                    f.code,
                    f.raw_hex,
                    f.status_raw or "",
                    st,
                    *common,
                ]
            )
    return buf.getvalue()


def render_report(
    session: dict[str, Any],
    scans: list,
    annotations: list,
    findings: list[Finding],
    monitoring: dict[str, Any] | None = None,
) -> str:
    prof = get_profile(session["vehicle_profile"])
    meta = session["meta"]
    out: list[str] = [f"# Diagnostic session report: {session['id']}", ""]
    if session["synthetic"]:
        out += [
            "> **SYNTHETIC DATA.** This session comes from the simulator or a synthetic fixture. It is not a measurement from a real vehicle.",
            "",
        ]
    out += [
        "Read-only investigation aid. It reads generic OBD data and never clears faults, resets adaptations, "
        "actuates components or triggers a DPF regeneration. It does not certify roadworthiness.",
        "",
        "## Session",
        "",
        f"- Vehicle profile: {prof.name}",
        f"- Identity: {prof.identity_note}",
        f"- Kind: {session['kind']} | source: {session['source']} | label: {session['label'] or '-'}",
        f"- Started: {_iso(session['started_at'])} | ended: {_iso(session['ended_at'])}",
        f"- Adapter: {(meta.get('adapter') or {}).get('identity') or 'unknown'} | protocol: {(meta.get('adapter') or {}).get('protocol_name') or 'unknown'}",
    ]
    tr = meta.get("transport")
    if tr:
        parts = [f"{tr[k]} {label}" for k, label in _LINK_COUNTERS if tr.get(k) is not None]
        parts.append(f"state {tr.get('state')}")
        if tr.get("last_error"):
            parts.append(f"last error: {tr['last_error']}")
        out += ["- Adapter link at end of session: " + ", ".join(parts)]
    vi = meta.get("vehicle_info")
    if vi:
        cal = ", ".join(c["text"] for c in vi.get("calibration_ids", [])) or "not reported"
        out += [f"- VIN: {vi.get('vin') or vi.get('vin_status')} | calibration ID(s): {cal}"]
    if meta.get("aborted"):
        out += [f"- **Session ended early:** {meta['aborted']}"]
    cap = meta.get("dde_capability")
    if cap:
        v = cap["verdicts"]
        out += [
            "",
            "## DDE capability",
            "",
            f"- Generic OBD: {v['generic_obd']['verdict']}",
            f"- Manufacturer transport: {v['manufacturer_transport']['verdict']} "
            f"({v['manufacturer_transport']['meaning']})",
            f"- Manufacturer-specific requests sent: {cap['dde']['manufacturer_requests_sent']['value']}",
            "- Details: dde_capability.md / dde_capability.json",
        ]
    out += ["", "## Fault scans", ""]
    if not scans:
        out += ["No fault scan in this session.", ""]
    for sc in scans:
        mil = "unknown" if sc.mil_on is None else ("ON" if sc.mil_on else "off")
        out += [f"### {sc.phase} scan at {_iso(sc.t_wall)} (MIL {mil})", ""]
        for kind, st in sc.mode_status.items():
            codes = [f"{d.code} (raw {d.raw_hex}, ECU {d.ecu})" for d in sc.dtcs if d.kind == kind]
            state = ", ".join(codes) if codes else ("none" if st == "supported" else f"not available ({st})")
            out += [f"- {kind}: {state}"]
        ff = sc.freeze_frame
        if ff:
            out += [f"- freeze frame: {ff.status}" + (f" for {ff.dtc}" if ff.dtc else "")]
            out += [
                f"  - {r.name}: {r.value} {r.unit or ''}".rstrip() for r in ff.readings if r.value is not None
            ]
        out += render_dde_faults(sc)
        out += [""]
    metrics = meta.get("metrics") or []
    if metrics:
        out += [
            "## Sampling quality",
            "",
            "| PID | Signal | actual Hz | ok / attempts | dropped | latency mean / p95 / max (ms) | max gap (s) |",
            "|---|---|---|---|---|---|---|",
        ]
        for m in metrics:
            out += [
                f"| {m['pid']:02X} | {m['name']} | {m['actual_hz']} | {m['ok']} / {m['attempts']} | {m['dropped']} | {m['latency_ms_mean']} / {m['latency_ms_p95']} / {m['latency_ms_max']} | {m['max_gap_s']} |"
            ]
        out += [""]
    missing = (meta.get("selected") or {}).get("unsupported") or []
    if missing:
        from app.parameters.standard_pids import pid_name

        out += [
            "Requested but **not supported** by this ECU (not polled, no values): "
            + ", ".join(f"{pid_name(p)} ({p:02X})" for p in missing),
            "",
        ]
    if annotations:
        out += (
            ["## Event markers", ""]
            + [
                f"- {_iso(a.t_wall)}: {a.label}"
                + (f" [{a.origin}]" if a.origin != "manual" else "")
                + (f" - {a.note}" if a.note else "")
                for a in annotations
            ]
            + [""]
        )
    if monitoring is not None:
        out += render_monitoring(monitoring)
    out += [
        "## Findings",
        "",
        "Evidence strength is categorical (insufficient / suggestive / strong). It is not a probability.",
        "",
    ]
    for f in findings:
        out += [
            f"### {f.title}",
            "",
            f"- **Hypothesis:** {f.hypothesis}",
            f"- **Evidence strength:** {f.strength}",
            "",
            f.explanation,
            "",
        ]
        for title, items in (
            ("Supporting", [e.text for e in f.supporting]),
            ("Contradicting", [e.text for e in f.contradicting]),
            ("Conditions and measured context", f.conditions),
            ("Caveats", f.caveats),
            ("Next safe steps", f.next_steps),
        ):
            if items:
                out += [f"**{title}**", ""] + [f"- {i}" for i in items] + [""]
    return "\n".join(out) + "\n"


def build_files(store: Store, sid: str, include_vin: bool = False) -> dict[str, str]:
    session = store.get_session(sid)
    if session is None:
        raise KeyError(f"no session {sid}")
    samples, scans, annotations = store.samples(sid), store.scans(sid), store.annotations(sid)
    findings = analyze_session(store, sid)
    vin = (session["meta"].get("vehicle_info") or {}).get("vin")
    dump = lambda o: json.dumps(o, indent=2, sort_keys=True, ensure_ascii=False) + "\n"  # noqa: E731

    monitoring = summary_for(store, sid) if is_monitoring_session(session) else None
    ann_buf = io.StringIO()
    w = csv.writer(ann_buf, lineterminator="\n")
    # the original four columns first; event columns only when a session has typed events
    typed = any(a.event_type or a.origin != "manual" for a in annotations)
    w.writerow(
        ["t_wall", "t_mono", "label", "note"] + (["event_type", "origin", "confidence"] if typed else [])
    )
    for a in annotations:
        row = [a.t_wall, "" if a.t_mono is None else a.t_mono, a.label, a.note]
        w.writerow(row + ([a.event_type or "", a.origin, a.confidence or ""] if typed else []))

    files = {
        "README.txt": (
            f"mini-obd export of session {sid}\n"
            f"synthetic data: {'YES - simulator/fixture, not a real vehicle' if session['synthetic'] else 'no'}\n"
            f"VIN: {'included with explicit consent' if include_vin else 'redacted'}\n\n"
            "session.json     session metadata, capabilities, sampling metrics\n"
            "samples.csv/json normalized readings (one row per ECU answer or failed read; nothing interpolated)\n"
            "dtcs.json        fault scans with raw DTC bytes and freeze frames\n"
            "annotations.csv  event markers\n"
            "transcript.jsonl raw adapter trace (replayable with: mini-obd replay transcript.jsonl)\n"
            "findings.json    evidence-based findings\n"
            "report.md        human-readable report\n"
        ),
        "session.json": dump(session),
        "samples.csv": samples_csv(samples),
        "samples.json": dump(samples),
        "dtcs.json": dump([s.model_dump() for s in scans]),
        "annotations.csv": ann_buf.getvalue(),
        "transcript.jsonl": "\n".join(transcript_lines(store, sid, include_vin)) + "\n",
        "findings.json": dump([f.model_dump() for f in findings]),
        "report.md": render_report(session, scans, annotations, findings, monitoring),
    }
    if monitoring is not None:
        prof = get_profile(session["vehicle_profile"])
        meta = session["meta"]
        files["README.txt"] += (
            "\ncontinuous-monitoring files:\n"
            "monitoring.json        full monitoring summary (shutdowns, fan runs, voltage, coolant, DPF, link, air path)\n"
            "events.jsonl           every timeline event: manual, observed and link events with origin/evidence\n"
            "detected_events.json   inferred diagnostic events (always 'possible', never confirmed)\n"
            "sampling_quality.json  tiers, effective intervals, adjustments and per-signal rates\n"
            "failed_samples.jsonl   every failed read attempt (no value, reason kept)\n"
            "capabilities.json      adapter, ECU capabilities, requested/unsupported signals\n"
            "vehicle_profile.json   the vehicle profile the session was recorded with\n"
        )
        files.update(
            {
                "monitoring.json": dump(monitoring),
                "events.jsonl": "".join(
                    json.dumps(a.model_dump(), sort_keys=True, ensure_ascii=False) + "\n" for a in annotations
                ),
                "detected_events.json": dump(monitoring["detected_events"]),
                "sampling_quality.json": dump(
                    {**monitoring["sampling"], "signal_status": monitoring["signals"]}
                ),
                "failed_samples.jsonl": "".join(
                    json.dumps(s_, sort_keys=True) + "\n" for s_ in samples if s_["quality"] not in GOOD
                ),
                "capabilities.json": dump(
                    {
                        "adapter": meta.get("adapter"),
                        "capabilities": meta.get("capabilities"),
                        "selected": meta.get("selected"),
                        "sampling_plan": meta.get("sampling_plan"),
                    }
                ),
                "vehicle_profile.json": dump(prof.to_dict()),
            }
        )
    # the CSV only when a read was actually attempted; "unavailable" is still in dtcs.json / report.md
    if any(
        sc.manufacturer is not None and sc.manufacturer.status not in ("unavailable", "not_attempted")
        for sc in scans
    ):
        files["README.txt"] += (
            "\nBMW DDE fault memory (kept separate from generic DTCs; codes exactly as returned):\n"
            "dde_faults.csv         one row per manufacturer fault, or one status row per read attempt\n"
        )
        files["dde_faults.csv"] = dde_faults_csv(scans)
    cap = session["meta"].get("dde_capability")
    if cap:
        from app.manufacturer.bmw.capability import render_markdown

        files["README.txt"] += (
            "\nDDE capability assessment (generic OBD + adapter identity only; no manufacturer request):\n"
            "dde_capability.json    observed / claimed / unknown fields with sources and verdicts\n"
            "dde_capability.md      the same as a table for review\n"
        )
        files["dde_capability.json"] = dump(cap)
        files["dde_capability.md"] = render_markdown(cap)
    if vin and not include_vin:
        files = {k: v.replace(vin, REDACTED) for k, v in files.items()}
    return files


def build_zip(store: Store, sid: str, include_vin: bool = False) -> bytes:
    files = build_files(store, sid, include_vin)
    session = store.get_session(sid)
    stamp = time.gmtime(max(session["started_at"], 315532800))[:6]  # type: ignore[index]  # ZIP epoch is 1980
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"mini-obd-{sid}/{name}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name].encode("utf-8"), compresslevel=6)
    return buf.getvalue()
