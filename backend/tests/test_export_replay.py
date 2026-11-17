import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.adapters.simulator import SYNTHETIC_VIN, SimulatorTransport
from app.fixtures_gen import FIXTURES, record_scenario
from app.service import Workbench
from app.storage.export import SAMPLE_COLUMNS, build_files, build_zip
from app.storage.transcript import ingest_file, ingest_transcript, transcript_lines

FIX = Path(__file__).resolve().parents[2] / "fixtures"


def test_zip_is_reproducible_and_complete(store):
    sid = record_scenario(store, "rough_idle", 15, [(3, "vibration")])
    z1, z2 = build_zip(store, sid), build_zip(store, sid)
    assert z1 == z2
    with zipfile.ZipFile(io.BytesIO(z1)) as zf:
        names = sorted(Path(n).name for n in zf.namelist())
        assert names == sorted(
            [
                "README.txt",
                "session.json",
                "samples.csv",
                "samples.json",
                "dtcs.json",
                "annotations.csv",
                "transcript.jsonl",
                "findings.json",
                "report.md",
            ]
        )
        assert zf.testzip() is None


def test_csv_and_json_match_the_store(store):
    sid = record_scenario(store, "egr_mismatch", 10, [(2, "MIL on")])
    files = build_files(store, sid)
    rows = list(csv.DictReader(io.StringIO(files["samples.csv"])))
    samples = store.samples(sid)
    assert list(rows[0]) == SAMPLE_COLUMNS and len(rows) == len(samples)
    rpm = [r for r in rows if r["pid"] == "0C"]
    assert float(rpm[0]["value"]) > 500 and rpm[0]["unit"] == "rpm" and rpm[0]["raw_hex"]
    assert float(rows[-1]["t_rel_s"]) > float(rows[0]["t_rel_s"]) >= 0
    assert json.loads(files["samples.json"]) == samples
    dtcs = json.loads(files["dtcs.json"])
    assert dtcs[0]["dtcs"][0] == {"code": "P0401", "raw_hex": "0401", "kind": "stored", "ecu": "7E8"}
    assert json.loads(files["session.json"])["synthetic"] is True
    assert "MIL on" in files["annotations.csv"]
    report = files["report.md"]
    assert "SYNTHETIC DATA" in report and "P0401" in report and "not a probability" in report
    assert all(json.loads(line) for line in files["transcript.jsonl"].splitlines())


def test_missing_values_are_empty_cells_not_zeros(store):
    sid = record_scenario(store, "wifi_drop", 15, [])
    rows = list(csv.DictReader(io.StringIO(build_files(store, sid)["samples.csv"])))
    bad = [r for r in rows if r["quality"] == "disconnected"]
    assert bad and all(r["value"] == "" for r in bad)


def test_vin_is_redacted_by_default_and_included_only_with_consent(store):
    wb = Workbench(store)
    wb.attach(SimulatorTransport(), "simulator", "healthy_idle")
    sid = wb.scan(read_vin=True)
    assert store.get_session(sid)["meta"]["vehicle_info"]["vin"] == SYNTHETIC_VIN  # kept locally
    vin_hex = SYNTHETIC_VIN.encode().hex().upper()
    redacted = build_files(store, sid)
    for name, text in redacted.items():
        assert SYNTHETIC_VIN not in text, name
        assert vin_hex[:20] not in text.replace(" ", ""), name
    assert "[REDACTED]" in redacted["session.json"] and '"status": "redacted"' in redacted["transcript.jsonl"]
    assert SYNTHETIC_VIN in build_files(store, sid, include_vin=True)["session.json"]
    assert SYNTHETIC_VIN.encode() not in build_zip(store, sid)


def test_exported_transcript_replays_to_the_same_data(store):
    sid = record_scenario(store, "dpf_history", 10, [(2, "MIL on")])
    sid2 = ingest_transcript(transcript_lines(store, sid), store)
    key = lambda r: (r["t_mono"], r["pid"], r["ecu"], r["value"], r["raw_hex"], r["quality"])  # noqa: E731
    assert [key(r) for r in store.samples(sid2)] == [key(r) for r in store.samples(sid)]
    a, b = store.scans(sid), store.scans(sid2)
    assert [(s.phase, [d.model_dump() for d in s.dtcs], s.mil_on) for s in a] == [
        (s.phase, [d.model_dump() for d in s.dtcs], s.mil_on) for s in b
    ]
    assert b[0].freeze_frame.dtc == "P2002" and len(b[0].freeze_frame.readings) == len(
        a[0].freeze_frame.readings
    )
    s2 = store.get_session(sid2)
    assert s2["source"] == "replay" and s2["synthetic"] is True
    assert [a.label for a in store.annotations(sid2)] == ["MIL on"]
    assert s2["meta"]["metrics"] == store.get_session(sid)["meta"]["metrics"]


@pytest.mark.parametrize("stem", [v[0] for v in FIXTURES.values()])
def test_repo_fixtures_are_labelled_synthetic_and_replayable(store, stem):
    path = FIX / f"{stem}.jsonl"
    text = path.read_text()
    header = json.loads(text.splitlines()[0])
    assert header["synthetic"] is True and "SYNTHETIC" in header["note"]
    assert SYNTHETIC_VIN not in text and "0902" not in {
        json.loads(x).get("request") for x in text.splitlines()[1:50]
    }
    sid = ingest_file(path, store)
    assert store.get_session(sid)["synthetic"] is True and store.samples(sid)


def test_ingest_rejects_other_files(store):
    with pytest.raises(ValueError):
        ingest_transcript(['{"type":"exchange"}'], store)
    with pytest.raises(ValueError):
        ingest_transcript([], store)


def test_replay_never_interprets_forbidden_requests(store):
    lines = transcript_lines(store, record_scenario(store, "healthy_idle", 3, []))
    bad = json.loads(lines[5])
    bad.update(request="04", phase="record")
    sid = ingest_transcript(lines + [json.dumps(bad)], store)
    assert store.get_session(sid)["meta"]["replay_skipped_requests"] == ["04"]
