"""Transport abstraction: one serialized ELM request at a time, fully logged."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

from app.models.domain import Exchange
from app.obd.allowlist import AllowedCommand, ForbiddenRequestError


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class VirtualClock:
    """Deterministic clock for fixtures and tests; sleep() just advances time."""

    def __init__(self, wall_start: float = 1_700_000_000.0) -> None:
        self._wall0 = wall_start
        self._t = 0.0

    def monotonic(self) -> float:
        return self._t

    def wall(self) -> float:
        return self._wall0 + self._t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._t += seconds


class TransportError(Exception):
    """Could not (re)connect to the adapter."""


class TransportClosedError(TransportError):
    """Connection dropped during an exchange. ``exchange`` holds what was received."""

    def __init__(self, message: str, exchange: Exchange | None = None) -> None:
        super().__init__(message)
        self.exchange = exchange


class Transport(ABC):
    kind = "abstract"

    def __init__(self, clock=None, min_interval_s: float = 0.0, default_timeout_s: float = 5.0) -> None:
        self.clock = clock or SystemClock()
        self.min_interval_s = min_interval_s
        self.default_timeout_s = default_timeout_s
        self.listeners: list[Callable[[Exchange], None]] = []
        self.generation = 0  # bumped on every successful (re)connect
        self.state = "disconnected"  # disconnected | connecting | connected | reconnecting
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._last_end: float | None = None
        self._seq = 0

    # -- lifecycle ----------------------------------------------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def cancel(self) -> None:
        """Abort the exchange in flight (it returns with status 'cancelled')."""
        self._cancel.set()

    def describe(self) -> dict[str, object]:
        return {"kind": self.kind, "state": self.state, "last_error": self.last_error}

    # -- subclass hooks -------------------------------------------------------
    @abstractmethod
    def _ensure_connected(self) -> None: ...

    @abstractmethod
    def _roundtrip(self, wire: bytes, timeout_s: float) -> tuple[bytes, str]:
        """Send and read until the '>' prompt. Returns (raw bytes, status)."""

    # -- the single entry point -----------------------------------------------
    def send(self, cmd: AllowedCommand, timeout_s: float | None = None) -> Exchange:
        if not isinstance(cmd, AllowedCommand):
            raise ForbiddenRequestError("transports only accept AllowedCommand objects")
        with self._lock:  # one outstanding ELM request at a time
            self._cancel.clear()
            if self._last_end is not None and self.min_interval_s > 0:
                wait = self.min_interval_s - (self.clock.monotonic() - self._last_end)
                self.clock.sleep(wait)
            self._ensure_connected()
            t_wall = self.clock.wall()
            t0 = self.clock.monotonic()
            raw, status = self._roundtrip(cmd.wire(), timeout_s or self.default_timeout_s)
            t1 = self.clock.monotonic()
            self._last_end = t1
            self._seq += 1
            ex = Exchange(
                seq=self._seq,
                t_wall=t_wall,
                t_mono=t0,
                request=cmd.text,
                request_hex=cmd.wire().hex().upper(),
                response_hex=raw.hex().upper(),
                response_text=raw.decode("latin-1"),
                latency_ms=round((t1 - t0) * 1000, 3),
                status=status,  # type: ignore[arg-type]
            )
            for fn in list(self.listeners):
                fn(ex)
            if status == "closed":
                raise TransportClosedError("adapter connection dropped during request", ex)
            return ex
