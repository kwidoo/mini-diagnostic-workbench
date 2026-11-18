"""DDE-02: evidence-traceable, disabled-by-default BMW DDE request catalog.

The catalog is a typed MANIFEST, not a list of guessed CAN ids / service bytes. An entry
says what metric it would serve, which DDE variant(s) it applies to, how it would be
framed and addressed, the exact request bytes, the expected response layout and decoder,
where all of that comes from (``source_ids``), how far it is verified, and how risky it is.

An entry can only become executable through :meth:`RequestCatalog.decide`, which requires
ALL of:

1. ``enabled`` set in a reviewed change of this file (default: False);
2. a structurally complete entry (:func:`validate_entry` finds no problem);
3. ``risk_class == "pure_read"`` and a read-only service / sub-function;
4. ``verification_state == "vehicle_verified"`` - or, for SYNTHETIC entries only, a
   synthetic (simulator) session;
5. the user's explicit opt-in for manufacturer reads on this vehicle profile;
6. a confirmed DDE variant that matches the entry exactly (and no second entry for the
   same metric also matching: ambiguity disables both);
7. the entry's decoder registered under the same id AND version;
8. on a real adapter, a DDE-01 verdict of ``manufacturer_transport_candidate``.

Anything else is reported as ``blocked`` (a prerequisite is missing) or ``unavailable``
(no entry at all), with the reasons. :meth:`RequestCatalog.interpret` never returns a
value for a non-executable entry: the bytes are kept as ``raw_only``.

Nothing here sends anything. The guarded transport (DDE-03) only accepts a request built
from an executable entry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.manufacturer.bmw.decoders import DECODERS, FAULT_DECODERS, DecodedValue, DecodeError

CATALOG_VERSION = "dde-catalog/2026-11-18.1"

VerificationState = Literal["literature_only", "fixture_verified", "vehicle_verified"]
RiskClass = Literal["pure_read", "temporary_session_state", "active_or_write"]
RequestKind = Literal["identification", "fault_memory", "live_value"]
SourceKind = Literal[
    "vehicle_trace", "factory_definition", "vetted_implementation", "literature", "synthetic"
]

#: Read-only diagnostic services an entry may use (KWP2000 / UDS). Anything else is invalid.
READ_ONLY_SERVICES: dict[int, str] = {
    0x17: "KWP2000 ReadStatusOfDiagnosticTroubleCodes",
    0x18: "KWP2000 ReadDiagnosticTroubleCodesByStatus",
    0x19: "UDS ReadDTCInformation (read sub-functions only)",
    0x1A: "KWP2000 ReadEcuIdentification",
    0x21: "KWP2000 ReadDataByLocalIdentifier",
    0x22: "ReadDataByIdentifier (KWP2000 / UDS)",
}
#: UDS 0x19 sub-functions that only report (no clearing / no state change).
UDS_19_READ_SUBFUNCTIONS = frozenset({0x01, 0x02, 0x04, 0x06, 0x0A})
#: Never executable, whatever an entry claims (clear, control, session, reset, security, link and
#: communication control, dynamic definitions, writes, actuation, routines, programming, tester present).
FORBIDDEN_SERVICES = frozenset(
    {0x04, 0x08, 0x10, 0x11, 0x14, 0x27, 0x28, 0x2C, 0x2E, 0x2F, 0x30, 0x31, 0x34, 0x35, 0x36, 0x37,
     0x38, 0x3B, 0x3D, 0x3E, 0x83, 0x84, 0x85, 0x86, 0x87}
)  # fmt: skip

_HEX = re.compile(r"^(?:[0-9A-F]{2})+$")
_CANID11 = re.compile(r"^[0-7][0-9A-F]{2}$")
_BYTE = re.compile(r"^[0-9A-F]{2}$")


@dataclass(frozen=True)
class Source:
    id: str
    kind: SourceKind
    title: str
    reference: str  # URL or repository path
    version: str = ""  # document version / commit / retrieval date
    license: str = ""
    note: str = ""


@dataclass(frozen=True)
class FlowControl:
    header: str  # CAN id the adapter sends flow-control frames on
    data: str  # flow-control frame bytes (hex), including an extended address byte if used
    mode: int  # ELM AT FC SM mode


@dataclass(frozen=True)
class Addressing:
    bus: Literal["can_11bit_500k"]
    framing: Literal["iso15765_normal", "iso15765_extended"]
    tx_can_id: str  # request CAN id, 3 hex digits
    rx_can_id: str  # response CAN id, 3 hex digits
    tx_ext_address: str | None = None  # extended addressing: target byte in requests
    rx_ext_address: str | None = None  # extended addressing: first byte of every response frame
    flow_control: FlowControl | None = None


@dataclass(frozen=True)
class ResponseLayout:
    positive_sid: int  # e.g. 0x62 for 0x22
    echo_hex: str  # bytes after the SID that must echo the request (e.g. the identifier)
    min_length: int  # full positive payload length incl. SID, minimum
    max_length: int
    decoder_id: str
    decoder_version: str
    unit: str | None = None
    valid_range: tuple[float, float] | None = None
    notes: str = ""


@dataclass(frozen=True)
class CatalogEntry:
    request_id: str
    metric: str  # stable signal key, e.g. "dpf_soot_mass"
    title: str
    priority: Literal["P0", "P1", "P2"]
    kind: RequestKind
    verification_state: VerificationState
    risk_class: RiskClass
    source_ids: tuple[str, ...] = ()
    compatible_variants: tuple[str, ...] = ()  # exact DDE variant identifiers this entry applies to
    service: int | None = None
    payload_hex: str | None = None
    addressing: Addressing | None = None
    response: ResponseLayout | None = None
    enabled: bool = False
    synthetic: bool = False
    #: evidence statements, each prefixed "vehicle:" (this car) or "literature:" / "synthetic:"
    evidence: tuple[str, ...] = ()
    blocked_reason: str = ""
    establish_by: tuple[str, ...] = ()
    sample_interval_s: float | None = None  # suggested polling interval for live values (DDE-06)
    #: what a decoded value MEANS when that is documented, e.g. "regeneration_active_flag": the decoded
    #: status {"active": bool} is the ECU's own regeneration-active flag. Only such an entry may confirm
    #: a regeneration (DDE-06); a value without documented semantics is never interpreted that way.
    semantics: str | None = None

    @property
    def complete(self) -> bool:
        return all(
            (self.service is not None, self.payload_hex, self.addressing, self.response, self.source_ids,
             self.compatible_variants)
        )  # fmt: skip


@dataclass(frozen=True)
class ExecutionContext:
    """Everything outside the catalog that decides whether an entry may run now."""

    opted_in: bool = False
    confirmed_variant: str | None = None
    adapter_verdict: str | None = None  # DDE-01 manufacturer_transport verdict
    synthetic_session: bool = False


@dataclass
class Decision:
    request_id: str
    metric: str
    executable: bool
    status: Literal["executable", "blocked"]
    reasons: list[str] = field(default_factory=list)


def validate_entry(e: CatalogEntry, sources: dict[str, Source]) -> list[str]:
    """Structural problems of one entry. An entry with problems can never execute."""
    p: list[str] = []
    if not e.request_id or not e.metric:
        p.append("request_id and metric are required")
    for sid in e.source_ids:
        if sid not in sources:
            p.append(f"unknown source_id {sid!r}")
        elif sources[sid].kind == "synthetic" and not e.synthetic:
            p.append(f"source {sid!r} is synthetic but the entry is not marked synthetic")
    if e.synthetic and any(s in sources and sources[s].kind != "synthetic" for s in e.source_ids):
        p.append("a synthetic entry must cite synthetic sources only")
    if e.payload_hex is not None:
        if not _HEX.match(e.payload_hex):
            p.append(f"payload {e.payload_hex!r} is not canonical upper-case hex")
        else:
            sid = int(e.payload_hex[:2], 16)
            if e.service is None or sid != e.service:
                p.append(f"payload service {sid:02X} does not match declared service {e.service}")
    if e.service is not None:
        if e.service in FORBIDDEN_SERVICES or e.service not in READ_ONLY_SERVICES:
            p.append(f"service {e.service:02X} is not a read-only diagnostic service")
        if e.service == 0x19 and e.payload_hex and len(e.payload_hex) >= 4:
            if int(e.payload_hex[2:4], 16) not in UDS_19_READ_SUBFUNCTIONS:
                p.append(f"UDS 19 sub-function {e.payload_hex[2:4]} is not a read-only sub-function")
    a = e.addressing
    if a is not None:
        for name in ("tx_can_id", "rx_can_id"):
            if not _CANID11.match(getattr(a, name)):
                p.append(f"addressing.{name} {getattr(a, name)!r} is not an 11-bit CAN id")
        if a.framing == "iso15765_extended":
            for name in ("tx_ext_address", "rx_ext_address"):
                v = getattr(a, name)
                if v is None or not _BYTE.match(v):
                    p.append(f"extended addressing needs addressing.{name} as one hex byte")
        elif a.tx_ext_address or a.rx_ext_address:
            p.append("normal addressing must not carry extended address bytes")
        fc = a.flow_control
        if fc is not None and (
            not _CANID11.match(fc.header) or not _HEX.match(fc.data) or fc.mode not in (0, 1, 2)
        ):
            p.append("flow_control needs an 11-bit header, hex data and mode 0..2")
    r = e.response
    if r is not None:
        if e.service is not None and r.positive_sid != e.service + 0x40:
            p.append(f"positive response SID {r.positive_sid:02X} does not match service {e.service:02X}")
        if r.echo_hex and not _HEX.match(r.echo_hex):
            p.append("response.echo_hex is not hex")
        if e.payload_hex and r.echo_hex and not e.payload_hex[2:].startswith(r.echo_hex):
            p.append("response.echo_hex must repeat the request bytes after the service")
        if r.min_length < 1 or r.max_length < r.min_length:
            p.append("response length bounds are inconsistent")
    if e.verification_state != "literature_only" and not e.complete:
        p.append(
            f"{e.verification_state} entry is incomplete (service, payload, addressing, response, "
            "sources and variants are all required)"
        )
    if e.enabled and not e.complete:
        p.append("an enabled entry must be complete")
    if not e.complete and not e.establish_by:
        p.append("an incomplete entry must say what would establish it (establish_by)")
    return p


class CatalogError(ValueError):
    pass


class RequestCatalog:
    def __init__(
        self,
        entries: Iterable[CatalogEntry],
        sources: Iterable[Source],
        version: str = CATALOG_VERSION,
        allow_synthetic: bool = False,
    ) -> None:
        self.version = version
        self.sources = {s.id: s for s in sources}
        self.entries: dict[str, CatalogEntry] = {}
        self.synthetic = allow_synthetic
        for e in entries:
            if e.synthetic and not allow_synthetic:
                raise CatalogError(f"{e.request_id}: synthetic entries are refused by this catalog")
            if e.request_id in self.entries:
                raise CatalogError(f"duplicate request_id {e.request_id}")
            self.entries[e.request_id] = e

    def problems(self) -> dict[str, list[str]]:
        return {rid: pr for rid, e in self.entries.items() if (pr := validate_entry(e, self.sources))}

    def by_metric(self, metric: str) -> list[CatalogEntry]:
        return [e for e in self.entries.values() if e.metric == metric]

    def metrics(self) -> list[str]:
        out: list[str] = []
        for e in self.entries.values():
            if e.metric not in out:
                out.append(e.metric)
        return out

    def decide(self, request_id: str, ctx: ExecutionContext) -> Decision:
        e = self.entries[request_id]
        r: list[str] = []
        if not e.enabled:
            r.append("disabled in the catalog (enabled: false)")
        r += validate_entry(e, self.sources)
        if not e.complete:
            r.append(e.blocked_reason or "entry is incomplete")
        if e.risk_class != "pure_read":
            r.append(f"risk class {e.risk_class}: only pure reads may execute")
        if e.synthetic:
            if not ctx.synthetic_session:
                r.append("synthetic entry: only allowed against the simulator")
            if e.verification_state not in ("fixture_verified", "vehicle_verified"):
                r.append("synthetic entry is not fixture-verified")
        else:
            if e.verification_state != "vehicle_verified":
                r.append(f"verification state {e.verification_state}: vehicle verification required")
            if ctx.synthetic_session:
                r.append("real (non-synthetic) entries never run against the simulator")
            elif ctx.adapter_verdict != "manufacturer_transport_candidate":
                r.append(
                    f"adapter verdict is {ctx.adapter_verdict or 'not assessed'} (DDE-01 candidate required)"
                )
        if not ctx.opted_in:
            r.append("no user opt-in for manufacturer reads on this vehicle")
        if ctx.confirmed_variant is None:
            r.append("DDE variant not confirmed")
        elif ctx.confirmed_variant not in e.compatible_variants:
            r.append(f"confirmed DDE variant {ctx.confirmed_variant!r} is not covered by this entry")
        else:
            rivals = [
                o.request_id
                for o in self.by_metric(e.metric)
                if o.request_id != e.request_id
                and ctx.confirmed_variant in o.compatible_variants
                and o.enabled
            ]
            if rivals:
                r.append(f"ambiguous: {', '.join(rivals)} also claim this metric for the same variant")
        if e.response is not None:
            registry = FAULT_DECODERS if e.kind == "fault_memory" else DECODERS
            dec = registry.get(e.response.decoder_id)
            if dec is None:
                r.append(f"decoder {e.response.decoder_id!r} is not registered")
            elif dec.version != e.response.decoder_version:
                r.append(
                    f"decoder {e.response.decoder_id} version {dec.version} != catalog {e.response.decoder_version}"
                )
            elif dec.synthetic and not e.synthetic:
                r.append("a real entry cannot use a synthetic decoder")
        r = list(dict.fromkeys(r))
        return Decision(e.request_id, e.metric, not r, "executable" if not r else "blocked", r)

    def executable(self, ctx: ExecutionContext, kind: RequestKind | None = None) -> list[CatalogEntry]:
        return [
            e
            for e in self.entries.values()
            if (kind is None or e.kind == kind) and self.decide(e.request_id, ctx).executable
        ]

    def availability(self, ctx: ExecutionContext, wanted: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Per metric: executable / blocked / unavailable with reasons (for status, UI, exports)."""
        out = []
        for metric in list(dict.fromkeys([*wanted, *self.metrics()])):
            entries = self.by_metric(metric)
            if not entries:
                out.append(
                    {
                        "metric": metric,
                        "status": "unavailable",
                        "entries": [],
                        "reasons": ["no catalog entry"],
                    }
                )
                continue
            decisions = [self.decide(e.request_id, ctx) for e in entries]
            ok = [d for d in decisions if d.executable]
            first = entries[0]
            out.append(
                {
                    "metric": metric,
                    "title": first.title,
                    "priority": first.priority,
                    "kind": first.kind,
                    "status": "executable" if ok else "blocked",
                    "entries": [
                        {
                            "request_id": d.request_id,
                            "verification_state": self.entries[d.request_id].verification_state,
                            "status": d.status,
                            "reasons": d.reasons,
                        }
                        for d in decisions
                    ],
                    "reasons": [] if ok else decisions[0].reasons,
                    "establish_by": list(first.establish_by),
                }
            )
        return out

    def interpret(self, request_id: str, payload: bytes, ctx: ExecutionContext) -> DecodedValue:
        """Decode a positive response payload (SID first). Non-executable entries never produce a
        value: the bytes are kept as raw_only. Layout violations give quality 'error'."""
        e = self.entries[request_id]
        d = self.decide(request_id, ctx)
        raw = payload.hex().upper()
        if not d.executable or e.response is None:
            return DecodedValue(quality="raw_only", raw_hex=raw, detail="; ".join(d.reasons) or "no layout")
        lay = e.response
        if not payload or payload[0] != lay.positive_sid:
            return DecodedValue(quality="error", raw_hex=raw, detail="not the expected positive response")
        if not (lay.min_length <= len(payload) <= lay.max_length):
            return DecodedValue(
                quality="error",
                raw_hex=raw,
                detail=f"length {len(payload)} outside {lay.min_length}..{lay.max_length}",
            )
        echo = bytes.fromhex(lay.echo_hex)
        if payload[1 : 1 + len(echo)] != echo:
            return DecodedValue(
                quality="error", raw_hex=raw, detail="response does not echo the request identifier"
            )
        dec = DECODERS[lay.decoder_id]
        try:
            out = dec.fn(payload[1 + len(echo) :])
        except DecodeError as exc:
            return DecodedValue(quality="error", raw_hex=raw, detail=str(exc))
        out.raw_hex = raw
        out.decoder_version = dec.version
        if lay.unit is not None:
            out.unit = lay.unit
        if out.value is not None and lay.valid_range is not None:
            lo, hi = lay.valid_range
            if not lo <= out.value <= hi:
                # plausibility is checked but never proves correctness; outside it is not a value
                return DecodedValue(
                    quality="error",
                    raw_hex=raw,
                    detail=f"value {out.value} outside the documented range {lo}..{hi}",
                )
        return out

    def describe(self, ctx: ExecutionContext) -> dict[str, Any]:
        return {
            "version": self.version,
            "synthetic": self.synthetic,
            "context": {
                "opted_in": ctx.opted_in,
                "confirmed_variant": ctx.confirmed_variant,
                "adapter_verdict": ctx.adapter_verdict,
                "synthetic_session": ctx.synthetic_session,
            },
            "availability": self.availability(ctx),
        }


# ---------------------------------------------------------------------------------------------
# The real catalog. Every wanted metric is listed; none is executable today.
# ---------------------------------------------------------------------------------------------

SOURCES: tuple[Source, ...] = (
    Source(
        "this_vehicle_traces",
        "vehicle_trace",
        "Real generic-OBD recordings of this MINI (local database)",
        "~/.mini-obd/workbench.sqlite3 sessions 20261118-143936-c028, -160246-3cf2, -161803-c2a6",
        "2026-11-18",
        note="Generic OBD only; contains no manufacturer request or response.",
    ),
    Source(
        "ediabaslib_adapter_types",
        "literature",
        "EdiabasLib: supported adapter types",
        "https://github.com/uholeschak/ediabaslib/blob/master/docs/AdapterTypes.md",
        "retrieved 2026-11-18",
        "GPL-3.0 (project)",
        "ELM327: BMW-FAST over D-CAN only (BMW from 3/2007); recommended ELM327 1.4b / 1.5 / original 2.1.",
    ),
    Source(
        "ediabaslib_source",
        "vetted_implementation",
        "EdiabasLib source code (BMW-FAST / D-CAN implementation)",
        "https://github.com/uholeschak/ediabaslib",
        "not yet reviewed for this project",
        "GPL-3.0",
        "Candidate reference for D-CAN framing / addressing. Not reviewed; nothing copied from it.",
    ),
    Source(
        "elm327_at_commands",
        "literature",
        "Elm Electronics: ELM327 AT commands",
        "https://cdn.sparkfun.com/assets/4/e/5/0/2/ELM327_AT_Commands.pdf",
        "October 2010",
        note="AT CEA (v1.4), AT CRA reset (v1.4b), AT FC SH/SD/SM (v1.1), AT SH (v1.0).",
    ),
    Source(
        "bmw_sib_e70_4862",
        "literature",
        "BMW SIB MC-10147607-9999 (E70 X5 xDrive35d), via NHTSA",
        "https://static.nhtsa.gov/odi/tsbs/2015/MC-10147607-9999.pdf",
        note="Describes fault 4862 for a DIFFERENT vehicle / engine; context only.",
    ),
)

_FAULT_ESTABLISH = (
    "identify the DDE variant from the car (BMW identification job read with a BMW-capable tester, or a "
    "verified identification entry in this catalog)",
    "obtain the variant's documented fault-memory job: service, payload, response layout, status-byte meaning "
    "(factory job definition where rights permit, or a vetted implementation such as EdiabasLib, cited by "
    "version) - without copying restricted definition files into this repository",
    "record a read-only trace of that job on THIS car with a reference BMW tool next to the same adapter "
    "session, and store it as a fixture",
    "DDE-01 verdict manufacturer_transport_candidate for this adapter, then a stationary test (DDE-03)",
)


def _live(
    metric: str, title: str, priority: str, extra: str = "", interval: float | None = None
) -> CatalogEntry:
    return CatalogEntry(
        request_id=f"dde.{metric}.unverified",
        metric=metric,
        title=title,
        priority=priority,  # type: ignore[arg-type]
        kind="live_value",
        verification_state="literature_only",
        risk_class="pure_read",
        blocked_reason="no documented, variant-matched request / response layout / scaling" + extra,
        establish_by=(
            "identify the DDE variant (see dde_identification)",
            "a documented read-only identifier for this value on that variant, with byte layout, signedness, "
            "endianness, scaling, unit, valid range and status semantics, from a citable source",
            "a recorded trace on this car compared against a reference BMW tool showing the same physical value",
            "golden decoder tests from that trace (DDE-05)",
        ),
        evidence=(
            "vehicle: not offered through generic OBD (generic PIDs are separate and stay separate)",
            "literature: no authoritative definition reviewed for this project",
        ),
        sample_interval_s=interval,
    )


ENTRIES: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        request_id="dde.identification.unverified",
        metric="dde_identification",
        title="DDE identification / software variant",
        priority="P0",
        kind="identification",
        verification_state="literature_only",
        risk_class="pure_read",
        source_ids=("this_vehicle_traces", "ediabaslib_source"),
        blocked_reason="addressing and identification request for this DDE not documented from a reviewed source",
        establish_by=(
            "review the D-CAN framing / addressing in a vetted implementation (EdiabasLib, cited by commit) or "
            "factory documentation; record whether extended addressing and which flow-control setup is needed",
            "confirm with a BMW-capable tester which DDE variant and software level this car has",
            "capture a read-only identification exchange from the car next to that tester; add as fixture",
        ),
        evidence=(
            "vehicle: generic OBD shows ECUs 7E8 (engine ECU candidate) and 7E9 on CAN 11/500 (2026-11-18)",
            "vehicle: service 09 calibration ID / ECU name not yet read (DDE-01 on-car run pending)",
            "literature: EdiabasLib documents ELM327 support for BMW-FAST over D-CAN; framing not yet reviewed",
        ),
    ),
    CatalogEntry(
        request_id="dde.fault_memory.unverified",
        metric="dde_fault_memory",
        title="BMW DDE fault memory (incl. 4862 / 480A) with status, counter, environment",
        priority="P0",
        kind="fault_memory",
        verification_state="literature_only",
        risk_class="pure_read",
        source_ids=("bmw_sib_e70_4862",),
        blocked_reason="no documented fault-memory job for this DDE variant",
        establish_by=_FAULT_ESTABLISH,
        evidence=(
            "vehicle: owner reports BMW codes 4862 and 480A (history, not read by this tool)",
            "vehicle: generic stored P115D, MIL on (2026-11-18) - a different, generic code space",
            "literature: SIB for E70 describes 4862 as air-mass / EGR plausibility on a different engine",
        ),
    ),
    _live("dpf_regeneration_active", "DPF regeneration currently active", "P0", interval=5.0),
    _live("dpf_regeneration_request", "DPF regeneration request / status", "P0", interval=10.0),
    _live("dpf_soot_mass", "DPF soot load (calculated / measured)", "P0", interval=10.0),
    _live("dpf_ash_mass", "DPF ash load", "P1", interval=60.0),
    _live("dpf_diff_pressure", "DPF differential pressure", "P0", interval=2.0),
    _live("exhaust_temperature_pre_dpf", "Exhaust temperature before DPF / catalyst", "P0", interval=2.0),
    _live("exhaust_backpressure", "Exhaust back-pressure", "P1", interval=5.0),
    _live("dpf_last_regeneration", "Distance / time since last successful regeneration", "P1", interval=60.0),
    _live("egr_commanded", "EGR commanded (manufacturer value)", "P0", interval=1.0),
    _live("egr_actual", "EGR actual position / rate (manufacturer value)", "P0", interval=1.0),
    _live("air_mass_target", "Air mass target", "P0", interval=1.0),
    _live("air_mass_actual", "Air mass actual (manufacturer value)", "P0", interval=1.0),
    _live("intake_pressure_mfr", "Intake / charge pressure (manufacturer value)", "P1", interval=1.0),
    _live("boost_commanded", "Boost pressure commanded", "P1", interval=1.0),
    _live("boost_actual", "Boost pressure actual (manufacturer value)", "P1", interval=1.0),
    _live("rail_pressure_commanded", "Rail pressure commanded", "P1", interval=2.0),
    _live("rail_pressure_actual_mfr", "Rail pressure actual (manufacturer value)", "P1", interval=2.0),
    _live(
        "injector_corrections",
        "Injector quantity corrections (per cylinder)",
        "P2",
        extra="; injector corrections are only added when clearly documented",
        interval=30.0,
    ),
)

#: Metrics DDE-02 was asked to cover, in priority order.
WANTED_METRICS: tuple[str, ...] = tuple(e.metric for e in ENTRIES)


def default_catalog() -> RequestCatalog:
    """The real catalog: synthetic entries are refused; nothing is executable today."""
    return RequestCatalog(ENTRIES, SOURCES)
