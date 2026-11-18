# MINI Diagnostic Workbench

A local-first, **read-only** OBD investigation tool. It talks to a Vgate iCar Pro
Wi-Fi (ELM327-compatible) adapter from a Mac, reads generic OBD data, records it with
honest timestamps and quality flags, and produces evidence-based findings. It was
built for one incident (a 2016 MINI Countryman 2.0 diesel with intermittent rough
idle) and is structured as a multi-vehicle platform.

It never clears faults, resets adaptations, actuates components, triggers a DPF
regeneration, codes or programs anything. See [docs/SAFETY.md](docs/SAFETY.md).

## What it can and cannot read

| | Status |
|---|---|
| Generic OBD live data (service 01), freeze frame (02), stored / pending / permanent DTCs (03 / 07 / 0A), vehicle info (09), raw monitor results (06) | Implemented. What the car actually supports is discovered at connect time. |
| BMW DDE data: fault codes 4862 / 480A, DPF soot load and regeneration history, DPF differential pressure, EGR target/actual, injector corrections, commanded rail pressure | **Not available.** Shown as "not supported / not measured". Needs a BMW-capable tester. |
| Porsche ME7.8 specific data | **Not available**, same reason. |

Details: [docs/OBD_SUPPORT_MATRIX.md](docs/OBD_SUPPORT_MATRIX.md).

**Real-car status.** Generic OBD over CAN (11-bit, 500 kbit/s) has been observed on the
MINI in real recordings since 2026-11-18 (adapter `ELM327 v2.2` claim, ECUs `7E8` / `7E9`,
service 01 / 02 / 03 / 06 discovery, stored `P115D`): see
[docs/DDE_CAPABILITY.md](docs/DDE_CAPABILITY.md). That is not a full hardware validation
(ISO-TP multi-frame, service 09 identification and K-line on the Porsche are still
unobserved), so no profile carries the `hardware_validated` flag. The adapter assumptions
are listed in [docs/DIAGNOSTIC_PROTOCOL.md](docs/DIAGNOSTIC_PROTOCOL.md).
Profile status `generic_obd_ready` means generic OBD only: manufacturer DDE/DME
diagnostics are not implemented. Follow the
checklist in [docs/HARDWARE_SETUP_MAC.md](docs/HARDWARE_SETUP_MAC.md) for the first
real session.

### Last verified test run (2026-11-17, Linux VM, Python 3.12+, Node 22)

```
$ cd backend && uv run ruff check app tests && uv run pytest
All checks passed!
301 passed in 153.13s
$ cd frontend && npm run typecheck && npm test
> vue-tsc --noEmit
 Test Files  6 passed (6)
      Tests  35 passed (35)
```

Continuous monitoring (docs/MONITORING.md) is covered by `tests/test_monitoring.py`,
`tests/test_monitoring_api.py` and `src/__tests__/monitoring.spec.ts`, against synthetic
drive cycles only.

Covered since the review follow-up: late/partial/trailing replies and cancel/timeout/
connection loss on real sockets (`tests/test_resync.py`), concurrent lifecycle
operations through the HTTP API (`tests/test_lifecycle.py`), EGR-error statistics
(`tests/test_egr.py`), the hardware checklist against the allowlist
(`tests/test_hardware_checklist.py`), and the UI guards/indicators
(`src/__tests__/lifecycle.spec.ts`). Not covered: real hardware, browser end-to-end.

## Setup on a clean Mac (Apple Silicon)

```bash
brew install uv node            # uv installs Python 3.12 itself
cd mini-diagnostic-workbench
make setup                      # uv sync --frozen  +  npm ci   (pinned lockfiles)
make test                       # backend + frontend test suites, no hardware
make demo                       # offline end-to-end demo on synthetic data
make serve                      # dashboard at http://127.0.0.1:8765
```

No Internet is needed after `make setup`. The service binds to `127.0.0.1`. Data lives
in `~/.mini-obd/workbench.sqlite3` (override with `MINI_OBD_DB`).

Docker is optional and covers tests, replay and the simulator:
`make docker-test`, `make docker-demo`, `make docker-serve`.

## Command line

Run from `backend/` as `uv run mini-obd ...`.

```bash
mini-obd profiles                               # list vehicle profiles; --use <id> selects one
mini-obd probe --host 192.168.0.10 --port 35000 # adapter identity, protocol, ECUs
mini-obd scan                                   # stored / pending / permanent DTCs + freeze frame
mini-obd scan --read-vin                        # also read the VIN (kept local)
mini-obd inventory                              # which PIDs the ECU supports, each read once
mini-obd dde-capability                         # DDE-01: adapter + ECU identity report, no BMW request
mini-obd dde-capability --session <id>          # same, offline from a stored session (read-only)
mini-obd dde-catalog                            # DDE-02: BMW request catalog, per metric blocked / executable
mini-obd dde-settings --opt-in --variant <v>    # DDE-03: opt in + record the DDE variant a BMW tester confirmed
mini-obd record --duration 120 --profile idle   # timed recording with pre/post fault scans
mini-obd record --profile mini_extended_monitoring --until-stop   # continuous; Ctrl-C stops and saves
mini-obd annotate --event fan_running           # predefined event (also while another process records)
mini-obd monitoring                             # shutdowns, fan runs, voltage, DPF availability
mini-obd drive-type city                        # label the latest session
mini-obd replay ../fixtures/rough-idle.jsonl    # import a transcript as a session
mini-obd sessions
mini-obd analyze --session <session-id>         # or omit for the latest
mini-obd export --session <session-id> --format zip   # VIN redacted unless --include-vin
mini-obd serve                                  # API + dashboard
```

Add `--simulator <scenario>` to `probe`, `scan`, `inventory` or `record` to use the fake
adapter (`healthy_idle`, `rough_idle`, `egr_mismatch`, `dpf_history`, `wifi_drop`,
`unsupported_pids`, `slow_adapter`, `porsche_healthy_idle`, `porsche_bank_imbalance`, and the
synthetic drive cycles `mini_city_drive`, `mini_highway_drive`, `mini_warmup`,
`mini_dpf_regen_possible`, `mini_shutdown_during_regen`, `mini_voltage_drop`,
`mini_ignition_off`, `mini_ecu_silent_running`, `mini_dde_capability`). Continuous monitoring: docs/MONITORING.md.
Add `--vehicle porsche-boxster-986-mk2` before the command for the other profile.

## First run on the actual MINI

1. Park outdoors or somewhere well ventilated. Handbrake on, gearbox in neutral/park.
2. Plug the Vgate iCar Pro into the OBD socket. Ignition **on**, engine **off**.
3. On the Mac, join the adapter's Wi-Fi (default SSID `V-LINK`). The Mac will have no
   Internet while joined; that is expected.
4. `cd backend && uv run mini-obd probe` and check it reports an adapter identity and a
   CAN protocol. If it says it cannot reach the adapter, see
   [docs/HARDWARE_SETUP_MAC.md](docs/HARDWARE_SETUP_MAC.md).
5. `uv run mini-obd scan`. Note every code. Nothing is cleared.
6. `uv run mini-obd inventory`. This is the important one: it shows which PIDs this ECU
   really offers. Look for MAF, MAP, barometric pressure, rail pressure, EGR PIDs, and
   any extended diesel PIDs listed as "raw-only" (for example 7A, DPF).
7. `make serve`, open http://127.0.0.1:8765, connect, and record 300 s at idle from the
   Live tab. Only start the engine if it is safe. Press the event-marker buttons the
   moment the idle goes rough or the MIL comes on.
8. Repeat cold and fully warm, on different days. Then open Findings and Sessions & compare.
9. Stop immediately on metallic knocking, overheating, exhaust or coolant smell, or a
   flashing MIL, and have the car inspected.
10. Export the session ZIP for your records or your mechanic.

## Layout

```
backend/app/
  vehicles/      vehicle profiles (MINI R60 diesel, Porsche 986)       <- vehicle-specific
  ecus/          ECU definitions and their unavailable parameters      <- vehicle-specific
  diagnostics/   analysis engine + rules selected per profile
  parameters/    standard SAE J1979 PID definitions                    <- shared
  obd/           allowlist, ELM framing, CAN / K-line decoding, client <- shared
  adapters/      transports: Vgate Wi-Fi TCP, simulator                <- shared
  acquisition/   recorder, sampling metrics, live hub                  <- shared
  storage/       SQLite, transcript replay, export                     <- shared
  manufacturer/  read-only extension interface, off unless verified
  api/  cli.py  service.py
frontend/        Vue 3 + TypeScript + Vite + Pinia + Tailwind v4
fixtures/        synthetic transcripts
docs/
```

More: [ARCHITECTURE](docs/ARCHITECTURE.md), [VEHICLE_PROFILES](docs/VEHICLE_PROFILES.md).
