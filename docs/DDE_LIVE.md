# BMW DDE live parameters (DDE-05)

Manufacturer live values (DPF regeneration state, soot load, differential pressure, exhaust
temperature, EGR commanded / actual, air mass target / actual, ...) as readings with full
provenance. **For the real car none is available**: the catalog has no verified entry
(DDE_REQUEST_CATALOG.md), so nothing is requested and nothing is shown as a value. All
behaviour is tested against the simulator with invented layouts.

## Reading shape

A manufacturer reading is a normal `Reading` with:

| Field | Value |
|---|---|
| `mode` | `0x100` (`MANUFACTURER_MODE`): outside every OBD service number, so it is never treated as a service 01 PID or as the adapter voltage (mode 0) |
| `pid` | stable id per signal key (0x100 + position in the catalog's metric list) |
| `source` | `bmw_dde` |
| `signal_key` | stable key, e.g. `dpf_soot_mass`. The identity to use in storage, charts and analysis |
| `request_id`, `catalog_version`, `decoder_version` | which entry, catalog and layout produced it |
| `ecu` | response CAN id / extended address that answered (e.g. `7B8/F5` in the simulator) |
| `raw_hex` | the positive payload as received |
| `status` | decoded status semantics, only when the layout defines them (e.g. `{"active": true}`) |
| `quality` / `value` | `ok` with a value from a verified decoder; `raw_only` (bytes, no value); `no_data`, `negative` (NRC in `detail`), `timeout`, `disconnected`, `error` (layout / range / framing, or an unsupported adapter) with **no value** |

Storage: six additive, nullable columns on `samples` (`source`, `signal_key`, `request_id`,
`decoder_version`, `catalog_version`, `status_json`), added by an idempotent migration.
Older rows and generic samples keep exactly their original shape in `samples()`, CSV and
JSON exports. The extra CSV columns appear only in sessions that contain manufacturer
readings. Sampling metrics (`compute_metrics`) and the generic charts stay limited to
service 01 + adapter voltage.

Replay: transcripts carry manufacturer readings as `manufacturer_reading` records, decoded
at recording time with their decoder and catalog versions. Replay restores them. It never
re-decodes manufacturer bytes with a different layout.

## Validation

`RequestCatalog.interpret` checks the positive SID, the echoed identifier, the length
bounds and the documented range before a decoder runs, and decoders reject undefined
status bits. A value inside the range is plausible, not proven. A wrong DDE variant gives
`raw_only`. Nothing is converted with scalings taken from other engines.

## Availability

`DdeLiveReader` reads only executable entries (catalog gate + opt-in + variant + adapter
verdict). A signal that answers NO DATA / negative three times in a row, while the ECU
answers other requests, is suspended with a growing back-off (60 s up to 600 s). An adapter
that cannot provide the addressing stops all manufacturer reads on that connection. Per
metric: `executable`, `blocked` (with reasons), `unavailable` (no entry), `suspended`,
`unsupported_adapter`.

Where it shows today: `mini-obd inventory` / Faults → "Read PID inventory" reads each
executable live signal once (session meta `dde_live`). Continuous recording is DDE-06.
