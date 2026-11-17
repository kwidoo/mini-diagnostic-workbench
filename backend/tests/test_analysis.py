import pytest

from app.adapters.simulator import SimulatorTransport
from app.diagnostics.engine import analyze_session
from app.fixtures_gen import record_scenario
from app.service import Workbench
from app.vehicles.profiles import PROFILES

STRENGTHS = {"insufficient", "suggestive", "strong"}


def findings(store, scenario, duration=60, marks=()):
    sid = record_scenario(store, scenario, duration, list(marks))
    return {f.id: f for f in analyze_session(store, sid)}


def text(f):
    parts = [f.hypothesis, f.explanation, *f.caveats, *f.conditions, *f.next_steps]
    parts += [e.text for e in f.supporting + f.contradicting]
    return " ".join(parts)


def test_rough_idle_is_quantified_with_coincident_signals_and_caveats(store):
    fs = findings(store, "rough_idle", 90, [(22, "rough idle started")])
    f = fs["idle_rpm_stability"]
    assert f.strength == "strong"
    assert any("windows exceeded" in e.text for e in f.supporting)
    maf = next(e for e in f.supporting if e.values.get("pid") == "10")
    assert maf.values["range_unstable"] > 2 * maf.values["range_stable"]
    assert any("temporal association only" in c and "moved first" in c for c in f.caveats)
    assert any("rough idle started" in e.text for e in f.supporting)
    assert "replace the" not in text(f).lower() and "is faulty" not in text(f).lower()


def test_healthy_idle_does_not_report_instability_but_flags_intermittency(store):
    f = findings(store, "healthy_idle")["idle_rpm_stability"]
    assert f.strength == "insufficient" and not f.supporting and f.contradicting
    assert any("intermittent" in c for c in f.caveats)


def test_low_sample_rate_downgrades_and_is_stated(store):
    f = findings(store, "slow_adapter", 60)["idle_rpm_stability"]
    assert f.strength == "insufficient" and any("usable RPM samples" in c for c in f.caveats)


def test_air_path_needs_dtc_and_signal_for_strong(store):
    egr = findings(store, "egr_mismatch")["air_path_egr_maf"]
    assert egr.strength == "strong" and len(egr.supporting) == 2
    rough = findings(store, "rough_idle")["air_path_egr_maf"]
    assert rough.strength == "suggestive"  # a generic code alone never becomes "strong"
    healthy = findings(store, "healthy_idle")["air_path_egr_maf"]
    assert healthy.strength == "insufficient"
    assert any("Derived (not measured): boost gauge" in c for c in healthy.conditions)
    assert any("not a failed part" in c for c in egr.caveats)


def test_dpf_finding_states_what_is_not_known(store):
    f = findings(store, "dpf_history", 20)["dpf_480a"]
    assert f.strength == "suggestive"
    cond = " ".join(f.conditions)
    assert "soot load: not measured" in cond and "regeneration status and history: not measured" in cond
    assert "480A" in cond and "raw bytes" in cond.lower()
    assert any("never triggers a DPF regeneration" in c for c in f.caveats)
    assert findings(store, "healthy_idle", 20)["dpf_480a"].strength == "insufficient"


def test_dde_4862_is_hedged_on_ecu_variant(store):
    f = findings(store, "healthy_idle", 20)["dde_4862"]
    assert f.strength == "insufficient"
    assert "subject to confirming the DDE variant" in f.explanation and "different engine" in f.explanation
    assert any("not read by this tool" in c for c in f.conditions)


def test_unsupported_signals_are_called_not_supported(store):
    fs = findings(store, "unsupported_pids", 20)
    cond = " ".join(fs["air_path_egr_maf"].conditions)
    assert "MAF (PID 10): not supported by this ECU via generic OBD." in cond
    assert not fs["air_path_egr_maf"].supporting


def test_repair_history_is_observation_only(store):
    f = findings(store, "healthy_idle", 10)["repair_history"]
    assert f.strength == "insufficient" and "not proof" in f.explanation
    assert all("not independently verified" in c for c in f.conditions[:2])


def test_manufacturer_values_are_never_invented(store):
    fs = findings(store, "healthy_idle", 10)
    f = fs["manufacturer_data_bmw-dde"]
    assert any("DPF soot load" in c for c in f.conditions) and "No such request was sent" in " ".join(
        f.caveats
    )
    for finding in fs.values():
        for e in finding.supporting + finding.contradicting:
            assert not any(k in e.values for k in ("soot", "egr_actual", "dpf_diff_pressure"))


def test_strength_is_categorical_everywhere(store):
    for scenario in ("rough_idle", "dpf_history", "porsche_bank_imbalance"):
        for f in findings(store, scenario, 30).values():
            assert f.strength in STRENGTHS
            assert "probability" not in text(f).lower() and "% likely" not in text(f).lower()
            assert f.next_steps or f.id.startswith("manufacturer") or f.caveats


def test_scan_only_session_with_generic_dtc_never_claims_cause(store):
    wb = Workbench(store)
    wb.attach(SimulatorTransport("dpf_history"), "simulator", "dpf_history")
    fs = {f.id: f for f in analyze_session(store, wb.scan())}
    assert all(f.strength != "strong" for f in fs.values())
    assert any("does not show whether the filter" in c for c in fs["dpf_480a"].caveats)


def test_progression_needs_several_sessions(store):
    one = findings(store, "healthy_idle", 20)["intermittent_progression"]
    assert one.strength == "insufficient" and any("at least two" in c for c in one.caveats)
    record_scenario(store, "rough_idle", 40, [(22, "rough idle started")])
    last = findings(store, "egr_mismatch", 20)["intermittent_progression"]
    assert last.strength == "suggestive"
    assert any("appeared" in e.text for e in last.supporting)
    assert any("coolant" in e.text for e in last.supporting)


def test_porsche_profile_uses_same_engine_with_its_own_rules(store):
    fs = findings(store, "porsche_bank_imbalance", 80)
    assert set(fs) == {
        "idle_rpm_stability",
        "fuel_trim_bank_balance",
        "intermittent_progression",
        "manufacturer_data_porsche-me7-8",
    }
    assert fs["fuel_trim_bank_balance"].strength == "suggestive"
    assert "bank 2" in " ".join(fs["fuel_trim_bank_balance"].next_steps)
    assert findings(store, "porsche_healthy_idle", 80)["fuel_trim_bank_balance"].strength == "insufficient"


@pytest.mark.parametrize("profile", list(PROFILES.values()), ids=lambda p: p.id)
def test_every_profile_rule_exists(profile):
    from app.diagnostics.rules import RULES

    assert set(profile.rule_ids) <= set(RULES)
