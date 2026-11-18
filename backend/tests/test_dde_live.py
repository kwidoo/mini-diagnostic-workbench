"""DDE-05: verified (here: synthetic) DDE live parameters as provenance-tagged readings."""

import csv
import dataclasses
import io
import sqlite3

import pytest

from app.acquisition.recorder import compute_metrics
from app.adapters.simulator import SCENARIOS, SimDde, SimulatorTransport
from app.manufacturer.bmw.catalog import ExecutionContext
from app.manufacturer.bmw.guarded import DdeResult
from app.manufacturer.bmw.live import DdeLiveReader, signal_pid, to_reading
from app.manufacturer.bmw.runtime import DdeRuntime
from app.manufacturer.bmw.synthetic import SYNTHETIC_VARIANT, synthetic_catalog
from app.models.domain import MANUFACTURER_MODE
from app.obd.client import ObdClient
from app.service import Workbench
from app.storage.db import Store
from app.storage.export import SAMPLE_COLUMNS, build_files
from app.storage.transcript import ingest_transcript, transcript_lines

SC = "mini_dde_synthetic"
SYN_OK = ExecutionContext(opted_in=True, confirmed_variant=SYNTHETIC_VARIANT, synthetic_session=True)


def _wb(store, opt_in=True, scenario=SC):
    wb = Workbench(store)
    if opt_in:
        wb.set_dde_settings(opt_in=True, confirmed_variant=SYNTHETIC_VARIANT)
    sc = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    wb.attach(SimulatorTransport(sc), "simulator", sc.name)
    return wb


@pytest.mark.parametrize(
    "rid,payload,quality,value,status",
    [
        ("syn.dpf_soot_mass", "62F1020190", "ok", 4.0, None),  # 400 x 0.01 g
        ("syn.dpf_diff_pressure", "62F10301F4", "ok", 50.0, None),  # 500 x 0.1 hPa
        ("syn.egr_actual", "62F10603E8", "ok", 100.0, None),  # upper limit inclusive
        ("syn.egr_actual", "62F10603E9", "error", None, None),  # 100.1 % outside the documented range
        ("syn.dpf_regeneration_active", "62F10100", "ok", 0.0, {"active": False}),
        ("syn.dpf_regeneration_active", "62F10102", "error", None, None),  # undefined status bit
        ("syn.dpf_soot_mass", "62F10201", "error", None, None),  # too short
        ("syn.dpf_soot_mass", "62F1030190", "error", None, None),  # another identifier echoed
    ],
)
def test_golden_decoders_limits_and_status_semantics(rid, payload, quality, value, status):
    v = synthetic_catalog().interpret(rid, bytes.fromhex(payload), SYN_OK)
    assert (v.quality, v.value, v.status) == (quality, value, status)


def test_wrong_variant_is_raw_only_never_a_value():
    ctx = dataclasses.replace(SYN_OK, confirmed_variant="OTHER-DDE")
    v = synthetic_catalog().interpret("syn.dpf_soot_mass", bytes.fromhex("62F1020190"), ctx)
    assert v.quality == "raw_only" and v.value is None and v.raw_hex == "62F1020190"


@pytest.mark.parametrize(
    "q", ["no_data", "negative", "timeout", "disconnected", "error", "unsupported_adapter"]
)
def test_failures_never_appear_as_measurements(q):
    e = synthetic_catalog().entries["syn.dpf_soot_mass"]
    r = to_reading(
        DdeResult(e.request_id, e.metric, e.kind, q, value=12.3, text="x", nrc="31", raw_hex="7F2231"), e
    )
    assert r.value is None and r.text is None and r.quality != "ok" and r.source == "bmw_dde"
    assert to_reading(DdeResult(e.request_id, e.metric, e.kind, "not_attempted"), e) is None


def test_inventory_stores_tagged_readings_apart_from_generic_obd(store):
    wb = _wb(store)
    sid = wb.inventory()
    rows = store.samples(sid)
    dde = [r for r in rows if r.get("source") == "bmw_dde"]
    generic = [r for r in rows if "source" not in r]
    assert generic and all(r["mode"] == 1 for r in generic)
    keys = {r["signal_key"] for r in dde}
    assert {"dpf_soot_mass", "egr_actual", "air_mass_target", "dpf_regeneration_active"} <= keys
    for r in dde:
        assert r["mode"] == MANUFACTURER_MODE and r["pid"] == signal_pid(r["signal_key"]) and r["pid"] > 0xFF
        assert r["ecu"] == "7B8/F5" and r["catalog_version"].startswith("dde-catalog/synthetic")
        assert r["quality"] == "ok" and r["request_id"].startswith("syn.")
    regen = next(r for r in dde if r["signal_key"] == "dpf_regeneration_active")
    assert regen["status_json"] == '{"active": false}'  # t ~ 0: idle phase of the synthetic drive
    # sampling metrics and charts stay generic: DDE samples are not mixed into PID statistics
    assert all(m.mode in (0, 1) for m in compute_metrics(rows))
    meta = store.get_session(sid)["meta"]["dde_live"]
    by = {m["metric"]: m for m in meta["availability"]}
    assert by["dpf_soot_mass"]["status"] == "executable" and by["dpf_soot_mass"]["ok"] == 1
    assert by["boost_actual"]["status"] == "unavailable"  # no entry, not even synthetic


def test_disabled_dde_sends_nothing_and_keeps_inventory_generic(store):
    wb = _wb(store, opt_in=False)
    sid = wb.inventory()
    assert not any("source" in r for r in store.samples(sid))
    assert not any(c.startswith("ATSH") for c in wb.transport.sim.received)
    assert {m["status"] for m in store.get_session(sid)["meta"]["dde_live"]["availability"]} == {
        "blocked",
        "unavailable",
    }


def test_exports_and_replay_keep_provenance(store):
    wb = _wb(store)
    sid = wb.inventory()
    files = build_files(store, sid)
    header = next(csv.reader(io.StringIO(files["samples.csv"])))
    assert header[: len(SAMPLE_COLUMNS)] == SAMPLE_COLUMNS and "signal_key" in header
    assert '"source": "bmw_dde"' in files["samples.json"]
    lines = transcript_lines(store, sid)
    assert any('"type": "manufacturer_reading"' in x for x in lines)
    sid2 = ingest_transcript(lines, store, label="replay")
    a = [r for r in store.samples(sid) if r.get("source")]
    b = [r for r in store.samples(sid2) if r.get("source")]
    assert [(r["signal_key"], r["value"], r["decoder_version"]) for r in a] == [
        (r["signal_key"], r["value"], r["decoder_version"]) for r in b
    ]


def test_old_database_is_migrated_additively(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, kind TEXT NOT NULL, vehicle_profile TEXT NOT NULL,
          source TEXT NOT NULL, synthetic INTEGER NOT NULL DEFAULT 0, label TEXT NOT NULL DEFAULT '',
          started_at REAL NOT NULL, ended_at REAL, meta TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE samples (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), t_wall REAL,
          t_mono REAL, mode INTEGER, pid INTEGER, ecu TEXT, name TEXT, unit TEXT, value REAL, text TEXT,
          raw_hex TEXT, quality TEXT, detail TEXT, latency_ms REAL);
        INSERT INTO sessions VALUES ('old', 'record', 'mini-countryman-r60-diesel', 'real', 0, '', 1.0, 2.0, '{}');
        INSERT INTO samples VALUES (1, 'old', 1.0, 0.5, 1, 12, '7E8', 'Engine RPM', 'rpm', 801.0, NULL, '0C84', 'ok', NULL, 30.0);
        """
    )
    db.commit()
    db.close()
    s = Store(path)
    (row,) = s.samples("old")
    assert set(row) == {
        "t_wall", "t_mono", "mode", "pid", "ecu", "name", "unit", "value", "text", "raw_hex", "quality", "detail",
        "latency_ms",
    }  # fmt: skip
    assert row["value"] == 801.0
    s.close()


def test_signal_that_keeps_failing_is_suspended_with_backoff():
    sc = SCENARIOS[SC]
    base = sc.dde

    def responses(t, req):
        return None if req == "22F104" else base.responses(t, req)  # exhaust temperature: never answers

    scen = dataclasses.replace(sc, name="dde_flaky", dde=SimDde("7B0", "7B8", responses, "5A", "F5"))
    t = SimulatorTransport(scen)
    c = ObdClient(t)
    c.initialize()
    c.discover()
    live = DdeLiveReader(DdeRuntime(synthetic_catalog(), SYN_OK, c))
    for _ in range(3):
        assert live.read("exhaust_temperature_pre_dpf").quality == "no_data"
    assert not live.due("exhaust_temperature_pre_dpf")
    av = {a["metric"]: a for a in live.availability()}
    assert (
        av["exhaust_temperature_pre_dpf"]["status"] == "suspended"
        and av["exhaust_temperature_pre_dpf"]["retry_after_s"] == 60.0
    )
    assert live.read("dpf_soot_mass").quality == "ok" and av["dpf_soot_mass"]["status"] == "executable"
    t.clock.sleep(61)
    assert live.due("exhaust_temperature_pre_dpf")
