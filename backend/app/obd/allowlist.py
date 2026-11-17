"""Protocol-boundary allowlist.

Every byte sent to the adapter goes through :class:`AllowedCommand`. The class
validates itself on construction, so there is no way to build an instance that
carries a request outside the read-only set. Transports only accept
``AllowedCommand`` objects, never strings.

Allowed:
  * a fixed set of ELM adapter-initialisation AT commands (they configure the
    adapter's text output, never the vehicle);
  * SAE J1979 / ISO 15031-5 read-only services 01, 02, 03, 06, 07, 09, 0A.

Explicitly NOT allowed (non-exhaustive): service 04 (clear DTCs / reset
readiness), 08 (control of on-board systems), any UDS/KWP service (10, 11, 14,
19, 22, 23, 27, 28, 2E, 2F, 31, 3E, ...), ``ATSH``/``ATCRA``/``ATFC*``/``ATPP``/
``ATMA`` and every other AT command that would re-address or re-programme the
adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

READ_ONLY_MODES: frozenset[int] = frozenset({0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A})

#: Mode 09 InfoTypes that may be requested. 02 = VIN (privacy-gated in the client).
MODE09_PIDS: frozenset[int] = frozenset({0x00, 0x02, 0x04, 0x06, 0x0A})

#: Exact AT commands allowed, with what they do.
AT_COMMANDS: dict[str, str] = {
    "ATZ": "reset adapter (adapter only)",
    "ATI": "adapter identity",
    "AT@1": "adapter device description",
    "ATE0": "echo off",
    "ATL0": "linefeeds off",
    "ATS1": "spaces on",
    "ATH1": "show CAN headers (needed to tell ECUs apart)",
    "ATAT1": "adaptive timing, default mode",
    "ATSP0": "automatic protocol selection",
    "ATDP": "describe current protocol",
    "ATDPN": "describe current protocol by number",
    "ATRV": "read adapter supply voltage",
}

_HEX_RE = re.compile(r"^[0-9A-F]+$")


class ForbiddenRequestError(ValueError):
    """Raised when something tries to build a request outside the allowlist."""


def _canonical(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


@dataclass(frozen=True)
class AllowedCommand:
    text: str  # canonical: upper-case, no spaces, no terminator

    def __post_init__(self) -> None:
        canon = _canonical(self.text)
        if canon != self.text:
            raise ForbiddenRequestError(f"non-canonical command text: {self.text!r}")
        if canon.startswith("AT"):
            if canon not in AT_COMMANDS:
                raise ForbiddenRequestError(f"AT command not on the allowlist: {canon!r}")
            return
        _validate_obd(canon)

    # -- constructors -----------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> AllowedCommand:
        return cls(_canonical(text))

    @classmethod
    def at(cls, text: str) -> AllowedCommand:
        canon = _canonical(text)
        if not canon.startswith("AT"):
            raise ForbiddenRequestError(f"not an AT command: {text!r}")
        return cls(canon)

    @classmethod
    def obd(cls, mode: int, pid: int | None = None) -> AllowedCommand:
        if mode == 0x02:
            if pid is None:
                raise ForbiddenRequestError("mode 02 needs a PID")
            return cls(f"02{pid:02X}00")  # frame 00 only
        if pid is None:
            return cls(f"{mode:02X}")
        return cls(f"{mode:02X}{pid:02X}")

    # -- accessors --------------------------------------------------------
    @property
    def is_at(self) -> bool:
        return self.text.startswith("AT")

    @property
    def mode(self) -> int | None:
        return None if self.is_at else int(self.text[0:2], 16)

    @property
    def pid(self) -> int | None:
        if self.is_at or len(self.text) < 4:
            return None
        return int(self.text[2:4], 16)

    def wire(self) -> bytes:
        return self.text.encode("ascii") + b"\r"


def _validate_obd(canon: str) -> None:
    if not _HEX_RE.match(canon) or len(canon) % 2:
        raise ForbiddenRequestError(f"not a hex OBD request: {canon!r}")
    mode = int(canon[0:2], 16)
    if mode not in READ_ONLY_MODES:
        raise ForbiddenRequestError(
            f"service {mode:02X} is not a read-only OBD service and is blocked: {canon!r}"
        )
    args = canon[2:]
    if mode in (0x03, 0x07, 0x0A):
        if args:
            raise ForbiddenRequestError(f"service {mode:02X} takes no parameters: {canon!r}")
    elif mode in (0x01, 0x06):
        if len(args) != 2:
            raise ForbiddenRequestError(f"service {mode:02X} takes exactly one PID/MID: {canon!r}")
    elif mode == 0x02:
        if len(args) != 4 or args[2:] != "00":
            raise ForbiddenRequestError(f"service 02 takes one PID and frame 00: {canon!r}")
    elif mode == 0x09:
        if len(args) != 2 or int(args, 16) not in MODE09_PIDS:
            raise ForbiddenRequestError(f"service 09 InfoType not on the allowlist: {canon!r}")
