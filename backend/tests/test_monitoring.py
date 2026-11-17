"""Continuous MINI monitoring (task 2): profile, recorder, events, analysis, export, recovery.

All data here is SYNTHETIC (simulator drive cycles). Passing these tests says nothing
about the real MINI or the real Vgate adapter.
"""

from __future__ import annotations

import io
import json
import time
import zipfile

import pytest

from app.acquisition import monitor as monitor_mod
from app.adapters.simulator import SimulatorTransport
from app.diagnostics.engine import analyze_session, load_session
from app.diagnostics.monitoring import is_monitoring_session, monitoring_summary, summary_for
from app.models.domain import Annotation, Reading
from app.obd.allowlist import AllowedCommand
from app.service import Workbench
from app.storage.export import build_files, build_zip
from app.storage.transcript import ingest_transcript, transcript_lines
from app.vehicles.profiles import (
    DPF_STANDARD_PIDS,
    MINI_MONITORING_EVENTS,
    MINI_R60_DIESEL,
    PORSCHE_986,
    resolve_tiers,
)

MINI = MINI_R60_DIESEL.id
PROFILE = "mini_extended_monitoring"


def monitor(store, scenario, duration, seed=1, **kw):
    wb = Workbench(store)
    wb.set_profile(MINI)
    t = SimulatorTransport(scenario, default_timeout_s=2.0, seed=seed)
    wb.attach(t, "simulator", scenario)
    sid = wb.record(None, PROFILE, until_stop=True, max_duration_s=duration, **kw)
    return wb, sid, t


def mark(store, sid, event_type, seconds_after_first_sample, note=""):
    first = min(s["t_wall"] for s in store.samples(sid))
    store.add_annotation(
        sid,
        Annotation(
            t_wall=first + seconds_after_first_sample,
            label=event_type,
            note=note,
            event_type=event_type,
            origin="manual",
        ),
    )


def events(store, sid, *types):
    return [a for a in store.annotations(sid) if a.event_type in types]


def rel(store, sid, t_wall):
    return t_wall - min(s["t_wall"] for s in store.samples(sid))


# -- profile -------------------------------------------------------------------------------------


def test_profile_exists_with_tiers_and_predefined_events():
    rp = MINI_R60_DIESEL.recording_profile(PROFILE)
    assert rp.tiers is not None and rp.monitoring and rp.adapter_voltage
    assert {0x0C, 0x0D, 0x04, 0x10, 0x0B} == set(rp.fast)
    assert 0x05 in rp.medium and 0x42 in rp.medium and 0x2C in rp.medium and 0x2D in rp.medium
    assert set(DPF_STANDARD_PIDS) <= set(rp.slow)
    ids = [e[0] for e in MINI_MONITORING_EVENTS]
    assert ids == [
        "engine_started", "engine_stopped", "ignition_off", "fan_running", "fan_stopped", "fan_unusually_loud",
        "battery_warning", "battery_warning_cleared", "suspected_dpf_regeneration", "mil_on", "mil_off",
        "rough_idle", "power_loss", "unusual_engine_noise",
    ]  # fmt: skip
    # Porsche profile untouched
    assert all(p.tiers is None for p in PORSCHE_986.recording_profiles) and not PORSCHE_986.monitoring_events


def test_resolve_tiers_never_polls_unsupported_and_lists_them():
    rp = MINI_R60_DIESEL.recording_profile(PROFILE)
    plan = resolve_tiers(rp, {0x0C, 0x0D, 0x05, 0x42, 0x59})
    assert plan["fast"] == [0x0C, 0x0D] and plan["medium"] == [0x05, 0x42, 0x59]
    assert 0x10 in plan["unsupported"] and 0x7A in plan["unsupported"] and 0x23 not in plan["unsupported"]


# -- continuous recording --------------------------------------------------------------------------


def test_city_drive_engine_events_shutdown_and_ecu_loss(store):
    _, sid, t = monitor(store, "mini_city_drive", 800, drive_type="city")
    s = store.get_session(sid)
    meta = s["meta"]
    assert s["ended_at"] and meta["until_stop"] is True and meta["requested_duration_s"] is None
    assert meta["user_labels"] == {"drive_type": "city"} and is_monitoring_session(s)
    assert [a.event_type for a in events(store, sid, "engine_started", "engine_stopped", "ecu_communication_lost")] == [
        "engine_started", "engine_stopped", "ecu_communication_lost",
    ]  # fmt: skip
    stop = events(store, sid, "engine_stopped")[0]
    assert stop.origin == "observed" and stop.evidence["last_running_rpm"]["value"] >= 300
    assert stop.evidence["coolant_c"]["value"] > 60 and stop.evidence["ecu_voltage_v"]["value"] > 13
    lost = events(store, sid, "ecu_communication_lost")[0]
    assert "NOT proof" in lost.note and lost.origin == "observed"
    # never infers ignition off from communication loss
    assert not events(store, sid, "ignition_off")
    # no scan sent to a silent ECU
    assert "post_scan_skipped" in meta and [sc.phase for sc in store.scans(sid)] == ["pre"]
    caps = meta["monitoring"]["observed_capabilities"]
    assert caps["engine_stops_observed"] == 1 and caps["ecu_answers_after_engine_stop"] > 0
    assert caps["adapter_voltage_reads_while_ecu_unresponsive"] > 0
    assert "manual" in caps["fan_activity"]


def test_no_vehicle_request_while_ecu_unresponsive_only_adapter_voltage(store):
    _, sid, t = monitor(store, "mini_ignition_off", 600)
    lost = events(store, sid, "ecu_communication_lost")[0]
    after = [x for x in store.exchanges(sid) if x["t_wall"] > lost.t_wall and x["phase"] == "record"]
    assert after and {x["request"] for x in after} == {"ATRV"}
    # every command the fake adapter ever received is on the read-only allowlist
    for cmd in t.sim.received:
        AllowedCommand.parse(cmd)
    assert not t.sim.dtcs_cleared
    m = summary_for(store, sid)
    ad = m["voltage"]["adapter"]
    assert ad["status"] == "measured" and ad["shutdowns"][0]["after"]["n"] > 5
    assert ad["provenance"].startswith("adapter") and m["voltage"]["ecu"]["provenance"].startswith("ECU")


def test_ecu_silent_while_running_is_not_reported_as_engine_stop(store):
    _, sid, _ = monitor(store, "mini_ecu_silent_running", 200)
    assert not events(store, sid, "engine_stopped", "ignition_off")
    lost = events(store, sid, "ecu_communication_lost")[0]
    assert lost.evidence["engine_last_seen"] == "running" and "engine stop was not observed" in lost.note
    m = summary_for(store, sid)
    assert m["shutdowns"] == [] and m["engine_runs"][0]["stop_observed"] is False


def test_opt_in_probe_backs_off_and_manual_resume(store):
    _, sid, _ = monitor(store, "mini_ecu_silent_running", 900, resume_probe_s=60)
    probes = store.get_session(sid)["meta"]["monitoring"]["observed_capabilities"]["ecu_probes"]
    assert 2 <= probes <= 5  # 60, 120, 240, 480 s back-off within ~830 s of silence
    _, sid2, _ = monitor(store, "mini_ecu_silent_running", 600)
    assert store.get_session(sid2)["meta"]["monitoring"]["observed_capabilities"]["ecu_probes"] == 0


def test_wifi_drop_records_link_events_and_keeps_recording(store):
    _, sid, _ = monitor(store, "wifi_drop", 30)
    lost = events(store, sid, "adapter_connection_lost")
    restored = events(store, sid, "adapter_connection_restored")
    assert lost and restored and all(r.evidence["gap_s"] > 0 for r in restored)
    rows = store.samples(sid)
    assert any(r["quality"] == "disconnected" and r["value"] is None for r in rows)
    assert sum(1 for r in rows if r["quality"] == "ok") > 50
    assert store.get_session(sid)["meta"]["transport"]["resyncs"] >= 1


def test_adaptive_sampling_stretches_low_tiers_first_on_a_slow_adapter(store):
    _, sid, _ = monitor(store, "slow_adapter", 240)
    mon = store.get_session(sid)["meta"]["monitoring"]
    assert mon["adjustments"], "a slow adapter must trigger an adjustment"
    adj = mon["adjustments"][-1]
    assert adj["scale"]["low"] >= adj["scale"]["fast"] >= 1.0
    assert adj["capacity_rps"] < 5
    eff = mon["final_effective_intervals"]
    assert eff["slow"] > mon["base_intervals"]["slow"]
    # every sample keeps its own timestamp; nothing is duplicated or filled in
    rows = [r for r in store.samples(sid) if r["mode"] == 1 and r["pid"] == 0x0C and r["ecu"] == "7E8"]
    assert len({r["t_mono"] for r in rows}) == len(rows)
    q = summary_for(store, sid)["sampling"]
    rpm = next(x for x in q["signals"] if x["pid"] == 0x0C and x.get("mode", 1) == 1)
    assert rpm["tier"] == "fast" and rpm["target_hz_base"] == 2.0


def test_healthy_adapter_needs_no_adjustment_and_unsupported_dpf_pids_are_not_polled(store):
    _, sid, _ = monitor(store, "healthy_idle", 120)
    mon = store.get_session(sid)["meta"]["monitoring"]
    assert mon["adjustments"] == [] and mon["final_scale"] == {"fast": 1.0, "low": 1.0}
    requests = {x["request"] for x in store.exchanges(sid) if x["phase"] == "record"}
    assert not any(f"01{p:02X}" in requests for p in DPF_STANDARD_PIDS)
    assert "ATRV" in requests


def test_pid_answering_no_data_is_suspended_not_polled_forever(store):
    import dataclasses

    from app.adapters.simulator import SCENARIOS

    base = SCENARIOS["healthy_idle"]

    def signals(t, rng):
        v = base.signals(t, rng)
        return v

    sc = dataclasses.replace(base, name="dead_7a", raw_only_pids=(0x7A,))
    wb = Workbench(store)
    wb.set_profile(MINI)
    tr = SimulatorTransport(sc, default_timeout_s=2.0)
    # 7A is advertised but answers NO DATA
    orig = tr.sim._obd

    def obd(cmd):
        return ["NO DATA"] if cmd == "017A" else orig(cmd)

    tr.sim._obd = obd
    wb.attach(tr, "simulator", "dead_7a")
    sid = wb.record(None, PROFILE, until_stop=True, max_duration_s=300)
    reqs = [x for x in store.exchanges(sid) if x["request"] == "017A" and x["phase"] == "record"]
    assert 5 <= len(reqs) <= 9  # 5 tries, then 60 s, 120 s back-off ... not every 10 s
    mon = store.get_session(sid)["meta"]["monitoring"]
    assert "01:7A" in mon["suspended_at_end"]
    assert not events(store, sid, "ecu_communication_lost")


# -- analysis ----------------------------------------------------------------------------------


def test_highway_drive_has_no_possible_regeneration(store):
    _, sid, _ = monitor(store, "mini_highway_drive", 1300, drive_type="highway")
    m = summary_for(store, sid)
    assert m["drive"]["max_speed_kmh"] >= 100 and m["drive"]["moving_share"] > 0.5
    assert m["dpf"]["possible_regenerations"] == []


def test_warmup_without_history_reports_limitation_not_a_fault(store):
    _, sid, _ = monitor(store, "mini_warmup", 600)
    c = summary_for(store, sid)["coolant"]
    assert c["status"] == "measured" and c["max_running"] > c["first"]["value"] + 30
    assert c["usual_warm_c"] is None and c["detected"] == [] and c["limitations"]
    f = {x.id: x for x in analyze_session(store, sid)}["monitoring_cooling"]
    assert f.strength == "insufficient"


def test_possible_regeneration_is_inferred_never_confirmed(store):
    _, sid, _ = monitor(store, "mini_dpf_regen_possible", 1000)
    m = summary_for(store, sid)
    regens = m["dpf"]["possible_regenerations"]
    assert len(regens) == 1
    r = regens[0]
    assert r["status"] == "possible" and r["origin"] == "inferred" and r["confidence"] in ("low", "moderate")
    assert 360 <= r["t_start_rel_s"] <= 460 and r["t_end_rel_s"] - r["t_start_rel_s"] >= 240
    assert set(r["indicators"]) == {"egr_nearly_closed", "idle_rpm_raised", "maf_raised"}
    assert m["dpf"]["confirmed_regenerations"] == []
    avail = {
        i["key"]: i["status"] for g in m["dpf"]["availability"] if g["group"] == "wanted" for i in g["items"]
    }
    assert avail["differential_pressure"] == "raw_only" and avail["exhaust_temperatures"] == "raw_only"
    assert avail["soot_mass"] == "not_available" and avail["regeneration_state"] == "not_available"
    raw = [s for s in store.samples(sid) if s["pid"] == 0x7A and s["mode"] == 1]
    assert raw and all(s["quality"] == "raw_only" and s["value"] is None and s["raw_hex"] for s in raw)
    # not stored as a timeline event: detections are recomputed, never persisted as facts
    assert not [a for a in store.annotations(sid) if a.origin == "inferred"]


def test_shutdown_during_regen_with_fan_after_run(store):
    _, sid, _ = monitor(store, "mini_shutdown_during_regen", 1100)
    stop = events(store, sid, "engine_stopped")[0]
    t_stop = rel(store, sid, stop.t_wall)
    mark(store, sid, "fan_running", t_stop + 5, "fan audible")
    mark(store, sid, "fan_stopped", t_stop + 245)
    mark(store, sid, "ignition_off", t_stop + 20)
    m = summary_for(store, sid)
    fan = m["fan"]["runs"]
    assert len(fan) == 1 and fan[0]["duration_s"] == pytest.approx(240, abs=0.5)
    assert fan[0]["after_shutdown"] and fan[0]["seconds_after_shutdown"] == pytest.approx(5, abs=0.5)
    assert fan[0]["origin"] == "manual"
    intr = m["dpf"]["possible_interrupted_regenerations"]
    assert (
        intr
        and intr[0]["status"] == "possible"
        and intr[0]["fan_runs_after_s"] == [pytest.approx(240, abs=0.5)]
    )
    assert intr[0]["ecu_evidence"].startswith("none")
    findings = {f.id: f for f in analyze_session(store, sid)}
    assert findings["monitoring_dpf_regeneration"].strength == "suggestive"
    assert findings["monitoring_shutdown_fan"].strength == "suggestive"
    assert any("240 s" in e.text for e in findings["monitoring_shutdown_fan"].supporting)
    # coolant was still read after the stop while the ECU answered (heat soak)
    post = m["coolant"]["after_shutdown"][0]
    assert post["measured_for_s"] > 10 and post["origin"] == "observed"


def test_unpaired_fan_marker_has_no_invented_duration(store):
    _, sid, _ = monitor(store, "mini_ignition_off", 300)
    mark(store, sid, "fan_running", 100)
    mark(store, sid, "fan_stopped", 20)  # stopped before any running mark: orphan
    fans = summary_for(store, sid)["fan"]
    assert fans["runs"][0]["duration_s"] is None and fans["runs"][0]["complete"] is False
    assert len(fans["fan_stopped_without_running"]) == 1


def test_battery_voltage_drop_and_low_battery_warning(store):
    _, sid, _ = monitor(store, "mini_voltage_drop", 700)
    mark(store, sid, "battery_warning", 190, "battery symbol on the dash")
    mark(store, sid, "battery_warning_cleared", 260)
    m = summary_for(store, sid)
    ecu = m["voltage"]["ecu"]
    assert ecu["low_while_running"]["samples"] > 5 and ecu["low_while_running"]["min_v"] < 12.2
    assert any(d["context"] is None for d in ecu["drops"])
    w = next(x for x in m["voltage"]["battery_warnings"] if x["event"] == "battery_warning")
    assert w["engine_running"] and w["nearby"]["ecu_voltage"]["min"] < 12.2
    assert w["nearby"]["adapter_voltage"] is not None  # adapter value kept separately
    f = {x.id: x for x in analyze_session(store, sid)}["monitoring_electrical"]
    assert f.strength == "suggestive"
    assert any("not by itself a fault" in c for c in f.caveats)
    assert not any(
        w in " ".join([f.title, f.hypothesis]).lower() for w in ("alternator fail", "battery fail")
    )


def test_missing_dpf_parameters_are_explicitly_not_available(store):
    _, sid, _ = monitor(store, "healthy_idle", 60)
    m = summary_for(store, sid)
    std = next(g for g in m["dpf"]["availability"] if g["group"] == "standard_pids")["items"]
    assert all(i["status"] == "not_available" and "not advertised" in i["reason"] for i in std)
    mfr = next(g for g in m["dpf"]["availability"] if g["group"] == "manufacturer")["items"]
    assert {i["key"] for i in mfr} >= {"dpf_soot_load", "dpf_regen_status"}
    assert all(i["status"] == "not_available" for i in mfr)
    sig = {s["hex"]: s for s in m["signals"]}
    assert sig["7A"]["status"] == "not_available" and sig["0C"]["status"] == "measured"
    assert sig["ATRV"]["provenance"] == "adapter"


def test_history_baseline_enables_coolant_checks(store):
    for _ in range(2):
        monitor(store, "mini_highway_drive", 1300)
    _, sid, _ = monitor(store, "mini_warmup", 400)
    m = summary_for(store, sid)
    assert m["coolant"]["usual_warm_c"] is not None and m["coolant"]["usual_warm_sessions"] >= 2


def test_uncertain_results_never_become_confirmed(store):
    sids = []
    for sc, d in (
        ("mini_dpf_regen_possible", 1000),
        ("mini_shutdown_during_regen", 900),
        ("mini_voltage_drop", 600),
    ):
        sids.append(monitor(store, sc, d)[1])
    mark(store, sids[1], "suspected_dpf_regeneration", 500)
    for sid in sids:
        m = summary_for(store, sid)
        for d in m["detected_events"]:
            assert (
                d["status"] == "possible"
                and d["origin"] == "inferred"
                and d["confidence"] in ("low", "moderate")
            )
        assert m["dpf"]["confirmed_regenerations"] == []
        for f in analyze_session(store, sid):
            if f.id.startswith("monitoring_"):
                assert f.strength != "strong", f.id
        for a in store.annotations(sid):
            assert a.origin in ("manual", "observed")


def test_existing_profiles_and_duration_recording_unchanged(store):
    from app.fixtures_gen import record_scenario

    sid = record_scenario(store, "rough_idle", 20, [])
    meta = store.get_session(sid)["meta"]
    assert "monitoring" not in meta and "sampling_plan" not in meta and meta["requested_duration_s"] == 20
    assert not [f for f in analyze_session(store, sid) if f.id.startswith("monitoring_")]
    files = build_files(store, sid)
    assert (
        "monitoring.json" not in files
        and files["annotations.csv"].splitlines()[0] == "t_wall,t_mono,label,note"
    )


# -- export / replay -----------------------------------------------------------------------------


def test_monitoring_export_contains_all_parts_and_redacts_vin(store):
    wb, sid, _ = monitor(store, "mini_shutdown_during_regen", 900)
    store.update_session(sid, meta={"vehicle_info": {"vin": "SYNTHETICVIN00001", "vin_status": "read"}})
    mark(store, sid, "fan_running", 610)
    files = build_files(store, sid)
    for name in (
        "session.json", "vehicle_profile.json", "capabilities.json", "transcript.jsonl", "samples.csv",
        "failed_samples.jsonl", "events.jsonl", "detected_events.json", "sampling_quality.json",
        "findings.json", "monitoring.json", "report.md",
    ):  # fmt: skip
        assert name in files, name
    ev = [json.loads(x) for x in files["events.jsonl"].splitlines()]
    assert {e["origin"] for e in ev} == {"manual", "observed"}
    assert all(e["status"] == "possible" for e in json.loads(files["detected_events.json"]))
    failed = [json.loads(x) for x in files["failed_samples.jsonl"].splitlines()]
    assert failed and all(f["value"] is None for f in failed)
    assert json.loads(files["capabilities.json"])["capabilities"]["primary_ecu"] == "7E8"
    assert "Continuous monitoring" in files["report.md"] and "inferred" in files["report.md"]
    assert all("SYNTHETICVIN00001" not in v for v in files.values())
    z = build_zip(store, sid)
    assert z == build_zip(store, sid)
    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        assert zf.testzip() is None


def test_replayed_monitoring_session_is_analysable_offline(store):
    _, sid, _ = monitor(store, "mini_shutdown_during_regen", 900)
    mark(store, sid, "fan_running", 610)
    mark(store, sid, "fan_stopped", 800)
    orig = summary_for(store, sid)
    rid = ingest_transcript(transcript_lines(store, sid), store, label="replay")
    rep = monitoring_summary(load_session(store, rid), [load_session(store, rid)])
    assert is_monitoring_session(store.get_session(rid))
    assert rep["voltage"]["adapter"]["status"] == "measured"
    assert (
        len(rep["fan"]["runs"]) == 1
        and rep["fan"]["runs"][0]["duration_s"] == orig["fan"]["runs"][0]["duration_s"]
    )
    assert [e["event_type"] for e in rep["events"]] == [e["event_type"] for e in orig["events"]]
    assert len(rep["dpf"]["possible_regenerations"]) == len(orig["dpf"]["possible_regenerations"]) == 1


# -- interruption & recovery -----------------------------------------------------------------------


def test_keyboard_interrupt_closes_the_session_and_keeps_data(store, monkeypatch):
    calls = {"n": 0}
    real = monitor_mod.MonitoringRecorder._absorb

    def boom(self, item, rs, t0):
        calls["n"] += 1
        if calls["n"] == 400:
            raise KeyboardInterrupt
        return real(self, item, rs, t0)

    monkeypatch.setattr(monitor_mod.MonitoringRecorder, "_absorb", boom)
    wb = Workbench(store)
    wb.set_profile(MINI)
    wb.attach(SimulatorTransport("mini_city_drive", default_timeout_s=2.0), "simulator", "x")
    with pytest.raises(KeyboardInterrupt):
        wb.record(None, PROFILE, until_stop=True)
    s = store.list_sessions()[-1]
    assert s["ended_at"] and "KeyboardInterrupt" in s["meta"]["aborted"]
    assert len(store.samples(s["id"])) >= 390 and "monitoring" in s["meta"]
    assert store.get_setting(f"lease:{s['id']}") is None
    assert wb.active is None and wb.operation is None


def test_interrupted_session_is_recovered_on_next_start(store):
    sid = store.create_session(
        "record", MINI, "real", False, started_at=time.time() - 600, meta={"sampling_plan": {}}
    )
    store.add_samples(
        sid,
        [
            Reading(
                pid=0x0C,
                name="Engine speed",
                value=800.0,
                raw_hex="0C80",
                t_wall=time.time() - 400,
                t_mono=1.0,
            )
        ],
    )
    store.set_setting(f"lease:{sid}", {"pid": 1, "t": time.time() - 500})
    alive = store.create_session("record", MINI, "real", False, started_at=time.time() - 30)
    store.set_setting(f"lease:{alive}", {"pid": 2, "t": time.time()})
    recovered = Workbench(store).recover_interrupted()
    assert recovered == [sid]
    s = store.get_session(sid)
    assert s["ended_at"] == pytest.approx(time.time() - 400, abs=5)
    assert "interrupted" in s["meta"]["aborted"] and s["meta"]["recovered"]["last_record_wall"]
    assert s["meta"]["metrics"][0]["pid"] == 0x0C and len(store.samples(sid)) == 1
    assert store.get_session(alive)["ended_at"] is None  # another process still owns it


def test_samples_are_saved_incrementally_during_a_recording(tmp_path):
    from app.adapters.simulator import FakeElmServer
    from app.adapters.tcp import VgateWiFiTransport
    from app.storage.db import Store

    store = Store(tmp_path / "inc.sqlite3")
    reader = Store(tmp_path / "inc.sqlite3")  # a second connection, like another process
    with FakeElmServer("mini_city_drive") as srv:
        wb = Workbench(store)
        wb.attach(VgateWiFiTransport(srv.host, srv.port), "simulator", "mini_city_drive")
        import threading

        th = threading.Thread(
            target=lambda: wb.record(None, PROFILE, until_stop=True, pre_scan=False), daemon=True
        )
        th.start()
        deadline = time.time() + 10
        sid = None
        while time.time() < deadline and not (sid and len(reader.samples(sid)) > 20):
            sid = (wb.active or {}).get("session_id") or sid
            time.sleep(0.1)
        assert sid and len(reader.samples(sid)) > 20 and reader.get_session(sid)["ended_at"] is None
        lease = reader.get_setting(f"lease:{sid}")
        assert lease and time.time() - lease["t"] < 30
        wb.stop_recording()
        th.join(10)
        assert reader.get_session(sid)["ended_at"] is not None
        wb.disconnect()
    store.close()
    reader.close()


def test_monitoring_fixture_replays_offline(store):
    from pathlib import Path

    from app.fixtures_gen import MONITORING_FIXTURES
    from app.storage.transcript import ingest_file

    for stem, _, _ in MONITORING_FIXTURES.values():
        path = Path(__file__).resolve().parents[2] / "fixtures" / f"{stem}.jsonl"
        header = json.loads(path.read_text().splitlines()[0])
        assert header["synthetic"] is True and header["recording"]["recording_profile"] == PROFILE
        sid = ingest_file(path, store)
        m = summary_for(store, sid)
        assert m["is_monitoring_session"] and m["synthetic"]
        assert m["fan"]["runs"][0]["complete"] and m["fan"]["runs"][0]["after_shutdown"]
        assert m["voltage"]["adapter"]["status"] == "measured"
        # observed stop + manual ignition-off marker = ONE shutdown episode (task 5)
        assert [s["origin"] for s in m["shutdowns"]] == ["observed"]
        ep = m["shutdowns"][0]
        assert ep["engine_stop"]["origin"] == "observed" and ep["ignition_off"]["origin"] == "manual"
