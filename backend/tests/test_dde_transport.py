"""DDE-03: narrow, typed manufacturer transport. Fake ELM over real TCP sockets throughout;
nothing here says anything about a real adapter or a real DDE."""

import dataclasses
import threading

import pytest

from app.adapters.simulator import FakeElmServer, SimulatorTransport
from app.adapters.tcp import VgateWiFiTransport
from app.manufacturer.bmw.catalog import ExecutionContext
from app.manufacturer.bmw.guarded import (
    ApprovedRequest,
    GuardedCommand,
    NotApprovedError,
    UnsupportedAdapterError,
    _cmd,
    approve,
)
from app.manufacturer.bmw.runtime import DdeRuntime
from app.manufacturer.bmw.synthetic import SYN_ADDR, SYNTHETIC_VARIANT, synthetic_catalog
from app.obd.allowlist import AllowedCommand, ForbiddenRequestError
from app.obd.client import ObdClient
from app.service import Workbench

SYN_OK = ExecutionContext(opted_in=True, confirmed_variant=SYNTHETIC_VARIANT, synthetic_session=True)
SETUP = ["ATSH7B0", "ATCEA5A", "ATCRA7B8", "ATFCSH7B0", "ATFCSD5A300000", "ATFCSM1"]
RESTORE = ["ATSH7DF", "ATCEA", "ATCRA", "ATFCSM0"]


@pytest.fixture
def tcp():
    servers = []

    def make(scenario="mini_dde_synthetic", script=None, timeout=2.0):
        srv = FakeElmServer(scenario, script=script).start()
        servers.append(srv)
        t = VgateWiFiTransport(srv.host, srv.port, default_timeout_s=timeout, backoff_initial_s=0.05)
        c = ObdClient(t)
        c.initialize()
        c.discover()
        return srv, c

    yield make
    for s in servers:
        s.stop()


def _cmds(srv, start=0):
    return [c for _, c in srv.log[start:]]


def test_guarded_objects_cannot_be_built_outside_approve():
    with pytest.raises(ForbiddenRequestError):
        GuardedCommand("ATSH7E0", "setup")
    with pytest.raises(ForbiddenRequestError):
        ApprovedRequest(None, "v", (), None, ())  # type: ignore[arg-type]
    for text, role in (
        ("2E0101FF", "request"),  # write
        ("2F010203", "request"),  # actuation
        ("3101FF00", "request"),  # routine
        ("1003", "request"),  # session change
        ("1101", "request"),  # reset
        ("14FFFFFF", "request"),  # clear
        ("2701", "request"),  # security access
        ("04", "request"),
        ("08", "request"),
        ("ATMA", "setup"),
        ("ATD", "restore"),
        ("ATPP0E", "setup"),
        ("ATSH7E0", "request"),
        ("ATSH18DAF110", "setup"),  # 29-bit header not allowed
        ("22F190", "setup"),
    ):
        with pytest.raises(ForbiddenRequestError):
            _cmd(text, role)
    # the generic allowlist is unchanged: still no manufacturer service, no addressing AT command
    for text in ("22F190", "1802FFFF", "ATSH7B0", "ATCEA5A", "ATCRA7B8", "ATFCSM1", "04", "0800", "2701"):
        with pytest.raises(ForbiddenRequestError):
            AllowedCommand.parse(text)


def test_transport_rejects_anything_but_an_approved_request():
    t = SimulatorTransport("mini_dde_synthetic")
    with pytest.raises(ForbiddenRequestError):
        t.send_manufacturer("ATSH7B0")  # type: ignore[arg-type]


def test_approve_derives_exact_setup_and_restore_and_refuses_unfit_adapters():
    cat = synthetic_catalog()
    can = {"is_can": True, "protocol_number": "A6"}
    ap = approve(cat, "syn.dpf_soot_mass", SYN_OK, can)
    assert [c.text for c in ap.setup] == SETUP and ap.request.text == "22F102"
    assert [c.text for c in ap.restore] == RESTORE
    for adapter in (
        {"is_can": False, "protocol_number": "A3"},
        {"is_can": True, "protocol_number": "A7"},
        None,
    ):
        with pytest.raises(UnsupportedAdapterError):
            approve(cat, "syn.dpf_soot_mass", SYN_OK, adapter)
    for ctx in (
        dataclasses.replace(SYN_OK, opted_in=False),
        dataclasses.replace(SYN_OK, confirmed_variant="OTHER"),
        dataclasses.replace(
            SYN_OK, synthetic_session=False, adapter_verdict="manufacturer_transport_candidate"
        ),
    ):
        with pytest.raises(NotApprovedError):
            approve(cat, "syn.dpf_soot_mass", ctx, can)
    with pytest.raises(NotApprovedError):
        approve(cat, "no.such.entry", SYN_OK, can)


def test_wrong_address_payload_or_variant_is_rejected_before_transmission(tcp):
    srv, c = tcp()
    base = synthetic_catalog().entries["syn.dpf_soot_mass"]
    bad = [
        dataclasses.replace(
            base, request_id="bad.addr", addressing=dataclasses.replace(SYN_ADDR, tx_can_id="18DA")
        ),
        dataclasses.replace(base, request_id="bad.payload", payload_hex="2EF102"),
        dataclasses.replace(base, request_id="bad.variant", compatible_variants=("OTHER",)),
        dataclasses.replace(base, request_id="bad.disabled", enabled=False),
    ]
    from app.manufacturer.bmw.catalog import RequestCatalog
    from app.manufacturer.bmw.synthetic import SYNTHETIC_SOURCE

    cat = RequestCatalog(bad, (SYNTHETIC_SOURCE,), allow_synthetic=True)
    rt = DdeRuntime(cat, SYN_OK, c)
    n = len(srv.log)
    for e in bad:
        r = rt.read(e.request_id)
        assert r.quality == "not_attempted" and r.value is None
    assert len(srv.log) == n  # nothing reached the adapter


def test_approved_read_over_tcp_then_baseline_restored(tcp):
    srv, c = tcp()
    rt = DdeRuntime(synthetic_catalog(), SYN_OK, c)
    n = len(srv.log)
    r = rt.read("syn.dpf_soot_mass")
    assert r.quality == "ok" and r.value is not None and r.unit == "g" and r.ecu == "7B8/F5"
    assert r.catalog_version.startswith("dde-catalog/synthetic") and r.decoder_version == "1"
    assert _cmds(srv, n) == [*SETUP, "22F102", *RESTORE]
    assert srv.sim.baseline()
    # generic OBD afterwards is unaffected and needs no re-initialisation
    (rpm,) = [x for x in c.read_pid(0x0C) if x.ecu == "7E8"]
    assert rpm.quality == "ok" and "ATZ" not in _cmds(srv, n)
    # multi-frame manufacturer answer (identification text)
    ident = rt.read("syn.identification")
    assert ident.quality == "ok" and ident.text.startswith("SYNTHETIC-DDE-0")
    assert c.transport.describe()["manufacturer_transactions"] == 2


def test_negative_response_and_no_data_are_not_values(tcp):
    srv, c = tcp()
    base = synthetic_catalog().entries["syn.dpf_soot_mass"]
    unknown = dataclasses.replace(
        base, request_id="syn.unknown", metric="x", payload_hex="22AAAA",
        response=dataclasses.replace(base.response, echo_hex="AAAA"),
    )  # fmt: skip
    rt = DdeRuntime(synthetic_catalog((unknown,)), SYN_OK, c)
    neg = rt.read("syn.unknown")
    assert neg.quality == "negative" and neg.nrc == "31" and neg.value is None
    assert srv.sim.baseline()
    # ECU silent: NO DATA
    srv.sim.scenario = dataclasses.replace(srv.sim.scenario, ecu_online=lambda t: False)
    nd = rt.read("syn.dpf_soot_mass")
    assert nd.quality == "no_data" and nd.value is None and srv.sim.baseline()


def _script_on(match, chunks):
    def script(cmd, n, normal):
        return chunks(normal) if cmd == match else None

    return script


@pytest.mark.parametrize(
    "chunks,expect",
    [
        # partial ISO-TP: first frame only
        (lambda normal: [(0, b"7B8 F5 10 1A 62 F1 00 53 59 \r\r>")], "error"),
        # frame from another CAN id: whole reply untrusted
        (lambda normal: [(0, b"7E8 F5 05 62 F1 00 41 42 00 \r\r>")], "error"),
        # wrong extended address byte
        (lambda normal: [(0, b"7B8 F1 05 62 F1 00 41 42 00 \r\r>")], "error"),
        # bytes after the prompt (late / extra data)
        (lambda normal: [(0, normal + b"7B8 F5 03 62 F1\r>")], "error"),
        # adapter buffer / bus problem
        (lambda normal: [(0, b"CAN ERROR\r\r>")], "error"),
    ],
)
def test_framing_errors_never_produce_values(tcp, chunks, expect):
    srv, c = tcp(script=_script_on("22F100", chunks))
    rt = DdeRuntime(synthetic_catalog(), SYN_OK, c)
    r = rt.read("syn.identification")
    assert r.quality == expect and r.value is None and r.text is None
    (rpm,) = [x for x in c.read_pid(0x0C) if x.ecu == "7E8"]
    assert rpm.quality == "ok"
    assert srv.sim.baseline()  # restored, or the adapter was reset by ATZ before the generic read


def test_timeout_or_drop_during_request_fails_closed(tcp):
    srv, c = tcp(script=_script_on("22F102", lambda normal: [(1.5, normal)]), timeout=0.5)
    rt = DdeRuntime(synthetic_catalog(), SYN_OK, c)
    n = len(srv.log)
    r = rt.read("syn.dpf_soot_mass")
    assert r.quality == "timeout" and r.value is None
    sent = _cmds(srv, n)
    assert "ATSH7DF" not in sent  # restore is never sent on a desynchronised link ...
    assert c.transport.describe()["manufacturer_restore_failures"] == 1
    # ... instead the next request re-initialises from ATZ on a fresh connection
    rpm = c.read_pid(0x0C)
    later = _cmds(srv, n + len(sent))
    assert later[0] == "ATZ" and later.index("ATZ") < later.index("010C")
    assert any(x.quality == "ok" for x in rpm) and srv.sim.baseline()
    # a DDE read never triggers that re-initialisation by itself
    c.transport.invalidate("test")
    n2 = len(srv.log)
    assert rt.read("syn.dpf_soot_mass").quality == "not_attempted" and len(srv.log) == n2

    srv2, c2 = tcp(script=_script_on("22F102", lambda normal: [(0, normal[:10]), (0.05, None)]))
    rt2 = DdeRuntime(synthetic_catalog(), SYN_OK, c2)
    d = rt2.read("syn.dpf_soot_mass")
    assert d.quality == "disconnected" and d.value is None
    assert any(x.quality == "ok" for x in c2.read_pid(0x0C))


def test_restore_failure_invalidates_the_connection(tcp):
    srv, c = tcp(script=_script_on("ATCRA", lambda normal: [(0, b"?\r\r>")]))
    rt = DdeRuntime(synthetic_catalog(), SYN_OK, c)
    r = rt.read("syn.dpf_soot_mass")
    assert r.quality == "ok"  # the answer itself was clean ...
    assert c.transport.describe()["manufacturer_restore_failures"] == 1
    assert not c.link_ready()  # ... but the connection is discarded
    n = len(srv.log)
    c.read_pid(0x0C)
    assert _cmds(srv, n)[0] == "ATZ"


def test_unsupported_adapter_is_remembered_and_never_retried(tcp):
    srv, c = tcp("healthy_idle")  # this fake adapter answers '?' to addressing commands
    rt = DdeRuntime(synthetic_catalog(), SYN_OK, c)
    n = len(srv.log)
    r = rt.read("syn.dpf_soot_mass")
    assert r.quality == "unsupported_adapter" and "ATSH7B0" in r.detail
    # set-up stops at the first refusal; the restore attempt is refused too -> connection discarded
    assert _cmds(srv, n) == ["ATSH7B0", "ATSH7DF"] and not c.link_ready()
    n = len(srv.log)
    assert rt.read("syn.egr_actual").quality == "unsupported_adapter" and len(srv.log) == n
    assert rt.executable() == []


def test_mixed_generic_and_dde_polling_is_serialised(tcp):
    srv, c = tcp()
    rt = DdeRuntime(synthetic_catalog(), SYN_OK, c)
    errors: list[str] = []
    stop = threading.Event()

    def generic():
        while not stop.is_set():
            for r in c.read_pid(0x0C):
                if r.ecu == "7E8" and r.quality != "ok":
                    errors.append(f"generic {r.quality}")

    th = threading.Thread(target=generic)
    th.start()
    try:
        for _ in range(6):
            for rid in ("syn.dpf_soot_mass", "syn.egr_actual", "syn.identification"):
                if rt.read(rid).quality != "ok":
                    errors.append(rid)
    finally:
        stop.set()
        th.join()
    assert not errors
    # every transaction is contiguous: no generic request between ATSH7B0 and ATFCSM0
    cmds = _cmds(srv)
    inside = False
    for cmd in cmds:
        if cmd == "ATSH7B0":
            inside = True
        elif cmd == "ATFCSM0":
            inside = False
        elif inside:
            assert cmd.startswith("AT") or cmd.startswith("22"), cmd
    # generic requests only ever ran under the generic baseline
    assert all(
        h in (None, "7DF") and e is None and r is None for q, h, e, r in srv.sim.obd_log if q == "010C"
    )


def test_workbench_opt_in_variant_and_profile_gate(store):
    wb = Workbench(store)
    wb.attach(SimulatorTransport("mini_dde_synthetic"), "simulator", "mini_dde_synthetic")
    rt = wb.dde_runtime()
    r = rt.read("syn.dpf_soot_mass")
    assert r.quality == "not_attempted" and "opt-in" in r.detail  # default: opted out
    assert wb.dde_settings()["opt_in"] is False
    wb.set_dde_settings(opt_in=True)
    assert "variant not confirmed" in wb.dde_runtime().read("syn.dpf_soot_mass").detail
    wb.set_dde_settings(confirmed_variant=SYNTHETIC_VARIANT)
    assert wb.dde_runtime().read("syn.dpf_soot_mass").quality == "ok"
    st = wb.dde_status()
    assert st["synthetic"] and any(m["status"] == "executable" for m in st["availability"])
    with pytest.raises(ValueError):
        wb.set_dde_settings(confirmed_variant="   ")
    # the Porsche profile has no DDE path at all
    wb.disconnect()
    wb.set_profile("porsche-boxster-986-mk2")
    assert wb.dde_runtime() is None and wb.dde_status()["supported"] is False
    with pytest.raises(ValueError):
        wb.set_dde_settings(opt_in=True)


def test_real_source_uses_the_real_catalog_where_nothing_runs(store):
    wb = Workbench(store)
    wb.set_dde_settings(opt_in=True, confirmed_variant="ANY")
    wb.attach(SimulatorTransport("mini_dde_synthetic"), "real", None)  # pretend: real adapter
    st = wb.dde_status()
    assert not st["synthetic"] and all(m["status"] == "blocked" for m in st["availability"])
    rt = wb.dde_runtime()
    assert rt.executable() == []
    sim = wb.transport.sim
    n = len(sim.received)
    for rid in rt.catalog.entries:
        assert rt.read(rid).quality == "not_attempted"
    assert len(sim.received) == n


def test_api_settings_and_status(tmp_path):
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    with TestClient(create_app(str(tmp_path / "a.sqlite3"))) as api:
        st = api.get("/api/dde/status").json()
        assert st["supported"] and st["settings"]["opt_in"] is False
        st = api.put("/api/dde/settings", json={"opt_in": True, "confirmed_variant": "X"}).json()
        assert st["settings"]["opt_in"] is True and st["settings"]["confirmed_variant"] == "X"
        st = api.put("/api/dde/settings", json={"clear_variant": True}).json()
        assert st["settings"]["confirmed_variant"] is None
        assert all(m["status"] == "blocked" for m in st["availability"])
