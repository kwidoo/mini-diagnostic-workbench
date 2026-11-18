"""SYNTHETIC catalog + decoders: exercise the gated DDE path against the simulator ONLY.

Everything here is invented. The CAN ids, extended address bytes, identifiers, layouts and
scalings do not describe any BMW / MINI ECU. They exist so that the catalog gate, the
guarded transport, fault-memory and live-value handling and the monitoring integration can
be tested end to end. :func:`app.manufacturer.bmw.catalog.default_catalog` refuses these
entries, and :meth:`RequestCatalog.decide` refuses them outside a synthetic session.
"""

from __future__ import annotations

from app.manufacturer.bmw.catalog import (
    Addressing,
    CatalogEntry,
    FlowControl,
    RequestCatalog,
    ResponseLayout,
    Source,
)
from app.manufacturer.bmw.decoders import DecodedValue, DecodeError, Decoder, register, unsigned_be

SYNTHETIC_VARIANT = "SYNTHETIC-DDE-0"
SYNTHETIC_CATALOG_VERSION = "dde-catalog/synthetic-1"

SYNTHETIC_SOURCE = Source(
    "synthetic_simulator",
    "synthetic",
    "Invented layouts served by the built-in fake ELM327",
    "backend/app/adapters/simulator.py",
    "synthetic-1",
    note="Not a BMW definition. Simulator only.",
)

#: invented addressing (deliberately not any known BMW id)
SYN_ADDR = Addressing(
    bus="can_11bit_500k",
    framing="iso15765_extended",
    tx_can_id="7B0",
    rx_can_id="7B8",
    tx_ext_address="5A",
    rx_ext_address="F5",
    flow_control=FlowControl(header="7B0", data="5A300000", mode=1),
)


def _u16(scale: float):
    return lambda data: unsigned_be(data, 2, scale, 0.0, None)


def _flag(data: bytes) -> DecodedValue:
    if len(data) < 1:
        raise DecodeError("expected 1 status byte")
    if data[0] & 0xFE:
        raise DecodeError(f"undefined status bits set: {data[0]:02X}")
    active = bool(data[0] & 0x01)
    return DecodedValue(quality="ok", value=1.0 if active else 0.0, status={"active": active})


def _ascii(data: bytes) -> DecodedValue:
    return DecodedValue(quality="ok", text=data.replace(b"\x00", b"").decode("ascii", "replace").strip())


SYN_DECODERS = {
    d.id: register(d)
    for d in (
        Decoder("synthetic.u16_0p1", "1", _u16(0.1), True, "u16 big-endian x 0.1"),
        Decoder("synthetic.u16_0p01", "1", _u16(0.01), True, "u16 big-endian x 0.01"),
        Decoder("synthetic.flag", "1", _flag, True, "bit 0 = active, other bits must be 0"),
        Decoder("synthetic.ascii", "1", _ascii, True, "ASCII text"),
    )
}

#: metric -> (identifier hex, decoder, unit, valid range, interval s, priority)
SYN_LIVE: dict[str, tuple[str, str, str | None, tuple[float, float] | None, float, str]] = {
    "dpf_regeneration_active": ("F101", "synthetic.flag", None, None, 5.0, "P0"),
    "dpf_soot_mass": ("F102", "synthetic.u16_0p01", "g", (0.0, 100.0), 10.0, "P0"),
    "dpf_diff_pressure": ("F103", "synthetic.u16_0p1", "hPa", (0.0, 1000.0), 2.0, "P0"),
    "exhaust_temperature_pre_dpf": ("F104", "synthetic.u16_0p1", "degC", (-40.0, 1000.0), 2.0, "P0"),
    "egr_commanded": ("F105", "synthetic.u16_0p1", "%", (0.0, 100.0), 1.0, "P0"),
    "egr_actual": ("F106", "synthetic.u16_0p1", "%", (0.0, 100.0), 1.0, "P0"),
    "air_mass_target": ("F107", "synthetic.u16_0p1", "mg/stroke", (0.0, 2000.0), 1.0, "P0"),
    "air_mass_actual": ("F108", "synthetic.u16_0p1", "mg/stroke", (0.0, 2000.0), 1.0, "P0"),
}


def _entry(metric: str, did: str, decoder: str, unit, rng, interval: float, prio: str) -> CatalogEntry:
    return CatalogEntry(
        request_id=f"syn.{metric}",
        metric=metric,
        title=f"SYNTHETIC {metric}",
        priority=prio,  # type: ignore[arg-type]
        kind="live_value",
        verification_state="fixture_verified",
        risk_class="pure_read",
        source_ids=("synthetic_simulator",),
        compatible_variants=(SYNTHETIC_VARIANT,),
        service=0x22,
        payload_hex="22" + did,
        addressing=SYN_ADDR,
        response=ResponseLayout(0x62, did, 4, 6, decoder, "1", unit, rng),
        enabled=True,
        synthetic=True,
        evidence=("synthetic: served by the fake ELM327; no relation to a real DDE",),
        sample_interval_s=interval,
    )


SYN_IDENT = CatalogEntry(
    request_id="syn.identification",
    metric="dde_identification",
    title="SYNTHETIC DDE identification",
    priority="P0",
    kind="identification",
    verification_state="fixture_verified",
    risk_class="pure_read",
    source_ids=("synthetic_simulator",),
    compatible_variants=(SYNTHETIC_VARIANT,),
    service=0x22,
    payload_hex="22F100",
    addressing=SYN_ADDR,
    response=ResponseLayout(0x62, "F100", 4, 40, "synthetic.ascii", "1"),
    enabled=True,
    synthetic=True,
    evidence=("synthetic: served by the fake ELM327",),
)

SYN_ENTRIES: tuple[CatalogEntry, ...] = (
    SYN_IDENT,
    *(_entry(m, *spec) for m, spec in SYN_LIVE.items()),
)


def synthetic_catalog(extra: tuple[CatalogEntry, ...] = ()) -> RequestCatalog:
    return RequestCatalog(
        (*SYN_ENTRIES, *extra), (SYNTHETIC_SOURCE,), version=SYNTHETIC_CATALOG_VERSION, allow_synthetic=True
    )
