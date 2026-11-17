# Safety and scope

## What the tool is

A read-only investigation aid. It reads diagnostic data and does calculations on the
Mac. It is not a service or programming tool, and it does not certify that a vehicle is
roadworthy.

## What it cannot do, and how that is enforced

It cannot clear faults, reset adaptations or learned values, clear readiness, trigger a
DPF regeneration, actuate EGR / throttle / injectors / fans, change coding, immobiliser,
firmware or emissions settings, send arbitrary UDS or ISO-TP requests, or scan for
service or data identifiers.

| Layer | Enforcement |
|---|---|
| Protocol boundary | `backend/app/obd/allowlist.py`. Every request is an `AllowedCommand`, which validates itself on construction. Allowed: OBD services 01, 02 (frame 00), 03, 06, 07, 09 (InfoTypes 00, 02, 04, 06, 0A), 0A, and twelve adapter-setup AT commands. Service 04 (clear), 08 (control) and every UDS/KWP service are rejected. So are `ATSH`, `ATCRA`, `ATFC*`, `ATPP`, `ATMA` and any other AT command. |
| Transports | Only accept `AllowedCommand` objects. Passing a string raises. |
| Client | `ObdClient` has no method that takes a free-form command. |
| Service, API, CLI, UI | No raw console, no endpoint or command that forwards adapter text. Tests assert this for the route table and the UI controls. |
| Manufacturer extensions | Off. An extension can only declare requests that already pass the allowlist, which today admits no manufacturer request. |
| Tests | `tests/test_allowlist.py` runs scan + inventory + recording against the simulator and checks every command the fake adapter ever received. |

`ATZ` resets the adapter, not the car. `ATSP0` asks the adapter to auto-detect the
protocol; on K-line vehicles that involves the normal OBD bus initialisation.

## Continuous monitoring

- No new request type: monitoring uses the same allowlisted generic OBD reads plus the
  adapter-only `ATRV`. No DPF regeneration, fan activation, DTC clearing or adaptation
  reset is possible, and no BMW-specific request exists.
- When the ECU stops answering (typically ignition off) the recorder sends no vehicle
  requests; it does not try to wake modules. Only the adapter voltage (`ATRV`, adapter-local)
  is read. If the adapter's Wi-Fi drops meanwhile, the recorder only re-opens the TCP link and
  sends `ATRV`: it never re-initialises the adapter (that would end with a `0100` probe on
  the vehicle bus) and never sends a vehicle request to test the restored link. Vehicle
  polling resumes only on an explicit user request (or an opt-in sparse probe); after an
  adapter-only reconnect that request re-initialises the adapter first, and the dashboard
  says so.
- Loss of ECU communication is never recorded as "ignition off".
- The dashboard is for a passenger or stationary use; the recording runs unattended.

## Using it safely

- Default: vehicle stationary, parked safely, outdoors or well ventilated, ignition on
  with engine off, or idling when that is safe.
- Road recording is opt-in. A passenger operates the laptop. The driver does not
  interact with it.
- **Stop testing and seek professional inspection** if the engine runs severely
  irregularly, there is metallic knocking, it overheats, you smell exhaust or coolant,
  or the MIL flashes.
- Exhaust gas in an enclosed space is dangerous. Do not idle the engine in a closed garage.

## Privacy

- Everything stays on the Mac. No cloud APIs, no telemetry, service bound to `127.0.0.1`.
- The VIN is not requested unless you ask (`--read-vin`, or the checkbox on the Faults
  screen). When read, it is stored in the local database only.
- Exports redact the VIN by default, in the metadata and in the raw adapter trace.
  Including it takes an explicit choice (`--include-vin`, or the checkbox next to Export).
- Fixtures in the repository contain no VIN.

## Limits of the analysis

- Findings are hypotheses with evidence for and against, graded insufficient /
  suggestive / strong. They are not probabilities and not a diagnosis.
- A generic trouble code names a monitored system, not a failed part.
- Thresholds (for example "unstable idle window") are screening heuristics chosen for
  this tool. They are not manufacturer limits.
- Signals are polled one after another, so the tool cannot tell which signal moved first.
