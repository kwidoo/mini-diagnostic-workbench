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

from app.diagnostics.engine import analyze_session
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


def render_report(session: dict[str, Any], scans: list, annotations: list, findings: list[Finding]) -> str:
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
            + [f"- {_iso(a.t_wall)}: {a.label}" + (f" - {a.note}" if a.note else "") for a in annotations]
            + [""]
        )
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

    ann_buf = io.StringIO()
    w = csv.writer(ann_buf, lineterminator="\n")
    w.writerow(["t_wall", "t_mono", "label", "note"])
    for a in annotations:
        w.writerow([a.t_wall, "" if a.t_mono is None else a.t_mono, a.label, a.note])

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
        "report.md": render_report(session, scans, annotations, findings),
    }
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
