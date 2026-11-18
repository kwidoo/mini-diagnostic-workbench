"""Decoder registry for BMW DDE responses (DDE-02 / DDE-05).

A decoder turns the DATA bytes of a positive response (after SID + echoed identifier) into
a :class:`DecodedValue`. Decoders are registered with an id and a version; a catalog entry
names both and must match exactly, so a layout change can never be applied to data
recorded with another decoder version.

No REAL decoder is registered: no response layout of this car's DDE has been verified.
Synthetic decoders (``synthetic=True``) exist only for simulator tests and are refused for
real catalog entries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class DecodeError(ValueError):
    pass


@dataclass
class DecodedValue:
    quality: str  # ok | raw_only | error
    value: float | None = None
    text: str | None = None
    unit: str | None = None
    status: dict[str, Any] | None = None
    raw_hex: str = ""
    detail: str | None = None
    decoder_version: str | None = None


@dataclass(frozen=True)
class Decoder:
    id: str
    version: str
    fn: Callable[[bytes], DecodedValue]
    synthetic: bool = False
    description: str = ""


DECODERS: dict[str, Decoder] = {}


def register(dec: Decoder) -> Decoder:
    if dec.id in DECODERS and DECODERS[dec.id] != dec:
        raise ValueError(f"decoder {dec.id} already registered with a different definition")
    DECODERS[dec.id] = dec
    return dec


def unsigned_be(data: bytes, nbytes: int, scale: float, offset: float, unit: str | None) -> DecodedValue:
    """Helper for documented layouts: big-endian unsigned integer * scale + offset."""
    if len(data) < nbytes:
        raise DecodeError(f"expected {nbytes} data bytes, got {len(data)}")
    raw = int.from_bytes(data[:nbytes], "big")
    return DecodedValue(quality="ok", value=round(raw * scale + offset, 4), unit=unit)


# -- fault-memory list decoders (DDE-04) ----------------------------------------------------


@dataclass
class FaultRecord:
    code: str
    raw_hex: str
    status_raw: str | None = None
    status: dict[str, Any] | None = None
    counter: int | None = None


@dataclass(frozen=True)
class FaultDecoder:
    """Positive fault-memory payload (SID first) -> (records, complete). ``complete`` is True only
    when the layout accounted for every byte (declared count == records, no trailing bytes)."""

    id: str
    version: str
    fn: Callable[[bytes], tuple[list[FaultRecord], bool]]
    synthetic: bool = False
    description: str = ""


FAULT_DECODERS: dict[str, FaultDecoder] = {}


def register_fault(dec: FaultDecoder) -> FaultDecoder:
    if dec.id in FAULT_DECODERS and FAULT_DECODERS[dec.id] != dec:
        raise ValueError(f"fault decoder {dec.id} already registered with a different definition")
    FAULT_DECODERS[dec.id] = dec
    return dec
