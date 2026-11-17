import pytest

from app.acquisition.recorder import compute_metrics
from app.adapters.simulator import SimulatorTransport
from app.fixtures_gen import record_scenario
from app.models.domain import Annotation
from app.service import BusyError, NotConnectedError, Workbench
from app.vehicles.profiles import MINI_R60_DIESEL, resolve_schedule


def test_recording_is_timestamped_ordered_and_consistent(store):
    sid = record_scenario(store, "rough_idle", 30, [(5, "vibration")])
    s = store.get_session(sid)
    rows = [r for r in store.samples(sid) if r["mode"] == 1]
    assert s["kind"] == "record" and s["synthetic"] is True and s["ended_at"] > s["started_at"]
    t = [r["t_mono"] for r in rows]
    assert t == sorted(t) and len(set(t)) > 100
    assert all(r["t_wall"] > 1e9 and r["latency_ms"] > 0 for r in rows)
    assert 29 <= t[-1] - t[0] <= 30.5
    for r in rows:  # raw and physical value always travel together
        assert (r["value"] is None) == (r["quality"] not in ("ok",))
        if r["quality"] == "ok":
            assert r["raw_hex"] and r["unit"]


def test_metrics_report_actual_rate_drops_latency_and_gaps(store):
    sid = record_scenario(store, "rough_idle", 30, [])
    metrics = {m["pid"]: m for m in store.get_session(sid)["meta"]["metrics"]}
    rpm = metrics[0x0C]
    assert rpm["attempts"] == rpm["ok"] + rpm["dropped"] and rpm["error_rate"] == 0
    assert 3 < rpm["actual_hz"] < 8 and rpm["latency_ms_p95"] >= rpm["latency_ms_mean"] > 0
    assert rpm["max_gap_s"] < 1
    assert metrics[0x05]["actual_hz"] < rpm["actual_hz"] / 3  # slow-group signal


def test_actual_hz_matches_sample_count(store):
    sid = record_scenario(store, "healthy_idle", 20, [])
    rows = [r for r in store.samples(sid) if r["mode"] == 1 and r["pid"] == 0x0C and r["ecu"] == "7E8"]
    m = next(m for m in compute_metrics(store.samples(sid)) if m.pid == 0x0C)
    span = max(r["t_mono"] for r in store.samples(sid)) - min(r["t_mono"] for r in store.samples(sid))
    assert m.actual_hz == pytest.approx(len(rows) / span, rel=0.01)


def test_unsupported_signals_are_never_polled_and_listed(store):
    sid = record_scenario(store, "unsupported_pids", 10, [])
    meta = store.get_session(sid)["meta"]
    assert meta["selected"]["fast"] == [0x0C]
    assert {0x10, 0x0B, 0x23} <= set(meta["selected"]["unsupported"])
    polled = {r["pid"] for r in store.samples(sid)}
    assert not polled & {0x10, 0x0B, 0x23, 0x59, 0x22}
    assert not [
        x for x in store.exchanges(sid) if x["phase"] == "record" and x["request"] in ("0110", "010B", "0123")
    ]


def test_wifi_drop_is_recorded_as_missing_not_as_values(store):
    sid = record_scenario(store, "wifi_drop", 20, [])
    rows = store.samples(sid)
    dropped = [r for r in rows if r["quality"] == "disconnected"]
    assert dropped and all(r["value"] is None and r["raw_hex"] == "" for r in dropped)
    assert sum(1 for r in rows if r["quality"] == "ok") > len(
        dropped
    )  # recording carried on after reconnects
    m = {m["pid"]: m for m in store.get_session(sid)["meta"]["metrics"]}[0x0C]
    assert m["dropped"] >= 1 and m["error_rate"] > 0 and m["max_gap_s"] > 0.5
    assert any(x["status"] == "closed" for x in store.exchanges(sid))  # partial raw response kept


def test_raw_transcript_phases_and_pre_post_scans(store):
    sid = record_scenario(store, "dpf_history", 5, [])
    phases = [x["phase"] for x in store.exchanges(sid)]
    order = [p for i, p in enumerate(phases) if i == 0 or p != phases[i - 1]]
    assert order == ["init", "discover", "scan:pre", "record", "scan:post"]
    assert [s.phase for s in store.scans(sid)] == ["pre", "post"]
    ex = store.exchanges(sid)[0]
    assert ex["request"] == "ATZ" and ex["response_hex"] and ex["latency_ms"] > 0


def test_resolve_schedule_picks_first_supported_alternative():
    prof = MINI_R60_DIESEL.recording_profile("idle")
    fast, slow, missing = resolve_schedule(prof, {0x0C, 0x10, 0x59, 0x05})
    assert fast == [0x0C, 0x10, 0x59] and slow == [0x05] and 0x0B in missing and 0x23 not in missing


def test_operations_need_a_connection_and_are_exclusive(store):
    wb = Workbench(store)
    with pytest.raises(NotConnectedError):
        wb.scan()
    wb.attach(SimulatorTransport(), "simulator", "healthy_idle")
    assert wb._op.acquire(blocking=False)
    try:
        with pytest.raises(BusyError):
            wb.scan()
    finally:
        wb._op.release()
    with pytest.raises(KeyError):
        wb.record(1, "no-such-profile")


def test_store_transactions_roll_back(store):
    sid = store.create_session("scan", "mini-countryman-r60-diesel", "simulator", True)
    with pytest.raises(RuntimeError):
        with store.tx() as db:
            db.execute(
                "INSERT INTO annotations(session_id,t_wall,label,note) VALUES(?,?,?,?)", (sid, 1.0, "x", "")
            )
            raise RuntimeError("boom")
    assert store.annotations(sid) == []
    store.add_annotation(sid, Annotation(t_wall=2.0, label="MIL on"))
    assert [a.label for a in store.annotations(sid)] == ["MIL on"]
    assert store._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_annotation_during_and_after_recording(store):
    wb = Workbench(store)
    wb.attach(SimulatorTransport(), "simulator", "healthy_idle")
    sid = wb.scan()
    a = wb.annotate(sid, "MIL on", "seen on dash")
    assert a.t_mono is None and store.annotations(sid)[0].note == "seen on dash"
    with pytest.raises(KeyError):
        wb.annotate("nope", "x")
