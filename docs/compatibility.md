# Compatibility

English | [Русский](compatibility.ru.md)

The reference configuration is a K1C with K1 Series CFS Upgrade, first-generation
CFS (`cfs0`), CFS MCU 1.1.3 and the stock `serial_485.so`. Normal mode runs
the Box commands used by the printer, screen and slicer. Set `passive: true`
in `[box]` for diagnostics and individually admitted trials.

## Coverage

| Area | Reference verification |
| --- | --- |
| Startup and spool status | Connected on MCU 1.1.3. The screen and Creality Print showed all four slots with their expected colours. |
| Preload, load and unload | T1A–T1D preload with fresh RFID and remaining data. Two full T1C load/unload cycles without manual assistance. |
| Tool changes and print | C→A→C changes, two completed two-colour jobs, and a completed three-colour OrcaSlicer job. |
| Pause and recovery | Ordinary pause/resume completed in a two-colour job. The three-colour job resumed after the operator reloaded C. PC tests cover the new Retry path and runout recovery. Klipper reached ready after installing the Retry build. |
| Firmware profiles | Per-address identity and reply layout checks on PC for the six `cfs0` versions below. Hardware verification covers 1.1.3. |
| Installation and runtime | Backup and restore checked on PC and file/cache rollback checked on the printer. Python 3.8 is the project and CI default. The reference printer runs Python 3.8.2. |

The [contract matrix](contract-matrix.md) records checks for each interface.
See the [installation guide](installation.md) for the normal and diagnostic modes.
Hosted results are available in [GitHub Actions](https://github.com/truestealth/cfs_klipper/actions).

<a id="state-configuration-and-storage"></a>

## State, configuration and storage

Explicit `Box(config, base_dir)` construction uses the supplied data root.
`load_config()` obtains `extras.base_info.base_dir`, loads the configured stock
serial section and registers the selected lifecycle. Ready resolves the other
printer dependencies and borrows the native API. The reference startup check
compared the active include chain and all 54 settings.

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
for position-disabled setups. The load, continuation and resume paths validate
their required positions before motion.

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
- Storage preserves the existing JSON keys, unknown fields and material database.
  Screen material files and address-manager data stay with their owners.

## Native integration

The extension borrows the configured stock `serial_485` object. PC checks cover
each address's identity and firmware profile, all 25 named reply statuses,
missing responses, communication-test byte zero and snapshots taken before
error effects. The [native API contract](protocol.md#native-integration) describes
transport ownership, status dispatch and retries.

<a id="host-actions"></a>

## Host actions

Host actions use the configured positions, acceleration, fan, heater, extrusion,
cutter and calibration settings. Registration
before retained macro aliases provides 101 unconditional commands, including
one blocked legacy command, plus two Hall commands when `switch_pin` is truthy. Cleaning uses four fixed strokes; `clean_run_count` is
retained as configuration without changing that routine. Cutter current state
and the sticky observed event remain separate; blind motion is not a measured cut.

Standalone `BOX_MATERIAL_FLUSH` accepts `180 <= TEMP <= 300`, with invalid values
falling back to `Tn_extrude_temp`. An omitted `LEN` preserves `Tn_retrude`, even
when negative: retained Klipper returns a missing parameter's default before
checking explicit-input bounds. Explicit `LEN` remains restricted to 0..200.
The handler passes `tnn=None`. A retained macro's optional `M109` is a separate
heating step.

Colour purge volume uses the retained Klipper `chelper.get_ffi()` interface,
`rgb_t` structures and `get_flushing_volume`. PC checks verify the subsequent
length conversion. Tool changes on the reference printer used the factory helper.

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

## Runout, recovery and resume

A failed tool change from virtual SD keeps the target, open file and continuation
position across `BOX_ERROR_CLEAR`. After inserting filament, press **Retry** to
check the selected slot and run the existing load and purge phases.
`BOX_RESUME_EXTRUDE` gates continuation on the real head sensor. A failed retry
leaves the job paused.

Ending-material drain, candidate selection, deferred refill, macro replay and
power-loss hint restoration have PC checks. Refill decisions require fresh
sensor replies. `BOX_ADD_TNN` queues a known virtual tool on
`virtual_sdcard.resume_tnn` for later execution. Shutdown removes the extension's
queued work, and device changes invalidate stale callbacks.

## Preload, loading and unloading

Preload distinguishes literal `pause` from `paused`. Force permits the former, while `printing` still
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
source's last-result behavior.

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
recovered direct-feed parser accepts addresses through 9. Retained Jinja and
sensor-producer execution have separate PC checks.


## Tool changes and print lifecycle

Tool selection resolves the physical spool from the logical tool and uses the
logical indices for the slicer's purge matrix. Tray and prime-tower paths have
distinct sequences. `tn_final_extrude_len` is a float with default `-2.5` and no
loader bounds. PC tests cover cross-box changes, purge failures and replacement
of a source or target during a change.

`BOX_END_PRINT` resets the live map, clears saved recovery hints and sets global
`enable` to `1`. It disables the configured head runout sensor, or omits the
sensor command when none is configured. The original used a fixed sensor name;
this follows the project policy for custom and absent sensors. It is distinct
from the physical `BOX_END` operation.

<a id="pc-integration-and-remaining-blockers"></a>

## Klipper and macro integration

The reference `START_PRINT` macro calls `CFS_PRINT_START_GUARD` before startup,
heating and motion. It determines the first tool from the selected G-code file
and checks an explicit `INITIAL_TOOL` against it. After heating,
`BOX_START_PRINT_EXTRUDE_MATERIAL` loads the first tool. The reference LAN service
supplies the physical `TNN` to material-feed commands. Direct callers of
`BOX_EXTRUDE_MATERIAL` must supply it, too.

Tests exercise macro rendering and argument forwarding with Jinja2, and use
substitutes for reactor timing, serial replies and physical effects. With
`has_extrude_pos=0`, configuration data remains available and actions requiring
extrusion positions stop before motion. `BOX_END` checks its final safe position
before lowering Z or draining the ending material.

<a id="firmware-versions"></a>

## CFS firmware versions

**Baseline. CFS MCU 1.1.3 on a K1C with K1 Series CFS Upgrade.**

| CFS MCU firmware | Project target | Replacement validation |
| --- | --- | --- |
| 1.1.3 | Baseline | Reference hardware results above; PC regression tests |
| 1.2.2, 1.4.2, 1.5.0 | Reference versions | Static `cfs0`/G32 comparison and PC profile/layout checks |
| 1.5.2 | Additional compatibility target | Static comparison and PC profile/layout checks |
| 1.5.3 | Additional compatibility target | Ordinary paths match examined 1.5.2; identity and profile checked on PC |

Compatibility is recorded per CFS hardware variant, MCU firmware and retained
host-library version. A successful check on one MCU version does not validate
the others. Selection is implemented inside shared code for exact versions
**1.1.3, 1.2.2, 1.4.2, 1.5.0, 1.5.2 and 1.5.3**, independently for each address.
It requires an observed version/SN and an explicitly confirmed `cfs0` family;
the version reply does not identify the hardware family. Unknown versions or
unconfirmed families cannot select a profile or inherit baseline operations.
Identity invalidation and device replacement require renewed identification;
different serial numbers also clear the previously confirmed family.

The comparison covers first-generation `cfs0`/G32 applications. CFS2 (`cfs6`) and
configurations with an external CFS-C cutter require their own validation.
The G30 images for 1.1.3, 1.2.2, 1.4.2, 1.5.0 and 1.5.2 match the corresponding
G32 application binaries byte for byte. This establishes application code
equivalence. The G30 loader and printer mechanics require separate verification.

For early `cfs0` 1.0.8, 1.0.9 and 1.1.2, passive mode covers `0x14` identity
and four-byte `0x0A` state, fault and slot-event observations. The MCU can
consume a pending notification when it answers `0x0A`. Box records the reply
without running filament recovery or updating saved spool data. The scope is
based on application analysis and PC tests. Mechanical operation requires
separate verification.
In a mixed chain, an active change or retract with an unresolved filament
source is blocked if an early unit might still have filament in the path.

Static analysis of two CFS2 applications found different reply layouts:

| Board and application | Identity `0x14` | State `0x0A` |
| --- | --- | --- |
| G31 `cfs6_236_000` | ASCII `236` and a variable-length identifier | 13 state bytes or four slot-event bytes with status `0x30` |
| G51 `cfs6_237_000` | ASCII `237` and a variable-length identifier | 13 state bytes or four slot-event bytes with status `0x30` |

The fixed part of either identity reply carries the version digits. Selecting
a CFS2 profile requires a trusted family identifier and confirmation that the
stock host transport routes those replies. K1C runtime support is unverified.

MCU firmware is separate from the printer firmware and `[box] version` setting.
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

The [contract matrix](contract-matrix.md) records field types, sentinels and
update requirements. User mapping is preserved. The physical filament location
is verified separately from cached material, colour and the last T-command.

## Commands and configuration

The matrix lists the existing `BOX_*` and tool-selection commands, parameters,
error behavior and macro dependencies. Keep the working configuration and user
wrappers, including `rename_existing` chains and command registration order.

## Acceptance and boundaries

PC tests use synthetic data and substitutes for Klipper and the serial API.
Hardware results apply to the stated printer, MCU and operation. See
[architecture](architecture.md) and the [protocol contract](protocol.md).
