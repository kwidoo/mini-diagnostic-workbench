"""mini-obd command line. Read-only: there is no command that sends arbitrary adapter requests."""

from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
import time
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
from app.vehicles.profiles import MONITORING_EVENT_IDS, PROFILES

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
    p = sub.add_parser(
        "dde-capability",
        help="DDE-01: assess adapter + engine ECU identity for BMW DDE access (generic OBD only; "
        "no manufacturer request). With --session: assess a stored session offline, read-only",
    )
    _connection_args(p)
    p.add_argument("--read-vin", action="store_true", help="also read the VIN (never shown in the report)")
    p.add_argument("--label", default="")
    p.add_argument(
        "--session",
        default=None,
        help="assess a stored session instead of connecting (id or 'latest'); nothing is changed",
    )
    p.add_argument("--out", default=None, help="also write the report: *.json or *.md")
    sub.add_parser(
        "dde-catalog", help="DDE-02: BMW DDE request catalog - per metric executable / blocked and why"
    )
    p = sub.add_parser(
        "dde-settings",
        help="DDE-03: opt in / out of gated BMW DDE reads and record the DDE variant you confirmed "
        "(default: opted out). Shows what is executable and why not",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--opt-in", action="store_true", help="allow manufacturer reads that pass every gate")
    g.add_argument("--opt-out", action="store_true")
    g2 = p.add_mutually_exclusive_group()
    g2.add_argument("--variant", default=None, help="DDE variant as confirmed by a BMW-capable tester")
    g2.add_argument("--clear-variant", action="store_true")
    p = sub.add_parser("inventory", help="list supported PIDs and read each once")
    _connection_args(p)
    p.add_argument("--read-vin", action="store_true")
    p.add_argument("--label", default="")
    p = sub.add_parser("record", help="record selected signals for a fixed duration, or until stopped")
    _connection_args(p)
    p.add_argument("--duration", type=float, default=120.0, help="seconds (ignored with --until-stop)")
    p.add_argument(
        "--profile",
        default="idle",
        help="recording profile of the active vehicle (idle, warmup, road, mini_extended_monitoring)",
    )
    p.add_argument(
        "--until-stop",
        action="store_true",
        help="record until Ctrl-C (or SIGTERM); the session is closed cleanly and kept",
    )
    p.add_argument(
        "--max-duration", type=float, default=None, help="optional safety limit in seconds for --until-stop"
    )
    p.add_argument("--drive-type", choices=["city", "highway", "mixed", "other"], default=None)
    p.add_argument(
        "--resume-probe",
        type=float,
        default=None,
        metavar="SECONDS",
        help="opt-in: after the ECU stops answering, send one probe request this often (backs off). "
        "Default: no vehicle request until the recording is stopped",
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
    p = sub.add_parser(
        "annotate", help="add an event marker to a session (also while another process records)"
    )
    p.add_argument("--session", default="latest")
    p.add_argument("--label", default="")
    p.add_argument(
        "--event", choices=sorted(MONITORING_EVENT_IDS), default=None, help="predefined MINI monitoring event"
    )
    p.add_argument("--note", default="")
    p = sub.add_parser("monitoring", help="monitoring summary of a session (shutdowns, fan, voltage, DPF)")
    p.add_argument("--session", default="latest")
    p = sub.add_parser("drive-type", help="label a session as city / highway / mixed / other")
    p.add_argument("--session", default="latest")
    p.add_argument("type", choices=["city", "highway", "mixed", "other"])
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


def _record_with_signals(wb: Workbench, a: argparse.Namespace, pids: list[int] | None) -> str:
    """Run the recording in the foreground. First Ctrl-C / SIGTERM: stop cleanly (the session is
    closed and kept). Second Ctrl-C: abort immediately (the session is still closed with what was saved)."""
    presses = {"n": 0}

    def on_signal(signum, frame) -> None:  # noqa: ARG001
        presses["n"] += 1
        if presses["n"] == 1:
            print("\nstopping: closing the session ... (Ctrl-C again to abort now)", file=sys.stderr)
            wb.stop_recording()
        else:
            raise KeyboardInterrupt

    old = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    in_main = threading.current_thread() is threading.main_thread()
    if in_main:
        for sig in old:
            signal.signal(sig, on_signal)
    q = wb.hub.subscribe()
    done = threading.Event()
    printer = threading.Thread(target=_print_live_events, args=(q, done), daemon=True)
    printer.start()
    try:
        return wb.record(
            a.duration,
            a.profile,
            pids,
            a.label,
            not a.no_pre_scan,
            not a.no_post_scan,
            a.freeze_frame,
            until_stop=a.until_stop,
            max_duration_s=a.max_duration,
            drive_type=a.drive_type,
            resume_probe_s=a.resume_probe,
        )
    finally:
        done.set()
        wb.hub.unsubscribe(q)
        if in_main:
            for sig, handler in old.items():
                signal.signal(sig, handler)


def _print_live_events(q: queue.Queue, done: threading.Event) -> None:
    """Timeline events and link-state changes on stderr while a recording runs."""
    last_state = None
    while not done.is_set():
        try:
            ev = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if ev.get("type") == "annotation":
            an = ev["annotation"]
            stamp = time.strftime("%H:%M:%S", time.localtime(an["t_wall"]))
            print(f"[{stamp}] {an['origin']:<8} {an['label']}: {an['note']}", file=sys.stderr)
        elif ev.get("type") == "monitor" and ev["monitor"].get("state") != last_state:
            last_state = ev["monitor"].get("state")
            print(
                f"[link] {last_state}; intervals {ev['monitor'].get('effective_intervals')}", file=sys.stderr
            )


def _print_monitoring(m: dict) -> None:
    def t(x):
        return "n/a" if x is None else f"{x:.0f}s"

    print(f"session {m['session_id']}" + ("  [SYNTHETIC DATA]" if m["synthetic"] else ""))
    d = m["drive"]
    print(
        f"drive: running {d['running_s'] / 60:.1f} min, max {d['max_speed_kmh']} km/h, "
        f"label {d['drive_type_user_label'] or '-'}"
    )
    print("signals:")
    for s in m["signals"]:
        print(f"  {s['hex']:<5}{s['name'][:46]:<48}{s['tier'] or '-':<7}{s['provenance']:<8}{s['status']}")
    print("engine stops:")
    for st in m["shutdowns"]:
        print(f"  {t(st['t_rel_s'])}: {st['origin']} ({st['basis']})")
    print("fan runs (manual markers):")
    for f in m["fan"]["runs"]:
        dur = f"{f['duration_s']:.0f}s" if f["duration_s"] is not None else "end not marked"
        print(
            f"  {t(f['t_start_rel_s'])}: {dur}"
            + (f", {f['seconds_after_shutdown']:.0f}s after stop" if f["after_shutdown"] else "")
        )
    for key in ("ecu", "adapter"):
        v = m["voltage"][key]
        if v["status"] == "measured":
            run = v["engine_running"] or {}
            print(
                f"voltage {key}: start {v['at_start']['value']} V, running median {run.get('median')} V, drops {len(v['drops'])}"
            )
        else:
            print(f"voltage {key}: not measured")
    print("DPF:")
    for g in m["dpf"]["availability"]:
        if g["group"] == "wanted":
            for i in g["items"]:
                print(f"  {i['name']}: {i['status']}")
    for r in m["detected_events"]:
        print(f"detected ({r['origin']}, {r['status']}, {r['confidence']}): {r['text']}")
    for line in m["dpf"]["limitations"] + m["coolant"].get("limitations", []):
        print(f"limitation: {line}")
    for e in m["link"]:
        print(f"link {t(e['t_rel_s'])}: {e['type']}")


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    store = Store(a.db or config.db_path())
    wb = Workbench(store)
    if a.vehicle:
        wb.set_profile(a.vehicle)
    if a.cmd not in ("simulate", "fixtures", "profiles"):
        for sid in wb.recover_interrupted():
            print(
                f"note: session {sid} was left open by an interrupted run; closed, data kept", file=sys.stderr
            )
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
        wb.shutdown()
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
                hw = "hardware-validated" if p.hardware_validated else "NOT hardware-validated"
                print(
                    f"{'*' if p.id == wb.profile.id else ' '} {p.id:<30} {p.name}  "
                    f"[{p.status}; manufacturer DDE/DME data: not read; {hw}]"
                )
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

    if a.cmd == "dde-capability":
        from app.manufacturer.bmw.capability import assess_session, render_markdown, report_rows

        if a.session:
            sid = _resolve(store, a.session)
            report = assess_session(store, sid)
        else:
            _connect(wb, a)
            sid = wb.dde_capability(a.label, a.read_vin)
            report = store.get_session(sid)["meta"]["dde_capability"]
        if a.out:
            out = Path(a.out)
            out.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n"
                if out.suffix == ".json"
                else render_markdown(report),
                encoding="utf-8",
            )
            print(f"wrote {out}", file=sys.stderr)
        if a.json:
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        v = report["verdicts"]
        print(f"session {sid}" + ("  [SYNTHETIC DATA]" if report["synthetic"] else ""))
        print(f"generic OBD           : {v['generic_obd']['verdict']}")
        print(f"manufacturer transport: {v['manufacturer_transport']['verdict']}")
        for b in v["manufacturer_transport"]["blockers"]:
            print(f"  blocker   : {b}")
        for b in v["manufacturer_transport"]["unverified"]:
            print(f"  unverified: {b}")
        for name, val, quality, src in report_rows(report):
            print(f"  {name:<34}{quality:<14}{val[:60]:<62}{src}")
        if not a.session:
            print(f"export for review (VIN redacted): mini-obd export --session {sid}", file=sys.stderr)
        return 0

    if a.cmd == "dde-settings":
        if a.opt_in or a.opt_out or a.variant is not None or a.clear_variant:
            wb.set_dde_settings(
                opt_in=True if a.opt_in else (False if a.opt_out else None),
                confirmed_variant=a.variant,
                clear_variant=a.clear_variant,
            )
        st = wb.dde_status()
        if a.json:
            print(json.dumps(st, indent=2))
            return 0
        if not st["supported"]:
            print(f"{st['vehicle_profile']}: no BMW DDE")
            return 0
        s_ = st["settings"]
        print(
            f"opt-in: {'YES' if s_.get('opt_in') else 'no'}; confirmed DDE variant: {s_.get('confirmed_variant') or '-'}"
        )
        print(
            f"catalog {st['version']}; adapter verdict: {st['context']['adapter_verdict'] or 'not assessed'}"
        )
        for m in st["availability"]:
            print(f"  {m['metric']:<30}{m['status']:<11}{(m['reasons'] or [''])[0][:80]}")
        return 0

    if a.cmd == "dde-catalog":
        from app.manufacturer.bmw.catalog import ExecutionContext, default_catalog

        cat = default_catalog()
        desc = cat.describe(ExecutionContext())
        if a.json:
            print(json.dumps(desc, indent=2))
            return 0
        n_exec = sum(m["status"] == "executable" for m in desc["availability"])
        print(f"catalog {cat.version}: {n_exec} executable of {len(desc['availability'])} metrics")
        for m in desc["availability"]:
            print(
                f"  {m.get('priority', '-'):<3}{m['metric']:<30}{m['status']:<11}{(m['reasons'] or [''])[0][:70]}"
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
        tiers = [k for k in ("fast", "medium", "slow") if plan.get(k)]
        for k in tiers:
            print(
                f"recording ({k}): "
                + ", ".join(f"{p:02X} {PIDS[p].name if p in PIDS else pid_name(p)}" for p in plan[k]),
                file=sys.stderr,
            )
        if plan["unsupported"]:
            print(
                "not supported, skipped: " + ", ".join(f"{p:02X} {pid_name(p)}" for p in plan["unsupported"]),
                file=sys.stderr,
            )
        if a.until_stop:
            limit = f" (safety limit {a.max_duration:.0f} s)" if a.max_duration else ""
            print(f"recording until stopped{limit}: press Ctrl-C to stop and save", file=sys.stderr)
        sid = _record_with_signals(wb, a, pids)
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
        if s["meta"].get("post_scan_skipped"):
            print(f"  post-scan skipped: {s['meta']['post_scan_skipped']}")
        print(f"  {'PID':<5}{'signal':<49}{'Hz':>7}{'ok':>6}{'drop':>6}{'lat ms':>8}{'max gap s':>11}")
        for m in s["meta"]["metrics"]:
            hexid = "ATRV" if m.get("mode", 1) == 0 else f"{m['pid']:02X}"
            print(
                f"  {hexid:<5}{m['name'][:47]:<49}{m['actual_hz']:>7}{m['ok']:>6}{m['dropped']:>6}{m['latency_ms_mean'] or 0:>8}{m['max_gap_s'] or 0:>11}"
            )
        return 0

    if a.cmd == "monitoring":
        from app.diagnostics.monitoring import summary_for

        sid = _resolve(store, a.session)
        m = summary_for(store, sid)
        if a.json:
            print(json.dumps(m, indent=2, default=str))
            return 0
        _print_monitoring(m)
        return 0

    if a.cmd == "drive-type":
        sid = _resolve(store, a.session)
        wb.set_drive_type(sid, a.type)
        print(f"{sid}: drive type {a.type}")
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
            open_ = "" if s["ended_at"] else "  [OPEN]"
            dt = (s["meta"].get("user_labels") or {}).get("drive_type")
            print(
                f"{s['id']}  {s['kind']:<8} {s['source']:<9} {'SYNTHETIC' if s['synthetic'] else 'real':<9} {s['vehicle_profile']:<28} {s['label']}"
                + (f" ({dt})" if dt else "")
                + open_
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
        ann = wb.annotate(sid, a.label, a.note, a.event)
        print(f"marker '{ann.label}' added to {sid}")
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
