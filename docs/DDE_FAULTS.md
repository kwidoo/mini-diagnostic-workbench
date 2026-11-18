# BMW DDE fault memory in scans (DDE-04)

Goal: see whether BMW DDE codes such as `4862` and `480A` are stored now, with their native
status. This adds to the generic P-codes and never replaces them.

## Where it runs

Only at explicitly defined, infrequent moments of the existing MINI workflow, never in
the polling loop:

- `mini-obd scan` / Faults tab → "Scan faults";
- the optional pre- and post-scans of a recording (`record` without `--no-pre-scan` /
  `--no-post-scan`). The post-scan is skipped as before when the ECU stopped answering.

The read goes through the DDE-02 catalog gate and the DDE-03 guarded transport. It never
clears anything (services 14 / 04 cannot be built). Its exchanges are kept in the raw trace
with phase `dde:faults`. Replay keeps them raw: they are not re-decoded.

## What is stored

Each `DtcScan` gets an additive field. Older sessions simply have none:

- `origin: "generic_obd"` on the generic part (`dtcs`, `mode_status`, `freeze_frame`);
- `manufacturer`: a `ManufacturerFaultRead` (`origin: "bmw_dde"`) with `status`, `detail`,
  `synthetic`, `request_id`, `catalog_version`, `decoder_id` / `decoder_version`, the
  answering `ecu`, raw bytes, `nrc` and `faults`. Each fault keeps the code **exactly as
  returned** (hex), its raw bytes, its status byte, and decoded status bits only from a
  verified layout. Manufacturer codes are never put into `dtcs` and never mapped to P-codes.

| `status` | Meaning |
|---|---|
| `unavailable` | no executable fault-memory entry (today for the real car: not verified, not opted in, ...). **Nothing was sent.** |
| `not_attempted` | gate or link refused at send time (e.g. link not initialised); nothing sent |
| `unsupported_adapter` | the adapter refused the documented addressing (`?`); not retried on that connection |
| `read` | complete, successful read with faults |
| `no_faults` | complete, successful read, no fault listed. The **only** way "no faults" is shown |
| `partial` | positive answer that did not decode completely; listed codes kept, others may be missing |
| `negative` | negative response, NRC kept |
| `no_data` / `timeout` / `disconnected` / `error` | as named; no codes |

Shown in: the Faults tab (each scan card has a separate "BMW DDE fault memory (measured)"
block), CLI `scan` output, `report.md`, `dtcs.json`, and `dde_faults.csv` in the ZIP
(the CSV only when a read was actually attempted).
Owner-reported `4862` / `480A` stay in the vehicle record as history. The `dde_4862` and
`dpf_480a` findings use a measured read as supporting evidence when it lists the code, and
as contradicting evidence only when a **complete** read does not. Any other status is
stated as "no statement about the code".

## Status for the real car

No fault-memory request is verified for this DDE (see DDE_REQUEST_CATALOG.md), so every
real MINI scan records `unavailable` with the reason and sends nothing manufacturer-specific.
All behaviour above is tested against the simulator with invented addressing and an invented
layout (`tests/test_dde_faults.py`).

## Manual checklist (for the day a verified entry exists)

Not performed. No on-car validated result exists.

1. DDE-01 capability run shows `manufacturer_transport_candidate`; DDE variant confirmed
   with a BMW tester and entered with `mini-obd dde-settings --variant ...`; opt-in set.
2. Stationary, ignition on, engine off. `mini-obd scan`. Expect exactly the documented
   set-up / request / restore commands in the trace (phase `dde:faults`).
3. Compare the listed codes and status bytes with the reference tester's fault memory
   printout of the same day. Record the result and the date here.
4. Only then mark the catalog entry `vehicle_verified` in a reviewed change.
