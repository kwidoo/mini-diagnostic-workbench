"""DDE-05: verified BMW DDE live parameters as provenance-tagged readings.

``DdeLiveReader`` reads executable ``live_value`` / ``identification`` catalog entries through
the gated runtime and turns each result into a :class:`Reading` that can never be confused
with generic OBD:

* ``mode = MANUFACTURER_MODE`` (0x100, outside every OBD service number) and a stable ``pid``
  per signal key (index in the catalog's metric list, offset 0x100);
* ``source = "bmw_dde"``, ``signal_key``, ``request_id``, ``catalog_version``,
  ``decoder_version``, the answering ``ecu`` (response CAN id / extended address), raw
  payload, timestamps and latency, and decoded ``status`` semantics when the layout defines them;
* a value ONLY for quality ``ok``. ``raw_only`` keeps bytes; NO DATA / NRC / timeout /
  disconnected / error / unsupported adapter carry no value. Nothing becomes 0.

Availability is dynamic: a signal that keeps failing (NO DATA / negative) while the ECU
answers other requests is suspended with a growing back-off; an adapter that cannot provide
the addressing stops all manufacturer reads on that connection. Unknown stays unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.manufacturer.bmw.catalog import WANTED_METRICS, CatalogEntry
from app.manufacturer.bmw.guarded import DdeResult
from app.models.domain import MANUFACTURER_MODE, Reading

SUSPEND_AFTER = 3  # consecutive NO DATA / negative answers of one signal
SUSPEND_S = (60.0, 600.0)  # initial and maximum back-off
_QUALITY = {
    "ok": "ok",
    "raw_only": "raw_only",
    "no_data": "no_data",
    "negative": "negative",
    "timeout": "timeout",
    "disconnected": "disconnected",
    "error": "error",
    "unsupported_adapter": "error",
}


def signal_pid(metric: str) -> int:
    """Stable numeric id for a manufacturer signal key (never in the 00-FF generic PID range)."""
    keys = list(WANTED_METRICS)
    return 0x100 + (keys.index(metric) if metric in keys else 0xFF)


def to_reading(r: DdeResult, e: CatalogEntry) -> Reading | None:
    """DdeResult -> Reading. ``not_attempted`` (nothing was sent) gives None: no sample is stored."""
    if r.quality == "not_attempted":
        return None
    q = _QUALITY.get(r.quality, "error")
    detail = r.detail
    if r.quality == "unsupported_adapter":
        detail = f"unsupported adapter: {r.detail}"
    if r.quality == "negative" and r.nrc:
        detail = f"negative response, NRC {r.nrc}"
    return Reading(
        mode=MANUFACTURER_MODE,
        pid=signal_pid(e.metric),
        ecu=r.ecu,
        name=e.title,
        unit=r.unit if q == "ok" else (e.response.unit if e.response else None),
        value=r.value if q == "ok" else None,
        text=r.text if q == "ok" else None,
        raw_hex=r.raw_hex,
        quality=q,  # type: ignore[arg-type]
        detail=detail,
        t_wall=r.t_wall,
        t_mono=r.t_mono,
        latency_ms=r.latency_ms,
        source="bmw_dde",
        signal_key=e.metric,
        request_id=e.request_id,
        decoder_version=r.decoder_version,
        catalog_version=r.catalog_version,
        status=r.status if q == "ok" else None,
    )


@dataclass
class _State:
    fails: int = 0
    suspend_s: float = 0.0
    suspended_until: float | None = None
    attempts: int = 0
    ok: int = 0
    last_quality: str | None = None


class DdeLiveReader:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.entries: dict[str, CatalogEntry] = {
            e.metric: e for e in runtime.executable() if e.kind in ("live_value", "identification")
        }
        self.state: dict[str, _State] = {m: _State() for m in self.entries}

    @property
    def clock(self):
        return self.runtime.client.transport.clock

    def due(self, metric: str) -> bool:
        st = self.state[metric]
        return st.suspended_until is None or self.clock.monotonic() >= st.suspended_until

    def read(self, metric: str, ecu_answering: bool = True) -> Reading | None:
        """One read. ``ecu_answering``: other vehicle requests succeed right now, so NO DATA / NRC
        count against THIS signal (otherwise a general outage is not blamed on one signal)."""
        e = self.entries[metric]
        st = self.state[metric]
        res = self.runtime.read(e.request_id)
        reading = to_reading(res, e)
        if reading is None:
            st.last_quality = "not_attempted"
            return None
        st.attempts += 1
        st.last_quality = res.quality
        if reading.quality in ("ok", "raw_only"):
            st.ok += 1
            st.fails, st.suspend_s, st.suspended_until = 0, 0.0, None
        elif reading.quality in ("no_data", "negative") and ecu_answering:
            st.fails += 1
            if st.fails >= SUSPEND_AFTER:
                st.suspend_s = min(SUSPEND_S[1], (st.suspend_s * 2) or SUSPEND_S[0])
                st.suspended_until = self.clock.monotonic() + st.suspend_s
        return reading

    def snapshot(self) -> list[Reading]:
        """Each executable live signal once (inventory)."""
        out = []
        for m in self.entries:
            r = self.read(m)
            if r is not None:
                out.append(r)
            if self.runtime.unsupported:
                break
        return out

    def availability(self) -> list[dict[str, Any]]:
        """Per wanted metric: the catalog decision plus what this connection actually showed."""
        out = []
        for a in self.runtime.catalog.availability(self.runtime.ctx, wanted=WANTED_METRICS):
            m = a["metric"]
            st = self.state.get(m)
            status = a["status"]
            if self.runtime.unsupported:
                status = "unsupported_adapter"
            elif st and st.suspended_until is not None:
                status = "suspended"
            out.append(
                {
                    "metric": m,
                    "title": a.get("title"),
                    "status": status,
                    "reasons": [self.runtime.unsupported] if self.runtime.unsupported else a["reasons"],
                    "attempts": st.attempts if st else 0,
                    "ok": st.ok if st else 0,
                    "last_quality": st.last_quality if st else None,
                    "retry_after_s": st.suspend_s if st and st.suspended_until is not None else None,
                }
            )
        return out


def monitoring_plan(live: DdeLiveReader, tiers: Any) -> list[dict[str, Any]]:
    """DDE-06: capability-driven manufacturer items for the monitoring scheduler. Live values only
    (identification is not polled), never in the fast tier: P0 values at the medium interval (or
    their documented interval, whichever is longer), everything else slow. The scheduler stretches
    these further when the adapter is slow (bounded share of link time)."""
    out = []
    for metric, e in live.entries.items():
        if e.kind != "live_value":
            continue
        want = e.sample_interval_s or tiers.slow_s
        tier = "medium" if e.priority == "P0" and want <= tiers.medium_s * 1.5 else "slow"
        base = max(want, tiers.medium_s if tier == "medium" else tiers.slow_s)
        out.append(
            {
                "metric": metric,
                "title": e.title,
                "request_id": e.request_id,
                "priority": e.priority,
                "tier": tier,
                "base_interval_s": base,
                "unit": e.response.unit if e.response else None,
                "semantics": e.semantics,
            }
        )
    return out
