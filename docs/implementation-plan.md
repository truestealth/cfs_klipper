# Implementation plan

English | [Русский](implementation-plan.ru.md)

**Status:** stage 1 is complete. Stage 2 state/configuration/storage and stage 3
native/decoder/version components are implemented and PC-tested.
Stage 4 adds lifecycle, operation-context and common-error components. Stage 5
implements configured host mechanical primitives with synthetic PC checks. Stage 6
adds preload, loading and unloading phases with PC checks. Full tool changes,
retained sensor-producer execution, recovery and hardware acceptance remain incomplete. The bootstrap refuses activation; no installer or printer
changes have been introduced.

## Target and design

Implement the full stock Klipper `box` contract for the reference K1C with K1
Series CFS Upgrade, using CFS MCU **1.1.3** as the baseline. Retain the installed
`serial_485.so`, MCU firmware, address manager, screen, Creality Print, existing
macros, configuration and spool data.

Use one implementation with explicit differences for known firmware versions
inside the code. Start from the existing Klipper layout and Python 3.8 target.
Add modules when implemented behavior needs them; use the standard library,
`unittest` and the existing minimal CI. Split small working changes within each
stage, with matching tests and English/Russian documentation.

The stage order follows dependencies. State/data work and native-call work can
proceed independently after the contract inventory. Shared lifecycle and
operation-error handling must precede mechanical operations.

## Stages and completion criteria

### 1. Turn the recovered contract into an implementation checklist

Map each externally observable behavior to its implementation stage and check:

- 71 named G-code registrations: 69 unconditional and two conditional Hall
  commands; aliases and all 32 generated tool commands;
- parameters, defaults, bounds, return/error behavior and registration order;
- status fields/types, the configuration endpoint, configuration modification
  and saving, persisted data, events and macro dependencies;
- all 19 host command identifiers and 25 decoded response statuses, separating
  working call paths from reserved identifiers without a recovered direct API.

Use the existing reconstruction. Resolve gaps as the relevant path is reached.
Before executing a conflicting path, close the observed legacy `0x13` request
length mismatch and distinguish public STAGE values, host stage names and wire
stages. Include both `[box] version` encodings in the inventory. Check alternate
serial section names, optional head/cut sensors and position-disabled setups.

**Complete when:** every required behavior has an owner stage and a concrete
acceptance case; unresolved execution paths are explicitly identified. A command
name or a success-returning placeholder does not establish implementation.

**Stage 1 result:** [contract matrix](contract-matrix.md), with every requirement
bound to an implementation stage and planned checks, including macro consumers.

### 2. Implement state, configuration and compatible storage

**PC result:** components and nine data handlers are implemented, with runnable
references in the [matrix](contract-matrix.md). The stock transport is untouched.
See the [storage differences](compatibility.md#state-configuration-and-storage)
for preservation rules and the distinction between cached labels and observations.

Implement the four-box/sixteen-slot state, physical and remapped tool names,
configuration loading, `get_status()` and `box/get_box_cfg`. Add the applicable
non-mechanical metadata, grouping and remapping commands, then configuration
modification/saving and compatible persistence.

Preserve historical field names, types and distinct sentinel values; status is
a deep copy. Keep per-box sensors separate from the shared stock buffer cache.
Treat loader bounds, endpoint bounds and runtime modification rules individually.
Read existing spool/material data and preserve user values and wrappers. Keep
saved source/material hints separate from current observations.

Preserve the direct Python paths consumed by retained printer modules, including
live state writes, the metadata hook and `virtual_sdcard.resume_tnn`. Expose
compatible attributes without reproducing the old internal class hierarchy.

**Complete when:** synthetic client cases cover all 21 configuration endpoint
fields, remapping across boxes, data editing and grouping. Temporary-file tests
cover existing/missing data, failed writes, saved metadata for an empty slot and
power-loss data cleanup without losing the material cache.

### 3. Integrate native calls, reply decoding and per-device versions

**PC result:** exact request bodies, validated command/status replies, borrowed
native API calls and per-address identities/profiles are implemented. Twelve
native-command handlers join the nine data handlers in substitute-based tests.
The stage-3 suite had 103 test methods. Real native ABI/wire behavior and
operation/error/event integration remain unverified; the bootstrap refuses
activation. See the [native integration boundaries](protocol.md#native-integration)
and runnable references in the [matrix](contract-matrix.md).

Obtain the configured stock Klipper serial object and use its supported request
API. Implement the required host bodies, response validation and command-specific
decoding. Distinguish normal replies, slot events, faults, no response and
exceptions. Read and retain raw version/serial identity for each address before
version-dependent mechanics, including after reconnect or a device change.

Account for the existing native retry layers and receive-admission restrictions.
Keep transaction timeout, operation deadline and loss-of-communication detection
separate. Preserve the recovered sender retry behavior where required; add no
generic retry layer. Stock serial retains UART and connection ownership.

In particular, `retries_en=False` does not guarantee one transmission, the
`3600` argument takes the native lower-level one-second wait path, and response
registration does not expose every incoming frame. Make these constraints
explicit in the relevant call and callback checks.

**Complete when:** a small native substitute verifies exact request bytes,
timeouts, result handling and allowed repeated API calls. Cases cover malformed
frames, negative statuses, event replies without telemetry refresh, late/missing
replies, mixed firmware versions and invalid/changed identities. These checks
establish native API use; actual wire transmissions require target validation.

### 4. Implement Klipper lifecycle and the foundation for operations

**PC result:** explicit ready/shutdown, configured dependencies, per-address
reconnect/identity, round-robin heartbeat, owned scheduling, cooperative operation
context and shared client reporting are implemented. There are 23 functional
handlers. Targeted heartbeat ownership and failed stage6 results during a stage4
native wait are tested. Stage 6 supplies loading consumers and power-on refresh;
retained sensor-producer execution and reason-specific recovery remain partial.
Unavailable phase actions fail explicitly. See [lifecycle integration](protocol.md#lifecycle-integration)
and the [matrix](contract-matrix.md). The bootstrap still refuses activation.

Implement command/event registration as the handlers become functional, dependency
lookup, ready/shutdown behavior, connect/reconnect and per-address heartbeat.
Coordinate active operations, target/source context, cooperative waits, callbacks,
saved error context, reporting and the common pause/abort behavior needed by the
next stages. Release this extension's own timers/callbacks during shutdown.

The head-sensor callback must remain deliverable while loading is in progress.
The event named `box:extrude_process_stage7` sends wire stage **6** during stage
**4**; a completion flag can also accompany failure. Use the saved result, not
the flag alone. Do not hold a blocking lock across that interaction. Retain the
distinction between native connection state and a particular CFS being present.

**Complete when:** controlled reactor tests cover callback timing/repetition,
disconnect during an operation, an error during waiting, targeted heartbeat
control and shutdown with pending work. Required failure reporting works before
any mechanical operation depends on it. Incomplete builds cannot start unfinished
procedures or report them as successful.

### 5. Implement configured motion, heating, cutting and cleaning

**PC result:** configured positioning, Z and limit context, optional fan pins,
heating and extrusion, cut-sensor and blind cutting, cleaning and calibration
are implemented and tested with substitutes. Missing position fields are checked
before the affected movement. The cleaning route uses four strokes independent
of `clean_run_count`; fan saves use `last_value * scale`. `speed_factor` starts
as `None`; no M220 behavior is inferred. Registered-action compositions cover
wheel/feed/cleaning, shared error restoration and macro aliases. Hardware and
full tool-change routes remain unverified. The [host policies](compatibility.md#host-actions)
record deliberate guards. Colour-derived purge calculations use the retained Klipper
`chelper` interface (`get_ffi`, `rgb_t` and `get_flushing_volume`); its native
implementation is substituted in PC tests.

Implement the host primitives used by all routes: configured positions, heating,
extruder modes, Z moves, temporary motion limits, fans, cutting with and without
the configured cut sensor, nozzle cleaning, purge/extruder actions and relevant
calibration commands. Preserve user macro wrappers and configured dimensions.

**Complete when:** tests verify guards, emitted G-code ordering and required
E/Z/fan/limit state on completion and each specified failure exit. Missing or
stale required observations cannot become measured success. An established
no-sensor fallback remains distinguishable from an actual sensor reading.

### 6. Implement complete CFS loading, unloading and preload phases

**PC result:** preload, staged load, head-result consumption and known/unknown
unload phases are implemented and exercised with substitutes and real Box
composition. Legacy opcode 0x13, typed encoding 0 and wire stages above 7
remain blocked; printer acceptance remains open.

Implement preload permission OPEN/CLOSE separately from RUN/TIGHT procedures,
pretension/tighten control, staged loading, head callback coordination, final
feed and unloading. Cover known-source unloading and both unknown-source routes,
selected by head state. Implement each phase's documented retries and failure
exits together with the successful path.

**Complete when:** PC scenarios cover supported stages, callback before/during
polling, failed stage 6, delayed results, sensor disagreement and each unload
starting condition. ACK, a queued action, `False`, `None` and an exception retain
their separate meanings. The active source is cleared or changed only at the
specified transition; saved `last_cmd` does not establish filament position.

### 7. Compose full tool changes and the print lifecycle

Compose logical/physical remapping, same-content/continue paths, cutting,
unloading, loading, PRINT mode, extruder feed, tray purging/cleaning or the
slicer's prime tower, and required state restoration. Implement the separate
direct phase commands as well as the full T route.

Include first-tool selection from print metadata, start/end preload policy,
enable/tighten behavior, slicer-version and flushing metadata, `BOX_END_PRINT`,
`BOX_END` and the service command restoring PRINT after nozzle clearing.

**Complete when:** complete synthetic print/tool-change scenarios exercise tray
and tower paths, first T, same-content/continue, renamed user macros and end of
print, with failures at significant boundaries. Screen/status and Creality Print
inputs retain their required meanings throughout the operation.

### 8. Complete runout, recovery, pause/resume and power-loss behavior

Implement runout/draining, same-material replacement selection, candidate sensor
checks, alias remapping and deferred tool changes. Complete reason-specific
error clear/retry, macro replay, pause/resume guards and after-operation handling.
Restore power-loss hints and mappings while checking the current observations.

**Complete when:** scenarios cover a missing refill candidate, several candidates
and aliases, interrupted callbacks, resume-time retry suppression, each required
error family, deferred targets and restart with conflicting saved/live state.
Clearing host error state does not assert that the physical route has rolled back.

### 9. Close PC integration and prepare reversible installation

Check the complete command/API matrix, registration order, optional configurations,
settings and persisted formats against consumer scenarios. Exercise combined
native, reactor, macro and file behavior using small substitutes and synthetic
data. Keep the Python 3.8 CI checks focused on implemented behavior, using the
project's default version file.

Prepare and test the direct Klipper file layout, backup/rollback procedure and
installation documentation against temporary directories. Limit replacement to
the intended `box` entry and implementation; preserve other subsystems and data.

**Complete when:** every required checklist entry is implemented and PC-tested,
or has an explicitly unresolved status that prevents claiming full compatibility.
No inert handler is counted as coverage. Installation/rollback file checks pass,
and the remaining printer acceptance cases are listed by exact environment.

### 10. Perform staged hardware acceptance

Use an agreed reversible installation and record the exact host library,
configuration, CFS hardware and MCU identity. Validate **1.1.3 first**:

1. Connection, identity, telemetry and client interfaces with mechanical startup
   actions excluded from this initial check.
2. Configured motion, heating, cutting and cleaning.
3. Preload/pretension, complete loading/unloading and full tool changes.
4. Real print start/end, tray/tower purging, pause/resume and client behavior.
5. Controlled failures, disconnect/reconnect, runout/refill, restart and
   power-loss recovery; settings/spool data and rollback preservation.

Then validate other targeted versions separately. Record results for the number
of physical CFS units actually tested; PC simulation of four boxes does not
establish hardware acceptance of four boxes.

**Complete when:** the full operation cycle, failure/recovery paths, screen,
Creality Print and retained settings/data meet the checklist on the recorded
environment. Static analysis, PC tests and hardware acceptance remain separate
coverage labels.

## Firmware differences inside the shared code

| Examined version | Host responsibility |
| --- | --- |
| 1.1.3 | Baseline ordinary/error `0x0A` data layout: four bytes; slot-event status `0x30`: four bytes |
| 1.2.2 | Same examined command/layout group; retain exact identity and independent validation status |
| 1.4.2 | Six ordinary/error data bytes, four slot-event bytes; account for RFID lifecycle and additional tag observations |
| 1.5.0 | Keep RFID unavailable/unknown distinct from an empty slot; account for the MCU's autonomous buffer recovery |
| 1.5.2 / 1.5.3 | Shared examined ordinary-operation behavior, with each exact version retained; no automatic switch from `0x10` to `0x12` |

Keep a small explicit mapping of known differences next to the code that uses
them. Add a branch only when a host operation needs it. RFID writes, consumption
accounting and autonomous motor recovery remain MCU responsibilities. Additional
commands unused by stock `box` do not become prerequisites for its replacement.

Unknown hardware/firmware combinations do not silently inherit 1.1.3 mechanics.
MCU identity, printer/host-library version and `[box] version` encoding remain
separate. The [firmware history](compatibility.md#firmware-history) records the
established differences and evidence limits.

## Review and parallel work

State/configuration, native decoding/version cases and consumer-oriented tests
can be delegated independently with clear file ownership. One integration owner
coordinates lifecycle, error context and mechanical operations. Review shared
interfaces before joining changes and run the relevant combined scenarios.

Resolve observed contract conflicts before executing their routes. Record any
intentional behavior change separately from compatibility work. The current
plan does not authorize silently changing user settings or repairing unrelated
subsystems. See [architecture.md](architecture.md), [protocol.md](protocol.md)
and [compatibility.md](compatibility.md).
