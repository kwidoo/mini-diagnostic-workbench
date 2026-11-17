"""Acceptance criterion 7: no ECU-changing request can be built or sent."""

import pytest

from app.adapters.simulator import SimulatorTransport
from app.fixtures_gen import record_scenario
from app.obd.allowlist import AT_COMMANDS, AllowedCommand, ForbiddenRequestError
from app.service import Workbench
from app.storage.db import Store

ALLOWED = [
    "0100",
    "010C",
    "01 0c",
    "020C00",
    "03",
    "07",
    "0A",
    "0600",
    "0900",
    "0902",
    "0904",
    "ATZ",
    "at e0",
    "ATDPN",
]
FORBIDDEN = [
    "04",  # clear DTCs / reset readiness
    "0400",
    "0800",
    "08",  # control of on-board systems
    "05",
    "0501",
    "10",
    "1003",
    "11",
    "1101",
    "14FFFFFF",
    "190209",
    "22F190",
    "2E1234",
    "2F0001",
    "3101FF00",
    "3E00",
    "27",
    "28",
    "010C0D",  # multi-PID requests are not allowed
    "020C01",  # only freeze frame 00
    "0903",
    "09",  # InfoTypes outside the list
    "030000",
    "0A00",
    "ATSH7E0",
    "ATMA",
    "ATPP0E",
    "ATCRA7E8",
    "ATFCSH7E0",
    "ATSP6",
    "ATD",
    "ATWS",
    "ATCAF0",
    "AT",
    "",
    "XYZ",
    "0",
    "01 0",
]


@pytest.mark.parametrize("text", ALLOWED)
def test_allowed(text):
    cmd = AllowedCommand.parse(text)
    assert cmd.wire().endswith(b"\r")


@pytest.mark.parametrize("text", FORBIDDEN)
def test_forbidden(text):
    with pytest.raises(ForbiddenRequestError):
        AllowedCommand.parse(text)


def test_direct_construction_cannot_bypass_validation():
    with pytest.raises(ForbiddenRequestError):
        AllowedCommand("04")
    with pytest.raises(ForbiddenRequestError):
        AllowedCommand("01 0c")  # non canonical
    with pytest.raises(ForbiddenRequestError):
        AllowedCommand.obd(0x04)
    with pytest.raises(ForbiddenRequestError):
        AllowedCommand.obd(0x22, 0xF1)
    with pytest.raises(ForbiddenRequestError):
        AllowedCommand.at("0100")


def test_at_allowlist_has_no_addressing_or_programming_commands():
    for cmd in AT_COMMANDS:
        assert not cmd.startswith(("ATSH", "ATCRA", "ATFC", "ATPP", "ATMA", "ATCM", "ATCF", "ATBRD", "ATWM"))


def test_transport_rejects_plain_strings():
    t = SimulatorTransport()
    t.connect()
    with pytest.raises(ForbiddenRequestError):
        t.send("04")  # type: ignore[arg-type]
    assert t.sim.received == []


@pytest.mark.parametrize("scenario", ["dpf_history", "unsupported_pids", "porsche_healthy_idle", "wifi_drop"])
def test_full_workflow_only_sends_allowlisted_requests(scenario):
    store = Store(":memory:")
    wb = Workbench(store)
    t = SimulatorTransport(scenario, default_timeout_s=2.0)
    wb.attach(t, "simulator", scenario, allow_vin=True)
    wb.scan(read_vin=True)
    wb.inventory()
    wb.record(10, "idle", freeze_frame=True)
    assert t.sim.received, "nothing was sent"
    for cmd in t.sim.received:
        AllowedCommand.parse(cmd)  # raises if anything outside the allowlist ever reached the adapter
    assert not t.sim.dtcs_cleared
    modes = {c[:2] for c in t.sim.received if not c.startswith("AT")}
    assert modes <= {"01", "02", "03", "06", "07", "09", "0A"}


def test_recorded_fixture_sessions_never_clear_dtcs():
    store = Store(":memory:")
    sid = record_scenario(store, "egr_mismatch", 10, [])
    scans = store.scans(sid)
    assert [d.code for d in scans[0].dtcs] == [d.code for d in scans[-1].dtcs] == ["P0401"]
