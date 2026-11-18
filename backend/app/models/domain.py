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
    # -- provenance of manufacturer readings (DDE-05); None for generic OBD / adapter readings --
    source: str | None = None  # "bmw_dde"
    signal_key: str | None = None  # stable key, e.g. "dpf_soot_mass" (never a generic PID)
    request_id: str | None = None
    decoder_version: str | None = None
    catalog_version: str | None = None
    status: dict[str, Any] | None = None  # decoded status semantics from a verified layout


#: ``Reading.mode`` of manufacturer readings: outside every OBD service number, so they can never be
#: mistaken for service 01 (mode 1) or adapter (mode 0) samples. Identify them by ``signal_key``.
MANUFACTURER_MODE = 0x100


class Dtc(BaseModel):
    code: str
    raw_hex: str
    kind: DtcKind
    ecu: str | None = None


ManufacturerFaultStatus = Literal[
    "not_attempted",  # nothing was sent (no gate pass, no connection, link not initialised)
    "unavailable",  # no executable fault-memory entry for this car / adapter / opt-in
    "unsupported_adapter",  # the adapter cannot provide the documented addressing
    "read",  # complete, successful read with at least one fault
    "no_faults",  # complete, successful read that listed no fault ("no faults found")
    "partial",  # positive answer whose layout did not fully decode: codes kept, completeness unknown
    "negative",  # the ECU answered with a negative response (NRC kept)
    "no_data",
    "timeout",
    "disconnected",
    "error",
]


class ManufacturerFault(BaseModel):
    """One manufacturer fault-memory record exactly as returned. ``status`` is only filled from a
    verified (or, in the simulator, synthetic) layout; otherwise just ``status_raw``."""

    code: str  # manufacturer code exactly as returned (hex), e.g. "4862" - never mapped to a P-code
    raw_hex: str
    status_raw: str | None = None
    status: dict[str, Any] | None = None
    counter: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ManufacturerFaultRead(BaseModel):
    origin: Literal["bmw_dde"] = "bmw_dde"
    status: ManufacturerFaultStatus
    detail: str | None = None
    synthetic: bool = False
    request_id: str | None = None
    catalog_version: str | None = None
    decoder_id: str | None = None
    decoder_version: str | None = None
    ecu: str | None = None
    raw_hex: str = ""
    nrc: str | None = None
    t_wall: float | None = None
    exchange_seqs: list[int] = Field(default_factory=list)
    faults: list[ManufacturerFault] = Field(default_factory=list)


class DtcScan(BaseModel):
    t_wall: float
    phase: str = "scan"  # scan | pre | post
    #: origin of ``dtcs`` / ``mode_status`` / ``freeze_frame``; older stored scans default to generic
    origin: Literal["generic_obd"] = "generic_obd"
    dtcs: list[Dtc] = Field(default_factory=list)
    # kind -> "supported" | "no_data" | "error" ; never silently empty
    mode_status: dict[str, str] = Field(default_factory=dict)
    mil_on: bool | None = None
    dtc_count_reported: int | None = None
    freeze_frame: FreezeFrame | None = None
    #: BMW DDE fault memory read at the same time (DDE-04). None: not part of this scan (older
    #: sessions, other vehicle profiles). Kept separate: never merged into ``dtcs``.
    manufacturer: ManufacturerFaultRead | None = None


class FreezeFrame(BaseModel):
    status: str  # "present" | "none_stored" | "no_data"
    dtc: str | None = None
    dtc_raw_hex: str | None = None
    ecu: str | None = None
    readings: list[Reading] = Field(default_factory=list)


EventOrigin = Literal[
    "manual",  # entered by a person (button, CLI); what they saw/heard, not a measurement
    "observed",  # derived directly from a measured transition (e.g. RPM went from running to 0)
    "inferred",  # heuristic interpretation of indirect evidence; never a confirmation
]


class Annotation(BaseModel):
    """A point on the session timeline: a free-text marker, a predefined event or a link event.

    Older sessions only have ``label`` / ``note`` (origin defaults to ``manual``).
    """

    t_wall: float
    t_mono: float | None = None
    label: str
    note: str = ""
    event_type: str | None = None  # predefined event id (e.g. fan_running), None for free text
    origin: EventOrigin = "manual"
    confidence: str | None = None  # e.g. "measured", "low", "moderate"; None for manual markers
    evidence: dict[str, Any] = Field(default_factory=dict)


class PidMetrics(BaseModel):
    pid: int
    mode: int = 1  # 1 = OBD service 01; 0 = adapter-measured (ATRV), not an ECU value
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
