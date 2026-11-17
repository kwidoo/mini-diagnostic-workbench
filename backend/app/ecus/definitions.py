"""ECU definitions: what an ECU family is, and what this tool can / cannot read from it.

An ECU definition never contains request bytes. Manufacturer parameters are
declared here only so the UI and reports can say *explicitly* that they are
unavailable. Making one available requires a verified extension (see
app/manufacturer/extension.py and docs/DIAGNOSTIC_PROTOCOL.md).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManufacturerParameter:
    key: str
    name: str
    unit: str | None = None
    status: str = "unavailable"  # only ever "unavailable" until an extension is verified
    reason: str = "No verified request mapping, addressing or scaling for this ECU variant."


@dataclass(frozen=True)
class EcuDefinition:
    id: str
    name: str
    manufacturer: str
    family: str
    fuel: str
    variant_confirmed: bool
    variant_note: str
    generic_obd_note: str
    access_requirements: str
    manufacturer_parameters: tuple[ManufacturerParameter, ...] = ()


def _p(key: str, name: str, unit: str | None = None) -> ManufacturerParameter:
    return ManufacturerParameter(key, name, unit)


BMW_DDE = EcuDefinition(
    id="bmw-dde-unconfirmed",
    name="BMW/MINI diesel engine control (DDE), variant not yet identified",
    manufacturer="BMW Group",
    family="DDE (Digital Diesel Electronics)",
    fuel="diesel",
    variant_confirmed=False,
    variant_note=(
        "The exact DDE variant, engine code and software level are unknown until read from the car "
        "(calibration ID via service 09) or confirmed from vehicle paperwork. BMW fault-code meanings "
        "depend on the variant."
    ),
    generic_obd_note=(
        "Emission-related generic OBD (services 01/02/03/07/09/0A) is expected on a 2016 EU diesel. "
        "Which PIDs are supported is discovered from the car, not assumed."
    ),
    access_requirements=(
        "BMW-specific fault memory (hex codes such as 4862 / 480A), DPF soot/ash and regeneration history, "
        "EGR target/actual, injector quantity corrections and commanded rail pressure are served by BMW's own "
        "diagnostic protocol and job definitions. In practice that means a BMW-capable tester: ISTA or "
        "INPA/EDIABAS with a K+DCAN interface, or a commercial multi-brand scan tool with BMW coverage. "
        "A generic ELM327 adapter speaks to the same connector, but this project has no verified addressing, "
        "request identifiers or scaling for the ECU in this car, so none are sent."
    ),
    manufacturer_parameters=(
        _p("dpf_diff_pressure", "DPF differential pressure", "hPa"),
        _p("dpf_soot_load", "DPF soot load"),
        _p("dpf_regen_status", "DPF regeneration status / history"),
        _p("exhaust_backpressure", "Exhaust back-pressure", "hPa"),
        _p("egr_target", "EGR target"),
        _p("egr_actual", "EGR actual"),
        _p("maf_target", "Air-mass target", "mg/stroke"),
        _p("injector_corrections", "Injector quantity corrections (per cylinder)"),
        _p("rail_pressure_commanded", "Rail pressure, commanded", "bar"),
        _p("rail_pressure_actual_mfr", "Rail pressure, actual (manufacturer value)", "bar"),
        _p("bmw_fault_memory", "BMW-specific fault memory (hex fault codes)"),
    ),
)

BOSCH_ME78 = EcuDefinition(
    id="bosch-me7-8-unconfirmed",
    name="Bosch Motronic ME7.8 (Porsche DME), variant not yet identified",
    manufacturer="Bosch / Porsche",
    family="Motronic ME7.8",
    fuel="petrol",
    variant_confirmed=False,
    variant_note="Model year, engine (2.7 / 3.2 S) and DME software level must be confirmed from the car.",
    generic_obd_note=(
        "Generic OBD is expected over the K-line (ISO 9141-2 or ISO 14230-4), not CAN. The actual protocol is "
        "read back from the adapter (ATDPN) and never assumed. K-line polling is much slower than CAN."
    ),
    access_requirements=(
        "Porsche-specific data (per-cylinder misfire counters, camshaft deviation, adaptation values, "
        "over-rev ranges, Porsche fault memory) needs a Porsche-capable tester (PIWIS / PST2 or a commercial "
        "tool with Porsche coverage). This project has no verified Porsche requests, so none are sent."
    ),
    manufacturer_parameters=(
        _p("misfire_counters", "Misfire counters (per cylinder)"),
        _p("camshaft_deviation", "Camshaft position deviation (bank 1 / bank 2)", "°"),
        _p("mixture_adaptation", "Mixture adaptation values (idle / part load)"),
        _p("overrev_ranges", "Over-rev range counters"),
        _p("maf_raw", "MAF sensor raw signal", "V"),
        _p("porsche_fault_memory", "Porsche-specific fault memory"),
    ),
)

ECU_DEFINITIONS: dict[str, EcuDefinition] = {e.id: e for e in (BMW_DDE, BOSCH_ME78)}
