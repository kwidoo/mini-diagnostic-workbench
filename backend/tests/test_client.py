import pytest

from app.adapters.simulator import SYNTHETIC_VIN, SimulatorTransport
from app.obd.client import EcuNotRespondingError, ObdClient, ObdError, UnsupportedProtocolError


def test_init_log_and_protocol_readback(sim_client):
    c = sim_client()
    cmds = [e["command"] for e in c.adapter.init_log]
    assert cmds[:7] == ["ATZ", "ATE0", "ATL0", "ATS1", "ATH1", "ATAT1", "ATSP0"]
    assert c.adapter.identity == "ELM327 v2.3"
    assert c.adapter.protocol_number == "A6" and c.adapter.is_can and c.adapter.is_29bit is False
    assert c.family == "can"


def test_query_before_init_is_refused():
    c = ObdClient(SimulatorTransport())
    with pytest.raises(ObdError):
        c.read_pid(0x0C)


def test_capability_discovery_per_ecu(sim_client):
    caps = sim_client().capabilities
    assert caps.primary_ecu == "7E8"
    eng = next(e for e in caps.ecus if e.ecu == "7E8")
    assert {0x0C, 0x10, 0x0B, 0x23, 0x42} <= set(eng.mode01_pids)
    assert 0x20 not in eng.mode01_pids  # bitmap PIDs are not signals
    assert next(e for e in caps.ecus if e.ecu == "7E9").mode01_pids == [0x01, 0x0D]


def test_unsupported_services_and_pids_are_reported_not_invented(sim_client):
    c = sim_client("unsupported_pids")
    assert 0x10 not in c.capabilities.supported_mode01()
    r = c.read_pid(0x10)[0]  # MAF is not supported by this ECU
    assert r.quality == "no_data" and r.value is None and r.raw_hex == ""
    scan = c.scan_dtcs()
    assert scan.mode_status["permanent"] == "no_data"
    assert scan.mode_status["stored"] == "supported" and scan.dtcs == []
    assert c.capabilities.modes["06"] == "negative"
    assert c.vehicle_info(True)["vin_status"] == "not_supported"


def test_negative_response_quality(sim_client):
    c = sim_client("unsupported_pids")
    ff = c.read_freeze_frame()
    assert ff.status.startswith("negative") and "NRC 11" in ff.status


def test_dtcs_keep_raw_bytes_kind_and_ecu(sim_client):
    scan = sim_client("dpf_history").scan_dtcs()
    got = {(d.kind, d.code, d.raw_hex, d.ecu) for d in scan.dtcs}
    assert got == {
        ("stored", "P2002", "2002", "7E8"),
        ("pending", "P2453", "2453", "7E8"),
        ("permanent", "P2002", "2002", "7E8"),
    }
    assert scan.mil_on is True and scan.dtc_count_reported == 1


def test_freeze_frame_belongs_to_a_dtc(sim_client):
    ff = sim_client("dpf_history").read_freeze_frame()
    assert ff.status == "present" and ff.dtc == "P2002" and ff.ecu == "7E8"
    vals = {r.pid: r.value for r in ff.readings}
    assert vals[0x0C] == 1450.0 and vals[0x0D] == 42.0
    assert all(r.mode == 2 for r in ff.readings)
    assert sim_client("healthy_idle").read_freeze_frame().status == "none_stored"


def test_vin_is_not_requested_without_consent(sim_client):
    c = sim_client()
    info = c.vehicle_info()
    assert info["vin"] is None and info["vin_status"] == "not_requested"
    assert "0902" not in c.transport.sim.received
    assert info["calibration_ids"][0]["text"] == "SYNTHETIC-CAL-01"
    assert c.vehicle_info(include_vin=True)["vin"] == SYNTHETIC_VIN  # multi-frame ISO-TP


def test_multi_ecu_answers_stay_separate(sim_client):
    rs = sim_client().read_pid(0x0D)
    assert sorted(r.ecu for r in rs) == ["7E8", "7E9"]


def test_raw_only_pid_has_no_physical_value(sim_client):
    c = sim_client("dpf_history")
    assert 0x7A in c.capabilities.supported_mode01()
    r = c.read_pid(0x7A)[0]
    assert r.quality == "raw_only" and r.value is None and r.raw_hex


def test_kline_vehicle_decodes(sim_client):
    c = sim_client("porsche_bank_imbalance", allow_vin=True)
    assert c.family == "kline" and c.adapter.protocol_number == "A3"
    assert c.capabilities.primary_ecu == "10"
    assert c.read_pid(0x09)[0].value == pytest.approx(14.0, abs=1.0)
    scan = c.scan_dtcs()
    assert [(d.code, d.kind) for d in scan.dtcs] == [("P0174", "pending")]
    assert scan.mode_status["permanent"] == "no_data"
    assert c.vehicle_info()["vin"] == SYNTHETIC_VIN


def test_timeout_is_a_quality_flag_not_a_value():
    t = SimulatorTransport("slow_adapter", default_timeout_s=2.0)
    c = ObdClient(t)
    c.initialize()
    t.default_timeout_s = 0.1  # slower than every simulated answer
    r = c.read_pid(0x0C)[0]
    assert r.quality == "timeout" and r.value is None


def test_ecu_not_responding_gives_actionable_error():
    t = SimulatorTransport()
    orig = t.sim.handle
    t.sim.handle = lambda cmd: (b"SEARCHING...\rUNABLE TO CONNECT\r\r>", 0.0) if cmd == "0100" else orig(cmd)
    with pytest.raises(EcuNotRespondingError, match="ignition"):
        ObdClient(t).initialize()


def test_unsupported_protocol_is_refused():
    t = SimulatorTransport()
    orig = t.sim.handle
    t.sim.handle = lambda cmd: (b"A2\r\r>", 0.0) if cmd == "ATDPN" else orig(cmd)
    with pytest.raises(UnsupportedProtocolError):
        ObdClient(t).initialize()
