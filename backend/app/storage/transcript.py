"""Raw adapter transcript (JSONL): write from a stored session, ingest for replay.

Format ``mini-obd-transcript/1``: a header line, then one line per adapter
exchange (in order, tagged with the phase it belonged to) and per annotation.
Replay re-decodes the raw responses with the same decoders used live.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.models.domain import Annotation, DtcScan, Exchange, FreezeFrame
from app.obd.allowlist import AllowedCommand, ForbiddenRequestError
from app.obd.client import DTC_MODES, decode_exchange, interpret_dtcs, interpret_pid
from app.parameters.standard_pids import BITMAP_PIDS
from app.storage.db import Store

FORMAT = "mini-obd-transcript/1"
REDACTED = "[REDACTED]"


def transcript_lines(store: Store, sid: str, include_vin: bool = False) -> list[str]:
    session = store.get_session(sid)
    assert session is not None
    meta = session["meta"]
    header = {
        "type": "header",
        "format": FORMAT,
        "synthetic": session["synthetic"],
        "kind": session["kind"],
        "vehicle_profile": session["vehicle_profile"],
        "label": session["label"],
        "scenario": meta.get("scenario"),
        "adapter": meta.get("adapter"),
        "capabilities": meta.get("capabilities"),
        "note": "SYNTHETIC simulator data - not a measurement from a real vehicle"
        if session["synthetic"]
        else "recorded from a real adapter",
    }
    lines = [json.dumps(header, sort_keys=True)]
    for ex in store.exchanges(sid):
        if ex["request"] == "0902" and not include_vin:
            ex.update(response_hex="", response_text=REDACTED, status="redacted")
        lines.append(json.dumps({"type": "exchange", **ex}, sort_keys=True))
    for a in store.annotations(sid):
        lines.append(json.dumps({"type": "annotation", **a.model_dump()}, sort_keys=True))
    return lines


def _decode(ex: Exchange, family: str, is_29bit: bool | None):
    return decode_exchange(ex, family, is_29bit)  # same trust rules as live decoding


def ingest_transcript(
    lines: Iterable[str], store: Store, label: str | None = None, source: str = "replay"
) -> str:
    """Create a session from a transcript. Returns the new session id."""
    records = [json.loads(line) for line in lines if line.strip()]
    if not records or records[0].get("type") != "header" or records[0].get("format") != FORMAT:
        raise ValueError(f"not a {FORMAT} transcript (missing header line)")
    header = records[0]
    adapter = header.get("adapter") or {}
    family = "can" if adapter.get("is_can") else "kline"
    is_29bit = adapter.get("is_29bit")

    exchanges = [
        Exchange(**{k: v for k, v in r.items() if k != "type"}) for r in records if r["type"] == "exchange"
    ]
    annotations = [
        Annotation(**{k: v for k, v in r.items() if k != "type"})
        for r in records
        if r["type"] == "annotation"
    ]
    started = exchanges[0].t_wall if exchanges else 0.0
    ended = exchanges[-1].t_wall if exchanges else 0.0

    samples = []
    scans: dict[str, DtcScan] = {}
    skipped: list[str] = []
    for ex in exchanges:
        if ex.status == "redacted":
            continue
        try:
            cmd = AllowedCommand.parse(ex.request)
        except ForbiddenRequestError:
            skipped.append(ex.request)  # nothing is ever sent during replay; just not interpreted
            continue
        if cmd.is_at:
            continue
        mode, pid = cmd.mode, cmd.pid
        decoded = _decode(ex, family, is_29bit)
        if ex.phase.startswith("scan"):
            phase = ex.phase.partition(":")[2] or "scan"
            scan = scans.setdefault(ex.phase, DtcScan(t_wall=ex.t_wall, phase=phase))
            if mode == 1 and pid == 1:
                first = [
                    int(r.raw_hex[0:2], 16)
                    for r in interpret_pid(1, 1, decoded)
                    if r.quality == "ok" and r.raw_hex
                ]
                if first:
                    scan.mil_on = any(b & 0x80 for b in first)
                    scan.dtc_count_reported = sum(b & 0x7F for b in first)
            elif mode in DTC_MODES.values():
                kind = next(k for k, m in DTC_MODES.items() if m == mode)
                dtcs, st = interpret_dtcs(kind, decoded, family)
                scan.dtcs.extend(dtcs)
                scan.mode_status[kind] = st
            elif mode == 2 and pid == 2:
                good = [r for r in interpret_pid(2, 2, decoded) if r.raw_hex]
                hit = next((r for r in good if r.raw_hex[:4] != "0000"), None)
                if hit:
                    from app.parameters.standard_pids import decode_dtc

                    b1, b2 = int(hit.raw_hex[0:2], 16), int(hit.raw_hex[2:4], 16)
                    scan.freeze_frame = FreezeFrame(
                        status="present", dtc=decode_dtc(b1, b2), dtc_raw_hex=hit.raw_hex[:4], ecu=hit.ecu
                    )
                else:
                    scan.freeze_frame = FreezeFrame(status="none_stored" if good else "no_data")
            elif mode == 2 and scan.freeze_frame and scan.freeze_frame.status == "present":
                scan.freeze_frame.readings.extend(
                    r for r in interpret_pid(2, pid, decoded) if r.ecu in (None, scan.freeze_frame.ecu)
                )
        elif (
            ex.phase in ("record", "inventory")
            and mode == 1
            and (pid not in BITMAP_PIDS or ex.phase == "inventory")
        ):
            samples.extend(interpret_pid(1, pid, decoded))

    meta: dict[str, Any] = {
        "adapter": adapter,
        "capabilities": header.get("capabilities"),
        "scenario": header.get("scenario"),
        "replayed_from_kind": header.get("kind"),
        "replay_skipped_requests": skipped,
    }
    sid = store.create_session(
        kind=header.get("kind") or "record",
        vehicle_profile=header["vehicle_profile"],
        source=source,
        synthetic=bool(header.get("synthetic")),
        label=label or header.get("label") or "replay",
        started_at=started,
        meta=meta,
    )
    store.add_exchanges(sid, exchanges)
    store.add_samples(sid, samples)
    for scan in scans.values():
        store.add_scan(sid, scan)
    for a in annotations:
        store.add_annotation(sid, a)
    from app.acquisition.recorder import compute_metrics

    store.update_session(
        sid, ended_at=ended, meta={"metrics": [m.model_dump() for m in compute_metrics(store.samples(sid))]}
    )
    return sid


def ingest_file(path: str | Path, store: Store, label: str | None = None) -> str:
    with open(path, encoding="utf-8") as fh:
        return ingest_transcript(fh, store, label=label or Path(path).stem)
