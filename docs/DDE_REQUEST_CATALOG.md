# BMW DDE request catalog (DDE-02)

`backend/app/manufacturer/bmw/catalog.py` is a typed **manifest** of the BMW DDE requests
this project would like to use. It holds no guessed CAN ids, addresses, request bytes or
scalings. Today **no entry is executable**. Every wanted metric is listed as `blocked`
with what would establish it.

`mini-obd dde-catalog` (or `GET /api/dde/catalog`) prints the current state. Tests:
`backend/tests/test_dde_catalog.py`.

## Entry record

| Field | Meaning |
|---|---|
| `request_id`, `metric`, `title`, `priority`, `kind` | identity; `metric` is the stable signal key used in storage (DDE-05/06) |
| `compatible_variants` | exact DDE variant identifiers the entry applies to. No wildcard, no "N47 family" |
| `service`, `payload_hex` | the exact request (canonical hex). The service must be read-only: 17, 18, 19 (sub-functions 01/02/04/06/0A only), 1A, 21, 22 |
| `addressing` | bus (`can_11bit_500k`), framing (`iso15765_normal` / `iso15765_extended`), request / response CAN ids, extended address bytes, flow-control setup |
| `response` | positive SID, echoed identifier, length bounds, `decoder_id` + `decoder_version`, unit, documented valid range |
| `source_ids` | citations (`Source`: kind, reference, version / retrieval date, licence) |
| `verification_state` | `literature_only` / `fixture_verified` / `vehicle_verified` |
| `risk_class` | `pure_read` / `temporary_session_state` / `active_or_write`; only `pure_read` can execute |
| `evidence` | statements prefixed `vehicle:` (this car) or `literature:` / `synthetic:` |
| `blocked_reason`, `establish_by` | why it cannot run and the concrete steps that would change that |
| `enabled` | `false` by default; only a reviewed change sets it |

## Gate

`RequestCatalog.decide(entry, context)` returns `executable` only when **all** hold:

1. `enabled: true`, and `validate_entry` finds no structural problem (hex, service/payload
   consistency, read-only service / sub-function, 11-bit ids, extended-address bytes, SID
   and echo consistency, known sources, synthetic sources only on synthetic entries);
2. `risk_class == pure_read`;
3. `verification_state == vehicle_verified` (synthetic entries: `fixture_verified` and a
   simulator session only);
4. the user opted in for manufacturer reads on this vehicle;
5. the confirmed DDE variant is in `compatible_variants`, and no other enabled entry
   claims the same metric for that variant (ambiguity disables both);
6. the decoder is registered with the same id and version, and a real entry never uses a
   synthetic decoder;
7. on a real adapter, the DDE-01 verdict is `manufacturer_transport_candidate`.

`RequestCatalog.interpret` returns a value only for an executable entry, and only after
checking the positive SID, the echoed identifier, the length bounds and the documented
range. Everything else is `raw_only` (bytes kept, no value) or `error`. A range check
filters out impossible values. It does not prove a value is correct.

Forbidden in any entry: 04, 08, 10 (session change), 11 (reset), 14 (clear), 27
(security access), 28, 2C, 2E, 2F (actuation), 30, 31 (routines), 34-38, 3B, 3D, 3E,
83-87.

## Evidence matrix (catalog `dde-catalog/2026-11-18.1`)

Columns: **V** DDE variant identified · **A** addressing / framing documented · **R**
request bytes documented · **L** response layout and scaling documented · **S** citable
source · **F** fixture · **T** trace from this car · **Risk**. `-` = missing. `lit` =
literature for other vehicles / adapters only.

| Pri | Metric | V | A | R | L | S | F | T | Risk | State |
|---|---|---|---|---|---|---|---|---|---|---|
| P0 | `dde_identification` | - | lit (EdiabasLib, not reviewed) | - | - | partial | - | generic OBD only | pure read | blocked |
| P0 | `dde_fault_memory` (4862, 480A) | - | - | - | - | SIB for E70 (context) | - | generic P115D only | pure read | blocked |
| P0 | `dpf_regeneration_active` | - | - | - | - | - | - | - | pure read | blocked |
| P0 | `dpf_regeneration_request` | - | - | - | - | - | - | - | pure read | blocked |
| P0 | `dpf_soot_mass` | - | - | - | - | - | - | - | pure read | blocked |
| P0 | `dpf_diff_pressure` | - | - | - | - | - | - | - | pure read | blocked |
| P0 | `exhaust_temperature_pre_dpf` | - | - | - | - | - | - | - | pure read | blocked |
| P0 | `egr_commanded`, `egr_actual` | - | - | - | - | - | - | - | pure read | blocked |
| P0 | `air_mass_target`, `air_mass_actual` | - | - | - | - | - | - | - | pure read | blocked |
| P1 | `dpf_ash_mass`, `dpf_last_regeneration` | - | - | - | - | - | - | - | pure read | blocked |
| P1 | `exhaust_backpressure` | - | - | - | - | - | - | - | pure read | blocked |
| P1 | `intake_pressure_mfr`, `boost_commanded`, `boost_actual` | - | - | - | - | - | - | - | pure read | blocked |
| P1 | `rail_pressure_commanded`, `rail_pressure_actual_mfr` | - | - | - | - | - | - | - | pure read | blocked |
| P2 | `injector_corrections` | - | - | - | - | - | - | - | pure read | blocked (only if clearly documented) |

Generic OBD equivalents (PID 10 MAF, 2C/2D EGR, 0B MAP, 23/59 rail, raw-only 7A/7C/73)
stay separate signals and are never merged with DDE values.

Hardware support is **conditional**: EdiabasLib documents ELM327 adapters for BMW-FAST over
D-CAN only, recommends specific chip versions, and does not list the Vgate iCar Pro
Wi-Fi. This adapter reports `ELM327 v2.2` (a claim). See DDE_CAPABILITY.md.

## Prioritised blockers

1. **Adapter transport evidence.** Run the DDE-01 on-car capability check (service 09
   multi-frame). Without `manufacturer_transport_candidate` nothing can run on the car.
2. **DDE variant.** Have the DDE identified (variant, software level) with a BMW-capable
   tester (ISTA / INPA / a vetted tool), and record it in the vehicle record.
3. **Framing and addressing reference.** Review a vetted implementation (EdiabasLib, cited
   by commit) or factory documentation for D-CAN addressing and flow control. Record the
   result as a `Source` and an `Addressing` value, with no code copied.
4. **Identification request.** A documented identification request plus a trace from this
   car next to a reference tool, giving the first `vehicle_verified` entry.
5. **Fault memory (4862 / 480A).** The variant's documented fault-memory job (status byte
   meanings, counters, environment data only if the layout is documented), plus a trace
   compared with the reference tool's printout.
6. **P0 live values.** One documented identifier per metric, with layout / scaling / range,
   a trace, and golden decoder tests (DDE-05).
7. **P1 / P2** only after the P0 values are verified.

Restricted definition files (for example factory job descriptions) must not be copied into
this repository. Cite them by name and version and record only the verified facts needed
for one entry.

## Synthetic catalog

`backend/app/manufacturer/bmw/synthetic.py` has invented entries (invented CAN ids
`7B0`/`7B8`, extended address bytes `5A`/`F5`, identifiers `F1xx`, variant
`SYNTHETIC-DDE-0`) with synthetic decoders. They exercise the gate, the guarded transport
and storage against the simulator only. `default_catalog()` refuses synthetic entries,
and the gate refuses them outside a simulator session.
