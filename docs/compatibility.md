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

<a id="firmware-versions"></a>

## CFS firmware versions

**Baseline: CFS MCU 1.1.3 on the reference K1C with K1 Series CFS Upgrade.**
The reference printer uses the stock host `serial_485.so`. This baseline defines
the starting contract for the open `box` replacement; the replacement itself
has not passed hardware acceptance.

| CFS MCU firmware | Project target | Replacement validation |
| --- | --- | --- |
| 1.1.3 | Baseline; current reference CFS firmware | Static command/response/operation review; no replacement hardware acceptance |
| 1.2.2, 1.4.2, 1.5.0 | Intermediate reference versions | Static comparison of the collected cfs0/G32 images; no replacement hardware acceptance |
| 1.5.2 | Additional compatibility target | Static comparison completed for documented paths; target hardware validation pending |
| 1.5.3 | Additional compatibility target | Same ordinary command paths as the examined 1.5.2 image; target hardware validation pending |

Compatibility is recorded per CFS hardware variant, MCU firmware and retained
host-library version. A successful check on one MCU version does not validate
the others. Version selection is not implemented in the scaffold.
The future implementation must identify each connected box's running firmware
and select only verified command, response and operation behavior for it.
An unknown version must not silently inherit the 1.1.3 mechanical behavior.

Use shared implementation code with explicit, verified per-device differences.
The comparison concerns collected cfs0/G32 applications; it does not establish
compatibility with another CFS generation or hardware ID.

MCU firmware is separate from the printer firmware and `[box] version` setting.
An MCU update does not by itself add new host commands or validate the existing
operation sequence. Keep the screen/status interface and user settings stable.
See the [protocol version requirements](protocol.md#firmware-versions).

<a id="firmware-history"></a>

### Firmware change history

These compatibility notes record observations from static comparison of the
examined firmware applications. Each entry describes changes from the preceding
examined version; the changes accumulate relative to the 1.1.3 baseline.
Coverage is limited to the documented interfaces and operation paths.

#### 1.1.3 — baseline contract

- Ordinary and current-mode error replies to `0x0A` contain four data bytes:
  temperature, humidity, pending flags and mode. Slot-event replies with status
  `0x30` also contain four data bytes, with a different meaning.
- Loading uses staged command `0x10`; unloading uses `0x11`. Preload permission
  and preload actions are separate operations under `0x0D`.
- Busy and pretension guards are already present. An initial loading ACK does
  not establish physical completion.
- The later commands `0x12` and `0x5B`–`0x5D` are absent.

#### 1.2.2 — examined baseline interfaces retained

No changes were found in the examined application command set, existing request
lengths or four-byte ordinary `0x0A` layout relative to 1.1.3. This finding does
not establish equivalence of the complete firmware.

#### 1.4.2 — extended state and RFID lifecycle

- Ordinary and current-mode error `0x0A` data expands from four to six bytes.
  The original four fields retain their positions. The added fields are a
  connection-sensor mask and per-channel tag flags; the full meaning of the tag
  flags remains unresolved. The sixth byte must not be treated as constant zero.
- Slot-event replies with status `0x30` retain four data bytes. Decode the status
  before selecting the payload layout.
- Entering/leaving print mode and starting material retraction now participate
  in RFID usage tracking. The MCU updates consumed length and writes tag data;
  the host must account for that lifecycle without duplicating consumption.
- Commands `0x5B`–`0x5D` add UID-derived identification, secure-chip exchange and
  factory-test paths. Availability of a command does not establish the presence
  of the optional hardware.

#### 1.5.0 — buffer recovery and tag filtering

- The buffer watchdog adds an autonomous channel-motor start/stop attempt during
  buffer feeding. This changes physical behavior, beyond adding diagnostic logs;
  host retries must account for the MCU's own recovery attempt.
- Additional tag-state checks can make RFID data unavailable. The response
  layouts of `0x02` and `0x03` remain unchanged, while the selected RFID or
  remaining-percentage result can differ.
- The six-byte ordinary `0x0A` layout and four-byte slot events remain.

#### 1.5.2 — additional feed command `0x12`

- Adds `0x12`, with slot mask, mode, speed byte and a big-endian 16-bit distance.
  Existing staged loading through `0x10` remains available.
- Mode 0 reuses part of the preparation for `0x10` stage 0, omitting its final
  drive/wait step. It is not an alias for the complete stage.
- In distance-feed mode 1, the transmitted speed byte is ignored; the MCU uses
  its configured speed. Reaching the requested distance and stopping on a
  stall or iteration limit all return status `0x00`, so that reply alone cannot
  establish distance completion.
- The retained `serial_485.so` applies a different command-specific retry policy
  to `0x12` than to `0x10`. The examined stock `box` does not use `0x12`; the new
  command is not automatically substituted into its operation sequence.

#### 1.5.3 — ordinary operation contract retained from 1.5.2

The examined images differ by 31 bytes: 27 in version/build metadata, two factory
test parameters changed from 99 to 50, and two bytes in a header field whose
purpose is not independently established. Ordinary command paths, response
layouts and loading/unloading algorithms match 1.5.2.

Versions 1.5.2 and 1.5.3 can share the ordinary-operation compatibility profile
while retaining their exact reported identities. Factory tests differ; neither
version has hardware acceptance of the open replacement.

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
