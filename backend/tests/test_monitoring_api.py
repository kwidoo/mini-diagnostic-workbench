"""Monitoring through the HTTP API and real sockets: start/stop/restart until-stop recordings,
manual events while recording and after the engine stopped, the PR #1 lifecycle lock and
transport resynchronisation during a long recording. SYNTHETIC data only."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.adapters.simulator import FakeElmServer
from app.adapters.tcp import VgateWiFiTransport
from app.api.main import create_app

MINI = "mini-countryman-r60-diesel"
PORSCHE = "porsche-boxster-986-mk2"
PROFILE = "mini_extended_monitoring"


@pytest.fixture
def api(tmp_path):
    app = create_app(str(tmp_path / "api.sqlite3"))
    with TestClient(app) as client:
        yield client


def _attach(api, srv):
    wb = api.app.state.wb
    wb.attach(
        VgateWiFiTransport(srv.host, srv.port, default_timeout_s=2, backoff_initial_s=0.05), "simulator", "x"
    )
    return wb


def _wait(pred, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _start(api, **kw):
    r = api.post("/api/recording/start", json={"profile": PROFILE, "until_stop": True, **kw})
    assert r.status_code == 200, r.text
    return r.json()["recording"]["session_id"]


def _stop(api, sid):
    assert api.post("/api/recording/stop").json() == {"stopping": True}
    _wait(lambda: api.get("/api/status").json()["recording"] is None, what="recording stop")
    assert api.get(f"/api/sessions/{sid}").json()["ended_at"]


def test_plan_reports_tiers_and_adapter_voltage(api):
    with FakeElmServer("mini_dpf_regen_possible") as srv:
        _attach(api, srv)
        p = api.get(f"/api/recording/plan?profile={PROFILE}").json()
        assert p["tiers"] == {"fast": 0.5, "medium": 2.0, "slow": 10.0} and p["monitoring"] is True
        assert p["adapter_voltage"] == "polled"
        assert [x["hex"] for x in p["fast"]] == ["0C", "0D", "04", "10", "0B"]
        assert {"7A", "7C", "78"} <= {x["hex"] for x in p["slow"]}
        assert {"7B", "86"} <= {x["hex"] for x in p["unsupported"]}
        idle = api.get("/api/recording/plan?profile=idle").json()
        assert idle["tiers"] is None and "medium" not in idle  # duration-based profiles unchanged
        api.app.state.wb.disconnect()


def test_until_stop_events_lifecycle_and_restart(api):
    with FakeElmServer("mini_city_drive") as srv:
        wb = _attach(api, srv)
        sid = _start(api, label="city run", drive_type="city", max_duration_s=600)
        st = api.get("/api/status").json()
        assert st["recording"]["until_stop"] is True and st["recording"]["duration_s"] == 600
        _wait(lambda: (api.get("/api/status").json()["recording"] or {}).get("monitor"), what="monitor state")
        # PR #1 lifecycle lock: nothing that changes the connection while recording
        assert api.post("/api/connect", json={"mode": "simulator"}).status_code == 409
        assert api.post("/api/disconnect").status_code == 409
        assert api.put("/api/profiles/active", json={"id": PORSCHE}).status_code == 409
        assert api.post("/api/scan", json={}).status_code == 409
        assert (
            api.post("/api/recording/start", json={"profile": PROFILE, "until_stop": True}).status_code == 409
        )
        # manual events never need the adapter lock
        r = api.post(f"/api/sessions/{sid}/annotations", json={"event_type": "fan_running", "note": "loud"})
        assert (
            r.status_code == 200
            and r.json()["origin"] == "manual"
            and r.json()["event_type"] == "fan_running"
        )
        r = api.post(f"/api/sessions/{sid}/annotations", json={"event_type": "battery_warning"})
        assert r.status_code == 200
        assert api.post(f"/api/sessions/{sid}/annotations", json={"event_type": "nope"}).status_code == 400
        assert api.post(f"/api/sessions/{sid}/annotations", json={}).status_code == 400
        # a person cannot post an observed/inferred event: extra fields are ignored
        r = api.post(f"/api/sessions/{sid}/annotations", json={"label": "x", "origin": "observed"})
        assert r.json()["origin"] == "manual"
        assert api.post("/api/recording/resume-ecu").json()["requested"] is False  # ECU answering
        _wait(lambda: len(api.get(f"/api/sessions/{sid}/samples").json()["signals"]) > 5, what="samples")
        _stop(api, sid)
        # the session can still receive events after it was closed (e.g. fan stops later)
        r = api.post(f"/api/sessions/{sid}/annotations", json={"event_type": "fan_stopped"})
        assert r.status_code == 200 and r.json()["t_mono"] is None
        m = api.get(f"/api/sessions/{sid}/monitoring").json()
        fan = m["fan"]["runs"]
        assert len(fan) == 1 and fan[0]["complete"] and fan[0]["duration_s"] > 0
        assert m["drive"]["drive_type_user_label"] == "city"
        bw = next(w for w in m["voltage"]["battery_warnings"] if w["event"] == "battery_warning")
        assert bw["nearby"]["ecu_voltage"] is not None
        series = api.get(f"/api/sessions/{sid}/samples").json()
        atrv = next(s for s in series["signals"] if s["hex"] == "ATRV")
        assert atrv["provenance"] == "adapter" and atrv["points"]
        assert all(
            a["t"] is not None for a in series["annotations"]
        )  # post-recording marker placed by wall time
        # restart a second recording on the same connection
        sid2 = _start(api)
        assert sid2 != sid
        _wait(lambda: len(api.get(f"/api/sessions/{sid2}/samples").json()["signals"]) > 3, what="samples 2")
        _stop(api, sid2)
        sessions = api.get("/api/sessions").json()
        assert {s["id"] for s in sessions if s["monitoring"]} == {sid, sid2}
        assert next(s for s in sessions if s["id"] == sid)["drive_type"] == "city"
        assert api.put(f"/api/sessions/{sid2}/drive-type", json={"drive_type": "highway"}).json() == {
            "drive_type": "highway"
        }
        assert api.put(f"/api/sessions/{sid2}/drive-type", json={"drive_type": "space"}).status_code == 400
        hist = api.get("/api/monitoring/history").json()
        assert [r["id"] for r in hist["sessions"]] == [sid2, sid]
        assert (
            hist["sessions"][1]["fan_durations_s"] == []
            or hist["sessions"][1]["fan_runs_after_shutdown"] >= 0
        )
        assert hist["sessions"][0]["drive_type"] == "highway"
        # Stop -> disconnect works (lifecycle released)
        assert api.post("/api/disconnect").status_code == 200
        z = api.get(f"/api/sessions/{sid}/export")
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        wb.shutdown()


def test_duration_based_recording_interface_is_unchanged(api):
    with FakeElmServer("healthy_idle") as srv:
        _attach(api, srv)
        r = api.post("/api/recording/start", json={"duration_s": 1, "profile": "idle"})
        assert r.status_code == 200 and r.json()["recording"]["until_stop"] is False
        _wait(lambda: api.get("/api/status").json()["recording"] is None, what="end")
        s = api.get("/api/sessions").json()[0]
        assert s["monitoring"] is False and s["recording_profile"] == "idle"
        api.app.state.wb.disconnect()


def test_resync_during_monitoring_recording_never_decodes_stale_bytes(api):
    """PR #1 interplay: bytes after the prompt (late reply) on a monitoring recording force a
    resync + re-init; the recording carries on and the tainted reply is not a value."""
    stale = b"41 0C 0F A0 \r\r>"
    hits = {"n": 0}

    def script(cmd, n, raw):
        if cmd == "010C":
            hits["n"] += 1
            if hits["n"] == 8:
                return [(0.0, raw + stale)]
        return None

    with FakeElmServer("mini_city_drive", script=script) as srv:
        wb = _attach(api, srv)
        sid = _start(api)
        _wait(lambda: hits["n"] > 12, timeout=20, what="enough RPM requests")
        _stop(api, sid)
        s = api.get(f"/api/sessions/{sid}").json()
        assert s["meta"]["transport"]["resyncs"] >= 1
        assert srv.connections >= 2  # the tainted socket was dropped and a fresh one opened
        exs = wb.store.exchanges(sid)
        bad = [x for x in exs if x["status"] == "desync"]
        assert bad
        rpm = [r for r in wb.store.samples(sid) if r["pid"] == 0x0C and r["t_mono"] == bad[0]["t_mono"]]
        assert rpm and all(r["value"] is None for r in rpm)
        assert all(r["value"] != 1000.0 for r in wb.store.samples(sid) if r["pid"] == 0x0C)
        # observed events on a link resync stay link events, never engine events
        evs = [a.event_type for a in wb.store.annotations(sid)]
        assert "engine_stopped" not in evs
        wb.shutdown()


def test_recovery_runs_at_api_startup(tmp_path):
    from app.storage.db import Store

    db = tmp_path / "rec.sqlite3"
    st = Store(db)
    sid = st.create_session(
        "record", MINI, "real", False, started_at=time.time() - 900, meta={"sampling_plan": {}}
    )
    st.close()
    app = create_app(str(db))
    with TestClient(app) as client:
        s = client.get(f"/api/sessions/{sid}").json()
        assert s["ended_at"] and "interrupted" in s["meta"]["aborted"]
