"""DDE-03: narrow, typed manufacturer-diagnostics transport path.

There is no public way to send manufacturer bytes. The only object the transport accepts
for a manufacturer request is an :class:`ApprovedRequest`, and the only way to obtain one is
:func:`approve`, which

* asks the catalog gate (:meth:`RequestCatalog.decide`) - opt-in, verification record,
  variant, decoder, adapter verdict - and refuses with the reasons otherwise;
* refuses adapters / vehicle protocols the entry's addressing does not fit
  (``UnsupportedAdapterError``): only ISO 15765-4 CAN 11-bit 500 kbit/s;
* derives the adapter set-up commands from the entry's addressing (``AT SH``, ``AT CEA``,
  ``AT CRA``, ``AT FC SH / SD / SM``), the exact request payload, and the restore sequence
  back to the generic-OBD baseline.

Every command is a :class:`GuardedCommand`, which validates itself on construction (exact
patterns for the handful of addressing AT commands; read-only services only for the
payload) and can only be constructed inside this module.

Restoring the baseline: after every transaction - success or not - the transport sends the
restore commands and checks each answers ``OK``. If that fails or the connection is lost,
the connection is invalidated: the next request of any kind first re-opens the link and
re-initialises the adapter from ``ATZ`` (which resets every ELM setting). Addressing can
therefore never leak into generic OBD requests (fail closed).

Assumption A3 (to verify on the car, see docs/DIAGNOSTIC_PROTOCOL.md): with ``ATH1`` and
``AT CEA hh`` an ELM327 prints each received frame as ``<id> <ext> <pci> <data...>``. A
reply that does not look like that is reported as an error, never decoded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.manufacturer.bmw.catalog import (
    FORBIDDEN_SERVICES,
    READ_ONLY_SERVICES,
    CatalogEntry,
    ExecutionContext,
    RequestCatalog,
)
from app.models.domain import Exchange
from app.obd.allowlist import ForbiddenRequestError
from app.obd.elm import CanFrame, ElmStatus, InvalidFrameError, assemble_isotp, parse_can_line, parse_reply

_TOKEN = object()  # only this module can build GuardedCommand instances

#: the ONLY adapter commands a manufacturer transaction may carry, as exact patterns
_AT_PATTERNS = (
    re.compile(r"ATSH[0-7][0-9A-F]{2}"),  # 11-bit request header
    re.compile(r"ATCEA(?:[0-9A-F]{2})?"),  # CAN extended address on / off
    re.compile(r"ATCRA(?:[0-7][0-9A-F]{2})?"),  # receive filter on one 11-bit id / reset
    re.compile(r"ATFCSH[0-7][0-9A-F]{2}"),  # flow-control header
    re.compile(r"ATFCSD(?:[0-9A-F]{2}){1,5}"),  # flow-control data
    re.compile(r"ATFCSM[012]"),  # flow-control mode
)
#: generic OBD baseline for ISO 15765-4 11-bit: functional header 7DF, no extended address,
#: no receive filter, adapter-generated flow control (ELM defaults after ATZ)
BASELINE_HEADER = "7DF"
GENERIC_CAN_11BIT_500K = "6"


class NotApprovedError(ForbiddenRequestError):
    def __init__(self, request_id: str, reasons: list[str]) -> None:
        super().__init__(f"{request_id}: not approved: " + "; ".join(reasons))
        self.reasons = reasons


class UnsupportedAdapterError(NotApprovedError):
    pass


@dataclass(frozen=True)
class GuardedCommand:
    text: str
    role: str  # setup | request | restore
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise ForbiddenRequestError("GuardedCommand can only be built from an approved catalog entry")
        if self.role not in ("setup", "request", "restore"):
            raise ForbiddenRequestError(f"unknown role {self.role!r}")
        if self.text.startswith("AT"):
            if self.role == "request" or not any(p.fullmatch(self.text) for p in _AT_PATTERNS):
                raise ForbiddenRequestError(
                    f"adapter command not allowed in a manufacturer transaction: {self.text}"
                )
            return
        if self.role != "request" or not re.fullmatch(r"(?:[0-9A-F]{2})+", self.text):
            raise ForbiddenRequestError(f"malformed manufacturer request {self.text!r}")
        sid = int(self.text[:2], 16)
        if sid in FORBIDDEN_SERVICES or sid not in READ_ONLY_SERVICES:
            raise ForbiddenRequestError(f"service {sid:02X} is not a read-only service")

    @property
    def is_at(self) -> bool:
        return self.text.startswith("AT")

    def wire(self) -> bytes:
        return self.text.encode("ascii") + b"\r"


def _cmd(text: str, role: str) -> GuardedCommand:
    return GuardedCommand(text, role, _TOKEN)


@dataclass(frozen=True)
class ApprovedRequest:
    entry: CatalogEntry
    catalog_version: str
    setup: tuple[GuardedCommand, ...]
    request: GuardedCommand
    restore: tuple[GuardedCommand, ...]
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise ForbiddenRequestError("ApprovedRequest can only be created by approve()")

    @property
    def request_id(self) -> str:
        return self.entry.request_id


def approve(
    catalog: RequestCatalog, request_id: str, ctx: ExecutionContext, adapter: dict[str, Any] | None
) -> ApprovedRequest:
    """Gate + adapter fit -> the one object a transport accepts for a manufacturer request."""
    if request_id not in catalog.entries:
        raise NotApprovedError(request_id, ["not in the catalog"])
    d = catalog.decide(request_id, ctx)
    if not d.executable:
        raise NotApprovedError(request_id, d.reasons)
    e = catalog.entries[request_id]
    a = e.addressing
    assert a is not None and e.payload_hex is not None  # guaranteed by decide()
    proto = ((adapter or {}).get("protocol_number") or "").upper().lstrip("A")
    if not (adapter or {}).get("is_can") or proto != GENERIC_CAN_11BIT_500K:
        raise UnsupportedAdapterError(
            request_id, [f"vehicle protocol {proto or 'unknown'} is not ISO 15765-4 CAN 11-bit 500 kbit/s"]
        )
    setup = [_cmd(f"ATSH{a.tx_can_id}", "setup")]
    restore = [_cmd(f"ATSH{BASELINE_HEADER}", "restore")]
    if a.framing == "iso15765_extended":
        setup.append(_cmd(f"ATCEA{a.tx_ext_address}", "setup"))
        restore.append(_cmd("ATCEA", "restore"))
    setup.append(_cmd(f"ATCRA{a.rx_can_id}", "setup"))
    restore.append(_cmd("ATCRA", "restore"))
    if a.flow_control is not None:
        fc = a.flow_control
        setup += [
            _cmd(f"ATFCSH{fc.header}", "setup"),
            _cmd(f"ATFCSD{fc.data}", "setup"),
            _cmd(f"ATFCSM{fc.mode}", "setup"),
        ]
        restore.append(_cmd("ATFCSM0", "restore"))
    return ApprovedRequest(
        e, catalog.version, tuple(setup), _cmd(e.payload_hex, "request"), tuple(restore), _TOKEN
    )


def at_ok(ex: Exchange) -> bool:
    """A setup / restore exchange counts only when it completed cleanly and said exactly OK."""
    if ex.status != "ok":
        return False
    reply = parse_reply(bytes.fromhex(ex.response_hex), ex.request)
    return reply.prompt_seen and reply.status == ElmStatus.OK and reply.text.strip().upper() == "OK"


# ---------------------------------------------------------------------------------------------
# Result of one manufacturer read
# ---------------------------------------------------------------------------------------------


@dataclass
class DdeResult:
    request_id: str
    metric: str
    kind: str
    # ok | raw_only | no_data | negative | timeout | disconnected | error | unsupported_adapter | not_attempted
    quality: str
    value: float | None = None
    text: str | None = None
    unit: str | None = None
    status: dict[str, Any] | None = None
    raw_hex: str = ""
    payload: bytes | None = None  # reassembled positive payload (SID first), for list decoders (faults)
    nrc: str | None = None
    ecu: str | None = None  # response CAN id (+ extended address) the answer came from
    detail: str | None = None
    t_wall: float = 0.0
    t_mono: float = 0.0
    latency_ms: float = 0.0
    catalog_version: str = ""
    decoder_version: str | None = None
    exchange_seqs: list[int] = field(default_factory=list)


def decode_request_exchange(
    ex: Exchange, approved: ApprovedRequest
) -> tuple[str, bytes | None, str | None, str | None]:
    """Request exchange -> (quality, positive payload, nrc, detail). Strict: frames from any other
    CAN id or with a wrong extended address make the whole reply untrusted."""
    a = approved.entry.addressing
    assert a is not None
    if ex.status == "closed":
        return "disconnected", None, None, "connection dropped"
    if ex.status in ("timeout", "cancelled"):
        return "timeout", None, None, f"no prompt within deadline ({ex.status})"
    if ex.status == "desync":
        return "error", None, None, "unexpected bytes on the adapter link; not trusted"
    reply = parse_reply(bytes.fromhex(ex.response_hex), ex.request)
    if reply.status == ElmStatus.NO_DATA:
        return "no_data", None, None, "adapter reported NO DATA"
    if reply.status == ElmStatus.UNKNOWN_COMMAND:
        return "unsupported_adapter", None, None, "adapter rejected the request ('?')"
    if reply.status != ElmStatus.OK:
        return "error", None, None, f"adapter status {reply.status.value}: {'; '.join(reply.notes)}"
    frames: list[CanFrame] = []
    for line in reply.lines:
        try:
            fr = parse_can_line(line, False)
        except InvalidFrameError as exc:
            return "error", None, None, f"malformed frame: {exc}"
        if fr.ecu != a.rx_can_id:
            return (
                "error",
                None,
                None,
                f"frame from unexpected CAN id {fr.ecu} (expected {a.rx_can_id}); not trusted",
            )
        if a.framing == "iso15765_extended":
            if fr.data[0] != int(a.rx_ext_address or "0", 16) or len(fr.data) < 2:
                return "error", None, None, f"frame without the expected extended address {a.rx_ext_address}"
            fr = CanFrame(fr.ecu, fr.data[1:])
        frames.append(fr)
    msgs = assemble_isotp(frames)
    if len(msgs) != 1:
        return "error", None, None, f"expected one message, got {len(msgs)}"
    m = msgs[0]
    if not m.complete:
        return "error", None, None, f"incomplete ISO-TP message: {m.problem}"
    sid = approved.entry.service
    if m.is_negative:
        if m.payload[1] != sid:
            return "error", None, None, f"negative response for another service {m.payload[1]:02X}"
        return "negative", m.payload, f"{m.payload[2]:02X}", f"negative response, NRC {m.payload[2]:02X}"
    if sid is None or not m.payload or m.payload[0] != sid + 0x40:
        return "error", None, None, "not a positive response to this request"
    return "ok", m.payload, None, None
