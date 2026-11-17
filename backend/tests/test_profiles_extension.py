import pytest

from app.ecus.definitions import ECU_DEFINITIONS
from app.manufacturer.extension import (
    BmwDdeExtension,
    ExtensionRegistry,
    ExtensionUnavailableError,
    ManufacturerExtension,
    VerificationRecord,
    default_registry,
)
from app.obd.allowlist import AllowedCommand, ForbiddenRequestError
from app.service import Workbench
from app.vehicles.profiles import DEFAULT_PROFILE_ID, PROFILES, get_profile


def test_two_profiles_with_separate_ecus_protocols_and_rules():
    mini, porsche = PROFILES["mini-countryman-r60-diesel"], PROFILES["porsche-boxster-986-mk2"]
    assert (
        DEFAULT_PROFILE_ID == mini.id
        and mini.status == "generic_obd_ready"
        and porsche.status == "generic_obd_incremental"
    )
    assert mini.expected_protocol_families == ("can",) and porsche.expected_protocol_families == ("kline",)
    assert mini.ecu_ids != porsche.ecu_ids and set(mini.ecu_ids + porsche.ecu_ids) <= set(ECU_DEFINITIONS)
    assert "dde_4862" in mini.rule_ids and "dde_4862" not in porsche.rule_ids
    assert "NOT confirmed" in mini.identity_note
    d = mini.to_dict()
    assert (
        d["ecus"][0]["variant_confirmed"] is False and d["owner_reported"]["repairs"][0]["verified"] is False
    )
    with pytest.raises(KeyError):
        get_profile("nope")


def test_active_profile_persists_and_tags_sessions(store):
    wb = Workbench(store)
    assert wb.profile.id == DEFAULT_PROFILE_ID
    wb.set_profile("porsche-boxster-986-mk2")
    assert Workbench(store).profile.id == "porsche-boxster-986-mk2"
    rec = wb.vehicle_record()
    rec["confirmed"]["model_year"] = "2003"
    wb.save_vehicle_record(rec)
    assert wb.vehicle_record()["confirmed"]["model_year"] == "2003"
    assert wb.vehicle_record("mini-countryman-r60-diesel")["confirmed"]["model_year"] == ""


def test_extensions_are_off_and_report_unavailable():
    reg = default_registry()
    for st in reg.status():
        assert st["enabled"] is False and st["verified"] is False and st["requests"] == []
        assert st["parameters"] and all(
            p["status"] == "unavailable" and p["value"] is None for p in st["parameters"]
        )
    names = [p["name"] for p in reg.status(("bmw-dde-unconfirmed",))[0]["parameters"]]
    for wanted in (
        "DPF differential pressure",
        "DPF soot load",
        "EGR target",
        "EGR actual",
        "Rail pressure, commanded",
    ):
        assert wanted in names
    with pytest.raises(ExtensionUnavailableError):
        BmwDdeExtension().read("dpf_soot_load")


def test_opt_in_alone_or_verification_alone_does_not_enable():
    reg = ExtensionRegistry()
    ext = BmwDdeExtension()
    reg.register(ext)
    reg.opt_in(ext.id)
    assert not reg.is_enabled(ext.id)
    ext.verification = VerificationRecord("id", "ref", ("src",), ("fixture.jsonl",))
    assert not reg.is_enabled(ext.id)  # still no allowlisted request to send


def test_extension_cannot_declare_manufacturer_requests():
    with pytest.raises(ForbiddenRequestError):
        AllowedCommand.parse("22F190")

    class Rogue(ManufacturerExtension):
        id = "rogue"
        ecu_definition_id = "bmw-dde-unconfirmed"

        def allowlisted_requests(self):
            return ("2E0001FF",)  # a plain string, not an AllowedCommand

    with pytest.raises(ExtensionUnavailableError):
        ExtensionRegistry().register(Rogue())


def test_profile_status_separates_generic_manufacturer_and_hardware_validation():
    """P1-4: no profile may claim 'complete' while BMW/Porsche manufacturer data is not read."""
    for p in PROFILES.values():
        assert p.status in ("generic_obd_ready", "generic_obd_incremental") and p.status != "complete"
        caps = p.to_dict()["capabilities"]
        assert caps["manufacturer_diagnostics"] == "not_available"
        assert (
            caps["hardware_validated"] is False
            and "Not hardware-validated" in caps["hardware_validation_note"]
        )
    mini = PROFILES["mini-countryman-r60-diesel"]
    assert mini.to_dict()["capabilities"]["generic_obd"] == "ready"
    # owner-reported BMW codes are labelled as history, never as something this tool read
    for c in mini.owner_reported["manufacturer_fault_codes"]:
        assert "not read by this tool" in c["source"]
