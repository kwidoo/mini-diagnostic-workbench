"""Vehicle profiles: the only place where vehicle-specific choices live.

A profile selects ECU definitions, expected protocol families, the parameter
set to prioritise, recording profiles and diagnostic rules. Transports,
protocol decoding, recording, storage, visualisation and the analysis engine
are shared by all profiles.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.ecus.definitions import ECU_DEFINITIONS
from app.parameters.standard_pids import pid_name

PidOrGroup = int | tuple[int, ...]  # a tuple means "first one the ECU supports"


@dataclass(frozen=True)
class RecordingProfile:
    id: str
    name: str
    fast: tuple[PidOrGroup, ...]  # polled every cycle
    slow: tuple[PidOrGroup, ...] = ()  # one of these per cycle, rotating


@dataclass(frozen=True)
class VehicleProfile:
    id: str
    name: str
    make: str
    model: str
    engine: str
    fuel: str
    # Scope of the *generic OBD* support only: "generic_obd_ready" | "generic_obd_incremental".
    # It says nothing about manufacturer (DDE/DME) diagnostics, which this tool does not read.
    status: str
    identity_note: str
    ecu_ids: tuple[str, ...]
    expected_protocol_families: tuple[str, ...]
    protocol_note: str
    priority_pids: tuple[int, ...]
    recording_profiles: tuple[RecordingProfile, ...]
    rule_ids: tuple[str, ...]
    event_labels: tuple[str, ...]
    owner_reported: dict = field(default_factory=dict)
    first_run_checklist: tuple[str, ...] = ()
    hardware_validated: bool = False  # only set after a real-vehicle protocol log has been verified
    hardware_validation_note: str = (
        "Not hardware-validated: tested against the synthetic ELM327 simulator only, not yet on this vehicle."
    )

    def recording_profile(self, profile_id: str) -> RecordingProfile:
        for p in self.recording_profiles:
            if p.id == profile_id:
                return p
        raise KeyError(f"vehicle {self.id!r} has no recording profile {profile_id!r}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ecus"] = [asdict(ECU_DEFINITIONS[e]) for e in self.ecu_ids]
        d["priority_pids"] = [{"pid": p, "hex": f"{p:02X}", "name": pid_name(p)} for p in self.priority_pids]
        d["capabilities"] = self.capability_indicators()
        return d

    def capability_indicators(self, manufacturer_enabled: bool = False) -> dict:
        """Three separate indicators; never merged into one 'complete' flag."""
        return {
            "generic_obd": "ready" if self.status == "generic_obd_ready" else "incremental",
            "manufacturer_diagnostics": "available" if manufacturer_enabled else "not_available",
            "manufacturer_note": (
                "Manufacturer-specific (BMW DDE / Bosch DME) fault memory and parameters are not read by this "
                "tool; owner-reported manufacturer codes are history, not a reading."
            ),
            "hardware_validated": self.hardware_validated,
            "hardware_validation_note": self.hardware_validation_note,
        }


RAIL = (0x23, 0x59, 0x22)

MINI_R60_DIESEL = VehicleProfile(
    id="mini-countryman-r60-diesel",
    name="MINI Countryman R60 2.0 Diesel (BMW DDE)",
    make="MINI",
    model="Countryman (probably R60)",
    engine="2.0 diesel (variant unconfirmed, possibly Cooper SD)",
    fuel="diesel",
    status="generic_obd_ready",
    identity_note=(
        "Owner-reported: 2016 MINI Countryman 2.0 diesel, probably R60, possibly Cooper SD with JCW styling. "
        "Platform, engine code and ECU variant are NOT confirmed until read from the car or vehicle documents."
    ),
    ecu_ids=("bmw-dde-unconfirmed",),
    expected_protocol_families=("can",),
    protocol_note="ISO 15765-4 CAN is expected for a 2016 EU vehicle; the actual protocol is read back from the adapter.",
    priority_pids=(
        0x0C,
        0x0D,
        0x05,
        0x0F,
        0x33,
        0x10,
        0x0B,
        0x04,
        0x11,
        0x23,
        0x59,
        0x22,
        0x42,
        0x1F,
        0x2C,
        0x2D,
        0x46,
    ),
    recording_profiles=(
        RecordingProfile(
            "idle",
            "Idle investigation",
            (0x0C, 0x10, 0x0B, RAIL),
            (0x05, 0x0D, 0x04, 0x2C, 0x2D, 0x33, 0x42, 0x0F, 0x01),
        ),
        RecordingProfile("warmup", "Warm-up", (0x0C, 0x05, 0x10), (0x0B, RAIL, 0x0F, 0x04, 0x42, 0x0D, 0x01)),
        RecordingProfile(
            "road",
            "Road (passenger operates the laptop)",
            (0x0C, 0x0D, 0x10, 0x0B, 0x04),
            (RAIL, 0x05, 0x11, 0x2C, 0x2D, 0x33, 0x01),
        ),
    ),
    rule_ids=(
        "idle_rpm_stability",
        "air_path_egr_maf",
        "dpf_480a",
        "dde_4862",
        "intermittent_progression",
        "repair_history",
        "manufacturer_data_availability",
    ),
    event_labels=(
        "rough idle started",
        "rough idle stopped",
        "MIL on",
        "MIL off",
        "vibration",
        "rattling noise",
        "after warmup",
        "jerk",
    ),
    owner_reported={
        "symptoms": [
            "strong idle RPM fluctuation",
            "jerking",
            "vibration / unusual rattling sounds",
            "intermittent Check Engine lamp",
        ],
        "manufacturer_fault_codes": [
            {"code": "4862", "system": "BMW DDE", "source": "owner report, not read by this tool"},
            {"code": "480A", "system": "BMW DDE", "source": "owner report, not read by this tool"},
        ],
        "repairs": [
            {"description": "EGR cooler replaced", "verified": False},
            {"description": "Intake manifold replaced", "verified": False},
        ],
        "notes": "Same symptoms reported before and after the repair.",
    },
    first_run_checklist=(
        "Park outdoors or in a well-ventilated place, handbrake on, gearbox in neutral/park.",
        "Plug the Vgate iCar Pro into the OBD socket, ignition ON, engine OFF.",
        "Join the Mac to the adapter Wi-Fi (default SSID V-LINK). Mobile data / other networks are not needed.",
        "mini-obd probe   (expect adapter identity and a CAN protocol)",
        "mini-obd scan    (stored / pending / permanent DTCs + freeze frame; nothing is cleared)",
        "mini-obd inventory   (which PIDs this ECU really supports; note any DPF/EGR extended PIDs listed as raw-only)",
        "Start the engine only if it is safe; mini-obd record --duration 300 --profile idle, and mark events in the UI.",
        "Stop immediately on metallic knocking, overheating, exhaust/coolant smell or a flashing MIL.",
    ),
)

PORSCHE_986 = VehicleProfile(
    id="porsche-boxster-986-mk2",
    name="Porsche Boxster 986 Mk2 (Bosch ME7.8)",
    make="Porsche",
    model="Boxster 986 Mk2",
    engine="flat-six petrol (2.7 or 3.2 S, unconfirmed)",
    fuel="petrol",
    status="generic_obd_incremental",
    identity_note="Model year, engine size and DME variant are not confirmed until read from the car or vehicle documents.",
    ecu_ids=("bosch-me7-8-unconfirmed",),
    expected_protocol_families=("kline",),
    protocol_note=(
        "Generic OBD over K-line (ISO 9141-2 / ISO 14230-4) is expected; the actual protocol is read back from "
        "the adapter. K-line decoding in this tool is tested against the simulator only, not yet against the car."
    ),
    priority_pids=(0x0C, 0x0D, 0x05, 0x0F, 0x10, 0x04, 0x11, 0x06, 0x07, 0x08, 0x09, 0x0E, 0x14, 0x18),
    recording_profiles=(
        RecordingProfile(
            "idle",
            "Idle investigation",
            (0x0C, 0x10, 0x06, 0x08),
            (0x07, 0x09, 0x05, 0x0E, 0x14, 0x18, 0x0D, 0x04, 0x01),
        ),
        RecordingProfile("warmup", "Warm-up", (0x0C, 0x05, 0x06, 0x08), (0x07, 0x09, 0x10, 0x0F, 0x0D, 0x01)),
    ),
    rule_ids=(
        "idle_rpm_stability",
        "fuel_trim_bank_balance",
        "intermittent_progression",
        "manufacturer_data_availability",
    ),
    event_labels=(
        "rough idle started",
        "rough idle stopped",
        "MIL on",
        "MIL off",
        "hesitation",
        "after warmup",
    ),
    owner_reported={"symptoms": [], "manufacturer_fault_codes": [], "repairs": [], "notes": ""},
    first_run_checklist=(
        "Park outdoors or in a well-ventilated place, handbrake on, gearbox in neutral.",
        "Plug in the adapter, ignition ON, engine OFF; join the adapter Wi-Fi.",
        "mini-obd --vehicle porsche-boxster-986-mk2 probe   (note which protocol the adapter reports)",
        "mini-obd --vehicle porsche-boxster-986-mk2 scan, then inventory.",
        "Expect a low sample rate on K-line; choose few signals per recording.",
    ),
)

PROFILES: dict[str, VehicleProfile] = {p.id: p for p in (MINI_R60_DIESEL, PORSCHE_986)}
DEFAULT_PROFILE_ID = MINI_R60_DIESEL.id


def get_profile(profile_id: str | None) -> VehicleProfile:
    if profile_id is None:
        return PROFILES[DEFAULT_PROFILE_ID]
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise KeyError(f"unknown vehicle profile {profile_id!r}; known: {', '.join(PROFILES)}") from None


def resolve_schedule(
    profile: RecordingProfile, supported: set[int]
) -> tuple[list[int], list[int], list[int]]:
    """-> (fast PIDs, slow PIDs, requested-but-unsupported PIDs). Unsupported PIDs are never polled."""

    def pick(items: tuple[PidOrGroup, ...]) -> tuple[list[int], list[int]]:
        ok: list[int] = []
        missing: list[int] = []
        for item in items:
            group = item if isinstance(item, tuple) else (item,)
            hit = next((p for p in group if p in supported), None)
            if hit is None:
                missing.append(group[0])
            elif hit not in ok:
                ok.append(hit)
        return ok, missing

    fast, m1 = pick(profile.fast)
    slow, m2 = pick(profile.slow)
    return fast, [p for p in slow if p not in fast], m1 + m2
