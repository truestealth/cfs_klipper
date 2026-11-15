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

Admission permits only typed `GET_RFID` (`0x02`), `GET_REMAIN_LEN` (`0x03`),
`GET_BUFFER_STATE` (`0x05`), `GET_FILAMENT_SENSOR_STATE` (`0x08`), `GET_BOX_STATE`
(`0x0A`) and `GET_VERSION_SN` (`0x14`). Wheel requests, raw diagnostics,
`CREATE_CONNECT`, motor/preload writes and host actions are rejected. Incoming
error/runout/head events cannot trigger pause, motion restoration, heating,
tightening or sensor-enable G-code. Automatic preload permission and power-on
RUN are suppressed. Existing macros can encounter these explicit admission
errors; this mode is not suitable for printing. There is currently no
configuration or G-code option to enable mechanics.

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
rollback; a stock-runtime restart and mechanical acceptance remain untested.

After direct RFID/remaining reads, the touchscreen and Creality Print recognized
CFS but showed all four slots as grey or undefined. The changed cache was backed
up before the exact pre-query spool cache was restored with Klipper stopped.
A passive start of the same sources restored slot display in both interfaces,
as confirmed by the user. No decoder/runtime fix or repeated RFID/remaining
query was involved. This verifies cached display, not fresh physical RFID
acquisition; RFID and remaining values were restored together.
