"""Air-path diagnostics in continuous monitoring: MAF anomalies, EGR / MAP / RPM associations,
generic DTCs (P115D) and multi-session trends.

All data here is SYNTHETIC: hand-built sample series or the simulator scenario
``mini_air_path_anomalies`` (fixtures/mini-monitoring-air-path.jsonl), an anonymised stand-in for
the first real recording (20261118-143936-c028), whose archive was not available to the tests.
Nothing here says anything about the real MINI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.diagnostics import monitoring as M
from app.diagnostics.air_path import air_path_row, air_path_summary, dtc_summary
from app.diagnostics.dtc_info import classify
from app.diagnostics.engine import analyze_session, load_session
from app.diagnostics.monitoring import history_for, summary_for
from app.fixtures_gen import AIR_PATH_FIXTURES, record_monitoring
from app.models.domain import Annotation, Dtc, DtcScan, Reading
from app.storage.db import Store
from app.storage.export import build_files, build_zip
from app.storage.transcript import ingest_transcript
from app.vehicles.profiles import MINI_EXTENDED_MONITORING, MINI_R60_DIESEL

MINI = MINI_R60_DIESEL.id
PROFILE = "mini_extended_monitoring"
T0 = 1_760_000_000.0
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
RPM, SPEED, LOAD, COOLANT, MAP, MAF, EGR_CMD, EGR_ERR = 0x0C, 0x0D, 0x04, 0x05, 0x0B, 0x10, 0x2C, 0x2D
CAPS = {
    "primary_ecu": "7E8",
    "ecus": [{"ecu": "7E8", "mode01_pids": [1, 4, 5, 11, 12, 13, 16, 44, 45]}],
}


# -- helpers ----------------------------------------------------------------------------------------


def r(pid: int, value: float | None, t: float, quality: str = "ok", raw: str | None = None) -> Reading:
    if raw is None and value is not None and pid == MAF:
        raw = f"{int(round(value * 100)):04X}"
    return Reading(
        pid=pid,
        ecu="7E8" if quality == "ok" else None,
        name=f"pid {pid:02X}",
        value=value,
        raw_hex=raw or ("" if value is None else "00"),
        quality=quality,  # type: ignore[arg-type]
        t_wall=T0 + t,
        t_mono=t,
    )


def drive(
    seconds: float,
    *,
    maf=lambda t: 9.5,
    rpm=lambda t: 800.0,
    speed=lambda t: 0.0,
    coolant=lambda t: 80.0,
    egr_err=lambda t: 0.0,
    map_=lambda t: 101.0,
    maf_quality=lambda t: "ok",
    step: float = 0.5,
) -> list[Reading]:
    """Sequential polling: RPM, speed, load, MAF, MAP every ``step`` s (each 0.1 s apart), coolant / EGR every 2 s."""
    out: list[Reading] = []
    k = 0
    t = 0.0
    while t < seconds:
        out += [r(RPM, rpm(t), t), r(SPEED, speed(t + 0.1), t + 0.1), r(LOAD, 20.0, t + 0.2)]
        q = maf_quality(t + 0.3)
        out.append(r(MAF, maf(t + 0.3) if q == "ok" else None, t + 0.3, quality=q))
        out.append(r(MAP, map_(t + 0.4), t + 0.4))
        if k % 4 == 0:
            out += [
                r(COOLANT, coolant(t), t + 0.45),
                r(EGR_CMD, 35.0, t + 0.46),
                r(EGR_ERR, egr_err(t), t + 0.47),
            ]
        t = round(t + step, 3)
        k += 1
    return out


def session(
    store: Store, readings: list[Reading], *, synthetic: bool = True, start: float = T0, **meta
) -> str:
    sid = store.create_session(
        "record",
        MINI,
        "simulator" if synthetic else "real",
        synthetic,
        started_at=start,
        meta={"sampling_plan": {"tiers": None}, "recording_profile": PROFILE, "capabilities": CAPS, **meta},
    )
    shift = start - T0
    for x in readings:
        x.t_wall += shift
    store.add_samples(sid, readings)
    store.update_session(sid, ended_at=max(x.t_wall for x in readings))
    return sid


def scan(
    store: Store, sid: str, codes: tuple[str, ...], phase: str = "pre", mil: bool = True, t: float = 0.0
) -> None:
    store.add_scan(
        sid,
        DtcScan(
            t_wall=T0 + t,
            phase=phase,
            dtcs=[Dtc(code=c, raw_hex=c[1:], kind="stored", ecu="7E8") for c in codes],
            mode_status={"stored": "supported", "pending": "supported"},
            mil_on=mil,
        ),
    )


def ap_of(store: Store, sid: str) -> dict:
    return summary_for(store, sid)["air_path"]


def in_window(a: float, b: float, v: float):
    return lambda t: v if a <= t < b else 9.5


# -- 1. zero MAF with confirmed running RPM ----------------------------------------------------------


def test_zero_maf_with_confirmed_running_rpm_is_a_candidate_anomaly(store):
    sid = session(store, drive(120, maf=in_window(60.0, 60.4, 0.0)))
    ap = ap_of(store, sid)
    maf = ap["maf"]
    assert maf["zero_running"] == 1 and maf["low_running"] == 1 and maf["episode_count"] == 1
    ep = maf["episodes"][0]
    assert (
        ep["type"] == "maf_zero_while_running" and ep["origin"] == "inferred" and ep["status"] == "candidate"
    )
    assert ep["pattern"] == "single_transient" and ep["confidence"] == "low" and ep["repeated"] is False
    first = ep["rpm"]["first"]
    assert first["rpm_before"]["value"] >= 600 and first["rpm_after"]["value"] >= 600
    assert (
        first["rpm_before"]["age_s"] <= M.AIRPATH_RPM_MAX_AGE_S
        and first["rpm_after"]["age_s"] <= M.AIRPATH_RPM_MAX_AGE_S
    )
    # raw evidence: the zero is in the ECU answer itself
    assert ep["evidence"][0]["response"] == "41 10 00 00" and ep["evidence"][0]["ecu"] == "7E8"
    assert ep in ap["detected_events"]
    assert "does not identify a faulty part" in ep["text"]


def test_low_but_not_zero_maf_is_listed_as_low(store):
    sid = session(store, drive(120, maf=in_window(60.0, 60.4, 0.6)))
    maf = ap_of(store, sid)["maf"]
    assert maf["zero_running"] == 0 and maf["low_running"] == 1
    assert maf["episodes"][0]["type"] == "maf_low_while_running"


# -- 2. zero MAF after a confirmed engine stop -------------------------------------------------------


def test_zero_maf_after_a_confirmed_stop_is_not_a_running_anomaly(store):
    def rpm(t):
        return 800.0 if t < 60 else 0.0

    def maf(t):
        return 9.5 if t < 60 else 0.0

    sid = session(store, drive(120, rpm=rpm, maf=maf))
    maf_ = ap_of(store, sid)["maf"]
    assert maf_["zero_running"] == 0 and maf_["episode_count"] == 0
    assert maf_["zero_engine_not_running"] > 50
    # only the sample at the stop transition and the last one (no RPM after it) are not judged
    assert maf_["running_state"]["unknown"] == 2


# -- 3. communication failures are not zero MAF --------------------------------------------------------


def test_failed_maf_reads_are_never_counted_as_zero(store):
    sid = session(store, drive(120, maf_quality=lambda t: "no_data" if 50 <= t < 70 else "ok"))
    ap = ap_of(store, sid)
    maf = ap["maf"]
    assert maf["failed_reads"]["n"] == 40 and maf["failed_reads"]["by_quality"] == {"no_data": 40}
    assert maf["zero_running"] == 0 and maf["low_running"] == 0 and maf["episode_count"] == 0
    assert any("not counted as zero" in x for x in ap["limitations"])


def test_zero_maf_next_to_an_ecu_silence_is_not_judged(store):
    """The ECU stopped answering RPM (comm loss) while MAF happened to read 0: engine state unknown."""
    rows = [x for x in drive(60) if not (x.pid == RPM and x.t_mono >= 40)]
    rows += [r(RPM, None, 40.0 + i, quality="timeout") for i in range(20)]
    rows.append(r(MAF, 0.0, 45.0))
    sid = session(store, rows)
    maf = ap_of(store, sid)["maf"]
    assert maf["zero_running"] == 0 and maf["episode_count"] == 0
    assert maf["low_running_state_unknown"] >= 1


def test_session_without_maf_reports_unavailable_not_zero(store):
    rows = [x for x in drive(120) if x.pid not in (MAF, EGR_ERR)]
    sid = session(
        store, rows, capabilities={**CAPS, "ecus": [{"ecu": "7E8", "mode01_pids": [4, 5, 11, 12, 13, 44]}]}
    )
    ap = ap_of(store, sid)
    assert ap["status"] == "maf_not_available" and ap["signals"]["maf"]["status"] == "not_available"
    row = air_path_row(load_session(store, sid))
    for k in (
        "maf_running_samples",
        "maf_zero_running",
        "maf_low_running",
        "maf_low_per_1k",
        "maf_episodes",
        "cold",
        "idle",
    ):
        assert row[k] is None, k
    assert row["egr_excursions"] is None and row["egr_status"] == "not_available"


# -- 4. grouping into episodes ---------------------------------------------------------------------------


def test_repeated_low_samples_group_into_episodes_without_losing_raw_evidence(store):
    def maf(t):
        if 30 <= t < 33:  # 6 consecutive zero samples: one persistent episode
            return 0.0
        if 60 <= t < 60.4 or 90 <= t < 90.4:  # two single transients, close enough to repeat
            return 0.0
        if 400 <= t < 400.4:  # far away: its own cluster
            return 0.3
        return 9.5

    sid = session(store, drive(450, maf=maf))
    maf_ = ap_of(store, sid)["maf"]
    eps = maf_["episodes"]
    assert [e["samples"] for e in eps] == [6, 1, 1, 1]
    assert eps[0]["pattern"] == "persistent" and eps[0]["duration_s"] == 2.5
    assert [e["cluster"] for e in eps] == [0, 0, 0, 1]
    assert [e["repeated"] for e in eps] == [True, True, True, False]
    assert maf_["repeated_clusters"] == 1 and maf_["persistent_episodes"] == 1
    # every candidate sample keeps its own timestamp and raw answer
    assert len(eps[0]["evidence"]) == 6 and all(ev["response"] == "41 10 00 00" for ev in eps[0]["evidence"])
    assert sorted(ev["t_rel_s"] for ev in eps[0]["evidence"]) == [ev["t_rel_s"] for ev in eps[0]["evidence"]]
    assert eps[3]["evidence"][0]["raw_hex"] == "001E" and eps[3]["type"] == "maf_low_while_running"
    # recovery after the episode is measured, not assumed
    assert eps[0]["maf_recovered"]["value_gs"] == 9.5 and eps[0]["maf_before"]["value_gs"] == 9.5
    assert maf_["episode_total_s"] == 2.5 and maf_["zero_running"] == 8 and maf_["low_running"] == 9


def test_cold_warm_and_idle_moving_distribution(store):
    def coolant(t):
        return 20.0 if t < 100 else 80.0

    def speed(t):
        return 0.0 if t < 150 else 40.0

    def maf(t):
        return 0.0 if any(a <= t < a + 0.4 for a in (50, 120, 200)) else 9.5

    sid = session(store, drive(260, coolant=coolant, speed=speed, maf=maf))
    m = ap_of(store, sid)["maf"]
    assert m["by_temperature"]["cold"]["episodes"] == 1 and m["by_temperature"]["warm"]["episodes"] == 2
    assert m["by_motion"]["idle"]["episodes"] == 2 and m["by_motion"]["moving"]["episodes"] == 1
    assert m["by_temperature"]["cold"]["low_per_1k"] is not None
    assert {b["from_c"] for b in m["episodes_by_coolant_c"]} == {20, 80}


# -- 5. associations respect timestamps and staleness ----------------------------------------------------


def test_associations_use_held_values_with_their_age_and_ignore_stale_ones(store):
    rows = drive(120, maf=in_window(60.0, 60.4, 0.0), egr_err=lambda t: 30.0 if 56 <= t < 64 else 0.0)
    sid = session(store, rows)
    ep = ap_of(store, sid)["maf"]["episodes"][0]
    ctx = ep["context"]
    # MAP polled 0.1 s after MAF: the held value at the episode start is the one BEFORE it, with its age
    assert ctx["map"]["fresh"] and 0 < ctx["map"]["age_s"] <= 0.5
    assert ctx["egr_error"]["value"] == 30.0 and ctx["egr_error"]["fresh"]
    assert ep["associations"]["egr_excursion_nearby"] is True

    # the same session, but EGR error stopped being reported 20 s before the episode: stale, not associated
    rows2 = [
        x
        for x in drive(120, maf=in_window(60.0, 60.4, 0.0))
        if not (x.pid in (EGR_ERR, EGR_CMD, COOLANT) and 40 <= x.t_mono < 120)
    ]
    sid2 = session(store, rows2, start=T0 + 10_000)
    ap2 = ap_of(store, sid2)
    ep2 = ap2["maf"]["episodes"][0]
    assert (
        ep2["context"]["egr_error"]["fresh"] is False
        and ep2["context"]["egr_error"]["age_s"] > M.AIRPATH_CONTEXT_MAX_AGE_S
    )
    assert any("egr_error: stale" in x for x in ep2["limitations"])
    assert ep2["associations"]["egr_excursion_nearby"] is None  # no EGR sample in the window: not evaluable
    assert ap2["correlations"]["egr_excursion_nearby"]["not_evaluable"] == 1
    # coolant is allowed to be older (it changes slowly): 24 s old is still fresh for the cold / warm split
    assert ep2["context"]["coolant"]["fresh"] and ep2["temperature_state"] == "warm"
    assert "not causal" in ap2["correlations"]["note"] or "not causation" in ap2["correlations"]["note"]


def test_map_change_and_symptom_marker_are_associations_with_both_sides_kept(store):
    rows = drive(
        300,
        maf=lambda t: 0.0 if (100 <= t < 100.4 or 200 <= t < 200.4) else 9.5,
        map_=lambda t: 90.0 if 99 <= t < 102 else 101.0,
    )
    sid = session(store, rows)
    store.add_annotation(
        sid, Annotation(t_wall=T0 + 205, label="unusual_engine_noise", event_type="unusual_engine_noise")
    )
    store.add_annotation(sid, Annotation(t_wall=T0 + 20, label="vibration"))  # free-text, nothing nearby
    ap = ap_of(store, sid)
    c = ap["correlations"]
    assert c["map_change"] == {
        "changed": 1,
        "unchanged": 1,
        "not_evaluable": 0,
        "screen": M.AIRPATH_MAP_CHANGE_KPA,
    }
    marks = {m["event"]: m for m in c["symptom_markers"]}
    assert marks["unusual_engine_noise"]["near_air_path_anomaly"] is True
    assert marks["vibration"]["near_air_path_anomaly"] is False
    assert any("MAP changed" in x for x in c["supporting"]) and any(
        "MAP stayed" in x for x in c["contradicting"]
    )
    assert any("vibration" in x and "no air-path anomaly" in x for x in c["contradicting"])


# -- 6. P115D in the air-path findings -------------------------------------------------------------------


def test_p115d_is_kept_raw_with_uncertainty_and_never_reads_as_no_air_path_fault(store):
    sid = session(store, drive(120, maf=in_window(60.0, 61.6, 0.0)))
    scan(store, sid, ("P115D",), "pre", mil=True)
    c = classify("P115D", MINI, None)
    assert (
        c["family"] == "manufacturer_defined"
        and c["description"] is None
        and c["description_status"] == "not_verified"
    )
    dt = dtc_summary(load_session(store, sid))
    assert dt["unverified_manufacturer_codes"] == ["P115D"] and dt["generic_air_path_codes"] == []
    d = dt["detected"][0]
    assert (d["code"], d["ecu"], d["status"], d["scan_phase"], d["raw_hex"]) == (
        "P115D",
        "7E8",
        "stored",
        "pre",
        "115D",
    )
    assert (
        "set before this session" in dt["by_code"][0]["timing"]
        and "cannot be tied" in dt["by_code"][0]["timing"]
    )
    assert dt["mil_on"] is True
    # owner-reported BMW codes are separate and never mixed with generic ones
    assert {c["code"] for c in dt["owner_reported_manufacturer_codes"]} == {"4862", "480A"}
    assert "4862" not in dt["codes"]

    f = {x.id: x for x in analyze_session(store, sid)}["air_path_egr_maf"]
    text = " ".join([*(e.text for e in f.contradicting), *(e.text for e in f.supporting)])
    assert "No generic DTC in the air-mass" not in text
    cond = " ".join(f.conditions)
    assert (
        "P115D" in cond
        and "NOT evidence that there is no air-path fault" in cond
        and "No verified description" in cond
    )
    assert "Owner-reported BMW DDE fault(s) 4862, 480A" in cond
    assert f.strength == "suggestive"  # MAF zero while running; P115D does not upgrade it to 'strong'
    assert any("MAF below 1 g/s" in e.text for e in f.supporting)
    # dde_4862 sees the same evidence
    assert any(
        "MAF below 1 g/s" in e.text
        for e in {x.id: x for x in analyze_session(store, sid)}["dde_4862"].supporting
    )


def test_code_set_during_the_session_is_not_tied_to_an_anomaly_time(store):
    sid = session(store, drive(120, maf=in_window(60.0, 60.4, 0.0)))
    scan(store, sid, (), "pre", mil=False)
    scan(store, sid, ("P115D",), "post", mil=True, t=120)
    b = dtc_summary(load_session(store, sid))["by_code"][0]
    assert "appeared during this session" in b["timing"] and "exact time is unknown" in b["timing"]


def test_a_verified_description_is_only_used_for_a_confirmed_ecu_variant(monkeypatch):
    from app.diagnostics import dtc_info

    monkeypatch.setitem(
        dtc_info.VERIFIED_MANUFACTURER_DESCRIPTIONS,
        "P115D",
        [
            {
                "description": "TEST ONLY",
                "vehicle_profile": MINI,
                "ecu_variant": "TEST-DDE",
                "source": "unit test",
            }
        ],
    )
    assert classify("P115D", MINI, None)["description"] is None
    assert classify("P115D", MINI, "OTHER")["description"] is None
    assert classify("P115D", MINI, "TEST-DDE")["description"] == "TEST ONLY"


def test_no_scan_means_unknown_not_no_faults(store):
    sid = session(store, drive(60))
    dt = ap_of(store, sid)["dtcs"]
    assert dt["scans"] == [] and dt["mil_on"] is None
    assert any("unknown" in n for n in dt["notes"])
    assert air_path_row(load_session(store, sid))["dtcs"] is None


# -- 7. real and synthetic baselines stay apart ---------------------------------------------------------------


def test_real_and_synthetic_sessions_never_share_air_path_baselines(store):
    def zeros(n):
        return lambda t: 0.0 if any(a <= t < a + 0.4 for a in range(20, 20 + 20 * n, 20)) else 9.5

    real = [
        session(store, drive(300, maf=zeros(1)), synthetic=False, start=T0 + i * 10_000) for i in range(3)
    ]
    before = {sid: ap_of(store, sid)["history"] for sid in real}
    hist_before = history_for(store, MINI)
    real_trend = next(t for t in hist_before["air_path_trends"] if t["cohort"]["origin"] == "real")

    syn = [
        session(store, drive(300, maf=zeros(10)), synthetic=True, start=T0 + 50_000 + i * 10_000)
        for i in range(3)
    ]
    for sid in real:
        assert ap_of(store, sid)["history"] == before[sid]
        assert ap_of(store, sid)["history"]["other_sessions"] == 2
    hist = history_for(store, MINI)
    trends = {t["cohort"]["origin"]: t for t in hist["air_path_trends"]}
    assert trends["real"] == real_trend and trends["real"]["sessions"] == 3
    assert trends["synthetic"]["sessions"] == 3
    assert trends["synthetic"]["medians"]["maf_zero_per_1k"] > trends["real"]["medians"]["maf_zero_per_1k"]
    rows = {r["id"]: r for r in hist["sessions"]}
    assert rows[syn[0]]["air_path"]["vs_cohort"]["other_sessions"] == 2
    assert (
        rows[real[0]]["air_path"]["vs_cohort"]["medians"]["maf_zero_per_1k"]
        == real_trend["medians"]["maf_zero_per_1k"]
    )


def test_trend_does_not_depend_on_a_single_recent_session(store):
    ids = [
        session(store, drive(200, maf=in_window(50.0, 50.4, 0.0)), synthetic=False, start=T0 + i * 86_400)
        for i in range(5)
    ]
    scan(store, ids[0], ("P115D",))
    scan(store, ids[3], ("P115D",))
    for sid in ids[1:3] + ids[4:]:
        scan(store, sid, (), mil=False)
    trend = history_for(store, MINI)["air_path_trends"][0]
    assert trend["sessions"] == 5 and len(trend["series"]) == 5
    assert trend["earlier_vs_later"] is not None
    rec = {x["code"]: x for x in trend["dtc_recurrence"]}
    assert rec["P115D"]["sessions"] == 2 and rec["P115D"]["share_of_scanned_sessions"] == 0.4
    assert trend["mil_on_sessions"] == 2
    # removing the latest session leaves a trend of the remaining four
    with store.tx() as db:
        db.execute("DELETE FROM samples WHERE session_id=?", (ids[-1],))
        db.execute("DELETE FROM dtc_scans WHERE session_id=?", (ids[-1],))
        db.execute("DELETE FROM sessions WHERE id=?", (ids[-1],))
    t2 = history_for(store, MINI)["air_path_trends"][0]
    assert t2["sessions"] == 4 and t2["dtc_recurrence"][0]["sessions"] == 2


# -- 8. old archives remain analysable and exportable --------------------------------------------------------


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.jsonl")))
def test_every_existing_fixture_still_replays_analyses_and_exports(store, name):
    sid = ingest_transcript((FIXTURES / name).read_text().splitlines(), store)
    files = build_files(store, sid)
    assert {
        "session.json",
        "samples.csv",
        "samples.json",
        "transcript.jsonl",
        "findings.json",
        "report.md",
    } <= set(files)
    findings = {f["id"]: f for f in json.loads(files["findings.json"])}
    s = store.get_session(sid)
    if s["vehicle_profile"] == MINI:
        assert "air_path_egr_maf" in findings
    if "monitoring.json" in files:
        mon = json.loads(files["monitoring.json"])
        assert mon["air_path"]["signals"]["maf"]["status"] in ("measured", "not_available", "no_valid_answer")
        assert "### Air path" in files["report.md"]
        for f in ("events.jsonl", "detected_events.json", "sampling_quality.json"):
            assert f in files
    assert build_zip(store, sid) == build_zip(store, sid)  # still byte-identical


def test_session_recorded_before_this_change_is_reanalysed_automatically(store):
    """A stored monitoring session has no air-path summary of its own: it is derived on load."""
    sid = ingest_transcript((FIXTURES / "mini-monitoring-shutdown-fan.jsonl").read_text().splitlines(), store)
    assert "air_path" not in json.dumps(store.get_session(sid)["meta"])
    m = summary_for(store, sid)
    assert m["air_path"]["status"] == "evaluated" and m["air_path"]["maf"]["zero_running"] == 0
    # its MAF zeros after the engine stop are not running anomalies
    assert m["air_path"]["maf"]["episode_count"] == 0
    row = next(r for r in history_for(store, MINI)["sessions"] if r["id"] == sid)
    assert row["air_path"]["maf_status"] == "measured" and row["engine_stops"] >= 1


# -- 9. monitoring outputs include the new data automatically ----------------------------------------------------


def test_reference_fixture_reproduces_the_first_real_recording_pattern(store):
    sid = ingest_transcript((FIXTURES / "mini-monitoring-air-path.jsonl").read_text().splitlines(), store)
    s = store.get_session(sid)
    assert s["synthetic"]  # stand-in, never presented as real data
    m = summary_for(store, sid)
    ap = m["air_path"]
    maf = ap["maf"]
    assert maf["zero_running"] >= 10 and maf["low_running"] > maf["zero_running"]
    assert maf["zero_engine_not_running"] > 0  # zeros with the engine off exist and are not counted
    assert maf["persistent_episodes"] >= 1 and maf["repeated_clusters"] >= 1
    assert {e["temperature_state"] for e in maf["episodes"]} == {"cold", "warm"}
    assert ap["egr_error"]["count"] >= 1 and ap["egr_error"]["both_directions"] >= 1
    assert ap["dtcs"]["codes"] == ["P115D"] and ap["dtcs"]["mil_on"] is True
    assert m["coolant"]["first"]["value"] <= 8 and 65 <= m["coolant"]["max_running"] <= 80
    # the session ends with ECU communication loss, not a measured engine stop
    assert [x["type"] for x in m["link"]] == ["ecu_communication_lost"] and m["shutdowns"] == []
    noise = next(x for x in ap["correlations"]["symptom_markers"] if x["event"] == "unusual_engine_noise")
    assert noise["near_air_path_anomaly"] is True

    files = build_files(store, sid)
    mon = json.loads(files["monitoring.json"])
    assert mon["air_path"]["maf"]["episode_count"] == maf["episode_count"]
    assert mon["heuristics"]["maf_low_gs"] == 1.0 and mon["heuristics"]["airpath_running_rpm"] == 600.0
    detected = json.loads(files["detected_events.json"])
    assert sum(1 for d in detected if d["type"].startswith("maf_")) >= maf["episode_count"]
    findings = {f["id"]: f for f in json.loads(files["findings.json"])}
    assert findings["air_path_egr_maf"]["strength"] == "suggestive"
    rep = files["report.md"]
    assert "### Air path (MAF / EGR / MAP / DTC)" in rep and "41 10 00 00" in rep and "P115D" in rep
    assert "description not verified" in rep and "not causation" in rep
    # raw data stays exactly as recorded: zeros are in samples.csv with their raw bytes
    assert ",16,7E8," not in files["samples.csv"]  # pid is written as hex
    assert any(
        line.split(",")[3] == "10" and line.split(",")[9] == "0000"
        for line in files["samples.csv"].splitlines()[1:]
    )


def test_history_api_exposes_air_path_statistics(store):
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    app = create_app(str(store.path))
    with TestClient(app) as c:
        for name in ("mini-monitoring-air-path.jsonl", "mini-monitoring-shutdown-fan.jsonl"):
            ingest_transcript((FIXTURES / name).read_text().splitlines(), store)
        h = c.get(f"/api/monitoring/history?vehicle={MINI}").json()
        assert h["air_path_trends"] and h["air_path_trends"][0]["sessions_with_maf"] == 2
        rows = h["sessions"]
        assert all("air_path" in r for r in rows)
        hit = next(r for r in rows if r["air_path"]["maf_episodes"])
        assert hit["air_path"]["maf_low_per_1k"] > 0 and hit["air_path"]["dtcs"] == ["P115D"]
        mon = c.get(f"/api/sessions/{hit['id']}/monitoring").json()
        assert mon["air_path"]["maf"]["episodes"]


# -- 10. recording workload and protocol safety unchanged ------------------------------------------------------------


def test_recording_workload_and_protocol_safety_are_unchanged(store):
    # the recording profile is exactly the one documented before this change: no new PIDs, no new tier
    assert MINI_EXTENDED_MONITORING.fast == (0x0C, 0x0D, 0x04, 0x10, 0x0B)
    assert MINI_EXTENDED_MONITORING.medium[:4] == (0x05, 0x42, 0x2C, 0x2D)
    stem, _, marks = AIR_PATH_FIXTURES["mini_air_path_anomalies"]
    sid = record_monitoring(store, "mini_air_path_anomalies", 180, marks)
    meta = store.get_session(sid)["meta"]
    sel = meta["selected"]
    allowed = {f"01{p:02X}" for tier in ("fast", "medium", "slow") for p in sel[tier]} | {"ATRV"}
    ex = store.exchanges(sid)
    rec = {e["request"] for e in ex if e["phase"] == "record"}
    assert rec <= allowed  # only the planned signals are polled while recording
    assert not any(e["request"].startswith("04") for e in ex)  # nothing is ever cleared
    scans = [e for e in ex if e["phase"].startswith("scan")]
    assert {e["request"] for e in scans} <= {"03", "07", "0A", "0101"}  # DTC reads only in the pre/post scans
    # analysis, findings, history and export are offline: no request, no new sample
    n_ex, n_s = len(ex), len(store.samples(sid))
    summary_for(store, sid)
    analyze_session(store, sid)
    history_for(store, MINI)
    build_files(store, sid)
    assert len(store.exchanges(sid)) == n_ex and len(store.samples(sid)) == n_s


def test_air_path_summary_is_memoised_and_stable(store):
    sid = session(store, drive(120, maf=in_window(60.0, 60.4, 0.0)))
    sd = load_session(store, sid)
    a, b = air_path_summary(sd), air_path_summary(sd)
    assert a == b and a["maf"] is b["maf"]
