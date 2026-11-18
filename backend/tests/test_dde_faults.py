"""DDE-04: BMW DDE fault memory inside the existing scans. Synthetic simulator only."""

import dataclasses
import io
import zipfile

from app.adapters.simulator import REGEN as REGEN_CYCLE
from app.adapters.simulator import SCENARIOS, FakeElmServer, SimulatorTransport, _syn_dde
from app.adapters.tcp import VgateWiFiTransport
from app.cli import main
from app.diagnostics.engine import analyze_session
from app.manufacturer.bmw.synthetic import SYNTHETIC_VARIANT
from app.models.domain import DtcScan
from app.service import Workbench
from app.storage.export import build_files, build_zip

SC = "mini_dde_synthetic"


def _wb(store, scenario=SC, opt_in=True, source="simulator"):
    wb = Workbench(store)
    if opt_in:
        wb.set_dde_settings(opt_in=True, confirmed_variant=SYNTHETIC_VARIANT)
    sc = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    wb.attach(SimulatorTransport(sc), source, sc.name)
    return wb


def _tcp_wb(store, script):
    srv = FakeElmServer(SC, script=script).start()
    wb = Workbench(store)
    wb.set_dde_settings(opt_in=True, confirmed_variant=SYNTHETIC_VARIANT)
    wb.attach(
        VgateWiFiTransport(srv.host, srv.port, default_timeout_s=2, backoff_initial_s=0.05), "simulator", SC
    )
    return wb, srv


def test_multiframe_fault_read_keeps_codes_exact_and_separate(store):
    wb = _wb(store)
    sid = wb.scan(freeze_frame=False)
    (sc,) = store.scans(sid)
    m = sc.manufacturer
    assert m is not None and m.status == "read" and m.synthetic and m.origin == "bmw_dde"
    assert [f.code for f in m.faults] == ["4862", "480A"]
    assert m.faults[0].status_raw == "24" and m.faults[0].status["present"] is False
    assert (
        m.ecu == "7B8/F5"
        and m.decoder_version == "1"
        and m.catalog_version.startswith("dde-catalog/synthetic")
    )
    # generic codes are untouched and never contain the manufacturer codes
    assert sc.origin == "generic_obd" and [d.code for d in sc.dtcs if d.kind == "stored"] == ["P115D"]
    assert not any(d.code in ("4862", "480A") or d.raw_hex in ("4862", "480A") for d in sc.dtcs)
    sim = wb.transport.sim
    assert sim.manufacturer_requests == ["1802FFFF"] and sim.baseline() and not sim.dtcs_cleared
    phases = {e["phase"] for e in store.exchanges(sid) if e["request"] in ("ATSH7B0", "1802FFFF")}
    assert phases == {"dde:faults"}


def test_empty_fault_memory_is_no_faults_only_after_a_complete_read(store):
    empty = dataclasses.replace(SCENARIOS[SC], name="dde_empty", dde=_syn_dde(REGEN_CYCLE, ()))
    wb = _wb(store, empty)
    (sc,) = store.scans(wb.scan(freeze_frame=False))
    assert sc.manufacturer.status == "no_faults" and sc.manufacturer.faults == []


def test_negative_truncated_and_unsupported_adapter(store):
    neg, srv = _tcp_wb(
        store, lambda cmd, n, normal: [(0, b"7B8 F5 03 7F 18 22 00 00 \r\r>")] if cmd == "1802FFFF" else None
    )
    try:
        m = store.scans(neg.scan(freeze_frame=False))[0].manufacturer
        assert m.status == "negative" and m.nrc == "22" and m.faults == []
    finally:
        neg.disconnect()
        srv.stop()

    trunc, srv = _tcp_wb(
        store,
        lambda cmd, n, normal: [(0, b"7B8 F5 06 58 02 48 62 24 48 \r\r>")] if cmd == "1802FFFF" else None,
    )
    try:
        m = store.scans(trunc.scan(freeze_frame=False))[0].manufacturer
        assert m.status == "partial" and [f.code for f in m.faults] == ["4862"]
    finally:
        trunc.disconnect()
        srv.stop()

    wb = _wb(store, "healthy_idle")  # fake adapter rejects addressing commands
    m = store.scans(wb.scan(freeze_frame=False))[0].manufacturer
    assert m.status == "unsupported_adapter" and "ATSH7B0" in m.detail
    assert "1802FFFF" not in wb.transport.sim.received


def test_without_verification_or_opt_in_nothing_is_sent_and_no_false_no_faults(store):
    wb = _wb(store, opt_in=False)
    sid = wb.scan(freeze_frame=False)
    m = store.scans(sid)[0].manufacturer
    assert m.status == "unavailable" and "opt-in" in m.detail and m.faults == []
    assert not any(c.startswith("ATSH") or c == "1802FFFF" for c in wb.transport.sim.received)
    # a "real" adapter uses the real catalog: unavailable, nothing sent, even with opt-in
    wb2 = _wb(store, source="real")
    m2 = store.scans(wb2.scan(freeze_frame=False))[0].manufacturer
    assert m2.status == "unavailable" and not m2.synthetic
    assert not any(c.startswith("ATSH") for c in wb2.transport.sim.received)
    # the analysis does not turn "unavailable" into "no 4862"
    f = {x.id: x for x in analyze_session(store, sid)}["dde_4862"]
    assert not f.contradicting or not any("did not list 4862" in e.text for e in f.contradicting)
    assert any("unavailable" in c for c in f.conditions)


def test_measured_codes_feed_the_findings_separately_from_owner_reports(store):
    wb = _wb(store)
    sid = wb.scan(freeze_frame=False)
    fs = {x.id: x for x in analyze_session(store, sid)}
    f = fs["dde_4862"]
    assert f.strength == "suggestive"
    assert any(
        "BMW DDE fault memory (scan scan) lists 4862" in e.text and "SYNTHETIC" in e.text
        for e in f.supporting
    )
    assert any("Owner-reported" in c for c in f.conditions)
    assert any("lists 480A" in e.text for e in fs["dpf_480a"].supporting)


def test_recording_pre_scan_carries_the_dde_read_but_not_the_polling_loop(store):
    wb = _wb(store)
    sid = wb.record(5, "idle", pre_scan=True, post_scan=True)
    scans = store.scans(sid)
    assert [s.phase for s in scans] == ["pre", "post"] and all(s.manufacturer.status == "read" for s in scans)
    assert wb.transport.sim.manufacturer_requests == [
        "1802FFFF",
        "1802FFFF",
    ]  # once per scan, never in the loop


def test_exports_cli_and_backward_compatibility(store, tmp_path, capsys):
    wb = _wb(store)
    sid = wb.scan(freeze_frame=False)
    files = build_files(store, sid)
    assert (
        "dde_faults.csv" in files and "4862" in files["dde_faults.csv"] and "480A" in files["dde_faults.csv"]
    )
    assert "BMW DDE fault memory [SYNTHETIC]: **read**" in files["report.md"]
    assert '"manufacturer"' in files["dtcs.json"]
    zf = zipfile.ZipFile(io.BytesIO(build_zip(store, sid)))
    assert any(n.endswith("dde_faults.csv") for n in zf.namelist())
    # an old stored scan (before DDE-04) still loads and exports unchanged in shape
    old = '{"t_wall": 1.0, "phase": "scan", "dtcs": [{"code": "P0401", "raw_hex": "0401", "kind": "stored", "ecu": "7E8"}], "mode_status": {"stored": "supported"}, "mil_on": true, "dtc_count_reported": 1, "freeze_frame": null}'
    sc = DtcScan.model_validate_json(old)
    assert sc.manufacturer is None and sc.origin == "generic_obd"
    sid_old = store.create_session("scan", "mini-countryman-r60-diesel", "real", False)
    with store.tx() as db:
        db.execute(
            "INSERT INTO dtc_scans(session_id,t_wall,phase,data) VALUES(?,?,?,?)", (sid_old, 1.0, "scan", old)
        )
    f_old = build_files(store, sid_old)
    assert "dde_faults.csv" not in f_old and "BMW DDE fault memory" not in f_old["report.md"]
    # CLI scan output lists the DDE read on its own line
    db = str(tmp_path / "cli.sqlite3")
    assert main(["--db", db, "dde-settings", "--opt-in", "--variant", SYNTHETIC_VARIANT]) == 0
    capsys.readouterr()
    assert main(["--db", db, "scan", "--simulator", SC, "--no-freeze-frame"]) == 0
    out = capsys.readouterr().out
    assert "BMW DDE fault memory [SYNTHETIC]: read" in out and "4862 (manufacturer code as returned" in out


def test_porsche_scans_have_no_dde_part(store):
    wb = Workbench(store)
    wb.set_profile("porsche-boxster-986-mk2")
    wb.attach(SimulatorTransport("porsche_healthy_idle"), "simulator", "porsche_healthy_idle")
    (sc,) = store.scans(wb.scan(freeze_frame=False))
    assert sc.manufacturer is None
    assert not any(c.startswith("ATSH") for c in wb.transport.sim.received)
