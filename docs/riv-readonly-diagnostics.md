# RIV4835CSH1S read-only diagnostics draft

The LCD manual lists 34 settings (01-29 and 35-39); 00 is an exit command.
This draft adds protocol-based readback for 24 of those settings. Program 28
already has hardware-validated readback and a writable number in upstream.
Nine settings still lack a documented address in the source below. Do not
claim full LCD coverage or deploy this draft over an existing patched install.

## Sources and current integration audit

- Supplied RIV4835CSH1S Manual VB3, printed pages 23-28 and 30.
- [Renogy developer documentation](https://platform.renogy.com/docs/),
  [Inverter Modbus Protocol V1.0, 2025-06-04](https://renogy-website.oss-us-east-1.aliyuncs.com/DeveloperPlatform/Inverter_Modbus_Protocol_V1.0_EN.pdf).
- [Program 28 HA PR #223](https://github.com/IAmTheMitchell/renogy-ha/pull/223)
  is merged; main pins renogy-ble 2.8.0 and contains the number entity.
- [Program 01 HA PR #236](https://github.com/IAmTheMitchell/renogy-ha/pull/236)
  remains open. It uses independently validated 4441 / 0x1159. This draft adds
  a separate read-only sensor without replacing that select.
- Upstream RIV polling reads measurement blocks, device ID, and Program 28.
  It does not read the remaining LCD settings, operating state, fault slots, or
  warning register. The existing Fault Code High/Low sensors are DCC fields,
  not RIV LCD fault-code entities.

## LCD coverage

| LCD | Setting | Register (decimal) | Status |
|---|---|---:|---|
| 01 | Output priority | 4441 | Draft, protocol based; LCD comparison required |
| 02 | Output frequency | 4442 | Draft, protocol based; LCD comparison required |
| 03 | AC input range | 4443 | Draft, protocol based; LCD comparison required |
| 04 | Battery to utility setpoint | 4437 | Draft, protocol based; LCD comparison required |
| 05 | Utility to battery setpoint | 4439 | Draft, protocol based; LCD comparison required |
| 06 | Charging source priority | 4447 | Draft, protocol based; LCD comparison required |
| 07 | Total charge current | 4422 | Draft, protocol based; LCD comparison required |
| 08 | Battery type | 4424 | Draft, protocol based; LCD comparison required |
| 09 | Boost voltage | 4426 | Draft, protocol based; LCD comparison required |
| 10 | Boost duration | 4435 | Draft, protocol based; LCD comparison required |
| 11 | Float voltage | 4427 | Draft, protocol based; LCD comparison required |
| 12 | Load disconnect voltage | 4431 | Draft, protocol based; LCD comparison required |
| 13 | Overdischarge delay | 4433 | Draft, protocol based; LCD comparison required |
| 14 | Low voltage warning | 4430 | Draft, protocol based; LCD comparison required |
| 15 | Discharge limit voltage | 4432 | Draft, protocol based; LCD comparison required |
| 16 | Equalization enable | — | Unmapped; unavailable until verified |
| 17 | Equalization voltage | 4425 | Draft, protocol based; LCD comparison required |
| 18 | Equalization duration | 4434 | Draft, protocol based; LCD comparison required |
| 19 | Equalization timeout | 4440 | Draft, protocol based; LCD comparison required |
| 20 | Equalization interval | 4436 | Draft, protocol based; LCD comparison required |
| 21 | Immediate equalization | — | Unmapped; unavailable until verified |
| 22 | ECO mode | 4444 | Draft, protocol based; LCD comparison required |
| 23 | Overload restart | — | Unmapped; unavailable until verified |
| 24 | Overtemperature restart | — | Unmapped; unavailable until verified |
| 25 | Buzzer | 4101 | Draft, protocol based; LCD comparison required |
| 26 | Mode transition alarm | — | Unmapped; unavailable until verified |
| 27 | Overload bypass | — | Unmapped; unavailable until verified |
| 28 | AC charge current | 57861 / 0xE205 | Existing, hardware validated |
| 29 | Transformer supply | — | Unmapped; unavailable until verified |
| 35 | Disconnect recovery | 4429 | Draft, protocol based; LCD comparison required |
| 36 | PV charge current | — | Unmapped; unavailable until verified |
| 37 | Boost return voltage | 4428 | Draft, protocol based; LCD comparison required |
| 38 | AC output voltage | — | Unmapped; unavailable until verified |
| 39 | AC input current limit | 4456 | Draft, protocol based; LCD comparison required |

## Faults and warnings

Current faults occupy four independent registers 4398-4401. Each holds a
code, not a bitmask. All four slots must be fresh and supported before the
aggregate says no faults. Unknown codes retain their numbers. 0xFFFF means
unsupported; missing or unsupported slots produce unavailable fault entities.
Warning register 4393 is a bitmask; known and unknown bits are retained.
Operating state is register 4405. The full modern protocol fault dictionary
is included. This does not add a persistent hardware fault-history reader.

The supplied LCD manual leaves 16, 18, 24, 25, 27, and 28 unnamed. The newer
generic protocol defines them, including 18 as AC charging hardware
overcurrent. That interpretation needs confirmation for this inverter's
firmware and must not retroactively be treated as a proven diagnosis.

## Activation and validation

The companion HA branch creates read-only diagnostic sensors only for the
RIV4835CSH1S profile. Extra traffic defaults off. Its options form adds
"Read LCD settings and faults (experimental)". The companion library's
`read_inverter_diagnostics()` uses only FC03 and cannot write settings.
Older libraries continue normal polling with diagnostic entities unavailable.

Both changes are required. Before installing, identify the currently installed
HA and library revisions and preserve the existing cell and output-priority
patches. The dependency pins remain unchanged until a supporting release is
available; a standard 2.8.0 install does not contain this new reader.

Validate every displayed value against the physical LCD, especially Programs
04, 05, 06 and 39. The protocol's voltage examples use a 12 V battery system;
the draft divides raw registers by 10 as documented, without guessing a
48 V multiplier. Raw registers, UTC snapshot completion time, read errors,
and `hardware_validated: false` are included in diagnostic attributes.

The snapshot spans sequential reads. It is not simultaneous. A failed block
is omitted; its BLE session is discarded before any following command.
The normal poll result, battery telemetry, and availability are preserved.
Nine blocks add at least about 12 seconds of polling overhead; unsupported
blocks can add further timeouts and reconnects. Start with a 60-second
polling interval for hardware validation. Faults shorter than the polling
interval may be missed; Home Assistant Recorder records observed states.

To finish all LCD features, obtain the missing model-specific register map or
capture read-only Renogy app/LCD correlations. Do not infer addresses merely
from nearby registers, and do not provoke electrical faults to test codes.
