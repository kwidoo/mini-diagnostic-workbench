import io
import socket
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app


@pytest.fixture
def api(tmp_path):
    app = create_app(str(tmp_path / "api.sqlite3"))
    with TestClient(app) as client:
        yield client


def _wait_idle(api, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if api.get("/api/status").json()["recording"] is None:
            return
        time.sleep(0.1)
    raise AssertionError("recording did not finish")


def test_status_is_read_only_and_disconnected(api):
    st = api.get("/api/status").json()
    assert st["read_only"] is True and st["connected"] is False and st["adapter"] is None


def test_no_endpoint_accepts_adapter_commands(api):
    paths = {r.path for r in api.app.routes}
    assert not any(
        k in p
        for p in paths
        for k in ("command", "raw", "console", "clear", "reset", "actuat", "regen", "write", "coding")
    )
    for path in ("/api/command", "/api/raw", "/api/dtc/clear", "/api/elm"):
        assert api.post(path, json={"command": "04"}).status_code == 404
    assert api.delete("/api/sessions/x").status_code in (404, 405)


def test_profile_selection(api):
    data = api.get("/api/profiles").json()
    assert data["active"] == "mini-countryman-r60-diesel"
    by_id = {p["id"]: p for p in data["profiles"]}
    assert set(by_id) == {"mini-countryman-r60-diesel", "porsche-boxster-986-mk2"}
    assert any(s["name"] == "rough_idle" for s in by_id["mini-countryman-r60-diesel"]["scenarios"])
    assert all(
        p["status"] == "unavailable"
        for p in by_id["porsche-boxster-986-mk2"]["manufacturer"][0]["parameters"]
    )
    assert (
        api.put("/api/profiles/active", json={"id": "porsche-boxster-986-mk2"}).json()["active"]
        == "porsche-boxster-986-mk2"
    )
    assert api.get("/api/status").json()["vehicle_profile"] == "porsche-boxster-986-mk2"
    assert api.put("/api/profiles/active", json={"id": "nope"}).status_code == 404
    v = api.get("/api/vehicle").json()
    v["confirmed"]["engine"] = "3.2"
    assert api.put("/api/vehicle", json=v).json()["confirmed"]["engine"] == "3.2"
    assert api.put("/api/vehicle", json={"x": 1}).status_code == 400


def test_errors_are_clear(api):
    assert api.post("/api/scan", json={}).status_code == 409
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = api.post("/api/connect", json={"mode": "real", "host": "127.0.0.1", "port": port, "timeout_s": 0.5})
    assert r.status_code == 502 and "V-LINK" in r.json()["detail"]
    assert api.post("/api/connect", json={"mode": "simulator", "scenario": "nope"}).status_code == 404
    assert api.get("/api/sessions/nope").status_code == 404
    assert api.post("/api/recording/start", json={"duration_s": 1}).status_code == 409


def test_end_to_end_simulator_flow(api):
    st = api.post("/api/connect", json={"mode": "simulator", "scenario": "dpf_history"}).json()
    assert (
        st["connected"]
        and st["synthetic"]
        and st["adapter"]["is_can"]
        and st["capabilities"]["primary_ecu"] == "7E8"
    )

    scan = api.post("/api/scan", json={"label": "t"}).json()
    kinds = {(d["kind"], d["code"]) for d in scan["scans"][0]["dtcs"]}
    assert ("stored", "P2002") in kinds and ("permanent", "P2002") in kinds
    assert scan["scans"][0]["freeze_frame"]["dtc"] == "P2002"
    assert scan["meta"]["vehicle_info"]["vin_status"] == "not_requested"

    inv = api.post("/api/inventory", json={}).json()
    assert any(r["pid"] == 0x0C and r["value"] for r in inv["readings"])
    assert {x["hex"] for x in inv["raw_only"]} == {"7A", "7C"}
    assert any(x["hex"] == "59" for x in inv["unsupported_priority"])

    plan = api.get("/api/recording/plan").json()
    assert [p["hex"] for p in plan["fast"]] == ["0C", "10", "0B", "23"]

    with api.websocket_connect("/ws/live") as ws:
        assert ws.receive_json()["type"] == "status"
        rec = api.post("/api/recording/start", json={"duration_s": 1.5, "label": "api"}).json()["recording"]
        sid = rec["session_id"]
        assert api.post("/api/recording/start", json={"duration_s": 1}).status_code == 409
        assert (
            api.post(f"/api/sessions/{sid}/annotations", json={"label": "vibration"}).json()["t_mono"]
            is not None
        )
        types = set()
        for _ in range(20):
            for ev in ws.receive_json().get("events", []):
                types.add(ev["type"])
            if "reading" in types:
                break
        assert "reading" in types
    _wait_idle(api)

    sessions = api.get("/api/sessions").json()
    assert sessions[0]["id"] == sid and sessions[0]["synthetic"] is True and len(sessions) == 3
    series = api.get(f"/api/sessions/{sid}/samples").json()
    rpm = next(s for s in series["signals"] if s["hex"] == "0C")
    assert rpm["unit"] == "rpm" and len(rpm["points"]) > 3 and rpm["points"][0][0] >= 0
    assert series["annotations"][0]["label"] == "vibration" and series["annotations"][0]["t"] is not None
    detail = api.get(f"/api/sessions/{sid}").json()
    assert [s["phase"] for s in detail["scans"]] == ["pre", "post"] and detail["meta"]["metrics"]
    fs = {f["id"]: f for f in api.get(f"/api/sessions/{sid}/findings").json()}
    assert fs["dpf_480a"]["strength"] == "suggestive"

    z = api.get(f"/api/sessions/{sid}/export")
    assert z.headers["content-type"] == "application/zip"
    assert any(n.endswith("report.md") for n in zipfile.ZipFile(io.BytesIO(z.content)).namelist())

    cmp_ = api.get("/api/compare", params={"a": scan["id"], "b": sid}).json()
    assert cmp_["dtc_diff"]["both"] and cmp_["same_vehicle_profile"] is True

    assert api.post("/api/disconnect").json()["connected"] is False


def test_stop_recording_early(api):
    api.post("/api/connect", json={"mode": "simulator", "scenario": "healthy_idle"})
    sid = api.post("/api/recording/start", json={"duration_s": 60}).json()["recording"]["session_id"]
    time.sleep(0.5)
    api.post("/api/recording/stop")
    _wait_idle(api)
    s = api.get(f"/api/sessions/{sid}").json()
    assert s["ended_at"] - s["started_at"] < 10 and [x["phase"] for x in s["scans"]] == ["pre", "post"]


def test_replay_fixture_and_compare(api):
    names = [f["name"] for f in api.get("/api/fixtures").json()]
    assert "rough-idle.jsonl" in names and "healthy-idle.jsonl" in names
    a = api.post("/api/replay", json={"fixture": "healthy-idle.jsonl"}).json()
    b = api.post("/api/replay", json={"fixture": "rough-idle.jsonl"}).json()
    assert a["source"] == "replay" and a["synthetic"] is True
    cmp_ = api.get("/api/compare", params={"a": a["id"], "b": b["id"]}).json()
    assert cmp_["dtc_diff"]["only_b"] == ["P0101 (pending)"] and cmp_["dtc_diff"]["only_a"] == []
    assert cmp_["series_a"]["signals"] and cmp_["series_b"]["signals"]
    idle = next(
        f for f in api.get(f"/api/sessions/{b['id']}/findings").json() if f["id"] == "idle_rpm_stability"
    )
    assert idle["strength"] == "strong"
    for bad in ("../backend/pyproject.toml", "/etc/passwd", "nope.jsonl", "..\\x.jsonl"):
        assert api.post("/api/replay", json={"fixture": bad}).status_code == 404
