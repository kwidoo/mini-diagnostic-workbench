"""ELM327 text framing and CAN / ISO-TP reassembly.

Assumptions (documented in docs/DIAGNOSTIC_PROTOCOL.md):
  * The adapter is initialised with ``ATH1`` (headers on) and ``ATS1`` (spaces
    on). With headers on, an ELM327 prints each received CAN frame on its own
    line as ``<ID> <PCI> <data...>`` and does NOT strip ISO-TP PCI bytes or
    join multi-frame responses. The adapter still sends the ISO-TP flow-control
    frame itself (ELM default ``CFC1``). Reassembly is therefore done here,
    per source ECU, and is strict: a sequence error yields an *incomplete*
    message rather than a silently concatenated payload.
  * Only ISO 15765-4 CAN (ELM protocols 6-9) is decoded. Other protocols are
    reported as unsupported by this tool, not guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

PROMPT = b">"

_HEX2 = re.compile(r"^[0-9A-F]{2}$")
_HEXS = re.compile(r"^[0-9A-F]+$")


class ElmStatus(StrEnum):
    OK = "ok"
    NO_DATA = "no_data"
    UNKNOWN_COMMAND = "unknown_command"  # "?"
    STOPPED = "stopped"
    UNABLE_TO_CONNECT = "unable_to_connect"
    BUS_ERROR = "bus_error"
    BUFFER_FULL = "buffer_full"
    TIMEOUT = "timeout"
    PARTIAL = "partial"
    EMPTY = "empty"


_ERROR_TOKENS: list[tuple[str, ElmStatus]] = [
    ("UNABLE TO CONNECT", ElmStatus.UNABLE_TO_CONNECT),
    ("NO DATA", ElmStatus.NO_DATA),
    ("STOPPED", ElmStatus.STOPPED),
    ("BUFFER FULL", ElmStatus.BUFFER_FULL),
    ("CAN ERROR", ElmStatus.BUS_ERROR),
    ("BUS BUSY", ElmStatus.BUS_ERROR),
    ("BUS ERROR", ElmStatus.BUS_ERROR),
    ("FB ERROR", ElmStatus.BUS_ERROR),
    ("DATA ERROR", ElmStatus.BUS_ERROR),
    ("RX ERROR", ElmStatus.BUS_ERROR),
    ("ERROR", ElmStatus.BUS_ERROR),  # e.g. "BUS INIT: ...ERROR"
]


@dataclass
class ElmReply:
    status: ElmStatus
    lines: list[str] = field(default_factory=list)  # data lines only
    notes: list[str] = field(default_factory=list)  # SEARCHING..., BUS INIT..., error tokens
    prompt_seen: bool = False

    @property
    def text(self) -> str:
        return " | ".join(self.lines)


def parse_reply(raw: bytes, request: str = "", timed_out: bool = False) -> ElmReply:
    """Normalise one raw adapter response into data lines + status."""
    text = raw.replace(b"\x00", b"").decode("latin-1")
    prompt_seen = ">" in text
    text = text.replace(">", "")
    req_canon = re.sub(r"\s+", "", request).upper()

    lines: list[str] = []
    notes: list[str] = []
    error: ElmStatus | None = None
    for part in re.split(r"[\r\n]+", text):
        line = part.strip()
        if not line:
            continue
        upper = line.upper()
        if req_canon and re.sub(r"\s+", "", upper) == req_canon:
            continue  # command echo
        if upper.startswith("SEARCHING"):
            notes.append(line)
            continue
        if upper.startswith("BUS INIT"):
            notes.append(line)
            if "ERROR" in upper:
                error = error or ElmStatus.BUS_ERROR
            continue
        if upper == "?":
            notes.append(line)
            error = error or ElmStatus.UNKNOWN_COMMAND
            continue
        matched = next((st for tok, st in _ERROR_TOKENS if tok in upper), None)
        if matched is not None:
            notes.append(line)
            error = error or matched
            continue
        lines.append(line)

    if not prompt_seen:
        status = ElmStatus.TIMEOUT if timed_out else ElmStatus.PARTIAL
    elif error is not None and not lines:
        status = error
    elif error in (ElmStatus.BUFFER_FULL, ElmStatus.STOPPED, ElmStatus.BUS_ERROR):
        status = error  # data present but the adapter flagged it as unreliable
    elif lines:
        status = ElmStatus.OK
    else:
        status = ElmStatus.EMPTY
    return ElmReply(status=status, lines=lines, notes=notes, prompt_seen=prompt_seen)


# ---------------------------------------------------------------------------
# CAN frames
# ---------------------------------------------------------------------------


class InvalidFrameError(ValueError):
    pass


@dataclass(frozen=True)
class CanFrame:
    ecu: str  # "7E8" (11-bit) or "18DAF110" (29-bit)
    data: bytes


def parse_can_line(line: str, is_29bit: bool | None = None) -> CanFrame:
    """Parse one ``ATH1`` CAN line, with or without spaces."""
    tokens = line.upper().split()
    if not tokens:
        raise InvalidFrameError("empty line")
    if len(tokens) == 1:  # ATS0 style, no spaces
        s = tokens[0]
        if not _HEXS.match(s):
            raise InvalidFrameError(f"non-hex characters in frame: {line!r}")
        if is_29bit is None:
            is_29bit = len(s) % 2 == 0  # 3+2n is odd, 8+2n is even
        idlen = 8 if is_29bit else 3
        if len(s) < idlen + 2 or (len(s) - idlen) % 2:
            raise InvalidFrameError(f"frame length does not fit a CAN header: {line!r}")
        ecu, body = s[:idlen], s[idlen:]
        data = bytes.fromhex(body)
    elif len(tokens[0]) == 3:
        if not _HEXS.match(tokens[0]) or not all(_HEX2.match(t) for t in tokens[1:]):
            raise InvalidFrameError(f"malformed 11-bit frame: {line!r}")
        ecu, data = tokens[0], bytes.fromhex("".join(tokens[1:]))
    else:
        if not all(_HEX2.match(t) for t in tokens) or len(tokens) < 5:
            raise InvalidFrameError(f"malformed frame / missing header: {line!r}")
        ecu, data = "".join(tokens[:4]), bytes.fromhex("".join(tokens[4:]))
    if not data or len(data) > 8:
        raise InvalidFrameError(f"CAN frame must carry 1..8 data bytes: {line!r}")
    return CanFrame(ecu=ecu, data=data)


@dataclass
class EcuMessage:
    ecu: str
    payload: bytes
    complete: bool = True
    problem: str | None = None

    @property
    def is_negative(self) -> bool:
        return len(self.payload) >= 3 and self.payload[0] == 0x7F


def assemble_isotp(frames: list[CanFrame]) -> list[EcuMessage]:
    """Reassemble ISO 15765-2 messages per source ECU.

    Frames of different ECUs may be interleaved; each ECU's frames are handled
    in arrival order. Nothing is concatenated across ECUs or across a sequence
    error.
    """
    messages: list[EcuMessage] = []
    open_msgs: dict[str, tuple[EcuMessage, int, int]] = {}  # ecu -> (msg, total, next_seq)

    def close_incomplete(ecu: str, why: str) -> None:
        msg, _total, _seq = open_msgs.pop(ecu)
        msg.complete = False
        msg.problem = why

    for fr in frames:
        pci = fr.data[0] >> 4
        if pci == 0x0:  # single frame
            if fr.ecu in open_msgs:
                close_incomplete(fr.ecu, "single frame arrived inside a multi-frame message")
            n = fr.data[0] & 0x0F
            body = fr.data[1 : 1 + n]
            msg = EcuMessage(ecu=fr.ecu, payload=bytes(body))
            if n == 0 or len(body) < n:
                msg.complete = False
                msg.problem = f"single frame declares {n} bytes, carries {len(body)}"
            messages.append(msg)
        elif pci == 0x1:  # first frame
            if fr.ecu in open_msgs:
                close_incomplete(fr.ecu, "new first frame before previous message finished")
            if len(fr.data) < 2:
                messages.append(EcuMessage(fr.ecu, b"", False, "truncated first frame"))
                continue
            total = ((fr.data[0] & 0x0F) << 8) | fr.data[1]
            msg = EcuMessage(ecu=fr.ecu, payload=bytes(fr.data[2:]))
            messages.append(msg)
            open_msgs[fr.ecu] = (msg, total, 1)
        elif pci == 0x2:  # consecutive frame
            if fr.ecu not in open_msgs:
                messages.append(EcuMessage(fr.ecu, b"", False, "consecutive frame without first frame"))
                continue
            msg, total, expect = open_msgs[fr.ecu]
            seq = fr.data[0] & 0x0F
            if seq != expect:
                close_incomplete(fr.ecu, f"sequence error: expected {expect:X}, got {seq:X}")
                continue
            msg.payload += bytes(fr.data[1:])
            if len(msg.payload) >= total:
                msg.payload = msg.payload[:total]
                del open_msgs[fr.ecu]
            else:
                open_msgs[fr.ecu] = (msg, total, (expect + 1) & 0x0F)
        elif pci == 0x3:  # flow control (normally not echoed) - ignore
            continue
        else:
            messages.append(EcuMessage(fr.ecu, b"", False, f"unknown PCI type {pci:X}"))

    for ecu in list(open_msgs):
        msg, total, _ = open_msgs[ecu]
        close_incomplete(ecu, f"incomplete multi-frame message: {len(msg.payload)}/{total} bytes")
    return messages


def decode_can_reply(reply: ElmReply, is_29bit: bool | None = None) -> tuple[list[EcuMessage], list[str]]:
    """Data lines -> per-ECU messages. Returns (messages, problems)."""
    frames: list[CanFrame] = []
    problems: list[str] = []
    for line in reply.lines:
        try:
            frames.append(parse_can_line(line, is_29bit))
        except InvalidFrameError as exc:
            problems.append(str(exc))
    msgs = assemble_isotp(frames)
    problems.extend(f"{m.ecu}: {m.problem}" for m in msgs if m.problem)
    return msgs, problems


# ---------------------------------------------------------------------------
# K-line (ISO 9141-2 / ISO 14230-4 KWP2000) frames
# ---------------------------------------------------------------------------


def parse_kline_line(line: str) -> EcuMessage:
    """Parse one ``ATH1`` K-line message: 3 header bytes, data, 1 checksum byte.

    ISO 9141-2 headers look like ``48 6B <src>``; KWP2000 like ``8N F1 <src>``
    (N = data length). The source address (third byte) identifies the ECU.
    The checksum (8-bit sum of all preceding bytes) is verified.
    """
    s = "".join(line.upper().split())
    if not _HEXS.match(s) or len(s) % 2:
        raise InvalidFrameError(f"malformed K-line message: {line!r}")
    raw = bytes.fromhex(s)
    if len(raw) < 5:
        raise InvalidFrameError(f"K-line message too short for header + checksum: {line!r}")
    ecu = f"{raw[2]:02X}"
    payload = raw[3:-1]
    if sum(raw[:-1]) & 0xFF != raw[-1]:
        return EcuMessage(ecu, payload, False, "checksum mismatch")
    return EcuMessage(ecu, payload)


def decode_kline_reply(reply: ElmReply) -> tuple[list[EcuMessage], list[str]]:
    msgs: list[EcuMessage] = []
    problems: list[str] = []
    for line in reply.lines:
        try:
            msgs.append(parse_kline_line(line))
        except InvalidFrameError as exc:
            problems.append(str(exc))
    problems.extend(f"{m.ecu}: {m.problem}" for m in msgs if m.problem)
    return msgs, problems


# ---------------------------------------------------------------------------
# Protocol families (by ELM protocol number, as read back with ATDPN)
# ---------------------------------------------------------------------------

PROTOCOL_NAMES = {
    "1": "SAE J1850 PWM",
    "2": "SAE J1850 VPW",
    "3": "ISO 9141-2",
    "4": "ISO 14230-4 KWP (5 baud init)",
    "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN (11 bit, 500 kbaud)",
    "7": "ISO 15765-4 CAN (29 bit, 500 kbaud)",
    "8": "ISO 15765-4 CAN (11 bit, 250 kbaud)",
    "9": "ISO 15765-4 CAN (29 bit, 250 kbaud)",
}
CAN_PROTOCOLS = {"6", "7", "8", "9"}
KLINE_PROTOCOLS = {"3", "4", "5"}


def protocol_family(number: str | None) -> str:
    """'can' | 'kline' | 'unsupported' for an ATDPN value such as 'A6' or '3'."""
    n = (number or "").upper().lstrip("A")
    if n in CAN_PROTOCOLS:
        return "can"
    if n in KLINE_PROTOCOLS:
        return "kline"
    return "unsupported"


def decode_reply(
    reply: ElmReply, family: str, is_29bit: bool | None = None
) -> tuple[list[EcuMessage], list[str]]:
    if family == "can":
        return decode_can_reply(reply, is_29bit)
    if family == "kline":
        return decode_kline_reply(reply)
    return [], [f"protocol family {family!r} is not decoded by this tool"]
