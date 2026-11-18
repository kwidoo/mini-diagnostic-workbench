"""What this tool may say about a DTC code, and what it may not.

Generic OBD returns SAE J2012-format codes. Codes in the SAE-defined ranges (e.g. P0xxx) belong to
published families; codes in the manufacturer-controlled ranges (P1xxx, P30xx-P33xx, B1/B2, C1/C2,
U1/U2) have a meaning that the vehicle manufacturer defines, often per engine / ECU variant.

A manufacturer-specific description is only shown when a VERIFIED mapping exists for the vehicle
profile AND a confirmed ECU variant. Nothing is verified for the MINI yet (its DDE variant is
unconfirmed), so for example P115D is shown as the raw code with an uncertainty note, never with a
guessed text. Owner-reported BMW hex fault codes (4862, 480A) are a different thing again: they
were read elsewhere with a BMW tester and are history, not a reading of this tool.
"""

from __future__ import annotations

from typing import Any

# SAE J2012 generic code families used only to say "a generic code in this family is present".
AIR_PATH_CODES = (
    {f"P010{i}" for i in range(10)}
    | {f"P040{i}" for i in range(10)}
    | {"P0234", "P0235", "P0236", "P0237", "P0238", "P0299", "P2413"}
)
DPF_CODES = {
    "P2002",
    "P2003",
    "P242F",
    "P244A",
    "P244B",
    "P2452",
    "P2453",
    "P2454",
    "P2455",
    "P2456",
    "P2458",
    "P2459",
    "P2463",
    "P246C",
}

#: code -> verified descriptions, each only valid for one vehicle profile and one CONFIRMED ECU variant:
#: {"description": ..., "vehicle_profile": ..., "ecu_variant": ..., "source": ...}. Deliberately empty:
#: no manufacturer description has been verified against this car's DDE variant. Add entries only with
#: a citable source for that exact variant.
VERIFIED_MANUFACTURER_DESCRIPTIONS: dict[str, list[dict[str, str]]] = {}

UNVERIFIED_NOTE = (
    "manufacturer-defined code range: its meaning depends on the manufacturer and the ECU variant. No verified "
    "description exists for this vehicle / ECU variant, so it is shown as the raw code only. It may or may not "
    "concern the air path."
)


def is_manufacturer_range(code: str) -> bool:
    """SAE J2012 manufacturer-controlled ranges: P1xxx, P30xx-P33xx, B1/B2, C1/C2, U1/U2."""
    code = code.upper()
    if len(code) != 5:
        return False
    if code[0] == "P":
        return code[1] == "1" or (code[1] == "3" and code[2] in "0123")
    return code[1] in "12"


def confirmed_ecu_variant(meta: dict[str, Any]) -> str | None:
    """A CONFIRMED ECU variant, or None. Only an explicit ``meta["ecu_variant"]`` counts; a calibration
    ID alone is not mapped to a variant by this tool."""
    v = (meta or {}).get("ecu_variant")
    return str(v) if v else None


def classify(code: str, vehicle_profile: str | None = None, ecu_variant: str | None = None) -> dict[str, Any]:
    """-> {code, family, air_path_relevance, description, description_status, note}.

    ``air_path_relevance``: ``air_path_family`` (SAE air-mass / manifold-pressure / EGR / boost family),
    ``unknown`` (manufacturer-defined meaning, not verified: neither counted for nor against the air path),
    or ``other_family`` (an SAE-range code outside those families).
    """
    code = code.upper()
    verified = [
        d
        for d in VERIFIED_MANUFACTURER_DESCRIPTIONS.get(code, [])
        if vehicle_profile
        and ecu_variant
        and d["vehicle_profile"] == vehicle_profile
        and d["ecu_variant"] == ecu_variant
    ]
    if code in AIR_PATH_CODES:
        family, relevance = "sae_air_path", "air_path_family"
        note = "SAE generic code in the air-mass / manifold-pressure / EGR / boost families; it names a monitored system, not a failed part."
    elif code in DPF_CODES:
        family, relevance = "sae_particulate_filter", "other_family"
        note = "SAE generic code in the particulate-filter family."
    elif is_manufacturer_range(code):
        family, relevance = "manufacturer_defined", "unknown"
        note = UNVERIFIED_NOTE
    else:
        family, relevance = "sae_other", "other_family"
        note = "SAE-range code outside the families this tool groups as air path or DPF."
    if verified:
        return {
            "code": code,
            "family": family,
            "air_path_relevance": relevance,
            "description": verified[0]["description"],
            "description_status": "verified",
            "description_source": verified[0].get("source"),
            "note": f"Description verified for {vehicle_profile} / ECU variant {ecu_variant}.",
        }
    return {
        "code": code,
        "family": family,
        "air_path_relevance": relevance,
        "description": None,
        "description_status": "not_verified" if family == "manufacturer_defined" else "generic_family_only",
        "description_source": None,
        "note": note,
    }
