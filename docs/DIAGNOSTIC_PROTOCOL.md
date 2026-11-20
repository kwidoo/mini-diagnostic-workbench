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

Init answers are checked, not just logged: `ATZ` must answer with an `ELM` identity,
`ATE0`/`ATH1`/`ATSP0` must answer exactly `OK`, `ATDPN` must be a protocol number. Any
other answer means the reply stream is out of step with the requests (for example a
late ECU answer read as the `ATZ` reply); init then fails and the connection is
discarded. Optional commands (`ATL0`, `ATS1`, `ATAT1`) may also answer `?` (clones);
`ATI` must contain `ELM`, `ATRV` must be a voltage, `0100` must carry a `41 00` answer.

Init is all-or-nothing per connection. The sequence runs as one *attempt* pinned to one
TCP connection (`Transport.begin_initialization`). Every init exchange - required or
optional AT command, the `0100` probe, the protocol readback - must complete cleanly with
a prompt; an incomplete one (timeout, partial `OK` without `>`, bytes after the prompt,
dropped TCP) aborts the attempt whatever text it carried. The attempt never reconnects
and never continues on another connection: the next attempt starts again with `ATZ` on a
fresh one. Vehicle requests are allowed only once an attempt has completed on the
current connection; a connection that was never initialised is refused just like one
that was re-established. The `0100` probe is the only vehicle request an attempt may
send. Each call to `ObdClient.initialize()` makes one attempt (no retry loop); failures
are counted in the transport status (`init_state`, `init_failures`) and the raw
exchanges stay in the init log/trace.

## Resynchronisation

The ELM327 link has no request ids: the only way to pair a reply with its request is
order. After an exchange that did not end cleanly with a `>` prompt, the position in
the stream is unknown and a late reply could be read as the answer to the next request.
The rules (backend/app/adapters/base.py, tcp.py, obd/client.py):

| Event | Exchange status | Reading | What happens to the connection |
|---|---|---|---|
| no prompt before the deadline | `timeout` | `timeout`, no value | marked unsynchronised |
| user/stop cancelled the request | `cancelled` | `timeout`, no value | marked unsynchronised |
| partial reply, then nothing | `timeout` (raw partial bytes kept) | `timeout`, no value | marked unsynchronised |
| TCP dropped mid-reply | `closed` (raw partial bytes kept) | `disconnected` | dropped |
| bytes after the `>` prompt | `desync` (raw kept) | `error`, no value | marked unsynchronised |
| bytes waiting *before* a request is sent | `desync` (stray bytes kept); request **not sent** | `error`, no value | marked unsynchronised |

An unsynchronised connection is never reused. Before the next request the client
(1) closes it and opens a new TCP connection, (2) runs the full init sequence above on
the new connection, (3) only then sends the vehicle request. The transport refuses any
vehicle (non-AT) request on a connection the adapter has not been re-initialised on
(`TransportUnsyncedError`). A reply from an incomplete exchange is never decoded into a
value, live or on replay. Sessions store the adapter-link diagnostics both when they
start (`meta.transport_at_start`) and when they end (`meta.transport`, the final values
used by exports and reports). Regression tests: backend/tests/test_init_sync.py,
backend/tests/test_resync.py (real sockets
against the fake ELM327, with the late reply arriving after the next request was sent).

Not hardware-validated: whether a real Vgate adapter forwards a late ECU answer to a
*new* TCP client is unknown. The `ATZ` identity check covers that case; the first real
sessions should be checked for `desync` exchanges and the `resyncs` counter.

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

EGR error (PID 2D) is summarised with signed mean, mean |error|, max |error|, 5-s windows
with large |error| and windows around user annotations, so +/- deviations cannot cancel.
It needs at least 20 samples over 10 s; sampling below 1 Hz is reported as a limitation.
The 15 % level is a screening heuristic of this tool, not a vehicle-specific threshold.

## Manufacturer-specific requests (DDE-03 guarded path)

No manufacturer request is executable for the real car today: the BMW catalog
(DDE_REQUEST_CATALOG.md) has no verified entry. The path below exists so that a verified
entry, once it exists, can run safely. It is exercised against the simulator with
invented addressing only.

- **No raw API.** The generic allowlist (`AllowedCommand`) is unchanged: it still
  rejects every manufacturer service and every addressing AT command. A manufacturer
  request is an `ApprovedRequest`, and only `guarded.approve()` creates one, from an
  entry the catalog gate calls executable (opt-in, vehicle verification, variant,
  decoder, DDE-01 verdict) on an adapter that reports ISO 15765-4 CAN 11/500.
  `GuardedCommand` validates itself: exact patterns for `AT SH hhh`, `AT CEA [hh]`,
  `AT CRA [hhh]`, `AT FC SH hhh`, `AT FC SD ..`, `AT FC SM n`, and read-only services for
  the payload.
- **One transaction, one lock hold.** `Transport.send_manufacturer` sends set-up, request
  and restore without releasing the transport lock, so no generic request can run in
  between. One request is outstanding at a time across generic and manufacturer reads.
- **Set-up:** `AT SH <tx>`, `AT CEA <ext>` (extended addressing only), `AT CRA <rx>`
  (receive filter: only the addressed ECU's frames), `AT FC SH / SD / SM` (when the entry
  documents flow control). Each must answer exactly `OK`, otherwise the request is not sent.
  A `?` means the adapter cannot do it: the result is `unsupported_adapter`, and no
  further manufacturer request is sent on that connection (no retry, no probing).
- **Restore to the generic baseline:** `AT SH 7DF`, `AT CEA`, `AT CRA`, `AT FC SM 0`,
  always, each checked for `OK`. If restoring fails, or the request timed out / the link
  dropped / desynchronised, the connection is invalidated. The next request of any kind
  re-opens it and re-initialises the adapter from `ATZ`, which resets every ELM setting.
  Addressing can never leak into generic OBD or into another profile or session.
- **No re-initialisation for a DDE read.** `ObdClient.manufacturer_exchange` sends only
  on an already initialised connection. Otherwise the read is `not_attempted`. Generic
  polling re-initialises under the existing rules; a manufacturer read never wakes a link
  by itself.
- **Decoding (assumption A3, to verify on the car):** with `ATH1` and `AT CEA`, each
  received frame is printed as `<rx id> <ext> <PCI> <data>`. Every frame must come from
  the entry's response id with the expected extended address, otherwise the whole reply is
  untrusted (`error`). ISO-TP reassembly is the same strict per-ECU reassembly as above.
  The positive SID and echoed identifier, the length bounds and the documented range are
  checked by the catalog before a decoder runs.
- **Result qualities:** `ok` (value from a verified decoder), `raw_only`, `no_data`,
  `negative` (NRC kept), `timeout`, `disconnected`, `error` (framing / foreign frames /
  incomplete ISO-TP / layout), `unsupported_adapter`, `not_attempted` (gate or link). Only
  `ok` carries a value.

Controls: `mini-obd dde-settings --opt-in --variant <confirmed>` (or `--opt-out`,
`--clear-variant`), `PUT /api/dde/settings`, and the Vehicle tab ("Manufacturer reads").
The default is opted out with no variant. `mini-obd dde-settings` shows, per metric, why
it is blocked. Tests: `backend/tests/test_dde_transport.py` (fake ELM over TCP: exact
transactions, restore, NRC, NO DATA, partial ISO-TP, foreign frames, timeout, drop,
restore failure, unsupported adapter, mixed generic/DDE polling from two threads).
