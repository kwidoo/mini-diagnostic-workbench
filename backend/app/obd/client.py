"""Generic OBD client: safe adapter init, capability discovery, read-only services.

The client never builds a request string by hand; everything goes through
``AllowedCommand`` (see allowlist.py). There is deliberately no method that
takes an arbitrary command.
"""

from __future__ import annotations

import re
import threading

from app.adapters.base import Transport
from app.models.domain import (
    AdapterInfo,
    Capabilities,
    Dtc,
    DtcScan,
    EcuCapabilities,
    Exchange,
    FreezeFrame,
    Reading,
)
from app.obd.allowlist import AllowedCommand
from app.obd.elm import (
    PROTOCOL_NAMES,
    EcuMessage,
    ElmReply,
    ElmStatus,
    decode_reply,
    parse_reply,
    protocol_family,
)
from app.parameters.standard_pids import (
    BITMAP_PIDS,
    PID_MONITOR_STATUS,
    PIDS,
    decode_dtc,
    decode_monitor_status,
    decode_supported_bitmap,
    pid_name,
)

DTC_MODES = {"stored": 0x03, "pending": 0x07, "permanent": 0x0A}

INIT_SEQUENCE = ["ATE0", "ATL0", "ATS1", "ATH1", "ATAT1", "ATSP0"]
INIT_REQUIRED = {"ATE0", "ATH1", "ATSP0"}


class ObdError(Exception):
    pass


class AdapterInitError(ObdError):
    pass


class EcuNotRespondingError(ObdError):
    pass


class UnsupportedProtocolError(ObdError):
    pass


Decoded = tuple[Exchange, ElmReply, list[EcuMessage], list[str]]


def decode_exchange(ex: Exchange, family: str, is_29bit: bool | None) -> Decoded:
    """Raw exchange -> decoded messages. Only a cleanly completed exchange can yield data.

    A reply that ended without a clean prompt (timeout, cancel, connection loss)
    or with unexpected bytes around it (desync) is ambiguous: its raw bytes are
    kept, but the parsed status is forced to non-OK so no value is ever decoded.
    """
    reply = parse_reply(bytes.fromhex(ex.response_hex), ex.request, ex.status in ("timeout", "cancelled"))
    if ex.status != "ok" and reply.status == ElmStatus.OK:
        reply.status = ElmStatus.TIMEOUT if ex.status in ("timeout", "cancelled") else ElmStatus.PARTIAL
        reply.notes.append(f"exchange status {ex.status}: reply not trusted")
    msgs, problems = decode_reply(reply, family, is_29bit)
    return ex, reply, msgs, problems


def _positive(msgs: list[EcuMessage], prefix: bytes) -> list[EcuMessage]:
    return [m for m in msgs if m.complete and m.payload.startswith(prefix)]


def _failure_quality(
    ex: Exchange, reply: ElmReply, msgs: list[EcuMessage], problems: list[str]
) -> tuple[str, str]:
    if ex.status == "closed":
        return "disconnected", "connection dropped"
    if ex.status == "desync":
        return (
            "error",
            "unexpected bytes on the adapter link (out-of-sequence reply); not trusted, link resynchronised",
        )
    if ex.status in ("timeout", "cancelled") or reply.status == ElmStatus.TIMEOUT:
        return "timeout", f"no prompt within deadline ({ex.status})"
    if reply.status == ElmStatus.NO_DATA:
        return "no_data", "adapter reported NO DATA"
    neg = [m for m in msgs if m.is_negative]
    if neg:
        return "negative", f"negative response from {neg[0].ecu}: NRC {neg[0].payload[2]:02X}"
    detail = "; ".join(problems + reply.notes) or f"adapter status {reply.status.value}"
    return "error", detail


def interpret_pid(mode: int, pid: int, decoded: Decoded) -> list[Reading]:
    """Service 01 / 02 response -> one Reading per responding ECU (never an invented value)."""
    ex, reply, msgs, problems = decoded
    base = dict(
        mode=mode, pid=pid, name=pid_name(pid), t_wall=ex.t_wall, t_mono=ex.t_mono, latency_ms=ex.latency_ms
    )
    spec = PIDS.get(pid)
    if spec:
        base["unit"] = spec.unit
    prefix = bytes([mode + 0x40, pid]) + (b"\x00" if mode == 2 else b"")
    good = _positive(msgs, prefix) if reply.status == ElmStatus.OK else []
    if not good:
        quality, detail = _failure_quality(ex, reply, msgs, problems)
        return [Reading(**base, quality=quality, detail=detail)]  # type: ignore[arg-type]
    out: list[Reading] = []
    for m in good:
        data = m.payload[len(prefix) :]
        r = Reading(**base, ecu=m.ecu, raw_hex=data.hex().upper())
        if spec:
            if len(data) >= spec.nbytes:
                r.value = round(spec.decode(data), 4)
            else:
                r.quality, r.detail = "error", f"expected {spec.nbytes} data bytes, got {len(data)}"
        elif pid == PID_MONITOR_STATUS and len(data) >= 4:
            st = decode_monitor_status(data)
            r.value, r.unit = float(st["dtc_count"]), "DTCs"  # type: ignore[arg-type]
            r.text = f"MIL {'ON' if st['mil_on'] else 'off'}, {st['dtc_count']} emission DTC(s) reported"
        elif pid in BITMAP_PIDS and len(data) >= 4:
            r.quality = "raw_only"
            r.text = "supported-PID bitmap"
        else:
            r.quality = "raw_only"
            r.detail = "ECU answered; no verified decoder in this tool, raw bytes kept"
        out.append(r)
    return out


def interpret_dtcs(kind: str, decoded: Decoded, family: str) -> tuple[list[Dtc], str]:
    """Service 03/07/0A response -> (DTCs, mode status)."""
    ex, reply, msgs, problems = decoded
    mode = DTC_MODES[kind]
    good = _positive(msgs, bytes([mode + 0x40])) if reply.status == ElmStatus.OK else []
    if not good:
        quality, detail = _failure_quality(ex, reply, msgs, problems)
        return [], quality if quality == "no_data" else f"{quality}: {detail}"
    dtcs: list[Dtc] = []
    for m in good:
        body = m.payload[1:]
        if family == "can":
            count, body = (body[0], body[1:]) if body else (0, b"")
            body = body[: 2 * count]
        for i in range(0, len(body) - 1, 2):
            b1, b2 = body[i], body[i + 1]
            if b1 == 0 and b2 == 0:
                continue
            dtcs.append(Dtc(code=decode_dtc(b1, b2), raw_hex=f"{b1:02X}{b2:02X}", kind=kind, ecu=m.ecu))  # type: ignore[arg-type]
    return dtcs, "supported"


class ObdClient:
    def __init__(self, transport: Transport, allow_vin: bool = False) -> None:
        self.transport = transport
        self.allow_vin = allow_vin
        self.adapter = AdapterInfo()
        self.capabilities: Capabilities | None = None
        self.family = "unsupported"
        self._gen: int | None = None
        self._initialised_once = False  # a failed re-init is retried on the next query
        # one logical client operation (re-init + request) at a time; re-entrant for initialize()
        self._lock = threading.RLock()

    # -- adapter ---------------------------------------------------------------
    def _at(self, text: str, timeout_s: float = 3.0) -> ElmReply:
        cmd = AllowedCommand.at(text)
        ex = self.transport.send(cmd, timeout_s)
        _, reply, _, _ = decode_exchange(ex, "unsupported", None)
        self.adapter.init_log.append(
            {
                "command": cmd.text,
                "response": reply.text or "; ".join(reply.notes),
                "status": reply.status.value,
            }
        )
        return reply

    def initialize(self) -> AdapterInfo:
        """Safe ELM init: adapter-only AT commands, then one 0100 to bring the bus up.

        Any failure leaves the connection marked unsynchronised, so the next attempt
        starts from a fresh connection instead of an unknown stream position.
        """
        with self._lock:
            self._gen = None
            try:
                return self._initialize()
            except Exception as exc:
                self.transport.invalidate(f"adapter initialisation failed: {exc}")
                raise

    def _initialize(self) -> AdapterInfo:
        self.adapter = AdapterInfo()
        if self.transport.state != "connected":
            self.transport.connect()
        z = self._at("ATZ", 6.0)
        if z.status != ElmStatus.OK or "ELM" not in z.text.upper():
            # also catches a late reply to an earlier request being read as the ATZ answer
            raise AdapterInitError(
                f"adapter did not answer ATZ with an ELM327 identity ({z.status.value}: {z.text!r}). "
                "Is this an ELM327-compatible adapter on this host/port?"
            )
        for text in INIT_SEQUENCE:
            r = self._at(text)
            if text in INIT_REQUIRED and r.text.strip().upper() != "OK":
                raise AdapterInitError(f"adapter rejected {text}: {r.text or r.status.value}")
        self.transport.mark_initialized()  # vehicle requests are allowed from here on
        self.adapter.identity = self._at("ATI").text or None
        self.adapter.device_description = self._at("AT@1").text or None
        self.adapter.voltage = self._at("ATRV").text or None

        cmd = AllowedCommand.obd(0x01, 0x00)
        ex = self.transport.send(cmd, 15.0)  # protocol search can take several seconds
        _, probe, _, _ = decode_exchange(ex, "unsupported", None)
        self.adapter.init_log.append(
            {
                "command": cmd.text,
                "response": probe.text or "; ".join(probe.notes),
                "status": probe.status.value,
            }
        )
        if probe.status != ElmStatus.OK:
            raise EcuNotRespondingError(
                f"adapter is alive but no ECU answered the generic OBD probe ({probe.status.value}"
                f"{': ' + '; '.join(probe.notes) if probe.notes else ''}). "
                "Check ignition is ON and the adapter is fully seated."
            )
        num = (self._at("ATDPN").text or "").strip().upper()
        if not re.fullmatch(r"A?[0-9A-C]", num):
            # anything else means the reply stream is out of step with our requests
            raise AdapterInitError(f"unexpected ATDPN reply {num!r}: adapter link out of sequence")
        self.adapter.protocol_number = num or None
        self.adapter.protocol_name = self._at("ATDP").text or PROTOCOL_NAMES.get(num.lstrip("A"))
        self.family = protocol_family(num)
        self.adapter.is_can = self.family == "can"
        self.adapter.is_29bit = num.lstrip("A") in ("7", "9") if self.adapter.is_can else None
        self._gen = self.transport.generation
        self._initialised_once = True
        if self.family == "unsupported":
            raise UnsupportedProtocolError(
                f"vehicle protocol {num!r} ({self.adapter.protocol_name}) is not decoded by this tool "
                "(supported: ISO 15765-4 CAN, ISO 9141-2, ISO 14230-4)."
            )
        return self.adapter

    # -- requests ----------------------------------------------------------------
    def query(self, cmd: AllowedCommand, timeout_s: float | None = None) -> Decoded:
        with self._lock:
            if not self._initialised_once:
                raise ObdError("adapter not initialised")
            # Resynchronise/reconnect FIRST, then re-initialise the adapter if the
            # connection changed, and only then send the vehicle request. A request is
            # never sent on a connection whose stream position is unknown.
            if self.transport.prepare() != self._gen:
                self.initialize()
            ex = self.transport.send(cmd, timeout_s)
            return decode_exchange(ex, self.family, self.adapter.is_29bit)

    # -- discovery ---------------------------------------------------------------
    def discover(self) -> Capabilities:
        per_ecu: dict[str, EcuCapabilities] = {}
        modes = {m: "not_probed" for m in ("01", "02", "03", "06", "07", "09", "0A")}

        def ecu(name: str) -> EcuCapabilities:
            return per_ecu.setdefault(name, EcuCapabilities(ecu=name))

        def walk(mode: int, field: str, data_offset) -> str:
            status = "no_data"
            for base in BITMAP_PIDS:
                decoded = self.query(AllowedCommand.obd(mode, base))
                ex, reply, msgs, problems = decoded
                prefix = bytes([mode + 0x40, base])
                good = _positive(msgs, prefix) if reply.status == ElmStatus.OK else []
                if not good:
                    if base == 0:
                        q, d = _failure_quality(ex, reply, msgs, problems)
                        status = q if q in ("no_data", "negative") else f"{q}: {d}"
                    break
                status = "supported"
                more = False
                for m in good:
                    data = data_offset(m.payload)
                    if len(data) < 4:
                        continue
                    pids = decode_supported_bitmap(base, data)
                    getattr(ecu(m.ecu), field).extend(p for p in pids if p not in BITMAP_PIDS)
                    more = more or (base + 0x20) in pids
                if not more:
                    break
            return status

        modes["01"] = walk(0x01, "mode01_pids", lambda p: p[2:])
        modes["02"] = walk(0x02, "mode02_pids", lambda p: p[3:])
        modes["09"] = walk(0x09, "mode09_pids", lambda p: p[2:] if len(p) == 6 else p[3:])
        if self.family == "can":
            modes["06"] = walk(0x06, "mode06_mids", lambda p: p[2:])
        else:
            modes["06"] = "not decoded on K-line by this tool"
        for e in per_ecu.values():
            e.mode02_pids = [p for p in e.mode02_pids if p != 0x02]

        ecus = sorted(per_ecu.values(), key=lambda e: e.ecu)
        primary = max(ecus, key=lambda e: len(e.mode01_pids)).ecu if ecus else None
        self.capabilities = Capabilities(ecus=ecus, primary_ecu=primary, modes=modes)
        return self.capabilities

    def _caps(self) -> Capabilities:
        return self.capabilities or self.discover()

    # -- live data -----------------------------------------------------------------
    def read_pid(self, pid: int) -> list[Reading]:
        return interpret_pid(1, pid, self.query(AllowedCommand.obd(0x01, pid)))

    def snapshot(self) -> list[Reading]:
        out: list[Reading] = []
        for pid in sorted(self._caps().supported_mode01()):
            out.extend(self.read_pid(pid))
        return out

    # -- faults --------------------------------------------------------------------
    def scan_dtcs(self, phase: str = "scan", freeze_frame: bool = True) -> DtcScan:
        caps = self._caps()
        scan = DtcScan(t_wall=self.transport.clock.wall(), phase=phase)
        status = [r for r in self.read_pid(PID_MONITOR_STATUS) if r.quality == "ok" and r.raw_hex]
        if status:
            first = [int(r.raw_hex[0:2], 16) for r in status]
            scan.mil_on = any(b & 0x80 for b in first)
            scan.dtc_count_reported = sum(b & 0x7F for b in first)
        for kind, mode in DTC_MODES.items():
            dtcs, st = interpret_dtcs(kind, self.query(AllowedCommand.obd(mode)), self.family)
            scan.dtcs.extend(dtcs)
            scan.mode_status[kind] = st
            caps.modes[f"{mode:02X}"] = st
        if freeze_frame:
            scan.freeze_frame = self.read_freeze_frame()
        return scan

    def read_freeze_frame(self) -> FreezeFrame:
        ex, reply, msgs, problems = self.query(AllowedCommand.obd(0x02, 0x02))
        good = _positive(msgs, b"\x42\x02\x00") if reply.status == ElmStatus.OK else []
        if not good:
            q, d = _failure_quality(ex, reply, msgs, problems)
            return FreezeFrame(status=q if q == "no_data" else f"{q}: {d}")
        m = next((g for g in good if g.payload[3:5] not in (b"", b"\x00\x00")), None)
        if m is None or len(m.payload) < 5:
            return FreezeFrame(status="none_stored")
        b1, b2 = m.payload[3], m.payload[4]
        ff = FreezeFrame(status="present", dtc=decode_dtc(b1, b2), dtc_raw_hex=f"{b1:02X}{b2:02X}", ecu=m.ecu)
        pids = next((e.mode02_pids for e in self._caps().ecus if e.ecu == m.ecu), [])
        for pid in pids:
            for r in interpret_pid(2, pid, self.query(AllowedCommand.obd(0x02, pid))):
                if r.ecu in (None, m.ecu):
                    ff.readings.append(r)
        return ff

    # -- vehicle information ----------------------------------------------------------
    def _mode09_text(self, infotype: int) -> list[dict[str, str]]:
        ex, reply, msgs, _ = self.query(AllowedCommand.obd(0x09, infotype))
        good = _positive(msgs, bytes([0x49, infotype])) if reply.status == ElmStatus.OK else []
        by_ecu: dict[str, bytes] = {}
        for m in good:  # CAN: one message; K-line: several numbered messages
            by_ecu[m.ecu] = by_ecu.get(m.ecu, b"") + m.payload[3:]
        return [
            {"ecu": e, "text": b.replace(b"\x00", b"").decode("latin-1").strip(), "raw_hex": b.hex().upper()}
            for e, b in by_ecu.items()
        ]

    def vehicle_info(self, include_vin: bool | None = None) -> dict[str, object]:
        """Calibration IDs always (when supported); VIN only with explicit consent."""
        want_vin = self.allow_vin if include_vin is None else include_vin
        supported = {p for e in self._caps().ecus for p in e.mode09_pids}
        info: dict[str, object] = {"vin": None, "vin_status": "not_requested", "calibration_ids": []}
        if 0x04 in supported:
            info["calibration_ids"] = self._mode09_text(0x04)
        if 0x02 not in supported:
            info["vin_status"] = "not_supported"
        elif want_vin:
            vins = self._mode09_text(0x02)
            info["vin"] = vins[0]["text"] if vins else None
            info["vin_status"] = "read" if vins else "no_data"
        return info

    def monitors(self) -> list[dict[str, str]]:
        """Service 06 results, raw. Scaling (unit-and-scaling IDs) is not decoded."""
        out: list[dict[str, str]] = []
        for e in self._caps().ecus:
            for mid in e.mode06_mids:
                ex, reply, msgs, _ = self.query(AllowedCommand.obd(0x06, mid))
                for m in _positive(msgs, bytes([0x46, mid])):
                    out.append({"ecu": m.ecu, "mid": f"{mid:02X}", "raw_hex": m.payload.hex().upper()})
        return out
