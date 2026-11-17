"""Real sockets against the fake ELM327 TCP server."""

import dataclasses
import socket
import threading
import time

import pytest

from app.adapters.base import TransportClosedError, TransportError
from app.adapters.simulator import SCENARIOS, FakeElmServer
from app.adapters.tcp import VgateWiFiTransport
from app.obd.allowlist import AllowedCommand
from app.obd.client import ObdClient
from app.obd.elm import parse_reply


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_failure_has_actionable_message():
    t = VgateWiFiTransport("127.0.0.1", _free_port(), connect_timeout_s=0.5)
    with pytest.raises(TransportError, match="V-LINK"):
        t.connect()
    assert t.state == "disconnected"


def test_send_without_connect_is_refused():
    with pytest.raises(TransportError, match="not connected"):
        VgateWiFiTransport("127.0.0.1", _free_port()).send(AllowedCommand.at("ATI"))


def test_roundtrip_raw_bytes_timestamps_and_latency():
    with FakeElmServer("healthy_idle") as srv:
        t = VgateWiFiTransport(srv.host, srv.port)
        seen = []
        t.listeners.append(seen.append)
        t.connect()
        a = t.send(AllowedCommand.at("ATI"))
        b = t.send(AllowedCommand.at("ATRV"))
        t.close()
    assert a.request_hex == "4154490D" and a.response_text.endswith(">")
    assert bytes.fromhex(a.response_hex).decode("latin-1") == a.response_text
    assert a.status == "ok" and a.latency_ms > 0
    assert b.t_mono >= a.t_mono + a.latency_ms / 1000 and b.t_wall >= a.t_wall and b.seq == a.seq + 1
    assert seen == [a, b]


def test_requests_are_serialised_one_at_a_time():
    with FakeElmServer("healthy_idle") as srv:
        t = VgateWiFiTransport(srv.host, srv.port)
        c = ObdClient(t)
        c.initialize()
        exchanges = []
        t.listeners.append(exchanges.append)
        pids = [0x0C, 0x10, 0x0B, 0x05] * 3
        results = {}

        def worker(i, pid):
            results[i] = c.read_pid(pid)[0]

        threads = [threading.Thread(target=worker, args=(i, p)) for i, p in enumerate(pids)]
        [th.start() for th in threads]
        [th.join() for th in threads]
        t.close()
    assert all(results[i].pid == p and results[i].quality == "ok" for i, p in enumerate(pids))
    exchanges.sort(key=lambda e: e.t_mono)
    for a, b in zip(exchanges, exchanges[1:], strict=False):
        assert b.t_mono >= a.t_mono + a.latency_ms / 1000 - 1e-6  # never overlapping on the wire
        assert f"41 {b.request[2:4]}" in b.response_text  # each answer belongs to its own request


def test_rate_limit_min_interval():
    with FakeElmServer("healthy_idle") as srv:
        t = VgateWiFiTransport(srv.host, srv.port, min_interval_s=0.15)
        t.connect()
        a = t.send(AllowedCommand.at("ATI"))
        b = t.send(AllowedCommand.at("ATI"))
        t.close()
    assert b.t_mono - (a.t_mono + a.latency_ms / 1000) >= 0.149


def test_slow_adapter_times_out_then_recovers():
    with FakeElmServer("slow_adapter") as srv:
        t = VgateWiFiTransport(srv.host, srv.port, default_timeout_s=3)
        c = ObdClient(t)
        c.initialize()
        ex = t.send(AllowedCommand.obd(1, 0x0C), timeout_s=0.1)
        assert ex.status == "timeout"
        assert parse_reply(bytes.fromhex(ex.response_hex), "010C", True).status.value == "timeout"
        time.sleep(0.7)
        assert c.read_pid(0x0C)[0].quality == "ok"  # the late answer was drained, not mis-attributed
        t.close()


def test_drop_gives_partial_exchange_then_reconnects_with_backoff_and_reinit():
    sc = dataclasses.replace(SCENARIOS["wifi_drop"], drop_after_s=1.5)
    with FakeElmServer(sc) as srv:
        t = VgateWiFiTransport(srv.host, srv.port, backoff_initial_s=0.05)
        c = ObdClient(t)
        c.initialize()
        gen = t.generation
        closed = None
        deadline = time.monotonic() + 5
        while closed is None and time.monotonic() < deadline:
            try:
                c.read_pid(0x0C)
            except TransportClosedError as exc:
                closed = exc
        assert closed is not None and closed.exchange.status == "closed"
        assert ">" not in closed.exchange.response_text  # partial response kept as received
        r = c.read_pid(0x0C)[0]  # transparently reconnects and re-initialises the adapter
        assert r.quality == "ok" and r.ecu == "7E8"
        assert t.generation == gen + 1 and t.reconnect_count == 1 and srv.connections == 2
        t.close()


def test_reconnect_gives_up_with_clear_error():
    srv = FakeElmServer("healthy_idle").start()
    sleeps = []

    class Clock:
        monotonic = staticmethod(time.monotonic)
        wall = staticmethod(time.time)

        def sleep(self, s):
            sleeps.append(s)

    t = VgateWiFiTransport(
        srv.host, srv.port, reconnect_attempts=4, backoff_initial_s=0.5, backoff_max_s=1.5, clock=Clock()
    )
    t.connect()
    srv.stop()
    time.sleep(0.4)
    with pytest.raises(TransportClosedError):
        t.send(AllowedCommand.at("ATI"), timeout_s=0.5)
    with pytest.raises(TransportError, match="reconnect attempts failed"):
        t.send(AllowedCommand.at("ATI"), timeout_s=0.5)
    assert sleeps[-4:] == [0.5, 1.0, 1.5, 1.5]  # exponential, capped
    assert t.state == "disconnected"


def test_cancel_and_graceful_close():
    with FakeElmServer("slow_adapter") as srv:
        t = VgateWiFiTransport(srv.host, srv.port)
        t.connect()
        t.send(AllowedCommand.at("ATE0"))
        threading.Timer(0.05, t.cancel).start()
        ex = t.send(AllowedCommand.obd(1, 0x0C), timeout_s=5)
        assert ex.status == "cancelled" and ex.latency_ms < 2000
        t.close()
        t.close()  # idempotent
        assert t.state == "disconnected"
