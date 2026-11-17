"""mini-obd command line. Read-only: there is no command that sends arbitrary adapter requests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app import config
from app.adapters.base import TransportError
from app.adapters.simulator import SCENARIOS
from app.adapters.tcp import DEFAULT_HOST, DEFAULT_PORT
from app.diagnostics.engine import analyze_session
from app.obd.client import ObdError
from app.parameters.standard_pids import PIDS, RAW_ONLY_PIDS, pid_name
from app.service import BusyError, NotConnectedError, Workbench
from app.storage.db import Store
from app.storage.export import build_files, build_zip
from app.storage.transcript import ingest_file
from app.vehicles.profiles import PROFILES

SAFETY = (
    "READ-ONLY tool. Test with the vehicle stationary, parked safely, outdoors or well ventilated.\n"
    "Stop and seek professional inspection if the engine runs severely irregularly, you hear metallic knocking,\n"
    "it overheats, you smell exhaust or coolant, or the MIL flashes. This tool does not certify roadworthiness."
)


def _connection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default=DEFAULT_HOST, help=f"adapter IP (default {DEFAULT_HOST})")
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"adapter TCP port (default {DEFAULT_PORT})"
    )
    p.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    p.add_argument(
        "--min-interval", type=float, default=0.0, help="minimum seconds between requests (rate limit)"
    )
    p.add_argument(
        "--simulator",
        metavar="SCENARIO",
        nargs="?",
        const="healthy_idle",
        help="use the built-in fake ELM327 instead of hardware: " + ", ".join(SCENARIOS),
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mini-obd", description="MINI Diagnostic Workbench (read-only OBD investigation tool)"
    )
    ap.add_argument(
        "--db", default=None, help="SQLite path (default ~/.mini-obd/workbench.sqlite3 or $MINI_OBD_DB)"
    )
    ap.add_argument(
        "--vehicle", default=None, choices=list(PROFILES), help="vehicle profile for this command"
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("profiles", help="list vehicle profiles, or select the active one")
    p.add_argument("--use", choices=list(PROFILES))
    p = sub.add_parser("probe", help="connect, initialise the adapter, report protocol and ECUs")
    _connection_args(p)
    p = sub.add_parser("scan", help="read stored/pending/permanent DTCs and freeze frame (clears nothing)")
    _connection_args(p)
    p.add_argument(
        "--read-vin",
        action="store_true",
        help="also read the VIN (stored locally; redacted in exports by default)",
    )
    p.add_argument("--no-freeze-frame", action="store_true")
    p.add_argument("--label", default="")
    p = sub.add_parser("inventory", help="list supported PIDs and read each once")
    _connection_args(p)
    p.add_argument("--read-vin", action="store_true")
    p.add_argument("--label", default="")
    p = sub.add_parser("record", help="record selected signals for a fixed duration")
    _connection_args(p)
    p.add_argument("--duration", type=float, default=120.0)
    p.add_argument(
        "--profile", default="idle", help="recording profile of the active vehicle (idle, warmup, road)"
    )
    p.add_argument(
        "--pids", default=None, help="explicit comma-separated hex PIDs, e.g. 0C,10,0B (overrides --profile)"
    )
    p.add_argument("--label", default="")
    p.add_argument("--no-pre-scan", action="store_true")
    p.add_argument("--no-post-scan", action="store_true")
    p.add_argument(
        "--freeze-frame", action="store_true", help="include freeze-frame reads in the pre/post scans"
    )
    p = sub.add_parser(
        "replay", help="import a transcript (fixture or exported transcript.jsonl) as a session"
    )
    p.add_argument("file")
    p = sub.add_parser("sessions", help="list stored sessions")
    p = sub.add_parser("analyze", help="evidence-based findings for a session")
    p.add_argument("--session", default="latest")
    p = sub.add_parser("annotate", help="add an event marker to a session")
    p.add_argument("--session", default="latest")
    p.add_argument("--label", required=True)
    p.add_argument("--note", default="")
    p = sub.add_parser("export", help="export a session")
    p.add_argument("--session", default="latest")
    p.add_argument("--format", choices=["zip", "json", "csv", "md", "jsonl"], default="zip")
    p.add_argument("--out", default=None)
    p.add_argument(
        "--include-vin", action="store_true", help="explicit consent to include the VIN in the export"
    )
    p = sub.add_parser("serve", help="run the local API + dashboard")
    p.add_argument("--host", default=config.API_HOST, help="bind address (default 127.0.0.1; do not expose)")
    p.add_argument("--port", type=int, default=config.API_PORT)
    p = sub.add_parser("simulate", help="run the fake ELM327 TCP server (synthetic data)")
    p.add_argument("--scenario", default="rough_idle", choices=list(SCENARIOS))
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("fixtures", help="regenerate the synthetic fixtures")
    p.add_argument("--out", default=str(config.fixtures_dir()))
    p = sub.add_parser("demo", help="offline end-to-end demo against the simulator")
    p.add_argument("--duration", type=float, default=45.0)
    return ap


def _connect(wb: Workbench, a: argparse.Namespace) -> None:
    if a.simulator:
        wb.connect("simulator", scenario=a.simulator, timeout_s=a.timeout, min_interval_s=a.min_interval)
        print(f"[simulator] SYNTHETIC scenario '{a.simulator}': not real vehicle data", file=sys.stderr)
    else:
        print(f"connecting to {a.host}:{a.port} ...", file=sys.stderr)
        wb.connect("real", a.host, a.port, timeout_s=a.timeout, min_interval_s=a.min_interval)


def _resolve(store: Store, ref: str) -> str:
    if ref != "latest":
        if store.get_session(ref) is None:
            raise KeyError(f"no session {ref!r}")
        return ref
    sessions = store.list_sessions(order="rowid")
    if not sessions:
        raise KeyError("no sessions stored yet")
    return sessions[-1]["id"]


def _print_status(st: dict) -> None:
    ad, caps = st["adapter"], st["capabilities"]
    print(f"adapter      : {ad['identity']} ({ad['device_description']})")
    print(f"adapter volts: {ad['voltage']} (measured by the adapter)")
    print(f"protocol     : {ad['protocol_name']} [{ad['protocol_number']}]")
    for e in caps["ecus"]:
        mark = " (primary)" if e["ecu"] == caps["primary_ecu"] else ""
        print(
            f"ECU {e['ecu']}{mark}: {len(e['mode01_pids'])} live PIDs, {len(e['mode02_pids'])} freeze-frame PIDs"
        )
    print("services     : " + ", ".join(f"{m}={s}" for m, s in caps["modes"].items()))


def _print_scans(store: Store, sid: str) -> None:
    for sc in store.scans(sid):
        mil = "unknown" if sc.mil_on is None else "ON" if sc.mil_on else "off"
        print(f"[{sc.phase}] MIL {mil}; ECU-reported emission DTC count: {sc.dtc_count_reported}")
        for kind, st in sc.mode_status.items():
            codes = [f"{d.code} [raw {d.raw_hex}, ECU {d.ecu}]" for d in sc.dtcs if d.kind == kind]
            print(
                f"  {kind:<9}: "
                + (", ".join(codes) if codes else "none" if st == "supported" else f"NOT AVAILABLE ({st})")
            )
        ff = sc.freeze_frame
        if ff:
            print(f"  freeze frame: {ff.status}" + (f" for {ff.dtc}" if ff.dtc else ""))
            for r in ff.readings:
                if r.value is not None:
                    print(f"    {r.name}: {r.value} {r.unit or ''}")


def _print_findings(findings) -> None:
    for f in findings:
        print(f"\n## {f.title}  [evidence: {f.strength}]")
        print(f"   hypothesis: {f.hypothesis}")
        for title, items in (
            ("supporting", [e.text for e in f.supporting]),
            ("contradicting", [e.text for e in f.contradicting]),
            ("context", f.conditions),
            ("caveats", f.caveats),
            ("next safe steps", f.next_steps),
        ):
            for i in items:
                print(f"   {title:<15}: {i}")


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    store = Store(a.db or config.db_path())
    wb = Workbench(store)
    if a.vehicle:
        wb.set_profile(a.vehicle)
    try:
        return _run(a, store, wb)
    except (
        TransportError,
        ObdError,
        NotConnectedError,
        BusyError,
        KeyError,
        ValueError,
        FileNotFoundError,
    ) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {msg}", file=sys.stderr)
        return 2
    finally:
        wb.disconnect()
        store.close()


def _run(a: argparse.Namespace, store: Store, wb: Workbench) -> int:
    if a.cmd == "profiles":
        if a.use:
            wb.set_profile(a.use)
        if a.json:
            print(
                json.dumps(
                    {"active": wb.profile.id, "profiles": [p.to_dict() for p in PROFILES.values()]}, indent=2
                )
            )
        else:
            for p in PROFILES.values():
                print(f"{'*' if p.id == wb.profile.id else ' '} {p.id:<30} {p.name}  [{p.status}]")
        return 0

    if a.cmd == "probe":
        _connect(wb, a)
        st = wb.status()
        print(json.dumps(st, indent=2)) if a.json else _print_status(st)
        fam = "can" if st["adapter"]["is_can"] else "kline"
        if fam not in wb.profile.expected_protocol_families:
            print(
                f"note: profile '{wb.profile.name}' expected {wb.profile.expected_protocol_families}, adapter reports {fam}."
            )
        return 0

    if a.cmd == "scan":
        print(SAFETY, file=sys.stderr)
        _connect(wb, a)
        sid = wb.scan(a.label, a.read_vin, not a.no_freeze_frame)
        if a.json:
            print(json.dumps({"session": sid, "scans": [s.model_dump() for s in store.scans(sid)]}, indent=2))
        else:
            print(f"session {sid}")
            _print_scans(store, sid)
            vi = store.get_session(sid)["meta"]["vehicle_info"]
            print(
                f"  VIN: {vi['vin'] or vi['vin_status']}; calibration: {', '.join(c['text'] for c in vi['calibration_ids']) or 'not reported'}"
            )
        return 0

    if a.cmd == "inventory":
        _connect(wb, a)
        sid = wb.inventory(a.label, a.read_vin)
        rows = store.samples(sid)
        if a.json:
            print(json.dumps({"session": sid, "readings": rows}, indent=2))
            return 0
        print(f"session {sid}")
        _print_status(wb.status())
        print("\nsupported PIDs (as reported by the ECUs):")
        for r in rows:
            val = (
                f"{r['value']} {r['unit'] or ''}"
                if r["value"] is not None and r["quality"] == "ok"
                else f"[{r['quality']}] raw {r['raw_hex']}"
            )
            print(f"  {r['pid']:02X} {r['ecu'] or '-':<8} {r['name']:<52} {r['text'] or val}")
        supported = {r["pid"] for r in rows}
        missing = [p for p in wb.profile.priority_pids if p not in supported]
        print("\npriority signals NOT SUPPORTED by this ECU via generic OBD (no value exists):")
        for p in missing:
            print(f"  {p:02X} {pid_name(p)}")
        raw = sorted(p for p in supported if p in RAW_ONLY_PIDS)
        if raw:
            print(
                "\nsupported but raw-only (no verified decoder in this tool): "
                + ", ".join(f"{p:02X} {RAW_ONLY_PIDS[p]}" for p in raw)
            )
        print("\nmanufacturer-specific parameters: UNAVAILABLE through this tool")
        for ext in wb.registry.status(wb.profile.ecu_ids):
            for prm in ext["parameters"]:
                print(f"  - {prm['name']}: {prm['status']}")
        return 0

    if a.cmd == "record":
        print(SAFETY, file=sys.stderr)
        _connect(wb, a)
        pids = [int(x, 16) for x in a.pids.split(",")] if a.pids else None
        plan = wb.plan_recording(a.profile, pids)
        print(
            "recording: "
            + ", ".join(
                f"{p:02X} {PIDS[p].name if p in PIDS else pid_name(p)}" for p in plan["fast"] + plan["slow"]
            ),
            file=sys.stderr,
        )
        if plan["unsupported"]:
            print(
                "not supported, skipped: " + ", ".join(f"{p:02X} {pid_name(p)}" for p in plan["unsupported"]),
                file=sys.stderr,
            )
        try:
            sid = wb.record(
                a.duration, a.profile, pids, a.label, not a.no_pre_scan, not a.no_post_scan, a.freeze_frame
            )
        except KeyboardInterrupt:
            wb.stop_recording()
            raise
        s = store.get_session(sid)
        if a.json:
            print(
                json.dumps(
                    {"session": sid, "metrics": s["meta"]["metrics"], "aborted": s["meta"].get("aborted")},
                    indent=2,
                )
            )
            return 0
        print(
            f"session {sid}"
            + (f"  (ended early: {s['meta']['aborted']})" if s["meta"].get("aborted") else "")
        )
        print(f"  {'PID':<4}{'signal':<50}{'Hz':>7}{'ok':>6}{'drop':>6}{'lat ms':>8}{'max gap s':>11}")
        for m in s["meta"]["metrics"]:
            print(
                f"  {m['pid']:02X}  {m['name']:<50}{m['actual_hz']:>7}{m['ok']:>6}{m['dropped']:>6}{m['latency_ms_mean'] or 0:>8}{m['max_gap_s'] or 0:>11}"
            )
        return 0

    if a.cmd == "replay":
        sid = ingest_file(a.file, store)
        s = store.get_session(sid)
        tag = "SYNTHETIC " if s["synthetic"] else ""
        print(
            f"session {sid}: {tag}{s['kind']} for {s['vehicle_profile']}, {len(store.samples(sid))} samples, {len(store.scans(sid))} scans"
        )
        return 0

    if a.cmd == "sessions":
        rows = store.list_sessions(order="rowid")
        if a.json:
            print(json.dumps(rows, indent=2))
        for s in [] if a.json else rows:
            print(
                f"{s['id']}  {s['kind']:<8} {s['source']:<9} {'SYNTHETIC' if s['synthetic'] else 'real':<9} {s['vehicle_profile']:<28} {s['label']}"
            )
        return 0

    if a.cmd == "analyze":
        sid = _resolve(store, a.session)
        findings = analyze_session(store, sid)
        if a.json:
            print(json.dumps([f.model_dump() for f in findings], indent=2))
        else:
            s = store.get_session(sid)
            print(f"session {sid}" + ("  [SYNTHETIC DATA]" if s["synthetic"] else ""))
            _print_findings(findings)
        return 0

    if a.cmd == "annotate":
        sid = _resolve(store, a.session)
        wb.annotate(sid, a.label, a.note)
        print(f"marker added to {sid}")
        return 0

    if a.cmd == "export":
        sid = _resolve(store, a.session)
        if a.format == "zip":
            out = Path(a.out or f"mini-obd-{sid}.zip")
            out.write_bytes(build_zip(store, sid, a.include_vin))
        else:
            name = {
                "json": "session.json",
                "csv": "samples.csv",
                "md": "report.md",
                "jsonl": "transcript.jsonl",
            }[a.format]
            out = Path(a.out or f"mini-obd-{sid}-{name}")
            out.write_text(build_files(store, sid, a.include_vin)[name], encoding="utf-8")
        print(f"wrote {out}  (VIN {'included' if a.include_vin else 'redacted'})")
        return 0

    if a.cmd == "serve":
        import uvicorn

        from app.api.main import create_app

        store.close()
        print(f"dashboard: http://{a.host}:{a.port}/   (local only; read-only tool)")
        uvicorn.run(create_app(a.db or config.db_path()), host=a.host, port=a.port, log_level="info")
        return 0

    if a.cmd == "simulate":
        import time

        from app.adapters.simulator import FakeElmServer

        srv = FakeElmServer(a.scenario, port=a.port).start()
        print(f"SYNTHETIC fake ELM327 '{a.scenario}' on {srv.host}:{srv.port} (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
        return 0

    if a.cmd == "fixtures":
        from app.fixtures_gen import generate

        for path in generate(a.out):
            print(f"wrote {path}")
        return 0

    if a.cmd == "demo":
        print("== offline demo: everything below is SYNTHETIC simulator data ==")
        wb.set_profile("mini-countryman-r60-diesel")
        wb.connect("simulator", scenario="dpf_history")
        _print_status(wb.status())
        print("\n-- fault scan (scenario dpf_history) --")
        _print_scans(store, wb.scan("demo scan"))
        print(f"\n-- recording {a.duration:.0f} s (scenario rough_idle) --")
        wb.connect("simulator", scenario="rough_idle")
        sid = wb.record(a.duration, "idle", label="demo rough idle")
        for m in store.get_session(sid)["meta"]["metrics"]:
            print(f"  {m['pid']:02X} {m['name']:<48} {m['actual_hz']:>6} Hz  ok {m['ok']}/{m['attempts']}")
        print("\n-- findings --")
        _print_findings(analyze_session(store, sid))
        out = Path(f"mini-obd-{sid}.zip")
        out.write_bytes(build_zip(store, sid))
        print(f"\nexported {out}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
