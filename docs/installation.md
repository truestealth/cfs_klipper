# Installation and test commands

[Русский](installation.ru.md)

The installer replaces the Klipper Box entry and adds the CFS extension. It
preserves stock `serial_485.so`, MCU firmware, configuration, macros and user data.
Normal CFS operation is enabled by default.

## Installation and rollback

### Managed deployment on the printer

Run on the Linux Klipper host from the extracted release with Python 3.8 or
later. Finish standalone motion and filament operations before running it.
Keep the state directory on persistent storage and reuse it for updates and
rollback.

```sh
extras=/usr/share/klipper/klippy/extras
cfs_state=/usr/data/cfs-deployment
python3 tools/deploy.py install --extras "$extras" --state-dir "$cfs_state"
python3 tools/deploy.py update --extras "$extras" --state-dir "$cfs_state"
python3 tools/deploy.py rollback --extras "$extras" --state-dir "$cfs_state"
```

Run `update` from the release you want to deploy. To adopt an existing
installation, also pass `--previous-release` with the matching extracted
release and `--previous-backup` with its original v3 Box backup. The manager
accepts the verified stock K1C entry and an unchanged set of managed module
paths. It checks for active, paused and queued jobs and heater targets, then
records the running Klipper's launch parameters and verifies readiness after
replacing files. Add `--check` to an action to validate its prerequisites
without stopping Klipper. A failed activation triggers recovery of the previous
installation. An interruption can leave `pending.json` and a snapshot in the
state directory. Keep them for recovery and inspect the recorded phase before
another deployment.

Factory rollback restores the Box entry and its original bytecode. If an
active macro calls `CFS_PRINT_START_GUARD` or another extension-only command,
restore a stock-compatible macro before running `rollback`. The manager checks
this dependency before stopping Klipper.

### File-only installer

Run from the checkout or extracted package with Python 3.8 or later. Stop
Klipper before installing or restoring files. Check the target's service wrapper
before using its start branch: it may copy factory configuration or restart an
MCU service.

The extras directory and backup parent must exist. Choose a new backup path
outside extras. The installer requires a regular `extras/box.py` and an absent
`extras/cfs`. When updating an installed build, restore it first with its matching
installer and backup. The first command previews the changes.

```sh
python tools/install.py --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
python tools/install.py install --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
python tools/install.py restore --extras /path/to/klipper/klippy/extras --backup /path/to/new-backup
```

Installation backs up `box.py` and its entry caches (`box.pyc` and
`__pycache__/box.cpython-*.pyc`) with hashes, modes and timestamps. Restore
verifies the backup and installed sources, removes the extension and its
recognized generated caches, and restores the original files and metadata.
Keep the backup. A native entry such as `box.so` can shadow `box.py` and is
rejected. The installer retains `box_wrapper.so`.

New backups use manifest version 3. Version 2 backups with a sibling `box.pyc`
need separate verification before restore. The installer assumes no other
writers. If its rollback fails, retain `box.py.original` for recovery.

<a id="cfs-firmware-maintenance"></a>

## CFS firmware maintenance

The firmware tool handles the first-generation `cfs0` G32 controller. It
checks known original images and delegates RS-485 transfers to a verified
copy of the external loader supplied by the operator. Keep both the current
and target images. The default command verifies the plan on disk.

```sh
python3 tools/cfs_firmware.py update /path/to/current.bin /path/to/target.bin
python3 tools/cfs_firmware.py downgrade /path/to/current.bin /path/to/target.bin
```

For hardware operations, use Linux as root with exclusive access to the serial
port. Stop Klipper and physically connect only the intended CFS. Pass `--apply`,
`--device`, `--backend`, `--serial` and `--isolated` to start the device checks.
This enters the bootloader before the `APPLY` and `YES` confirmations used for
writing. Downgrade also requires `--ack-untested-downgrade`. After power-cycling
the CFS, run `verify TARGET.bin` with the same hardware options except
`--apply` and the downgrade acknowledgment. Confirm the live version before
using the CFS again. Keep the images and the loader matching `BACKEND_SHA256`
outside the source checkout. The wrapper leaves the printer-side `version.json`
alone and reports the firmware from the CFS response. The wrapper has PC tests
and the physical downgrade remains unverified.

<a id="passive-startup"></a>

## Normal and passive startup

Keep the stock address manager enabled and set `[box] bus` to the existing
`serial_485` section name. The extension uses exact firmware profiles for
first-generation `cfs0` boxes, with **1.1.3** as the baseline.

In normal mode the slicer sends CFS mapping and enable commands, then runs
ordinary tool changes in the virtual-SD job through `END_PRINT`. Where the
retained `START_PRINT` uses `CFS_PRINT_START_GUARD`, the guard checks the first
tool in that file and its mapped CFS.

If a failed tool load pauses the job, clearing the error retains the target
slot for that job and file position. Replace the filament and choose **Retry**.
An empty slot, a missing head-sensor confirmation or a changed job keeps the
print from resuming.

Set `passive: true` in `[box]` to use the diagnostic commands below. Passive
mode allows identity and telemetry requests `0x02/03/05/08/0A/14` and blocks
ordinary Box motion, native writes and automatic preload. External macros keep
their behavior and may heat or move before reaching a guarded Box command.

Data paths still use `extras.base_info.base_dir` as `<base_dir>`. Spool
metadata is in `<base_dir>/creality/userdata/box/tn_data.json` and
`material_database.json`. The flushing marker is in
`<base_dir>/creality/userdata/config/flushing_sign`. Telemetry and data
commands can update the metadata cache in passive mode.

<a id="single-operation-trial"></a>

## Preload

```gcode
CFS_TEST_PRELOAD TNN=T1A CONFIRM=1
```

Select a physical slot T1A–T4D. This trial requires a connected cfs0 **1.1.3**,
encoding 1, standby with an empty head, and no active job, error or competing
operation. Fresh STATE, MATERIAL and CONNECTIONS must show IDLE, the selected
filament present and no connected lane. The command then requests one preload
RUN and reads IDLE, RFID and remaining length. Its cooperative budget is 300
seconds, with two-second reads. Observe the material: IDLE and an ACK do not
report the MCU's separate preload-busy state.

<a id="material-operation-trial"></a>

## Full loading and unloading

```gcode
CFS_TEST_LOAD TNN=T1A CONFIRM=1
CFS_TEST_UNLOAD TNN=T1A CONFIRM=1
```

Run these separately in standby on one connected cfs0 **1.1.3**, encoding 1,
with no job, error, refill, drain or competing operation. They use the configured
load and quit recipes and pass the physical slot to the feed phases. The
trial checks the recipe and motion limits before acting. It requires a real
head sensor, homed XYZ and a verified route. Cutter entry requires a released
switch and Y at or before the configured safe Y.

| Observation | LOAD | UNLOAD |
| --- | --- | --- |
| Initial head/source | Head empty, no loaded source | Head present, both source hints match `TNN` or both are absent |
| Fresh mode | IDLE `0` | IDLE `0` or PRINTING `2` |
| Fresh MATERIAL | Selected slot present | Selected slot present |
| Fresh CONNECTIONS | Zero | Exactly the selected slot |
| Fresh buffer | Neutral `0` or empty limit `2` | Valid normal observation |
| Final result | Head present, selected CONNECTIONS, mode `2`, buffer not full | Head empty, CONNECTIONS zero, mode `0`, buffer `2`, cutter released |

LOAD uses the configured `Tn_extrude` for its flush (150 mm in the reference
profile). The configured length must be integral in 1–200 mm and effective
feed at most 200 mm, with finite positive velocity/percent pairs and speed and
extrusion factors of 1.

With a known source, UNLOAD cuts, requests BUFFER retraction, withdraws E−20
with the head and submits E−30 at F600 before one MATERIAL request. A normal
reply is followed by `M400` and fresh clearance checks. With no source hints,
the trial uses `TNN` to check the single detected lane, then uses selector-zero
BUFFER and selected-lane MATERIAL while preserving the absent hints.

These trials have a 900-second cooperative budget and two-second reads. A
failed phase stops before later phases or automatic retries.

<a id="tool-change-trial"></a>

## Full tool change

```gcode
CFS_TEST_TOOL_CHANGE TOOL=T2 CONFIRM=1
```

Select a mapped logical or physical T-command. The 900-second trial requires a
known loaded source and a different target on the same connected cfs0 **1.1.3**,
encoding 1. It checks standby, homed XYZ, safe Y, current head/source agreement,
target filament and the calculated purge before cutting. It checks fresh
observations between unload, load and purge.

The change and print trials cap the main purge and E−20 assist at F360
(6 mm/s). The E−30 tail and short cleaning pulses use their own speeds. In
normal operation, the Box-side change purge on baseline 1.1.3 also uses a
6 mm/s ceiling. Sensed cuts in the trials wait up to five seconds for the
current switch. A timeout stops the trial after that stroke.

<a id="print-trial"></a>

## One reviewed print file in passive mode

```gcode
CFS_TEST_PRINT_ARM FILENAME=<file> SHA256=<sha256> TOOLS=T1A,T1C CONFIRM=1
CFS_TEST_PRINT_CONTROL ACTION=<action> CONFIRM=1
CFS_TEST_PRINT_DISARM CONFIRM=1
```

Use an existing virtual-SD filename, its lowercase SHA-256 and two distinct
physical slots on one connected cfs0 **1.1.3**, encoding 1. The file must contain
one `START_PRINT`, one `END_PRINT`, and tools mapped to both slots. Limits are
2 MiB, eight tool actions including startup and a 30-minute cooperative budget.
Arming checks the file and device. Start the file separately.

For this trial, the retained `START_PRINT` must call
`CFS_PRINT_START_GUARD INITIAL_TOOL=<index-or-minus-one>` before its preparation
steps and `BOX_START_PRINT_EXTRUDE_MATERIAL` after heating. The guard uses
the first T-command in the armed file. An explicit index must match. The installer
does not change macros. Startup checks fresh CFS sensors and the head against
the recorded source.

Control actions are `PAUSE`, `RESUME`, `CANCEL_PRINT`, `BOX_TNN_RETRY_PROCESS`
and `BOX_ERROR_CLEAR`. Resume and retry require the owned job to be paused.
Controlled pause requires homed XYZ, safe Y and no active tool change.
Disarming stops future lines. Configuration, device identity, mappings, file
and metadata changes or an abnormal reply revoke the trial. Genuine `0x50`
retains its runout meaning.

<a id="native-buffer-trial"></a>

## CFS-only retraction

```gcode
CFS_TEST_RETRACT TNN=T1C CONFIRM=1
CFS_TEST_RETRACT TNN=T1C TRIGGER=MATERIAL CONFIRM=1
```

With `HEAD_ASSIST=0` (default), these commands require no homing and use only
the CFS motors. Both source hints must match `TNN` or be absent. Fresh
CONNECTIONS must show exactly the selected lane and MATERIAL must include it.
The first STATE may carry `0x13` or `0x19` with four data bytes and mode `4`.
Subsequent replies must be normal. Both triggers require IDLE and empty-limit
buffer `2` at the end, while the selected filament remains present.

| Trigger | Cooperative budget | Request cap | Final path |
| --- | --- | --- | --- |
| BUFFER (default) | 60 s | 10 s | CONNECTIONS zero or selected lane, with head state reported |
| MATERIAL | 240 s | 150 s | CONNECTIONS zero, head empty |

For already cut filament, add `HEAD_ASSIST=1` only with `TRIGGER=MATERIAL`.
This requires filament at the head, a pressed active cutter switch, a hot
extruder and unit speed and extrusion factors. Fresh IDLE, selected
CONNECTIONS and buffer `2` after BUFFER retraction authorize the E-only assist:
blocking E−20 at F360, then E−30 at F600 before MATERIAL and `M400` after a
normal reply. The trial preserves source hints and never finishes the print.

## Execution and completion

`CONFIRM=1` explicitly authorizes the named test and its own callbacks. The
cooperative budgets do not interrupt a stock-library wait, an MCU action or
queued motion. Before another command, confirm the head and bed have stopped.
The recipes may leave heaters on. After a failure, check the actual material,
position and heater state.

<a id="reference-trial-results"></a>
<a id="reference-printer-evidence"></a>

## Reference validation

On a K1C CFS Upgrade with first-generation CFS firmware **1.1.3**, printer checks
covered preload on A–D, two T1C load/unload cycles, C→A→C and two-colour
printing including a pause/resume cycle. A three-colour OrcaSlicer job
(`T0 → T2 → T3`) finished after the operator reloaded and resumed following a
broken filament.

Local tests cover the paused-tool retry and the head-sensor check before
resume. The build passed startup on the printer. Automatic recovery from a
broken filament still needs a hardware test. See the
[hardware validation plan](implementation-plan.md) for the remaining checks.
