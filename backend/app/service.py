"""Workbench: the one service layer used by both the CLI and the HTTP API.

It exposes read-only operations only: connect, scan, inventory, record,
annotate, replay. There is no pass-through for arbitrary adapter commands.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

from app.acquisition.hub import LiveHub
from app.acquisition.recorder import Recorder, compute_metrics
from app.adapters.base import Transport
from app.adapters.simulator import SCENARIOS, FakeElmServer
from app.adapters.tcp import DEFAULT_HOST, DEFAULT_PORT, VgateWiFiTransport
from app.manufacturer.extension import ExtensionRegistry, default_registry
from app.models.domain import Annotation, Exchange, Reading
from app.obd.client import ObdClient
from app.storage.db import Store
from app.vehicles.profiles import DEFAULT_PROFILE_ID, PROFILES, VehicleProfile, get_profile, resolve_schedule


class BusyError(Exception):
    pass


class NotConnectedError(Exception):
    pass


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
        self._op = threading.Lock()
        self._buf_lock = threading.Lock()
        self._ex_buf: list[Exchange] = []
        self._connect_log: list[Exchange] = []
        self._sid: str | None = None
        self._phase = "init"
        self._stop = threading.Event()

    # -- vehicle profile -------------------------------------------------------
    @property
    def profile(self) -> VehicleProfile:
        pid = self.store.get_setting("active_profile", DEFAULT_PROFILE_ID)
        return get_profile(pid if pid in PROFILES else DEFAULT_PROFILE_ID)

    def set_profile(self, profile_id: str) -> VehicleProfile:
        prof = get_profile(profile_id)
        self.store.set_setting("active_profile", prof.id)
        return prof

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
        self.disconnect()
        self.transport, self.source, self.scenario = transport, source, scenario
        self._connect_log = []
        transport.listeners.append(self._on_exchange)
        self.client = ObdClient(transport, allow_vin=allow_vin)
        try:
            self._phase = "init"
            self.client.initialize()
            self._phase = "discover"
            self.client.discover()
        except Exception:
            self.disconnect()
            raise
        self.hub.publish({"type": "status", "status": self.status()})
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
        if self.active:
            raise BusyError("a recording is running")
        if mode == "simulator":
            if scenario not in SCENARIOS:
                raise KeyError(f"unknown simulator scenario {scenario!r}")
            self.disconnect()
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
                st = self.attach(transport, "simulator", scenario)
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
        return self.attach(transport, "real")

    def disconnect(self) -> None:
        self._stop.set()
        if self.transport is not None:
            self.transport.close()
        if self.sim_server is not None:
            self.sim_server.stop()
        self.transport = self.client = self.sim_server = None
        self.source, self.scenario = "none", None

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
            "vehicle_profile": self.profile.id,
        }

    def _need_client(self) -> ObdClient:
        if self.client is None:
            raise NotConnectedError("not connected to an adapter")
        return self.client

    # -- sessions ----------------------------------------------------------------
    @contextmanager
    def _session(self, kind: str, label: str):
        client = self._need_client()
        if not self._op.acquire(blocking=False):
            raise BusyError("another scan or recording is in progress")
        clock = client.transport.clock
        try:
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
                    "transport": client.transport.describe(),
                },
            )
            self.store.add_exchanges(sid, list(self._connect_log))
            self._sid = sid
            meta: dict[str, Any] = {}
            try:
                yield sid, client, meta
            finally:
                self._flush()
                self._sid = None
                self._phase = "idle"
                meta["capabilities"] = client.capabilities.model_dump() if client.capabilities else None
                meta["metrics"] = [m.model_dump() for m in compute_metrics(self.store.samples(sid))]
                self.store.update_session(sid, ended_at=clock.wall(), meta=meta)
        finally:
            self._op.release()

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

    def plan_recording(self, profile_id: str = "idle", pids: list[int] | None = None) -> dict[str, list[int]]:
        supported = self._need_client()._caps().supported_mode01()
        if pids:
            return {
                "fast": [p for p in pids if p in supported],
                "slow": [],
                "unsupported": [p for p in pids if p not in supported],
            }
        fast, slow, missing = resolve_schedule(self.profile.recording_profile(profile_id), supported)
        return {"fast": fast, "slow": slow, "unsupported": missing}

    def record(
        self,
        duration_s: float,
        profile_id: str = "idle",
        pids: list[int] | None = None,
        label: str = "",
        pre_scan: bool = True,
        post_scan: bool = True,
        freeze_frame: bool = False,
        started: threading.Event | None = None,
    ) -> str:
        plan = self.plan_recording(profile_id, pids)
        self._stop = threading.Event()
        with self._session("record", label) as (sid, client, meta):
            clock = client.transport.clock
            meta.update(
                selected=plan, recording_profile=None if pids else profile_id, requested_duration_s=duration_s
            )
            self.active = {"session_id": sid, "started_at": clock.wall(), "duration_s": duration_s, **plan}
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

            try:
                if pre_scan:
                    self._phase = "scan:pre"
                    self.store.add_scan(sid, client.scan_dtcs("pre", freeze_frame))
                self._phase = "record"
                Recorder(client, plan["fast"], plan["slow"], duration_s, on_readings, self._stop).run()
                self._flush(list(pending))
                pending.clear()
                if post_scan:
                    self._phase = "scan:post"
                    self.store.add_scan(sid, client.scan_dtcs("post", freeze_frame))
            except Exception as exc:  # keep what was captured; record why it ended
                meta["aborted"] = f"{type(exc).__name__}: {exc}"
                self._flush(list(pending))
            finally:
                self.active = None
                self.hub.publish({"type": "recording", "state": "stopped", "session_id": sid})
        return sid

    def stop_recording(self) -> None:
        self._stop.set()

    def annotate(self, sid: str, label: str, note: str = "") -> Annotation:
        if self.store.get_session(sid) is None:
            raise KeyError(f"no session {sid}")
        live = self.active and self.active["session_id"] == sid and self.client
        clock = self.client.transport.clock if live else None  # type: ignore[union-attr]
        import time

        a = Annotation(
            t_wall=clock.wall() if clock else time.time(),
            t_mono=clock.monotonic() if clock else None,
            label=label,
            note=note,
        )
        self.store.add_annotation(sid, a)
        self.hub.publish({"type": "annotation", "session_id": sid, "annotation": a.model_dump()})
        return a
