"""Fake ELM327 for offline work: protocol core, in-process transport, TCP server.

EVERYTHING HERE IS SYNTHETIC. The signal shapes are invented to exercise the
software; they do not reproduce measurements from any real MINI or Porsche.
"""

from __future__ import annotations

import math
import random
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.adapters.base import Transport, VirtualClock
from app.parameters.standard_pids import BITMAP_PIDS, encode_dtc

SYNTHETIC_VIN = "SYNTHETICVIN00001"
SYNTHETIC_CAL = "SYNTHETIC-CAL-01"


def _clamp(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(v))))


def _u16(v: float) -> bytes:
    return _clamp(v, 0, 0xFFFF).to_bytes(2, "big")


def _u8(v: float) -> bytes:
    return bytes([_clamp(v, 0, 255)])


_ENCODERS: dict[int, Callable[[float], bytes]] = {
    **{p: (lambda v: _u8(v * 255 / 100)) for p in (0x04, 0x11, 0x2C, 0x2F, 0x45, 0x49)},
    **{p: (lambda v: _u8(v + 40)) for p in (0x05, 0x0F, 0x46, 0x5C)},
    **{p: (lambda v: _u8((v + 100) * 128 / 100)) for p in (0x06, 0x07, 0x08, 0x09, 0x2D)},
    **{p: _u8 for p in (0x0B, 0x0D, 0x33)},
    0x0C: lambda v: _u16(v * 4),
    0x0E: lambda v: _u8((v + 64) * 2),
    0x10: lambda v: _u16(v * 100),
    **{p: (lambda v: _u8(v * 200) + b"\xff") for p in (0x14, 0x15, 0x18, 0x19)},
    0x1F: _u16,
    0x22: lambda v: _u16(v / 0.079),
    0x23: lambda v: _u16(v / 10),
    0x59: lambda v: _u16(v / 10),
    0x42: lambda v: _u16(v * 1000),
}


def encode_pid(pid: int, value: float) -> bytes:
    return _ENCODERS[pid](value)


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    vehicle: str
    signals: Callable[[float, random.Random], dict[int, float]]
    family: str = "can"  # can | kline
    protocol_number: str = "6"
    protocol_name: str = "ISO 15765-4 (CAN 11/500)"
    compression_ignition: bool = True
    stored: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    permanent: tuple[str, ...] = ()
    mode0a: bool = True
    mode02: bool = True
    mode06: str = "ok"  # ok | negative | no_data
    mode09: tuple[int, ...] = (0x02, 0x04)
    freeze: tuple[str, tuple[tuple[int, float], ...]] | None = None
    raw_only_pids: tuple[int, ...] = ()  # advertised + answered with opaque bytes
    delay: tuple[float, float] = (0.02, 0.05)
    drop_after_s: float | None = None
    mil: bool = False
    second_ecu: bool = True
    synthetic: bool = True
    # t -> does the engine ECU answer OBD requests? (None: always). Models ignition off / sleeping ECU.
    ecu_online: Callable[[float], bool] | None = None
    # t -> adapter supply voltage for ATRV (None: constant 14.1 V). Adapter-local, works with the ECU asleep.
    adapter_voltage: Callable[[float], float] | None = None


# -- synthetic signal generators ------------------------------------------------

_ROUGH_WINDOWS = ((20.0, 38.0), (62.0, 85.0), (104.0, 112.0))


def _in_rough(t: float) -> bool:
    tm = t % 120.0
    return any(a <= tm < b for a, b in _ROUGH_WINDOWS)


def _diesel(rough: bool = False, egr_error: float = 1.0, maf_base: float = 9.5):
    def fn(t: float, rng: random.Random) -> dict[int, float]:
        n = rng.gauss
        rpm = 800 + 6 * math.sin(0.9 * t) + n(0, 4)
        maf = maf_base + n(0, 0.15)
        mapv = 100 + n(0, 0.4)
        rail = 30000 + n(0, 150)
        if rough and _in_rough(t):
            osc = math.sin(2 * math.pi * 0.8 * t)
            rpm += 130 * osc + n(0, 25)
            maf += 2.2 * math.sin(2 * math.pi * 0.8 * t + 0.5) + n(0, 0.3)
            mapv += 2 * osc
            rail += 1800 * osc
        return {
            0x04: 22 + n(0, 1), 0x05: min(88.0, 62 + 0.12 * t), 0x0B: mapv, 0x0C: rpm, 0x0D: 0,
            0x0F: 27, 0x10: maf, 0x11: 0, 0x1F: t, 0x23: rail, 0x2C: 38 + n(0, 1),
            0x2D: egr_error + n(0, 1.5), 0x33: 101, 0x42: 14.1 + n(0, 0.03), 0x46: 18, 0x49: 0,
        }  # fmt: skip

    return fn


def _petrol_flat6(ltft_b2: float = 1.5):
    def fn(t: float, rng: random.Random) -> dict[int, float]:
        n = rng.gauss
        return {
            0x04: 18 + n(0, 1), 0x05: min(92.0, 70 + 0.1 * t), 0x06: n(0, 2.0), 0x07: 1.5 + n(0, 0.2),
            0x08: n(0, 2.0), 0x09: ltft_b2 + n(0, 0.2), 0x0C: 700 + 5 * math.sin(0.7 * t) + n(0, 5),
            0x0D: 0, 0x0E: 8 + n(0, 1), 0x0F: 30, 0x10: 4.6 + n(0, 0.1), 0x11: 3.5,
            0x14: 0.45 + 0.35 * math.sin(2.1 * t), 0x18: 0.45 + 0.35 * math.sin(2.1 * t + 1),
        }  # fmt: skip

    return fn


def _minimal(t: float, rng: random.Random) -> dict[int, float]:
    return {0x04: 22, 0x05: 80, 0x0C: 800 + rng.gauss(0, 4), 0x0D: 0, 0x0F: 27}


# -- synthetic drive cycles for the monitoring recorder ---------------------------------------
# A drive is a list of (phase, duration_s). Phases:
#   "ign"    ignition on, engine off (ECU answers, RPM 0)
#   "idle"   engine idling, stationary
#   "city"   stop-and-go up to ~50 km/h
#   "road"   steady ~110 km/h
#   "regen"  stationary idle with the indirect regeneration pattern (EGR closed, RPM/MAF raised)
#   "off"    ignition off: the ECU stops answering; only the adapter voltage remains
# Everything is invented to exercise the software, not modelled on a real MINI.


@dataclass(frozen=True)
class DriveCycle:
    phases: tuple[tuple[str, float], ...]
    ambient_c: float = 12.0
    coolant_start_c: float = 15.0
    plateau_c: float = 88.0
    warm_tau_s: float = 240.0
    low_voltage: tuple[tuple[float, float, float], ...] = ()  # (t_from, t_to, volts) while running
    step_drops: tuple[tuple[float, float], ...] = ()  # (t, drop V for 2 s)
    maf_dropouts: tuple[
        tuple[float, float, float], ...
    ] = ()  # (t_from, t_to, reported MAF g/s) while running
    egr_err_swings: tuple[
        tuple[float, float, float], ...
    ] = ()  # (t_from, t_to, +/- amplitude %) while running

    def phase_at(self, t: float) -> tuple[str, float, float]:
        """-> (phase, seconds into phase, engine-running seconds so far)."""
        acc = 0.0
        run = 0.0
        for ph, dur in self.phases:
            if t < acc + dur:
                into = t - acc
                return ph, into, run + (into if ph not in ("ign", "off") else 0.0)
            acc += dur
            if ph not in ("ign", "off"):
                run += dur
        return self.phases[-1][0], t - acc, run

    def online(self, t: float) -> bool:
        return self.phase_at(t)[0] != "off"

    def _coolant(self, t: float) -> float:
        ph, into, run = self.phase_at(t)
        warm = self.coolant_start_c + (self.plateau_c - self.coolant_start_c) * (
            1 - math.exp(-run / self.warm_tau_s)
        )
        if ph in ("ign", "off") and run > 0:  # heat soak after a stop, then slow cooling
            warm += 3.0 * math.exp(-into / 120.0) * (1 - math.exp(-into / 20.0)) - into / 600.0
        return warm

    def _system_volts(self, t: float, running: bool, rng: random.Random) -> float:
        if not running:
            ph, into, run = self.phase_at(t)
            v = 12.55 - (0.15 * (1 - math.exp(-into / 300.0)) if run > 0 else 0.0)
        else:
            v = 14.15
            for a, b, volts in self.low_voltage:
                if a <= t < b:
                    v = volts
        for at, drop in self.step_drops:
            if at <= t < at + 2.0:
                v -= drop
        return v + rng.gauss(0, 0.02)

    def adapter_volts(self, t: float) -> float:
        ph = self.phase_at(t)[0]
        rng = random.Random(int(t * 10))
        return round(self._system_volts(t, ph not in ("ign", "off"), rng) - 0.15, 1)

    def signals(self, t: float, rng: random.Random) -> dict[int, float]:
        n = rng.gauss
        ph, into, _ = self.phase_at(t)
        running = ph not in ("ign", "off")
        rpm = speed = load = maf = egr = 0.0
        mapv = 101.0
        if ph in ("idle", "regen"):
            rpm, load, maf, egr, mapv = (
                800 + n(0, 6),
                20 + n(0, 1),
                9.5 + n(0, 0.15),
                38 + n(0, 1),
                101 + n(0, 0.4),
            )
        elif ph == "city":
            cyc = into % 90.0
            speed = max(0.0, 50 * math.sin(math.pi * cyc / 60.0)) if cyc < 60 else 0.0
            rpm = 800 + speed * 32 + n(0, 15)
            load, maf, egr = (
                25 + speed * 0.6 + n(0, 2),
                9.5 + speed * 0.5 + n(0, 0.4),
                max(0.0, 35 - speed * 0.3 + n(0, 1)),
            )
            mapv = 101 + speed * 0.9 + n(0, 1)
        elif ph == "road":
            speed = 110 + n(0, 2)
            rpm, load, maf, egr, mapv = (
                2200 + n(0, 30),
                45 + n(0, 3),
                48 + n(0, 2),
                6 + n(0, 1),
                165 + n(0, 3),
            )
        if ph == "regen":
            rpm += 160
            maf *= 1.45
            egr = max(0.0, 1.0 + n(0, 0.5))
        cool = self._coolant(t)
        volts = self._system_volts(t, running, rng)
        out = {
            0x04: load, 0x05: cool, 0x0B: mapv, 0x0C: max(0.0, rpm), 0x0D: speed, 0x0F: self.ambient_c + 8 + n(0, 0.3),
            0x10: max(0.0, maf), 0x1F: 0.0 if not running else min(65535.0, into), 0x23: 30000 + rpm * 8 if running else 0,
            0x2C: egr, 0x2D: n(0, 1.5) if running else 0.0, 0x33: 101, 0x42: volts, 0x46: self.ambient_c, 0x49: 0,
        }  # fmt: skip
        # applied after the common values so that cycles without them draw exactly the same random sequence
        if running:
            for a, b, value in self.maf_dropouts:
                if a <= t < b:
                    out[0x10] = value  # what the ECU reports; air keeps flowing (MAP / RPM unchanged)
            for a, b, amp in self.egr_err_swings:
                if a <= t < b:
                    out[0x2D] = amp * math.sin(2 * math.pi * (t - a) / 6.0) + n(0, 2.0)
        return out


def _drive(cycle: DriveCycle, **kw) -> dict:
    return dict(signals=cycle.signals, ecu_online=cycle.online, adapter_voltage=cycle.adapter_volts, **kw)


CITY = DriveCycle((("ign", 8), ("idle", 40), ("city", 540), ("idle", 30), ("ign", 30), ("off", 600)))
HIGHWAY = DriveCycle(
    (
        ("ign", 8),
        ("idle", 30),
        ("city", 120),
        ("road", 960),
        ("city", 90),
        ("idle", 20),
        ("ign", 30),
        ("off", 600),
    ),
    coolant_start_c=40.0,
)
WARMUP = DriveCycle((("ign", 8), ("idle", 900)), coolant_start_c=5.0, ambient_c=3.0, warm_tau_s=300.0)
REGEN = DriveCycle((("ign", 5), ("idle", 420), ("regen", 300), ("idle", 300)), coolant_start_c=85.0)
SHUTDOWN_IN_REGEN = DriveCycle(
    (("ign", 5), ("idle", 420), ("regen", 180), ("ign", 25), ("off", 900)), coolant_start_c=85.0
)
VOLTAGE_DROP = DriveCycle(
    (("ign", 8), ("idle", 40), ("city", 400), ("idle", 30), ("ign", 30), ("off", 300)),
    low_voltage=((150.0, 230.0, 11.9),),
    step_drops=((300.0, 1.6),),
)
# Anonymised stand-in for the first real MINI recording (2026-11-18): cold start (~7 degC -> ~73 degC),
# stored generic P115D with MIL on, MAF reported as 0.00 / < 1 g/s for short spells with the engine running,
# EGR-error swings both ways, and ECU communication lost at the end WITHOUT a measured engine stop.
# The times and values are invented; only the observable pattern is reproduced.
AIR_PATH = DriveCycle(
    (("ign", 6), ("idle", 80), ("city", 300), ("idle", 45), ("off", 60)),
    ambient_c=4.0,
    coolant_start_c=7.0,
    warm_tau_s=250.0,
    maf_dropouts=(
        (40.0, 40.6, 0.0),  # cold idle: single transient
        (70.0, 71.6, 0.0),  # cold idle
        (150.0, 150.6, 0.4),  # driving: low, not zero
        (230.0, 232.0, 0.0),  # driving: repeated cluster ...
        (250.0, 251.0, 0.0),
        (265.0, 265.6, 0.6),
        (400.0, 404.0, 0.0),  # warm idle: persistent
    ),
    egr_err_swings=((220.0, 280.0, 32.0), (330.0, 360.0, 28.0)),
)
IGNITION_OFF = DriveCycle((("ign", 5), ("idle", 60), ("ign", 20), ("off", 1200)), coolant_start_c=80.0)
ECU_SILENT = DriveCycle((("ign", 5), ("idle", 60), ("off", 1200)), coolant_start_c=80.0)


MINI = "mini-countryman-r60-diesel"
PORSCHE = "porsche-boxster-986-mk2"

SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("healthy_idle", "Synthetic steady diesel idle, no DTCs", MINI, _diesel()),
        Scenario(
            "rough_idle", "Synthetic idle with intermittent RPM oscillation and coincident MAF/rail changes",
            MINI, _diesel(rough=True), pending=("P0101",),
        ),
        Scenario(
            "egr_mismatch", "Synthetic sustained generic EGR-error PID with air-path DTC",
            MINI, _diesel(egr_error=30.0, maf_base=14.0), stored=("P0401",), mil=True,
            freeze=("P0401", ((0x04, 24), (0x05, 84), (0x0C, 810), (0x0D, 0), (0x10, 14.2))),
        ),
        Scenario(
            "dpf_history", "Synthetic DPF-related generic DTC history; DPF PIDs advertised but raw-only",
            MINI, _diesel(), stored=("P2002",), pending=("P2453",), permanent=("P2002",), mil=True,
            freeze=("P2002", ((0x04, 31), (0x05, 86), (0x0C, 1450), (0x0D, 42), (0x10, 21.5))),
            raw_only_pids=(0x7A, 0x7C),
        ),
        Scenario("wifi_drop", "Synthetic healthy idle; adapter drops TCP every few seconds", MINI, _diesel(),
                 drop_after_s=2.0),
        Scenario(
            "unsupported_pids", "Synthetic ECU with a minimal PID set and missing services", MINI, _minimal,
            mode0a=False, mode02=False, mode06="negative", mode09=(), second_ecu=False,
        ),
        Scenario("slow_adapter", "Synthetic healthy idle with slow adapter responses", MINI, _diesel(),
                 delay=(0.25, 0.6)),
        Scenario("mini_city_drive", "Synthetic short urban drive, engine stop, ignition off", MINI, **_drive(CITY)),
        Scenario("mini_highway_drive", "Synthetic drive with a long highway section", MINI, **_drive(HIGHWAY)),
        Scenario("mini_warmup", "Synthetic cold start and warm-up at idle", MINI, **_drive(WARMUP)),
        Scenario(
            "mini_dpf_regen_possible", "Synthetic warm idle with an indirect regeneration-like pattern; DPF PIDs raw-only",
            MINI, raw_only_pids=(0x7A, 0x7C, 0x78), **_drive(REGEN),
        ),
        Scenario(
            "mini_shutdown_during_regen", "Synthetic regeneration-like pattern interrupted by an engine stop + ignition off",
            MINI, raw_only_pids=(0x7A, 0x7C, 0x78), **_drive(SHUTDOWN_IN_REGEN),
        ),
        Scenario("mini_voltage_drop", "Synthetic drive with low running voltage and a sudden drop", MINI,
                 **_drive(VOLTAGE_DROP)),
        Scenario(
            "mini_air_path_anomalies",
            "Synthetic cold drive: MAF 0 / < 1 g/s spells while running, EGR-error swings, stored P115D, MIL on, "
            "ECU lost at the end", MINI, stored=("P115D",), mil=True, **_drive(AIR_PATH),
        ),
        Scenario("mini_ignition_off", "Synthetic idle, engine stop, then the ECU stops answering", MINI,
                 **_drive(IGNITION_OFF)),
        Scenario("mini_ecu_silent_running", "Synthetic: the ECU stops answering while the engine was running",
                 MINI, **_drive(ECU_SILENT)),
        Scenario(
            "porsche_healthy_idle", "Synthetic petrol flat-six idle over K-line (ISO 9141-2)", PORSCHE,
            _petrol_flat6(), family="kline", protocol_number="3", protocol_name="ISO 9141-2",
            compression_ignition=False, mode0a=False, mode06="no_data", second_ecu=False, delay=(0.08, 0.14),
        ),
        Scenario(
            "porsche_bank_imbalance", "Synthetic petrol flat-six with diverging long-term trims between banks",
            PORSCHE, _petrol_flat6(ltft_b2=14.0), family="kline", protocol_number="3",
            protocol_name="ISO 9141-2", compression_ignition=False, mode0a=False, mode06="no_data",
            second_ecu=False, delay=(0.08, 0.14), pending=("P0174",),
        ),
    ]
}  # fmt: skip


def _bitmap(base: int, supported: set[int]) -> bytes:
    bits = 0
    for i in range(32):
        pid = base + i + 1
        if pid in supported or (i == 31 and any(p > pid for p in supported)):
            bits |= 1 << (31 - i)
    return bits.to_bytes(4, "big")


@dataclass
class ElmSimulator:
    """ELM327 text-protocol state machine in front of a synthetic ECU."""

    scenario: Scenario
    clock: object = field(default_factory=VirtualClock)
    seed: int = 1

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.received: list[str] = []  # every command ever received (tests inspect this)
        self.dtcs_cleared = False  # set if a (forbidden) service 04 ever arrives
        self.t0 = self.clock.monotonic()  # type: ignore[attr-defined]
        self._reset()

    def _reset(self) -> None:
        self.echo, self.linefeed, self.spaces, self.headers = True, False, True, False
        self.searched = False

    # -- public -----------------------------------------------------------------
    def handle(self, command: str) -> tuple[bytes, float]:
        """One command (no terminator) -> (raw response incl. prompt, delay seconds)."""
        cmd = "".join(command.upper().split())
        self.received.append(cmd)
        delay = self.rng.uniform(*self.scenario.delay)
        if cmd.startswith("AT"):
            lines = self._at(cmd)
            delay = 0.3 if cmd == "ATZ" else min(delay, 0.03)
        else:
            lines = self._obd(cmd)
            if lines and lines[0] != "?" and not self.searched:
                self.searched = True
                lines = (["BUS INIT: OK"] if self.scenario.family == "kline" else ["SEARCHING..."]) + lines
                delay += 0.2
        eol = "\r\n" if self.linefeed else "\r"
        out = (command + eol if self.echo and cmd != "ATZ" else "") + eol.join(lines) + eol + eol + ">"
        if cmd == "ATZ":
            out = eol + eol + "ELM327 v2.3" + eol + eol + ">"
        return out.encode("latin-1"), delay

    # -- AT -------------------------------------------------------------------
    def _at(self, cmd: str) -> list[str]:
        sc = self.scenario
        if cmd == "ATZ":
            self._reset()
            return ["ELM327 v2.3"]
        if cmd == "ATI":
            return ["ELM327 v2.3"]
        if cmd == "AT@1":
            return ["SYNTHETIC ELM327 SIMULATOR"]
        if cmd in ("ATE0", "ATE1"):
            self.echo = cmd.endswith("1")
        elif cmd in ("ATL0", "ATL1"):
            self.linefeed = cmd.endswith("1")
        elif cmd in ("ATS0", "ATS1"):
            self.spaces = cmd.endswith("1")
        elif cmd in ("ATH0", "ATH1"):
            self.headers = cmd.endswith("1")
        elif cmd in ("ATSP0", "ATAT1"):
            pass
        elif cmd == "ATDP":
            return ["AUTO, " + sc.protocol_name]
        elif cmd == "ATDPN":
            return ["A" + sc.protocol_number]
        elif cmd == "ATRV":
            if sc.adapter_voltage is not None:
                return [f"{sc.adapter_voltage(self._t()):.1f}V"]
            return ["14.1V"]
        else:
            return ["?"]
        return ["OK"]

    # -- OBD ------------------------------------------------------------------
    def _t(self) -> float:
        return self.clock.monotonic() - self.t0  # type: ignore[attr-defined]

    def _values(self) -> dict[int, float]:
        return self.scenario.signals(self._t(), self.rng)

    def _engine_pids(self) -> set[int]:
        return set(self.scenario.signals(0.0, random.Random(0))) | {0x01} | set(self.scenario.raw_only_pids)

    def _obd(self, cmd: str) -> list[str]:
        sc = self.scenario
        try:
            body = bytes.fromhex(cmd)
        except ValueError:
            return ["?"]
        if not body:
            return ["?"]
        mode, args = body[0], body[1:]
        if sc.ecu_online is not None and not sc.ecu_online(self._t()):
            return ["NO DATA"]  # ignition off / ECU asleep: nothing on the bus answers
        eng = "7E8" if sc.family == "can" else "10"
        msgs: list[tuple[str, bytes]] = []
        can = sc.family == "can"

        if mode == 0x01 and len(args) == 1:
            pid = args[0]
            pids = self._engine_pids()
            if pid in BITMAP_PIDS:
                if pid == 0 or any(p > pid for p in pids):
                    msgs.append((eng, bytes([0x41, pid]) + _bitmap(pid, pids)))
                if pid == 0 and sc.second_ecu and can:
                    msgs.append(("7E9", bytes([0x41, 0x00]) + _bitmap(0, {0x01, 0x0D})))
            elif pid == 0x01:
                n = len(sc.stored)
                b = 0x0F if sc.compression_ignition else 0x07
                msgs.append((eng, bytes([0x41, 0x01, (0x80 if sc.mil else 0) | n, b, 0, 0])))
                if sc.second_ecu and can:
                    msgs.append(("7E9", bytes([0x41, 0x01, 0, 0, 0, 0])))
            elif pid in sc.raw_only_pids:
                msgs.append((eng, bytes([0x41, pid, 0x01, 0x02, 0x03, 0x04, 0x05])))
            elif pid in pids:
                msgs.append((eng, bytes([0x41, pid]) + encode_pid(pid, self._values()[pid])))
                if pid == 0x0D and sc.second_ecu and can:
                    msgs.append(("7E9", bytes([0x41, 0x0D, 0])))
        elif mode == 0x02 and len(args) == 2 and sc.mode02:
            pid = args[0]
            fz = dict(sc.freeze[1]) if sc.freeze else {}
            if pid == 0x00:
                msgs.append((eng, bytes([0x42, 0, 0]) + _bitmap(0, set(fz) | {0x02})))
            elif pid == 0x02:
                code = encode_dtc(sc.freeze[0]) if sc.freeze else b"\x00\x00"
                msgs.append((eng, bytes([0x42, 2, 0]) + code))
            elif pid in fz:
                msgs.append((eng, bytes([0x42, pid, 0]) + encode_pid(pid, fz[pid])))
        elif mode in (0x03, 0x07, 0x0A) and not args:
            codes = {0x03: sc.stored, 0x07: sc.pending, 0x0A: sc.permanent}[mode]
            if mode != 0x0A or (sc.mode0a and can):
                raw = b"".join(encode_dtc(c) for c in codes)
                if can:
                    msgs.append((eng, bytes([mode + 0x40, len(codes)]) + raw))
                    if sc.second_ecu and mode == 0x03:
                        msgs.append(("7E9", bytes([0x43, 0])))
                else:  # K-line: 3 DTCs per message, zero padded, no count byte
                    raw = raw or b"\x00" * 6
                    for i in range(0, len(raw), 6):
                        msgs.append((eng, bytes([mode + 0x40]) + raw[i : i + 6].ljust(6, b"\x00")))
        elif mode == 0x06 and len(args) == 1:
            if sc.mode06 == "negative":
                msgs.append((eng, bytes([0x7F, 0x06, 0x11])))
            elif sc.mode06 == "ok" and args[0] == 0x00:
                msgs.append((eng, bytes([0x46, 0x00]) + _bitmap(0, {0x01})))
            elif sc.mode06 == "ok" and args[0] == 0x01:
                msgs.append((eng, bytes.fromhex("460181" + "0A0BB80000FFFF")))
        elif mode == 0x09 and len(args) == 1:
            it = args[0]
            if it == 0x00 and sc.mode09:
                msgs.append((eng, bytes([0x49, 0x00]) + _bitmap(0, set(sc.mode09))))
            elif it in sc.mode09 and it in (0x02, 0x04):
                text = (SYNTHETIC_VIN if it == 0x02 else SYNTHETIC_CAL).encode()
                if can:
                    msgs.append((eng, bytes([0x49, it, 0x01]) + text))
                else:
                    padded = (b"\x00" * 3 + text) if it == 0x02 else text
                    for n, i in enumerate(range(0, len(padded), 4), start=1):
                        msgs.append((eng, bytes([0x49, it, n]) + padded[i : i + 4]))
        elif mode == 0x04:
            self.dtcs_cleared = True  # a real ECU would have erased fault memory here
            msgs.append((eng, b"\x44"))
        elif can:
            msgs.append((eng, bytes([0x7F, mode, 0x11])))  # service not supported

        if not msgs:
            return ["NO DATA"]
        return self._format(msgs)

    # -- output formatting ------------------------------------------------------
    def _hex(self, data: bytes) -> str:
        return (" " if self.spaces else "").join(f"{b:02X}" for b in data)

    def _format(self, msgs: list[tuple[str, bytes]]) -> list[str]:
        sep = " " if self.spaces else ""
        lines: list[str] = []
        for ecu, payload in msgs:
            if self.scenario.family == "kline":
                if self.headers:
                    frame = bytes([0x48, 0x6B, int(ecu, 16)]) + payload
                    frame += bytes([sum(frame) & 0xFF])
                    lines.append(self._hex(frame))
                else:
                    lines.append(self._hex(payload))
            elif not self.headers:
                if len(payload) <= 7:
                    lines.append(self._hex(payload))
                else:
                    lines.append(f"{len(payload):03X}")
                    for n, i in enumerate(range(0, len(payload), 7)):
                        lines.append(f"{n:X}: " + self._hex(payload[i : i + 7]))
            elif len(payload) <= 7:
                lines.append(ecu + sep + self._hex((bytes([len(payload)]) + payload).ljust(8, b"\x00")))
            else:
                lines.append(
                    ecu
                    + sep
                    + self._hex(bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6])
                )
                rest, seq = payload[6:], 1
                while rest:
                    lines.append(ecu + sep + self._hex((bytes([0x20 | seq]) + rest[:7]).ljust(8, b"\x00")))
                    rest, seq = rest[7:], (seq + 1) & 0x0F
        return lines


class SimulatorTransport(Transport):
    """In-process transport on a virtual clock (fast, deterministic; used for fixtures/tests)."""

    kind = "simulator-inprocess"

    def __init__(self, scenario: str | Scenario = "healthy_idle", clock=None, seed: int = 1, **kw) -> None:
        super().__init__(clock=clock or VirtualClock(), **kw)
        sc = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
        self.sim = ElmSimulator(sc, clock=self.clock, seed=seed)

    def connect(self) -> None:
        self.state = "connected"
        self.generation += 1
        self._opened = self.clock.monotonic()

    def close(self) -> None:
        self.state = "disconnected"

    def _ensure_connected(self) -> None:
        if self.state != "connected":
            self.connect()

    def _resync(self) -> None:
        self.state = "disconnected"  # next _ensure_connected() is a fresh "connection"

    def _roundtrip(self, wire: bytes, timeout_s: float) -> tuple[bytes, str]:
        raw, delay = self.sim.handle(wire.decode("ascii").rstrip("\r"))
        drop = self.sim.scenario.drop_after_s
        if drop is not None and self.clock.monotonic() - self._opened > drop:
            self.clock.sleep(delay)
            self.state = "disconnected"  # emulated Wi-Fi drop: half a response, then gone
            return raw[: max(1, len(raw) // 2)].replace(b">", b""), "closed"
        if delay > timeout_s:
            self.clock.sleep(timeout_s)
            return b"", "timeout"
        self.clock.sleep(delay)
        return raw, "ok"


class FakeElmServer:
    """Threaded TCP server speaking the ELM text protocol on 127.0.0.1."""

    def __init__(
        self,
        scenario: str | Scenario = "healthy_idle",
        host: str = "127.0.0.1",
        port: int = 0,
        seed: int = 1,
        time_scale: float = 1.0,
        script: Callable[[str, int, bytes], list[tuple[float, bytes | None]] | None] | None = None,
    ) -> None:
        """``script(command, n, normal_response)`` may return ``[(delay_s, chunk), ...]`` to send
        instead of the normal response (tests use it to inject late, partial or trailing bytes).
        A ``None`` chunk closes the connection at that point (emulated drop/reconnect).
        ``n`` counts commands across all connections, starting at 0."""
        sc = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
        self.scenario = sc
        self.time_scale = time_scale
        from app.adapters.base import SystemClock

        self.sim = ElmSimulator(sc, clock=SystemClock(), seed=seed)
        self.connections = 0
        self.script = script
        self.log: list[tuple[int, str]] = []  # (connection number, command) in arrival order
        self._log_lock = threading.Lock()
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                with outer._log_lock:
                    outer.connections += 1
                    conn = outer.connections
                sock: socket.socket = self.request
                sock.settimeout(0.2)
                opened = time.monotonic()
                buf = b""
                while not outer._stop.is_set():
                    try:
                        chunk = sock.recv(1024)
                    except TimeoutError:
                        continue
                    except OSError:
                        return
                    if not chunk:
                        return
                    buf += chunk
                    while b"\r" in buf:
                        line, buf = buf.split(b"\r", 1)
                        text = line.decode("latin-1").strip()
                        with outer._log_lock:
                            n = len(outer.log)
                            outer.log.append((conn, text))
                        raw, delay = outer.sim.handle(text)
                        scripted = outer.script(text, n, raw) if outer.script else None
                        if scripted is not None:
                            try:
                                for pause, part in scripted:
                                    time.sleep(pause)
                                    if part is None:
                                        sock.close()
                                        return
                                    sock.sendall(part)
                            except OSError:
                                return
                            continue
                        time.sleep(delay * outer.time_scale)
                        drop = sc.drop_after_s is not None and time.monotonic() - opened > sc.drop_after_s
                        try:
                            if drop:  # emulate a Wi-Fi drop: half a response, then gone
                                sock.sendall(raw[: max(1, len(raw) // 2)].replace(b">", b""))
                                sock.close()
                                return
                            sock.sendall(raw)
                        except OSError:
                            return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._stop = threading.Event()
        self._server = Server((host, port), Handler)
        self.host, self.port = self._server.server_address[:2]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def start(self) -> FakeElmServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeElmServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
