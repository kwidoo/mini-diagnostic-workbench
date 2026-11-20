# DDE capability assessment (DDE-01)

What can be learned about the adapter and the MINI's engine ECU **before** any BMW
manufacturer protocol is attempted. The assessment uses only requests the generic
allowlist already permits; it sends **zero manufacturer-specific requests** and never
claims to have identified the DDE software.

Code: `backend/app/manufacturer/bmw/capability.py`. Tests: `backend/tests/test_dde_capability.py`,
`frontend/src/__tests__/dde-capability.spec.ts`.

## What it reads

| Request | Why | Already allowed before DDE-01 |
|---|---|---|
| `ATZ`, `ATI`, `AT@1`, `ATRV`, `ATDPN`, `ATDP` | adapter identity string, description, supply voltage, protocol read-back (from the normal initialisation) | yes |
| `0100` / `0120` / `0140`, `0900`, `0200`.., `0600`.. | capability discovery: which ECUs answer, which PIDs / InfoTypes they advertise (normal connect) | yes |
| `0904` calibration ID, `0906` CVN, `090A` ECU name | generic identification; their answers are longer than one CAN frame, so they also prove ISO-TP multi-frame reception with adapter flow control | yes |
| `0902` VIN | only with `--read-vin` / the checkbox; the VIN never enters the report and is redacted in exports | yes |

No `ATSH`, `ATCEA`, `ATCRA`, `ATFC*`, no UDS / KWP service, no address scan. The report
counts every request in the session trace that is outside the generic allowlist
(`dde.manufacturer_requests_sent`, must be 0).

## Report

Every field carries `value`, `quality`, `source` and `detail`:

| Quality | Meaning |
|---|---|
| `observed` | read from the car / adapter in this session; the exchange `seq` is kept |
| `claimed` | a string the adapter firmware reports about itself (`ATI`, `AT@1`); clones copy these |
| `derived` | computed from observed data by a stated rule |
| `not_supported` | the ECUs answered discovery and did not advertise it |
| `not_attempted` | not requested (VIN without consent, InfoType never read in that session) |
| `failed` | requested, no usable answer (NO DATA / negative / timeout / malformed, kept in `detail`) |
| `unknown` | nothing readable through allowed requests establishes it |

Verdicts (never a boolean "DDE supported"):

| Verdict | When |
|---|---|
| `generic_obd_verified` | real adapter, at least one ECU answered capability discovery; `generic_obd_simulated` for simulator sessions |
| `manufacturer_transport_blocked` | a stated prerequisite is missing: not CAN, not 11-bit/500 kbit/s, no ELM identity, or a claimed ELM327 version older than 1.4 (no `AT CEA`) |
| `manufacturer_transport_unverified` | no blocker, but the evidence for "candidate" is missing (e.g. no completed ISO-TP multi-frame answer yet). Synthetic sessions never get further than this |
| `manufacturer_transport_candidate` | real CAN 11/500, an ELM327 version claim, and an observed completed ISO-TP multi-frame answer. It means "the next documented step may be reviewed", **not** that BMW access works |

Each verdict lists its blockers and its open (unverified) points. `dde.variant` and
`dde.software_version` stay `unknown`: calibration ID and ECU name are SAE J1979
identifiers, and this tool does not map them to a DDE variant.

## Observed / assumed / unknown for this car (as of 2026-11-18)

From the real recordings in the local database (`20261118-143936-c028`,
`20261118-160246-3cf2`, `20261118-161803-c2a6`), assessed offline and read-only with
`mini-obd dde-capability --session <id>`. The stored sessions were not modified.

| Item | Status | Evidence |
|---|---|---|
| Adapter answers as an ELM327 | observed | `ATZ` / `ATI` -> `ELM327 v2.2` in all three sessions |
| ELM327 version 2.2 | **claimed only** | firmware string; says nothing about the chip. Not one of the versions EdiabasLib recommends for BMW D-CAN (1.4b, 1.5, original 2.1) [1] |
| Adapter description | claimed | `AT@1` -> `OBDII to RS232 Interpreter` (the genuine default text, widely copied) |
| Chip / firmware family | unknown | not readable through allowed commands; not inferred |
| Vehicle protocol | observed | `ATDPN` `A6`, `ATDP` `AUTO, ISO 15765-4 (CAN 11/500)` |
| ECUs answering generic OBD | observed | `7E8` (26 service 01 PIDs, primary, engine ECU candidate) and `7E9` (12 PIDs, role not identified) |
| Service 09 InfoTypes advertised | observed | `7E8`: 02, 04, 06, 0A, 0B; `7E9`: 04, 06, 0A |
| Calibration ID / CVN / ECU name values | **not attempted** | the recordings never requested them; the capability run below does |
| ISO-TP multi-frame reception | **unknown** | every answer in those traces was a single frame (no `1x` first frame) |
| Generic stored DTC | observed | `P115D` stored by `7E8`, MIL on (pre-scans) |
| `AT CEA` / `AT SH` / flow-control setup accepted by this adapter | unknown | manufacturer-addressing commands; not sent in DDE-01 |
| BMW D-CAN through this adapter | unknown | Vgate iCar Pro Wi-Fi is not named in EdiabasLib's adapter list [1][2]; retailer listings of other Vgate units mention BimmerCode, which is not evidence for this unit |
| DDE variant / software level | unknown | needs a BMW identification job |
| Manufacturer-specific requests sent so far | observed: 0 | every request in the traces passes the generic allowlist |

Verdicts for those sessions: `generic_obd_verified`, `manufacturer_transport_unverified`
(no blocker; ISO-TP multi-frame not yet observed; adapter addressing commands untested;
no verified BMW request; DDE variant unknown).

## One safe on-car capability run

Uses the existing connection, nothing else. About 1 minute.

1. Park outdoors or in a ventilated place, handbrake on, **ignition ON, engine OFF**.
2. Adapter in the OBD socket, Mac joined to the adapter Wi-Fi (`V-LINK`).
3. Run one of:
   - CLI: `cd backend && uv run mini-obd dde-capability --label "capability 1"`
     (add `--read-vin` only if you want the VIN stored locally; it is never printed in the report)
   - Dashboard: Connection -> Connect (real), then Vehicle -> **Run capability check**.
4. Expected traffic: the normal initialisation and discovery, then `0904`, `0906`, `090A`
   (and `0902` only with consent). Nothing else. Stop on any warning light change or
   unexpected behaviour and disconnect the adapter.
5. Export for review: `uv run mini-obd export --session latest` (ZIP with
   `dde_capability.json` / `.md`, VIN redacted), or the "Export this assessment" link.

What the run can change: `calibration_id` / `cvn` / `ecu_name` become `observed`, and if
`7E8` answers `0904` with a multi-frame message, `isotp_multiframe` becomes `observed` and
the verdict may become `manufacturer_transport_candidate`. It cannot identify the DDE
variant and it does not test BMW addressing.

## Offline review of earlier sessions

`mini-obd dde-capability --session <id>` (or `GET /api/sessions/<id>/dde-capability`)
recomputes the report from a stored session's raw trace. It writes nothing; `--out
report.md` / `report.json` saves a copy.

## Adapter research notes

- EdiabasLib / Deep OBD list ELM327 adapters as usable for BMW **D-CAN only** (vehicles from
  3/2007), recommending versions 1.4b, 1.5 and original 2.1 on a PIC18F25K80, warning
  that fake 1.5 parts do not work and that ARM-based adapters are "in most cases not 100%
  ELM327 compatible" [1][2]. The Vgate iCar Pro Wi-Fi is not listed.
- CAN extended addressing (`AT CEA hh`) appeared in ELM327 v1.4; `AT CRA` reset in v1.4b;
  flow-control setup (`AT FC SH/SD/SM`) in v1.1 [3]. A version string is a claim, so this
  is checked as a prerequisite, not as proof.
- Flashing replacement firmware (EdiabasLib documents one) is **not** suggested: it is
  irreversible for a consumer adapter and outside this read-only project.

## Sources

1. EdiabasLib, Adapter types, https://github.com/uholeschak/ediabaslib/blob/master/docs/AdapterTypes.md (retrieved 2026-11-18)
2. Deep OBD for BMW and VAG, https://uholeschak.github.io/ediabaslib/docs/Deep_OBD_for_BMW_and_VAG.html (retrieved 2026-11-18)
3. Elm Electronics, ELM327 AT Commands (Oct 2010), https://cdn.sparkfun.com/assets/4/e/5/0/2/ELM327_AT_Commands.pdf
