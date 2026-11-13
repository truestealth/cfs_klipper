# Compatibility

English | [Русский](compatibility.ru.md)

**Status:** extension in development. No complete replacement cycle has been accepted
on the target printer. This document distinguishes a required interface from
an implemented or hardware-verified one.

## Coverage

| Area | Implementation | Validation |
| --- | --- | --- |
| Proposed Klipper import layout | Implemented | PC regression test; not a printer installation test |
| Refusal to activate the scaffold | Implemented | PC regression test before printer-object lookup |
| Full contract inventory and check bindings | Stage 1 complete | Real test references, partial families and remaining acceptance cases are tracked |
| State, configuration and compatible storage | Stage 2 components implemented | Synthetic state, all 53 options, 21 endpoint fields and temporary-file tests |
| Native `serial_485` calls | Stage 3 primitives implemented | Borrowed-API substitutes, typed request bodies and retry tests; stock binary not executed |
| Wire contract | Request bodies and reply validation implemented | Synthetic CRC, length, address and command checks; real exchange unverified |
| Per-device firmware selection | Implemented for six exact cfs0 versions | Independent identities, profiles and four-/six-byte layouts tested on a PC |
| Filament loading | Pending | Operation sequencing and hardware acceptance pending |
| Unload, change, pause/resume, recovery | Pending | No replacement validation |
| Stock G-code and status interfaces | Partial | 21 registered commands: nine data and twelve native; endpoint and snapshots tested; full 103-name contract pending |
| RFID, remaining length, saved materials | Partial | Normal replies, metadata/cache updates and deferred grouping tested; hardware observations and operational lifecycle pending |
| Reversible installation | Pending | No installer or rollback provided |
| Python 3.8+ | Source target | CI configured for 3.8 and 3.11 directly from the checkout; native MIPS runtime unverified |

The local suite passed **103 unittest methods on Windows with Python 3.11.18**.
Hosted CI has not run for stage 3; its configured matrix is not evidence of a
hosted result.

The matrix records **151 implemented requirements, 58 partial and 212 pending**.
Its 96 check families include 16 implemented, 17 partial and 63 planned cases
(78 PC, 18 hardware); all hardware acceptance is pending. These family counts
differ from individual unittest methods. See the [matrix](contract-matrix.md)
for runnable references and the remaining assertions of each partial family.

<a id="state-configuration-and-storage"></a>

## State, configuration and storage

Stage 2 components are tested on a PC. Explicit `Box(config, base_dir)`
construction uses the supplied data root without obtaining a printer or serial
object. `load_config()` continues to refuse activation. The data-command subset
is registered only in bounded tests; live Klipper lifecycle is a later stage.

State preserves the four-box/sixteen-slot schema, raw value types and distinct
sentinels. Client snapshots are deep copies. Saved labels load into a separate
cache; an explicit presence-mask synchronization can apply them to live state.
That synchronization does not establish sensor freshness or completed motion.
Tool remapping saves the complete sixteen-entry map without selecting a source.
Grouping uses type/colour and the read-only material catalogue; candidate sensor
checks remain part of the later refill operation.

All 53 loader options retain their own types, defaults and bounds. The endpoint
uses its separate 21-field value/min/max schema. Construction errors, including
missing optional positions, discard partial fields and send/return `{}`;
errors from sending itself propagate. This resolves the data endpoint behavior
for position-disabled setups; their mechanical paths remain unverified.

The nine data handlers cover remapping, public/inner data edits, diagnostic
presentation, auto-refill permission, CFS-print enable, grouping and MODIFY/SAVE.
CFS-print enable changes the retained head runout helper with the inverse flag
and saves enable; tests only record that G-code, without operating a sensor.
The inner-data public command still supplies no sensor subpart.

The implementation makes these deliberate storage/configuration choices:

- MODIFY accepts literals, lists/tuples and numeric arithmetic, without arbitrary
  Python execution. Supplied string `"0"` changes RAM; SAVE independently skips
  falsey evaluated values. Existing runtime/UI/loader bounds remain separate.
- SAVE edits only `[box]`, keeps the `.1` backup and other section bytes, and
  rejects multiline values before any write. List/tuple profiles are written as
  comma-separated values readable by Klipper, instead of legacy generic repr.
  Prefix matching, truthy-only save and next-section insertion remain explicit:
  missing keys at EOF remain pending, and zero may remain a RAM-only change.
- Existing malformed JSON/cache data raises without replacement. Cache shape
  is checked before later direct indexing. Writes use a temporary sibling,
  flush/fsync and atomic replacement, preserving existing file permissions.
  Failures are reported; a live edit made before a save failure is not falsely
  described as rolled back. A slot edit merges the current file before writing.
- The compatible JSON keys and material database remain unchanged. Unknown
  fields survive. Recovery hints and cache-preserving cleanup are available as
  data APIs; applying recovery in printer operations remains stage 8. Screen
  material files and address-manager data stay with their owners.

## Native integration

Stage 3 implements request primitives through the configured stock `serial_485`
object, command-specific reply decoding and per-address identity/profile selection.
Explicit binding borrows the object without connecting or disconnecting it.
The PC suite uses substitutes; no stock binary ran and no printer connection was made.
`load_config()` still refuses activation.

Checks cover all 25 named reply statuses and their contextual outcomes, normal
data/cache updates, missing-response distinctions, communication-test byte zero
and EXTRUDE_PROCESS snapshots before error effects. Deferred RFID grouping is
guarded against a changed device or binding. Slot-event operations, error/pause
handling, timeout processing, tighten-up, drain/refill and mechanics still need
their lifecycle/operation handlers. A route requiring an unbound handler fails
explicitly; an ACK does not establish completed motion.

See [native integration and its execution limits](protocol.md#native-integration)
for the implemented boundaries and remaining checks.

<a id="firmware-versions"></a>

## CFS firmware versions

**Baseline: CFS MCU 1.1.3 on the reference K1C with K1 Series CFS Upgrade.**
The reference printer uses the stock host `serial_485.so`. This baseline defines
the starting contract for the open `box` replacement; the replacement itself
has not passed hardware acceptance.

| CFS MCU firmware | Project target | Replacement validation |
| --- | --- | --- |
| 1.1.3 | Baseline; current reference CFS firmware | Static contract review and PC profile/layout checks; no replacement hardware acceptance |
| 1.2.2, 1.4.2, 1.5.0 | Intermediate reference versions | Static cfs0/G32 comparison and PC profile/layout checks; no replacement hardware acceptance |
| 1.5.2 | Additional compatibility target | Static comparison and PC profile/layout checks; target hardware validation pending |
| 1.5.3 | Additional compatibility target | Ordinary paths match examined 1.5.2; exact identity/profile tested on a PC; target hardware validation pending |

Compatibility is recorded per CFS hardware variant, MCU firmware and retained
host-library version. A successful check on one MCU version does not validate
the others. Selection is implemented inside shared code for exact versions
**1.1.3, 1.2.2, 1.4.2, 1.5.0, 1.5.2 and 1.5.3**, independently for each address.
It requires an observed version/SN and an explicitly confirmed `cfs0` family;
the version reply does not identify the hardware family. Unknown versions or
unconfirmed families cannot select a profile or inherit baseline operations.
Identity invalidation and device replacement require renewed identification;
different serial numbers also clear the previously confirmed family.

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

The [contract matrix](contract-matrix.md) records recovered types, sentinels,
configuration and update/persistence requirements. Runtime compatibility remains
unverified until the corresponding behavior and acceptance cases are implemented.
Cached material/color and the last selected T-command do not establish the
physical filament location. Preserve user mapping instead of substituting an
identity map.

## Commands and configuration

The matrix inventories the existing `BOX_*` and tool-selection commands,
parameters, error behavior and macro dependencies, including material/color edits,
refresh and operations used by the screen and slicer. Implement each with its
bound verification cases.

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
