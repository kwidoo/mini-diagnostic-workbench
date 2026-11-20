"""DDE-03: runtime for gated manufacturer reads on an existing connection.

``DdeRuntime`` ties together the catalog (what may run), the execution context (opt-in,
confirmed variant, DDE-01 verdict, simulator or real) and the client (transport lock,
initialised connection). It is the only caller of ``ObdClient.manufacturer_exchange``.

Behaviour on failures - nothing is ever fabricated:

* not approved (gate)          -> ``not_attempted`` with the gate's reasons, nothing sent;
* adapter / protocol unfit     -> ``unsupported_adapter``; remembered: no further manufacturer
  request on this connection (no reckless retries);
* a set-up command answered ``?`` -> ``unsupported_adapter`` (same);
* link not initialised          -> ``not_attempted`` (no re-initialisation for a DDE read);
* NO DATA / NRC / timeout / dropped / malformed -> that quality, raw bytes kept, no value.
"""

from __future__ import annotations

from typing import Any

from app.adapters.base import TransportClosedError, TransportError, TransportUnsyncedError
from app.manufacturer.bmw.catalog import CatalogEntry, ExecutionContext, RequestCatalog
from app.manufacturer.bmw.guarded import (
    ApprovedRequest,
    DdeResult,
    NotApprovedError,
    UnsupportedAdapterError,
    approve,
    at_ok,
    decode_request_exchange,
)
from app.models.domain import Exchange


class DdeRuntime:
    def __init__(self, catalog: RequestCatalog, ctx: ExecutionContext, client: Any) -> None:
        self.catalog = catalog
        self.ctx = ctx
        self.client = client
        # set once the adapter proved unable; then nothing is sent. Remembered on the client, i.e. for
        # the whole adapter connection (scans, inventory and recordings share it)
        self._unsupported: str | None = None
        self.counts: dict[str, int] = {}

    @property
    def unsupported(self) -> str | None:
        """Refusal remembered on the CLIENT (one adapter connection), so every runtime on that
        connection - scans, inventory, recordings - respects it; a new connection starts clean."""
        return self._unsupported or getattr(self.client, "dde_unsupported", None)

    def _mark_unsupported(self, why: str | None) -> None:
        self._unsupported = why or "adapter cannot provide the documented addressing"
        mark = getattr(self.client, "mark_dde_unsupported", None)
        if mark is not None:
            mark(self._unsupported)

    # -- planning -------------------------------------------------------------------------
    def executable(self, kind: str | None = None) -> list[CatalogEntry]:
        if self.unsupported:
            return []
        return self.catalog.executable(self.ctx, kind)  # type: ignore[arg-type]

    def describe(self) -> dict[str, Any]:
        d = self.catalog.describe(self.ctx)
        d["unsupported_adapter"] = self.unsupported
        d["results"] = dict(self.counts)
        return d

    # -- one read -------------------------------------------------------------------------
    def _result(self, e: CatalogEntry, quality: str, detail: str | None = None, **kw: Any) -> DdeResult:
        self.counts[quality] = self.counts.get(quality, 0) + 1
        clock = self.client.transport.clock
        kw.setdefault("t_wall", clock.wall())
        kw.setdefault("t_mono", clock.monotonic())
        return DdeResult(
            request_id=e.request_id,
            metric=e.metric,
            kind=e.kind,
            quality=quality,
            detail=detail,
            catalog_version=self.catalog.version,
            **kw,
        )

    def read(self, request_id: str) -> DdeResult:
        e = self.catalog.entries[request_id]
        if self.unsupported:
            return self._result(e, "unsupported_adapter", self.unsupported)
        try:
            approved = approve(self.catalog, request_id, self.ctx, self.client.adapter.model_dump())
        except UnsupportedAdapterError as exc:
            self._mark_unsupported("; ".join(exc.reasons))
            return self._result(e, "unsupported_adapter", self.unsupported)
        except NotApprovedError as exc:
            return self._result(e, "not_attempted", "; ".join(exc.reasons))
        try:
            exs = self.client.manufacturer_exchange(approved)
        except TransportClosedError as exc:
            exs = getattr(exc, "exchanges", None) or ([exc.exchange] if exc.exchange else [])
            return self._from_exchanges(approved, exs, closed=True)
        except TransportUnsyncedError as exc:
            return self._result(e, "not_attempted", str(exc))
        except TransportError as exc:
            return self._result(e, "disconnected", str(exc))
        if exs is None:
            return self._result(e, "not_attempted", "adapter link not initialised on this connection")
        return self._from_exchanges(approved, exs)

    def _from_exchanges(
        self, approved: ApprovedRequest, exs: list[Exchange], closed: bool = False
    ) -> DdeResult:
        e = approved.entry
        seqs = [x.seq for x in exs]
        req = next((x for x in exs if x.request == approved.request.text), None)
        if req is None:
            # the request was never sent: which set-up step failed?
            bad = next((x for x in exs if x.request.startswith("AT") and not at_ok(x)), None)
            if bad is not None and "?" in bad.response_text:
                self._mark_unsupported(
                    f"adapter rejected {bad.request} ('?'): it cannot provide this addressing"
                )
                return self._result(e, "unsupported_adapter", self.unsupported, exchange_seqs=seqs)
            q = "disconnected" if closed else "error"
            what = f"{bad.request}: {bad.status} {bad.response_text!r}" if bad else "no exchange"
            return self._result(e, q, f"adapter set-up failed ({what}); request not sent", exchange_seqs=seqs)
        timing = dict(t_wall=req.t_wall, t_mono=req.t_mono, latency_ms=req.latency_ms, exchange_seqs=seqs)
        quality, payload, nrc, detail = decode_request_exchange(req, approved)
        a = e.addressing
        ecu = f"{a.rx_can_id}" + (f"/{a.rx_ext_address}" if a and a.rx_ext_address else "") if a else None
        raw = req.response_hex
        if quality == "unsupported_adapter":
            self._mark_unsupported(detail)
        if quality != "ok" or payload is None:
            return self._result(e, quality, detail, raw_hex=raw, nrc=nrc, ecu=ecu, payload=payload, **timing)
        if e.kind != "live_value" and e.kind != "identification":
            # list layouts (fault memory) are decoded by their own module from the payload
            return self._result(
                e, "ok", None, raw_hex=payload.hex().upper(), payload=payload, ecu=ecu, **timing
            )
        v = self.catalog.interpret(e.request_id, payload, self.ctx)
        return self._result(
            e,
            v.quality,
            v.detail,
            value=v.value,
            text=v.text,
            unit=v.unit,
            status=v.status,
            raw_hex=payload.hex().upper(),
            payload=payload,
            ecu=ecu,
            decoder_version=v.decoder_version,
            **timing,
        )
