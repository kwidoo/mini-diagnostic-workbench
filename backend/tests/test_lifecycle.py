"""P0-2 regression tests: complete workbench operations are serialised.

connect / disconnect / profile switch / scan / inventory / record share one
operation guard. Anything that would change the connection or the vehicle
identity while an operation runs is rejected (HTTP 409); Stop then disconnect works.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.adapters.simulator import FakeElmServer
from app.adapters.tcp import VgateWiFiTransport
from app.api.main import create_app
from app.service import BusyError, ProfileMismatchError, Workbench

MINI = "mini-countryman-r60-diesel"
PORSCHE = "porsche-boxster-986-mk2"


@pytest.fixture
def api(tmp_path):
    app = create_app(str(tmp_path / "api.sqlite3"))
    with TestClient(app) as client:
        yield client


def _slow_server(slow_cmd: str, pause: float = 1.5):
    """Healthy MINI idle where one command answers slowly (to hold an operation open)."""
    return FakeElmServer("healthy_idle", script=lambda c, n, raw: [(pause, raw)] if c == slow_cmd else None)


def _attach(api, srv):
    wb = api.app.state.wb
    wb.attach(VgateWiFiTransport(srv.host, srv.port, default_timeout_s=5), "simulator", "healthy_idle")
    return wb


def _in_background(fn):
    out: dict = {}

    def run():
        out["result"] = fn()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, out


def _wait_operation(api, name, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if api.get("/api/status").json()["operation"] == name:
            return
        time.sleep(0.02)
    raise AssertionError(f"operation {name!r} never started")


def _wait_idle(api, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = api.get("/api/status").json()
        if st["operation"] is None and st["recording"] is None:
            return st
        time.sleep(0.05)
    raise AssertionError("operation did not finish")


def _assert_lifecycle_changes_rejected(api, running):
    for method, path, body in [
        ("post", "/api/connect", {"mode": "simulator", "scenario": "healthy_idle"}),
        ("post", "/api/disconnect", None),
        ("put", "/api/profiles/active", {"id": PORSCHE}),
        ("post", "/api/scan", {}),
        ("post", "/api/inventory", {}),
        ("post", "/api/recording/start", {"duration_s": 1}),
    ]:
        r = getattr(api, method)(path, json=body) if body is not None else getattr(api, method)(path)
        assert r.status_code == 409, (path, r.status_code, r.text)
        assert running in r.json()["detail"] or "already running" in r.json()["detail"], r.json()


def test_scan_blocks_connect_disconnect_profile_switch_and_other_operations(api):
    with _slow_server("03") as srv:
        _attach(api, srv)
        th, out = _in_background(lambda: api.post("/api/scan", json={"label": "slow"}))
        _wait_operation(api, "scan")
        _assert_lifecycle_changes_rejected(api, "scan")
        th.join(20)
        scan = out["result"]
        assert scan.status_code == 200 and scan.json()["ended_at"] is not None
        st = _wait_idle(api)
        assert st["connected"] and st["vehicle_profile"] == MINI and st["connected_profile"] == MINI
        assert srv.connections == 1  # nobody reconnected underneath the scan
        assert api.post("/api/disconnect").status_code == 200


def test_inventory_blocks_recording_and_profile_switch(api):
    with _slow_server("0105") as srv:
        _attach(api, srv)
        th, out = _in_background(lambda: api.post("/api/inventory", json={}))
        _wait_operation(api, "inventory")
        _assert_lifecycle_changes_rejected(api, "inventory")
        th.join(20)
        assert out["result"].status_code == 200
        _wait_idle(api)
        api.post("/api/disconnect")


def test_recording_blocks_lifecycle_changes_then_stop_then_disconnect(api):
    api.post("/api/connect", json={"mode": "simulator", "scenario": "healthy_idle"})
    sid = api.post("/api/recording/start", json={"duration_s": 60}).json()["recording"]["session_id"]
    _wait_operation(api, "recording")
    _assert_lifecycle_changes_rejected(api, "recording")
    assert api.get("/api/status").json()["connected"] is True
    api.post("/api/recording/stop")
    _wait_idle(api)
    assert api.post("/api/disconnect").json()["connected"] is False
    s = api.get(f"/api/sessions/{sid}").json()
    assert s["ended_at"] is not None and s["vehicle_profile"] == MINI and not s["meta"].get("aborted")


def test_profile_switch_while_connected_requires_disconnect_and_reprobe(api):
    api.post("/api/connect", json={"mode": "simulator", "scenario": "healthy_idle"})
    r = api.put("/api/profiles/active", json={"id": PORSCHE})
    assert r.status_code == 409 and "disconnect" in r.json()["detail"]
    assert api.put("/api/profiles/active", json={"id": MINI}).status_code == 200  # same vehicle: no-op
    assert api.get("/api/status").json()["vehicle_profile"] == MINI

    api.post("/api/disconnect")
    assert api.put("/api/profiles/active", json={"id": PORSCHE}).status_code == 200
    st = api.post("/api/connect", json={"mode": "simulator", "scenario": "porsche_healthy_idle"}).json()
    assert st["connected_profile"] == PORSCHE and st["adapter"]["is_can"] is False  # freshly probed K-line
    scan = api.post("/api/scan", json={}).json()
    assert scan["vehicle_profile"] == PORSCHE
    assert scan["meta"]["capabilities"]["primary_ecu"] == "10"  # K-line ECU, not the MINI's 7E8
    api.post("/api/disconnect")


def test_profile_changed_behind_the_connection_is_refused(store):
    """E.g. the CLI switched the profile in the shared database while the API holds a connection."""
    from app.adapters.simulator import SimulatorTransport

    wb = Workbench(store)
    wb.attach(SimulatorTransport("healthy_idle"), "simulator", "healthy_idle")
    store.set_setting("active_profile", PORSCHE)
    with pytest.raises(ProfileMismatchError, match="reconnect"):
        wb.scan()
    assert wb.operation is None  # guard released
    wb.disconnect()


def test_workbench_rejects_overlapping_operations_directly(store):
    with _slow_server("03") as srv:
        wb = Workbench(store)
        wb.attach(VgateWiFiTransport(srv.host, srv.port), "simulator", "healthy_idle")
        th, out = _in_background(wb.scan)
        end = time.monotonic() + 5
        while wb.operation != "scan" and time.monotonic() < end:
            time.sleep(0.01)
        for fn in (wb.disconnect, lambda: wb.set_profile(PORSCHE), wb.inventory, lambda: wb.record(1)):
            with pytest.raises(BusyError, match="scan in progress"):
                fn()
        th.join(20)
        assert out["result"] and store.get_session(out["result"])["ended_at"] is not None
        wb.disconnect()


def test_shutdown_during_recording_closes_the_session_cleanly(store):
    from app.adapters.simulator import SimulatorTransport

    wb = Workbench(store)
    wb.attach(SimulatorTransport("healthy_idle"), "simulator", "healthy_idle")
    started = threading.Event()
    th, out = _in_background(lambda: wb.record(3600, post_scan=False, started=started))
    assert started.wait(5)
    wb.shutdown(wait_s=10)
    th.join(10)
    s = store.get_session(out["result"])
    assert s["ended_at"] is not None and wb.client is None and wb.operation is None and wb.active is None
