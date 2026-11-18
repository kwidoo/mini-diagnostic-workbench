"""DDE-01: capability assessment from generic OBD + adapter identity only."""

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.simulator import SCENARIOS, SYNTHETIC_VIN, Scenario, SimulatorTransport
from app.api.main import create_app
from app.cli import main
from app.manufacturer.bmw.capability import (
    assess,
    assess_session,
    parse_elm_version,
    render_markdown,
)
from app.obd.allowlist import AllowedCommand
from app.service import Workbench
from app.storage.export import build_files, build_zip

REAL = json.loads((Path(__file__).parent / "data" / "real-mini-20261118-connect.json").read_text())


def _wb(store, scenario="mini_dde_capability", sc=None):
    wb = Workbench(store)
    t = SimulatorTransport(sc or scenario)
    wb.attach(t, "simulator", scenario)
    return wb, t


def _all_allowlisted(received):
    for cmd in received:
        AllowedCommand.parse(cmd)  # raises on anything outside the generic allowlist


def test_capability_run_sends_only_generic_requests_and_reports_honestly(store):
    wb, t = _wb(store)
    sid = wb.dde_capability()
    _all_allowlisted(t.sim.received)
    assert "0902" not in t.sim.received  # no VIN without consent
    assert {"0904", "0906", "090A"} <= set(t.sim.received)
    assert not t.sim.dtcs_cleared
    rep = store.get_session(sid)["meta"]["dde_capability"]
    assert store.get_session(sid)["kind"] == "capability"
    ident = rep["identification"]
    assert ident["calibration_id"]["quality"] == "observed"
    assert ident["calibration_id"]["value"][0]["text"] == "SYNTHETIC-CAL-01"
    assert ident["cvn"]["value"][0]["raw_hex"] == "5EC0FFEE" and ident["cvn"]["value"][0]["text"] is None
    assert ident["ecu_name"]["value"][0]["text"].startswith("ECM")
    assert ident["vin"]["quality"] == "not_attempted"
    # calibration ID / ECU name are multi-frame on CAN: ISO-TP evidence is observed
    assert rep["isotp_multiframe"]["value"] is True and rep["isotp_multiframe"]["evidence"]
    assert rep["dde"]["manufacturer_requests_sent"]["value"] == 0
    # unknown stays unknown: no DDE variant is guessed from a calibration ID
    assert rep["dde"]["variant"]["quality"] == "unknown" and rep["dde"]["variant"]["value"] is None
    assert rep["adapter"]["chip"]["quality"] == "unknown"
    # synthetic data never upgrades to candidate / verified
    assert rep["verdicts"]["generic_obd"]["verdict"] == "generic_obd_simulated"
    assert rep["verdicts"]["manufacturer_transport"]["verdict"] == "manufacturer_transport_unverified"
    assert wb.latest_dde_capability()["session_id"] == sid


def test_partial_and_missing_mode09(store):
    wb, _ = _wb(store, "healthy_idle")  # advertises InfoTypes 02 + 04 only
    rep = store.get_session(wb.dde_capability())["meta"]["dde_capability"]
    assert rep["identification"]["calibration_id"]["quality"] == "observed"
    assert rep["identification"]["cvn"]["quality"] == "not_supported"
    assert rep["identification"]["ecu_name"]["quality"] == "not_supported"

    wb2, t2 = _wb(store, "unsupported_pids")  # no service 09 at all
    rep2 = store.get_session(wb2.dde_capability())["meta"]["dde_capability"]
    assert all(f["quality"] == "not_supported" for f in rep2["identification"].values())
    assert not any(c.startswith("09") and c != "0900" for c in t2.sim.received)
    # single-frame answers only: multi-frame ISO-TP stays unproven, verdict never candidate
    assert rep2["isotp_multiframe"]["value"] is False
    assert rep2["verdicts"]["manufacturer_transport"]["verdict"] != "manufacturer_transport_candidate"


def test_vin_opt_in_never_enters_report_or_default_export(store):
    wb, t = _wb(store)
    sid = wb.dde_capability(read_vin=True)
    assert "0902" in t.sim.received
    rep = store.get_session(sid)["meta"]["dde_capability"]
    assert rep["identification"]["vin"]["quality"] == "observed"
    assert SYNTHETIC_VIN not in json.dumps(rep)
    files = build_files(store, sid)
    assert "dde_capability.json" in files and "dde_capability.md" in files
    assert not any(SYNTHETIC_VIN in v for v in files.values())
    assert "## DDE capability" in files["report.md"]
    zf = zipfile.ZipFile(io.BytesIO(build_zip(store, sid)))
    assert not any(SYNTHETIC_VIN.encode() in zf.read(n) for n in zf.namelist())
    assert any(SYNTHETIC_VIN in v for v in build_files(store, sid, include_vin=True).values())


@pytest.mark.parametrize(
    "identity,expected",
    [("ELM327 v2.2", (2, 2)), ("ELM327 v1.4b", (1, 4)), ("OBDII v1.5", None), ("", None), (None, None)],
)
def test_parse_elm_version(identity, expected):
    v = parse_elm_version(identity)
    assert (v["tuple"] if v else None) == expected


def _report(identity="ELM327 v2.2", proto="A6", exchanges=None, synthetic=False, is_can=True):
    adapter = {**REAL["adapter"], "identity": identity, "protocol_number": proto, "is_can": is_can}
    if not is_can:
        adapter["protocol_name"] = "ISO 9141-2"
    return assess(
        adapter=adapter,
        capabilities=REAL["capabilities"],
        exchanges=REAL["exchanges"] if exchanges is None else exchanges,
        source="real",
        synthetic=synthetic,
    )


MULTIFRAME = {
    "seq": 99,
    "t_wall": 1.0,
    "t_mono": 1.0,
    "request": "0904",
    "request_hex": "303930340D",
    # 49 04 01 + 16 text bytes: first frame + 2 consecutive frames from 7E8
    "response_hex": "".join(
        f"{ord(c):02X}"
        for c in "7E8 10 13 49 04 01 41 42 43 \r7E8 21 44 45 46 47 48 49 4A \r7E8 22 4B 4C 4D 4E 4F 50 00 \r\r>"
    ),
    "response_text": "",
    "latency_ms": 80.0,
    "status": "ok",
    "phase": "capability",
}


def test_real_2026_11_18_trace_is_generic_verified_but_manufacturer_unverified():
    rep = _report()
    v = rep["verdicts"]
    assert v["generic_obd"]["verdict"] == "generic_obd_verified"
    assert v["manufacturer_transport"]["verdict"] == "manufacturer_transport_unverified"
    assert not v["manufacturer_transport"]["blockers"]
    assert any("ISO-TP" in x for x in v["manufacturer_transport"]["unverified"])
    assert rep["adapter"]["identity"]["quality"] == "claimed"
    assert any("not one of the ELM327 versions EdiabasLib recommends" in n for n in rep["adapter"]["notes"])
    assert [e["source_id"] for e in rep["ecus"]] == ["7E8", "7E9"]
    # service 09 InfoTypes were advertised but never read in that recording
    assert rep["identification"]["calibration_id"]["quality"] == "not_attempted"
    assert rep["identification"]["vin"]["quality"] == "not_attempted"
    assert rep["dde"]["manufacturer_requests_sent"]["value"] == 0


def test_candidate_needs_observed_multiframe_on_real_can_11bit_500k():
    rep = _report(exchanges=REAL["exchanges"] + [MULTIFRAME])
    assert rep["isotp_multiframe"]["value"] is True
    assert rep["verdicts"]["manufacturer_transport"]["verdict"] == "manufacturer_transport_candidate"
    assert "NOT proof" in rep["verdicts"]["manufacturer_transport"]["meaning"]
    # still nothing about the DDE itself is known
    assert rep["dde"]["variant"]["quality"] == "unknown"
    # the same evidence from a simulator is never a candidate
    assert (
        _report(exchanges=REAL["exchanges"] + [MULTIFRAME], synthetic=True)["verdicts"][
            "manufacturer_transport"
        ]["verdict"]
        == "manufacturer_transport_unverified"
    )


def test_misleading_or_old_clone_identities():
    old = _report("ELM327 v1.3", exchanges=REAL["exchanges"] + [MULTIFRAME])
    assert old["verdicts"]["manufacturer_transport"]["verdict"] == "manufacturer_transport_blocked"
    assert any("extended addressing" in b for b in old["verdicts"]["manufacturer_transport"]["blockers"])
    fake15 = _report("ELM327 v1.5")
    assert any("fake" in n for n in fake15["adapter"]["notes"])
    assert fake15["adapter"]["chip"]["value"] is None  # never inferred from ATI
    noversion = _report("OBDII interpreter")
    assert noversion["adapter"]["claimed_elm_version"]["quality"] == "unknown"
    assert noversion["verdicts"]["manufacturer_transport"]["verdict"] == "manufacturer_transport_unverified"
    kline = _report(is_can=False, proto="A3")
    assert kline["verdicts"]["manufacturer_transport"]["verdict"] == "manufacturer_transport_blocked"
    can29 = _report(proto="A7")
    assert any("11-bit" in b for b in can29["verdicts"]["manufacturer_transport"]["blockers"])


def test_trace_with_a_manufacturer_request_is_reported():
    bad = dict(MULTIFRAME, request="22F190", seq=100)
    rep = _report(exchanges=REAL["exchanges"] + [bad])
    assert rep["dde"]["manufacturer_requests_sent"]["value"] == 1


def test_offline_assessment_does_not_change_stored_sessions(store):
    wb, _ = _wb(store, "healthy_idle")
    sid = wb.record(3, "idle", pre_scan=False, post_scan=False)
    before = (store.get_session(sid), store.exchanges(sid), store.samples(sid))
    rep = assess_session(store, sid)
    assert rep["session_id"] == sid and rep["verdicts"]["generic_obd"]["verdict"] == "generic_obd_simulated"
    assert (store.get_session(sid), store.exchanges(sid), store.samples(sid)) == before
    assert "| adapter.identity |" in render_markdown(rep)


def test_api_and_cli(tmp_path, capsys):
    app = create_app(str(tmp_path / "api.sqlite3"))
    with TestClient(app) as api:
        assert api.get("/api/dde/capability/latest").json()["report"] is None
        assert api.post("/api/dde/capability", json={}).status_code == 409  # not connected
        api.post("/api/connect", json={"mode": "simulator", "scenario": "mini_dde_capability"})
        r = api.post("/api/dde/capability", json={}).json()
        assert r["kind"] == "capability" and r["report"]["dde"]["manufacturer_requests_sent"]["value"] == 0
        assert api.get("/api/dde/capability/latest").json()["report"]["session_id"] == r["id"]
        off = api.get(f"/api/sessions/{r['id']}/dde-capability").json()
        assert off["verdicts"] == r["report"]["verdicts"]
        zf = zipfile.ZipFile(io.BytesIO(api.get(f"/api/sessions/{r['id']}/export").content))
        assert any(n.endswith("dde_capability.json") for n in zf.namelist())
    db = str(tmp_path / "cli.sqlite3")
    assert main(["--db", db, "--json", "dde-capability", "--simulator", "mini_dde_capability"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdicts"]["manufacturer_transport"]["verdict"] == "manufacturer_transport_unverified"
    md = tmp_path / "cap.md"
    assert main(["--db", db, "dde-capability", "--session", "latest", "--out", str(md)]) == 0
    assert "DDE capability report" in md.read_text()


def test_scenario_registry_unchanged_for_existing_scenarios():
    # the new identity fields default to the previous fixed strings
    sc = SCENARIOS["healthy_idle"]
    assert sc.adapter_identity == "ELM327 v2.3" and isinstance(sc, Scenario)
