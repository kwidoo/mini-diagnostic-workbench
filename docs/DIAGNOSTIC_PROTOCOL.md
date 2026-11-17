# Diagnostic protocol notes

How the tool talks to the adapter, and the assumptions that the first real session
will confirm or correct.

## Initialisation

Sent in this order, each logged with its response (visible on the Connection screen and
in every session's raw trace):

| Command | Purpose |
|---|---|
| `ATZ` | reset the adapter (not the car) |
| `ATE0` | echo off (echoes are also stripped if present) |
| `ATL0` | linefeeds off (CR, LF and CRLF are all normalised) |
| `ATS1` | spaces on |
| `ATH1` | headers on, so each ECU's answer is identifiable |
| `ATAT1` | default adaptive timing |
| `ATSP0` | automatic protocol selection |
| `ATI`, `AT@1`, `ATRV` | identity, description, adapter-measured voltage |
| `0100` | first OBD request; brings the bus up (`SEARCHING...` / `BUS INIT`) |
| `ATDPN`, `ATDP` | read back the protocol the adapter settled on |

The protocol is read back, never assumed. CAN (6-9) and K-line (3-5) are decoded;
anything else stops with an explicit error. If the transport reconnects, the whole
sequence runs again before the next request, because the adapter may have reset.

## Framing

A response is everything up to the `>` prompt. The parser handles: command echo,
`SEARCHING...`, `BUS INIT: OK` / `...ERROR`, `NO DATA`, `?`, `STOPPED`,
`UNABLE TO CONNECT`, `CAN ERROR`, `BUS BUSY`, `BUFFER FULL`, `DATA ERROR`, NUL bytes, a
timeout with no prompt, and a partial response from a dropped connection. Each becomes
a status. None becomes a value.

### CAN (ISO 15765-4) - assumption A1

With headers on, an ELM327 prints one line per CAN frame: identifier, then the frame's
data bytes **including the ISO-TP PCI byte**, and does not join multi-frame messages.
The adapter still sends the flow-control frame itself (ELM default). So this tool does
the ISO-TP reassembly, per source ECU:

- `0N ..` single frame, N payload bytes;
- `1L LL ..` first frame with total length; `2N ..` consecutive frames, N must count up;
- frames from different ECUs may interleave and are kept apart;
- a sequence error or a missing frame gives an *incomplete* message, which is reported
  and not decoded. Frames are never concatenated on a guess.

Lines are accepted with or without spaces, with 11-bit (`7E8`) or 29-bit (`18DAF110`)
identifiers.

**To check on the car:** in the raw trace of the first scan, the answer to `0100`
should look like `7E8 06 41 00 xx xx xx xx`. If the Vgate formats lines differently,
discovery will fail visibly ("malformed frame") and the parser needs adjusting; it will
not silently mis-decode.

### K-line (ISO 9141-2 / ISO 14230-4) - assumption A2

With headers on, each message is one line: three header bytes, data, one checksum byte.
The third header byte is the source address and identifies the ECU. The checksum (8-bit
sum) is verified; a mismatch marks the message incomplete. DTC responses carry three
codes per message with no count byte; VIN and calibration ID arrive as numbered
messages. Tested against the simulator only.

## Requests

One request is outstanding at a time (a lock in the transport). Each exchange records
the request and response bytes, wall-clock and monotonic time, latency and status. An
optional minimum interval rate-limits polling. Before each request any late bytes from
an earlier timed-out request are discarded, so a late answer is never attributed to the
next request.

Only single-PID service 01 requests are used. Multi-PID requests are faster on some
ECUs but not universally supported, and are excluded by the allowlist.

## Decoding rules

- Service 01/02: positive response `41 pid ..` / `42 pid 00 ..`. One reading per
  answering ECU; they are never merged. Recordings and charts use the primary ECU (the
  one supporting the most PIDs).
- Service 03/07/0A on CAN: `43 count dtc dtc ..`; two bytes per DTC, `0000` is padding.
- Negative response `7F service code` is reported as "negative", with the code.
- A PID without a verified decoder gives quality `raw_only`: bytes kept, no number.

## Timing and sampling

PIDs are polled sequentially: the fast group every cycle plus one slow-group signal per
cycle. Two signals are therefore never sampled at the same instant. Each sample keeps
its own timestamp; nothing is interpolated or resampled for storage. Per-signal metrics
(actual Hz, attempts, drops, error rate, latency mean / p95 / max, largest gap) are
computed from what was stored. Analysis compares variability inside 5-second windows
and says explicitly that this shows association in time, not order or cause.

## Manufacturer-specific requests

None exist in this codebase. See OBD_SUPPORT_MATRIX.md for what adding one requires.
