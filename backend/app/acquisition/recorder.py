"""Rate-limited sequential PID poller and sampling-quality metrics.

PIDs are polled one after another (ELM allows one request at a time), so two
signals are never sampled at the same instant. Every sample keeps its own
timestamp; nothing is interpolated and failed reads are stored as failed.
"""

from __future__ import annotations

import statistics
import threading
from collections.abc import Callable
from typing import Any

from app.adapters.base import TransportClosedError, TransportError
from app.models.domain import PidMetrics, Reading
from app.obd.client import ObdClient, ObdError
from app.parameters.standard_pids import pid_name, signal_name

GOOD = ("ok", "raw_only")


class Recorder:
    def __init__(
        self,
        client: ObdClient,
        fast: list[int],
        slow: list[int],
        duration_s: float,
        on_readings: Callable[[list[Reading]], None],
        stop: threading.Event | None = None,
    ) -> None:
        if not fast and not slow:
            raise ValueError("no supported signals selected for recording")
        self.client, self.fast, self.slow, self.duration_s = client, fast, slow, duration_s
        self.on_readings = on_readings
        self.stop = stop or threading.Event()
        self.clock = client.transport.clock

    def _read(self, pid: int) -> list[Reading]:
        clock = self.clock
        fail = dict(pid=pid, name=pid_name(pid), t_wall=clock.wall(), t_mono=clock.monotonic())
        try:
            return self.client.read_pid(pid)
        except TransportClosedError as exc:
            ex = exc.exchange
            if ex:
                fail.update(t_wall=ex.t_wall, t_mono=ex.t_mono, latency_ms=ex.latency_ms)
            return [Reading(**fail, quality="disconnected", detail=str(exc))]
        except TransportError as exc:
            return [Reading(**fail, quality="disconnected", detail=str(exc))]
        except ObdError as exc:
            clock.sleep(0.5)
            return [Reading(**fail, quality="error", detail=str(exc))]

    def run(self) -> None:
        clock = self.clock
        end = clock.monotonic() + self.duration_s
        i = 0

        def live() -> bool:
            return clock.monotonic() < end and not self.stop.is_set()

        while live():
            cycle = list(self.fast)
            if self.slow:
                cycle.append(self.slow[i % len(self.slow)])
                i += 1
            for pid in cycle:
                if not live():
                    break
                self.on_readings(self._read(pid))


def compute_metrics(samples: list[dict[str, Any]]) -> list[PidMetrics]:
    """Per-signal sampling quality from stored samples (service 01, plus adapter-measured mode 0).

    One *attempt* = one request (several ECUs answering the same request share
    its timestamp). actual_hz = successful requests / session span.
    """
    rows = [s for s in samples if s["mode"] in (0, 1)]
    if not rows:
        return []
    span = max(s["t_mono"] for s in rows) - min(s["t_mono"] for s in rows)
    by_pid: dict[tuple[int, int], dict[float, list[dict[str, Any]]]] = {}
    for s in rows:
        by_pid.setdefault((s["mode"], s["pid"]), {}).setdefault(s["t_mono"], []).append(s)
    out: list[PidMetrics] = []
    # service 01 first (sorted by PID), adapter signals last: keeps the established order
    for (mode, pid), attempts in sorted(by_pid.items(), key=lambda kv: (kv[0][0] != 1, kv[0][1])):
        ok_times = sorted(t for t, rs in attempts.items() if any(r["quality"] in GOOD for r in rs))
        lat = sorted(
            rs[0]["latency_ms"] for t, rs in attempts.items() if any(r["quality"] in GOOD for r in rs)
        )
        intervals = [b - a for a, b in zip(ok_times, ok_times[1:], strict=False)]
        med = statistics.median(intervals) if intervals else None
        n, ok = len(attempts), len(ok_times)
        out.append(
            PidMetrics(
                pid=pid,
                mode=mode,
                name=signal_name(mode, pid),
                attempts=n,
                ok=ok,
                dropped=n - ok,
                error_rate=round((n - ok) / n, 4),
                actual_hz=round(ok / span, 3) if span > 0 else 0.0,
                latency_ms_mean=round(statistics.fmean(lat), 2) if lat else None,
                latency_ms_p95=round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2) if lat else None,
                latency_ms_max=round(lat[-1], 2) if lat else None,
                max_gap_s=round(max(intervals), 3) if intervals else None,
                gaps=sum(1 for d in intervals if med and d > 3 * med),
            )
        )
    return out
