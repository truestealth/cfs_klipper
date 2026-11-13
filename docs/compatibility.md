# Compatibility

English | [Русский](compatibility.ru.md)

**Status:** development scaffold. No complete replacement cycle has been accepted
on the target printer. This document distinguishes a required interface from
an implemented or hardware-verified one.

## Coverage

| Area | Implementation | Validation |
| --- | --- | --- |
| Proposed Klipper import layout | Implemented | PC regression test; not a printer installation test |
| Refusal to activate the scaffold | Implemented | PC regression test before printer-object lookup |
| Native `serial_485` calls | Pending | Partial static review; target request paths unverified |
| Wire framing | Documentation only | Offline reference checks; no target exchange by this package |
| Filament loading | Pending | Stock loading branches and hardware acceptance pending |
| Unload, change, pause/resume, recovery | Pending | No replacement validation |
| Stock G-code and status interfaces | Pending | Client expectations identified; target compatibility unverified |
| RFID, remaining length, saved materials | Pending | No replacement persistence/refresh validation |
| Reversible installation | Pending | No installer or rollback provided |
| Python 3.8+ | Source target | CI tests 3.8 and 3.11 directly from the checkout; native MIPS runtime unverified |

CI configuration is a proposed check matrix, not evidence that hosted checks
have run. Record the actual Python/platform when reporting local results.

<a id="status-contract"></a>

## Status contract

Preserve the existing `box` object and client-visible meanings. The compatibility
inventory currently includes:

- top-level connection state, last `t_command`, slot mapping and enable/refill
  fields;
- per-box `T1` through `T4`, with four slot values per connected box;
- `vender` (including that historical spelling), `material_type`, `color_value`,
  `remain_len`, version, temperature and humidity;
- distinct empty, present-with-unknown-material and unavailable observations.

This is a preliminary inventory, not a complete validated schema. Recover exact
types, sentinels, units and update/persistence rules from the target original.
Cached material/color and the last selected T-command do not establish the
physical filament location. Preserve user mapping instead of substituting an
identity map.

## Commands and configuration

Inventory the existing `BOX_*` and tool-selection commands, parameters and error
responses before replacement. Include commands for manual material/color edits
and refresh, as well as the operations used by the screen and slicer.

Preserve the working configuration and user wrappers, including
`rename_existing` chains and the established order of macro/command registration.
Configuration names and semantics are part of compatibility. Default values
from another printer are not a substitute for the user's current settings.

## Acceptance and boundaries

Tests use synthetic data and small substitutes for the required Klipper/native
objects. Add cases with each implemented route; no motor, heater or real port is
part of the PC suite. PC checks do not replace physical acceptance.

Before any authorized hardware trial, establish current filament state and the
exact host/MCU/configuration versions. Hardware access, service changes and
installation remain separate from creating this repository structure.

The stock `serial_485.so`, MCU firmware and other printer subsystems remain in
place. See
[architecture.md](architecture.md) and [protocol.md](protocol.md).
