# MINI extended monitoring

Continuous, read-only recording across daily drives, to investigate the radiator fan
running after shutdown, possibly interrupted DPF regenerations, recurring low-battery
warnings, DPF / EGR / intake / temperature relationships, and the owner-reported BMW
DDE faults 480A and 4862.

**Status: simulator-tested only.** Nothing here has been run on the real MINI or the
real Vgate adapter. Every claim below about behaviour on the car is an expectation
to verify, not a result.

## Recording

```bash
cd backend
uv run mini-obd record --profile mini_extended_monitoring --until-stop \
    [--max-duration 14400] [--drive-type city|highway|mixed|other] [--label "school run"]
```

- Runs until **Ctrl-C** (or SIGTERM). The first Ctrl-C stops cleanly: the session is
  closed and kept; a second Ctrl-C aborts at once (the session is still closed with
  everything already written).
- `--max-duration` is an optional safety limit. Without it there is no limit.
- Dashboard: tab **MINI monitoring -> Live -> Start monitoring (until stopped)**, or the
  **Until I press Stop** box on the Live tab. The duration-based recording works as before.
- Samples and adapter exchanges are written to SQLite about once a second; nothing is
  held in memory for the whole drive. Each sample keeps its own timestamp and latency.
- A session holds a lease that is refreshed every 10 s. A session left open by a
  process that died (crash, kill, power loss) is closed on the next start of `serve`
  or of any CLI command: end time = last stored record, `aborted` = "interrupted",
  `interrupted: true`, metrics computed, data kept. A session whose lease is still fresh
  (a recording in another process) is left alone.
- The recording settings (`recording_profile`, `selected`, `sampling_plan`,
  `user_labels`, `until_stop`, `requested_duration_s`, `max_duration_s`) are written to
  the session **when it is created, before the pre-scan or the first sample**. A session
  recovered after a crash therefore still appears in monitoring history, keeps its
  settings, and can be analysed and exported with the monitoring files. Recovery merges
  into that metadata; it never replaces it. A drive-type label changed during the
  recording is not overwritten when the recording ends.
- Session metadata keeps the vehicle profile, adapter info, ECU capabilities, the
  selected signals per tier, requested-but-unsupported signals, the raw ELM transcript,
  final link diagnostics and a `monitoring` summary of what the recorder did.

## Signals and sampling

Capability discovery runs first; only advertised PIDs are polled. Requested but not
advertised PIDs are listed as **not available** and never polled.

| Tier (initial interval) | Signals |
|---|---|
| fast (0.5 s) | 0C RPM, 0D speed, 04 load, 10 MAF, 0B MAP |
| medium (2 s) | 05 coolant, 42 ECU voltage, 2C commanded EGR, 2D EGR error, 0F intake air, rail pressure (23 / 59 / 22, first supported), 1F run time |
| slow (10 s) | 01 MIL / DTC count, 33 baro, 46 ambient, 5C oil temp, 69, 6B, 7A, 7B, 7C, 78, 79, 73, 8B, 86 (all raw only), adapter voltage (ATRV) |

- Requests stay strictly sequential through the existing client lock, allowlist and
  transport resynchronisation (PR #1). Nothing is sent in parallel.
- Scheduling: earliest deadline first, faster tier wins ties. Every 15 s the recorder
  measures the requests per second the link really delivered and keeps the planned
  load under 80 % of it, stretching medium/slow first (up to 4x) and the fast tier only
  if that is not enough. Base and effective intervals, and every adjustment, are stored.
- A PID the ECU advertises but keeps answering with NO DATA / a negative response
  (5 times in a row while other PIDs answer) is paused with a back-off of 60 s, doubling
  to 600 s, instead of being polled forever.
- The dashboard marks a value stale after 3 of its own effective intervals (minimum 3 s).

## Voltage

ECU voltage (PID 42) and adapter voltage (ATRV) are separate signals:

- PID 42 is reported by the ECU (provenance `ECU`).
- ATRV is the supply voltage measured by the adapter itself (provenance `adapter`), stored
  as samples with `mode = 0`. ATRV is an adapter command: it puts nothing on the vehicle
  bus. It is only polled if the adapter answered ATRV during initialisation.

Reported: voltage at the start, while the engine runs, the last value before each
engine stop, values after the stop where readable, large drops (>= 1 V within 5 s,
labelled when they coincide with an engine start or stop), and voltages within
+/-60 s of each battery-warning marker. Voltage is never presented as battery state of
charge or battery health; no alternator, battery or IBS diagnosis is made.

## Events

Predefined MINI events: `engine_started`, `engine_stopped`, `ignition_off`,
`fan_running`, `fan_stopped`, `fan_unusually_loud`, `battery_warning`,
`battery_warning_cleared`, `suspected_dpf_regeneration`, `mil_on`, `mil_off`,
`rough_idle`, `power_loss`, `unusual_engine_noise`.

Each event stores time (wall and, during a recording, monotonic), session id, event
type, optional note, origin and, where useful, evidence:

| Origin | Meaning |
|---|---|
| `manual` | marked by a person (dashboard buttons, `mini-obd annotate --event fan_running`). Live manual events carry the latest measured RPM / coolant / speed / voltages with their age as context. |
| `observed` | created by the recorder from a measured transition: `engine_started` / `engine_stopped` (RPM crossing 300 / 100 rpm), `mil_on` / `mil_off` (PID 01), `adapter_connection_lost` / `_restored` (with the gap), `ecu_communication_lost` / `_restored`. |
| `inferred` | analysis results (possible regeneration etc.). They are recomputed from stored data, never stored as facts, and always say *possible*. |

The API only creates `manual` events. Events can be added during a recording, after the
engine stopped (the session stays open), and after the session was closed (stamped with
the current time). Fan run duration = `fan_running` -> next `fan_stopped`; an unpaired
mark has no duration.

## Engine shutdown and ECU silence

When measured RPM falls to ~0 while the ECU still answers, an `engine_stopped`
(observed) event stores the last running RPM, coolant, ECU voltage and adapter voltage
with their age. The session stays open; whatever the ECU still reports (coolant heat
soak, voltage) keeps being recorded.

When the ECU stops answering (6 failed requests in a row over at least 5 s, adapter link
up), the recorder writes `ecu_communication_lost`. It does **not** infer ignition off.
It then sends **no vehicle request** (sleeping modules are not polled or woken) and only
reads ATRV on the existing, initialised connection. Vehicle polling resumes only when

- the user presses **Check whether the ECU answers (one request)** (dashboard,
  `POST /api/recording/resume-ecu`), or
- the recording was started with the opt-in `--resume-probe SECONDS`: one probe, with
  doubling back-off.

No scan is sent at the end of a recording whose ECU is silent (`post_scan_skipped`).
Whether the ECU answered after the engine stop, for how long, and whether ATRV was
readable afterwards is stored per session under `monitoring.observed_capabilities`:
what the session showed, not an assumption. The fan is never detected; only markers.

### Adapter Wi-Fi lost while vehicle polling is paused

While the ECU is unresponsive the ATRV reads also watch the adapter link. If the
Wi-Fi/TCP connection drops (a cut connection, or two ATRV reads in a row without an
answer), the recorder writes `adapter_connection_lost` (evidence
`ecu_polling_paused: true`) and switches to `adapter_unreachable`. Vehicle polling stays
paused. With back-off it only tries to reach the **adapter**: re-open the TCP
connection and send ATRV. The adapter is **not** re-initialised (initialisation ends
with a `0100` probe on the vehicle bus) and no vehicle request is sent. When the adapter
answers, `adapter_connection_restored` is written with the gap and the recorder returns
to `ecu_unresponsive`. The "check whether the ECU answers" button is not offered while
the adapter is unreachable; after an adapter-only reconnect the dashboard says that the
check re-initialises the adapter first (ATZ ... 0100), and it is only sent when you ask.
You can stop and save at any time. A gap still open at the end is stored as
`monitoring.adapter_connection_lost_at_end` (with `open_gap_s`), together with
`adapter_connection_losses(_while_ecu_paused)` and `adapter_only_reconnects`.

### Shutdown episodes

One physical shutdown often leaves several markers: the observed `engine_stopped`, your
own `engine_stopped` and `ignition_off`, and later the observed `ecu_communication_lost`.
They are grouped into **one shutdown episode**: markers within 300 s of each other, with
no engine start (measured, or your `engine_started` marker) in between. The anchor is
the observed engine stop when there is one, otherwise the earliest manual marker. Each
episode lists the events it groups with their own times and origins (`events`,
`engine_stop`, `ignition_off`, `ecu_communication_lost`); the original events all stay in
the timeline. ECU silence is attached to the shutdown it follows but never creates a
shutdown on its own. Fan runs, post-shutdown coolant/voltage and possibly interrupted
regenerations are reported per episode, and history counts episodes, not markers.

## Analysis (P1)

All screening values live in `backend/app/diagnostics/monitoring.py` (`HEURISTICS`,
exported with every summary). They are tool heuristics, mostly relative to the same
car's own history, not BMW / MINI limits.

- **Possible DPF regeneration**: no verified regeneration-state signal is available,
  so `confirmed_regenerations` is always empty. At warm, stationary idle, 30-s windows
  are compared with the car's usual warm idle (median over its monitoring sessions;
  needs >= 60 samples): commanded EGR <= 25 % of usual (usual >= 10 %), idle RPM +80
  rpm, MAF x1.25. Two indicators for >= 60 s -> *possible* (low confidence), three ->
  *possible* (moderate). Driving-time regenerations are not detectable this way.
- **Possibly interrupted regeneration**: a possible regeneration (or your
  `suspected_dpf_regeneration` marker within 10 min) active within 90 s of an engine
  stop / ECU silence, correlated with fan runs after that stop and the driving
  conditions. One result per shutdown episode.
- **Cooling**: relative to the car's usual warm coolant temperature (median over
  sessions with >= 20 min running): did not reach it (>= 10 degC below), dropped by
  >= 10 degC after warm-up, or repeatedly >= 8 degC above it. Without history only the
  curve is reported.
- **References are never pooled across data origins.** The usual warm coolant
  temperature and the warm-idle RPM / MAF / EGR baseline of a session come only from
  monitoring sessions of the same *cohort*: same vehicle profile, same origin (real
  adapter vs simulator; a replay keeps the origin of the recording it came from) and the
  same confirmed vehicle identity (a VIN actually read, or an explicit
  `vehicle_identity`; nothing is assumed). Adding or removing synthetic recordings does
  not change a real car's references or findings. History rows carry `baseline_origin`;
  `/api/monitoring/history` returns `references` (the real car's, or the synthetic ones
  if there is no real data) and `references_by_cohort` (identities only as a digest).
- **Electrical**: as listed under Voltage, plus "no charging rise seen" (running median
  not 0.3 V above the engine-off median) and running samples below 12.2 V (screen only).
- Findings `monitoring_*` appear only for monitoring sessions and never exceed
  `suggestive`.

## Dashboard (tab "MINI monitoring")

- **Live**: recording state and elapsed time, link state (ECU answering / not
  answering / adapter unreachable), engine state, effective intervals, RPM, speed,
  coolant, ECU voltage, adapter voltage, DPF availability, event buttons, event log.
- **Session details**: stacked charts on a shared time axis with events (dashed =
  manual, blue = observed) and shaded inferred intervals; event table with origin;
  engine stops and fan durations; voltage per source; DPF observations; cooling;
  sampling quality per signal (planned vs actual Hz, gaps, link events).
- **Compare drives**: per session and per drive-type label (city / highway / ...):
  engine stops, fan runs after stop and their mean duration, battery warnings,
  possible and possibly interrupted regenerations.

Badges everywhere: *measured*, *observed*, *manual*, *inferred - possible*, *raw only*,
*not available*.

Controls are for a passenger or for stationary use. The recording needs no
interaction while driving.

## Export

ZIP export of a monitoring session adds: `monitoring.json`, `events.jsonl` (all
timeline events with origin and evidence), `detected_events.json`,
`sampling_quality.json`, `failed_samples.jsonl`, `capabilities.json`,
`vehicle_profile.json`; `report.md` gets a "Continuous monitoring" section and
`annotations.csv` gets event_type / origin / confidence columns. Other sessions export
exactly as before. VIN redaction applies to every file. The transcript header carries
the recording context, so `mini-obd replay transcript.jsonl` rebuilds an equivalent
session (including ATRV samples and events) for offline analysis.

## Not available through this tool

DPF regeneration state and request, soot mass, ash load, distance/time since the last
regeneration, failed-regeneration counters, BMW fault memory (480A, 4862), EGR
target/actual, fan state. Extended standard PIDs (7A/7B/7C DPF, 78/79 EGT, 73 exhaust
pressure, 8B aftertreatment, 86 PM sensor, 69, 6B) are stored raw only if the ECU
offers them; decoders will only be added after their byte layout has been verified
against a real response.

## First extended recording on the car

1. Do the first-hardware checklist in HARDWARE_SETUP_MAC.md first (probe, scan,
   inventory). Note which DPF / EGT PIDs the inventory lists as raw only.
2. Cold car, outdoors. Adapter in the OBD socket, ignition on, Mac joined to V-LINK.
3. `uv run mini-obd serve`, open http://127.0.0.1:8765, Connection -> real adapter.
4. MINI monitoring -> Live: choose the drive type, optionally a safety limit (e.g. 4 h),
   **Start monitoring (until stopped)**. Check that RPM, coolant, ECU voltage and adapter
   voltage tiles fill in and the link shows "ECU answering". Then start the engine.
5. Drive normally. A passenger may mark events; the driver does not touch the laptop.
6. After parking, switch the engine off and leave the recording running. Mark
   `fan_running` when you hear the fan, `ignition_off` when you switch it off,
   `fan_stopped` when it stops, `battery_warning` if the warning appears.
7. Watch the link state: whether the ECU keeps answering after engine stop and whether
   the adapter voltage is still read after `ecu_communication_lost` is exactly what this
   first session tests. Then press **Stop and save**.
8. Session details -> check sampling quality (actual Hz, gaps, adjustments) and the
   transcript (`Export ZIP`) for `desync` / resyncs. Keep the ZIP.
