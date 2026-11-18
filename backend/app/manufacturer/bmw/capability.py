"""DDE capability assessment (task DDE-01).

Answers, from data the tool is ALREADY allowed to read (adapter AT identity commands,
generic OBD services 01 / 09, the raw exchange trace), how far this adapter + car
combination is from BMW manufacturer diagnostics - without sending a single
manufacturer-specific request.

Every field carries a value, a ``source`` (which exchange / command it came from) and a
``quality``:

* ``observed``      - read from the car / adapter in this session (raw evidence kept);
* ``claimed``       - a string the adapter firmware reports about itself (clones copy them);
* ``derived``       - computed from observed data by a rule stated in ``detail``;
* ``not_supported`` - the ECUs answered capability discovery and did not advertise it;
* ``not_attempted`` - the tool did not ask (e.g. VIN without consent, InfoType never read);
* ``failed``        - asked, no usable answer (status kept in ``detail``);
* ``unknown``       - nothing the tool can read establishes it.

The verdict for manufacturer access is one of ``manufacturer_transport_blocked``,
``manufacturer_transport_unverified`` or ``manufacturer_transport_candidate`` - never a
boolean "DDE supported". Even ``candidate`` only means "worth reviewing the next,
documented step"; it is not evidence that BMW diagnostics work through this adapter.
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.models.domain import Exchange
from app.obd.allowlist import AllowedCommand, ForbiddenRequestError
from app.obd.client import _failure_quality, _positive, decode_exchange
from app.obd.elm import ElmStatus, InvalidFrameError, parse_can_line, parse_reply

REPORT_FORMAT = "mini-obd-dde-capability/1"

#: ELM327 version that introduced CAN extended addressing (``AT CEA hh``), which BMW D-CAN
#: tester requests need according to the literature (Elm Electronics AT command list, Oct 2010).
CEA_MIN_VERSION = (1, 4)

#: EdiabasLib "Adapter types" (uholeschak/ediabaslib docs/AdapterTypes.md, retrieved 2026-11-18):
#: recommended ELM327 versions for BMW D-CAN are 1.4b, 1.5 and ORIGINAL 2.1 on a PIC18F25K80;
#: fake 1.5 parts "will not work"; ARM based adapters are "in most cases not 100% ELM327
#: compatible". Version strings are firmware claims: a clone can print any of them.
EDIABASLIB_RECOMMENDED = ("1.4b", "1.5", "2.1")
AMBIGUOUS_CLAIMS = {
    "1.5": "EdiabasLib warns that fake PIC18F25K80 parts reporting v1.5 exist and do not work",
    "2.1": "EdiabasLib recommends only ORIGINAL v2.1; v2.1 is also the most widely copied clone string",
}

SOURCES = {
    "ediabaslib_adapter_types": "https://github.com/uholeschak/ediabaslib/blob/master/docs/AdapterTypes.md",
    "deep_obd_adapters": "https://uholeschak.github.io/ediabaslib/docs/Deep_OBD_for_BMW_and_VAG.html",
    "elm327_at_commands": "https://cdn.sparkfun.com/assets/4/e/5/0/2/ELM327_AT_Commands.pdf",
}

_VERSION = re.compile(r"ELM327\s*v?(\d+)\.(\d+)([a-z]?)", re.IGNORECASE)
_IDENT = {0x04: "calibration_id", 0x06: "cvn", 0x0A: "ecu_name", 0x02: "vin"}


def _field(value: Any, quality: str, source: str | None = None, detail: str = "", **extra: Any) -> dict:
    return {"value": value, "quality": quality, "source": source, "detail": detail, **extra}


def _as_exchange(ex: Exchange | dict[str, Any]) -> Exchange:
    return ex if isinstance(ex, Exchange) else Exchange(**ex)


def parse_elm_version(identity: str | None) -> dict[str, Any] | None:
    """'ELM327 v2.2' -> {'text': '2.2', 'tuple': (2, 2), 'suffix': ''}; None when no version is claimed."""
    m = _VERSION.search(identity or "")
    if not m:
        return None
    major, minor, suffix = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    return {"text": f"{major}.{minor}{suffix}", "tuple": (major, minor), "suffix": suffix}


def manufacturer_requests_in(exchanges: list[Exchange]) -> list[str]:
    """Requests in a trace that are NOT on the generic allowlist (must be empty for DDE-01)."""
    out = []
    for ex in exchanges:
        try:
            AllowedCommand.parse(ex.request)
        except ForbiddenRequestError:
            out.append(ex.request)
    return out


def isotp_evidence(exchanges: list[Exchange], is_29bit: bool | None) -> list[dict[str, Any]]:
    """Cleanly completed ISO-TP multi-frame answers (first frame + consecutive frames, the adapter
    sending flow control) seen in the trace, per request and source ECU."""
    out: list[dict[str, Any]] = []
    for ex in exchanges:
        if ex.status != "ok" or ex.request.startswith("AT"):
            continue
        reply = parse_reply(bytes.fromhex(ex.response_hex), ex.request)
        if reply.status != ElmStatus.OK:
            continue
        first_frames: set[str] = set()
        for line in reply.lines:
            try:
                fr = parse_can_line(line, is_29bit)
            except InvalidFrameError:
                continue
            if fr.data[0] >> 4 == 0x1:
                first_frames.add(fr.ecu)
        if not first_frames:
            continue
        _, _, msgs, _ = decode_exchange(ex, "can", is_29bit)
        for m in msgs:
            if m.ecu in first_frames:
                out.append(
                    {
                        "seq": ex.seq,
                        "request": ex.request,
                        "ecu": m.ecu,
                        "payload_bytes": len(m.payload),
                        "complete": m.complete,
                        "problem": m.problem,
                    }
                )
    return out


def _identification(
    exchanges: list[Exchange], family: str, is_29bit: bool | None, supported09: dict[str, list[int]]
) -> dict[str, dict[str, Any]]:
    advertised = {p for pids in supported09.values() for p in pids}
    last: dict[int, Exchange] = {}
    for ex in exchanges:
        if len(ex.request) == 4 and ex.request.startswith("09"):
            it = int(ex.request[2:], 16)
            if it in _IDENT:
                last[it] = ex
    out: dict[str, dict[str, Any]] = {}
    for it, key in _IDENT.items():
        ex = last.get(it)
        src = f"service 09 InfoType {it:02X}"
        if ex is None:
            if supported09 and it not in advertised:
                out[key] = _field(None, "not_supported", src, "not advertised in the 0900 bitmap of any ECU")
            elif it == 0x02:
                out[key] = _field(None, "not_attempted", src, "the VIN is read only with explicit consent")
            else:
                out[key] = _field(None, "not_attempted", src, "not requested in this session")
            continue
        if ex.status == "redacted":
            out[key] = _field(None, "observed", src, "answer redacted in this trace", exchange_seq=ex.seq)
            continue
        _ex, reply, msgs, problems = decode_exchange(ex, family, is_29bit)
        good = _positive(msgs, bytes([0x49, it])) if reply.status == ElmStatus.OK else []
        if not good:
            q, d = _failure_quality(ex, reply, msgs, problems)
            out[key] = _field(None, "failed", src, f"{q}: {d}", exchange_seq=ex.seq)
            continue
        by_ecu: dict[str, bytes] = {}
        for m in good:
            by_ecu[m.ecu] = by_ecu.get(m.ecu, b"") + m.payload[3:]
        answers = []
        for ecu, data in by_ecu.items():
            if it == 0x02:
                # the VIN itself never enters the report (exports must not depend on redaction)
                answers.append({"ecu": ecu, "length": len(data.replace(b"\x00", b"")), "text": None})
            else:
                answers.append(
                    {
                        "ecu": ecu,
                        "raw_hex": data.hex().upper(),
                        "text": None if it == 0x06 else data.replace(b"\x00", b"").decode("latin-1").strip(),
                    }
                )
        detail = {
            0x04: "calibration ID(s) as reported by each ECU (SAE J1979); not a BMW software part number by itself",
            0x06: "calibration verification number(s): binary checksums, kept raw",
            0x0A: "generic ECU name (SAE J1979); does not identify the DDE software variant",
            0x02: "VIN read with consent; value withheld from this report (see the session, redacted in exports)",
        }[it]
        out[key] = _field(answers, "observed", src, detail, exchange_seq=ex.seq)
    return out


def assess(
    *,
    adapter: dict[str, Any] | None,
    capabilities: dict[str, Any] | None,
    exchanges: list[Exchange | dict[str, Any]],
    source: str,
    synthetic: bool,
    session_id: str | None = None,
    assessed_at: float | None = None,
) -> dict[str, Any]:
    """Build the capability report from one session's adapter info, capabilities and raw trace."""
    adapter = adapter or {}
    caps = capabilities or {}
    exs = [_as_exchange(e) for e in exchanges]
    is_can = bool(adapter.get("is_can"))
    is_29bit = adapter.get("is_29bit")
    family = "can" if is_can else "kline"

    def seq_of(cmd: str) -> int | None:
        return next((e.seq for e in exs if e.request == cmd), None)

    # -- adapter -------------------------------------------------------------------------
    ident = adapter.get("identity")
    ver = parse_elm_version(ident)
    notes = [
        "ATI / AT@1 are strings the adapter firmware reports about itself. Clones copy genuine strings, so "
        "they establish neither the chip nor its CAN feature set."
    ]
    if ver and ver["text"] in AMBIGUOUS_CLAIMS:
        notes.append(AMBIGUOUS_CLAIMS[ver["text"]])
    if ver and ver["text"] not in EDIABASLIB_RECOMMENDED:
        notes.append(
            f"v{ver['text']} is not one of the ELM327 versions EdiabasLib recommends for BMW D-CAN "
            f"({', '.join(EDIABASLIB_RECOMMENDED)}); the Vgate iCar Pro Wi-Fi is not named in its adapter list."
        )
    adapter_out = {
        "identity": _field(ident, "claimed" if ident else "unknown", "ATI", "", exchange_seq=seq_of("ATI")),
        "device_description": _field(
            adapter.get("device_description"),
            "claimed" if adapter.get("device_description") else "unknown",
            "AT@1",
            exchange_seq=seq_of("AT@1"),
        ),
        "claimed_elm_version": _field(
            ver["text"] if ver else None,
            "claimed" if ver else "unknown",
            "ATI",
            "parsed from the ATI string" if ver else "no ELM327 version in the ATI answer",
        ),
        "supply_voltage": _field(
            adapter.get("voltage"),
            "observed" if adapter.get("voltage") else "unknown",
            "ATRV",
            "measured by the adapter at its OBD pin, not an ECU value",
        ),
        "chip": _field(
            None,
            "unknown",
            None,
            "the chip / firmware family cannot be read through allowed commands; not inferred from ATI",
        ),
        "notes": notes,
    }

    # -- protocol / ECUs ---------------------------------------------------------------------------
    num = (adapter.get("protocol_number") or "").upper()
    proto = {
        "number": _field(
            num or None, "observed" if num else "unknown", "ATDPN", exchange_seq=seq_of("ATDPN")
        ),
        "name": _field(
            adapter.get("protocol_name"),
            "observed" if adapter.get("protocol_name") else "unknown",
            "ATDP",
            exchange_seq=seq_of("ATDP"),
        ),
        "can_11bit_500k": _field(
            num.lstrip("A") == "6" if num else None,
            "derived" if num else "unknown",
            "ATDPN",
            "ELM protocol 6 = ISO 15765-4 CAN 11-bit 500 kbit/s",
        ),
    }
    ecus = []
    for e in caps.get("ecus") or []:
        ecus.append(
            {
                "source_id": e["ecu"],
                "primary": e["ecu"] == caps.get("primary_ecu"),
                "mode01_pids": len(e.get("mode01_pids") or []),
                "mode02_pids": len(e.get("mode02_pids") or []),
                "mode06_mids": len(e.get("mode06_mids") or []),
                "mode09_infotypes": [f"{p:02X}" for p in e.get("mode09_pids") or []],
                "quality": "observed",
                "role": "engine ECU candidate (most service 01 PIDs)"
                if e["ecu"] == caps.get("primary_ecu")
                else "other emissions-related ECU (role not identified)",
            }
        )
    supported09 = {e["ecu"]: list(e.get("mode09_pids") or []) for e in caps.get("ecus") or []}
    ident09 = _identification(exs, family, is_29bit, supported09 if caps.get("ecus") else {})
    isotp = isotp_evidence(exs, is_29bit) if is_can else []
    isotp_ok = [x for x in isotp if x["complete"]]
    mfr = manufacturer_requests_in(exs)

    # -- generic OBD verdict ---------------------------------------------------------------------
    good01 = [e for e in exs if e.status == "ok" and e.request.startswith("01") and e.phase not in ("init",)]
    reasons_g: list[str] = []
    if not exs:
        g = "generic_obd_unverified"
        reasons_g.append("no raw exchanges in this session")
    elif not caps.get("ecus"):
        g = "generic_obd_unverified"
        reasons_g.append("no ECU answered capability discovery")
    elif synthetic:
        g = "generic_obd_simulated"
        reasons_g.append(
            "synthetic simulator data: proves the software path only, not the car or the adapter"
        )
    else:
        g = "generic_obd_verified"
        reasons_g.append(
            f"{len(ecus)} ECU(s) answered generic OBD capability discovery on {adapter.get('protocol_name')}"
        )
        reasons_g.append(f"{len(good01)} completed service 01 exchange(s) in the trace")
    generic = {"verdict": g, "reasons": reasons_g}

    # -- manufacturer transport verdict --------------------------------------------------------------
    blockers: list[str] = []
    missing: list[str] = []
    if g == "generic_obd_unverified":
        blockers.append("generic OBD itself is not established on this connection")
    if not is_can:
        blockers.append(
            f"vehicle protocol is {adapter.get('protocol_name') or 'unknown'}, not CAN; BMW D-CAN access through "
            "an ELM327 path is described for CAN only (EdiabasLib: 'Only D-CAN is supported')"
        )
    elif num.lstrip("A") != "6":
        blockers.append(
            f"CAN variant {adapter.get('protocol_name')} is not 11-bit / 500 kbit/s, the D-CAN bus described in the literature"
        )
    if not ident:
        blockers.append("the adapter gave no ELM327 identity")
    elif ver is None:
        missing.append("the ATI answer carries no ELM327 version; CAN extended addressing support unknown")
    elif ver["tuple"] < CEA_MIN_VERSION:
        blockers.append(
            f"claimed ELM327 v{ver['text']} predates CAN extended addressing (AT CEA, v1.4); BMW D-CAN tester "
            "requests use extended addressing in the literature"
        )
    if not isotp_ok:
        missing.append(
            "no completed ISO-TP multi-frame answer observed yet (adapter flow control unproven); a capability run "
            "reading service 09 InfoTypes 04 / 06 / 0A normally produces one"
        )
    missing += [
        "acceptance of AT CEA / AT SH / AT FC* by THIS adapter is untested: those are manufacturer-addressing "
        "commands and are not sent by this assessment",
        "no documented, variant-matched BMW request has been verified for this DDE (see DDE-02)",
        "the DDE variant / software level is unknown",
    ]
    if blockers:
        m = "manufacturer_transport_blocked"
    elif is_can and ver and isotp_ok and not synthetic:
        m = "manufacturer_transport_candidate"
    else:
        m = "manufacturer_transport_unverified"
    if synthetic:
        missing.insert(0, "synthetic session: says nothing about the real adapter or car")
    manufacturer = {
        "verdict": m,
        "blockers": blockers,
        "unverified": missing,
        "meaning": {
            "manufacturer_transport_blocked": "a stated prerequisite is missing; do not attempt manufacturer requests",
            "manufacturer_transport_unverified": "no blocker found, but the evidence needed to call it a candidate is missing",
            "manufacturer_transport_candidate": "prerequisites observed; the next step (documented requests, a "
            "stationary test with a reference tool) may be reviewed - NOT proof that DDE access works",
        }[m],
    }

    dde = {
        "variant": _field(
            None,
            "unknown",
            None,
            "the DDE software variant is not readable with generic OBD; calibration ID / ECU name are generic "
            "identifiers and are not mapped to a DDE variant by this tool",
        ),
        "software_version": _field(None, "unknown", None, "requires a BMW identification job (not sent)"),
        "manufacturer_requests_sent": _field(
            len(mfr),
            "observed",
            "raw trace",
            "count of requests outside the generic allowlist in this session (must be 0)",
            requests=mfr,
        ),
    }

    return {
        "format": REPORT_FORMAT,
        "session_id": session_id,
        "assessed_at": assessed_at if assessed_at is not None else time.time(),
        "source": source,
        "synthetic": synthetic,
        "adapter": adapter_out,
        "protocol": proto,
        "ecus": ecus,
        "identification": ident09,
        "isotp_multiframe": _field(
            bool(isotp_ok) if is_can else None,
            "observed" if isotp_ok else ("unknown" if is_can else "not_supported"),
            "raw trace",
            f"{len(isotp_ok)} completed multi-frame answer(s), {len(isotp) - len(isotp_ok)} incomplete"
            if is_can
            else "not a CAN session",
            evidence=isotp[:20],
        ),
        "dde": dde,
        "verdicts": {"generic_obd": generic, "manufacturer_transport": manufacturer},
        "sources": SOURCES,
    }


def assess_session(store: Any, sid: str) -> dict[str, Any]:
    """Offline assessment of a stored session. Read-only: nothing in the session is changed."""
    s = store.get_session(sid)
    if s is None:
        raise KeyError(f"no session {sid}")
    meta = s["meta"]
    return assess(
        adapter=meta.get("adapter"),
        capabilities=meta.get("capabilities"),
        exchanges=store.exchanges(sid),
        source=s["source"],
        synthetic=s["synthetic"],
        session_id=sid,
        assessed_at=s["ended_at"] or s["started_at"],
    )


def report_rows(report: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """(field, value, quality, source) rows for CLI / Markdown tables."""
    rows: list[tuple[str, str, str, str]] = []

    def add(name: str, f: dict[str, Any]) -> None:
        v = f["value"]
        if isinstance(v, list):
            v = "; ".join(
                f"{a['ecu']}: {a.get('text') or a.get('raw_hex') or ('VIN, ' + str(a.get('length')) + ' chars')}"
                for a in v
            )
        rows.append((name, "-" if v is None else str(v), f["quality"], f.get("source") or "-"))

    for k in ("identity", "device_description", "claimed_elm_version", "supply_voltage", "chip"):
        add(f"adapter.{k}", report["adapter"][k])
    for k in ("number", "name", "can_11bit_500k"):
        add(f"protocol.{k}", report["protocol"][k])
    for k, f in report["identification"].items():
        add(f"service09.{k}", f)
    add("isotp_multiframe", report["isotp_multiframe"])
    for k in ("variant", "software_version", "manufacturer_requests_sent"):
        add(f"dde.{k}", report["dde"][k])
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    v = report["verdicts"]
    out = [
        f"# DDE capability report ({report.get('session_id') or 'n/a'})",
        "",
        "> SYNTHETIC simulator data: not a statement about the car or the adapter."
        if report["synthetic"]
        else "> Recorded from a real adapter.",
        "",
        f"- Generic OBD: **{v['generic_obd']['verdict']}** - " + "; ".join(v["generic_obd"]["reasons"]),
        f"- Manufacturer transport: **{v['manufacturer_transport']['verdict']}** "
        f"({v['manufacturer_transport']['meaning']})",
    ]
    out += [f"  - blocker: {b}" for b in v["manufacturer_transport"]["blockers"]]
    out += [f"  - unverified: {b}" for b in v["manufacturer_transport"]["unverified"]]
    out += ["", "| Field | Value | Quality | Source |", "|---|---|---|---|"]
    out += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in report_rows(report)]
    out += ["", "ECUs:", ""]
    out += [
        f"- {e['source_id']}{' (primary)' if e['primary'] else ''}: {e['mode01_pids']} service 01 PIDs, "
        f"service 09 InfoTypes {', '.join(e['mode09_infotypes']) or 'none'}; {e['role']}"
        for e in report["ecus"]
    ]
    out += ["", "Adapter notes:", ""] + [f"- {n}" for n in report["adapter"]["notes"]]
    return "\n".join(out) + "\n"
