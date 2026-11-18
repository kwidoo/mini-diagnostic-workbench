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
