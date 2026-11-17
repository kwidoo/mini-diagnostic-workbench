"""Typed domain models shared by the CLI, API, storage and analysis layers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Quality = Literal[
    "ok",  # decoded value from a well-formed response
    "raw_only",  # ECU answered, but no verified decoder exists: raw bytes kept, no value
    "no_data",  # adapter said NO DATA (ECU did not answer this request)
    "negative",  # ECU answered with a negative response (7F ..)
    "timeout",  # no prompt before the deadline
    "disconnected",  # transport dropped
    "error",  # malformed / unparseable / adapter error
]

EvidenceStrength = Literal["insufficient", "suggestive", "strong"]
DtcKind = Literal["stored", "pending", "permanent"]


class Exchange(BaseModel):
    """One request/response pair exactly as seen on the adapter link."""

    seq: int = 0
    t_wall: float
    t_mono: float
    request: str
    request_hex: str
    response_hex: str
    response_text: str
    latency_ms: float
    # desync: unexpected bytes before the request or after the prompt; the reply is
    # kept raw but never decoded into a value, and the connection is discarded
    status: Literal["ok", "timeout", "partial", "closed", "cancelled", "desync", "redacted"]
    phase: str = ""  # init | discover | scan:<phase> | record | inventory (set by the workbench)


class AdapterInfo(BaseModel):
    identity: str | None = None  # ATI
    device_description: str | None = None  # AT@1
    voltage: str | None = None  # ATRV (adapter-measured, not an ECU value)
    protocol_number: str | None = None  # ATDPN
    protocol_name: str | None = None  # ATDP
    is_can: bool = False
    is_29bit: bool | None = None
    init_log: list[dict[str, str]] = Field(default_factory=list)


class EcuCapabilities(BaseModel):
    ecu: str
    mode01_pids: list[int] = Field(default_factory=list)
    mode02_pids: list[int] = Field(default_factory=list)
    mode06_mids: list[int] = Field(default_factory=list)
    mode09_pids: list[int] = Field(default_factory=list)


class Capabilities(BaseModel):
    ecus: list[EcuCapabilities] = Field(default_factory=list)
    primary_ecu: str | None = None
    # mode -> "supported" | "no_data" | "not_probed" | "error"
    modes: dict[str, str] = Field(default_factory=dict)

    def supported_mode01(self) -> set[int]:
        out: set[int] = set()
        for e in self.ecus:
            out.update(e.mode01_pids)
        return out


class Reading(BaseModel):
    mode: int = 1
    pid: int
    ecu: str | None = None
    name: str
    unit: str | None = None
    value: float | None = None
    text: str | None = None  # for non-numeric decodes
    raw_hex: str = ""
    quality: Quality = "ok"
    detail: str | None = None
    t_wall: float = 0.0
    t_mono: float = 0.0
    latency_ms: float = 0.0


class Dtc(BaseModel):
    code: str
    raw_hex: str
    kind: DtcKind
    ecu: str | None = None


class DtcScan(BaseModel):
    t_wall: float
    phase: str = "scan"  # scan | pre | post
    dtcs: list[Dtc] = Field(default_factory=list)
    # kind -> "supported" | "no_data" | "error" ; never silently empty
    mode_status: dict[str, str] = Field(default_factory=dict)
    mil_on: bool | None = None
    dtc_count_reported: int | None = None
    freeze_frame: FreezeFrame | None = None


class FreezeFrame(BaseModel):
    status: str  # "present" | "none_stored" | "no_data"
    dtc: str | None = None
    dtc_raw_hex: str | None = None
    ecu: str | None = None
    readings: list[Reading] = Field(default_factory=list)


class Annotation(BaseModel):
    t_wall: float
    t_mono: float | None = None
    label: str
    note: str = ""


class PidMetrics(BaseModel):
    pid: int
    name: str
    attempts: int
    ok: int
    dropped: int
    error_rate: float
    actual_hz: float
    latency_ms_mean: float | None
    latency_ms_p95: float | None
    latency_ms_max: float | None
    max_gap_s: float | None
    gaps: int


class Evidence(BaseModel):
    text: str
    values: dict[str, Any] = Field(default_factory=dict)


class Finding(BaseModel):
    id: str
    title: str
    hypothesis: str
    explanation: str
    strength: EvidenceStrength
    supporting: list[Evidence] = Field(default_factory=list)
    contradicting: list[Evidence] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


DtcScan.model_rebuild()
