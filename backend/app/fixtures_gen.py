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


def generate(out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for scenario, (stem, duration, marks) in FIXTURES.items():
        store = Store(":memory:")
        sid = record_scenario(store, scenario, duration, marks)
        path = out / f"{stem}.jsonl"
        path.write_text("\n".join(transcript_lines(store, sid)) + "\n", encoding="utf-8")
        paths.append(path)
        store.close()
    return paths
