# Architecture

## Layers

```
                 Vue dashboard (frontend/)            mini-obd CLI
                          |  REST + WebSocket              |
                          v                                v
                    app/api/main.py  ------------>  app/service.py  (Workbench)
                                                          |
        +----------------------+--------------------------+----------------------+
        |                      |                          |                      |
  vehicles/ + ecus/      acquisition/                diagnostics/            storage/
  (profile: which        recorder, metrics,          engine + rules          SQLite (WAL),
  ECUs, protocols,       live hub                    (hypotheses and         transcript replay,
  signals, rules)             |                      evidence)               export
                              v
                        obd/client.py   safe init, discovery, read-only services
                              |
                        obd/allowlist.py   <- the protocol boundary: AllowedCommand only
                              |
                        obd/elm.py         ELM text framing, CAN ISO-TP, K-line
                              |
                        adapters/          VgateWiFiTransport | SimulatorTransport
                              |
                     Vgate iCar Pro (TCP)  or  FakeElmServer (TCP, synthetic)
```

## Operation lifecycle

`Workbench` owns one non-blocking operation guard shared by connect, disconnect,
profile switch, scan, inventory and record. A second operation is rejected with
`BusyError` (HTTP 409) naming the one in progress; nothing is queued. A recording is
ended with `POST /api/recording/stop`, then `POST /api/disconnect` works. The
connection remembers the profile it was opened for (`connected_profile`): switching
to another profile while connected is refused (disconnect + reconnect re-probes the
vehicle), and a session refuses to start if the stored active profile no longer
matches the connection (e.g. changed by the CLI in the shared database). Process
shutdown (`Workbench.shutdown`) stops a recording, cancels the request in flight and
waits for the session to close before disconnecting. `/api/status` exposes
`operation` and `connected_profile`; the dashboard disables the matching controls.

Below the workbench, `ObdClient` serialises re-init + request, and the transport
serialises single wire exchanges and discards any connection whose reply stream is
out of step (see DIAGNOSTIC_PROTOCOL.md, "Resynchronisation").

## Multi-vehicle separation

| Concern | Where | Vehicle-specific? |
|---|---|---|
| Vehicle profiles | `app/vehicles/profiles.py` | yes: this is the only place that picks ECUs, protocols, signals and rules for a car |
| Transport adapters | `app/adapters/` | no |
| Diagnostic protocols | `app/obd/` (allowlist, ELM framing, CAN, K-line, client) | no; the protocol family comes from the adapter read-back |
| ECU definitions | `app/ecus/definitions.py` | per ECU family; lists manufacturer parameters as unavailable |
| PID / parameter definitions | `app/parameters/standard_pids.py` (SAE standard), ECU definitions (manufacturer) | standard set is shared |
| Diagnostic rules | `app/diagnostics/rules/` | rules are registered by id; each profile lists the ids it uses |

Recording, storage, visualisation, export and the analysis engine never branch on the
vehicle. Every session stores its `vehicle_profile`; the UI filters by the active one.
See VEHICLE_PROFILES.md for adding a vehicle.

## Session model

A session is a `scan`, a `snapshot` (inventory) or a `record`. SQLite tables:
`sessions` (with JSON metadata: adapter info, capabilities, selected signals, metrics),
`exchanges` (the raw adapter trace, each row tagged with a phase: `init`, `discover`,
`scan:pre`, `record`, `scan:post`, ...), `samples` (normalised readings, one row per
ECU answer or failed read), `dtc_scans`, `annotations`. Multi-row writes are single
transactions; the recorder flushes about once a second.

A recording runs: fault scan (pre) -> timed polling -> fault scan (post).

## Transcript format (`mini-obd-transcript/1`)

JSON Lines. Line 1 is a header (`synthetic`, `vehicle_profile`, adapter info,
capabilities). Then one `exchange` per adapter round trip and one `annotation` per event
marker. Fixtures and the `transcript.jsonl` inside an export use this format.
`mini-obd replay` re-decodes the raw responses with the same decoders as the live path,
so a replayed session and the original yield identical samples (tested).

## Export

`mini-obd export` / the Export button produce a ZIP: `session.json`, `samples.csv`,
`samples.json`, `dtcs.json`, `annotations.csv`, `transcript.jsonl`, `findings.json`,
`report.md`, `README.txt`. Fixed entry order and timestamps make the archive
byte-identical for the same database state. VIN redacted by default.

## API

REST under `/api` (status, profiles, vehicle, connect, scan, inventory, recording,
sessions, samples, findings, annotations, export, compare, fixtures, replay) and a
WebSocket `/ws/live` that streams readings, markers and status. Interactive docs at
`/docs` while the service runs. No endpoint carries adapter commands.

## Simulator

`ElmSimulator` is an ELM327 state machine (echo, linefeeds, spaces, headers, protocol
search) in front of a synthetic ECU, with scenarios per vehicle profile. It runs
in-process on a virtual clock (fixtures, fast tests) or as a TCP server
(`FakeElmServer`), so the real socket transport, reconnect logic and API are exercised
without hardware. The simulator records every command it receives, including whether a
clear-DTC request ever arrived; tests assert it never does.

## Optional LLM analysis

Not included. If added, it should stay separate and off by default, consume only the
structured findings / evidence, and have no path to the transport.
