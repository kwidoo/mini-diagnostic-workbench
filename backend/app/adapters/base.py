"""Transport abstraction: one serialized ELM request at a time, fully logged.

Synchronisation rule (see docs/DIAGNOSTIC_PROTOCOL.md, "Resynchronisation"):
an ELM327 link is a single request -> response-until-'>' stream with no request
ids. Once an exchange ends without a clean prompt (timeout, cancel, partial
reply, connection loss, unexpected bytes) the position in that stream is
unknown: a late reply may still arrive and would be indistinguishable from the
answer to the next request. Such a connection is therefore marked
*unsynchronised* and is never reused. The next send first discards it
(``_resync``), opens a fresh one, and vehicle requests are refused with
:class:`TransportUnsyncedError` until the client has re-initialised the adapter
on the new connection (:meth:`Transport.mark_initialized`).
"""

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


class TransportUnsyncedError(TransportError):
    """A vehicle request was attempted on a connection the adapter was not (re)initialised on."""


INCOMPLETE = ("timeout", "cancelled", "closed", "desync")


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
        self.synced = True  # False after any incomplete exchange: connection must not be reused
        self.initialized_generation: int | None = None  # generation the client last initialised on
        self.resyncs = 0

    # -- lifecycle ----------------------------------------------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def cancel(self) -> None:
        """Abort the exchange in flight (it returns with status 'cancelled')."""
        self._cancel.set()

    def describe(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "state": self.state,
            "last_error": self.last_error,
            "synced": self.synced,
            "resyncs": self.resyncs,
        }

    # -- synchronisation --------------------------------------------------------
    def invalidate(self, reason: str = "") -> None:
        """Mark the current connection unusable (e.g. adapter init saw an unexpected reply)."""
        with self._lock:
            self.synced = False
            if reason:
                self.last_error = reason

    def mark_initialized(self) -> None:
        """Called by the client once the adapter has been (re)initialised on this connection."""
        self.initialized_generation = self.generation

    def prepare(self) -> int:
        """Resynchronise/reconnect if needed *before* a request; returns the connection generation.

        The client compares the result with the generation it initialised on and
        re-initialises the adapter before sending any vehicle request.
        """
        with self._lock:
            self._sync_and_connect()
            return self.generation

    def _sync_and_connect(self) -> None:
        if not self.synced:
            self.resyncs += 1
            self._resync()
            self.synced = True
        self._ensure_connected()

    # -- subclass hooks -------------------------------------------------------
    @abstractmethod
    def _ensure_connected(self) -> None: ...

    @abstractmethod
    def _roundtrip(self, wire: bytes, timeout_s: float) -> tuple[bytes, str]:
        """Send and read until the '>' prompt. Returns (raw bytes, status)."""

    @abstractmethod
    def _resync(self) -> None:
        """Discard the current (unsynchronised) connection; the next _ensure_connected opens a new one."""

    # -- the single entry point -----------------------------------------------
    def send(self, cmd: AllowedCommand, timeout_s: float | None = None) -> Exchange:
        if not isinstance(cmd, AllowedCommand):
            raise ForbiddenRequestError("transports only accept AllowedCommand objects")
        with self._lock:  # one outstanding ELM request at a time
            self._cancel.clear()
            if self._last_end is not None and self.min_interval_s > 0:
                wait = self.min_interval_s - (self.clock.monotonic() - self._last_end)
                self.clock.sleep(wait)
            self._sync_and_connect()
            if (
                not cmd.is_at
                and self.initialized_generation is not None
                and self.generation != self.initialized_generation
            ):
                # a reconnect happened since the adapter was initialised: ELM settings
                # (echo, headers, protocol) are unknown, so no vehicle request may go out
                raise TransportUnsyncedError(
                    "adapter connection was re-established; adapter must be re-initialised "
                    f"before sending {cmd.text}"
                )
            t_wall = self.clock.wall()
            t0 = self.clock.monotonic()
            raw, status = self._roundtrip(cmd.wire(), timeout_s or self.default_timeout_s)
            t1 = self.clock.monotonic()
            self._last_end = t1
            if status in INCOMPLETE:
                self.synced = False  # never reuse a connection whose stream position is unknown
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
