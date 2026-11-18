"""DDE-06: BMW DDE inside the one continuous MINI recording. SYNTHETIC simulator data only:
passing says nothing about the real car or the real adapter."""

from __future__ import annotations

import dataclasses
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.adapters.simulator import SCENARIOS, SimDde, SimulatorTransport
from app.diagnostics.monitoring import history_for, summary_for
from app.manufacturer.bmw.synthetic import SYNTHETIC_VARIANT
from app.models.domain import Annotation
from app.service import Workbench
from app.storage.export import build_files, build_zip
from app.storage.transcript import ingest_file, ingest_transcript, transcript_lines

PROFILE = "mini_extended_monitoring"
FIX = Path(__file__).resolve().parents[2] / "fixtures"


def run(store, scenario, duration, opt_in=True, **kw):
    wb = Workbench(store)
    if opt_in:
        wb.set_dde_settings(opt_in=True, confirmed_variant=SYNTHETIC_VARIANT)
    sc = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    t = SimulatorTransport(sc, default_timeout_s=2.0)
    wb.attach(t, "simulator", sc.name)
    sid = wb.record(None, PROFILE, until_stop=True, max_duration_s=duration, **kw)
    return wb, sid, t


def ev(store, sid, *types):
    return [a for a in store.annotations(sid) if a.event_type in types]


def mark(store, sid, event_type, t_mono):
    first = min((s for s in store.samples(sid) if s["mode"] == 1), key=lambda s: s["t_mono"])
    store.add_annotation(
        sid,
        Annotation(
            t_wall=first["t_wall"] + (t_mono - first["t_mono"]), label=event_type, event_type=event_type
        ),
    )


@pytest.fixture(scope="module")
def drive(tmp_path_factory):
    """~23 min synthetic drive: city, regeneration-like phase, Wi-Fi outage, engine stop, ECU silence."""
    from app.storage.db import Store

    store = Store(tmp_path_factory.mktemp("dde") / "drive.sqlite3")
    wb, sid, t = run(store, "mini_dde_drive", 1400)
    stop = ev(store, sid, "engine_stopped")[0].t_mono
    mark(store, sid, "fan_running", stop + 5)
    mark(store, sid, "fan_stopped", stop + 25)
    yield store, wb, sid, t
    store.close()


def test_one_session_one_timeline_everything_in_order(drive):
    store, wb, sid, t = drive
    assert len(store.list_sessions()) == 1  # no separate DDE logger / session
    exs = store.exchanges(sid)
    assert [e["seq"] for e in exs] == sorted(e["seq"] for e in exs)
    monos = [e["t_mono"] for e in exs if e["phase"] == "record"]
    assert monos == sorted(monos)
    samples = store.samples(sid)
    dde = [s for s in samples if s.get("source") == "bmw_dde"]
    generic = [s for s in samples if s["mode"] == 1]
    assert dde and generic
    rec = [s for s in samples if s["mode"] in (0, 1, 0x100)]
    assert [s["t_mono"] for s in rec] == sorted(s["t_mono"] for s in rec)  # one interleaved timeline
    types = [a.event_type for a in store.annotations(sid)]
    for needed in (
        "engine_started", "dde_regeneration_started", "adapter_connection_lost", "dde_regeneration_unknown",
        "adapter_connection_restored", "engine_stopped", "ecu_communication_lost", "fan_running", "fan_stopped",
    ):  # fmt: skip
        assert needed in types, needed
    meta = store.get_session(sid)["meta"]
    assert meta["dde"]["status"] == "polling" and meta["dde"]["synthetic"] is True
    assert {p["metric"] for p in meta["dde"]["plan"]} >= {
        "dpf_regeneration_active",
        "dpf_soot_mass",
        "egr_actual",
    }
    assert all(p["tier"] in ("medium", "slow") for p in meta["dde"]["plan"])  # never the fast tier
    # a transaction cut by the Wi-Fi outage cannot restore: fail closed (ATZ on the next connection)
    assert meta["monitoring"]["dde"]["restore_failures"] <= len(ev(store, sid, "adapter_connection_lost"))
    assert t.sim.baseline()


def test_no_manufacturer_traffic_after_ecu_silence_or_during_outage(drive):
    store, wb, sid, t = drive
    lost = ev(store, sid, "ecu_communication_lost")[0].t_mono
    a_lost = ev(store, sid, "adapter_connection_lost")[0].t_mono
    a_back = ev(store, sid, "adapter_connection_restored")[0].t_mono
    dde_tx = [e for e in store.exchanges(sid) if e["request"] == "ATSH7B0"]
    assert dde_tx and all(e["t_mono"] < lost for e in dde_tx)
    assert not any(a_lost <= e["t_mono"] <= a_back for e in dde_tx)
    # after the outage the adapter is re-initialised by GENERIC polling before any DDE request
    first_dde = next(e["t_mono"] for e in dde_tx if e["t_mono"] > a_back)
    assert any(e["request"] == "ATZ" and a_lost < e["t_mono"] < first_dde for e in store.exchanges(sid))
    # only ATRV (adapter-local) while the ECU is silent: no vehicle request, no re-initialisation
    assert not any(e["request"] not in ("ATRV",) for e in store.exchanges(sid) if e["t_mono"] > lost + 1)


def test_regeneration_confirmed_only_by_fresh_dde_flag_and_kept_apart_from_inferred(drive):
    store, wb, sid, t = drive
    m = summary_for(store, sid)
    conf = m["dpf"]["confirmed_regenerations"]
    assert len(conf) == 1 and conf[0]["source"] == "bmw_dde" and conf[0]["origin"] == "measured"
    c = conf[0]
    # the synthetic regeneration phase runs 488..728 s; the Wi-Fi outage (700..730 s) hides its end:
    # the confirmed span stops at the last active reading before the outage, the end stays unknown
    assert 480 <= c["t_start_rel_s"] <= 560 and c["ended"] == "unknown (adapter connection lost)"
    assert c["t_end_rel_s"] < 700 and c["ended_between_rel_s"][1] is None
    assert not ev(store, sid, "dde_regeneration_ended")  # no claimed end across an unknown period
    gaps = m["dde"]["regeneration"]["unknown_gaps"]
    assert any(g["reason"] == "adapter connection lost" and g["state_before"] == "active" for g in gaps)
    assert any(d.get("status") == "confirmed" for d in m["detected_events"])
    assert all(
        d["status"] != "confirmed" for d in m["dpf"]["possible_regenerations"]
    )  # inferred stays possible
    d = m["dde"]
    assert d["regeneration"]["status"] == "measured" and "egr" in d["commanded_vs_actual"]
    assert all(s["provenance"] == "bmw_dde" for s in d["signals"])
    assert any("SYNTHETIC" in x for x in d["limitations"])
    # generic cadence kept: the fast tier was not stretched by DDE load
    assert m["sampling"]["final_effective_intervals"]["fast"] <= 1.0
    assert store.get_session(sid)["meta"]["monitoring"]["dde"]["final_scale"] >= 1.0


def test_exports_history_and_replay(drive):
    store, wb, sid, t = drive
    files = build_files(store, sid)
    assert "dde_monitoring.json" in files and "BMW DDE manufacturer values - SYNTHETIC" in files["report.md"]
    assert "confirmed by DDE" in files["report.md"]
    mon = json.loads(files["monitoring.json"])
    assert mon["dde"]["regeneration"]["confirmed"]
    assert "signal_key" in files["samples.csv"].splitlines()[0]
    with zipfile.ZipFile(io.BytesIO(build_zip(store, sid))) as zf:
        assert any(n.endswith("dde_monitoring.json") for n in zf.namelist())
    row = next(r for r in history_for(store, "mini-countryman-r60-diesel")["sessions"] if r["id"] == sid)
    assert row["dde_confirmed_regenerations"] == 1 and row["dde_soot_last"] is not None
    sid2 = ingest_transcript(transcript_lines(store, sid), store, label="replay")
    m2 = summary_for(store, sid2)
    assert len(m2["dpf"]["confirmed_regenerations"]) == 1
    assert m2["dde"]["catalog_version"] == "dde-catalog/synthetic-1"


def test_api_series_label_dde_signals(drive, tmp_path):
    from app.api.main import _series

    store, wb, sid, t = drive
    sigs = _series(store, sid)["signals"]
    dde = [s for s in sigs if s["provenance"] == "bmw_dde"]
    assert {s["signal_key"] for s in dde} >= {"dpf_soot_mass", "egr_actual"}
    assert all(s["pid"] > 0xFF for s in dde) and not any(
        s["provenance"] == "bmw_dde" and s["pid"] <= 0xFF for s in sigs
    )


def test_dde_disabled_keeps_generic_monitoring_and_unknown_not_false(store):
    wb, sid, t = run(store, "mini_dde_drive", 900, opt_in=False)
    meta = store.get_session(sid)["meta"]
    assert meta["dde"]["status"] == "not_polled" and meta["dde"]["plan"] == []
    assert not any(c.startswith("ATSH") for c in t.sim.received)
    m = summary_for(store, sid)
    assert m["dpf"]["confirmed_regenerations"] == [] and m["dde"]["regeneration"]["status"] == "not_available"
    assert m["drive"]["running_s"] > 600


def test_unsupported_adapter_stops_dde_once_generic_continues(store):
    wb, sid, t = run(store, "mini_city_drive", 300)  # fake adapter without addressing support
    assert sum(1 for c in t.sim.received if c == "ATSH7B0") == 1
    mon = store.get_session(sid)["meta"]["monitoring"]
    assert mon["dde"]["unsupported_adapter"]
    assert sum(1 for s in store.samples(sid) if s["mode"] == 1 and s["quality"] == "ok") > 300


def test_negative_responses_back_off_without_hurting_generic(store):
    sc = SCENARIOS["mini_dde_drive"]

    def responses(t, req):
        return bytes([0x7F, 0x22, 0x31]) if req in ("22F105", "22F106") else sc.dde.responses(t, req)

    scen = dataclasses.replace(sc, name="dde_neg", dde=SimDde("7B0", "7B8", responses, "5A", "F5"))
    wb, sid, t = run(store, scen, 400)
    egr = [s for s in store.samples(sid) if s.get("signal_key") == "egr_actual"]
    assert egr and all(s["quality"] == "negative" and s["value"] is None for s in egr)
    assert len(egr) <= 6  # suspended after 3, retried once after the back-off
    soot = [s for s in store.samples(sid) if s.get("signal_key") == "dpf_soot_mass"]
    assert soot and all(s["quality"] == "ok" for s in soot)
    av = {a["metric"]: a for a in store.get_session(sid)["meta"]["monitoring"]["dde"]["availability"]}
    assert (
        av["egr_actual"]["status"] in ("suspended", "executable")
        and av["egr_actual"]["last_quality"] == "negative"
    )


def test_slow_adapter_bounds_dde_share(store):
    slow = dataclasses.replace(SCENARIOS["mini_dde_drive"], name="dde_slow", delay=(0.12, 0.25))
    wb, sid, t = run(store, slow, 400)
    mon = store.get_session(sid)["meta"]["monitoring"]
    assert max(a.get("dde_scale", 1.0) for a in mon["adjustments"]) > 1.0  # DDE stretched first
    rpm = [s for s in store.samples(sid) if s["mode"] == 1 and s["pid"] == 0x0C and s["quality"] == "ok"]
    assert len(rpm) > 100  # generic monitoring stays usable


def test_old_real_style_sessions_are_untouched(store):
    sid = ingest_file(FIX / "mini-monitoring-air-path.jsonl", store)
    before = store.samples(sid)
    m = summary_for(store, sid)
    assert m["dde"]["status"] == "not_recorded" and m["dpf"]["confirmed_regenerations"] == []
    assert store.samples(sid) == before and not any("source" in s for s in before)
    files = build_files(store, sid)
    assert "dde_monitoring.json" not in files and "signal_key" not in files["samples.csv"].splitlines()[0]
