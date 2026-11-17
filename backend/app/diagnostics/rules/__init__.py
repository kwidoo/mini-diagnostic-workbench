"""Diagnostic rule registry. Rule id -> function(AnalysisContext) -> list[Finding]."""

from app.diagnostics.rules.diesel_bmw import air_path_egr_maf, dde_4862, dpf_480a
from app.diagnostics.rules.generic import (
    fuel_trim_bank_balance,
    idle_rpm_stability,
    intermittent_progression,
    manufacturer_data_availability,
    repair_history,
)

RULES = {
    "idle_rpm_stability": idle_rpm_stability,
    "intermittent_progression": intermittent_progression,
    "repair_history": repair_history,
    "manufacturer_data_availability": manufacturer_data_availability,
    "fuel_trim_bank_balance": fuel_trim_bank_balance,
    "air_path_egr_maf": air_path_egr_maf,
    "dpf_480a": dpf_480a,
    "dde_4862": dde_4862,
}
