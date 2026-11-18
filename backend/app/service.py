"""Workbench: the one service layer used by both the CLI and the HTTP API.

It exposes read-only operations only: connect, scan, inventory, record,
annotate, replay. There is no pass-through for arbitrary adapter commands.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any

from app.acquisition.hub import LiveHub
from app.acquisition.monitor import MonitoringRecorder
from app.acquisition.recorder import Recorder, compute_metrics
from app.adapters.base import Transport
from app.adapters.simulator import SCENARIOS, FakeElmServer
from app.adapters.tcp import DEFAULT_HOST, DEFAULT_PORT, VgateWiFiTransport
from app.manufacturer.extension import ExtensionRegistry, default_registry
from app.models.domain import Annotation, Exchange, Reading
from app.obd.client import ObdClient
from app.storage.db import Store
from app.vehicles.profiles import (
    DEFAULT_PROFILE_ID,
    PROFILES,
    RecordingProfile,
    VehicleProfile,
    get_profile,
    resolve_schedule,
    resolve_tiers,
)

#: a session whose owner has not refreshed its lease for this long is treated as interrupted
LEASE_STALE_S = 120.0
LEASE_REFRESH_S = 10.0
DRIVE_TYPES = ("city", "highway", "mixed", "other")
#: latest measurements attached (as context, with their age) to manual events during a live recording
EVENT_CONTEXT_SIGNALS = {"0C": "rpm", "05": "coolant_c", "0D": "speed_kmh", "42": "ecu_voltage_v"}


class BusyError(Exception):
    pass


class NotConnectedError(Exception):
    pass


class ProfileMismatchError(BusyError):
    """The active vehicle profile is not the one the current connection was established for."""


class Workbench:
    def __init__(
        self, store: Store, hub: LiveHub | None = None, registry: ExtensionRegistry | None = None
    ) -> None:
        self.store = store
        self.hub = hub or LiveHub()
        self.registry = registry or default_registry()
        self.transport: Transport | None = None
        self.client: ObdClient | None = None
        self.sim_server: FakeElmServer | None = None
        self.source = "none"  # real | simulator
        self.scenario: str | None = None
        self.active: dict[str, Any] | None = None  # the running recording, if any
        # ONE lock for every lifecycle step that touches the adapter or its identity:
        # connect, disconnect, profile switch, scan, inventory, record. Never blocks:
        # a second operation is rejected with BusyError naming the one in progress.
        self._op = threading.Lock()
        self.operation: str | None = None
        self.connected_profile: str | None = None  # profile the current connection was opened for
        self._buf_lock = threading.Lock()
        self._ex_buf: list[Exchange] = []
        self._connect_log: list[Exchange] = []
        self._sid: str | None = None
        self._phase = "init"
        self._stop = threading.Event()
        self._resume = threading.Event()  # "resume ECU polling" for a monitoring recording
        self._last_status: dict[str, Any] = {}

    # -- vehicle profile -------------------------------------------------------
    @property
    def profile(self) -> VehicleProfile:
        pid = self.store.get_setting("active_profile", DEFAULT_PROFILE_ID)
        return get_profile(pid if pid in PROFILES else DEFAULT_PROFILE_ID)

    def set_profile(self, profile_id: str) -> VehicleProfile:
        """Switch vehicle profile. Refused during any operation and while connected to a
        different vehicle: capabilities discovered on one ECU must never be reused under
        another profile, so switching requires disconnect + a new connection (re-probe)."""
        prof = get_profile(profile_id)
        with self._exclusive("switch vehicle profile"):
            if self.client is not None and self.connected_profile not in (None, prof.id):
                raise ProfileMismatchError(
                    f"connected for {self.connected_profile}; disconnect before switching to {prof.id} "
                    "(the new vehicle must be probed on a fresh connection)"
                )
            self.store.set_setting("active_profile", prof.id)
        return prof

    @contextmanager
    def _exclusive(self, what: str):
        if not self._op.acquire(blocking=False):
            running = self.operation or "another operation"
            raise BusyError(f"cannot {what}: {running} in progress (stop it first)")
        self.operation = what
        try:
            yield
        finally:
            self.operation = None
            self._op.release()

    def vehicle_record(self, profile_id: str | None = None) -> dict[str, Any]:
        """User-confirmed identity + owner-reported context for a profile (editable)."""
        prof = get_profile(profile_id) if profile_id else self.profile
        default = {
            "confirmed": {"model": "", "engine": "", "ecu": "", "model_year": ""},
            "owner_reported": prof.owner_reported,
        }
        return self.store.get_setting(f"vehicle:{prof.id}", default)

    def save_vehicle_record(self, record: dict[str, Any], profile_id: str | None = None) -> None:
        prof = get_profile(profile_id) if profile_id else self.profile
        self.store.set_setting(f"vehicle:{prof.id}", record)

    # -- connection --------------------------------------------------------------
    def _on_exchange(self, ex: Exchange) -> None:
        ex.phase = self._phase
        with self._buf_lock:
            (self._ex_buf if self._sid else self._connect_log).append(ex)

    def attach(
        self, transport: Transport, source: str, scenario: str | None = None, allow_vin: bool = False
    ) -> dict[str, Any]:
        """Initialise the adapter on an already-constructed transport and discover capabilities."""
        with self._exclusive("connect"):
            with self._final_status():
                self._attach(transport, source, scenario, allow_vin)
            return self._last_status

    @contextmanager
    def _final_status(self):
        """End a lifecycle operation: while still holding the operation lock (so nothing can
        interleave), clear the operation label, then publish/keep the resulting status.
        Runs on success and failure, so no client is left with a phantom busy state."""
        try:
            yield
        finally:
            self.operation = None  # the work is done; the lock is released right after
            self._last_status = self.status()
            self.hub.publish({"type": "status", "status": self._last_status})

    def _attach(
        self, transport: Transport, source: str, scenario: str | None = None, allow_vin: bool = False
    ) -> dict[str, Any]:
        self._disconnect()
        self.transport, self.source, self.scenario = transport, source, scenario
        self.connected_profile = self.profile.id
        self._connect_log = []
        transport.listeners.append(self._on_exchange)
        self.client = ObdClient(transport, allow_vin=allow_vin)
        try:
            self._phase = "init"
            self.client.initialize()
            self._phase = "discover"
            self.client.discover()
        except Exception:
            self._disconnect()
            raise
        return self.status()

    def connect(
        self,
        mode: str = "real",
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        scenario: str = "healthy_idle",
        timeout_s: float = 5.0,
        min_interval_s: float = 0.0,
    ) -> dict[str, Any]:
        if mode == "simulator" and scenario not in SCENARIOS:
            raise KeyError(f"unknown simulator scenario {scenario!r}")
        with self._exclusive("connect"):
            with self._final_status():
                self._connect(mode, host, port, scenario, timeout_s, min_interval_s)
            return self._last_status

    def _connect(
        self, mode: str, host: str, port: int, scenario: str, timeout_s: float, min_interval_s: float
    ) -> dict[str, Any]:
        if mode == "simulator":
            self._disconnect()
            server = FakeElmServer(scenario).start()
            transport = VgateWiFiTransport(
                server.host,
                server.port,
                connect_timeout_s=timeout_s,
                default_timeout_s=timeout_s,
                min_interval_s=min_interval_s,
                backoff_initial_s=0.2,
            )
            try:
                st = self._attach(transport, "simulator", scenario)
            except Exception:
                server.stop()
                raise
            self.sim_server = server
            return st
        transport = VgateWiFiTransport(
            host,
            port,
            connect_timeout_s=timeout_s,
            default_timeout_s=timeout_s,
            min_interval_s=min_interval_s,
        )
        return self._attach(transport, "real")

    def disconnect(self) -> None:
        """Close the adapter connection. Rejected while a scan/inventory/recording runs:
        stop the recording first (stop_recording), then disconnect."""
        with self._exclusive("disconnect"), self._final_status():
            self._disconnect()

    def shutdown(self, wait_s: float = 10.0) -> None:
        """Process exit: stop a recording, cancel the request in flight, wait for the running
        operation to close its session cleanly, then disconnect."""
        self._stop.set()
        if self.transport is not None:
            self.transport.cancel()
        if self._op.acquire(timeout=wait_s):
            try:
                self._disconnect()
            finally:
                self._op.release()
        elif self.transport is not None:
            self.transport.close()  # the operation is stuck; its own error handling ends the session

    def _disconnect(self) -> None:
        self._stop.set()
        if self.transport is not None:
            self.transport.close()
        if self.sim_server is not None:
            self.sim_server.stop()
        self.transport = self.client = self.sim_server = None
        self.source, self.scenario = "none", None
        self.connected_profile = None

    def status(self) -> dict[str, Any]:
        c = self.client
        return {
            "read_only": True,
            "connected": bool(self.transport and self.transport.state == "connected"),
            "source": self.source,
            "synthetic": self.source == "simulator",
            "scenario": self.scenario,
            "transport": self.transport.describe() if self.transport else None,
            "adapter": c.adapter.model_dump() if c else None,
            "capabilities": c.capabilities.model_dump() if c and c.capabilities else None,
            "recording": self.active,
            "operation": self.operation,
            "vehicle_profile": self.profile.id,
            "connected_profile": self.connected_profile,
        }

    def _need_client(self) -> ObdClient:
        if self.client is None:
            raise NotConnectedError("not connected to an adapter")
        return self.client

    # -- sessions ----------------------------------------------------------------
    @contextmanager
    def _session(self, kind: str, label: str):
        what = {
            "scan": "scan",
            "snapshot": "inventory",
            "record": "recording",
            "capability": "DDE capability assessment",
        }[kind]
        with self._exclusive(what):
            client = self._need_client()
            if self.connected_profile != self.profile.id:  # e.g. changed by another process
                raise ProfileMismatchError(
                    f"active profile {self.profile.id} does not match the connected vehicle "
                    f"({self.connected_profile}); reconnect to probe the vehicle again"
                )
            with self._session_body(kind, label, client) as ctx:
                yield ctx

    @contextmanager
    def _session_body(self, kind: str, label: str, client: ObdClient):
        clock = client.transport.clock
        sid = self.store.create_session(
            kind=kind,
            vehicle_profile=self.profile.id,
            source=self.source,
            synthetic=self.source != "real",
            label=label,
            started_at=clock.wall(),
            meta={
                "adapter": client.adapter.model_dump(),
                "capabilities": client.capabilities.model_dump() if client.capabilities else None,
                "scenario": self.scenario,
                # the adapter-link diagnostics when the session began; the FINAL ones are
                # written to meta["transport"] when the session ends (see finally below)
                "transport_at_start": client.transport.describe(),
            },
        )
        self.store.add_exchanges(sid, list(self._connect_log))
        self._sid = sid
        meta: dict[str, Any] = {}
        beat_stop = threading.Event()
        self._lease(sid)
        beat = threading.Thread(target=self._heartbeat, args=(sid, beat_stop), daemon=True, name="lease")
        beat.start()
        try:
            yield sid, client, meta
        finally:
            beat_stop.set()
            self._flush()
            self._sid = None
            self._phase = "idle"
            meta["capabilities"] = client.capabilities.model_dump() if client.capabilities else None
            # final adapter-link diagnostics (resyncs, reconnects, last error, state...), also for
            # cancelled/aborted sessions: what exports and session details report
            meta["transport"] = client.transport.describe()
            meta["metrics"] = [m.model_dump() for m in compute_metrics(self.store.samples(sid))]
            self.store.update_session(sid, ended_at=clock.wall(), meta=meta)
            self.store.delete_setting(f"lease:{sid}")

    # -- interrupted sessions -------------------------------------------------------
    def _lease(self, sid: str) -> None:
        self.store.set_setting(f"lease:{sid}", {"pid": os.getpid(), "t": time.time()})

    def _heartbeat(self, sid: str, stop: threading.Event) -> None:
        """Mark the session as owned by a live process (lets another process tell a running
        recording from one whose process died)."""
        while not stop.wait(LEASE_REFRESH_S):
            try:
                self._lease(sid)
            except Exception:  # never let bookkeeping break a recording
                pass

    def recover_interrupted(self, stale_after_s: float = LEASE_STALE_S) -> list[str]:
        """Close sessions left open by a process that died (crash, kill, power loss).

        Everything that was written stays; the session gets an end time (its newest stored
        record), an ``aborted`` reason and sampling metrics. A session whose lease is still
        being refreshed (a recording in another process) is left alone.
        """
        recovered: list[str] = []
        for s in self.store.open_sessions():
            sid = s["id"]
            if sid == self._sid:
                continue
            lease = self.store.get_setting(f"lease:{sid}")
            if lease and time.time() - float(lease.get("t", 0)) < stale_after_s:
                continue
            last = self.store.last_activity(sid)
            if not lease and last is not None and time.time() - last < stale_after_s:
                continue  # no lease (older version / other tool) but still being written
            ended = last if last is not None else s["started_at"]
            self.store.update_session(
                sid,
                ended_at=ended,
                # merged into the meta persisted at session start (recording settings are kept)
                meta={
                    "interrupted": True,
                    "aborted": "interrupted: the application stopped before the session was closed; "
                    "data up to the last stored record is kept",
                    "recovered": {"at": time.time(), "last_record_wall": last, "lease": lease},
                    "metrics": [m.model_dump() for m in compute_metrics(self.store.samples(sid))],
                },
            )
            self.store.delete_setting(f"lease:{sid}")
            recovered.append(sid)
        return recovered

    def _flush(self, readings: list[Reading] | None = None) -> None:
        with self._buf_lock:
            exs, self._ex_buf = self._ex_buf, []
        if self._sid:
            self.store.add_exchanges(self._sid, exs)
            if readings:
                self.store.add_samples(self._sid, readings)

    def scan(self, label: str = "", read_vin: bool = False, freeze_frame: bool = True) -> str:
        with self._session("scan", label) as (sid, client, meta):
            self._phase = "scan:scan"
            self.store.add_scan(sid, client.scan_dtcs("scan", freeze_frame))
            self._phase = "info"
            meta["vehicle_info"] = client.vehicle_info(read_vin)
        return sid

    def inventory(self, label: str = "", read_vin: bool = False) -> str:
        with self._session("snapshot", label) as (sid, client, meta):
            self._phase = "inventory"
            self._flush(client.snapshot())
            self._phase = "info"
            meta["vehicle_info"] = client.vehicle_info(read_vin)
            meta["monitors"] = client.monitors()
        return sid

    def dde_capability(self, label: str = "", read_vin: bool = False) -> str:
        """DDE-01: repeatable capability assessment on the existing connection.

        Sends only what the generic allowlist already permits: service 09 identification
        InfoTypes (calibration ID, CVN, ECU name; VIN only with consent). No manufacturer
        request exists on this path. The report is computed from the session's own raw trace
        (including the adapter initialisation) and stored in ``meta["dde_capability"]``.
        """
        from app.manufacturer.bmw.capability import assess

        with self._session("capability", label) as (sid, client, meta):
            self._phase = "capability"
            ident = client.identification(include_vin=read_vin)
            if read_vin:
                vins = ident["vin"].get("answers") or []
                meta["vehicle_info"] = {
                    "vin": vins[0]["text"] if vins else None,
                    "vin_status": "read" if vins else ident["vin"]["status"],
                    "calibration_ids": [
                        {k: a[k] for k in ("ecu", "text", "raw_hex")}
                        for a in ident["calibration_id"].get("answers") or []
                    ],
                }
            self._flush()
            meta["dde_capability"] = assess(
                adapter=client.adapter.model_dump(),
                capabilities=client.capabilities.model_dump() if client.capabilities else None,
                exchanges=self.store.exchanges(sid),
                source=self.source,
                synthetic=self.source != "real",
                session_id=sid,
                assessed_at=client.transport.clock.wall(),
            )
        return sid

    # -- DDE-03: gated manufacturer reads -----------------------------------------------------
    def dde_settings(self, profile_id: str | None = None) -> dict[str, Any]:
        """User-controlled DDE settings per vehicle profile: explicit opt-in (default OFF) and the
        DDE variant as confirmed by the user (e.g. from a BMW tester printout; never inferred)."""
        prof = get_profile(profile_id) if profile_id else self.profile
        return self.store.get_setting(f"dde:{prof.id}", {"opt_in": False, "confirmed_variant": None})

    def set_dde_settings(
        self, opt_in: bool | None = None, confirmed_variant: str | None = None, clear_variant: bool = False
    ) -> dict[str, Any]:
        prof = self.profile
        if not prof.supports_dde:
            raise ValueError(f"{prof.id} has no BMW DDE")
        cur = dict(self.dde_settings())
        if opt_in is not None:
            cur["opt_in"] = bool(opt_in)
        if clear_variant:
            cur["confirmed_variant"] = None
        elif confirmed_variant is not None:
            v = confirmed_variant.strip()
            if not v or len(v) > 80:
                raise ValueError("confirmed variant must be 1..80 characters")
            cur["confirmed_variant"] = v
        cur["updated_at"] = time.time()
        self.store.set_setting(f"dde:{prof.id}", cur)
        return cur

    def dde_context(self):
        """ExecutionContext for the current connection: opt-in + variant from the settings, the
        DDE-01 verdict from the latest REAL capability report, simulator or real."""
        from app.manufacturer.bmw.catalog import ExecutionContext

        st = self.dde_settings()
        synthetic = self.source != "real"
        verdict = None
        if not synthetic:
            rep = self.latest_dde_capability(real_only=True)
            verdict = rep["verdicts"]["manufacturer_transport"]["verdict"] if rep else None
        return ExecutionContext(
            opted_in=bool(st.get("opt_in")),
            confirmed_variant=st.get("confirmed_variant"),
            adapter_verdict=verdict,
            synthetic_session=synthetic,
        )

    def dde_catalog(self):
        """Real catalog for a real adapter; the SYNTHETIC catalog only against the simulator."""
        from app.manufacturer.bmw.catalog import default_catalog

        if self.source == "simulator":
            from app.manufacturer.bmw.synthetic import synthetic_catalog

            return synthetic_catalog()
        return default_catalog()

    def dde_runtime(self):
        """A DdeRuntime for the current connection, or None (no connection / no DDE on this profile)."""
        from app.manufacturer.bmw.runtime import DdeRuntime

        if self.client is None or not self.profile.supports_dde:
            return None
        return DdeRuntime(self.dde_catalog(), self.dde_context(), self.client)

    def dde_status(self) -> dict[str, Any]:
        prof = self.profile
        if not prof.supports_dde:
            return {"vehicle_profile": prof.id, "supported": False}
        cat = self.dde_catalog()
        ctx = self.dde_context()
        return {
            "vehicle_profile": prof.id,
            "supported": True,
            "settings": self.dde_settings(),
            "connected": self.client is not None,
            "source": self.source,
            **cat.describe(ctx),
        }

    def latest_dde_capability(
        self, profile_id: str | None = None, real_only: bool = False
    ) -> dict[str, Any] | None:
        """Most recent stored capability report for a vehicle profile (None if never run)."""
        prof = get_profile(profile_id) if profile_id else self.profile
        for s in reversed(self.store.list_sessions(prof.id, order="rowid")):
            if s["kind"] == "capability" and s["meta"].get("dde_capability"):
                if real_only and s["synthetic"]:
                    continue
                return s["meta"]["dde_capability"]
        return None

    def recording_profile(self, profile_id: str) -> RecordingProfile:
        return self.profile.recording_profile(profile_id)

    def plan_recording(self, profile_id: str = "idle", pids: list[int] | None = None) -> dict[str, list[int]]:
        supported = self._need_client()._caps().supported_mode01()
        if pids:
            return {
                "fast": [p for p in pids if p in supported],
                "slow": [],
                "unsupported": [p for p in pids if p not in supported],
            }
        rp = self.profile.recording_profile(profile_id)
        if rp.tiers is not None:
            return resolve_tiers(rp, supported)
        fast, slow, missing = resolve_schedule(rp, supported)
        return {"fast": fast, "slow": slow, "unsupported": missing}

    def adapter_voltage_available(self) -> bool:
        """ATRV answered during adapter initialisation (clones may not implement it)."""
        return bool(self.client and self.client.adapter.voltage)

    def record(
        self,
        duration_s: float | None,
        profile_id: str = "idle",
        pids: list[int] | None = None,
        label: str = "",
        pre_scan: bool = True,
        post_scan: bool = True,
        freeze_frame: bool = False,
        started: threading.Event | None = None,
        *,
        until_stop: bool = False,
        max_duration_s: float | None = None,
        drive_type: str | None = None,
        resume_probe_s: float | None = None,
    ) -> str:
        """Record until ``duration_s`` elapsed or, with ``until_stop``, until stop_recording()
        (``max_duration_s`` is then an optional safety limit). Samples are written to SQLite
        about once a second; nothing is held for the whole drive."""
        if until_stop:
            limit = max_duration_s
        elif duration_s is None:
            raise ValueError("a duration is required unless the recording runs until stopped")
        else:
            limit = duration_s
        if limit is not None and limit <= 0:
            raise ValueError("duration must be positive")
        if drive_type is not None and drive_type not in DRIVE_TYPES:
            raise ValueError(f"drive type must be one of {', '.join(DRIVE_TYPES)}")
        plan = self.plan_recording(profile_id, pids)
        rp = None if pids else self.profile.recording_profile(profile_id)
        tiered = rp is not None and rp.tiers is not None
        with self._session("record", label) as (sid, client, meta):
            self._stop = threading.Event()  # only once we own the operation lock
            self._resume = threading.Event()
            clock = client.transport.clock
            use_atrv = bool(tiered and rp and rp.adapter_voltage and client.adapter.voltage)
            meta.update(
                selected=plan,
                recording_profile=None if pids else profile_id,
                requested_duration_s=None if until_stop else duration_s,
                until_stop=until_stop,
                max_duration_s=max_duration_s if until_stop else None,
            )
            if drive_type:
                meta["user_labels"] = {"drive_type": drive_type}
            if tiered and rp is not None:
                meta["sampling_plan"] = {
                    "tiers": rp.tiers.as_dict() if rp.tiers else None,
                    "adapter_voltage": "polled"
                    if use_atrv
                    else ("adapter did not answer ATRV" if rp.adapter_voltage else "not requested"),
                }
            # Persist the recording settings NOW, before the pre-scan or the first sample: if the
            # process dies mid-recording, the recovered session still knows it is a monitoring
            # session and how it was recorded (profile, signals, sampling plan, labels, limits).
            self.store.update_session(sid, meta=dict(meta))
            # user labels are owned by the store from here on (set_drive_type may change them
            # while recording); the final meta write must not overwrite a newer label
            meta.pop("user_labels", None)
            self.active = {
                "session_id": sid,
                "started_at": clock.wall(),
                "duration_s": limit,
                "until_stop": until_stop,
                "profile": profile_id if not pids else None,
                "monitoring": bool(tiered and rp and rp.monitoring),
                **plan,
            }
            self.hub.latest.clear()  # never show/attach values from an earlier recording
            self.hub.publish({"type": "recording", "state": "started", **self.active})
            if started:
                started.set()
            pending: list[Reading] = []
            last_flush = clock.monotonic()

            def on_readings(rs: list[Reading]) -> None:
                nonlocal last_flush
                pending.extend(rs)
                for r in rs:
                    self.hub.publish({"type": "reading", "session_id": sid, "reading": r.model_dump()})
                if clock.monotonic() - last_flush >= 1.0 or len(pending) >= 200:
                    self._flush(list(pending))
                    pending.clear()
                    last_flush = clock.monotonic()

            def on_event(a: Annotation) -> None:
                self.store.add_annotation(sid, a)
                self.hub.publish({"type": "annotation", "session_id": sid, "annotation": a.model_dump()})

            def on_state(snap: dict[str, Any]) -> None:
                if self.active and self.active.get("session_id") == sid:
                    self.active["monitor"] = snap
                self.hub.publish({"type": "monitor", "session_id": sid, "monitor": snap})

            monitor: MonitoringRecorder | None = None
            try:
                if pre_scan:
                    self._phase = "scan:pre"
                    self.store.add_scan(sid, client.scan_dtcs("pre", freeze_frame))
                self._phase = "record"
                if tiered and rp is not None and rp.tiers is not None:
                    monitor = MonitoringRecorder(
                        client,
                        plan,
                        rp.tiers,
                        use_atrv,
                        limit,
                        on_readings,
                        on_event,
                        self._stop,
                        self._resume,
                        on_state,
                        resume_probe_s=resume_probe_s,
                        primary_ecu=client.capabilities.primary_ecu if client.capabilities else None,
                    )
                    monitor.run()
                else:
                    Recorder(
                        client,
                        plan["fast"],
                        plan["slow"],
                        float("inf") if limit is None else limit,
                        on_readings,
                        self._stop,
                    ).run()
                self._flush(list(pending))
                pending.clear()
                if post_scan:
                    if monitor is not None and monitor.state != "running":
                        # never send a scan to an ECU that stopped answering (it may be asleep)
                        meta["post_scan_skipped"] = (
                            f"recording ended with the link in state '{monitor.state}'; no request is sent "
                            "to an unresponsive ECU"
                        )
                    else:
                        self._phase = "scan:post"
                        self.store.add_scan(sid, client.scan_dtcs("post", freeze_frame))
            except BaseException as exc:  # keep what was captured; record why it ended
                meta["aborted"] = f"{type(exc).__name__}: {exc}"
                self._flush(list(pending))
                pending.clear()
                if not isinstance(exc, Exception):
                    raise  # KeyboardInterrupt / SystemExit: the session is still closed below
            finally:
                if monitor is not None:
                    meta["monitoring"] = monitor.summary()
                self.active = None
                self.hub.publish({"type": "recording", "state": "stopped", "session_id": sid})
        return sid

    def stop_recording(self) -> None:
        self._stop.set()

    def resume_ecu_polling(self) -> dict[str, Any]:
        """Ask a running monitoring recording to send ONE vehicle request to check whether the
        ECU answers again (after ecu_communication_lost). Does not touch the operation lock."""
        act = self.active
        if not act or not act.get("monitoring"):
            raise NotConnectedError("no monitoring recording is running")
        mon = act.get("monitor") or {}
        state = mon.get("state")
        if state == "adapter_unreachable" and mon.get("ecu_polling_paused"):
            return {
                "requested": False,
                "state": state,
                "detail": "the adapter is unreachable; vehicle polling stays paused. The recorder retries the "
                "adapter only (no vehicle request). Check the adapter's Wi-Fi, or stop and save the recording.",
            }
        if state != "ecu_unresponsive":
            return {"requested": False, "state": state, "detail": "vehicle polling is not paused"}
        self._resume.set()
        out: dict[str, Any] = {"requested": True, "state": state}
        if mon.get("resume_requires_reinit"):
            # the link was re-opened adapter-only while paused: the probe re-initialises first
            out["detail"] = (
                "the adapter is re-initialised first (ATZ ... 0100), then one vehicle request is sent"
            )
        return out

    def set_drive_type(self, sid: str, drive_type: str | None) -> dict[str, Any]:
        """User label city / highway / mixed / other (or None to clear). Stored in session meta."""
        s = self.store.get_session(sid)
        if s is None:
            raise KeyError(f"no session {sid}")
        if drive_type is not None and drive_type not in DRIVE_TYPES:
            raise ValueError(f"drive type must be one of {', '.join(DRIVE_TYPES)}")
        labels = dict(s["meta"].get("user_labels") or {})
        labels["drive_type"] = drive_type
        self.store.update_session(sid, meta={"user_labels": labels})
        return labels

    def _event_context(self) -> dict[str, Any]:
        """Latest measured values (with their age) at the moment a manual event is marked."""
        out: dict[str, Any] = {}
        now = time.time()
        for key, r in list(self.hub.latest.items()):
            pid, _, ecu = key.partition(":")
            mode = r.get("mode", 1)
            name = "adapter_voltage_v" if mode == 0 else EVENT_CONTEXT_SIGNALS.get(f"{int(pid):02X}")
            if not name or r.get("value") is None or r.get("quality") != "ok":
                continue
            primary = (
                self.client.capabilities.primary_ecu if self.client and self.client.capabilities else None
            )
            if mode == 1 and ecu not in ("None", primary or ecu):
                continue
            out[name] = {"value": r["value"], "age_s": round(now - r["t_wall"], 1)}
        act = self.active or {}
        mon = act.get("monitor") or {}
        if mon:
            out["link_state"] = mon.get("state")
            out["engine"] = mon.get("engine")
        return out

    def annotate(
        self, sid: str, label: str = "", note: str = "", event_type: str | None = None
    ) -> Annotation:
        """Manual marker (free text) or predefined event (``event_type``). Works during a recording,
        after the engine stopped and after the recording ended (wall-clock time then)."""
        session = self.store.get_session(sid)
        if session is None:
            raise KeyError(f"no session {sid}")
        evidence: dict[str, Any] = {}
        if event_type is not None:
            prof = get_profile(session["vehicle_profile"])
            labels = {i: lbl for i, lbl, _ in prof.monitoring_events}
            if event_type not in labels:
                raise ValueError(
                    f"unknown event type {event_type!r} for {prof.id}; known: {', '.join(labels) or 'none'}"
                )
            label = label or event_type
        if not label:
            raise ValueError("a marker needs a label or an event type")
        live = self.active and self.active["session_id"] == sid and self.client
        clock = self.client.transport.clock if live else None  # type: ignore[union-attr]
        if live and event_type is not None:
            ctx = self._event_context()
            if ctx:
                evidence["context_at_mark"] = ctx
        a = Annotation(
            t_wall=clock.wall() if clock else time.time(),
            t_mono=clock.monotonic() if clock else None,
            label=label,
            note=note,
            event_type=event_type,
            origin="manual",
            evidence=evidence,
        )
        self.store.add_annotation(sid, a)
        self.hub.publish({"type": "annotation", "session_id": sid, "annotation": a.model_dump()})
        return a
