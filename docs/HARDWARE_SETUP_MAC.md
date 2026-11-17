# Hardware setup on macOS

## Adapter

Vgate iCar Pro V2.3 Wi-Fi, ELM327-compatible. Documented defaults:

| Setting | Default | Source |
|---|---|---|
| Wi-Fi SSID | `V-LINK` | Vgate quick-start guide [1] |
| IP address | `192.168.0.10` | [1], [2] |
| TCP port | `35000` | [1], [2] |
| Wi-Fi password | none stated in [1] | check the label / leaflet of your unit |
| OBD protocols | J1850 PWM/VPW, ISO 9141-2, ISO 14230-4, ISO 15765-4 CAN | [1] |

All of these are configurable (`--host`, `--port`, or the Connection screen). The
adapter sleeps when the car is off and wakes at key-on [1].

## Steps

1. Plug the adapter into the OBD socket. Switch the ignition on.
2. macOS Wi-Fi menu: join `V-LINK`. macOS may warn that the network has no Internet.
   Keep it joined.
3. If the Mac is also on Ethernet or another network using `192.168.0.x`, disconnect it
   so the route to `192.168.0.10` goes over Wi-Fi.
4. `cd backend && uv run mini-obd probe`

## First hardware validation (status: NOT hardware-validated)

Nothing below has been run on a real vehicle yet. Until a real protocol log has been
recorded and checked against these expectations, every profile shows
"NOT hardware-validated" and results must be treated as unverified.

Mac + Vgate iCar Pro Wi-Fi. **Ignition on, engine off** for steps 1-4. Every step
uses existing read-only commands; nothing is cleared, actuated, coded, adapted or
regenerated, and no security access is requested (all blocked at the allowlist).

1. `uv run mini-obd --json probe > probe.json`
   - Adapter identity (`ATI`) starts with `ELM327`; note the exact version string.
   - Protocol read back (`ATDPN`): expected `A6` (CAN 11-bit/500k) on the MINI; record
     whatever is reported. Porsche 986: K-line (`A3`/`A4`/`A5`) is expected.
   - The init log shows each command and raw answer; check `0100` lines carry an ECU
     header (`7E8 ...` on CAN) and that no line is out of step with its command.
2. `uv run mini-obd scan --label "KOEO first scan"` - reads `0101`, `03`, `07`, `0A`,
   `020200` and the freeze frame PIDs that the ECU advertises. Write down every DTC.
3. `uv run mini-obd inventory --label "KOEO inventory"` - capability walk plus one read
   of every advertised PID, including `010C`, `0105`, `0110`, `010B` where supported.
   Expected KOEO: `010C` = 0 rpm, `0110` near 0 g/s, `010B` near barometric pressure.
4. `uv run mini-obd export --session <id>` for both sessions and keep the ZIPs: they
   contain the raw adapter transcript. Check in the transcript that
   - each response's service/PID bytes match the request (`41 0C` for `010C` ...),
   - no exchange has status `desync` and `resyncs` is 0 in the session's transport info,
   - unsupported PIDs are absent or `no_data`, never a value.
5. Only if mechanically safe (no significant shaking, knocking, overheating, smells or
   warning lamps): start the engine and record a short stationary idle
   (`uv run mini-obd record --duration 120 --profile idle`). Then repeat step 3.
6. Do **not** drive the vehicle for testing while any of those conditions are present.

Requests this checklist expects to see in the raw transcript (verified against the
allowlist by `backend/tests/test_hardware_checklist.py`):

<!-- checklist-requests -->
```
ATZ ATE0 ATL0 ATS1 ATH1 ATAT1 ATSP0 ATI AT@1 ATRV ATDPN ATDP
0100 0101 010C 0105 0110 010B 03 07 0A 020200
```
<!-- /checklist-requests -->

Recording the result: when a real log has been checked, note the adapter version,
protocol, ECU headers and date in docs/VEHICLE_PROFILES.md. Only then may a profile's
`hardware_validated` flag be set, and only for the protocol that was actually seen.
For the Porsche K-line path this requires a real 986 response; until then it stays
hardware-unvalidated even if the MINI has been validated.

## First extended (continuous) recording

After steps 1-4 above have been checked, follow "First extended recording on the car"
in MONITORING.md (`mini-obd record --profile mini_extended_monitoring --until-stop`).
It uses no request beyond the list above plus the PIDs the ECU advertises.

## Troubleshooting

| Message | Meaning | What to do |
|---|---|---|
| `cannot reach adapter at 192.168.0.10:35000` | TCP connect failed | Wrong Wi-Fi network, adapter asleep or unplugged, or a different IP/port. Cycle the ignition, rejoin `V-LINK`. `nc -vz 192.168.0.10 35000` should succeed. |
| `adapter did not answer ATZ` | Something accepted the connection but is not an ELM327 | Check host and port. |
| `no ECU answered the generic OBD probe (unable_to_connect)` | Adapter fine, car silent | Ignition on? Adapter fully seated? |
| `vehicle protocol ... is not decoded by this tool` | Adapter chose SAE J1850 | Not supported here. |
| Readings marked `timeout` or `disconnected` | Slow answers or Wi-Fi drops | The recording carries on and reconnects with backoff. Raise `--timeout`, move the Mac closer. |
| Readings marked `timeout` / `error` with "out-of-sequence reply", `resyncs` > 0 | A reply arrived late or with stray bytes | By design the connection is dropped, reopened and the adapter re-initialised (ATZ ...) before the next request; the late reply is kept raw and never used as a value. Each resync costs about a second. Frequent resyncs mean the timeout is too short for this adapter/ECU: raise `--timeout`. |

Only one client can use the adapter at a time. Close phone apps that talk to it.

## Docker

The container can run the tests, the simulator and replay. Reaching the real adapter
from Docker Desktop on macOS depends on its NAT reaching `192.168.0.10`; this has not
been tested, so use the local install for the car.

## Sources

1. Vgate iCar Pro Wi-Fi quick start guide, https://manualmachine.com/vgate/icarprowifi/17897015-quick-start-guide/
2. Car Scanner, "Configuring Wi-Fi connection to ELM327 adapter", https://www.carscanner.info/wifi/
3. Vgate forum thread https://forum.vgatemall.com/showthread.php?tid=8471 was named as a
   source for this project but could not be retrieved (bot check), so nothing here relies on it.
