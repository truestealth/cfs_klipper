# Installation and test commands

[Русский](installation.ru.md)

The installer copies the extension into an existing Klipper `extras` directory.
It leaves the stock `serial_485` library, MCU firmware, configuration, macros
and user data in place. Startup is passive; the test commands below grant one
explicit operation at a time.

## Installation and rollback

Run from the checkout with Python 3.8 or later. Stop Klipper before installing or
restoring files. Inspect the target's service wrapper before using it: its start
branch may copy factory configuration or restart an MCU service.

The extras directory and backup parent must exist. The backup path must be new
and separate from extras; `extras/box.py` must be a regular file and `extras/cfs`
must be absent. The default action previews changes:

```sh
python tools/install.py --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
python tools/install.py install --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
python tools/install.py restore --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
```

Installation backs up the original `box.py` and its entry caches, including
sibling `box.pyc` and `__pycache__/box.cpython-*.pyc`, with hashes, modes and
timestamps. It then copies the fixed Python source list and invalidates the
entry caches. Other extras, native libraries and data are unchanged.

Restore verifies the backup and installed sources, removes the owned package
and recognized generated caches, and restores the original entry/cache bytes
and metadata. Extra package entries, unknown caches, changed sources, links and
path traversal are rejected. Native entry points such as `box.so` are rejected
because they can shadow `box.py`; `box_wrapper.so` is retained.

New backups use manifest version 3. Version 2 modern-cache backups remain
supported; version 2 with a sibling `box.pyc` requires a separately verified
restore because its manifest cannot identify that cache's origin.

Keep the backup after restoration and verify the files before restarting.
The target must stay quiescent throughout: the installer does not lock out
other writers or provide a power-loss transaction. An installation error attempts
rollback; if restore itself fails, retain `box.py.original` for recovery.

<a id="passive-startup"></a>

## Passive startup

`load_config` obtains the data root from `extras.base_info.base_dir` and loads
`serial_485 <bus>` using `[box] bus`. It borrows that transport rather than opening
another connection. On `klippy:ready` it resolves the motion, print, sensor,
address-manager and cooperative-dispatch dependencies, emits `box:ready` and
starts heartbeat. The retained address-manager configuration must stay enabled.

The entry supplies first-generation `cfs0` context for addresses 1–4. Version
selection uses exact firmware profiles, with **1.1.3** as the baseline.

Passive admission allows typed identity and telemetry requests `0x02/03/05/08/0A/14`.
Ordinary mechanical commands, native writes and automatic preload are blocked;
there is no general mechanics switch. Automatic head/runout/error paths cannot
start motion or heating. Existing G-code outside Box keeps its own behavior:
`BOX_INFO_REFRESH` includes preload, and temperature wrappers can heat before
reaching a guarded Box command.

The spool cache remains at `<base_dir>/creality/userdata/box/tn_data.json` with
`material_database.json` beside it. The flushing marker remains at
`<base_dir>/creality/userdata/config/flushing_sign`. Fresh MATERIAL masks are read
before saved labels are applied, including reconnect. RFID/remaining reads and
explicit data commands can persist compatible cache updates; passive mode does
not mean read-only storage. `SAVE_BOX_CFG` retains the factory configuration paths.

<a id="single-operation-trial"></a>

## Preload

```gcode
CFS_TEST_PRELOAD TNN=T1A CONFIRM=1
```

`TNN` selects one physical slot, T1A–T4D. The command requires a connected cfs0
MCU **1.1.3**, encoding 1, ready/standby, no print, pause, active error or competing
operation, and a configured head sensor reporting no filament.

The sequence is fresh STATE (mode `0`), MATERIAL (selected slot present),
CONNECTIONS (zero), one selected `SET_PRE_LOADING` RUN, fresh STATE (mode `0`),
then selected-slot RFID and remaining length. Metadata is saved only after its
reply validates.

The cooperative budget is 300 seconds, with two-second reads and RUN bounded by
the remaining budget. Requests are not resent by the host. An ACK and IDLE
telemetry do not expose the MCU's separate preload-busy state, so observe the
material during the operation.

<a id="material-operation-trial"></a>

## Full loading and unloading

```gcode
CFS_TEST_LOAD TNN=T1A CONFIRM=1
CFS_TEST_UNLOAD TNN=T1A CONFIRM=1
```

These are separate invocations; one does not start the other. Each reads the
configured `BOX_LOAD_MATERIAL_WITH_MATERIAL` or `BOX_QUIT_MATERIAL` recipe,
requires the supported phase order and supplies physical `TNN` to its feed
phases. Existing macros are preserved. Custom recipes or extra arguments are
rejected by this admission.

Before a hardware check, home XYZ, inspect the actual position and simulate the
configured route. Verify that the bed and cutter path are clear. Cutter entry
requires a released switch and Y at or before the configured safe Y.

Both commands require one online cfs0 MCU **1.1.3**, encoding 1, ready/standby,
inactive virtual SD, and no pause/resume, active error, refill, drain or competing
operation. A real head sensor is required. Positions, travel, temperatures,
feed values and iteration counts are validated before heat, motion or native
writes. The configuration is pinned for the invocation.

| Observation | LOAD | UNLOAD |
| --- | --- | --- |
| Initial head/source | Head absent, no loaded source | Head present; source hints match `TNN` or both are absent |
| Fresh mode | IDLE `0` | IDLE `0` or PRINTING `2` |
| Fresh MATERIAL | Selected slot present | Selected slot present |
| Fresh CONNECTIONS | Zero | Exactly the selected slot |
| Fresh buffer | Neutral `0` or empty limit `2` | Valid normal observation |
| Final result | Head present, selected CONNECTIONS, mode `2`, buffer not full, source recorded | Head absent, CONNECTIONS zero, mode `0`, buffer `2`, cutter released, source absent |

LOAD performs the configured checks, extrusion positioning, cleaning, CFS loading,
extruder phase, flush and position restoration. Its owned flush supplies
`LEN=int(cfg.Tn_extrude)`: 150 mm in the reference profile. The length must be
integral in 1–200 mm, with finite positive velocity/percent pairs and effective
feed at most 200 mm. Actual speed and extrusion factors must equal 1.
A failed feed stops before further cleaning.

Known-source UNLOAD performs the configured checks and sensed cut, then BUFFER
retraction and blocking head E−20 assistance. It submits the final E−30 at F600
(nominally three seconds) before issuing one selected MATERIAL request. A normal
MATERIAL ACK is followed by `M400` before source cleanup and return positioning.
This allows head withdrawal during the CFS request without requiring identical
physical motor start times.

Unknown-source UNLOAD leaves both source hints absent. `TNN` identifies only the
single lane observed in fresh CONNECTIONS and MATERIAL. Its registered retraction
phase admits fresh CONNECTIONS, MATERIAL, STATE, IDLE, selector-zero BUFFER and
selected-lane MATERIAL in order. It retains the configured cold cut/heat sequence
and does not substitute an old source hint.

## CFS-only retraction

<a id="native-buffer-trial"></a>

```gcode
CFS_TEST_RETRACT TNN=T1C CONFIRM=1
CFS_TEST_RETRACT TNN=T1C TRIGGER=MATERIAL CONFIRM=1
```

The default trigger is BUFFER; MATERIAL is an explicit separate choice. These
commands send no XYZ/E movement, heating or cutting and require no homing.
They retain the same device/idle/ownership checks. Source hints must either
both be absent or both match `TNN`; fresh CONNECTIONS must name exactly that
lane and MATERIAL must include it.

The sequence is four fresh observations (STATE, MATERIAL, CONNECTIONS, BUFFER),
IDLE, fresh normal STATE, one selected retraction, then the four observations
again. Only the initial STATE may carry `0x13` or `0x19` with four data bytes and
mode `4`; later replies must be normal.

BUFFER uses a 60-second cooperative budget and a ten-second request cap.
Its final mode must be `0`, buffer `2`, and CONNECTIONS zero or the selected lane;
the head reading is reported without requiring absence. MATERIAL uses a
240-second budget and a 150-second request cap; final CONNECTIONS must be zero
and the head absent. Both require the selected material to remain present.
Neither assigns or clears a loaded source.

## Execution and completion

`CONFIRM=1` marks an explicit invocation. Permission belongs to the current
dispatch, operation, lifecycle, binding and device generation; it is revoked
when the invocation ends. Ordinary commands and unrelated callbacks cannot
inherit it. Full LOAD/UNLOAD temporarily admits only its registered phases and
the armed load's real head-arrival callback.

Every response must be fresh, normal and valid for its opcode/stage before
decoder effects. A phase error stops advancement without automatic retry or
recovery motion. Full LOAD/UNLOAD has a 900-second cooperative budget and
two-second own reads. These deadlines cannot interrupt a stock-library wait,
an MCU action or already submitted motion.

Command return does not guarantee an empty Klipper queue: the configured LOAD
restore tail can leave Z moving. Wait for actual motion to settle before the
next operation. The recipes do not add an automatic heater-off command; switch
off unused heating separately. On failure, inspect the actual material, position
and heater state before another command.

<a id="reference-trial-results"></a>

## Reference validation

On K1C CFS Upgrade with cfs0 MCU 1.1.3 and printer Python 3.8.2, passive startup
and file/cache rollback were verified. Preload was checked on all four slots,
with fresh RFID/remaining data and correct spool display on the touchscreen and
Creality Print. Two consecutive complete T1C load/unload cycles produced normal
nozzle flow and filament return without manual assistance.

Printing, controlled fault/recovery cases and other MCU versions remain in the
[hardware validation plan](implementation-plan.md).
