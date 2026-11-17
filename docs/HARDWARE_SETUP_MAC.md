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

## Troubleshooting

| Message | Meaning | What to do |
|---|---|---|
| `cannot reach adapter at 192.168.0.10:35000` | TCP connect failed | Wrong Wi-Fi network, adapter asleep or unplugged, or a different IP/port. Cycle the ignition, rejoin `V-LINK`. `nc -vz 192.168.0.10 35000` should succeed. |
| `adapter did not answer ATZ` | Something accepted the connection but is not an ELM327 | Check host and port. |
| `no ECU answered the generic OBD probe (unable_to_connect)` | Adapter fine, car silent | Ignition on? Adapter fully seated? |
| `vehicle protocol ... is not decoded by this tool` | Adapter chose SAE J1850 | Not supported here. |
| Readings marked `timeout` or `disconnected` | Slow answers or Wi-Fi drops | The recording carries on and reconnects with backoff. Raise `--timeout`, move the Mac closer. |

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
