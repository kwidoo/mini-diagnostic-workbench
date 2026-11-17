# Support matrix

Status words: **implemented** (code + tests against the simulator), **raw only**
(bytes stored, no physical value shown), **unavailable** (no request is sent).
Nothing in this table has been validated on a real car yet.

Three things are reported separately and never merged into one "complete" flag:
generic OBD support (this table), manufacturer DDE/DME diagnostics (not implemented:
see "BMW DDE: what would be needed"), and hardware validation (none yet). The profile
status `generic_obd_ready` refers to the first one only.

## Adapter and protocols

| Item | Status | Notes |
|---|---|---|
| Vgate iCar Pro Wi-Fi over TCP | implemented | Configurable host/port/timeouts, reconnect with exponential backoff |
| ISO 15765-4 CAN, 11-bit and 29-bit (ELM protocols 6-9) | implemented | Expected for the 2016 MINI. ISO-TP reassembly done by this tool |
| ISO 9141-2, ISO 14230-4 KWP (ELM 3-5) | implemented, simulator-tested only | Expected for the Porsche 986. Checksums verified |
| SAE J1850 PWM/VPW (ELM 1-2) | not supported | Reported as an explicit error |
| python-OBD | not used | Its documentation describes serial/USB/RF ports and Bluetooth setup, and does not mention Wi-Fi/TCP adapters [3]. The ELM text protocol over a socket is small enough to implement and test directly |

## Generic OBD services

| Service | Status | Notes |
|---|---|---|
| 01 live data | implemented | Capability bitmaps read first; unsupported PIDs are never polled |
| 02 freeze frame | implemented | Frame 00; the DTC that set it is shown |
| 03 stored DTCs | implemented | Raw bytes and source ECU kept |
| 07 pending DTCs | implemented | |
| 0A permanent DTCs | implemented | "not supported" when the ECU does not answer |
| 09 vehicle info | implemented | Calibration ID always; VIN only on request |
| 06 monitor results | raw only (CAN) | Unit-and-scaling IDs not decoded. Not decoded on K-line |
| 04 clear DTCs, 08 control | **blocked** | Rejected at the allowlist |

## Service 01 PIDs with decoders (SAE J1979 / ISO 15031-5)

04 load, 05 coolant, 06-09 fuel trims, 0B MAP (absolute), 0C RPM, 0D speed, 0E timing
advance, 0F intake air temperature, 10 MAF, 11 throttle, 14/15/18/19 O2 voltage,
1F run time, 21 distance with MIL, 22 rail pressure (relative), 23 rail gauge pressure,
2C commanded EGR, 2D EGR error, 2F fuel level, 30/31 warm-ups and distance since clear,
33 barometric pressure, 42 module voltage, 43 absolute load, 45 relative throttle,
46 ambient temperature, 49 pedal position, 4D/4E time with MIL / since clear,
59 rail absolute pressure, 5C oil temperature, 5E fuel rate. PID 01 gives MIL state and
DTC count.

MAP (0B) is absolute pressure. Boost gauge pressure is shown only as an explicitly
labelled derived value (MAP minus barometric pressure) and only when both were measured.

## Standard PIDs recognised but raw only

03, 13, 1C, 41, 51, 5D, 61-6F, 70-7C, 83-87, 8B. These include the extended diesel
PIDs: 69 commanded EGR / EGR error, 6B EGR temperature, 70 boost pressure control,
73 exhaust pressure, 7A/7B DPF, 7C DPF temperature, 87 intake manifold pressure.
If the MINI's ECU advertises any of them, the inventory lists them and stores the raw
bytes. Their multi-byte layouts were not verified for this project, so no value is
shown. Verifying one against the standard and adding a decoder is the cheapest route
to DPF or EGR data and needs no manufacturer protocol.

## What was asked for, and where it stands

| Wanted measurement | Through this adapter and tool |
|---|---|
| RPM, speed, coolant, intake air temp, barometric pressure, load, module voltage, run time | Generic PIDs, if the ECU supports them |
| MAF | Generic PID 10, if supported |
| MAP / boost | Generic PID 0B (absolute); boost derived only with PID 33 |
| Fuel rail pressure (actual) | Generic PID 23, 59 or 22, if supported |
| Commanded EGR, EGR error | Generic PIDs 2C / 2D if supported; loosely defined, treat with care |
| EGR target vs actual (BMW values) | **unavailable** |
| DPF differential pressure | **unavailable** (raw only if the ECU offers PID 7A) |
| DPF soot load, regeneration status and history | **unavailable** |
| Exhaust back-pressure | **unavailable** (raw only if the ECU offers PID 73) |
| Injector balance / quantity corrections | **unavailable** |
| Commanded rail pressure | **unavailable** |
| BMW fault memory (4862, 480A, ...) | **unavailable**; only generic P-codes are read |

## BMW DDE: what would be needed

- BMW hex fault codes and the values above are served by BMW's own diagnostic jobs.
  Reading them takes a BMW-capable tester: ISTA, or INPA/EDIABAS with a K+DCAN
  interface, or a commercial scan tool with BMW coverage. Which interface this exact
  car needs should be confirmed with whoever supplies the tester.
- An ELM327 sits on the same connector, but this project has no authoritative source
  for the DDE's addressing, request identifiers or scaling on this car, and the ECU
  variant is unconfirmed. Guessing them is ruled out, so nothing is sent.
- The extension interface (`backend/app/manufacturer/extension.py`) is in place and
  tested. Turning a parameter on requires: ECU variant identified from the car, a
  documented source for the request and scaling, recorded fixtures, and a reviewed
  change to the allowlist.
- Practical suggestion: have a workshop or a BMW-capable tool read the DDE fault memory
  and the DPF / EGR live values once, and keep that printout next to these sessions.

## The two BMW fault codes

- **4862**: a BMW bulletin for a different vehicle (E70 X5 xDrive35d) describes it as
  "Air system, air to EGR mass flow, plausibility", cause "Air mass monitoring of the EGR
  valve is outside allowable limits", and lists a charge-air temperature sensor test, a
  smoke test of the charge-air circuit, and the air-mass meter [4]. That is context for
  a different engine. For this MINI it is used only as "air-mass / EGR plausibility,
  subject to confirming the DDE variant".
- **480A**: reported by the owner in a DPF context. The reference page named for this
  project [5] could not be retrieved (robots exclusion), and BMW code texts differ by
  DDE variant, so the tool does not attach a description to it.

## Sources

1. Vgate iCar Pro Wi-Fi quick start guide, https://manualmachine.com/vgate/icarprowifi/17897015-quick-start-guide/
2. Car Scanner Wi-Fi adapter setup, https://www.carscanner.info/wifi/
3. python-OBD documentation, https://python-obd.readthedocs.io/en/latest/
4. BMW SIB via NHTSA, MC-10147607-9999, https://static.nhtsa.gov/odi/tsbs/2015/MC-10147607-9999.pdf
5. https://bmwteka.com/en/fault-codes/d73n57d0-480a (not retrievable; not relied on)
6. SAE J1979 / ISO 15031-5 (PID formulas), SAE J2012 (DTC format), ISO 15765-2/-4, ISO 9141-2, ISO 14230-4, and the ELM327 data sheet (adapter text protocol). These were applied from working knowledge, not re-read for this project; the first session on the car is the check.
