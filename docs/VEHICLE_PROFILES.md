# Vehicle profiles

| Profile id | Generic OBD status | ECU definition | Expected protocol | Rules |
|---|---|---|---|---|
| `mini-countryman-r60-diesel` | `generic_obd_ready` | BMW DDE, variant unconfirmed | CAN | idle stability, air path EGR/MAF, DPF / 480A, DDE 4862, progression, repair history, manufacturer-data availability |
| `porsche-boxster-986-mk2` | `generic_obd_incremental` | Bosch ME7.8, variant unconfirmed | K-line | idle stability, fuel-trim bank balance, progression, manufacturer-data availability |

The status covers **generic OBD only**. Each profile also carries two separate
indicators, shown side by side in the Vehicle tab and returned by `/api/profiles`:
manufacturer diagnostics (BMW DDE / Bosch DME fault memory and parameters: **not read**
by this tool for either car) and hardware validation (**false** for both: simulator
only, see docs/HARDWARE_SETUP_MAC.md). Owner-reported BMW codes 4862 / 480A are stored
as history and labelled "not read from the car"; the tool never claims to have read them.

Hardware validation log: none yet.

Profile switching is refused during any scan, inventory or recording, and while
connected: disconnect first, so the other vehicle is probed on a fresh connection and
no capabilities of one ECU are reused under another profile.

Select one in the dashboard header or Vehicle tab, or with `mini-obd profiles --use <id>`
(or `--vehicle <id>` for one command). The choice is stored; every session is tagged
with it; sessions, comparison and progression analysis are per profile.

A profile states expectations. It never overrides what the car reports: the protocol is
read back from the adapter, supported PIDs come from discovery, and if the two disagree
with the profile the tool says so and carries on with what the car reported. No profile
grants manufacturer-specific access.

## Porsche 986: what is done and what is left

Done: profile, ECU definition with unavailable Porsche parameters, K-line decoding
(simulator-tested), petrol recording profiles (trims, O2 voltages, timing advance), a
bank-balance rule, two synthetic scenarios and fixtures, the same UI/CLI flow.

Left for later increments: confirm the protocol and PID set on the car; decide which
petrol-specific rules are worth adding once real data exists (for example trims against
MAF, O2 sensor switching); owner-reported history for this car; road profile.

## Adding a vehicle

1. `app/ecus/definitions.py`: add an `EcuDefinition`. List manufacturer parameters; they
   are reported as unavailable.
2. `app/vehicles/profiles.py`: add a `VehicleProfile` (expected protocol families,
   priority PIDs, recording profiles, rule ids, event labels, checklist) and put it in
   `PROFILES`.
3. Rules: reuse existing ids, or add a function in `app/diagnostics/rules/` and register
   it in `RULES`. A rule returns `Finding` objects and must state caveats and next steps.
4. Optional: a simulator scenario with `vehicle=<profile id>` and a fixture entry in
   `app/fixtures_gen.py`.
5. `make test`. `tests/test_profiles_extension.py` and `tests/test_analysis.py` check that
   every profile's rules exist.

A new standard PID decoder goes in `app/parameters/standard_pids.py` with its formula
and a test in `tests/test_pids_dtc.py`, after checking the layout against the standard.
