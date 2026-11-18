"""Regression tests for the DDE code-review follow-up (fixes 1-7) and the integration checks.

SYNTHETIC simulator data only. Nothing here says anything about the real car or adapter."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from app.acquisition import monitor as monitor_mod
from app.adapters.simulator import REGEN, SCENARIOS, FakeElmServer, SimDde, SimulatorTransport, _dde_responses
from app.adapters.tcp import VgateWiFiTransport
from app.diagnostics.dde import regeneration_episodes
from app.diagnostics.monitoring import summary_for
from app.manufacturer.bmw.capability import assess, isotp_evidence
from app.manufacturer.bmw.catalog import (
    SOURCES,
    CatalogEntry,
    ExecutionContext,
    RequestCatalog,
    ResponseLayout,
    validate_entry,
)
from app.manufacturer.bmw.synthetic import SYN_ADDR, SYNTHETIC_VARIANT
from app.models.domain import Annotation, Exchange
from app.service import Workbench
from app.storage import transcript as transcript_mod
from app.storage.export import build_files
from app.storage.transcript import ingest_file, ingest_transcript, transcript_lines

PROFILE = "mini_extended_monitoring"
FIX = Path(__file__).resolve().parents[2] / "fixtures"
REAL = json.loads((Path(__file__).parent / "data" / "real-mini-20261118-connect.json").read_text())
T0 = 1_700_000_000.0


def monitor(store, scenario, duration, opt_in=True, **kw):
    wb = Workbench(store)
    if opt_in:
        wb.set_dde_settings(opt_in=True, confirmed_variant=SYNTHETIC_VARIANT)
    sc = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    t = SimulatorTransport(sc, default_timeout_s=2.0, **kw)
    wb.attach(t, "simulator", sc.name)
    sid = wb.record(None, PROFILE, until_stop=True, max_duration_s=duration)
    return wb, sid, t


def events(store, sid, *types):
    return [a for a in store.annotations(sid) if a.event_type in types]


# =============================================================================================
# 1. transcript replay keeps ONE chronological timeline
# =============================================================================================

KEYS = ("t_wall", "t_mono", "mode", "pid", "ecu", "value", "text", "raw_hex", "quality", "source", "signal_key",
        "request_id", "decoder_version", "catalog_version", "status_json")  # fmt: skip


def _timeline(store, sid):
    return [tuple(s.get(k) for k in KEYS) for s in store.samples(sid) if s["mode"] in (0, 1, 0x100)]


def test_mixed_session_replay_keeps_the_interleaved_timeline_and_content(store):
    wb, sid, t = monitor(store, "mini_dde_synthetic", 900)
    orig = _timeline(store, sid)
    assert any(r[KEYS.index("source")] == "bmw_dde" for r in orig) and any(r[2] == 1 for r in orig)
    sid2 = ingest_transcript(transcript_lines(store, sid), store, label="replay")
    rep = _timeline(store, sid2)
    assert [r[1] for r in rep] == sorted(r[1] for r in rep)  # chronological by insertion order
    assert rep == orig  # identical content AND order, provenance included
    # analysis, exports and charts run on the restored timeline and agree with the original
    m1, m2 = summary_for(store, sid), summary_for(store, sid2)
    assert m1["dde"]["regeneration"]["confirmed"] and (
        [(e["t_start_rel_s"], e["duration_s"], e["started"]) for e in m1["dde"]["regeneration"]["confirmed"]]
        == [
            (e["t_start_rel_s"], e["duration_s"], e["started"])
            for e in m2["dde"]["regeneration"]["confirmed"]
        ]
    )
    assert [a.event_type for a in store.annotations(sid)] == [a.event_type for a in store.annotations(sid2)]
    f2 = build_files(store, sid2)
    assert "signal_key" in f2["samples.csv"].splitlines()[0] and "dde_monitoring.json" in f2
    from app.api.main import _series

    assert any(s["provenance"] == "bmw_dde" and s["points"] for s in _series(store, sid2)["signals"])


def test_generic_only_transcripts_are_inserted_exactly_as_before(store, monkeypatch):
    def boom(_):  # pragma: no cover - must not be reached for generic-only transcripts
        raise AssertionError("generic-only replay must keep its original insertion path")

    monkeypatch.setattr(transcript_mod, "chronological", boom)
    for name in ("mini-monitoring-air-path.jsonl", "rough-idle.jsonl", "porsche-healthy-idle.jsonl"):
        sid = ingest_file(FIX / name, store)
        rows = store.samples(sid)
        assert rows and not any("source" in r for r in rows)


def test_chronological_is_deterministic_for_identical_timestamps():
    from app.models.domain import MANUFACTURER_MODE, Reading
    from app.storage.db import chronological

    a = Reading(pid=0x0C, name="rpm", t_mono=5.0, t_wall=T0 + 5)
    b = Reading(mode=MANUFACTURER_MODE, pid=0x103, name="x", t_mono=5.0, t_wall=T0 + 5, source="bmw_dde")
    c = Reading(pid=0x0D, name="speed", t_mono=5.0, t_wall=T0 + 5)
    d = Reading(pid=0x05, name="cool", t_mono=4.0, t_wall=T0 + 4)
    assert chronological([b, a, c, d]) == [d, a, c, b]
    assert chronological([b, a, c, d]) == chronological([b, a, c, d])


# =============================================================================================
# 2. regeneration freshness: attempted cadence, unknown gaps, bounds
# =============================================================================================


def _row(t, quality="ok", active=True):
    return {
        "t_wall": T0 + t,
        "t_mono": float(t),
        "quality": quality,
        "status_json": json.dumps({"active": active}) if quality == "ok" else None,
        "decoder_version": "1",
        "catalog_version": "c",
        "request_id": "r",
    }


def _ann(t, et):
    return Annotation(t_wall=T0 + t, t_mono=float(t), label=et, event_type=et, origin="observed")


def test_ok_flags_far_apart_with_failed_reads_between_are_not_a_continuous_regeneration():
    rows = [_row(t) for t in (0, 100, 200)]
    rows += [_row(t, "no_data") for t in range(5, 200, 5) if t not in (100,)]
    eps, gaps = regeneration_episodes(rows, 5.0, T0)
    assert all(e["duration_s"] == 0 for e in eps) and len(eps) == 3  # never one 200 s regeneration
    assert sum(e["duration_s"] for e in eps) == 0
    assert len([g for g in gaps if "failed" in g["reason"]]) == 2
    assert eps[0]["freshness_window_s"] == 15.0 and eps[0]["read_cadence_s"] == 5.0
    assert eps[1]["started"].startswith("active at the first fresh reading after an unknown gap")


def test_intermittent_failures_shorter_than_the_freshness_window_keep_the_run():
    rows = [_row(t) for t in range(0, 65, 5) if t not in (10, 15)] + [
        _row(10, "timeout"),
        _row(15, "negative"),
    ]
    eps, gaps = regeneration_episodes(rows, 5.0, T0, session_end_wall=T0 + 62)
    assert len(eps) == 1 and eps[0]["duration_s"] == 60 and not gaps
    assert eps[0]["ended"] == "still active at the end"


def test_adapter_loss_during_regeneration_splits_it_and_resumes_after_the_gap():
    rows = [_row(t) for t in range(0, 55, 5)] + [_row(t) for t in range(125, 155, 5)]
    ann = [_ann(52, "adapter_connection_lost"), _ann(120, "adapter_connection_restored")]
    eps, gaps = regeneration_episodes(rows, 5.0, T0, ann, T0 + 150)
    assert [e["duration_s"] for e in eps] == [50, 25]  # the 75 s outage is never counted
    assert eps[0]["ended"] == "unknown (adapter connection lost)" and eps[0]["ended_between_rel_s"][1] is None
    assert eps[1]["started"].startswith("active at the first fresh reading after an unknown gap")
    assert eps[1]["started_between_rel_s"] == [50.0, 125.0]
    assert gaps[0]["reason"] == "adapter connection lost" and gaps[0]["state_before"] == "active"


def test_ecu_silence_during_regeneration_ends_knowledge_not_the_regeneration():
    rows = [_row(t) for t in range(0, 55, 5)]
    eps, gaps = regeneration_episodes(rows, 5.0, T0, [_ann(55, "ecu_communication_lost")], T0 + 300)
    assert len(eps) == 1 and eps[0]["duration_s"] == 50
    assert eps[0]["ended"] == "unknown after the last fresh reading (ECU communication lost)"
    assert gaps[-1]["reason"] == "ECU communication lost"


def test_genuine_active_to_inactive_transition_has_bounds():
    rows = [_row(t, active=False) for t in range(0, 25, 5)] + [_row(t) for t in range(25, 65, 5)]
    rows += [_row(t, active=False) for t in range(65, 85, 5)]
    eps, gaps = regeneration_episodes(rows, 5.0, T0, session_end_wall=T0 + 80)
    (e,) = eps
    assert e["started_between_rel_s"] == [20.0, 25.0] and e["ended_between_rel_s"] == [60.0, 65.0]
    assert e["duration_s"] == 35 and e["ended"] == "flag inactive between two fresh readings" and not gaps


def test_live_events_and_session_analysis_use_the_same_semantics(store):
    # synthetic regeneration 425..725 s; the flag read fails (NO DATA) 560..660 s
    base = _dde_responses(REGEN)

    def responses(t, req):
        return None if req == "22F101" and 560 <= t < 660 else base(t, req)

    sc = dataclasses.replace(
        SCENARIOS["mini_dde_synthetic"], name="dde_flag_gap", dde=SimDde("7B0", "7B8", responses, "5A", "F5")
    )
    wb, sid, t = monitor(store, sc, 1000)
    ev = [a.event_type for a in events(store, sid, "dde_regeneration_started", "dde_regeneration_confirmed",
                                       "dde_regeneration_unknown", "dde_regeneration_ended")]  # fmt: skip
    assert ev == [
        "dde_regeneration_started",
        "dde_regeneration_unknown",
        "dde_regeneration_confirmed",
        "dde_regeneration_ended",
    ]
    m = summary_for(store, sid)
    eps = m["dde"]["regeneration"]["confirmed"]
    assert len(eps) == 2  # one per started / re-confirmed event
    assert (
        eps[0]["ended"].startswith("unknown")
        and eps[1]["ended"] == "flag inactive between two fresh readings"
    )
    total = sum(e["duration_s"] for e in eps)
    assert total < 300 - 90  # the ~100 s without a fresh flag is not counted


# =============================================================================================
# 3. bandwidth scheduling: generic first, DDE stretched / deferred, target recorded
# =============================================================================================


def _rpm_ok(store, sid):
    return sum(1 for s in store.samples(sid) if s["mode"] == 1 and s["pid"] == 0x0C and s["quality"] == "ok")


@pytest.mark.parametrize("delay", [None, (0.12, 0.25)], ids=["normal", "slow_adapter"])
def test_dde_never_degrades_the_generic_fast_tier(tmp_path, delay):
    from app.storage.db import Store

    runs = {}
    for opt_in in (False, True):
        st = Store(tmp_path / f"{opt_in}.sqlite3")
        sc = SCENARIOS["mini_dde_synthetic"]
        if delay:
            sc = dataclasses.replace(sc, name="dde_slow", delay=delay)
        wb, sid, t = monitor(st, sc, 400, opt_in=opt_in)
        mon = st.get_session(sid)["meta"]["monitoring"]
        runs[opt_in] = (mon, _rpm_ok(st, sid))
        st.close()
    (off, rpm_off), (on, rpm_on) = runs[False], runs[True]
    assert on["final_scale"]["fast"] <= off["final_scale"]["fast"] * 1.1
    assert rpm_on >= 0.8 * rpm_off
    assert off["dde"] is None and not any("dde_scale" in a for a in off["adjustments"])
    sched = on["dde"]["scheduling"]
    assert sched["target_share"] == 0.35 and "projected_share" in sched and "measured_share" in sched
    assert on["dde"]["final_effective_intervals"] and all(
        v > 0 for v in on["dde"]["final_effective_intervals"].values()
    )
    if delay:
        assert on["dde"]["final_scale"] > 1.0
        assert any("dde_effective_intervals" in a for a in on["adjustments"])


def test_heavy_dde_workload_is_recorded_as_target_exceeded_not_hidden(tmp_path, monkeypatch):
    from app.storage.db import Store

    monkeypatch.setattr(monitor_mod, "DDE_TARGET_SHARE", 0.02)  # force an unreachable target
    slow = dataclasses.replace(SCENARIOS["mini_dde_synthetic"], name="dde_heavy", delay=(0.12, 0.25))
    out = {}
    for opt_in in (False, True):
        st = Store(tmp_path / f"h{opt_in}.sqlite3")
        wb, sid, t = monitor(st, slow, 400, opt_in=opt_in)
        out[opt_in] = (st.get_session(sid)["meta"]["monitoring"], _rpm_ok(st, sid))
        st.close()
    (off, rpm_off), (on, rpm_on) = out[False], out[True]
    assert on["dde"]["final_scale"] == monitor_mod.DDE_MAX_SCALE
    assert any(a.get("dde", {}).get("target_exceeded") for a in on["adjustments"])
    # the generic tiers are scaled exactly as without DDE; the fast tier is not degraded by DDE
    assert on["final_scale"]["fast"] <= off["final_scale"]["fast"] * 1.1
    assert rpm_on >= 0.75 * rpm_off


def test_repeated_dde_failures_do_not_count_as_ecu_loss(store):
    sc = SCENARIOS["mini_dde_synthetic"]

    def responses(t, req):
        return None if req.startswith("22F1") and req != "22F101" else sc.dde.responses(t, req)

    neg = dataclasses.replace(sc, name="dde_fail", dde=SimDde("7B0", "7B8", responses, "5A", "F5"))
    wb, sid, t = monitor(store, neg, 400)
    assert not events(store, sid, "ecu_communication_lost")
    mon = store.get_session(sid)["meta"]["monitoring"]
    assert mon["final_state"] == "running" and _rpm_ok(store, sid) > 300


def test_no_burst_of_dde_transactions_after_reconnection(store):
    wb, sid, t = monitor(store, "mini_dde_drive", 900)
    back = events(store, sid, "adapter_connection_restored")[0].t_mono
    mon = store.get_session(sid)["meta"]["monitoring"]
    intervals = mon["dde"]["final_effective_intervals"]
    # each DDE signal at most once within its effective interval after the restore (no catch-up) ...
    for key, iv in intervals.items():
        first = [
            s["t_mono"]
            for s in store.samples(sid)
            if s.get("signal_key") == key and back <= s["t_mono"] < back + iv
        ]
        assert len(first) <= 1, (key, first)
    # ... and the first seconds carry no more DDE transactions than the steady rate predicts
    dde_tx = [
        e["t_mono"]
        for e in store.exchanges(sid)
        if e["request"] == "ATSH7B0" and back <= e["t_mono"] < back + 5
    ]
    assert len(dde_tx) <= sum(5 / iv for iv in intervals.values()) + 1


# =============================================================================================
# 4. live DDE freshness in the snapshot (the dashboard part is in dde-monitoring.spec.ts)
# =============================================================================================


def test_snapshot_carries_deadlines_and_is_unknown_when_not_running(store):
    snaps = []
    orig = monitor_mod.MonitoringRecorder.snapshot

    def spy(self):
        snap = orig(self)
        snaps.append(snap)
        return snap

    import unittest.mock

    with unittest.mock.patch.object(monitor_mod.MonitoringRecorder, "snapshot", spy):
        monitor(store, "mini_dde_drive", 1300)
    snaps = [s for s in snaps if s.get("dde")]
    running = [s for s in snaps if s["state"] == "running" and s["dde"]["regeneration"]["state"] == "active"]
    assert running and all(s["dde"]["regeneration"]["fresh_until_wall"] for s in running)
    silent = [s for s in snaps if s["state"] == "ecu_unresponsive"]
    assert silent and all(s["dde"]["regeneration"]["state"] == "unknown" for s in silent)
    assert all(s["dde"]["regeneration"]["fresh_until_wall"] is None for s in silent)
    sig = next(iter(running[-1]["dde"]["signals"].values()))
    assert sig["t_wall"] and sig["fresh_until_wall"] > sig["t_wall"]


# =============================================================================================
# 5. unsupported adapter: per adapter connection, across runtimes, reset by a new connection
# =============================================================================================


def test_unsupported_state_survives_runtimes_and_internal_reconnects_but_not_a_new_connection(store):
    def script(cmd, n, normal):
        return [(0, b"?\r\r>")] if cmd == "ATCEA5A" else None  # the adapter refuses extended addressing

    srv = FakeElmServer("mini_dde_synthetic", script=script).start()
    try:
        wb = Workbench(store)
        wb.set_dde_settings(opt_in=True, confirmed_variant=SYNTHETIC_VARIANT)
        wb.attach(
            VgateWiFiTransport(srv.host, srv.port, default_timeout_s=2, backoff_initial_s=0.05),
            "simulator",
            "x",
        )
        n_sh = lambda: sum(1 for _, c in srv.log if c == "ATSH7B0")  # noqa: E731
        m1 = store.scans(wb.scan(freeze_frame=False))[0].manufacturer
        assert m1.status == "unsupported_adapter" and "ATCEA5A" in m1.detail and n_sh() == 1
        gen = wb.client.transport.generation
        assert wb.client.link_ready()  # restore succeeded: the connection was kept
        m2 = store.scans(wb.scan(freeze_frame=False))[0].manufacturer
        assert m2.status == "unsupported_adapter" and n_sh() == 1  # same connection: nothing re-sent
        assert wb.dde_runtime().read("syn.dpf_soot_mass").quality == "unsupported_adapter" and n_sh() == 1
        # an internal reconnect (resync) is the same adapter connection: still remembered
        wb.client.transport.invalidate("test resync")
        assert any(r.quality == "ok" for r in wb.client.read_pid(0x0C))  # generic recovers (ATZ...)
        assert wb.client.transport.generation > gen
        assert wb.dde_runtime().read("syn.dpf_soot_mass").quality == "unsupported_adapter" and n_sh() == 1
        st = wb.dde_status()["adapter_state"]
        assert st["reason"] and st["connection_id"] == wb.client.connection_id
        # a genuinely new connection is assessed again (guarded, through the same gates)
        wb.attach(
            VgateWiFiTransport(srv.host, srv.port, default_timeout_s=2, backoff_initial_s=0.05),
            "simulator",
            "x",
        )
        assert wb.dde_status()["adapter_state"] is None
        assert wb.dde_runtime().read("syn.dpf_soot_mass").quality == "unsupported_adapter" and n_sh() == 2
        assert any(r.quality == "ok" for r in wb.client.read_pid(0x0C))
    finally:
        wb.disconnect()
        srv.stop()


def test_dde_read_never_initialises_the_adapter(store, monkeypatch):
    wb = Workbench(store)
    wb.set_dde_settings(opt_in=True, confirmed_variant=SYNTHETIC_VARIANT)
    wb.attach(SimulatorTransport("mini_dde_synthetic"), "simulator", "mini_dde_synthetic")
    wb.client.transport.invalidate("generic is recovering")
    calls = []
    monkeypatch.setattr(wb.client, "initialize", lambda: calls.append(1))
    n = len(wb.transport.sim.received)
    assert wb.dde_runtime().read("syn.dpf_soot_mass").quality == "not_attempted"
    assert calls == [] and len(wb.transport.sim.received) == n


# =============================================================================================
# 6. ISO-TP evidence belongs to the reconstructed multi-frame message
# =============================================================================================


def _ex(text, seq=99, request="0904"):
    return Exchange(
        seq=seq, t_wall=1.0, t_mono=1.0, request=request, request_hex="", response_hex=text.encode().hex().upper(),
        response_text=text, latency_ms=50.0, status="ok", phase="capability",
    )  # fmt: skip


def test_incomplete_first_frame_then_single_frame_is_not_multiframe_evidence():
    ex = _ex("7E8 10 13 49 04 01 41 42 43 \r7E8 03 7F 09 31 00 00 00 00 \r\r>")
    ev = isotp_evidence([ex], False)
    assert len(ev) == 1 and ev[0]["complete"] is False  # the single frame is not counted at all
    rep = assess(
        adapter=REAL["adapter"], capabilities=REAL["capabilities"],
        exchanges=REAL["exchanges"] + [ex.model_dump()], source="real", synthetic=False,
    )  # fmt: skip
    assert rep["isotp_multiframe"]["value"] is False
    assert rep["verdicts"]["manufacturer_transport"]["verdict"] == "manufacturer_transport_unverified"
    ok = _ex("7E8 10 13 49 04 01 41 42 43 \r7E8 21 44 45 46 47 48 49 4A \r7E8 22 4B 4C 4D 4E 4F 50 00 \r\r>")
    assert [e["complete"] for e in isotp_evidence([ok], False)] == [True]
    single = _ex("7E8 06 41 00 98 3B A0 13 \r\r>", request="0100")
    assert isotp_evidence([single], False) == []


# =============================================================================================
# 7. catalog validation never raises on malformed service 19 payloads
# =============================================================================================


def _e19(payload, **kw):
    base = dict(
        request_id="t19", metric="dde_fault_memory", title="t", priority="P0", kind="fault_memory",
        verification_state="literature_only", risk_class="pure_read", service=0x19, payload_hex=payload,
        establish_by=("x",), addressing=SYN_ADDR,
    )  # fmt: skip
    base.update(kw)
    return CatalogEntry(**base)


@pytest.mark.parametrize(
    "payload,problem",
    [
        ("19ZZ", "not valid hex"),
        ("19zz02", "not valid hex"),
        ("19", "truncated"),
        ("1914", "not a read-only sub-function"),
    ],
)
def test_malformed_service_19_payloads_are_problems_not_exceptions(payload, problem):
    e = _e19(payload)
    probs = validate_entry(e, {s.id: s for s in SOURCES})
    assert any(problem in p for p in probs), probs
    cat = RequestCatalog([e], SOURCES)
    assert "t19" in cat.problems()
    d = cat.decide("t19", ExecutionContext(opted_in=True, confirmed_variant="X"))
    assert not d.executable
    assert cat.availability(ExecutionContext())[0]["status"] == "blocked"


def test_valid_read_only_service_19_payload_validates():
    e = _e19(
        "1902FF",
        response=ResponseLayout(0x59, "02", 3, 255, "synthetic.fault_list", "1"),
        source_ids=("this_vehicle_traces",),
    )
    probs = validate_entry(e, {s.id: s for s in SOURCES})
    assert not any("19" in p and ("sub-function" in p or "truncated" in p or "hex" in p) for p in probs), (
        probs
    )


# =============================================================================================
# 8. integration regressions
# =============================================================================================


def test_porsche_profile_never_touches_bmw_dde(store, monkeypatch):
    from app.manufacturer.bmw import runtime as runtime_mod

    def boom(*a, **k):
        raise AssertionError("BMW DDE runtime created for the Porsche profile")

    monkeypatch.setattr(runtime_mod.DdeRuntime, "__init__", boom)
    wb = Workbench(store)
    wb.set_profile("porsche-boxster-986-mk2")
    wb.attach(SimulatorTransport("porsche_healthy_idle"), "simulator", "porsche_healthy_idle")
    wb.scan(freeze_frame=False)
    wb.inventory()
    sid = wb.record(5, "idle")
    assert "dde" not in store.get_session(sid)["meta"]
    assert not any(c.startswith("ATSH") for c in wb.transport.sim.received)


def test_generic_mini_monitoring_with_dde_disabled_is_unchanged(store):
    wb, sid, t = monitor(store, "mini_city_drive", 700, opt_in=False)
    meta = store.get_session(sid)["meta"]
    assert meta["dde"]["status"] == "not_polled" and meta["monitoring"]["dde"] is None
    assert not any("source" in s for s in store.samples(sid))
    assert not any(k.startswith("dde") for a in meta["monitoring"]["adjustments"] for k in a)
    m = summary_for(store, sid)
    assert m["dde"]["regeneration"]["status"] == "not_available" and m["dpf"]["confirmed_regenerations"] == []
    assert len({s["id"] for s in store.list_sessions()}) == 1  # one session


def test_no_manufacturer_request_while_ecu_unresponsive(store):
    wb, sid, t = monitor(store, "mini_dde_drive", 1388)
    lost = events(store, sid, "ecu_communication_lost")[0].t_mono
    later = [e["request"] for e in store.exchanges(sid) if e["t_mono"] > lost and e["phase"] == "record"]
    assert later and set(later) <= {"ATRV"}
