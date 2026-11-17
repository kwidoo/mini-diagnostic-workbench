"""PR #1 follow-up 2 (P0-1, P0-2): adapter initialisation is all-or-nothing per connection.

Every test runs real sockets against the fake ELM327 server and asserts on the
server's on-wire command log (``srv.log``), not on client-side bookkeeping.
"""

import pytest

from app.adapters.base import TransportError, TransportUnsyncedError
from app.adapters.simulator import FakeElmServer, SimulatorTransport
from app.adapters.tcp import VgateWiFiTransport
from app.obd.allowlist import AllowedCommand, ForbiddenRequestError
from app.obd.client import AdapterInitError, ObdClient, ObdError

FULL_INIT = [
    "ATZ",
    "ATE0",
    "ATL0",
    "ATS1",
    "ATH1",
    "ATAT1",
    "ATSP0",
    "ATI",
    "AT@1",
    "ATRV",
    "0100",
    "ATDPN",
    "ATDP",
]
STALE = b"41 0C 0F A0 \r\r>"


class Once:
    """Rewrite the reply to ``cmd`` the first ``times`` times it is seen."""

    def __init__(self, cmd, rewrite, times=1):
        self.cmd, self.rewrite, self.left = cmd, rewrite, times

    def __call__(self, cmd, n, raw):
        if cmd == self.cmd and self.left > 0:
            self.left -= 1
            return self.rewrite(raw)
        return None


def _on(srv, conn):
    return [c for k, c in srv.log if k == conn]


def _vehicle_requests(cmds):
    return [c for c in cmds if not c.startswith("AT")]


def _transport(srv):
    return VgateWiFiTransport(srv.host, srv.port, backoff_initial_s=0.05)


def _client(t):
    return ObdClient(t, at_timeout_s=0.3)


def _assert_full_init_then_ok(srv, conn, c):
    r = c.read_pid(0x0C)[0]
    assert r.quality == "ok" and r.value is not None
    assert _on(srv, conn) == FULL_INIT + ["010C"], _on(srv, conn)


# -- P0-1: incomplete / desynchronised initialisation exchanges ---------------------


def test_ate0_ok_without_prompt_aborts_init_and_retry_restarts_from_atz():
    script = Once("ATE0", lambda raw: [(0.0, b"OK\r\r")])  # 'OK' text, but no prompt ever
    with FakeElmServer("healthy_idle", script=script) as srv:
        t = _transport(srv)
        c = _client(t)
        with pytest.raises(AdapterInitError, match="incomplete adapter reply to ATE0"):
            c.initialize()
        assert c.adapter.init_log[-1]["command"] == "ATE0"  # raw exchange kept for diagnosis
        assert t.describe()["init_state"] == "failed" and t.describe()["init_failures"] == 1
        assert _on(srv, 1) == ["ATZ", "ATE0"]  # nothing after the broken exchange
        with pytest.raises(ObdError, match="not initialised"):
            c.read_pid(0x0C)  # never initialised: no vehicle request
        with pytest.raises(TransportUnsyncedError):
            t.send(AllowedCommand.obd(1, 0x0C))  # nor a bypassing raw one
        assert not _vehicle_requests([cmd for _, cmd in srv.log])
        c.initialize()  # the retry: fresh connection, from ATZ
        assert t.describe()["init_state"] == "initialized"
        _assert_full_init_then_ok(srv, srv.connections, c)
        t.close()


@pytest.mark.parametrize(
    "rewrite,status",
    [
        (lambda raw: [(1.0, raw)], "timeout"),  # optional ATL0 answered too late
        (lambda raw: [(0.0, raw + STALE)], "desync"),  # optional ATL0 + bytes after the prompt
        (lambda raw: [(0.0, b"OK\r"), (1.0, b"\r>")], "timeout"),  # 'OK' arrives, prompt doesn't
    ],
)
def test_optional_atl0_incomplete_is_fatal_to_the_attempt(rewrite, status):
    with FakeElmServer("healthy_idle", script=Once("ATL0", rewrite)) as srv:
        t = _transport(srv)
        seen = []
        t.listeners.append(seen.append)
        c = _client(t)
        with pytest.raises(AdapterInitError, match="incomplete adapter reply to ATL0"):
            c.initialize()
        assert seen[-1].request == "ATL0" and seen[-1].status == status
        assert _on(srv, 1) == ["ATZ", "ATE0", "ATL0"]
        assert t.synced is False and t.initialized_generation is None
        c.initialize()
        assert srv.connections == 2 and _on(srv, 1) == ["ATZ", "ATE0", "ATL0"]
        _assert_full_init_then_ok(srv, 2, c)
        t.close()


def test_optional_command_answered_with_question_mark_is_accepted():
    with FakeElmServer("healthy_idle", script=Once("ATAT1", lambda raw: [(0.0, b"?\r\r>")])) as srv:
        t = _transport(srv)
        c = _client(t)
        c.initialize()
        _assert_full_init_then_ok(srv, 1, c)
        t.close()


@pytest.mark.parametrize("cmd", ["ATE0", "ATH1", "ATSP0"])
def test_required_command_answered_with_question_mark_fails(cmd):
    with FakeElmServer("healthy_idle", script=Once(cmd, lambda raw: [(0.0, b"?\r\r>")])) as srv:
        t = _transport(srv)
        with pytest.raises(AdapterInitError, match=f"rejected {cmd}"):
            _client(t).initialize()
        assert not _vehicle_requests(_on(srv, 1))
        t.close()


@pytest.mark.parametrize("cmd", ["ATI", "ATRV"])
def test_ambiguous_informational_reply_fails_visibly(cmd):
    with FakeElmServer("healthy_idle", script=Once(cmd, lambda raw: [(0.0, b"41 0C 0F A0\r\r>")])) as srv:
        t = _transport(srv)
        with pytest.raises(AdapterInitError, match=f"unexpected {cmd} reply"):
            _client(t).initialize()
        assert not _vehicle_requests(_on(srv, 1))
        t.close()


def test_incomplete_0100_probe_and_protocol_readback_are_fatal():
    with FakeElmServer("healthy_idle", script=Once("0100", lambda raw: [(0.0, raw[:8])])) as srv:
        t = _transport(srv)
        c = _client(t)
        c.probe_timeout_s = 0.3
        with pytest.raises(AdapterInitError, match="incomplete adapter reply to 0100"):
            c.initialize()
        assert _on(srv, 1)[-1] == "0100" and t.initialized_generation is None
        t.close()
    with FakeElmServer("healthy_idle", script=Once("ATDPN", lambda raw: [(0.0, b"A6\r")])) as srv:
        t = _transport(srv)
        c = _client(t)
        with pytest.raises(AdapterInitError, match="incomplete adapter reply to ATDPN"):
            c.initialize()
        assert _on(srv, 1)[-1] == "ATDPN" and t.initialized_generation is None
        t.close()


def test_connection_drop_during_init_is_not_continued_on_the_new_connection():
    with FakeElmServer("healthy_idle", script=Once("ATS1", lambda raw: [(0.0, b"O"), (0.0, None)])) as srv:
        t = _transport(srv)
        c = _client(t)
        with pytest.raises(TransportError):
            c.initialize()
        assert _on(srv, 1) == ["ATZ", "ATE0", "ATL0", "ATS1"]
        assert srv.connections == 1, "the attempt must not reconnect and carry on with ATH1..."
        c.initialize()
        _assert_full_init_then_ok(srv, 2, c)
        t.close()


def test_failed_reinit_after_resync_sends_no_vehicle_request_then_recovers():
    """Re-init triggered by a timed-out read fails half-way; the vehicle request is withheld."""
    armed = {"rpm": True, "ate0": False}

    def script(cmd, n, raw):
        if cmd == "010C" and armed["rpm"]:
            armed["rpm"], armed["ate0"] = False, True
            return [(1.0, raw)]  # times out -> connection unsynchronised
        if cmd == "ATE0" and armed["ate0"]:
            armed["ate0"] = False
            return [(0.0, b"OK\r\r")]  # re-init on connection 2 breaks
        return None

    with FakeElmServer("healthy_idle", script=script) as srv:
        t = _transport(srv)
        c = _client(t)
        c.initialize()
        t.default_timeout_s = 0.2
        assert c.read_pid(0x0C)[0].quality == "timeout"
        with pytest.raises(AdapterInitError, match="ATE0"):
            c.read_pid(0x0C)
        assert _on(srv, 2) == ["ATZ", "ATE0"]
        r = c.read_pid(0x0C)[0]  # one more attempt, on connection 3, from ATZ
        assert r.quality == "ok" and r.value is not None
        assert srv.connections == 3 and _on(srv, 3) == FULL_INIT + ["010C"]
        t.close()


def test_init_attempt_is_pinned_to_its_connection():
    with FakeElmServer("healthy_idle") as srv:
        t = _transport(srv)
        t.connect()
        attempt = t.begin_initialization()
        attempt.send(AllowedCommand.at("ATZ"), 2.0)
        t.invalidate("test")  # e.g. a concurrent failure
        with pytest.raises(TransportUnsyncedError, match="restart initialisation from ATZ"):
            attempt.send(AllowedCommand.at("ATE0"))
        with pytest.raises(TransportUnsyncedError):
            attempt.complete()
        assert [cmd for _, cmd in srv.log] == ["ATZ"]  # ATE0 never left the client
        newer = t.begin_initialization()
        assert newer.generation == attempt.generation + 1
        with pytest.raises(TransportUnsyncedError, match="no longer current"):
            attempt.complete()  # a stale attempt can never authorise a newer connection
        t.close()


# -- P0-2: None is not an initialised generation ----------------------------------------


def test_fresh_connection_refuses_direct_vehicle_request():
    with FakeElmServer("healthy_idle") as srv:
        t = _transport(srv)
        t.connect()
        assert t.initialized_generation is None
        with pytest.raises(TransportUnsyncedError):
            t.send(AllowedCommand.obd(1, 0x0C))
        assert t.send(AllowedCommand.at("ATRV")).status == "ok"  # adapter-only AT still allowed
        assert [cmd for _, cmd in srv.log] == ["ATRV"]  # 010C never transmitted
        t.close()


def test_bootstrap_path_only_allows_at_and_0100():
    with FakeElmServer("healthy_idle") as srv:
        t = _transport(srv)
        t.connect()
        attempt = t.begin_initialization()
        with pytest.raises(ForbiddenRequestError):
            attempt.send(AllowedCommand.obd(1, 0x0C))
        assert not srv.log
        t.close()


def test_guard_restored_after_invalidation_and_reconnect():
    with FakeElmServer("healthy_idle") as srv:
        t = _transport(srv)
        c = _client(t)
        c.initialize()
        assert c.read_pid(0x0C)[0].quality == "ok"
        t.invalidate("test")
        with pytest.raises(TransportUnsyncedError):
            t.send(AllowedCommand.obd(1, 0x0C))  # after invalidation (reconnects, then refuses)
        assert "010C" not in _on(srv, 2)
        t._drop()  # connection lost between requests
        with pytest.raises(TransportUnsyncedError):
            t.send(AllowedCommand.obd(1, 0x0C))  # after reconnect, before re-init
        assert "010C" not in _on(srv, 3)
        assert c.read_pid(0x0C)[0].quality == "ok"  # the client re-initialises first
        assert _on(srv, 3) == FULL_INIT + ["010C"]
        t.close()


def test_inprocess_simulator_enforces_the_same_guard():
    t = SimulatorTransport("healthy_idle")
    t.connect()
    with pytest.raises(TransportUnsyncedError):
        t.send(AllowedCommand.obd(1, 0x0C))
    assert "010C" not in t.sim.received
    c = ObdClient(t)
    c.initialize()
    assert c.read_pid(0x0C)[0].quality == "ok"
