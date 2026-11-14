# Compatibility

English | [Русский](compatibility.ru.md)

**Status:** extension in development. No complete replacement cycle has been accepted
on the target printer. This document distinguishes a required interface from
an implemented or hardware-verified one.

## Coverage

| Area | Implementation | Validation |
| --- | --- | --- |
| Proposed Klipper import layout | Implemented | PC regression test; not a printer installation test |
| Passive Klipper startup | Implemented on PC | Real load_config, owner dependencies and data root; identity/telemetry only, host actions blocked |
| Full contract inventory and check bindings | Stage 1 complete | Real test references, partial families and remaining acceptance cases are tracked |
| State, configuration and compatible storage | Stage 2 components implemented | Synthetic state, all 54 options, 21 endpoint fields and temporary-file tests |
| Native `serial_485` calls | Stage 3 primitives implemented | Borrowed-API substitutes, typed request bodies and retry tests; stock binary not executed |
| Wire contract | Request bodies and reply validation implemented | Synthetic CRC, length, address and command checks; real exchange unverified |
| Per-device firmware selection | Implemented for six exact cfs0 versions | Independent identities, profiles and four-/six-byte layouts tested on a PC |
| Lifecycle, shared errors and operation context | Component implementation | Cooperative reactor and substitute tests; physical operations remain incomplete |
| Configured motion, heating, cutting and cleaning | Stage 5 components implemented | Synthetic G-code, fan, heater and sensor checks; hardware acceptance pending |
| Preload, loading and unloading | Stage 6 components implemented | Synthetic phase checks; hardware acceptance pending |
| Tool change and print lifecycle | Partial | PC substitutes cover tool changes and ending-material drain; hardware acceptance pending |
| Pause/resume and recovery | Stage 8 components | PC substitutes cover recovery, refill, resume and persisted hints; bounded retained-source checks supplement CI; hardware unverified |
| Stock G-code and status interfaces | Partial | All 103 names registered, including two conditional Hall commands and one blocked legacy feed command; endpoint and snapshots tested |
| RFID, remaining length, saved materials | Partial | Normal replies, metadata/cache updates and deferred grouping tested; hardware observations and mechanical slot handling pending |
| Reversible development-file installation | Implemented | Preview, backup, scoped restore, failure and tampering tests in temporary trees; passive bootstrap |
| Python 3.8 | Project and CI default | 576 methods run: 573 passed, 3 Windows symlink-privilege skips on Python 3.8.20; test-only Jinja2; native MIPS runtime unverified |

The 576-method total is the public suite only. Separate local runs passed 14
retained-module checks, 13 saved-macro checks and the 17 public macro checks
using the retained renderer. They are additional evidence, not extra public
test methods. The three skips require Windows native symlink privileges;
platform-independent reparse rejection is checked. The retained checks include
the actual virtual-SD worker after a passive diagnostic: it does not dispatch PAUSE.

PC admission adds 30 public methods: 10 bootstrap, 15 passive lifecycle and five
composed failure-state checks. Caller-boundary False/None returns are injected
after real phases; separate effect exceptions exercise interrupted phases.
All eight recovery leaves assert their distinct shared-context tails. These
checks do not imply physical rollback or a successful hardware operation.

Coverage records local component checks. Hosted results for each commit are
available in [GitHub Actions](https://github.com/truestealth/cfs_klipper/actions).
See the [matrix](contract-matrix.md) for implementation status, runnable references
and remaining acceptance cases. Hardware acceptance is pending.

<a id="state-configuration-and-storage"></a>

## State, configuration and storage

Stage 2 components are tested on a PC. Explicit `Box(config, base_dir)`
construction uses the supplied data root without obtaining a printer or serial
object. `load_config()` now obtains the established `extras.base_info.base_dir`,
loads the configured stock serial section, and registers a passive lifecycle.
Ready resolves the remaining printer dependencies and borrows the native API.
The real entry-to-heartbeat route is tested with substitutes; no printer was used.
See [passive startup](installation.md#passive-startup) for its restrictions.

State preserves the four-box/sixteen-slot schema, raw value types and distinct
sentinels. Client snapshots are deep copies. Saved labels load into a separate
cache; lifecycle initialization applies them only after a fresh successful
MATERIAL presence response. That observation does not establish completed motion.
Tool remapping saves the complete sixteen-entry map without selecting a source.
Grouping uses type/colour and the read-only material catalogue; candidate sensor
checks are performed by the implemented refill operation.

All 54 loader options retain their own types, defaults and bounds. The endpoint
uses its separate 21-field value/min/max schema. Construction errors, including
missing optional positions, discard partial fields and send/return `{}`;
errors from sending itself propagate. This resolves the data endpoint behavior
for position-disabled setups. Stage 5 validates fields needed by an affected
move before issuing it. Stage 9 checks position-disabled data, load, continuation
and resume paths before effects; exhaustive partial-coordinate combinations
remain unverified.

The nine data handlers cover remapping, public/inner data edits, diagnostic
presentation, auto-refill permission, CFS-print enable, grouping and MODIFY/SAVE.
CFS-print enable changes the configured head runout helper with the inverse flag
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
  data APIs; stage 8 restores validated hints in host operations. Screen
  material files and address-manager data stay with their owners.

## Native integration

Stage 3 implements request primitives through the configured stock `serial_485`
object, command-specific reply decoding and per-address identity/profile selection.
Explicit binding borrows the object without connecting or disconnecting it.
The PC suite uses substitutes; no stock binary ran and no printer connection was made.
`load_config()` permits only passive identity/telemetry admission.

Checks cover all 25 named reply statuses and their contextual outcomes, normal
data/cache updates, missing-response distinctions, communication-test byte zero
and EXTRUDE_PROCESS snapshots before error effects. Deferred RFID grouping is
guarded against a changed device or binding. Lifecycle, shared reporting,
timeout handling and operation ownership have component tests. Slot events
and drain/refill have synthetic PC checks; a route
requiring an unbound handler fails explicitly. An ACK does not establish
completed motion.

See [native integration and its execution limits](protocol.md#native-integration)
and [lifecycle integration](protocol.md#lifecycle-integration) for the implemented
boundaries and remaining checks.

<a id="host-actions"></a>

## Host actions

Stage 5 implements configured positioning, Z and acceleration context, fan
save/restore, heating, extrusion, cutting, cleaning and calibration. Registration
before retained macro aliases provides 101 unconditional commands, including
one blocked legacy command, plus two Hall commands when `switch_pin` is truthy. Cleaning uses four fixed strokes; `clean_run_count` is
retained as configuration without changing that routine. Cutter current state
and the sticky observed event remain separate; blind motion is not a measured cut.

Colour purge volume uses the retained Klipper `chelper.get_ffi()` interface,
`rgb_t` structures and `get_flushing_volume`. PC substitutes verify that call
and the subsequent length conversion; the target native helper has not been
executed. No colour-volume approximation or replacement helper is introduced.

The implemented host policies are:

- Validate required optional positions before the affected action; missing
  fields raise without inventing coordinates. Calibration requires actual head
  and cut sensors, and each search candidate must stay within configured travel.
- Guard cooperative calls against shutdown/restart, rebinding and a changed
  active target/device. Error and restoration tails follow their own branches;
  an exception does not promise physical rollback.
- Use a fresh, successful ordinary measuring-wheel reply with a finite value.
  Cached data, event replies and missing measurements cannot establish success
  or become an invented nozzle-blockage result.
- Reject a nonpositive segment size when purge splitting needs it, avoiding
  the original nonterminating zero-step branch.

Tests compose native wheel replies with segmented extrusion and actual Python
cleaning, shared error restoration, macro alias dispatch and temporary-file
configuration saves. Stage 9 adds bounded Jinja and retained-producer execution checks. Hardware
acceptance remains separate. The bootstrap blocks host actions in passive mode.

## Runout, recovery and resume

Stage 8 adds ending-material drain, candidate selection and remapping, deferred
refill, reason-specific error clearing and retry, macro replay, same-material
resume extrusion and power-loss hint restoration. PC tests use synthetic native
replies and controlled callbacks. A saved source is a host hint, not proof of
physical filament position; refill decisions require fresh successful sensor
replies. The existing no-head-sensor True fallback is retained and is not a
measurement. Errors and return values follow their specific paths; clearing a
host error does not establish mechanical rollback.

`BOX_ADD_TNN` queues a known virtual tool on `virtual_sdcard.resume_tnn`; it
does not execute the later tool change. The replacement rejects unknown queued
tool strings and cancels its unchanged queued target on shutdown. Public CI uses own fixtures; separate local checks execute pinned retained
virtual-SD replay and saved macro templates with substituted effects.
Cooperative guards stop stale work after shutdown, rebinding or relevant device
replacement. Shutdown clears the extension-owned refill and draining in-progress
flags while preserving source and error context. Hardware acceptance remains
pending; development-file installation is tested separately from activation.

## Preload, loading and unloading

Stage 6 adds `feeding`, `loading` and `retraction` phases. Preload distinguishes
literal `pause` from `paused`: force permits the former, while `printing` still
suppresses the request. RUN/TIGHT require the head helper to report absent; its
configured no-sensor fallback remains True. OPEN/CLOSE update RAM preference
before attempting connected addresses. RUN/TIGHT timeouts scale with mask bits;
an ACK or timeout is not physical completion.

Loading preserves the head callback result and the source's distinct `False`,
`None` and exception branches. Fresh ordinary successful material and buffer
observations are required for decisions; missing or event replies do not become
cached success. Unknown-source unloading scans fresh connection/material masks;
a failed shared buffer retract follows its cleanup branches and subsequent head
check. A connection-only slot reports the material error while retaining the
source's last-result behavior. These are PC checks, not mechanical acceptance.

Final feed encodes an integral configured length such as `3.0` exactly as byte
`3`, preserving the configured value. Fractional, nonfinite or out-of-byte values
raise before final-feed motion. This corrects the original mismatch between an
explicit `getfloat` setting and the byte encoder; an omitted integer default
stays an integer in the retained configuration reader.

The version-1 public `BOX_EXTRUDE_PROCESS` passes STAGE in the recovered call's
`trigger` position, so the default wire stage remains 2. Internal stage keywords
remain distinct. Encoding 0, typed wire stages above 7 and legacy opcode `0x13`
are explicitly blocked pending resolution; `BOX_EXTRUDE_2_PROCESS` is registered
but cannot send. Execution requires a confirmed CFS address 1..4, even though the
recovered direct-feed parser accepts addresses through 9. Stage 9 separately verifies retained Jinja and actual sensor-producer execution
on the PC. Hardware acceptance remains open.


## Tool changes and print lifecycle

Stage 7 composes tool selection with existing motion, load, unload and purge phases.
The slicer matrix uses logical tool indices after resolving physical spool mappings;
tower and tray paths have distinct effects. `tn_final_extrude_len` is a float
with default `-2.5` and no loader bounds. Composed PC tests run real Box phases
with synthetic native replies, including a cross-box change, tray and tower paths,
continuation, end cleanup and purge failure. They also check that source or target
replacement stops subsequent effects and that a late ending-material latch reaches
the drain route. These checks do not establish physical completion. Stage 8 adds
reason-specific recovery. Stage 9 adds bounded retained Jinja and producer
execution checks; these do not establish hardware acceptance.

`BOX_END_PRINT` resets the live map, clears saved recovery hints and sets global
`enable` to `1`. It disables the configured head runout sensor, or omits the
sensor command when none is configured. The original used a fixed sensor name;
this follows the project policy for custom and absent sensors. It is distinct
from the physical `BOX_END` operation.

## PC integration and remaining blockers

Public tests execute own synthetic Jinja fixtures, renamed base commands,
render-time snapshots, raw argument forwarding and real Box handlers. The only
additional test dependency is pinned Jinja2 3.1.6; runtime requirements are
unchanged. Separate local verification executes pinned retained Python modules
and saved macro sources. That evidence is not public CI or live configuration
verification: external actions, reactor timing and serial replies are substituted.

The actual retained head producer runs RunoutHelper before the Box event.
Disabled, debounced and duplicate helper input still permits a true-level event;
Box owns duplicate/stale rejection. Full loading consumes the saved stage-six
result even when False while the surrounding stage-four call succeeds. The
eight direct retained Box paths use shared live state. Retained virtual-SD
replay clears its target after any normal command return, including False, but
an exception retains the hint; neither outcome certifies physical loading.

Both examined saved material-present/material-absent load wrappers omit the
physical `TNN` required by `BOX_EXTRUDE_MATERIAL`. Static reconstruction predicts the same original-handler failure; the original
binary was not executed. This is an unresolved caller-contract blocker
(`GAP-MACRO-LOAD-TARGET`), not a successful load scenario. Supply an explicit
physical target or agree a compatibility policy before relying on those paths;
the extension does not infer a target or rewrite user macros. Saved copies do
not establish today's active includes or overrides: `GAP-MACRO-SNAPSHOT` remains
a precondition for printer activation/hardware acceptance.

Stage 9 adds preflight for unsupported loading encodings and required movement
positions. Explicit encoding-0 loading/tool changes stop before heating or
motion. With `has_extrude_pos=0`, data/endpoint access remains available while
affected load, movement, continuation and resume paths fail before their effects.
`BOX_END` validates its final safe-position requirement before Z movement,
ending-material drain or sensor/flag changes. The same-content path may still
use the established no-head-sensor fallback without tray positions or fan pins.
These are deliberate host guards, not inferred coordinates or physical rollback.

The [development installer](installation.md) changes only the scoped Box files
and preserves retained modules, configuration and spool data. Backup/restore and
failure cases are tested in temporary trees. Its bootstrap starts in passive
admission mode. Encoding 0, legacy opcode 0x13, typed stages above
seven, reserved operations and unaddressed diagnostics remain explicitly gated.

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
