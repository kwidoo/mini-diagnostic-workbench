# MINI extended monitoring

Continuous, read-only recording across daily drives, to investigate the radiator fan
running after shutdown, possibly interrupted DPF regenerations, recurring low-battery
warnings, DPF / EGR / intake / temperature relationships, recurring air-path anomalies
(MAF / EGR / MAP, generic DTCs such as P115D), and the owner-reported BMW DDE faults
480A and 4862.

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

## Air path: MAF / EGR / MAP / DTC (derived automatically)

Nothing to start or press: every monitoring session (also older ones, and replays of an
exported `transcript.jsonl`) is analysed when its summary, report or export is built. The
recording profile, its tiers and its request load are unchanged: no extra PID, no second
sampling loop, no extra DTC polling. All raw samples (timestamp, ECU, quality, raw bytes,
failed reads) stay exactly as recorded; the analysis only reads them. Code:
`backend/app/diagnostics/air_path.py`, thresholds in `HEURISTICS` (`monitoring.py`, keys
`airpath_*`, `maf_*`, `egr_error_screen_pct`), also exported in `monitoring.json`.

**Engine confirmed running.** A MAF sample is judged only if measured RPM right before *and*
right after it (each at most 3 s away) is >= 600 rpm. Both below 300 rpm = engine not
running (a zero is then expected and only counted separately). Anything else (stale or
missing RPM, ECU silence, cranking, a stop between the two RPM samples) = *unknown*, not
judged. A failed read (NO DATA, timeout, disconnect, negative answer) has no value and is
never a zero; an unsupported or unrecorded signal is `not available` and its statistics are
`null`, never 0.

| Result | Rule (screening heuristic, not a manufacturer value) |
|---|---|
| zero MAF while running | decoded MAF exactly 0.00 g/s (raw answer `41 10 00 00` is kept as evidence) |
| low MAF while running | MAF < 1 g/s |
| episode | low-MAF running samples < 5 s apart with no normal MAF in between; *persistent* if >= 3 s or >= 3 samples, else *single transient*; *repeated* if another episode starts within 120 s |
| abrupt dip | MAF falls to <= 50 % of the previous running sample (>= 2 g/s) and is back to >= 80 % within 3 s while RPM changed <= 10 % (excludes most gear changes; driver inputs remain possible) |
| EGR-error excursion | running samples with \|PID 2D\| >= 15 %, < 10 s apart = one excursion (an oscillation through zero stays one, direction "both") |
| cold / warm | coolant (held value <= 30 s old) below / at or above 60 °C, fixed so drives stay comparable |
| idle / moving | speed (held value <= 5 s old) 0 / above 0 |

Each episode keeps start/end time, duration, every MAF value with its raw answer, ECU and
latency (first 20), RPM before/after with their ages, RPM spread in +/-5 s, and speed,
coolant, MAP, load, commanded EGR and EGR error as **held values with their age** and a
`fresh` flag. PIDs are polled one after another; a value older than its limit is marked
stale and is not used for an association. Episodes and dips go to `detected_events.json`
(origin `inferred`, status `candidate`) and are shaded on the session charts.

**Associations, not causes.** For every episode: an EGR-error excursion within +/-5 s (and,
as a rough chance level, how often any running MAF sample had one that close), a MAP change
of >= 5 kPa or a commanded-EGR change of >= 10 % against the 15 s before, idle-RPM
instability (std >= 30 rpm and >= 2x the session's usual idle-window spread; stationary
only), and manual symptom markers (rough idle, power loss, unusual engine noise, free-text
vibration / jerk / noise) within 30 s of any air-path anomaly. Supporting and contradicting
observations are both kept (e.g. "MAP stayed within +/-5 kPa during 4 episodes"). Nothing
names an EGR valve, MAF sensor, turbocharger or intake part as faulty.

**Fault codes.** DTCs come only from the existing pre/post scans (raw code, ECU, scan
phase, stored / pending / permanent) and the MIL from PID 01; nothing is cleared.
Manufacturer-range codes (P1xxx, P30xx-P33xx, B/C/U 1-2) are shown as the raw code: a
description is used only if a verified mapping exists for the vehicle profile **and** a
confirmed ECU variant (none does yet, so P115D is "description not verified"). Such a code is
neither counted as an air-path fault nor allowed to produce "no air-path DTC" evidence. A
code in the pre-scan is reported as set before the session; a code only in the post-scan as
set during it, time unknown. No code is ever tied to the moment of an anomaly. Owner-reported
BMW codes (4862, 480A) are listed separately as history from a BMW tester.

**Longitudinal.** Each history row (`/api/monitoring/history`, Compare drives) carries an
`air_path` block: zero / low MAF per 1000 running samples, episodes (count, per running hour,
total and longest duration, persistent, repeated clusters), dips, cold / warm and idle /
moving counts, RPM-unstable episodes, EGR excursions per running hour and max |error|,
generic DTCs, MIL, symptom markers near an anomaly, and `vs_cohort` (medians of the *other*
sessions of the same cohort, from 2 sessions on). `air_path_trends` gives per cohort the
session series, medians, medians per drive-type label, pooled cold / warm / idle / moving
rates, episodes per 10 °C coolant band, DTC recurrence and an earlier-vs-later comparison
(from 4 sessions). Cohorts are the same as for the other references: one vehicle profile,
real or synthetic, one confirmed identity; they are never pooled.

The air-path finding (`air_path_egr_maf`, also used by `dde_4862`) now includes the MAF
evidence, the associations (as conditions / contradicting evidence), the generic DTCs with
their uncertainty, and the same car's earlier sessions. Zero MAF while running can make it
`suggestive`; `strong` still requires an SAE air-path-family DTC plus signal evidence.

| | Source |
|---|---|
| ECU measurements | MAF, RPM, speed, load, MAP, coolant, commanded EGR, EGR error values; DTC codes / status / MIL |
| Heuristically inferred | running state, zero / low episodes, dips, excursions, cold / warm, idle / moving, associations, rates, trends |
| Manual | symptom markers |
| Not available without BMW diagnostics | DDE fault memory (4862, 480A), meaning of P115D for this DDE variant, MAF / EGR / boost targets, EGR valve position, air-mass adaptation values |

Reference data: the first real recording (session `20261118-143936-c028`) is not in the
repository. `fixtures/mini-monitoring-air-path.jsonl` (simulator scenario
`mini_air_path_anomalies`) is a synthetic, anonymised stand-in reproducing its observable
pattern: cold start ~7 -> ~73 °C, stored P115D with MIL on, short MAF 0 / < 1 g/s spells
with the engine running, EGR-error swings both ways, an `unusual_engine_noise` marker, and
ECU communication lost at the end without a measured engine stop.

## Dashboard (tab "MINI monitoring")

- **Live**: recording state and elapsed time, link state (ECU answering / not
  answering / adapter unreachable), engine state, effective intervals, RPM, speed,
  coolant, ECU voltage, adapter voltage, DPF availability, event buttons, event log.
- **Session details**: stacked charts on a shared time axis with events (dashed =
  manual, blue = observed) and shaded inferred intervals; event table with origin;
  engine stops and fan durations; voltage per source; DPF observations; cooling;
  sampling quality per signal (planned vs actual Hz, gaps, link events).
- **Session details** also has an **Air path** panel: MAF screening counts, cold / warm and
  idle / moving distribution, episodes with raw answer and context ages, EGR-error
  excursions, supporting / contradicting associations, fault codes with their uncertainty,
  and the comparison with the car's other sessions. MAP and EGR-error charts were added to
  the stack; MAF candidates are shaded.
- **Compare drives**: per session and per drive-type label (city / highway / ...):
  engine stops, fan runs after stop and their mean duration, battery warnings,
  possible and possibly interrupted regenerations, MAF low / zero per 1000 running samples,
  MAF episodes (per hour), EGR excursions per hour, generic DTCs and MIL; below it, the
  air-path trend per data origin with DTC recurrence.

Badges everywhere: *measured*, *observed*, *manual*, *inferred - possible*,
*inferred - candidate* (air-path screening), *raw only*, *not available*.

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

## BMW DDE values in the same recording (DDE-06)

There is no separate DDE logger and no DDE-only recording command. `mini_extended_monitoring`
gets manufacturer items only when the DDE path is executable (DDE-02 catalog entry
verified for the confirmed variant, user opt-in, DDE-01 adapter verdict `candidate`).
**For the real car none is today**, so real recordings record `meta.dde.status =
"not_polled"` with the reason and run generic-only, exactly as before. Everything below
is exercised with the simulator's invented DDE (`mini_dde_drive`, `mini_dde_synthetic`).

- **One scheduler, one lock, one session.** DDE items (`dde:<signal_key>`) sit in the
  same deadline scheduler as the generic PIDs, in the medium / slow tiers only (never fast).
  Each one is one guarded transaction under the transport lock (DDE-03). Samples, exchanges
  and events share one SQLite session and one timeline.
- **Session metadata at start:** `meta.dde` = opt-in, confirmed variant, adapter verdict,
  synthetic, catalog version, the exact signal plan (`metric`, `request_id`, tier, base
  interval, semantics) and per-metric availability. It is persisted before the first sample.
- **Bandwidth (generic first):** a DDE transaction costs about eleven adapter exchanges
  (set-up, request, restore). Every adaptation window the recorder measures generic
  capacity from generic requests only and scales the generic tiers exactly as without DDE,
  so DDE never stretches the generic fast tier. DDE demand is computed from the measured time
  of one transaction and the *effective* DDE intervals. DDE intervals are then stretched
  (`dde_scale`, up to x8) to aim at **35 % of link time, or less if the generic tiers need
  more of the load budget**. 35 % is a target, not a hard limit: when x8 is not enough the
  adjustment records `dde.target_exceeded`. Each adjustment records generic capacity,
  measured / allowed / projected DDE share, transaction time and the effective DDE
  intervals. All four `*_share` values (`target_share`, `allowed_share`, `projected_share`,
  `measured_share`) are shares of **wall-clock** time: DDE link-busy seconds per second of the
  adaptation window (`window_s`). The DDE share of the link's *busy* time only is a different
  quantity and is reported separately as `busy_time_share`; the summary keeps `dde.scheduling` and `dde.final_effective_intervals`. Items
  that are due are served generic first; DDE is deferred. After a reconnect or ECU restore,
  generic items are polled at once and DDE items are spread over their next interval, with
  no catch-up burst. A signal that keeps answering NO DATA / negative is backed off (DDE-05).
  An adapter that refuses the addressing is remembered for the whole adapter connection
  (also across internal TCP resyncs). Only a new `connect` assesses it again.
- **Silence and shutdown:** DDE items are read only in the `running` state. After
  `ecu_communication_lost`, during `adapter_unreachable`, and after an adapter-only
  reconnect, no manufacturer request is sent. A DDE read never re-initialises the adapter
  (`not_attempted` instead); generic polling re-initialises under the existing rules first.
  Fault-memory reads happen only in the pre / post scans (DDE-04), never in the loop.
- **Regeneration:** only a decoded flag of a catalog entry whose semantics are
  `regeneration_active_flag` counts. Freshness uses the **attempted** read cadence (every read
  quality, i.e. the scheduler's effective interval), never the spacing of successful reads.
  Between two decoded readings the state is known only if they are at most 3x that cadence
  apart (minimum 10 s) and no ECU-silence / adapter-loss period lies between them. Otherwise
  the span is an *unknown gap* (reason, failed reads, state before): it is never "inactive"
  and never counted in a confirmed duration. A confirmed episode runs from its first to its
  last active reading within one known run. Start / end are reported as bounds
  (`started_between_rel_s`, `ended_between_rel_s`), with `started` / `ended` saying whether
  a transition was observed or the knowledge ended (gap, ECU silence, adapter loss). A bound
  is `null` when it is not known: if the flag was active before an unknown gap and is active
  again after it, the regeneration may have run straight through, so the resumed span has no
  start lower bound (the reading before the gap is not one); if it was inactive before the
  gap, that reading stays the lower bound. The live
  recorder applies the same rules: `dde_regeneration_started` / `_ended` only between two
  known states, `dde_regeneration_unknown` when an active state stops being fresh (stale, ECU
  silent, adapter lost), `dde_regeneration_confirmed` when the flag is active at the first
  fresh reading (start not observed). The live snapshot carries `fresh_until_wall` deadlines;
  the dashboard turns the state to unknown and values to "not current" at those deadlines on
  its own clock, also when no new event arrives. The generic "possible regeneration"
  heuristic remains a separate, inferred list.
- **Analysis** (`monitoring.json` → `dde`, from `app/diagnostics/dde.py`): per-signal
  provenance (`bmw_dde`), ok / failed counts, min / median / max, decoder versions;
  commanded − actual deltas (EGR, air mass, boost, rail) only for samples read within
  2.5 s of each other, labelled as association in time; limitations. DDE values are never
  pooled with generic PIDs (DDE air mass is not compared as if it were PID 10). History
  rows add `dde_confirmed_regenerations` (None without DDE data) and `dde_soot_last`.
- **Dashboard:** Live shows a "BMW DDE values" panel (value, quality, age / stale, interval,
  suspended, regeneration *confirmed by DDE* / *unknown*), or why nothing is read (opt-in
  off, no verified signal). Session details show confirmed-by-DDE regenerations next to
  the count inferred from generic OBD, plus DDE signal statistics. Charts receive DDE
  signals as their own series (`provenance: bmw_dde`, pid > 0xFF).
- **Export:** `dde_monitoring.json`, a "BMW DDE manufacturer values" report section, and
  provenance columns in `samples.csv` (only when DDE samples exist). Replay restores DDE
  readings as recorded. Old sessions and DBs load, analyse and export exactly as before
  (`dde.status = "not_recorded"`).

## Not available through this tool

For the real car, still: DPF regeneration state and request, soot mass, ash load, distance/time since the last
regeneration, failed-regeneration counters, BMW fault memory (480A, 4862) - none has a
verified DDE request yet (DDE_REQUEST_CATALOG.md) - the meaning
of manufacturer-range generic codes such as P115D for this DDE variant, EGR
target/actual, MAF and boost targets, fan state. Extended standard PIDs (7A/7B/7C DPF, 78/79 EGT, 73 exhaust
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
