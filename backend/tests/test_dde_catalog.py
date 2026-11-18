"""DDE-02: the request catalog cannot produce an executable request or a value without a
complete verification record, a matching variant and an explicit opt-in."""

import dataclasses
import json

import pytest

from app.cli import main
from app.manufacturer.bmw.catalog import (
    ENTRIES,
    SOURCES,
    CatalogEntry,
    CatalogError,
    ExecutionContext,
    RequestCatalog,
    ResponseLayout,
    default_catalog,
    validate_entry,
)
from app.manufacturer.bmw.decoders import DECODERS, DecodedValue, Decoder
from app.manufacturer.bmw.synthetic import (
    SYN_ADDR,
    SYN_ENTRIES,
    SYNTHETIC_SOURCE,
    SYNTHETIC_VARIANT,
    synthetic_catalog,
)
from app.manufacturer.extension import default_registry

REAL_OK = ExecutionContext(
    opted_in=True, confirmed_variant="VARIANT-X", adapter_verdict="manufacturer_transport_candidate"
)
SYN_OK = ExecutionContext(opted_in=True, confirmed_variant=SYNTHETIC_VARIANT, synthetic_session=True)

WANTED = (
    "dde_identification", "dde_fault_memory", "dpf_regeneration_active", "dpf_soot_mass", "dpf_ash_mass",
    "dpf_diff_pressure", "exhaust_temperature_pre_dpf", "exhaust_backpressure", "dpf_last_regeneration",
    "egr_commanded", "egr_actual", "air_mass_target", "air_mass_actual", "boost_commanded", "boost_actual",
    "rail_pressure_commanded", "rail_pressure_actual_mfr", "injector_corrections",
)  # fmt: skip


def _real_decoder():
    # a test-only "real" decoder (not synthetic) so a hypothetical vehicle-verified entry can be judged
    DECODERS.setdefault(
        "test.real_u16",
        Decoder(
            "test.real_u16",
            "3",
            lambda d: DecodedValue(quality="ok", value=float(int.from_bytes(d[:2], "big"))),
        ),
    )


def _verified(**kw) -> CatalogEntry:
    """A hypothetical fully verified entry (test only: no such entry exists for the car)."""
    _real_decoder()
    base = dict(
        request_id="test.verified",
        metric="dpf_soot_mass",
        title="test",
        priority="P0",
        kind="live_value",
        verification_state="vehicle_verified",
        risk_class="pure_read",
        source_ids=("this_vehicle_traces",),
        compatible_variants=("VARIANT-X",),
        service=0x22,
        payload_hex="22ABCD",
        addressing=SYN_ADDR,
        response=ResponseLayout(0x62, "ABCD", 5, 5, "test.real_u16", "3", "g", (0.0, 1000.0)),
        enabled=True,
    )
    base.update(kw)
    return CatalogEntry(**base)


def _cat(*entries):
    return RequestCatalog(entries, SOURCES)


def test_default_catalog_covers_every_wanted_metric_and_nothing_is_executable():
    cat = default_catalog()
    assert set(WANTED) <= set(cat.metrics())
    assert cat.problems() == {}
    for e in cat.entries.values():
        assert e.enabled is False and e.verification_state == "literature_only" and not e.synthetic
        assert e.payload_hex is None and e.addressing is None  # no guessed bytes / addresses
        assert e.establish_by and e.blocked_reason
    for ctx in (ExecutionContext(), REAL_OK, SYN_OK):
        assert cat.executable(ctx) == []
        assert all(a["status"] == "blocked" for a in cat.availability(ctx))
    assert cat.availability(ExecutionContext(), wanted=["not_a_metric"])[0]["status"] == "unavailable"


def test_default_catalog_refuses_synthetic_and_duplicate_entries():
    with pytest.raises(CatalogError):
        RequestCatalog((*ENTRIES, SYN_ENTRIES[1]), ())
    with pytest.raises(CatalogError):
        RequestCatalog((ENTRIES[0], ENTRIES[0]), ())


def test_complete_verified_entry_needs_opt_in_variant_and_adapter_candidate():
    cat = _cat(_verified())
    assert cat.decide("test.verified", REAL_OK).executable
    for ctx, why in (
        (dataclasses.replace(REAL_OK, opted_in=False), "opt-in"),
        (dataclasses.replace(REAL_OK, confirmed_variant=None), "not confirmed"),
        (dataclasses.replace(REAL_OK, confirmed_variant="VARIANT-Y"), "not covered"),
        (
            dataclasses.replace(REAL_OK, adapter_verdict="manufacturer_transport_unverified"),
            "candidate required",
        ),
        (dataclasses.replace(REAL_OK, synthetic_session=True), "simulator"),
    ):
        d = cat.decide("test.verified", ctx)
        assert not d.executable and any(why in r for r in d.reasons), (why, d.reasons)


@pytest.mark.parametrize(
    "change,why",
    [
        (dict(enabled=False), "disabled"),
        (dict(verification_state="fixture_verified"), "vehicle verification required"),
        (dict(verification_state="literature_only"), "vehicle verification required"),
        (dict(risk_class="temporary_session_state"), "only pure reads"),
        (dict(service=0x2E, payload_hex="2EABCD"), "not a read-only"),
        (dict(service=0x31, payload_hex="31010203"), "not a read-only"),
        (dict(service=0x19, payload_hex="1914"), "not a read-only sub-function"),
        (dict(payload_hex="21ABCD"), "does not match declared service"),
        (dict(payload_hex="22abcd"), "canonical"),
        (dict(source_ids=("nowhere",)), "unknown source_id"),
        (dict(source_ids=()), "incomplete"),
        (dict(response=ResponseLayout(0x62, "ABCD", 5, 5, "test.real_u16", "2")), "version"),
        (dict(response=ResponseLayout(0x62, "ABCD", 5, 5, "missing.decoder", "1")), "not registered"),
        (dict(response=ResponseLayout(0x62, "ABCD", 5, 5, "synthetic.u16_0p1", "1")), "synthetic decoder"),
        (dict(response=ResponseLayout(0x7F, "ABCD", 5, 5, "test.real_u16", "3")), "positive response SID"),
        (dict(addressing=dataclasses.replace(SYN_ADDR, tx_ext_address=None)), "extended addressing"),
        (dict(addressing=dataclasses.replace(SYN_ADDR, tx_can_id="18DAF110")), "11-bit"),
    ],
)
def test_incomplete_or_mismatched_entries_never_execute(change, why):
    cat = _cat(_verified(**change))
    d = cat.decide("test.verified", REAL_OK)
    assert not d.executable and any(why in r for r in d.reasons), d.reasons
    # and cannot produce a value
    v = cat.interpret("test.verified", bytes.fromhex("62ABCD0010"), REAL_OK)
    assert v.quality == "raw_only" and v.value is None and v.raw_hex == "62ABCD0010"


def test_ambiguous_definitions_disable_each_other():
    a = _verified(request_id="a")
    b = _verified(
        request_id="b",
        payload_hex="22ABCE",
        response=ResponseLayout(0x62, "ABCE", 5, 5, "test.real_u16", "3"),
    )
    cat = _cat(a, b)
    for rid in ("a", "b"):
        d = cat.decide(rid, REAL_OK)
        assert not d.executable and any("ambiguous" in r for r in d.reasons)
    # a variant-specific split is not ambiguous
    cat2 = _cat(a, dataclasses.replace(b, compatible_variants=("VARIANT-Y",)))
    assert cat2.decide("a", REAL_OK).executable and not cat2.decide("b", REAL_OK).executable


def test_interpret_validates_echo_length_and_range():
    cat = _cat(_verified())
    ok = cat.interpret("test.verified", bytes.fromhex("62ABCD0010"), REAL_OK)
    assert ok.quality == "ok" and ok.value == 16.0 and ok.decoder_version == "3" and ok.unit == "g"
    assert (
        cat.interpret("test.verified", bytes.fromhex("62ABCE0010"), REAL_OK).quality == "error"
    )  # wrong DID
    assert cat.interpret("test.verified", bytes.fromhex("62ABCD00"), REAL_OK).quality == "error"  # short
    assert cat.interpret("test.verified", bytes.fromhex("7F2231"), REAL_OK).quality == "error"  # negative
    hi = cat.interpret("test.verified", bytes.fromhex("62ABCDFFFF"), REAL_OK)
    assert hi.quality == "error" and hi.value is None and "range" in hi.detail


def test_synthetic_catalog_only_runs_against_the_simulator():
    cat = synthetic_catalog()
    assert cat.problems() == {}
    assert {e.metric for e in cat.executable(SYN_OK)} >= {"dpf_soot_mass", "egr_actual", "dde_identification"}
    real_ctx = dataclasses.replace(
        SYN_OK, synthetic_session=False, adapter_verdict="manufacturer_transport_candidate"
    )
    assert cat.executable(real_ctx) == []
    v = cat.interpret("syn.dpf_soot_mass", bytes.fromhex("62F1020190"), SYN_OK)
    assert v.quality == "ok" and v.value == 4.0 and v.unit == "g"
    flag = cat.interpret("syn.dpf_regeneration_active", bytes.fromhex("62F10101"), SYN_OK)
    assert flag.value == 1.0 and flag.status == {"active": True}
    assert cat.interpret("syn.dpf_regeneration_active", bytes.fromhex("62F10180"), SYN_OK).quality == "error"


def test_synthetic_source_cannot_back_a_real_entry():
    e = _verified(source_ids=("synthetic_simulator",))
    problems = validate_entry(e, {"synthetic_simulator": SYNTHETIC_SOURCE})
    assert any("synthetic but the entry is not marked synthetic" in p for p in problems)


def test_registry_status_and_cli_report_blocked(capsys, tmp_path):
    st = default_registry().status(("bmw-dde-unconfirmed",))[0]
    assert st["enabled"] is False and st["requests"] == []
    assert st["catalog"]["metrics"] and all(m["status"] == "blocked" for m in st["catalog"]["metrics"])
    assert main(["--db", str(tmp_path / "c.sqlite3"), "--json", "dde-catalog"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"].startswith("dde-catalog/") and not out["synthetic"]
    assert all(m["status"] == "blocked" for m in out["availability"])
