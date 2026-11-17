"""P0-1 regression tests: an incomplete ELM exchange must never contaminate the next request.

All tests use real sockets against the fake ELM327 server. The server's ``script``
hook injects late, partial, trailing or unsolicited bytes exactly where a real
Wi-Fi adapter could produce them.
"""

import dataclasses
import threading
import time

import pytest

from app.adapters.base import TransportClosedError, TransportUnsyncedError
from app.adapters.simulator import SCENARIOS, FakeElmServer
from app.adapters.tcp import VgateWiFiTransport
from app.obd.allowlist import AllowedCommand
from app.obd.client import AdapterInitError, ObdClient

STALE = b"41 0C 0F A0 \r\r>"  # a plausible RPM reply (1000 rpm) that belongs to no request


def _counting_scenario():
    """Every RPM answer is different, so a reply can be matched to the request that produced it."""
    base = SCENARIOS["healthy_idle"]
    k = {"n": 0}

    def signals(t, rng):
        v = base.signals(t, rng)
        k["n"] += 1
        v[0x0C] = 1000 + 25 * (k["n"] % 400)
        return v

    return dataclasses.replace(base, name="counting", signals=signals, delay=(0.0, 0.0), second_ecu=False)


class Script:
    """Records what the server *would* answer to each 010C and lets one answer be rewritten."""

    def __init__(self, rewrite):
        self.rewrite = rewrite  # (index of 010C, normal raw) -> chunks | None
        self.rpm: list[tuple[float, bytes]] = []  # (arrival time, normal response)
        self.atz = 0

    def __call__(self, cmd, n, raw):
        if cmd == "ATZ":
            self.atz += 1
            return None
        if cmd != "010C":
            return None
        self.rpm.append((time.monotonic(), raw))
        return self.rewrite(len(self.rpm) - 1, raw)


def _client(srv, timeout=0.2):
    t = VgateWiFiTransport(srv.host, srv.port, backoff_initial_s=0.05)
    seen = []
    t.listeners.append(seen.append)
    c = ObdClient(t)
    c.initialize()
    t.default_timeout_s = timeout  # init keeps its own generous timeouts
    return t, c, seen


def _wait_connections(srv, n, timeout=2.0):
    end = time.monotonic() + timeout
    while srv.connections < n and time.monotonic() < end:
        time.sleep(0.01)
    return srv.connections


def _commands_on(srv, conn):
    return [cmd for k, cmd in srv.log if k == conn]


def _data_part(raw: bytes) -> str:
    return raw.decode("latin-1").replace(">", "").strip()


def _assert_reinit_before_obd(srv, conn):
    cmds = _commands_on(srv, conn)
    first_obd = next(i for i, c in enumerate(cmds) if not c.startswith("AT"))
    assert cmds[0] == "ATZ" and {"ATE0", "ATH1", "ATSP0"} <= set(cmds[:first_obd]), cmds


def test_late_reply_after_timeout_is_never_taken_as_next_result():
    late = 1.5  # longer than reconnect + ATZ (0.3 s in the simulator) + init
    script = Script(lambda i, raw: [(late, raw)] if i == 0 else None)
    with FakeElmServer(_counting_scenario(), script=script) as srv:
        t, c, seen = _client(srv, timeout=0.15)
        first = c.read_pid(0x0C)[0]
        assert first.quality == "timeout" and first.value is None
        assert seen[-1].status == "timeout" and t.synced is False
        second = c.read_pid(0x0C)[0]  # issued while the first reply is still in flight
        t.close()
    (t_first, stale_raw), (t_second, fresh_raw) = script.rpm
    assert t_second < t_first + late, "the new request must be sent before the old reply arrives"
    assert stale_raw != fresh_raw
    assert second.quality == "ok"
    assert seen[-1].request == "010C" and _data_part(fresh_raw) in seen[-1].response_text
    assert _data_part(stale_raw) not in seen[-1].response_text
    assert srv.connections == 2 and _commands_on(srv, 2).count("010C") == 1
    _assert_reinit_before_obd(srv, 2)


def test_cancelled_exchange_is_discarded_and_adapter_reinitialised():
    script = Script(lambda i, raw: [(1.5, raw)] if i == 0 else None)
    with FakeElmServer(_counting_scenario(), script=script) as srv:
        t, c, seen = _client(srv, timeout=5)
        threading.Timer(0.05, t.cancel).start()
        first = c.read_pid(0x0C)[0]
        assert seen[-1].status == "cancelled" and first.quality == "timeout" and first.value is None
        second = c.read_pid(0x0C)[0]
        t.close()
    stale_raw, fresh_raw = script.rpm[0][1], script.rpm[1][1]
    assert second.quality == "ok" and _data_part(fresh_raw) in seen[-1].response_text
    assert _data_part(stale_raw) not in seen[-1].response_text
    _assert_reinit_before_obd(srv, 2)


def test_partial_reply_without_prompt_is_kept_raw_and_never_decoded():
    def rewrite(i, raw):
        if i == 0:
            half = raw[: len(raw) // 2]
            return [(0.0, half), (0.5, raw[len(raw) // 2 :])]  # rest (with prompt) arrives too late
        return None

    script = Script(rewrite)
    with FakeElmServer(_counting_scenario(), script=script) as srv:
        t, c, seen = _client(srv, timeout=0.2)
        first = c.read_pid(0x0C)[0]
        partial = seen[-1]
        second = c.read_pid(0x0C)[0]
        t.close()
    stale_raw = script.rpm[0][1]
    assert (
        partial.status == "timeout" and partial.response_hex == stale_raw[: len(stale_raw) // 2].hex().upper()
    )
    assert first.quality == "timeout" and first.value is None
    assert second.quality == "ok" and srv.connections == 2
    _assert_reinit_before_obd(srv, 2)


def test_bytes_after_prompt_mark_desync_and_value_is_not_trusted():
    script = Script(lambda i, raw: [(0.0, raw + STALE)] if i == 0 else None)
    with FakeElmServer(_counting_scenario(), script=script) as srv:
        t, c, seen = _client(srv)
        first = c.read_pid(0x0C)[0]
        assert seen[-1].status == "desync" and first.quality == "error" and first.value is None
        assert "out-of-sequence" in first.detail
        second = c.read_pid(0x0C)[0]
        t.close()
    assert second.quality == "ok" and srv.connections == 2
    _assert_reinit_before_obd(srv, 2)


def test_unsolicited_bytes_between_exchanges_block_the_request():
    """A late reply that lands *between* two exchanges on a seemingly healthy connection."""

    armed = threading.Event()

    def script(cmd, n, raw):
        if cmd == "ATRV" and armed.is_set():
            armed.clear()  # once
            return [(0.0, raw), (0.15, STALE)]
        return None

    with FakeElmServer(_counting_scenario(), script=script) as srv:
        t, c, seen = _client(srv)
        armed.set()
        t.send(AllowedCommand.at("ATRV"))
        time.sleep(0.4)  # the stray reply is now sitting in the socket buffer
        r = c.read_pid(0x0C)[0]
        blocked = [e for e in seen if e.status == "desync"]
        assert blocked and blocked[0].response_text == STALE.decode()
        assert "010C" not in _commands_on(srv, 1)[-1:], "request must not be sent on a dirty connection"
        assert r.quality == "error" and r.value is None
        assert c.read_pid(0x0C)[0].quality == "ok"
        t.close()
    _assert_reinit_before_obd(srv, 2)


def test_connection_loss_reinitialises_before_the_next_vehicle_request():
    # the drop must not hit the ~0.9 s re-initialisation itself: an init interrupted by a drop
    # now fails as a whole (it is never continued on the next connection), see test_init_sync.py
    sc = dataclasses.replace(SCENARIOS["wifi_drop"], drop_after_s=2.0)
    with FakeElmServer(sc) as srv:
        t, c, seen = _client(srv, timeout=2)
        closed = None
        deadline = time.monotonic() + 5
        while closed is None and time.monotonic() < deadline:
            try:
                c.read_pid(0x0C)
            except TransportClosedError as exc:
                closed = exc
        assert closed is not None and ">" not in closed.exchange.response_text  # partial kept raw
        assert c.read_pid(0x0C)[0].quality == "ok"
        t.close()
    _assert_reinit_before_obd(srv, 2)


def test_transport_refuses_vehicle_request_until_reinitialised():
    with FakeElmServer(_counting_scenario()) as srv:
        t, c, _ = _client(srv)
        t.invalidate("test")
        with pytest.raises(TransportUnsyncedError):
            t.send(AllowedCommand.obd(1, 0x0C))  # raw send bypassing the client
        assert _wait_connections(srv, 2) == 2 and "010C" not in _commands_on(srv, 2)
        assert c.read_pid(0x0C)[0].quality == "ok"  # the client re-initialises first
        t.close()
    _assert_reinit_before_obd(srv, 2)


def test_stale_reply_read_as_atz_answer_fails_init_and_next_attempt_is_clean():
    """Adapter keeps forwarding an old ECU answer to the new TCP client after a reconnect."""
    state = {"atz": 0}
    armed = threading.Event()

    def script(cmd, n, raw):
        if cmd == "ATZ":
            state["atz"] += 1
            if state["atz"] == 2:
                return [(0.0, STALE), (0.05, raw)]
        if cmd == "010C" and armed.is_set() and state["atz"] == 1:
            return [(1.5, raw)]  # first RPM read times out
        return None

    with FakeElmServer(_counting_scenario(), script=script) as srv:
        t, c, _ = _client(srv, timeout=0.15)
        armed.set()
        assert c.read_pid(0x0C)[0].quality == "timeout"
        with pytest.raises(AdapterInitError, match="ELM327 identity"):
            c.read_pid(0x0C)
        assert t.synced is False
        r = c.read_pid(0x0C)[0]
        t.close()
    assert r.quality == "ok" and srv.connections == 3
    _assert_reinit_before_obd(srv, 3)


@pytest.mark.parametrize("status", ["timeout", "cancelled", "closed", "desync"])
def test_incomplete_exchange_never_decodes_to_a_value_live_or_replay(status):
    """Even a byte-perfect reply is not trusted when its exchange did not complete cleanly."""
    from app.models.domain import Exchange
    from app.obd.client import decode_exchange, interpret_pid

    raw = b"7E8 04 41 0C 0F A0 \r\r>"
    ex = Exchange(
        t_wall=0, t_mono=0, request="010C", request_hex="", response_hex=raw.hex(),
        response_text=raw.decode(), latency_ms=1, status=status,
    )  # fmt: skip
    r = interpret_pid(1, 0x0C, decode_exchange(ex, "can", False))[0]
    assert r.value is None and r.quality in ("timeout", "disconnected", "error")
    ok = interpret_pid(1, 0x0C, decode_exchange(ex.model_copy(update={"status": "ok"}), "can", False))[0]
    assert ok.value == 1000.0


def test_inprocess_simulator_timeout_also_forces_reinit():
    from app.adapters.simulator import SimulatorTransport

    t = SimulatorTransport("slow_adapter", default_timeout_s=2.0)
    c = ObdClient(t)
    c.initialize()
    gen = t.generation
    assert c.query(AllowedCommand.obd(1, 0x0C), timeout_s=0.01)[0].status == "timeout"
    assert c.read_pid(0x0C)[0].quality == "ok"
    assert t.generation == gen + 1 and t.sim.received.count("ATZ") == 2
