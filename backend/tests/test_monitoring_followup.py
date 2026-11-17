"""PR #2 follow-up: crash-safe monitoring metadata, real/synthetic baseline isolation, linear-time
warm-idle analysis, shutdown episodes, and adapter loss while ECU polling is paused.

All recordings here are SYNTHETIC (simulator / hand-built sample series). Nothing here says
anything about the real MINI or the real Vgate adapter.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from app.acquisition import monitor as monitor_mod
from app.adapters.base import TransportError
from app.adapters.simulator import SimulatorTransport
from app.diagnostics import monitoring as mon
from app.diagnostics.engine import SessionData, analyze_session, load_session
from app.diagnostics.monitoring import (
    MixedReferencePoolError,
    history_for,
    is_monitoring_session,
    monitoring_summary,
    references,
    summary_for,
)
from app.models.domain import Annotation, Reading
from app.obd.allowlist import AllowedCommand
from app.service import Workbench
from app.storage.db import Store
from app.storage.export import build_files
from app.storage.transcript import ingest_transcript, transcript_lines
from app.vehicles.profiles import MINI_R60_DIESEL

MINI = MINI_R60_DIESEL.id
PROFILE = "mini_extended_monitoring"
T0 = 1_750_000_000.0


# -- helpers ---------------------------------------------------------------------------------------


def _reading(pid: int, value: float, t: float, mode: int = 1) -> Reading:
    return Reading(
        mode=mode, pid=pid, name=f"pid {pid:02X}", value=value, raw_hex="00", t_wall=t, t_mono=t - T0
    )


def idle_samples(t_start: float, seconds: float, coolant: float, rpm: float = 800.0, step: float = 2.0):
    """Warm stationary idle, one sample of each signal every ``step`` seconds."""
    out = []
    t = t_start
    while t < t_start + seconds:
        out += [
            _reading(0x0C, rpm, t),
            _reading(0x0D, 0.0, t + 0.2),
            _reading(0x05, coolant, t + 0.4),
            _reading(0x10, 9.0, t + 0.6),
            _reading(0x2C, 35.0, t + 0.8),
        ]
        t += step
    return out


def stored_monitoring_session(store: Store, synthetic: bool, coolant: float, start: float, **meta) -> str:
    sid = store.create_session(
        "record",
        MINI,
        "simulator" if synthetic else "real",
        synthetic,
        started_at=start,
        meta={"sampling_plan": {"tiers": None}, "recording_profile": PROFILE, **meta},
    )
    store.add_samples(sid, idle_samples(start, 1500, coolant, rpm=1000.0 if synthetic else 800.0))
    store.update_session(sid, ended_at=start + 1500)
    return sid


def mark(store: Store, sid: str, event_type: str, t: float, origin: str = "manual") -> None:
    store.add_annotation(sid, Annotation(t_wall=t, label=event_type, event_type=event_type, origin=origin))


# -- Task 1: metadata persisted before acquisition ---------------------------------------------------


def test_crash_mid_recording_recovers_a_usable_monitoring_session(tmp_path, monkeypatch):
    path = tmp_path / "live.sqlite3"
    snap = tmp_path / "after-power-loss.sqlite3"
    store = Store(path)
    calls = {"n": 0}
    real = monitor_mod.MonitoringRecorder._absorb

    def absorb(self, item, rs, t0):
        calls["n"] += 1
        if calls["n"] == 600:
            # power loss here: what survives is exactly what was COMMITTED to SQLite so far
            src, dst = sqlite3.connect(path), sqlite3.connect(snap)
            src.backup(dst)
            src.close()
            dst.close()
        return real(self, item, rs, t0)

    monkeypatch.setattr(monitor_mod.MonitoringRecorder, "_absorb", absorb)
    wb = Workbench(store)
    wb.set_profile(MINI)
    wb.attach(SimulatorTransport("mini_city_drive", default_timeout_s=2.0), "simulator", "mini_city_drive")
    sid = wb.record(None, PROFILE, until_stop=True, max_duration_s=900, drive_type="city")
    store.close()
    assert snap.exists()

    crashed = Store(snap)
    s = crashed.get_session(sid)
    assert s["ended_at"] is None  # never closed: the process "died"
    # the recording settings were already persisted before acquisition
    m = s["meta"]
    assert m["recording_profile"] == PROFILE and m["until_stop"] is True and m["max_duration_s"] == 900
    assert m["requested_duration_s"] is None and m["user_labels"] == {"drive_type": "city"}
    assert m["selected"]["fast"] and m["sampling_plan"]["tiers"] and is_monitoring_session(s)
    n_samples = len(crashed.samples(sid))
    n_events = len(crashed.annotations(sid))
    assert n_samples > 300 and crashed.exchanges(sid)

    recovered = Workbench(crashed).recover_interrupted(stale_after_s=0)
    assert recovered == [sid]
    s = crashed.get_session(sid)
    m = s["meta"]
    assert s["ended_at"] and m["interrupted"] is True and m["aborted"].startswith("interrupted")
    # recovery merged into, and did not replace, the persisted settings
    assert m["recording_profile"] == PROFILE and m["user_labels"] == {"drive_type": "city"}
    assert m["sampling_plan"]["tiers"] and m["metrics"] and is_monitoring_session(s)
    assert len(crashed.samples(sid)) == n_samples and len(crashed.annotations(sid)) == n_events

    hist = history_for(crashed, MINI)
    row = next(r for r in hist["sessions"] if r["id"] == sid)
    assert row["interrupted"] is True and row["drive_type"] == "city"
    summary = summary_for(crashed, sid)
    assert summary["is_monitoring_session"] and summary["drive"]["drive_type_user_label"] == "city"
    assert summary["signals"] and summary["engine_runs"]
    files = build_files(crashed, sid)
    for name in ("monitoring.json", "events.jsonl", "sampling_quality.json", "capabilities.json"):
        assert name in files
    assert '"interrupted": true' in files["session.json"]
    crashed.close()


def test_drive_type_changed_during_recording_is_not_overwritten_at_the_end(store, monkeypatch):
    real = monitor_mod.MonitoringRecorder._absorb
    calls = {"n": 0}
    holder: dict[str, Workbench] = {}

    def absorb(self, item, rs, t0):
        calls["n"] += 1
        if calls["n"] == 50:
            wb = holder["wb"]
            wb.set_drive_type(wb.active["session_id"], "highway")
        return real(self, item, rs, t0)

    monkeypatch.setattr(monitor_mod.MonitoringRecorder, "_absorb", absorb)
    wb = holder["wb"] = Workbench(store)
    wb.set_profile(MINI)
    wb.attach(SimulatorTransport("mini_city_drive", default_timeout_s=2.0), "simulator", "x")
    sid = wb.record(None, PROFILE, until_stop=True, max_duration_s=120, drive_type="city")
    assert store.get_session(sid)["meta"]["user_labels"] == {"drive_type": "highway"}


def test_classic_duration_recording_still_persists_its_settings(store):
    wb = Workbench(store)
    wb.set_profile(MINI)
    wb.attach(SimulatorTransport("healthy_idle", default_timeout_s=2.0), "simulator", "healthy_idle")
    sid = wb.record(20, "idle")
    m = store.get_session(sid)["meta"]
    assert m["requested_duration_s"] == 20 and m["until_stop"] is False and m["recording_profile"] == "idle"
    assert "sampling_plan" not in m and not is_monitoring_session(store.get_session(sid))


# -- Task 2: real and synthetic baselines never mix ----------------------------------------------------


def test_synthetic_sessions_never_move_the_real_vehicle_baseline(store):
    real = [stored_monitoring_session(store, False, 75.0, T0 + i * 10_000) for i in range(2)]
    current = stored_monitoring_session(store, False, 70.0, T0 + 30_000)
    before = summary_for(store, current)
    findings_before = [f.model_dump() for f in analyze_session(store, current)]
    assert before["references"]["usual_warm_coolant"]["value"] == 75.0
    assert before["references"]["warm_idle_baseline"]["rpm"] == 800.0
    assert before["references"]["cohort"]["origin"] == "real"

    synthetic = [stored_monitoring_session(store, True, 95.0, T0 + 40_000 + i * 10_000) for i in range(3)]
    after = summary_for(store, current)
    assert after["references"] == before["references"]
    assert after["coolant"] == before["coolant"] and after["dpf"] == before["dpf"]
    # 70 °C is within the margin of the REAL usual 75 °C: no finding. Pooled with the synthetic
    # 95 °C sessions (median 85 °C) it would have been reported as "below usual temperature".
    assert not [d for d in after["coolant"]["detected"] if d["type"] == "coolant_below_usual_temperature"]
    assert [f.model_dump() for f in analyze_session(store, current)] == findings_before

    for sid in real:
        assert summary_for(store, sid)["references"]["usual_warm_coolant"]["value"] == 75.0
    syn = summary_for(store, synthetic[0])
    assert syn["references"]["usual_warm_coolant"]["value"] == 95.0
    assert syn["references"]["warm_idle_baseline"]["rpm"] == 1000.0 and syn["baseline_origin"] == "synthetic"

    hist = history_for(store, MINI)
    assert hist["references"]["usual_warm_coolant"]["value"] == 75.0
    assert hist["references"]["cohort"]["origin"] == "real"
    by = {c["cohort"]["origin"]: c for c in hist["references_by_cohort"]}
    assert by["real"]["usual_warm_coolant"]["value"] == 75.0 and by["real"]["sessions"] == 3
    assert by["synthetic"]["usual_warm_coolant"]["value"] == 95.0 and by["synthetic"]["sessions"] == 3
    rows = {r["id"]: r for r in hist["sessions"]}
    assert {rows[s]["baseline_origin"] for s in real + [current]} == {"real"}
    assert {rows[s]["baseline_origin"] for s in synthetic} == {"synthetic"}

    # removing the synthetic sessions again changes nothing for the real vehicle
    with store.tx() as db:
        for sid in synthetic:
            db.execute("DELETE FROM samples WHERE session_id=?", (sid,))
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
    assert summary_for(store, current)["references"] == before["references"]


def test_references_refuse_a_mixed_pool_and_keep_confirmed_vehicles_apart(store):
    a = stored_monitoring_session(store, False, 75.0, T0)
    b = stored_monitoring_session(store, True, 95.0, T0 + 10_000)
    with pytest.raises(MixedReferencePoolError):
        references([load_session(store, a), load_session(store, b)])
    refs_real = references([load_session(store, a)])
    with pytest.raises(MixedReferencePoolError):
        monitoring_summary(load_session(store, b), refs=refs_real)

    car1 = stored_monitoring_session(store, False, 75.0, T0 + 20_000, vehicle_identity="vin:CAR1")
    car2 = stored_monitoring_session(store, False, 90.0, T0 + 30_000, vehicle_identity="vin:CAR2")
    assert summary_for(store, car1)["references"]["usual_warm_coolant"] == {"value": 75.0, "sessions": 1}
    assert summary_for(store, car2)["references"]["usual_warm_coolant"] == {"value": 90.0, "sessions": 1}
    # an unconfirmed session is not assumed to be either car
    assert summary_for(store, a)["references"]["usual_warm_coolant"] == {"value": 75.0, "sessions": 1}
    # identities are exposed only as a digest, never as the VIN itself
    cohorts = history_for(store, MINI)["references_by_cohort"]
    assert all("CAR" not in str(c["cohort"]) for c in cohorts) and len(cohorts) == 4


def test_replays_keep_the_origin_of_their_data(store):
    real = stored_monitoring_session(store, False, 75.0, T0)
    rid = ingest_transcript(transcript_lines(store, real), store, label="replay of real")
    assert (
        store.get_session(rid)["synthetic"] is False and mon.baseline_origin(store.get_session(rid)) == "real"
    )
    syn = stored_monitoring_session(store, True, 95.0, T0 + 10_000)
    sid = ingest_transcript(transcript_lines(store, syn), store, label="replay of sim")
    assert store.get_session(sid)["synthetic"] is True
    assert mon.baseline_origin(store.get_session(sid)) == "synthetic"


# -- Task 4: linear-time warm-idle analysis ----------------------------------------------------------


def _naive_warm_idle(sd: SessionData, plateau_c):
    """The previous implementation (repeated linear search), kept here as the reference."""

    def at(series, t):
        prev = None
        for s in series:
            if s.t > t:
                break
            prev = s.v
        return prev

    rpm, speed, cool = mon._series(sd, mon.RPM), mon._series(sd, mon.SPEED), mon._series(sd, mon.COOLANT)
    if not rpm or not speed or not cool:
        return {}
    ref = plateau_c if plateau_c is not None else max(s.v for s in cool)
    if ref < mon.WARM_MIN_PLATEAU_C:
        return {}

    def ok(t):
        r, v, c = at(rpm, t), at(speed, t), at(cool, t)
        return r is not None and r >= 300 and v is not None and v <= 0 and c is not None and c >= ref - 10

    return {pid: [s for s in mon._series(sd, pid) if ok(s.t)] for pid in (mon.RPM, mon.MAF, mon.EGR_CMD)}


@pytest.mark.parametrize(
    "scenario", ["mini_dpf_regen_possible", "mini_shutdown_during_regen", "mini_city_drive", "mini_warmup"]
)
def test_warm_idle_is_equivalent_to_the_previous_algorithm(store, scenario):
    wb = Workbench(store)
    wb.set_profile(MINI)
    wb.attach(SimulatorTransport(scenario, default_timeout_s=2.0), "simulator", scenario)
    sid = wb.record(None, PROFILE, until_stop=True, max_duration_s=900)
    sd = load_session(store, sid)
    for plateau in (None, 80.0, 88.0):
        fresh = load_session(store, sid)
        assert mon._warm_idle(fresh, plateau) == _naive_warm_idle(sd, plateau)
    m = summary_for(store, sid)
    assert m["dpf"]["baseline"] == mon.regen_baseline(
        [load_session(store, sid)], m["references"]["usual_warm_coolant"]["value"]
    )


def _long_session(seconds: float) -> SessionData:
    samples = [r.model_dump() for r in idle_samples(T0, seconds, 85.0, step=1.0)]
    session = {
        "id": f"long-{seconds}",
        "vehicle_profile": MINI,
        "synthetic": True,
        "started_at": T0,
        "meta": {},
    }
    return SessionData(session, samples, [], [])


def _cursor_steps(monkeypatch, sd: SessionData) -> tuple[int, int]:
    made: list[mon._Cursor] = []

    class Counting(mon._Cursor):
        def __init__(self, series):
            super().__init__(series)
            made.append(self)

    monkeypatch.setattr(mon, "_Cursor", Counting)
    out = mon._warm_idle(sd, 85.0)
    return sum(c.steps for c in made), sum(len(v) for v in out.values())


def test_warm_idle_lookup_is_linear_not_quadratic(monkeypatch):
    small, big = _long_session(5_000), _long_session(20_000)  # 25k and 100k samples
    steps_small, kept_small = _cursor_steps(monkeypatch, small)
    steps_big, kept_big = _cursor_steps(monkeypatch, big)
    n_small = len(small.samples)
    # every lookup pointer only moves forward: total work bounded by the series lengths
    # (the very first RPM sample precedes the first speed sample, so it cannot be judged)
    assert steps_small <= 3 * 3 * n_small and kept_small == 3 * 5_000 - 1
    assert kept_big == 3 * 20_000 - 1
    # 4x the data -> ~4x the work (a quadratic search would be ~16x); deterministic count, no timing
    assert 3.5 <= steps_big / steps_small <= 4.5


def test_derived_series_are_memoised_per_session(monkeypatch):
    sd = _long_session(2_000)
    built = {"n": 0}
    real_cursor = mon._Cursor

    class Spy(real_cursor):
        def __init__(self, series):
            built["n"] += 1
            super().__init__(series)

    monkeypatch.setattr(mon, "_Cursor", Spy)
    refs = references([sd])
    n_after_refs = built["n"]
    monitoring_summary(sd, refs=refs)
    monitoring_summary(sd, refs=refs)
    assert n_after_refs > 0 and built["n"] == n_after_refs  # warm idle never recomputed
    assert mon.engine_runs(sd) is not mon.engine_runs(sd)  # callers get fresh dicts


# -- Task 5: shutdown episodes --------------------------------------------------------------------


def _rpm_session(store: Store, segments: list[tuple[float, float, float]]) -> str:
    """segments: (t_from, t_to, rpm) relative to T0, one RPM sample per second, synthetic."""
    sid = store.create_session(
        "record", MINI, "simulator", True, started_at=T0, meta={"sampling_plan": {"tiers": None}}
    )
    rs = []
    for a, b, rpm in segments:
        rs += [_reading(0x0C, rpm, T0 + t) for t in range(int(a), int(b))]
    store.add_samples(sid, rs)
    store.update_session(sid, ended_at=T0 + segments[-1][1])
    return sid


def test_one_physical_shutdown_is_one_episode(store):
    _, sid, _ = _monitor(store, "mini_shutdown_during_regen", 1100)
    stop = next(a for a in store.annotations(sid) if a.event_type == "engine_stopped")
    lost = next(a for a in store.annotations(sid) if a.event_type == "ecu_communication_lost")
    t = stop.t_wall
    mark(store, sid, "fan_running", t + 5)
    mark(store, sid, "engine_stopped", t + 8)  # the person also marked the stop they heard
    mark(store, sid, "ignition_off", t + 20)
    mark(store, sid, "fan_stopped", t + 240)
    assert 20 < lost.t_wall - t < 60  # the simulator ECU goes silent ~30 s after the stop

    m = summary_for(store, sid)
    assert len(m["shutdowns"]) == 1
    ep = m["shutdowns"][0]
    assert ep["origin"] == "observed" and ep["t_wall"] == pytest.approx(t, abs=1.5)
    assert ep["engine_stop"]["origin"] == "observed" and ep["ignition_off"]["origin"] == "manual"
    assert ep["engine_stop_marked"] == pytest.approx(t + 8)
    assert ep["ecu_communication_lost"]["t_wall"] == lost.t_wall
    assert [(e["event_type"], e["origin"]) for e in ep["events"]] == [
        ("engine_stopped", "observed"), ("engine_stopped", "manual"), ("ignition_off", "manual"),
        ("ecu_communication_lost", "observed"),
    ]  # fmt: skip
    # all original events remain in the timeline, with their own origin and time
    types = [(e["event_type"], e["origin"]) for e in m["events"]]
    for want in [
        ("engine_stopped", "observed"), ("fan_running", "manual"), ("engine_stopped", "manual"),
        ("ignition_off", "manual"), ("ecu_communication_lost", "observed"), ("fan_stopped", "manual"),
    ]:  # fmt: skip
        assert want in types
    fan = m["fan"]["runs"]
    assert len(fan) == 1 and fan[0]["duration_s"] == pytest.approx(235, abs=0.5)
    assert fan[0]["shutdown_episode"] == 0 and fan[0]["seconds_after_shutdown"] == pytest.approx(5, abs=1.5)
    assert len(m["coolant"]["after_shutdown"]) == 1 and len(m["voltage"]["adapter"]["shutdowns"]) == 1
    intr = m["dpf"]["possible_interrupted_regenerations"]
    assert len(intr) == 1 and intr[0]["shutdown_episode"] == 0 and intr[0]["ended_by"] == "engine stop"
    assert intr[0]["fan_runs_after_s"] == [pytest.approx(235, abs=0.5)]
    row = next(r for r in history_for(store, MINI)["sessions"] if r["id"] == sid)
    assert row["engine_stops"] == 1 and row["fan_runs_after_shutdown"] == 1


def test_two_shutdowns_separated_by_a_restart_stay_two(store):
    sid = _rpm_session(store, [(0, 100, 800), (100, 200, 0), (200, 300, 800), (300, 400, 0)])
    mark(store, sid, "engine_stopped", T0 + 101)
    mark(store, sid, "ignition_off", T0 + 110)
    mark(store, sid, "fan_running", T0 + 120)
    mark(store, sid, "fan_stopped", T0 + 180)
    mark(store, sid, "engine_stopped", T0 + 301)  # only 200 s later, but the engine ran in between
    mark(store, sid, "ignition_off", T0 + 310)
    mark(store, sid, "fan_running", T0 + 320)
    mark(store, sid, "fan_stopped", T0 + 390)
    m = summary_for(store, sid)
    eps = m["shutdowns"]
    assert len(eps) == 2 and [e["origin"] for e in eps] == ["observed", "observed"]
    assert eps[0]["t_next_start_wall"] == pytest.approx(T0 + 200)
    assert [f["shutdown_episode"] for f in m["fan"]["runs"]] == [0, 1]
    assert [round(f["duration_s"]) for f in m["fan"]["runs"]] == [60, 70]


def test_manual_only_markers_split_on_a_marked_restart_and_ecu_loss_alone_is_no_shutdown(store):
    sid = _rpm_session(store, [(0, 50, 0)])  # engine never measured running
    mark(store, sid, "ignition_off", T0 + 10)
    mark(store, sid, "engine_started", T0 + 60)
    mark(store, sid, "engine_stopped", T0 + 120)
    # an ECU going silent with no stop observed or marked anywhere near it
    mark(store, sid, "ecu_communication_lost", T0 + 2_000, origin="observed")
    m = summary_for(store, sid)
    eps = m["shutdowns"]
    assert len(eps) == 2
    assert eps[0]["engine_stop"] is None and "ignition-off reported" in eps[0]["basis"]
    assert eps[1]["engine_stop"]["origin"] == "manual"
    assert all(e["ecu_communication_lost"] is None for e in eps[:1])


def test_ecu_loss_while_running_never_becomes_a_shutdown(store):
    _, sid, _ = _monitor(store, "mini_ecu_silent_running", 200)
    m = summary_for(store, sid)
    assert m["shutdowns"] == []


# -- Task 6: Wi-Fi loss while ECU polling is paused -----------------------------------------------------


class WifiOutage(SimulatorTransport):
    """Simulator whose Wi-Fi is down for [down_from, down_to) seconds of (virtual) time:
    requests in flight are cut, reconnects fail. Logs every command with its time."""

    def __init__(self, scenario: str, down_from: float, down_to: float, **kw) -> None:
        super().__init__(scenario, **kw)
        self.down = (down_from, down_to)
        self.sent: list[tuple[float, str]] = []
        self.reconnect_failures = 0

    def _wifi_down(self) -> bool:
        return self.down[0] <= self.clock.monotonic() < self.down[1]

    def _ensure_connected(self) -> None:
        if self.state != "connected" and self._wifi_down():
            self.clock.sleep(1.0)  # connect timeout
            self.reconnect_failures += 1
            raise TransportError("cannot reach adapter (emulated Wi-Fi outage)")
        super()._ensure_connected()

    def _roundtrip(self, wire: bytes, timeout_s: float):
        self.sent.append((self.clock.monotonic(), wire.decode("ascii").strip()))
        if self._wifi_down():
            self.clock.sleep(0.2)
            self.state = "disconnected"
            return b"", "closed"
        return super()._roundtrip(wire, timeout_s)


def _monitor(store, scenario, duration, transport=None, **kw):
    wb = Workbench(store)
    wb.set_profile(MINI)
    t = transport or SimulatorTransport(scenario, default_timeout_s=2.0, seed=1)
    wb.attach(t, "simulator", scenario)
    sid = wb.record(None, PROFILE, until_stop=True, max_duration_s=duration, **kw)
    return wb, sid, t


def test_wifi_drop_while_ecu_unresponsive_sends_no_vehicle_request(store):
    # mini_ignition_off: engine runs, stops at ~65 s, ECU silent from ~85 s; Wi-Fi down 200-330 s
    t = WifiOutage("mini_ignition_off", 200.0, 330.0, default_timeout_s=2.0)
    _, sid, _ = _monitor(store, "mini_ignition_off", 600, transport=t)
    s = store.get_session(sid)
    ev = store.annotations(sid)
    kinds = [a.event_type for a in ev if a.origin == "observed"]
    lost_ecu = next(a for a in ev if a.event_type == "ecu_communication_lost")
    lost_ad = [a for a in ev if a.event_type == "adapter_connection_lost"]
    back_ad = [a for a in ev if a.event_type == "adapter_connection_restored"]

    # 1-4: engine ran, stopped, ECU went silent, then adapter-local ATRV only
    assert kinds.index("engine_stopped") < kinds.index("ecu_communication_lost")
    t_lost = lost_ecu.t_mono
    after = [cmd for tm, cmd in t.sent if tm > t_lost]
    assert after and set(after) == {"ATRV"}, sorted(set(after))  # no ATZ/0100/01xx after the ECU went silent
    for _, cmd in t.sent:
        AllowedCommand.parse(cmd)  # PR #1 allowlist still holds for everything sent
    # 5: Wi-Fi drop recorded once, while paused
    assert len(lost_ad) == 1 and lost_ad[0].evidence["ecu_polling_paused"] is True
    assert 200 <= lost_ad[0].t_mono < 215 and lost_ad[0].evidence["while"] == "ecu_unresponsive"
    assert t.reconnect_failures >= 2
    # 6: adapter back (adapter-only reconnect), polling still paused
    assert len(back_ad) == 1 and back_ad[0].evidence["ecu_polling_paused"] is True
    assert 330 <= back_ad[0].t_mono < 400 and back_ad[0].evidence["gap_s"] > 100
    assert "NOT re-initialised" in back_ad[0].note
    assert not [a for a in ev if a.event_type == "ecu_communication_restored"]
    # ATRV readings resumed after the gap; disconnected attempts kept with no value
    rows = store.samples(sid)
    atrv = [r for r in rows if r["mode"] == 0]
    assert any(r["t_mono"] > back_ad[0].t_mono and r["quality"] == "ok" for r in atrv)
    assert any(r["quality"] == "disconnected" and r["value"] is None for r in atrv)
    assert sum(1 for r in rows if r["mode"] == 1 and r["quality"] == "ok") > 50  # earlier samples intact
    # 7: finalisation succeeded with consistent state bookkeeping
    meta = s["meta"]
    assert s["ended_at"] and "aborted" not in meta and "post_scan_skipped" in meta
    summary = meta["monitoring"]
    assert summary["final_state"] == "ecu_unresponsive" and summary["ecu_polling_paused_at_end"] is True
    assert summary["adapter_connection_lost_at_end"] is None
    assert (
        summary["adapter_connection_losses_while_ecu_paused"] == 1 and summary["adapter_only_reconnects"] == 1
    )
    tis = summary["time_in_state_s"]
    assert tis["adapter_unreachable"] >= 120 and tis["ecu_unresponsive"] > 0
    assert sum(tis.values()) == pytest.approx(600, abs=5)
    link = summary_for(store, sid)["link"]
    assert [x["type"] for x in link] == [
        "ecu_communication_lost", "adapter_connection_lost", "adapter_connection_restored",
    ]  # fmt: skip


def test_recording_stopped_while_adapter_still_unreachable_keeps_the_open_gap(store):
    t = WifiOutage("mini_ignition_off", 200.0, 10_000.0, default_timeout_s=2.0)
    _, sid, _ = _monitor(store, "mini_ignition_off", 500, transport=t)
    lost = next(a for a in store.annotations(sid) if a.event_type == "ecu_communication_lost")
    assert {cmd for tm, cmd in t.sent if tm > lost.t_mono} == {"ATRV"}
    meta = store.get_session(sid)["meta"]
    summary = meta["monitoring"]
    assert summary["final_state"] == "adapter_unreachable" and summary["ecu_polling_paused_at_end"]
    gap = summary["adapter_connection_lost_at_end"]
    assert gap and gap["open_gap_s"] > 250
    assert "post_scan_skipped" in meta and "aborted" not in meta
    assert not [a for a in store.annotations(sid) if a.event_type == "adapter_connection_restored"]


def test_resume_is_refused_while_the_adapter_is_unreachable(store):
    wb = Workbench(store)
    wb.active = {
        "session_id": "x",
        "monitoring": True,
        "monitor": {"state": "adapter_unreachable", "ecu_polling_paused": True},
    }
    r = wb.resume_ecu_polling()
    assert r["requested"] is False and "unreachable" in r["detail"] and not wb._resume.is_set()
    wb.active["monitor"] = {
        "state": "ecu_unresponsive",
        "ecu_polling_paused": True,
        "resume_requires_reinit": True,
    }
    r = wb.resume_ecu_polling()
    assert r["requested"] is True and "re-initialised" in r["detail"] and wb._resume.is_set()


def test_manual_resume_after_adapter_only_reconnect_reinitialises_first(store, monkeypatch):
    """After an adapter-only reconnect the next vehicle request (an explicit resume) must
    re-initialise the adapter on the new connection before anything else is sent."""
    t = WifiOutage("mini_ignition_off", 200.0, 260.0, default_timeout_s=2.0)
    real = monitor_mod.MonitoringRecorder._step_ecu_lost
    fired = {"done": False}

    def step(self):
        if not fired["done"] and self.clock.monotonic() > 400 and self._adapter_lost_at is None:
            fired["done"] = True
            self.resume.set()  # what POST /api/recording/resume-ecu does
            fired["at"] = self.clock.monotonic()
        return real(self)

    monkeypatch.setattr(monitor_mod.MonitoringRecorder, "_step_ecu_lost", step)
    _, sid, _ = _monitor(store, "mini_ignition_off", 500, transport=t)
    after = [cmd for tm, cmd in t.sent if tm >= fired["at"]]
    # first command of the user-approved probe is the adapter reset, never a bare vehicle request
    assert after[0] == "ATZ" and "0100" in after
    lost = next(a for a in store.annotations(sid) if a.event_type == "ecu_communication_lost")
    between = {cmd for tm, cmd in t.sent if lost.t_mono < tm < fired["at"]}
    assert between == {"ATRV"}


def test_existing_wifi_drop_while_running_still_reinitialises_and_resumes(store):
    _, sid, _ = _monitor(store, "wifi_drop", 30)
    ev = store.annotations(sid)
    lost = [a for a in ev if a.event_type == "adapter_connection_lost"]
    assert lost and all(a.evidence["ecu_polling_paused"] is False for a in lost)
    assert sum(1 for r in store.samples(sid) if r["quality"] == "ok") > 50


@pytest.fixture(autouse=True)
def _fast_clock_guard():
    t = time.monotonic()
    yield
    assert time.monotonic() - t < 120  # virtual-clock tests must not wait in real time
