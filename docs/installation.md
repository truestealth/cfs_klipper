# Local installation preparation

[Русский](installation.ru.md)

This tool copies the extension to a local Klipper `extras` directory. The
bootstrap initializes passive admission with mechanics blocked. A reference K1C
trial on CFS MCU 1.1.3 and printer Python 3.8.2 reached ready and sustained native
identity/state reads. Mechanical acceptance remains separate. The tool itself
does not contact a printer, manage services, or edit configuration, macros,
firmware or data; remote deployment and restarts require explicit authorization.

Run from this checkout with Python 3.8 or later. Both paths must be explicit;
the extras directory and backup parent must exist. The backup path itself must
not exist and must be separate from the extras directory. `extras/box.py` must
be an existing regular file, and `extras/cfs` must be absent. Links, reparse
points and parent traversal are rejected. Native `box.so`, `box.*.so`, `box.pyd`
and `box.*.pyd` entry points are rejected because they can shadow `box.py`;
`box_wrapper.so` is retained. The default action is a read-only
preview:

```sh
python tools/install.py --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
python tools/install.py install --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
python tools/install.py restore --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
```

Installation saves the original `box.py` bytes, mode and timestamps alongside
a hash manifest, then copies only the fixed Python file list. Both entry-cache
layouts are supported: `extras/__pycache__/box.cpython-*.pyc` and the sibling
`extras/box.pyc` used by the reference printer. Original caches are backed up and
invalidated before switching source; restore removes generated matching caches
and restores the originals with their bytes, mode and timestamps. If an entry
cache did not originally exist, a generated matching cache is removed on restore.
Other cache entries are left alone. Existing
`box*.so`, `serial*.so`, other extras and user data are not modified. Restore
checks the backup and every installed file and refuses if owned files changed or
the `cfs` directory contains additional entries, except for recognized bytecode
of the installed modules: `cfs/<module>.pyc` or matching files in
`cfs/__pycache__`. Only these owned cache files are removed.
Unknown cache entries and links are rejected. It verifies the restored bytes
and retains the backup. Keep the backup safe: hashes detect accidental changes,
but do not authenticate a deliberately altered manifest.

New backups use manifest version 3. Modern-cache backups from version 2 remain
restorable. A version 2 backup with a sibling `extras/box.pyc` is rejected before
mutation: the old manifest cannot establish whether that file was original or
generated. Preserve the original source/cache evidence and use a separately
verified recovery procedure for that older ambiguous layout.

An installation error attempts to roll back changes already made; the backup
directory remains for inspection. A restore error can require manual recovery
from `box.py.original`; do not discard it. The target must remain quiescent
throughout either operation: the tool does not lock out concurrent writers or
survive power loss as a transaction.

Stop the target Klipper process before install or restore, then verify the copied
files and backup before restarting it. Inspect the actual service wrapper first:
the examined firmware's start/restart branch can copy factory configuration and
restart another MCU service. Those actions are outside this file installer.

<a id="passive-startup"></a>

## Passive first startup

`load_config(config)` now returns the `box` object instead of rejecting every
startup. It obtains the absolute data root from the retained scalar
`extras.base_info.base_dir`, requires `gcode` and `webhooks`, and asks Klipper to
load `serial_485 <bus>` using `[box] bus`. It does not import the native binary
directly or open a second serial connection. A configured cutter switch uses
the retained `buttons` object.

On `klippy:ready`, the lifecycle resolves `toolhead`, `gcode_move`, `print_stats`,
`virtual_sdcard`, the configured head sensor, the four-entry retained
`extras.auto_addr_wrapper.addr_manager_table_mb` and `greenlet.getcurrent`.
The existing address-manager configuration must remain enabled; its own bus
requirements are not changed by Box's configured lookup.
Only then does it borrow the configured serial object, emit `box:ready` and
schedule its heartbeat. A failed ready attempt cancels extension-owned work
and releases its native binding without closing the stock transport.

The entry supplies explicit first-generation `cfs0` context for addresses 1–4.
This is the supported K1 CFS Upgrade target, not hardware identification from
the version reply. Exact firmware profiles remain required, with 1.1.3 as the
baseline; a replaced serial number clears family confirmation.

By default, admission permits only typed `GET_RFID` (`0x02`), `GET_REMAIN_LEN` (`0x03`),
`GET_BUFFER_STATE` (`0x05`), `GET_FILAMENT_SENSOR_STATE` (`0x08`), `GET_BOX_STATE`
(`0x0A`) and `GET_VERSION_SN` (`0x14`). Wheel requests, raw diagnostics,
`CREATE_CONNECT`, motor/preload writes and host actions are rejected. Incoming
error/runout/head events cannot trigger pause, motion restoration, heating,
tightening or sensor-enable G-code. Automatic preload permission and power-on
RUN are suppressed. Existing macros can encounter these explicit admission
errors; this mode is not suitable for printing. There is no general option to
enable mechanics. Separate explicit trials below grant one operation without
changing the passive lifecycle.

Use direct typed read commands for telemetry. `BOX_INFO_REFRESH` invokes preload
RUN, and existing temperature wrappers can heat before reaching a guarded Box
handler. The passive Box does not disable G-code owned by other subsystems.

After identification, each passive initialization reads a fresh MATERIAL presence
mask before applying saved labels to live state, including reconnect and addresses
already marked connected by the retained address manager. Passive diagnostics use
`Lifecycle.last_error`, leaving the retained virtual-SD pause hint `box.last_err`
unchanged.

The spool cache stays at `<base_dir>/creality/userdata/box/tn_data.json`, with
`material_database.json` beside it; the flushing marker stays at
`<base_dir>/creality/userdata/config/flushing_sign`. Existing valid cache data
is loaded and malformed data raises an error. RFID and remaining-length replies
follow the established metadata persistence: even a read request can rewrite
the compatible cache when the values stay equal. A missing file or `base_data`
creates the compatible empty cache. Passive admission therefore does not mean
read-only storage; explicit data/configuration commands can also save data.
`SAVE_BOX_CFG` retains the factory paths `/usr/data/printer_data/config/box.cfg`
and `/mnt/UDISK/printer_data/config/box.cfg`; the data root does not redirect them.

PC failure tests use substitutes and temporary paths. The first reference trial
also verified the live include chain and all 54 settings, preserved protected file
hashes, and observed 103 valid native read replies over seven minutes. These are
bounded passive observations, not mechanical acceptance or validation of another
firmware version.

The main version 3 installer subsequently completed a real file/cache restore
and passive reinstallation on that target. Original entry/cache bytes, mode and
nanosecond mtime were checked with Klipper stopped; the owned package and its
caches were removed. The stock Box process was not started between restoration
and reinstallation, avoiding its automatic startup actions. Passive boots after
installation remained ready. Fresh version 3 backups use the main installer for
this layout without a separate legacy-cache adapter. This verifies filesystem
rollback; a stock-runtime restart and full mechanical acceptance remain untested.

After direct RFID/remaining reads, the touchscreen and Creality Print recognized
CFS but showed all four slots as grey or undefined. The changed cache was backed
up before the exact pre-query spool cache was restored with Klipper stopped.
A passive start of the same sources restored slot display in both interfaces,
as confirmed by the user. No decoder/runtime fix or repeated RFID/remaining
query was involved. That earlier check verified cached display, not fresh physical RFID
acquisition; RFID and remaining values were restored together.

<a id="single-operation-trial"></a>

## Explicit single-operation preload trial

This path has PC tests and a bounded authorized trial on the reference K1C with
cfs0 MCU 1.1.3. One invocation per T1A–T1D slot returned fresh RFID/remaining
data, with physical activity observed by the operator. Full mechanical acceptance
remains separate. Further installations and trials require authorization and
physical preparation. The default lifecycle remains passive.
Preserve a spool-cache backup before any authorized future trial.

The extension-only command is `CFS_TEST_PRELOAD TNN=T1A CONFIRM=1`. `TNN` must be
one physical slot, T1A through T4D; mutable tool mapping is not used. `CONFIRM=1`
marks an explicit invocation, not operator authentication. No persistent armed
state or general mechanics switch is created. Existing host commands and
automatic ready, heartbeat, reconnect, head, runout and error paths keep their
passive restrictions throughout the trial.

Before RUN, the path requires a ready connected cfs0 MCU **1.1.3**, host encoding
1, standby print state, inactive virtual SD, no pause/error/other operation,
and an actual configured head sensor reporting no filament. It obtains fresh
normal replies in this order:

1. `GET_BOX_STATE`: mode IDLE (`0`).
2. `GET_FILAMENT_SENSOR_STATE`, MATERIAL: the selected slot is present.
3. `GET_FILAMENT_SENSOR_STATE`, CONNECTIONS: the entire mask is zero.
4. One `SET_PRE_LOADING` RUN request (`0x0D`), selecting one slot bit.
5. Another `GET_BOX_STATE`: mode IDLE (`0`) again.
6. `GET_RFID` for that slot only.
7. `GET_REMAIN_LEN` for that slot only.

The permission belongs to the current cooperative dispatch, lifecycle, native
binding, device generation and exact request object. It is consumed once and
revoked when the call finishes or fails. Context is checked around waits and
before native attempts/effects, with a 300-second cooperative deadline.
Read requests use two-second timeouts; RUN is limited to the remaining trial
budget. These limits do not interrupt the stock library while it is waiting.
Missing replies, nonzero statuses, events, non-IDLE state and invalid payloads end the
trial without proceeding. RFID must contain exactly the selected slot with
`none`, `unknown` or a 40-byte record before any metadata is persisted.

On MCU 1.1.3, RUN can prepare the shared hub before processing its selected slot.
The zero CONNECTIONS requirement avoids the observed occupied-channel retraction
branch at that snapshot; it does not guarantee physical isolation or disable
autonomous MCU behavior. The RUN ACK can follow an expired internal wait, so it
is not physical-completion proof; the subsequent fresh IDLE reply is mandatory.
That IDLE reply is also only an observation: ordinary state telemetry does not
expose the separate preload FSM busy predicate. Physical observation is required;
ACK plus mode `0` alone does not establish completion on timeout/failure paths.

The host does not resend trial requests after MCU CRC/LENGTH errors. Passing
`retries_en=False` cannot cancel retries already owned by the stock `.so`.
Revoking permission or reaching the cooperative deadline prevents subsequent
host work; it is not a physical stop of an in-flight native/MCU operation. No
unverified CLOSE/STOP cleanup is sent. Results and cache writes retain the normal
RFID/remaining semantics, including `unknown` and the baseline `100` fallback.
If RFID metadata has been saved before a later remaining-value request fails,
that fresh metadata stays saved; the trial does not restore old cache values.
PC checks do not establish hardware results. Other host actions, heaters and
toolhead motion receive no permission.

<a id="reference-trial-results"></a>

### Bounded reference results

Four separate explicitly confirmed invocations tested T1A, T1B, T1C and T1D once
each, with no operator retry. A missing-confirmation invocation was rejected
before the bus call. The 28 attributed API-return frames passed independent
CRC/address/opcode/status, request-mask and sequence checks; concurrent background
state replies were kept separate. Each slot returned a fresh 40-byte RFID record
matching its saved ID and a valid selected-slot remaining percentage.

Only the selected slot's remaining value changed in each affected cache record;
the T1D trial had no semantic cache change. Other spool keys and the material
database were preserved. The operator observed spool rotation/feed/retraction
for T1A, reported normal behavior for T1B–T1D, and confirmed correct colours in
both the touchscreen and Creality Print. Normal post-trial mode `0` accompanied
these observations. Stock native libraries, MCU firmware, configurations and
macros were retained; 52 protected files and all 25 installed sources matched
their expected hashes. These normal baseline results do not accept fault/timeout
paths, full loading/unloading, cutting, purging, printing, recovery or other MCU
versions. Remaining values are MCU reports, not an independent measurement of
the material on each spool.

The captured startup/heartbeat window before the explicit tests contained only
typed reads and no preload RUN. Final read-only checks left the printer ready
with an absent head-filament observation, zero CONNECTIONS and zero heater
targets; those reads did not change spool data. This evidence is for the same
baseline unit and retained host runtime, not another firmware/hardware pairing.

<a id="material-operation-trial"></a>

## Explicit load and unload trial preparation

This admission is prepared and tested on the PC only. It has not been installed
or run on the printer. The earlier four-slot preload results do not accept this
longer route. The stock `serial_485.so`, MCU firmware, configuration and macros
remain unchanged; startup and ordinary commands remain passive.

Two separate commands select a physical slot, bypassing logical tool mapping:

```gcode
CFS_TEST_LOAD TNN=T1A CONFIRM=1
```

```gcode
CFS_TEST_UNLOAD TNN=T1A CONFIRM=1
```

Each command grants only its own invocation. Loading does not chain unloading,
and unloading does not start another load. Confirmation is explicit invocation,
not authentication. Existing load wrappers and their missing `TNN` forwarding
are preserved; this path does not repair or accept those wrappers.

Both commands require one connected/online CFS with confirmed cfs0 MCU **1.1.3**,
host encoding 1, ready/standby, inactive virtual SD, no pause or resume, no active
error, refill, ending-material drain, operation or other trial. A real configured
head sensor is required. XYZ must already be homed; the trial does not home or
clear the bed mesh. Configured positions are validated against mechanical
`stepper_*` travel, including service positions beyond slicer limits. Feed values,
speeds, temperatures and iteration counts are checked before heat, motion or
native writes. The target temperature must allow extrusion within the heater's
configured limits. Unloading requires a configured active cutter switch,
initially released. The configuration is pinned for the operation.

| Observation | LOAD | UNLOAD |
| --- | --- | --- |
| Initial head/source | Head absent, no loaded source | Head present; `last_cmd` and saved filament source both match `TNN` |
| Fresh initial mode | IDLE `0` | IDLE `0` or PRINTING `2` |
| Fresh MATERIAL | Selected bit present | Selected bit present |
| Fresh CONNECTIONS | Entire mask zero | Exactly the selected bit |
| Fresh initial buffer | Empty `2` | A normal valid observation |
| Required final result | Head present, selected CONNECTIONS, mode `2`, buffer not full and matching source bookkeeping | Head absent, CONNECTIONS zero, mode `0`, empty buffer `2`, released cutter and cleared source bookkeeping |

The load command moves to the configured extrusion position and reuses the
existing complete transport phases: CFS feed, actual head arrival with the head
feed/stage-6 callback, final extruder/buffer feed, PRINT mode and source
bookkeeping. It also clears the normal `filament_useup` latch. The unload command
heats as required, performs configured sensed cutting, then uses the known-source
retraction path with recovery disabled and returns to the configured box
extrusion position. The `WITH_TNN` diagnostic route is not used because its
ignored returns cannot establish successful unloading. These commands cover
transport to the head and back, including cutting on unload. Purging, nozzle
cleaning, complete user macro cycles, tool changes and printing retain separate
acceptance cases.

Permission belongs to one operation context, lifecycle/binding/device generations
and the owning cooperative dispatch. Only an armed load's real head-arrival
callback receives temporary additional permission. Other head/slot/runout/error
callbacks, ordinary host commands and automatic startup paths remain gated.
Only the selected CFS and normal-route typed request bodies are admitted; raw
diagnostics and preload RUN remain blocked.

Every scoped reply must be fresh, normal and well formed before decoder effects.
Validation follows the command and feed stage: successful `0x10` stage 0 carries
one mode byte, stage 5 carries four wheel bytes, and stages 4/6/7 are empty.
An empty ACK is therefore not a substitute for every successful stage reply.
The first failure stops the trial before automatic loading recovery, another
sensed cutting attempt or unload recovery. Host CRC/LENGTH resends are disabled
and native requests pass `retries_en=False`. The cooperative operation budget is
900 seconds; own reads use at most two seconds, and other requests are bounded
by their native timeout and the remaining budget. These limits cannot interrupt
stock-library retries/waits, an MCU action or already queued Klipper motion.

After normal phase completion, the trial waits with `M400`, checks fresh final
observations and requests `M104 S0`. A failure does not automatically cut, stop,
unload or restore heat, fans, movement modes or position. Recorded source/data
changes are retained rather than replaced with stale cache values. Inspect the
actual state before deciding any further operation; a failed load may leave
material in the path without an accepted loaded-source result. Physical
observation remains required even when ACKs and final observations are normal.
