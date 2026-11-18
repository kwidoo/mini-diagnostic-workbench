"""Generate the synthetic fixtures in fixtures/ (deterministic, virtual clock, no hardware)."""

from __future__ import annotations

from pathlib import Path

from app.adapters.simulator import SCENARIOS, SimulatorTransport
from app.models.domain import Annotation
from app.service import Workbench
from app.storage.db import Store
from app.storage.transcript import transcript_lines

#: scenario -> (file stem, duration s, [(offset s, label)])
FIXTURES: dict[str, tuple[str, float, list[tuple[float, str]]]] = {
    "healthy_idle": ("healthy-idle", 120, []),
    "rough_idle": (
        "rough-idle",
        120,
        [(21, "rough idle started"), (39, "rough idle stopped"), (64, "vibration")],
    ),
    "egr_mismatch": ("egr-airflow-mismatch", 90, [(5, "MIL on")]),
    "dpf_history": ("dpf-dtc-history", 60, [(5, "MIL on")]),
    "wifi_drop": ("wifi-disconnect", 30, []),
    "unsupported_pids": ("unsupported-pids", 30, []),
    "slow_adapter": ("slow-adapter", 60, []),
    "porsche_healthy_idle": ("porsche-healthy-idle", 90, []),
    "porsche_bank_imbalance": ("porsche-bank-imbalance", 90, []),
}


def record_scenario(
    store: Store, scenario: str, duration_s: float, marks: list[tuple[float, str]], seed: int = 1
) -> str:
    wb = Workbench(store)
    wb.set_profile(SCENARIOS[scenario].vehicle)
    transport = SimulatorTransport(scenario, seed=seed, default_timeout_s=2.0)
    wb.attach(transport, "simulator", scenario)
    sid = wb.record(duration_s, "idle", label=f"synthetic {scenario}", freeze_frame=True)
    rec = [s for s in store.samples(sid) if s["mode"] == 1]
    if rec:
        t0_mono, t0_wall = rec[0]["t_mono"], rec[0]["t_wall"]
        for off, label in marks:
            store.add_annotation(
                sid,
                Annotation(t_wall=t0_wall + off, t_mono=t0_mono + off, label=label, note="synthetic marker"),
            )
    wb.disconnect()
    return sid


#: continuous-monitoring fixtures: scenario -> (file stem, max duration s, [(s after first sample, event, note)])
MONITORING_FIXTURES: dict[str, tuple[str, float, list[tuple[float, str, str]]]] = {
    "mini_ignition_off": (
        "mini-monitoring-shutdown-fan",
        180,
        [
            (70, "fan_running", "synthetic manual marker"),
            (85, "ignition_off", "synthetic manual marker"),
            (150, "fan_stopped", "synthetic manual marker"),
        ],
    ),
}


#: anonymised stand-in for the first real recording (session 20261118-143936-c028): same format as
#: MONITORING_FIXTURES, kept apart because it exercises the air path, not shutdown / fan behaviour
AIR_PATH_FIXTURES: dict[str, tuple[str, float, list[tuple[float, str, str]]]] = {
    "mini_air_path_anomalies": (
        "mini-monitoring-air-path",
        600,
        [
            (245, "unusual_engine_noise", "synthetic manual marker"),
            (402, "rough_idle", "synthetic manual marker"),
        ],
    ),
}


def record_monitoring(
    store: Store, scenario: str, duration_s: float, marks: list[tuple[float, str, str]], seed: int = 1
) -> str:
    wb = Workbench(store)
    wb.set_profile(SCENARIOS[scenario].vehicle)
    wb.attach(SimulatorTransport(scenario, seed=seed, default_timeout_s=2.0), "simulator", scenario)
    sid = wb.record(None, "mini_extended_monitoring", label=f"synthetic {scenario}", until_stop=True,
                    max_duration_s=duration_s)  # fmt: skip
    rec = [s for s in store.samples(sid) if s["mode"] == 1]
    if rec:
        t0_wall = rec[0]["t_wall"]
        for off, event, note in marks:
            store.add_annotation(
                sid, Annotation(t_wall=t0_wall + off, label=event, note=note, event_type=event)
            )
    wb.disconnect()
    return sid


def generate(out_dir: str | Path, only: str | None = None) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for scenario, (stem, duration, mmarks) in {**MONITORING_FIXTURES, **AIR_PATH_FIXTURES}.items():
        if only and stem != only:
            continue
        store = Store(":memory:")
        sid = record_monitoring(store, scenario, duration, mmarks)
        path = out / f"{stem}.jsonl"
        path.write_text("\n".join(transcript_lines(store, sid)) + "\n", encoding="utf-8")
        paths.append(path)
        store.close()
    for scenario, (stem, duration, marks) in FIXTURES.items():
        if only and stem != only:
            continue
        store = Store(":memory:")
        sid = record_scenario(store, scenario, duration, marks)
        path = out / f"{stem}.jsonl"
        path.write_text("\n".join(transcript_lines(store, sid)) + "\n", encoding="utf-8")
        paths.append(path)
        store.close()
    return paths
