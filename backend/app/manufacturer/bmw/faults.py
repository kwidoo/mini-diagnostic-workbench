"""DDE-04: BMW DDE fault-memory read, attached to the existing scans (never a separate workflow).

The read runs only at explicitly defined, infrequent moments - the MINI ``scan`` and the
optional recording pre / post scans - never in the polling loop. It never clears anything.

Every outcome has an explicit status (see ``ManufacturerFaultStatus``). ``no_faults`` is
reported only after a complete, successful read whose layout accounted for every byte. With
no executable catalog entry (today, for the real car) the status is ``unavailable`` and NO
request is sent; the generic DTCs of the same scan are unaffected.
"""

from __future__ import annotations

from typing import Any

from app.manufacturer.bmw.decoders import FAULT_DECODERS, DecodeError
from app.models.domain import ManufacturerFault, ManufacturerFaultRead

PASS_THROUGH = (
    "negative",
    "no_data",
    "timeout",
    "disconnected",
    "error",
    "unsupported_adapter",
    "not_attempted",
)


def read_fault_memory(
    runtime: Any | None, why_none: str = "no connection / no DDE on this profile"
) -> ManufacturerFaultRead:
    if runtime is None:
        return ManufacturerFaultRead(status="not_attempted", detail=why_none)
    synthetic = bool(runtime.ctx.synthetic_session)
    entries = runtime.executable("fault_memory")
    if not entries:
        if runtime.unsupported:
            return ManufacturerFaultRead(
                status="unsupported_adapter", detail=runtime.unsupported, synthetic=synthetic
            )
        avail = next(
            (a for a in runtime.catalog.availability(runtime.ctx) if a["metric"] == "dde_fault_memory"), None
        )
        reasons = (avail or {}).get("reasons") or ["no fault-memory entry in the catalog"]
        return ManufacturerFaultRead(
            status="unavailable",
            detail="; ".join(reasons[:3]),
            synthetic=synthetic,
            catalog_version=runtime.catalog.version,
        )
    e = entries[0]
    r = runtime.read(e.request_id)
    base: dict[str, Any] = dict(
        synthetic=synthetic,
        request_id=e.request_id,
        catalog_version=r.catalog_version,
        decoder_id=e.response.decoder_id if e.response else None,
        ecu=r.ecu,
        raw_hex=r.raw_hex,
        nrc=r.nrc,
        t_wall=r.t_wall,
        exchange_seqs=r.exchange_seqs,
    )
    if r.quality in PASS_THROUGH or r.payload is None:
        status = r.quality if r.quality in PASS_THROUGH else "error"
        return ManufacturerFaultRead(status=status, detail=r.detail, **base)  # type: ignore[arg-type]
    dec = FAULT_DECODERS[e.response.decoder_id]  # type: ignore[union-attr]
    base["decoder_version"] = dec.version
    try:
        records, complete = dec.fn(r.payload)
    except DecodeError as exc:
        return ManufacturerFaultRead(status="error", detail=f"fault list layout: {exc}", **base)
    faults = [
        ManufacturerFault(
            code=x.code, raw_hex=x.raw_hex, status_raw=x.status_raw, status=x.status, counter=x.counter
        )
        for x in records
    ]
    if not complete:
        return ManufacturerFaultRead(
            status="partial",
            detail="the answer did not decode completely; listed codes are kept, others may be missing",
            faults=faults,
            **base,
        )
    return ManufacturerFaultRead(status="read" if faults else "no_faults", faults=faults, **base)
