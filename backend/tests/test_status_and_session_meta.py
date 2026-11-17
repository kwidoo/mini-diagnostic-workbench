"""PR #1 follow-up 2 (P1-3, P1-4).

P1-3: once connect/disconnect has finished (successfully or not) the returned status, the
      polled status and the last published status event all show ``operation: None``.
P1-4: a session's metadata holds the FINAL adapter-link diagnostics, which exports use.
"""

import json
import queue
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.adapters.simulator import FakeElmServer
from app.adapters.tcp import VgateWiFiTransport
from app.api.main import create_app
from app.service import Workbench
from app.storage.export import build_files


@pytest.fixture
def api(tmp_path):
    app = create_app(str(tmp_path / "api.sqlite3"))
    with TestClient(app) as client:
        yield client


def _status_events(api):
    q = api.app.state.wb.hub.subscribe()

    def drain():
        out = []
        while True:
            try:
                ev = q.get_nowait()
            except queue.Empty:
                return out
            if ev["type"] == "status":
                out.append(ev["status"])

    return drain


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- P1-3 ----------------------------------------------------------------------------


def test_connect_response_status_and_published_event_are_idle_then_disconnect_works(api):
    events = _status_events(api)
    for _ in range(3):  # repeat connect/disconnect
        r = api.post("/api/connect", json={"mode": "simulator", "scenario": "healthy_idle"})
        assert r.status_code == 200
        assert r.json()["operation"] is None and r.json()["connected"] is True
        st = api.get("/api/status").json()
        assert st["operation"] is None and st["connected"] is True
        published = events()
        assert published and published[-1]["operation"] is None and published[-1]["connected"] is True
        assert all(p["operation"] is None for p in published), "no busy snapshot may be published"
        d = api.post("/api/disconnect")
        assert d.status_code == 200 and d.json()["connected"] is False and d.json()["operation"] is None
        assert events()[-1]["connected"] is False


def test_failed_connect_unreachable_leaves_no_phantom_busy_state(api):
    events = _status_events(api)
    r = api.post(
        "/api/connect", json={"mode": "real", "host": "127.0.0.1", "port": _free_port(), "timeout_s": 0.5}
    )
    assert r.status_code == 502
    st = api.get("/api/status").json()
    assert st["operation"] is None and st["connected"] is False
    published = events()
    assert published and published[-1]["operation"] is None and published[-1]["connected"] is False
    ok = api.post("/api/connect", json={"mode": "simulator", "scenario": "healthy_idle"})
    assert ok.status_code == 200 and ok.json()["operation"] is None
    assert api.post("/api/disconnect").status_code == 200


def test_failed_adapter_init_leaves_disconnected_idle_status(api):
    script = lambda c, n, raw: [(0.0, b"OK\r\r")] if c == "ATE0" else None  # noqa: E731
    with FakeElmServer("healthy_idle", script=script) as srv:
        events = _status_events(api)
        r = api.post(
            "/api/connect", json={"mode": "real", "host": srv.host, "port": srv.port, "timeout_s": 0.5}
        )
        assert r.status_code == 502 and "ATE0" in r.json()["detail"]
        assert not [c for _, c in srv.log if not c.startswith("AT")]  # no vehicle request at all
    st = api.get("/api/status").json()
    assert st["operation"] is None and st["connected"] is False and st["transport"] is None
    assert events()[-1]["operation"] is None


def test_connect_still_serialised_while_running(store):
    """Clearing the label at the end must not open a window for another lifecycle operation."""
    with FakeElmServer("healthy_idle", script=lambda c, n, raw: [(1.0, raw)] if c == "ATRV" else None) as srv:
        wb = Workbench(store)
        out = {}
        th = threading.Thread(
            target=lambda: out.update(st=wb.attach(VgateWiFiTransport(srv.host, srv.port), "simulator")),
            daemon=True,
        )
        th.start()
        end = time.monotonic() + 5
        while wb.operation != "connect" and time.monotonic() < end:
            time.sleep(0.01)
        from app.service import BusyError

        with pytest.raises(BusyError, match="connect in progress"):
            wb.disconnect()
        th.join(10)
        assert out["st"]["operation"] is None and out["st"]["connected"] is True
        wb.disconnect()
        assert wb.status()["operation"] is None


# -- P1-4 ----------------------------------------------------------------------------


def _record_with(store, script, **record_kw):
    with FakeElmServer("healthy_idle", script=script) as srv:
        wb = Workbench(store)
        t = VgateWiFiTransport(srv.host, srv.port, backoff_initial_s=0.05)
        wb.attach(t, "simulator", "healthy_idle")
        t.default_timeout_s = 0.3  # init/discovery done; make an injected stall time out quickly
        sid = wb.record(1.5, pids=[0x0C], pre_scan=False, post_scan=False, **record_kw)
        wb.disconnect()
    return sid


def test_final_transport_diagnostics_report_resyncs_from_the_recording(store):
    armed = {"n": 0}

    def script(cmd, n, raw):
        if cmd == "010C":
            armed["n"] += 1
            if armed["n"] == 3:
                return [(1.0, raw)]  # one RPM read stalls -> timeout -> resync + re-init
        return None

    sid = _record_with(store, script)
    meta = store.get_session(sid)["meta"]
    assert meta["transport_at_start"]["resyncs"] == 0
    assert meta["transport"]["resyncs"] >= 1 and meta["transport"]["reconnects"] >= 1
    files = build_files(store, sid)
    header = json.loads(files["transcript.jsonl"].splitlines()[0])
    assert header["transport"]["resyncs"] == meta["transport"]["resyncs"]
    assert json.loads(files["session.json"])["meta"]["transport"]["resyncs"] == meta["transport"]["resyncs"]
    assert f"{meta['transport']['resyncs']} resynchronisation(s)" in files["report.md"]
    assert any(e["status"] == "timeout" for e in store.exchanges(sid))  # the evidence, kept raw


def test_normal_session_reports_zero_resyncs(store):
    sid = _record_with(store, None)
    meta = store.get_session(sid)["meta"]
    assert meta["transport"]["resyncs"] == 0 and meta["transport"]["reconnects"] == 0
    header = json.loads(build_files(store, sid)["transcript.jsonl"].splitlines()[0])
    assert header["transport"]["resyncs"] == 0


def test_cancelled_recording_still_persists_final_diagnostics(store):
    def script(cmd, n, raw):
        return [(1.0, raw)] if cmd == "010C" else None  # every read stalls

    with FakeElmServer("healthy_idle", script=script) as srv:
        wb = Workbench(store)
        t = VgateWiFiTransport(srv.host, srv.port, backoff_initial_s=0.05)
        wb.attach(t, "simulator", "healthy_idle")
        t.default_timeout_s = 0.3
        started = threading.Event()
        out = {}
        th = threading.Thread(
            target=lambda: out.update(
                sid=wb.record(3600, pids=[0x0C], pre_scan=False, post_scan=False, started=started)
            ),
            daemon=True,
        )
        th.start()
        assert started.wait(5)
        time.sleep(1.5)
        wb.stop_recording()
        th.join(20)
        wb.disconnect()
    meta = store.get_session(out["sid"])["meta"]
    assert meta["transport"]["resyncs"] >= 1
    assert store.get_session(out["sid"])["ended_at"] is not None


def test_session_detail_api_uses_persisted_final_diagnostics(api):
    wb = api.app.state.wb
    sid = _record_with(wb.store, None)
    body = api.get(f"/api/sessions/{sid}").json()
    assert body["meta"]["transport"] == wb.store.get_session(sid)["meta"]["transport"]
