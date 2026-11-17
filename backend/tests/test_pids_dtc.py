import pytest

from app.parameters.standard_pids import (
    PIDS,
    decode_dtc,
    decode_monitor_status,
    decode_supported_bitmap,
    encode_dtc,
    pid_name,
)


@pytest.mark.parametrize(
    "pid,raw,expected",
    [
        (0x0C, "1AF8", 1726.0),  # (256A+B)/4
        (0x05, "7B", 83),  # A-40
        (0x0F, "28", 0),
        (0x10, "01F4", 5.0),  # (256A+B)/100
        (0x0B, "65", 101),
        (0x0D, "FF", 255),
        (0x04, "FF", 100.0),
        (0x06, "80", 0.0),
        (0x07, "00", -100.0),
        (0x2D, "C0", 50.0),
        (0x23, "0BB8", 30000.0),  # 10 kPa/bit
        (0x59, "0001", 10.0),
        (0x22, "03E8", 79.0),
        (0x42, "3714", 14.1),
        (0x0E, "90", 8.0),
        (0x14, "5AFF", 0.45),
        (0x1F, "0100", 256),
        (0x33, "65", 101),
    ],
)
def test_sae_formulas(pid, raw, expected):
    assert PIDS[pid].decode(bytes.fromhex(raw)) == pytest.approx(expected)


def test_every_pid_has_unit_formula_and_source():
    for spec in PIDS.values():
        assert spec.formula and spec.source.startswith("SAE J1979") and spec.name


def test_map_is_labelled_absolute_not_boost():
    assert "absolute" in PIDS[0x0B].name.lower() and "NOT boost" in PIDS[0x0B].note


def test_bitmap():
    assert decode_supported_bitmap(0x00, bytes.fromhex("80000001")) == [0x01, 0x20]
    assert decode_supported_bitmap(0x20, bytes.fromhex("00000000")) == []
    assert decode_supported_bitmap(0x00, bytes.fromhex("00180000")) == [0x0C, 0x0D]
    with pytest.raises(ValueError):
        decode_supported_bitmap(0, b"\x00")


@pytest.mark.parametrize(
    "raw,code",
    [
        ("0133", "P0133"),
        ("2002", "P2002"),
        ("4123", "C0123"),
        ("8001", "B0001"),
        ("C100", "U0100"),
        ("244A", "P244A"),
    ],
)
def test_dtc_decode_and_roundtrip(raw, code):
    b = bytes.fromhex(raw)
    assert decode_dtc(b[0], b[1]) == code
    assert encode_dtc(code) == b


def test_monitor_status():
    st = decode_monitor_status(bytes.fromhex("830F0000"))
    assert st == {"mil_on": True, "dtc_count": 3, "compression_ignition": True}
    assert decode_monitor_status(bytes.fromhex("00070000"))["mil_on"] is False


def test_unknown_pid_name_is_explicit():
    assert "no definition" in pid_name(0xEE)
    assert "DPF" in pid_name(0x7A)
