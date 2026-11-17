"""Standard (vehicle-independent) OBD parameter definitions.

Source standard for every formula here: SAE J1979 / ISO 15031-5, service 01
(the same data bytes are used by service 02 freeze frames).

PIDs whose multi-byte layout has not been verified against the standard for
this project are listed in ``RAW_ONLY_PIDS``: the tool can report that an ECU
*supports* them and keeps the raw bytes, but shows no physical value.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

SOURCE = "SAE J1979 / ISO 15031-5"
BITMAP_PIDS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)


@dataclass(frozen=True)
class PidSpec:
    pid: int
    key: str  # stable signal key used in storage / UI
    name: str
    unit: str | None
    nbytes: int
    decode: Callable[[bytes], float]
    formula: str
    note: str = ""
    source: str = SOURCE


def _a(d: bytes) -> int:
    return d[0]


def _ab(d: bytes) -> int:
    return (d[0] << 8) | d[1]


def _trim(d: bytes) -> float:
    return d[0] * 100 / 128 - 100


_SPECS = [
    PidSpec(0x04, "engine_load", "Calculated engine load", "%", 1, lambda d: _a(d) * 100 / 255, "A*100/255"),
    PidSpec(0x05, "coolant_temp", "Engine coolant temperature", "°C", 1, lambda d: _a(d) - 40, "A-40"),
    PidSpec(0x06, "stft_b1", "Short-term fuel trim, bank 1", "%", 1, _trim, "A*100/128-100"),
    PidSpec(0x07, "ltft_b1", "Long-term fuel trim, bank 1", "%", 1, _trim, "A*100/128-100"),
    PidSpec(0x08, "stft_b2", "Short-term fuel trim, bank 2", "%", 1, _trim, "A*100/128-100"),
    PidSpec(0x09, "ltft_b2", "Long-term fuel trim, bank 2", "%", 1, _trim, "A*100/128-100"),
    PidSpec(
        0x0B,
        "map_abs",
        "Intake manifold absolute pressure (MAP)",
        "kPa",
        1,
        lambda d: float(_a(d)),
        "A",
        note="Absolute pressure. This is NOT boost gauge pressure.",
    ),
    PidSpec(0x0C, "rpm", "Engine speed", "rpm", 2, lambda d: _ab(d) / 4, "(256A+B)/4"),
    PidSpec(0x0D, "speed", "Vehicle speed", "km/h", 1, lambda d: float(_a(d)), "A"),
    PidSpec(0x0E, "timing_advance", "Timing advance", "° before TDC", 1, lambda d: _a(d) / 2 - 64, "A/2-64"),
    PidSpec(0x0F, "iat", "Intake air temperature", "°C", 1, lambda d: _a(d) - 40, "A-40"),
    PidSpec(0x10, "maf", "Mass air flow (MAF)", "g/s", 2, lambda d: _ab(d) / 100, "(256A+B)/100"),
    PidSpec(0x11, "throttle", "Throttle position", "%", 1, lambda d: _a(d) * 100 / 255, "A*100/255"),
    PidSpec(
        0x14,
        "o2_b1s1_v",
        "O2 sensor bank 1 sensor 1 voltage",
        "V",
        2,
        lambda d: _a(d) / 200,
        "A/200",
        note="Byte B (sensor short-term trim) is kept in raw hex only.",
    ),
    PidSpec(0x15, "o2_b1s2_v", "O2 sensor bank 1 sensor 2 voltage", "V", 2, lambda d: _a(d) / 200, "A/200"),
    PidSpec(0x18, "o2_b2s1_v", "O2 sensor bank 2 sensor 1 voltage", "V", 2, lambda d: _a(d) / 200, "A/200"),
    PidSpec(0x19, "o2_b2s2_v", "O2 sensor bank 2 sensor 2 voltage", "V", 2, lambda d: _a(d) / 200, "A/200"),
    PidSpec(0x1F, "run_time", "Run time since engine start", "s", 2, lambda d: float(_ab(d)), "256A+B"),
    PidSpec(0x21, "dist_mil", "Distance travelled with MIL on", "km", 2, lambda d: float(_ab(d)), "256A+B"),
    PidSpec(
        0x22,
        "rail_pressure_rel",
        "Fuel rail pressure (relative to manifold vacuum)",
        "kPa",
        2,
        lambda d: _ab(d) * 0.079,
        "(256A+B)*0.079",
    ),
    PidSpec(
        0x23,
        "rail_pressure_gauge",
        "Fuel rail gauge pressure",
        "kPa",
        2,
        lambda d: _ab(d) * 10.0,
        "(256A+B)*10",
    ),
    PidSpec(0x2C, "egr_commanded", "Commanded EGR", "%", 1, lambda d: _a(d) * 100 / 255, "A*100/255"),
    PidSpec(0x2D, "egr_error", "EGR error", "%", 1, _trim, "A*100/128-100"),
    PidSpec(0x2F, "fuel_level", "Fuel tank level input", "%", 1, lambda d: _a(d) * 100 / 255, "A*100/255"),
    PidSpec(
        0x30, "warmups_since_clear", "Warm-ups since codes cleared", "count", 1, lambda d: float(_a(d)), "A"
    ),
    PidSpec(
        0x31, "dist_since_clear", "Distance since codes cleared", "km", 2, lambda d: float(_ab(d)), "256A+B"
    ),
    PidSpec(0x33, "baro", "Barometric (ambient absolute) pressure", "kPa", 1, lambda d: float(_a(d)), "A"),
    PidSpec(
        0x42, "module_voltage", "Control module voltage", "V", 2, lambda d: _ab(d) / 1000, "(256A+B)/1000"
    ),
    PidSpec(
        0x43, "abs_load", "Absolute load value", "%", 2, lambda d: _ab(d) * 100 / 255, "(256A+B)*100/255"
    ),
    PidSpec(
        0x45, "rel_throttle", "Relative throttle position", "%", 1, lambda d: _a(d) * 100 / 255, "A*100/255"
    ),
    PidSpec(0x46, "ambient_temp", "Ambient air temperature", "°C", 1, lambda d: _a(d) - 40, "A-40"),
    PidSpec(
        0x49,
        "accel_pedal_d",
        "Accelerator pedal position D",
        "%",
        1,
        lambda d: _a(d) * 100 / 255,
        "A*100/255",
    ),
    PidSpec(0x4D, "time_mil", "Time run with MIL on", "min", 2, lambda d: float(_ab(d)), "256A+B"),
    PidSpec(
        0x4E, "time_since_clear", "Time since codes cleared", "min", 2, lambda d: float(_ab(d)), "256A+B"
    ),
    PidSpec(
        0x59,
        "rail_pressure_abs",
        "Fuel rail absolute pressure",
        "kPa",
        2,
        lambda d: _ab(d) * 10.0,
        "(256A+B)*10",
    ),
    PidSpec(0x5C, "oil_temp", "Engine oil temperature", "°C", 1, lambda d: _a(d) - 40, "A-40"),
    PidSpec(0x5E, "fuel_rate", "Engine fuel rate", "L/h", 2, lambda d: _ab(d) / 20, "(256A+B)/20"),
]

PIDS: dict[int, PidSpec] = {s.pid: s for s in _SPECS}
KEY_TO_PID: dict[str, int] = {s.key: s.pid for s in _SPECS}

#: PID 01 is decoded separately (monitor status: MIL + DTC count).
PID_MONITOR_STATUS = 0x01

#: Standard PIDs this tool recognises by name but does NOT decode to physical values.
RAW_ONLY_PIDS: dict[int, str] = {
    0x03: "Fuel system status",
    0x13: "O2 sensors present",
    0x1C: "OBD standard this vehicle conforms to",
    0x41: "Monitor status this drive cycle",
    0x51: "Fuel type",
    0x5D: "Fuel injection timing",
    0x61: "Driver's demand engine torque",
    0x62: "Actual engine torque",
    0x63: "Engine reference torque",
    0x64: "Engine percent torque data",
    0x65: "Auxiliary input / output supported",
    0x66: "Mass air flow sensor (extended)",
    0x67: "Engine coolant temperature (extended)",
    0x68: "Intake air temperature sensor (extended)",
    0x69: "Commanded EGR and EGR error (extended)",
    0x6A: "Commanded diesel intake air flow control",
    0x6B: "EGR temperature",
    0x6C: "Commanded throttle actuator control",
    0x6D: "Fuel pressure control system",
    0x6E: "Injection pressure control system",
    0x6F: "Turbocharger compressor inlet pressure",
    0x70: "Boost pressure control",
    0x71: "Variable geometry turbo (VGT) control",
    0x72: "Wastegate control",
    0x73: "Exhaust pressure",
    0x74: "Turbocharger RPM",
    0x75: "Turbocharger temperature A",
    0x76: "Turbocharger temperature B",
    0x77: "Charge air cooler temperature",
    0x78: "Exhaust gas temperature bank 1",
    0x79: "Exhaust gas temperature bank 2",
    0x7A: "Diesel particulate filter (DPF) bank 1",
    0x7B: "Diesel particulate filter (DPF) bank 2",
    0x7C: "Diesel particulate filter (DPF) temperature",
    0x83: "NOx sensor",
    0x84: "Manifold surface temperature",
    0x85: "NOx reagent system",
    0x86: "Particulate matter (PM) sensor",
    0x87: "Intake manifold absolute pressure (extended)",
    0x8B: "Diesel aftertreatment status",
}


def pid_name(pid: int) -> str:
    if pid in PIDS:
        return PIDS[pid].name
    if pid == PID_MONITOR_STATUS:
        return "Monitor status since DTCs cleared (MIL, DTC count)"
    if pid in RAW_ONLY_PIDS:
        return RAW_ONLY_PIDS[pid]
    return f"PID {pid:02X} (no definition in this tool)"


def decode_supported_bitmap(base: int, data: bytes) -> list[int]:
    """4 bitmap bytes -> list of supported PIDs in (base, base+0x20]."""
    if len(data) < 4:
        raise ValueError("supported-PID bitmap needs 4 bytes")
    bits = int.from_bytes(data[:4], "big")
    return [base + i + 1 for i in range(32) if bits & (1 << (31 - i))]


def decode_monitor_status(data: bytes) -> dict[str, object]:
    """PID 01: A7 = MIL, A0-6 = DTC count, B3 = compression-ignition monitors."""
    if len(data) < 4:
        raise ValueError("PID 01 needs 4 bytes")
    return {
        "mil_on": bool(data[0] & 0x80),
        "dtc_count": data[0] & 0x7F,
        "compression_ignition": bool(data[1] & 0x08),
    }


def decode_dtc(b1: int, b2: int) -> str:
    """Two DTC bytes -> SAE J2012 code such as P0401."""
    letter = "PCBU"[b1 >> 6]
    return f"{letter}{(b1 >> 4) & 0x3:X}{b1 & 0xF:X}{b2 >> 4:X}{b2 & 0xF:X}"


def encode_dtc(code: str) -> bytes:
    letter = "PCBU".index(code[0].upper())
    d = [int(c, 16) for c in code[1:5]]
    return bytes([(letter << 6) | (d[0] << 4) | d[1], (d[2] << 4) | d[3]])
