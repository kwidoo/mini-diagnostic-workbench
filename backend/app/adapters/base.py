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
on the new connection.

Initialisation rule (see ``ObdClient.initialize``): adapter setup runs through an
:class:`InitAttempt` obtained from :meth:`Transport.begin_initialization`. An
attempt is pinned to ONE fresh connection generation; it never reconnects or
resynchronises. If any of its exchanges is incomplete, or the connection changes
underneath it, every further step of that attempt is refused and the attempt can
never be completed - the next attempt starts again with ATZ on a new connection.
Ordinary vehicle requests (:meth:`Transport.send`) are refused unless the current
generation was explicitly completed by an attempt; ``None`` never qualifies.
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

# the only vehicle request an initialisation attempt may send: the generic bus-up probe
BOOTSTRAP_REQUEST = "0100"


class InitAttempt:
    """Private initialisation path: AT commands + the single 0100 bootstrap probe,
    all on the one connection generation the attempt was started on."""

    def __init__(self, transport: Transport, generation: int) -> None:
        self._transport = transport
        self.generation = generation

    def send(self, cmd: AllowedCommand, timeout_s: float | None = None) -> Exchange:
        return self._transport._send_init(self, cmd, timeout_s)

    def complete(self) -> None:
        """Declare the adapter initialised on this generation (vehicle requests allowed)."""
        self._transport._complete_init(self)


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
        self.initialized_generation: int | None = None  # generation a completed InitAttempt ran on
        self.resyncs = 0
        self.init_state = "none"  # none | initializing | initialized | failed
        self.init_failures = 0
        self._attempt: InitAttempt | None = None  # the only attempt allowed to send/complete
        self._tainted_generation: int | None = None  # connection that lost sync (must be discarded)
        self.manufacturer_transactions = 0  # DDE-03 guarded transactions sent on this transport
        self.manufacturer_restore_failures = 0  # ... whose baseline restore failed (connection discarded)

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
            "init_state": self.init_state,
            "init_failures": self.init_failures,
            "manufacturer_transactions": self.manufacturer_transactions,
            "manufacturer_restore_failures": self.manufacturer_restore_failures,
        }

    # -- synchronisation --------------------------------------------------------
    def invalidate(self, reason: str = "") -> None:
        """Mark the current connection unusable (e.g. adapter init saw an unexpected reply)."""
        with self._lock:
            self.synced = False
            self._tainted_generation = self.generation
            self.initialized_generation = None
            if reason:
                self.last_error = reason

    def fail_initialization(self, reason: str) -> None:
        """An initialisation attempt failed: the connection is discarded before any further use."""
        with self._lock:
            self.init_failures += 1
            self.init_state = "failed"
        self.invalidate(reason)

    def begin_initialization(self) -> InitAttempt:
        """Start a new initialisation attempt; any previous initialisation is revoked.

        An unsynchronised connection (incomplete exchange, failed/partial earlier
        attempt) is discarded first, so the attempt - which starts with ATZ - always
        runs on a connection whose stream position is known.
        """
        with self._lock:
            self.initialized_generation = None
            self._attempt = None
            self._sync_and_connect()
            self.init_state = "initializing"
            self._attempt = InitAttempt(self, self.generation)
            return self._attempt

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
            # discard the connection that lost sync; a connection opened since then
            # (e.g. an explicit connect()) has carried nothing and is already clean
            if self._tainted_generation == self.generation:
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

    # -- the entry points ------------------------------------------------------
    def send(self, cmd: AllowedCommand, timeout_s: float | None = None) -> Exchange:
        """Ordinary request. Vehicle (non-AT) requests require a completed initialisation
        on the current connection generation."""
        if not isinstance(cmd, AllowedCommand):
            raise ForbiddenRequestError("transports only accept AllowedCommand objects")
        with self._lock:  # one outstanding ELM request at a time
            self._sync_and_connect()
            if not cmd.is_at and (
                self.initialized_generation is None or self.generation != self.initialized_generation
            ):
                # never initialised, invalidated, or reconnected since: ELM settings (echo,
                # headers, protocol) are unknown, so no vehicle request may go out
                raise TransportUnsyncedError(
                    "adapter is not initialised on the current connection; "
                    f"it must be (re-)initialised before sending {cmd.text}"
                )
            return self._exchange(cmd, timeout_s)

    def send_manufacturer(self, approved, timeout_s: float | None = None) -> list[Exchange]:
        """DDE-03: one manufacturer transaction - set-up, request, restore - under ONE lock hold, so
        no generic request can run in between. Only an ``ApprovedRequest`` (built by
        ``app.manufacturer.bmw.guarded.approve`` from an executable catalog entry) is accepted.

        Set-up and restore commands must each answer exactly ``OK``. A failed set-up skips the
        request. The restore sequence always runs while the connection is usable; if it fails, or
        the connection was lost / desynchronised, the connection is invalidated so the next
        request re-initialises the adapter from ATZ (fail closed: addressing never leaks into
        generic OBD). Returns every exchange in order; a dropped connection re-raises
        :class:`TransportClosedError` after the clean-up, with ``exchanges`` attached.
        """
        from app.manufacturer.bmw.guarded import ApprovedRequest, at_ok

        if not isinstance(approved, ApprovedRequest):
            raise ForbiddenRequestError("manufacturer requests must be ApprovedRequest objects")
        with self._lock:
            self._sync_and_connect()
            if self.initialized_generation is None or self.generation != self.initialized_generation:
                raise TransportUnsyncedError(
                    "adapter is not initialised on the current connection; no manufacturer request is sent"
                )
            exs: list[Exchange] = []
            closed: TransportClosedError | None = None
            restore_ok = True
            try:
                setup_ok = True
                for cmd in approved.setup:
                    ex = self._exchange(cmd, None)
                    exs.append(ex)
                    if not at_ok(ex):
                        setup_ok = False
                        break
                if setup_ok:
                    exs.append(self._exchange(approved.request, timeout_s))
            except TransportClosedError as exc:
                closed = exc
                if exc.exchange is not None:
                    exs.append(exc.exchange)
            if closed is None and self.synced and self.state == "connected":
                try:
                    for cmd in approved.restore:
                        ex = self._exchange(cmd, None)
                        exs.append(ex)
                        if not at_ok(ex):
                            restore_ok = False
                            break
                except TransportClosedError as exc:
                    closed = exc
                    if exc.exchange is not None:
                        exs.append(exc.exchange)
            if closed is not None or not self.synced or not restore_ok:
                self.manufacturer_restore_failures += 1
                reason = (
                    "manufacturer addressing could not be restored to the generic baseline; the adapter is "
                    "re-initialised (ATZ) before the next request"
                )
                self.synced = False
                self._tainted_generation = self.generation
                self.initialized_generation = None
                self.last_error = reason
            self.manufacturer_transactions += 1
        if closed is not None:
            closed.exchanges = exs  # type: ignore[attr-defined]
            raise closed
        return exs

    def _send_init(self, attempt: InitAttempt, cmd: AllowedCommand, timeout_s: float | None) -> Exchange:
        if not isinstance(cmd, AllowedCommand):
            raise ForbiddenRequestError("transports only accept AllowedCommand objects")
        if not cmd.is_at and cmd.text != BOOTSTRAP_REQUEST:
            raise ForbiddenRequestError(f"initialisation may only send AT commands and {BOOTSTRAP_REQUEST}")
        with self._lock:
            # never resync/reconnect here: an attempt must not continue on another connection
            self._check_attempt(attempt, f"send {cmd.text}")
            return self._exchange(cmd, timeout_s)

    def _complete_init(self, attempt: InitAttempt) -> None:
        with self._lock:
            self._check_attempt(attempt, "complete initialisation")
            self.initialized_generation = attempt.generation
            self.init_state = "initialized"
            self._attempt = None

    def _check_attempt(self, attempt: InitAttempt, what: str) -> None:
        if attempt is not self._attempt:
            raise TransportUnsyncedError(f"cannot {what}: initialisation attempt is no longer current")
        if not self.synced or self.generation != attempt.generation or self.state != "connected":
            self._attempt = None
            raise TransportUnsyncedError(
                f"cannot {what}: connection changed or lost sync during initialisation "
                f"(attempt on connection #{attempt.generation}); restart initialisation from ATZ"
            )

    def _exchange(self, cmd, timeout_s: float | None) -> Exchange:
        """One request -> response-until-prompt on the current connection (lock held). ``cmd`` is an
        AllowedCommand, or a GuardedCommand inside send_manufacturer (never a string)."""
        self._cancel.clear()
        if self._last_end is not None and self.min_interval_s > 0:
            wait = self.min_interval_s - (self.clock.monotonic() - self._last_end)
            self.clock.sleep(wait)
        t_wall = self.clock.wall()
        t0 = self.clock.monotonic()
        raw, status = self._roundtrip(cmd.wire(), timeout_s or self.default_timeout_s)
        t1 = self.clock.monotonic()
        self._last_end = t1
        if status in INCOMPLETE:
            self.synced = False  # never reuse a connection whose stream position is unknown
            self._tainted_generation = self.generation
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
