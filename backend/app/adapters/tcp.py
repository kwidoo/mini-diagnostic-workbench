"""Vgate iCar Pro Wi-Fi (ELM327-compatible) TCP transport.

Plain blocking sockets guarded by the base-class lock: the ELM protocol is
strictly request -> response-until-'>' so nothing more elaborate is needed.
"""

from __future__ import annotations

import socket

from app.adapters.base import Transport, TransportError

DEFAULT_HOST = "192.168.0.10"
DEFAULT_PORT = 35000


class VgateWiFiTransport(Transport):
    kind = "vgate-wifi-tcp"

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        connect_timeout_s: float = 5.0,
        default_timeout_s: float = 5.0,
        min_interval_s: float = 0.0,
        reconnect_attempts: int = 4,
        backoff_initial_s: float = 0.5,
        backoff_max_s: float = 8.0,
        auto_reconnect: bool = True,
        clock=None,
    ) -> None:
        super().__init__(clock=clock, min_interval_s=min_interval_s, default_timeout_s=default_timeout_s)
        self.host = host
        self.port = port
        self.connect_timeout_s = connect_timeout_s
        self.reconnect_attempts = reconnect_attempts
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.auto_reconnect = auto_reconnect
        self.reconnect_count = 0
        self._sock: socket.socket | None = None
        self._wanted = False  # user asked for a connection

    def describe(self) -> dict[str, object]:
        d = super().describe()
        d.update(host=self.host, port=self.port, reconnects=self.reconnect_count)
        return d

    # -- lifecycle ----------------------------------------------------------
    def _open(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout_s)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self.generation += 1
        self.state = "connected"
        self.last_error = None

    def connect(self) -> None:
        """Connection probe. Raises TransportError with an actionable message."""
        self._wanted = True
        self.state = "connecting"
        try:
            self._open()
        except OSError as exc:
            self.state = "disconnected"
            self.last_error = str(exc)
            raise TransportError(
                f"cannot reach adapter at {self.host}:{self.port} ({exc}). "
                "Check that the Mac is joined to the adapter's Wi-Fi network (default SSID V-LINK), "
                "the adapter is plugged into the OBD socket and the ignition is on."
            ) from exc

    def close(self) -> None:
        self._wanted = False
        self.cancel()
        self._drop()
        self.state = "disconnected"

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _ensure_connected(self) -> None:
        if self._sock is not None:
            return
        if not self._wanted:
            raise TransportError("not connected")
        if not self.auto_reconnect:
            raise TransportError("connection lost and auto-reconnect is disabled")
        self.state = "reconnecting"
        delay = self.backoff_initial_s
        last: OSError | None = None
        for _ in range(self.reconnect_attempts):
            try:
                self._open()
                self.reconnect_count += 1
                return
            except OSError as exc:
                last = exc
                self.clock.sleep(delay)
                delay = min(delay * 2, self.backoff_max_s)
            if not self._wanted:
                break
        self.state = "disconnected"
        self.last_error = str(last)
        raise TransportError(
            f"lost connection to {self.host}:{self.port}; {self.reconnect_attempts} reconnect attempts failed ({last})"
        )

    def _resync(self) -> None:
        # The ELM stream position is unknown (late reply may still be in flight):
        # never reuse this socket. _ensure_connected() opens a fresh one, which
        # bumps ``generation`` so the client re-initialises the adapter first.
        self._drop()
        if self._wanted:
            self.state = "reconnecting"

    # -- I/O ------------------------------------------------------------------
    def _roundtrip(self, wire: bytes, timeout_s: float) -> tuple[bytes, str]:
        sock = self._sock
        assert sock is not None
        buf = bytearray()
        try:
            # A synchronised connection must be silent between exchanges. Anything
            # waiting here is unsolicited (e.g. a late reply): do NOT send the request
            # on this connection; hand the stray bytes back as a 'desync' exchange.
            stray = bytearray()
            sock.setblocking(False)
            try:
                while chunk := sock.recv(4096):
                    stray.extend(chunk)
            except BlockingIOError:
                pass
            if stray:
                return bytes(stray), "desync"
            sock.settimeout(0.1)
            sock.sendall(wire)
            deadline = self.clock.monotonic() + timeout_s
            while True:
                if self._cancel.is_set():
                    return bytes(buf), "cancelled"
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    if self.clock.monotonic() >= deadline:
                        return bytes(buf), "timeout"
                    continue
                if not chunk:
                    raise ConnectionResetError("adapter closed the connection")
                buf.extend(chunk)
                if b">" in chunk:
                    # bytes after the prompt belong to no request we sent
                    trailing = bytes(buf[buf.index(b">") + 1 :])
                    return bytes(buf), "desync" if trailing.strip() else "ok"
        except OSError as exc:
            self.last_error = str(exc)
            self._drop()
            self.state = "disconnected" if not self._wanted else "reconnecting"
            return bytes(buf), "closed"
