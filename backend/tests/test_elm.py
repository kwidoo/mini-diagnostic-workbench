import pytest

from app.obd.elm import (
    CanFrame,
    ElmStatus,
    InvalidFrameError,
    assemble_isotp,
    decode_can_reply,
    decode_kline_reply,
    parse_can_line,
    parse_kline_line,
    parse_reply,
    protocol_family,
)


def test_echo_crlf_nul_and_prompt_are_normalised():
    r = parse_reply(b"010C\r\n\x007E8 04 41 0C 0C 80\r\n\r\n>", "010C")
    assert r.status == ElmStatus.OK and r.prompt_seen
    assert r.lines == ["7E8 04 41 0C 0C 80"]


def test_searching_is_a_note_not_data():
    r = parse_reply(b"SEARCHING...\r7E8 06 41 00 BE 3F A8 13\r\r>", "0100")
    assert r.status == ElmStatus.OK and r.notes == ["SEARCHING..."] and len(r.lines) == 1


@pytest.mark.parametrize(
    "raw,status",
    [
        (b"NO DATA\r\r>", ElmStatus.NO_DATA),
        (b"?\r\r>", ElmStatus.UNKNOWN_COMMAND),
        (b"STOPPED\r\r>", ElmStatus.STOPPED),
        (b"SEARCHING...\rUNABLE TO CONNECT\r\r>", ElmStatus.UNABLE_TO_CONNECT),
        (b"BUS INIT: ...ERROR\r\r>", ElmStatus.BUS_ERROR),
        (b"CAN ERROR\r\r>", ElmStatus.BUS_ERROR),
        (b"BUFFER FULL\r\r>", ElmStatus.BUFFER_FULL),
        (b"\r\r>", ElmStatus.EMPTY),
    ],
)
def test_adapter_status_tokens(raw, status):
    assert parse_reply(raw, "0100").status == status


def test_bus_init_ok_is_not_an_error():
    r = parse_reply(b"BUS INIT: OK\r48 6B 10 41 0C 0B B8 D3\r\r>", "010C")
    assert r.status == ElmStatus.OK and r.notes == ["BUS INIT: OK"]


def test_timeout_and_partial_are_distinguished():
    assert parse_reply(b"7E8 04 41 0C", "010C", timed_out=True).status == ElmStatus.TIMEOUT
    assert parse_reply(b"7E8 04 41 0C", "010C").status == ElmStatus.PARTIAL


def test_data_flagged_by_buffer_full_is_not_ok():
    assert (
        parse_reply(b"7E8 10 14 49 02 01 57 4D 57\rBUFFER FULL\r\r>", "0902").status == ElmStatus.BUFFER_FULL
    )


def test_can_line_with_and_without_spaces_and_29bit():
    assert parse_can_line("7E8 04 41 0C 0C 80") == CanFrame("7E8", bytes.fromhex("04410C0C80"))
    assert parse_can_line("7E804410C0C80") == CanFrame("7E8", bytes.fromhex("04410C0C80"))
    assert parse_can_line("18 DA F1 10 04 41 0C 0C 80").ecu == "18DAF110"
    assert parse_can_line("18DAF11004410C0C80", is_29bit=True).ecu == "18DAF110"


@pytest.mark.parametrize(
    "line", ["", "7E8", "7EX 04 41", "7E8 04 4", "41 0C 0C 80", "7E8 01 02 03 04 05 06 07 08 09", "ZZ"]
)
def test_invalid_headers_and_frames_are_rejected(line):
    with pytest.raises(InvalidFrameError):
        parse_can_line(line)


def test_multi_ecu_single_frames():
    r = parse_reply(b"7E8 03 41 0D 00\r7E9 03 41 0D 05\r\r>", "010D")
    msgs, problems = decode_can_reply(r)
    assert not problems
    assert [(m.ecu, m.payload.hex()) for m in msgs] == [("7E8", "410d00"), ("7E9", "410d05")]


def _f(ecu, hexs):
    return CanFrame(ecu, bytes.fromhex(hexs))


def test_isotp_multiframe_reassembly_interleaved_ecus():
    frames = [
        _f("7E8", "1014490201415A42"),
        _f("7E9", "0343000000000000"),
        _f("7E8", "2143444546474849"),
        _f("7E8", "224A4B4C4D4E4F50"),
    ]
    msgs = assemble_isotp(frames)
    vin = next(m for m in msgs if m.ecu == "7E8")
    assert vin.complete and len(vin.payload) == 0x14
    assert vin.payload[3:].decode() == "AZBCDEFGHIJKLMNOP"
    other = next(m for m in msgs if m.ecu == "7E9")
    assert other.complete and other.payload == bytes.fromhex("430000")


def test_isotp_sequence_error_is_incomplete_not_concatenated():
    msgs = assemble_isotp([_f("7E8", "1014490201415A42"), _f("7E8", "224A4B4C4D4E4F50")])
    assert len(msgs) == 1 and not msgs[0].complete and "sequence error" in msgs[0].problem
    assert len(msgs[0].payload) == 6  # only the first frame's bytes; the out-of-order frame was not appended


def test_isotp_missing_tail_and_orphan_consecutive():
    msgs = assemble_isotp([_f("7E8", "1014490201415A42")])
    assert not msgs[0].complete and "incomplete" in msgs[0].problem
    orphan = assemble_isotp([_f("7E8", "2143444546474849")])
    assert not orphan[0].complete


def test_single_frame_shorter_than_declared():
    assert not assemble_isotp([_f("7E8", "0641")])[0].complete


def test_kline_checksum_and_source():
    m = parse_kline_line("48 6B 10 41 0C 0B B8 D3")
    assert m.complete and m.ecu == "10" and m.payload == bytes.fromhex("410C0BB8")
    bad = parse_kline_line("48 6B 10 41 0C 0B B8 00")
    assert not bad.complete and bad.problem == "checksum mismatch"
    msgs, problems = decode_kline_reply(parse_reply(b"48 6B 10 41 0C 0B B8 00\r\r>"))
    assert problems
    with pytest.raises(InvalidFrameError):
        parse_kline_line("48 6B")


def test_protocol_family():
    assert protocol_family("A6") == "can" and protocol_family("3") == "kline"
    assert protocol_family("A2") == "unsupported" and protocol_family(None) == "unsupported"
