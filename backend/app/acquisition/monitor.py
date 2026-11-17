"""Continuous monitoring recorder: priority tiers, deadline scheduling, adaptive intervals.

Built for long, unattended recordings (``mini_extended_monitoring``). It uses the
same client and transport as the classic ``Recorder`` - so every request still
goes out one at a time, through the protocol allowlist, the client lock and the
transport resynchronisation rules - but schedules signals by priority tier
instead of a fixed round-robin:

* every signal has a target interval (fast / medium / slow tier);
* the most overdue signal is read next (earliest deadline first, faster tier wins ties);
* every adaptation window the recorder measures what the adapter really
  delivers (requests per second) and stretches the intervals - slow/medium tiers
  first, the fast tier only if that is not enough - so the schedule never asks
  for more than the link can do. Base and effective intervals are both reported.

It also follows the state of the link and of the ECU, and writes what it sees as
timeline events (origin ``observed``):

* ``adapter_connection_lost`` / ``adapter_connection_restored`` (with the gap);
* ``ecu_communication_lost`` / ``ecu_communication_restored``: the ECU stopped /
  started answering while the adapter link was fine. Loss of ECU communication is
  NOT treated as proof that the ignition was switched off. While the ECU is
  unresponsive no vehicle request is sent (sleeping modules are not polled or
  woken); only the adapter's own supply voltage (ATRV, adapter-local) is read,
  and only on the existing, already-initialised connection. Vehicle polling
  resumes when the user asks for it (or, opt-in, with a sparse backed-off probe);
* ``engine_started`` / ``engine_stopped`` from measured RPM transitions, with a
  snapshot of the last valid RPM / coolant / ECU voltage / adapter voltage;
* ``mil_on`` / ``mil_off`` from PID 01 transitions.

A PID that the ECU advertises but keeps answering with NO DATA / a negative
response is suspended with a growing back-off instead of being polled forever.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.adapters.base import TransportClosedError, TransportError, VirtualClock
from app.models.domain import Annotation, Reading
from app.obd.client import ObdClient, ObdError
from app.parameters.standard_pids import ADAPTER_VOLTAGE_PID, PID_MONITOR_STATUS, pid_name
from app.vehicles.profiles import SamplingTiers

GOOD = ("ok", "raw_only")
FAILED_VEHICLE = ("no_data", "timeout", "negative", "error")
TIERS = ("fast", "medium", "slow")
RPM, COOLANT, ECU_VOLTAGE = 0x0C, 0x05, 0x42

# Tool parameters (not vehicle limits). Documented in docs/ARCHITECTURE.md "Monitoring recorder".
ENGINE_RUNNING_RPM = 300.0  # at or above: engine running (same screen as the idle rule)
ENGINE_STOPPED_RPM = 100.0  # below, after running: engine stopped
ECU_LOST_MIN_FAILS = 6  # consecutive failed vehicle requests ...
ECU_LOST_MIN_S = 5.0  # ... spanning at least this long since the last good answer
PID_SUSPEND_AFTER = 5  # consecutive NO DATA / negative answers from one advertised PID
PID_SUSPEND_S = (60.0, 600.0)  # initial and maximum back-off for such a PID
ADAPT_EVERY_S = 15.0  # adaptation window
LOAD_BUDGET = 0.8  # use at most this share of the measured adapter capacity
MAX_LOW_TIER_STRETCH = 4.0  # medium/slow are stretched up to this factor before the fast tier is touched
BACKOFF_S = (2.0, 60.0)  # adapter unreachable: retry back-off (initial, max)
MAX_ADJUSTMENTS_KEPT = 200


@dataclass
class _Item:
    key: str
    tier: str
    pid: int | None  # None: adapter voltage (ATRV)
    base_s: float
    next_due: float = 0.0
    fails: int = 0  # consecutive NO DATA / negative (per PID)
    suspend_s: float = 0.0
    suspended_until: float | None = None
    attempts: int = 0
    ok: int = 0

    @property
    def name(self) -> str:
        return "Adapter supply voltage (ATRV)" if self.pid is None else pid_name(self.pid)


@dataclass
class _Last:
    value: float
    t_wall: float
    t_mono: float


@dataclass
class MonitorStats:
    state_time_s: dict[str, float] = field(default_factory=lambda: {s: 0.0 for s in STATES})
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    suspended: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: int = 0
    vehicle_ok_after_engine_stop: int = 0
    vehicle_ok_after_engine_stop_span_s: float = 0.0
    adapter_voltage_while_ecu_lost: int = 0
    adapter_voltage_attempts_while_ecu_lost: int = 0
    ecu_probes: int = 0
    engine_stops_observed: int = 0
    ecu_losses: int = 0


STATES = ("running", "ecu_unresponsive", "adapter_unreachable")


class MonitoringRecorder:
    def __init__(
        self,
        client: ObdClient,
        plan: dict[str, list[int]],
        tiers: SamplingTiers,
        adapter_voltage: bool,
        duration_s: float | None,
        on_readings: Callable[[list[Reading]], None],
        on_event: Callable[[Annotation], None],
        stop: threading.Event | None = None,
        resume: threading.Event | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
        resume_probe_s: float | None = None,
        primary_ecu: str | None = None,
    ) -> None:
        self.client = client
        self.clock = client.transport.clock
        self.on_readings, self.on_event, self.on_state = on_readings, on_event, on_state
        self.stop = stop or threading.Event()
        self.resume = resume or threading.Event()
        self.duration_s = duration_s
        self.resume_probe_s = resume_probe_s
        self.primary_ecu = primary_ecu
        self.tiers = tiers
        base = tiers.as_dict()
        self.items: list[_Item] = []
        for tier in TIERS:
            for pid in plan.get(tier, []):
                self.items.append(_Item(f"01:{pid:02X}", tier, pid, base[tier]))
        if adapter_voltage:
            self.items.append(_Item("adapter:ATRV", "slow", None, base["slow"]))
        if not any(i.pid is not None for i in self.items):
            raise ValueError("no supported signals selected for recording")
        self.scale = {"fast": 1.0, "low": 1.0}
        self.state = "running"
        self.stats = MonitorStats()
        self.engine: str = "unknown"  # unknown | running | stopped
        self._published_engine = self.engine
        self.mil: bool | None = None
        self.mil_at_start: bool | None = None
        self.last: dict[str, _Last] = {}
        self._fail_run = 0
        self._last_vehicle_ok: float | None = None
        self._adapter_lost_at: tuple[float, float] | None = None  # (t_wall, t_mono)
        self._ecu_lost_at: tuple[float, float] | None = None
        self._engine_stop_at: float | None = None
        self._window_start = self.clock.monotonic()
        self._busy = 0.0
        self._count = 0
        self._capacity: float | None = None
        self._state_since = self.clock.monotonic()
        self._backoff = BACKOFF_S[0]
        self._probe_wait = resume_probe_s
        self._probe_at: float | None = None
        self.started_mono = self.clock.monotonic()

    # -- helpers ------------------------------------------------------------------
    def interval(self, item: _Item) -> float:
        return item.base_s * (self.scale["fast"] if item.tier == "fast" else self.scale["low"])

    def effective_intervals(self) -> dict[str, float]:
        b = self.tiers.as_dict()
        return {
            "fast": round(b["fast"] * self.scale["fast"], 3),
            "medium": round(b["medium"] * self.scale["low"], 3),
            "slow": round(b["slow"] * self.scale["low"], 3),
        }

    def _live(self) -> bool:
        if self.stop.is_set():
            return False
        if self.duration_s is None:
            return True
        return self.clock.monotonic() - self.started_mono < self.duration_s

    def _idle(self, seconds: float) -> None:
        """Wait without blocking a stop request (virtual clocks just advance)."""
        end = self.clock.monotonic() + max(0.0, seconds)
        if self.duration_s is not None:
            end = min(end, self.started_mono + self.duration_s)
        while not self.stop.is_set() and not self.resume.is_set():
            left = end - self.clock.monotonic()
            if left <= 0:
                return
            step = min(left, 0.25)
            if isinstance(self.clock, VirtualClock):
                self.clock.sleep(step)
            else:
                self.stop.wait(step)

    def _set_state(self, state: str) -> None:
        now = self.clock.monotonic()
        self.stats.state_time_s[self.state] += now - self._state_since
        self._state_since = now
        self.state = state
        self._publish()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "engine": self.engine,
            "mil_on": self.mil,
            "base_intervals": self.tiers.as_dict(),
            "effective_intervals": self.effective_intervals(),
            "capacity_rps": round(self._capacity, 2) if self._capacity else None,
            "suspended": sorted(self.stats.suspended),
            "items": {
                i.key: {"tier": i.tier, "pid": i.pid, "interval_s": round(self.interval(i), 3)}
                for i in self.items
            },
            "resume_probe_s": self.resume_probe_s,
        }

    def _publish(self) -> None:
        if self.on_state:
            self.on_state(self.snapshot())

    def _event(
        self,
        event_type: str,
        note: str,
        evidence: dict[str, Any] | None = None,
        confidence: str = "measured",
        at: tuple[float, float] | None = None,
    ) -> None:
        t_wall, t_mono = at or (self.clock.wall(), self.clock.monotonic())
        self.on_event(
            Annotation(
                t_wall=t_wall,
                t_mono=t_mono,
                label=event_type,
                note=note,
                event_type=event_type,
                origin="observed",
                confidence=confidence,
                evidence=evidence or {},
            )
        )

    def _last_dict(self, key: str) -> dict[str, Any] | None:
        last = self.last.get(key)
        if last is None:
            return None
        return {
            "value": last.value,
            "t_wall": last.t_wall,
            "age_s": round(self.clock.monotonic() - last.t_mono, 2),
        }

    # -- reads ----------------------------------------------------------------------
    def _read_pid(self, pid: int) -> list[Reading]:
        clock = self.clock
        fail = dict(pid=pid, name=pid_name(pid), t_wall=clock.wall(), t_mono=clock.monotonic())
        try:
            return self.client.read_pid(pid)
        except TransportClosedError as exc:
            ex = exc.exchange
            if ex:
                fail.update(t_wall=ex.t_wall, t_mono=ex.t_mono, latency_ms=ex.latency_ms)
            return [Reading(**fail, quality="disconnected", detail=str(exc))]
        except TransportError:
            raise  # reconnect attempts exhausted: handled by the caller (back-off)
        except ObdError as exc:
            return [Reading(**fail, quality="error", detail=str(exc))]

    def _read_adapter(self, allow_reinit: bool) -> Reading | None:
        clock = self.clock
        try:
            return self.client.read_adapter_voltage(allow_reinit=allow_reinit)
        except TransportClosedError as exc:
            ex = exc.exchange
            return Reading(
                mode=0,
                pid=ADAPTER_VOLTAGE_PID,
                name="Adapter supply voltage (ATRV, measured by the adapter)",
                unit="V",
                t_wall=ex.t_wall if ex else clock.wall(),
                t_mono=ex.t_mono if ex else clock.monotonic(),
                quality="disconnected",
                detail=str(exc),
            )
        except ObdError:
            return None

    # -- bookkeeping per reading ----------------------------------------------------------
    def _primary(self, r: Reading) -> bool:
        return r.ecu is None or self.primary_ecu is None or r.ecu == self.primary_ecu

    def _absorb(self, item: _Item, rs: list[Reading], t0: float) -> None:
        now = self.clock.monotonic()
        self._busy += now - t0
        self._count += 1
        self.stats.requests += 1
        item.attempts += 1
        self.on_readings(rs)
        if item.pid is None:
            self._absorb_adapter(rs[0])
            return
        good = [r for r in rs if r.quality in GOOD]
        if good:
            item.ok += 1
            item.fails = 0
            if item.key in self.stats.suspended:
                self.stats.suspended.pop(item.key)
                item.suspended_until, item.suspend_s = None, 0.0
            self._fail_run = 0
            self._last_vehicle_ok = now
            self._link_ok(good[0])
            if self.engine == "stopped":
                self.stats.vehicle_ok_after_engine_stop += 1
                if self._engine_stop_at is not None:
                    self.stats.vehicle_ok_after_engine_stop_span_s = round(now - self._engine_stop_at, 2)
            for r in good:
                if self._primary(r) and r.value is not None:
                    self.last[f"{r.pid:02X}"] = _Last(r.value, r.t_wall, r.t_mono)
            prim = next((r for r in good if self._primary(r)), None)
            if prim is not None and prim.pid == RPM and prim.value is not None:
                self._engine_transition(prim)
            if prim is not None and prim.pid == PID_MONITOR_STATUS and prim.raw_hex:
                self._mil_transition(prim)
            return
        r = rs[0]
        if r.quality == "disconnected":
            self._link_lost(r)
            return
        if r.quality in ("no_data", "negative") and self._fail_run == 0:
            # only failures while the ECU answers other requests count: a general outage
            # (ignition off, bus asleep) is handled as ECU loss, not as N unsupported PIDs
            item.fails += 1
            if item.fails >= PID_SUSPEND_AFTER:
                item.suspend_s = min(PID_SUSPEND_S[1], (item.suspend_s * 2) or PID_SUSPEND_S[0])
                item.suspended_until = now + item.suspend_s
                self.stats.suspended[item.key] = {
                    "pid": item.pid,
                    "name": item.name,
                    "since_t_wall": r.t_wall,
                    "last_quality": r.quality,
                    "retry_after_s": item.suspend_s,
                }
                self._publish()
        self._fail_run += 1
        since = now - (self._last_vehicle_ok if self._last_vehicle_ok is not None else self.started_mono)
        if self._fail_run >= ECU_LOST_MIN_FAILS and since >= ECU_LOST_MIN_S:
            self._enter_ecu_lost(r, since)

    def _absorb_adapter(self, r: Reading) -> None:
        if r.quality == "ok" and r.value is not None:
            self.last["ATRV"] = _Last(r.value, r.t_wall, r.t_mono)
            self._link_ok(r)
            if self.state == "ecu_unresponsive":
                self.stats.adapter_voltage_while_ecu_lost += 1
        elif r.quality == "disconnected":
            self._link_lost(r)

    def _link_ok(self, r: Reading) -> None:
        if self._adapter_lost_at is not None:
            lost_wall, lost_mono = self._adapter_lost_at
            self._adapter_lost_at = None
            self._event(
                "adapter_connection_restored",
                f"adapter link working again after {r.t_mono - lost_mono:.1f} s; no values exist for that gap",
                {"gap_s": round(r.t_mono - lost_mono, 2), "lost_at_wall": lost_wall},
                at=(r.t_wall, r.t_mono),
            )

    def _link_lost(self, r: Reading) -> None:
        if self._adapter_lost_at is None:
            self._adapter_lost_at = (r.t_wall, r.t_mono)
            self._event(
                "adapter_connection_lost",
                "adapter connection dropped (Wi-Fi/TCP); the recorder reconnects and re-initialises the adapter",
                {"detail": r.detail, "while": self.state},
                at=(r.t_wall, r.t_mono),
            )

    def _engine_transition(self, r: Reading) -> None:
        rpm = float(r.value or 0.0)
        if rpm >= ENGINE_RUNNING_RPM:
            if self.engine == "stopped":
                self._event(
                    "engine_started",
                    "engine speed rose above the running threshold (measured RPM)",
                    {"rpm": rpm, "threshold_rpm": ENGINE_RUNNING_RPM},
                    at=(r.t_wall, r.t_mono),
                )
                self._engine_stop_at = None
            self.engine = "running"
            self.last["rpm_running"] = _Last(rpm, r.t_wall, r.t_mono)
        elif rpm < ENGINE_STOPPED_RPM:
            if self.engine == "running":
                self.stats.engine_stops_observed += 1
                self._engine_stop_at = r.t_mono
                self._event(
                    "engine_stopped",
                    "engine speed fell to ~0 while the ECU kept answering (measured RPM). The session stays open: "
                    "mark fan_running / fan_stopped and ignition_off when they happen.",
                    {
                        "rpm_now": rpm,
                        "last_running_rpm": self._last_dict("rpm_running"),
                        "coolant_c": self._last_dict(f"{COOLANT:02X}"),
                        "ecu_voltage_v": self._last_dict(f"{ECU_VOLTAGE:02X}"),
                        "adapter_voltage_v": self._last_dict("ATRV"),
                    },
                    at=(r.t_wall, r.t_mono),
                )
            self.engine = "stopped"
        if self.engine != self._published_engine:
            self._published_engine = self.engine
            self._publish()

    def _mil_transition(self, r: Reading) -> None:
        mil = bool(int(r.raw_hex[0:2], 16) & 0x80)
        if self.mil is None:
            self.mil_at_start = mil
        elif mil != self.mil:
            self._event(
                "mil_on" if mil else "mil_off",
                "MIL state changed in PID 01 (monitor status)",
                {"raw_hex": r.raw_hex},
                at=(r.t_wall, r.t_mono),
            )
        self.mil = mil

    def _enter_ecu_lost(self, r: Reading, since: float) -> None:
        self.stats.ecu_losses += 1
        self._ecu_lost_at = (r.t_wall, r.t_mono)
        engine_note = (
            " The engine was last seen RUNNING; an engine stop was not observed."
            if self.engine == "running"
            else ""
        )
        self._event(
            "ecu_communication_lost",
            "The ECU stopped answering while the adapter link was up. This is NOT proof that the ignition "
            "was switched off. Vehicle polling is paused (sleeping modules are not polled or woken); only the "
            "adapter supply voltage is read." + engine_note,
            {
                "consecutive_failures": self._fail_run,
                "seconds_since_last_answer": round(since, 2),
                "last_failure": r.quality,
                "engine_last_seen": self.engine,
                "last_rpm": self._last_dict(f"{RPM:02X}"),
                "coolant_c": self._last_dict(f"{COOLANT:02X}"),
                "ecu_voltage_v": self._last_dict(f"{ECU_VOLTAGE:02X}"),
                "adapter_voltage_v": self._last_dict("ATRV"),
            },
            at=(r.t_wall, r.t_mono),
        )
        self._fail_run = 0
        self._probe_wait = self.resume_probe_s
        self._probe_at = (self.clock.monotonic() + self._probe_wait) if self._probe_wait else None
        self._set_state("ecu_unresponsive")

    # -- scheduling ------------------------------------------------------------------------
    def _adapt(self) -> None:
        now = self.clock.monotonic()
        if now - self._window_start < ADAPT_EVERY_S or self._count < 5 or self._busy <= 0:
            return
        capacity = self._count / self._busy  # requests per second the link actually delivered
        self._capacity = capacity
        self._window_start, self._busy, self._count = now, 0.0, 0
        active = [i for i in self.items if i.suspended_until is None]
        d_fast = sum(1 / i.base_s for i in active if i.tier == "fast")
        d_low = sum(1 / i.base_s for i in active if i.tier != "fast")
        budget = LOAD_BUDGET * capacity
        if d_fast + d_low <= budget:
            fast, low = 1.0, 1.0
        elif d_fast + d_low / MAX_LOW_TIER_STRETCH <= budget:
            fast, low = 1.0, d_low / (budget - d_fast)
        else:
            s = (d_fast + d_low / MAX_LOW_TIER_STRETCH) / budget
            fast, low = s, MAX_LOW_TIER_STRETCH * s
        old = dict(self.scale)
        self.scale = {"fast": round(fast, 3), "low": round(low, 3)}
        changed = any(abs(self.scale[k] - old[k]) > 0.1 * old[k] for k in old)
        if changed:
            self.stats.adjustments.append(
                {
                    "t_wall": self.clock.wall(),
                    "t_rel_s": round(now - self.started_mono, 2),
                    "capacity_rps": round(capacity, 2),
                    "demand_rps_at_base": round(d_fast + d_low, 2),
                    "scale": dict(self.scale),
                    "effective_intervals": self.effective_intervals(),
                }
            )
            del self.stats.adjustments[:-MAX_ADJUSTMENTS_KEPT]
        self._publish()

    def _due(self) -> _Item | None:
        now = self.clock.monotonic()
        cands = [i for i in self.items if i.suspended_until is None or i.suspended_until <= now]
        if not cands:
            return None
        return min(cands, key=lambda i: (i.next_due, TIERS.index(i.tier)))

    def _step_running(self) -> None:
        self.resume.clear()  # "resume ECU polling" means nothing while polling runs
        item = self._due()
        now = self.clock.monotonic()
        if item is None:
            self._idle(1.0)
            return
        if item.next_due > now:
            self._idle(item.next_due - now)
            return
        item.next_due = now + self.interval(item)
        if item.suspended_until is not None:
            item.suspended_until = None  # one retry; re-suspended (longer) if it still fails
        t0 = self.clock.monotonic()
        try:
            if item.pid is None:
                r = self._read_adapter(allow_reinit=True)
                if r is not None:
                    self._absorb(item, [r], t0)
                return
            self._absorb(item, self._read_pid(item.pid), t0)
        except TransportError as exc:  # reconnects exhausted
            self._link_lost(
                Reading(
                    pid=item.pid or 0,
                    name=item.name,
                    quality="disconnected",
                    detail=str(exc),
                    t_wall=self.clock.wall(),
                    t_mono=self.clock.monotonic(),
                )
            )
            self._backoff = BACKOFF_S[0]
            self._set_state("adapter_unreachable")
        self._adapt()

    def _step_unreachable(self) -> None:
        self._idle(self._backoff)
        if not self._live():
            return
        self._backoff = min(BACKOFF_S[1], self._backoff * 2)
        first = next(i for i in self.items if i.pid is not None)
        t0 = self.clock.monotonic()
        try:
            rs = self._read_pid(first.pid)  # type: ignore[arg-type]
        except TransportError:
            return
        self._set_state("running")
        for i in self.items:
            i.next_due = self.clock.monotonic()
        self._absorb(first, rs, t0)

    def _step_ecu_lost(self) -> None:
        now = self.clock.monotonic()
        volt = next((i for i in self.items if i.pid is None), None)
        if self.resume.is_set():
            self.resume.clear()
            self._probe_ecu(manual=True)
            return
        if self._probe_at is not None and now >= self._probe_at:
            self._probe_ecu(manual=False)
            return
        if volt is not None and now >= volt.next_due:
            volt.next_due = now + volt.base_s  # base slow interval: nothing else competes for the link
            self.stats.adapter_voltage_attempts_while_ecu_lost += 1
            t0 = self.clock.monotonic()
            r = self._read_adapter(allow_reinit=False)  # never reconnect/re-init (that sends 0100)
            if r is not None:
                self._absorb(volt, [r], t0)
            return
        waits = [x for x in (volt.next_due if volt else None, self._probe_at) if x is not None]
        self._idle(min(waits) - now if waits else 1.0)

    def _probe_ecu(self, manual: bool) -> None:
        """One vehicle request to see whether the ECU answers again (user-requested or opt-in)."""
        self.stats.ecu_probes += 1
        first = next(i for i in self.items if i.pid is not None)
        t0 = self.clock.monotonic()
        try:
            rs = self._read_pid(first.pid)  # type: ignore[arg-type]
        except TransportError:
            rs = []
        if rs:
            self._busy += self.clock.monotonic() - t0
            self.on_readings(rs)
        good = [r for r in rs if r.quality in GOOD]
        if good:
            lost_wall, lost_mono = self._ecu_lost_at or (good[0].t_wall, good[0].t_mono)
            self._ecu_lost_at = None
            self._event(
                "ecu_communication_restored",
                f"the ECU answered again ({'probe requested by the user' if manual else 'opt-in periodic probe'}); "
                f"no vehicle values exist for the {good[0].t_mono - lost_mono:.0f} s before",
                {
                    "gap_s": round(good[0].t_mono - lost_mono, 2),
                    "lost_at_wall": lost_wall,
                    "manual_probe": manual,
                },
                at=(good[0].t_wall, good[0].t_mono),
            )
            self._last_vehicle_ok = self.clock.monotonic()
            self._link_ok(good[0])
            for i in self.items:
                i.next_due = self.clock.monotonic()
                i.fails, i.suspended_until, i.suspend_s = 0, None, 0.0
            self.stats.suspended.clear()
            self._set_state("running")
            self._after_restore(rs)
            return
        if not manual and self._probe_wait:
            self._probe_wait = min(PID_SUSPEND_S[1], self._probe_wait * 2)
            self._probe_at = self.clock.monotonic() + self._probe_wait

    def _after_restore(self, rs: list[Reading]) -> None:
        good = [r for r in rs if r.quality in GOOD and self._primary(r)]
        for r in good:
            if r.value is not None:
                self.last[f"{r.pid:02X}"] = _Last(r.value, r.t_wall, r.t_mono)
            if r.pid == RPM and r.value is not None:
                self._engine_transition(r)

    def run(self) -> None:
        self._publish()
        while self._live():
            if self.state == "running":
                self._step_running()
            elif self.state == "ecu_unresponsive":
                self._step_ecu_lost()
            else:
                self._step_unreachable()
        self.stats.state_time_s[self.state] += self.clock.monotonic() - self._state_since
        self._state_since = self.clock.monotonic()

    def summary(self) -> dict[str, Any]:
        """Written to session meta["monitoring"]: what the recorder did and what it could observe."""
        st = self.stats
        return {
            "final_state": self.state,
            "engine_last_seen": self.engine,
            "mil_at_start": self.mil_at_start,
            "mil_last": self.mil,
            "base_intervals": self.tiers.as_dict(),
            "final_effective_intervals": self.effective_intervals(),
            "final_scale": dict(self.scale),
            "adjustments": list(st.adjustments),
            "suspended_at_end": dict(st.suspended),
            "time_in_state_s": {k: round(v, 2) for k, v in st.state_time_s.items()},
            "requests": st.requests,
            "items": [
                {
                    "key": i.key,
                    "pid": i.pid,
                    "name": i.name,
                    "tier": i.tier,
                    "base_interval_s": i.base_s,
                    "final_interval_s": round(self.interval(i), 3),
                    "attempts": i.attempts,
                    "ok": i.ok,
                }
                for i in self.items
            ],
            # What this session actually demonstrated (never assumed): post-shutdown observability
            "observed_capabilities": {
                "engine_stops_observed": st.engine_stops_observed,
                "ecu_answers_after_engine_stop": st.vehicle_ok_after_engine_stop,
                "ecu_answered_for_s_after_engine_stop": st.vehicle_ok_after_engine_stop_span_s,
                "ecu_communication_losses": st.ecu_losses,
                "adapter_voltage_reads_while_ecu_unresponsive": st.adapter_voltage_while_ecu_lost,
                "adapter_voltage_attempts_while_ecu_unresponsive": st.adapter_voltage_attempts_while_ecu_lost,
                "ecu_probes": st.ecu_probes,
                "fan_activity": "manual annotations only: no generic OBD signal reports the radiator fan",
            },
        }
