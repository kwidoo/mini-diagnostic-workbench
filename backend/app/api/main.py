"""Local REST + WebSocket API. Binds to 127.0.0.1 by default; no cloud calls, no telemetry.

There is intentionally no endpoint that accepts an adapter command. Every
vehicle-facing operation goes through Workbench, which only knows read-only
operations.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config
from app.acquisition.recorder import GOOD
from app.adapters.base import TransportError
from app.adapters.simulator import SCENARIOS
from app.adapters.tcp import DEFAULT_HOST, DEFAULT_PORT
from app.diagnostics.engine import analyze_session
from app.diagnostics.monitoring import history_for, summary_for
from app.obd.client import ObdError
from app.parameters.standard_pids import ADAPTER_MODE, ADAPTER_VOLTAGE_NAME, PIDS, RAW_ONLY_PIDS, pid_name
from app.service import BusyError, NotConnectedError, Workbench
from app.storage.db import Store
from app.storage.export import build_zip
from app.storage.transcript import ingest_file
from app.vehicles.profiles import PROFILES


class ConnectBody(BaseModel):
    mode: str = "real"  # real | simulator
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    scenario: str = "healthy_idle"
    timeout_s: float = Field(5.0, gt=0, le=60)


class ScanBody(BaseModel):
    label: str = ""
    read_vin: bool = False
    freeze_frame: bool = True


class RecordBody(BaseModel):
    duration_s: float = Field(120, gt=0, le=3600)  # ignored when until_stop is set
    profile: str = "idle"
    pids: list[int] | None = None
    label: str = ""
    until_stop: bool = False  # run until POST /api/recording/stop
    max_duration_s: float | None = Field(None, gt=0, le=86400)  # optional safety limit for until_stop
    drive_type: str | None = None  # user label: city | highway | mixed | other
    resume_probe_s: float | None = Field(None, ge=30, le=3600)  # opt-in: probe a silent ECU this often


class AnnotationBody(BaseModel):
    label: str = Field("", max_length=120)
    note: str = Field("", max_length=1000)
    event_type: str | None = Field(None, max_length=60)  # predefined event (see profile monitoring_events)


class DriveTypeBody(BaseModel):
    drive_type: str | None = None


class ProfileBody(BaseModel):
    id: str


class ReplayBody(BaseModel):
    fixture: str


def _series(store: Store, sid: str) -> dict[str, Any]:
    """Per-signal points on a relative timebase. Failed reads are listed separately; nothing is filled in."""
    session = store.get_session(sid)
    if session is None:
        raise KeyError(sid)
    primary = (session["meta"].get("capabilities") or {}).get("primary_ecu")
    all_rows = store.samples(sid)
    rows = [s for s in all_rows if s["mode"] == 1]
    first = min(rows, key=lambda s: s["t_mono"]) if rows else None
    t0 = first["t_mono"] if first else 0.0
    wall0 = first["t_wall"] if first else None
    signals: dict[int, dict[str, Any]] = {}
    # adapter-measured supply voltage (ATRV) is its own signal: pid -1, provenance "adapter"
    for s in rows + [r for r in all_rows if r["mode"] == ADAPTER_MODE]:
        if s["mode"] == 1 and s["ecu"] not in (None, primary) and primary is not None:
            continue
        adapter = s["mode"] == ADAPTER_MODE
        key = -1 if adapter else s["pid"]
        sig = signals.setdefault(
            key,
            {
                "pid": key,
                "hex": "ATRV" if adapter else f"{s['pid']:02X}",
                "name": ADAPTER_VOLTAGE_NAME if adapter else s["name"],
                "unit": s["unit"],
                "decoded": adapter or s["pid"] in PIDS,
                "provenance": "adapter" if adapter else "ecu",
                "points": [],
                "failed": [],
            },
        )
        t = round(s["t_mono"] - t0, 3)
        if s["quality"] in GOOD and s["value"] is not None:
            sig["points"].append([t, s["value"]])
        elif s["quality"] not in GOOD:
            sig["failed"].append([t, s["quality"]])

    def rel(a) -> float | None:
        if not rows:
            return None
        if a.t_mono is not None:
            return round(a.t_mono - t0, 3)
        return round(a.t_wall - wall0, 3) if wall0 is not None else None  # marked after the recording

    ann = [{"t": rel(a), **a.model_dump()} for a in store.annotations(sid)]
    return {
        "session_id": sid,
        "signals": sorted(signals.values(), key=lambda x: x["pid"]),
        "annotations": ann,
    }


def _session_detail(store: Store, sid: str) -> dict[str, Any]:
    session = store.get_session(sid)
    if session is None:
        raise KeyError(sid)
    return {
        **session,
        "scans": [s.model_dump() for s in store.scans(sid)],
        "annotations": [a.model_dump() for a in store.annotations(sid)],
    }


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or config.db_path())
    wb = Workbench(store)
    wb.recover_interrupted()  # close sessions a crashed/killed process left open (data is kept)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        wb.shutdown()

    app = FastAPI(title="MINI Diagnostic Workbench", version="0.1.0", lifespan=lifespan)
    app.state.wb = wb

    def fail(status: int, exc: Exception) -> JSONResponse:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return JSONResponse({"detail": str(msg)}, status_code=status)

    app.add_exception_handler(TransportError, lambda r, e: fail(502, e))
    app.add_exception_handler(ObdError, lambda r, e: fail(502, e))
    app.add_exception_handler(NotConnectedError, lambda r, e: fail(409, e))
    app.add_exception_handler(BusyError, lambda r, e: fail(409, e))
    app.add_exception_handler(KeyError, lambda r, e: fail(404, e))
    app.add_exception_handler(ValueError, lambda r, e: fail(400, e))

    # -- status / profiles --------------------------------------------------
    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return wb.status()

    @app.get("/api/profiles")
    def profiles() -> dict[str, Any]:
        out = []
        for p in PROFILES.values():
            d = p.to_dict()
            d["scenarios"] = [
                {"name": s.name, "description": s.description}
                for s in SCENARIOS.values()
                if s.vehicle == p.id
            ]
            d["manufacturer"] = wb.registry.status(p.ecu_ids)
            d["capabilities"] = p.capability_indicators(any(m["enabled"] for m in d["manufacturer"]))
            out.append(d)
        return {"active": wb.profile.id, "profiles": out}

    @app.put("/api/profiles/active")
    def set_profile(body: ProfileBody) -> dict[str, Any]:
        # rejected (409) during any operation and while connected to a different vehicle
        return {"active": wb.set_profile(body.id).id}

    @app.get("/api/vehicle")
    def vehicle() -> dict[str, Any]:
        return wb.vehicle_record()

    @app.put("/api/vehicle")
    def save_vehicle(record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record.get("confirmed"), dict) or not isinstance(
            record.get("owner_reported"), dict
        ):
            raise ValueError("vehicle record needs 'confirmed' and 'owner_reported' objects")
        wb.save_vehicle_record(record)
        return wb.vehicle_record()

    @app.get("/api/manufacturer")
    def manufacturer() -> list[dict[str, Any]]:
        return wb.registry.status(wb.profile.ecu_ids)

    # -- connection -----------------------------------------------------------
    @app.post("/api/connect")
    def connect(body: ConnectBody) -> dict[str, Any]:
        if body.mode not in ("real", "simulator"):
            raise ValueError("mode must be 'real' or 'simulator'")
        return wb.connect(body.mode, body.host, body.port, body.scenario, body.timeout_s)

    @app.post("/api/disconnect")
    def disconnect() -> dict[str, Any]:
        # rejected (409) while a scan/inventory/recording runs: POST /api/recording/stop first
        wb.disconnect()
        return wb.status()

    # -- read-only vehicle operations --------------------------------------------
    @app.post("/api/scan")
    def scan(body: ScanBody) -> dict[str, Any]:
        return _session_detail(store, wb.scan(body.label, body.read_vin, body.freeze_frame))

    @app.post("/api/inventory")
    def inventory(body: ScanBody) -> dict[str, Any]:
        sid = wb.inventory(body.label, body.read_vin)
        rows = store.samples(sid)
        supported = {r["pid"] for r in rows}
        return {
            **_session_detail(store, sid),
            "readings": rows,
            "unsupported_priority": [
                {"pid": p, "hex": f"{p:02X}", "name": pid_name(p)}
                for p in wb.profile.priority_pids
                if p not in supported
            ],
            "raw_only": [
                {"pid": p, "hex": f"{p:02X}", "name": RAW_ONLY_PIDS[p]}
                for p in sorted(supported)
                if p in RAW_ONLY_PIDS
            ],
        }

    # -- DDE capability (DDE-01): generic OBD + adapter identity only ------------------
    @app.post("/api/dde/capability")
    def dde_capability(body: ScanBody) -> dict[str, Any]:
        sid = wb.dde_capability(body.label, body.read_vin)
        detail = _session_detail(store, sid)
        return {**detail, "report": detail["meta"]["dde_capability"]}

    @app.get("/api/dde/catalog")
    def dde_catalog() -> dict[str, Any]:
        from app.manufacturer.bmw.catalog import ExecutionContext, default_catalog

        return default_catalog().describe(ExecutionContext())

    @app.get("/api/dde/capability/latest")
    def dde_capability_latest() -> dict[str, Any]:
        return {"vehicle_profile": wb.profile.id, "report": wb.latest_dde_capability()}

    @app.get("/api/sessions/{sid}/dde-capability")
    def dde_capability_offline(sid: str) -> dict[str, Any]:
        from app.manufacturer.bmw.capability import assess_session

        return assess_session(store, sid)  # read-only: recomputed from the stored trace

    @app.get("/api/recording/plan")
    def plan(profile: str = "idle") -> dict[str, Any]:
        p = wb.plan_recording(profile)
        rp = wb.recording_profile(profile)
        out = {
            k: [
                {
                    "pid": x,
                    "hex": f"{x:02X}",
                    "name": pid_name(x),
                    "unit": PIDS[x].unit if x in PIDS else None,
                }
                for x in v
            ]
            for k, v in p.items()
        }
        out["tiers"] = rp.tiers.as_dict() if rp.tiers else None
        out["monitoring"] = rp.monitoring
        out["description"] = rp.description
        out["adapter_voltage"] = (
            ("polled" if wb.adapter_voltage_available() else "adapter did not answer ATRV")
            if rp.adapter_voltage
            else "not requested"
        )
        return out

    @app.post("/api/recording/start")
    def start(body: RecordBody) -> dict[str, Any]:
        if wb.active:
            raise BusyError("a recording is already running")
        wb.plan_recording(body.profile, body.pids)  # validates connection + profile before the thread starts
        started = threading.Event()
        err: list[Exception] = []

        def run() -> None:
            try:
                wb.record(
                    body.duration_s,
                    body.profile,
                    body.pids,
                    body.label,
                    started=started,
                    until_stop=body.until_stop,
                    max_duration_s=body.max_duration_s,
                    drive_type=body.drive_type,
                    resume_probe_s=body.resume_probe_s,
                )
            except Exception as exc:  # surfaced through /api/status polling and the session meta
                err.append(exc)
                started.set()

        threading.Thread(target=run, daemon=True, name="recorder").start()
        started.wait(10)
        if err:
            raise err[0]
        return {"recording": wb.active}

    @app.post("/api/recording/stop")
    def stop() -> dict[str, Any]:
        wb.stop_recording()
        return {"stopping": True}

    @app.post("/api/recording/resume-ecu")
    def resume_ecu() -> dict[str, Any]:
        # one vehicle request to see whether a silent ECU answers again (user decision)
        return wb.resume_ecu_polling()

    # -- sessions ----------------------------------------------------------------
    @app.get("/api/sessions")
    def sessions(vehicle: str | None = None) -> list[dict[str, Any]]:
        out = []
        for s in store.list_sessions(vehicle, order="rowid"):
            meta = s.pop("meta")
            s["aborted"] = meta.get("aborted")
            s["scenario"] = meta.get("scenario")
            s["recording_profile"] = meta.get("recording_profile")
            s["monitoring"] = bool(meta.get("sampling_plan") or meta.get("monitoring"))
            s["drive_type"] = (meta.get("user_labels") or {}).get("drive_type")
            out.append(s)
        return out[::-1]

    @app.get("/api/sessions/{sid}")
    def session(sid: str) -> dict[str, Any]:
        return _session_detail(store, sid)

    @app.get("/api/sessions/{sid}/samples")
    def samples(sid: str) -> dict[str, Any]:
        return _series(store, sid)

    @app.get("/api/sessions/{sid}/findings")
    def findings(sid: str) -> list[dict[str, Any]]:
        return [f.model_dump() for f in analyze_session(store, sid, manufacturer_status=None)]

    @app.post("/api/sessions/{sid}/annotations")
    def annotate(sid: str, body: AnnotationBody) -> dict[str, Any]:
        # always origin "manual": a person cannot post an observed/inferred event
        return wb.annotate(sid, body.label, body.note, body.event_type).model_dump()

    @app.get("/api/sessions/{sid}/monitoring")
    def monitoring(sid: str) -> dict[str, Any]:
        if store.get_session(sid) is None:
            raise KeyError(f"no session {sid}")
        return summary_for(store, sid)

    @app.put("/api/sessions/{sid}/drive-type")
    def drive_type(sid: str, body: DriveTypeBody) -> dict[str, Any]:
        return wb.set_drive_type(sid, body.drive_type)

    @app.get("/api/monitoring/history")
    def monitoring_history(vehicle: str | None = None) -> dict[str, Any]:
        # real and synthetic sessions (and different confirmed vehicles) never share a baseline
        return history_for(store, vehicle or wb.profile.id)

    @app.get("/api/sessions/{sid}/export")
    def export(sid: str, include_vin: bool = False) -> Response:
        data = build_zip(store, sid, include_vin)
        return Response(
            data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="mini-obd-{sid}.zip"'},
        )

    @app.get("/api/compare")
    def compare(a: str, b: str) -> dict[str, Any]:
        da, db = _session_detail(store, a), _session_detail(store, b)

        def codes(d: dict[str, Any]) -> set[str]:
            return {f"{x['code']} ({x['kind']})" for sc in d["scans"] for x in sc["dtcs"]}

        ca, cb = codes(da), codes(db)
        return {
            "a": da,
            "b": db,
            "series_a": _series(store, a),
            "series_b": _series(store, b),
            "dtc_diff": {"only_a": sorted(ca - cb), "only_b": sorted(cb - ca), "both": sorted(ca & cb)},
            "same_vehicle_profile": da["vehicle_profile"] == db["vehicle_profile"],
            "mixed_synthetic_and_real": da["synthetic"] != db["synthetic"],
        }

    # -- replay -------------------------------------------------------------------
    @app.get("/api/fixtures")
    def fixtures() -> list[dict[str, Any]]:
        d = config.fixtures_dir()
        return (
            [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(d.glob("*.jsonl"))]
            if d.is_dir()
            else []
        )

    @app.post("/api/replay")
    def replay(body: ReplayBody) -> dict[str, Any]:
        name = Path(body.fixture).name  # no directories: only files inside the fixtures folder
        path = config.fixtures_dir() / name
        if name != body.fixture or not path.is_file() or path.suffix != ".jsonl":
            raise KeyError(f"no fixture named {body.fixture!r}")
        return _session_detail(store, ingest_file(path, store))

    # -- live stream ----------------------------------------------------------------
    @app.websocket("/ws/live")
    async def live(ws: WebSocket) -> None:
        await ws.accept()
        q = wb.hub.subscribe()
        try:
            await ws.send_json({"type": "status", "status": wb.status()})
            while True:
                events = []
                try:
                    while len(events) < 200:
                        events.append(q.get_nowait())
                except queue.Empty:
                    pass
                if events:
                    await ws.send_json({"type": "batch", "events": events})
                await asyncio.sleep(0.1)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            wb.hub.unsubscribe(q)

    @app.api_route(
        "/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
    )
    def no_such_endpoint(rest: str, request: Request) -> JSONResponse:
        return JSONResponse({"detail": f"no such endpoint: {request.method} /api/{rest}"}, status_code=404)

    dist = config.frontend_dist()
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")
    return app
