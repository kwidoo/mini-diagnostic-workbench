"""CLI smoke tests - no hardware."""

import json
import socket
import zipfile
from pathlib import Path

import pytest

from app.cli import main

FIX = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def run(tmp_path, capsys):
    db = str(tmp_path / "cli.sqlite3")

    def _run(*args):
        code = main(["--db", db, *args])
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def test_profiles_and_selection(run):
    code, out, _ = run("profiles")
    assert code == 0 and "* mini-countryman-r60-diesel" in out and "porsche-boxster-986-mk2" in out
    code, out, _ = run("profiles", "--use", "porsche-boxster-986-mk2")
    assert "* porsche-boxster-986-mk2" in out


def test_probe_against_simulator_and_closed_port(run):
    code, out, err = run("probe", "--simulator", "healthy_idle")
    assert code == 0 and "ELM327 v2.3" in out and "CAN 11/500" in out and "SYNTHETIC" in err
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    code, out, err = run("probe", "--host", "127.0.0.1", "--port", str(port), "--timeout", "0.5")
    assert code == 2 and "cannot reach adapter" in err and "V-LINK" in err


def test_scan_inventory_record_analyze_export(run, tmp_path):
    code, out, err = run("scan", "--simulator", "dpf_history")
    assert (
        code == 0
        and "stored   : P2002 [raw 2002, ECU 7E8]" in out
        and "freeze frame: present for P2002" in out
    )
    assert "VIN: not_requested" in out and "READ-ONLY" in err

    code, out, _ = run("inventory", "--simulator", "unsupported_pids")
    assert (
        code == 0
        and "NOT SUPPORTED" in out
        and "10 Mass air flow (MAF)" in out
        and "DPF soot load: unavailable" in out
    )

    code, out, _ = run("record", "--simulator", "rough_idle", "--duration", "2", "--profile", "idle")
    assert code == 0 and "Engine speed" in out
    code, out, _ = run("--json", "sessions")
    sessions = json.loads(out)
    assert [s["kind"] for s in sessions] == ["scan", "snapshot", "record"]
    sid = sessions[-1]["id"]

    code, out, _ = run("annotate", "--label", "MIL on")
    assert code == 0
    code, out, _ = run("analyze", "--session", sid)
    assert code == 0 and "SYNTHETIC DATA" in out and "Idle RPM stability" in out and "evidence:" in out

    target = tmp_path / "x.zip"
    code, out, _ = run("export", "--session", sid, "--format", "zip", "--out", str(target))
    assert code == 0 and "VIN redacted" in out
    assert any(n.endswith("samples.csv") for n in zipfile.ZipFile(target).namelist())
    md = tmp_path / "r.md"
    assert (
        run("export", "--format", "md", "--out", str(md))[0] == 0
        and "# Diagnostic session report" in md.read_text()
    )


def test_replay_and_analyze_fixture(run):
    code, out, _ = run("replay", str(FIX / "rough-idle.jsonl"))
    assert code == 0 and "SYNTHETIC record" in out
    code, out, _ = run("--json", "analyze")
    fs = {f["id"]: f for f in json.loads(out)}
    assert fs["idle_rpm_stability"]["strength"] == "strong"


def test_porsche_profile_over_kline(run):
    code, out, _ = run(
        "--vehicle", "porsche-boxster-986-mk2", "scan", "--simulator", "porsche_bank_imbalance"
    )
    assert code == 0 and "pending  : P0174" in out and "permanent: NOT AVAILABLE (no_data)" in out


def test_errors(run):
    assert run("analyze", "--session", "nope")[0] == 2
    assert run("analyze")[0] == 2  # nothing stored yet
    assert run("replay", "/nonexistent.jsonl")[0] == 2
    with pytest.raises(SystemExit):
        main(["raw", "04"])  # there is no raw command


def test_record_until_stop_with_monitoring_profile_and_events(run):
    code, out, err = run(
        "record", "--simulator", "mini_city_drive", "--profile", "mini_extended_monitoring",
        "--until-stop", "--max-duration", "4", "--drive-type", "city", "--no-post-scan",
    )  # fmt: skip
    assert code == 0, err
    assert "recording until stopped (safety limit 4 s)" in err and "recording (medium):" in err
    assert "not supported, skipped:" in err and "7A" in err  # DPF PIDs this ECU does not offer
    assert "ATRV" in out and "session " in out
    code, out, err = run("annotate", "--event", "fan_running", "--note", "after stop")
    assert code == 0 and "fan_running" in out
    code, out, _ = run("annotate", "--event", "fan_stopped")
    assert code == 0
    code, out, _ = run("monitoring")
    assert code == 0 and "fan runs (manual markers):" in out and "voltage adapter:" in out
    assert "Calculated soot mass: not_available" in out
    code, out, _ = run("drive-type", "highway")
    assert code == 0 and "highway" in out
    code, out, _ = run("sessions")
    assert "(highway)" in out
    code, out, _ = run("--json", "monitoring")
    m = json.loads(out)
    assert m["fan"]["runs"][0]["complete"] is True and m["is_monitoring_session"] is True


def test_annotate_rejects_unknown_event(run):
    run("record", "--simulator", "healthy_idle", "--duration", "1", "--no-pre-scan", "--no-post-scan")
    with pytest.raises(SystemExit):
        run("annotate", "--event", "warp_drive")
