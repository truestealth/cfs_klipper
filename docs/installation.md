# Local installation preparation

[Русский](installation.ru.md)

This PC-tested tool copies the extension to a local Klipper `extras`
directory. The bootstrap initializes passive admission with mechanics blocked.
Component tests do not establish printer readiness; a future printer trial requires separate
authorization and hardware acceptance. This tool does not contact a printer,
manage services, or edit configuration, macros, firmware or data.

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
a hash manifest, then copies only the fixed Python file list. Matching CPython
`extras/__pycache__/box.cpython-*.pyc` files are backed up and invalidated before
switching source; restore removes generated matching caches and restores the
original caches with their bytes, mode and timestamps. Other cache entries are
left alone. Existing
`box*.so`, `serial*.so`, other extras and user data are not modified. Restore
checks the backup and every installed file and refuses if owned files changed or
the `cfs` directory contains additional entries, except for normal CPython
bytecode files for the installed modules in `cfs/__pycache__`, which are removed.
Unknown cache entries and links are rejected. It verifies the restored bytes
and retains the backup. Keep the backup safe: hashes detect accidental changes,
but do not authenticate a deliberately altered manifest.

An installation error attempts to roll back changes already made; the backup
directory remains for inspection. A restore error can require manual recovery
from `box.py.original`; do not discard it. The target must remain quiescent
throughout either operation: the tool does not lock out concurrent writers or
survive power loss as a transaction.

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

After identification, each passive initialization reads a fresh MATERIAL presence
mask before applying saved labels to live state, including reconnect and addresses
already marked connected by the retained address manager. Passive diagnostics use
`Lifecycle.last_error`, leaving the retained virtual-SD pause hint `box.last_err`
unchanged.

The spool cache stays at `<base_dir>/creality/userdata/box/tn_data.json`, with
`material_database.json` beside it; the flushing marker stays at
`<base_dir>/creality/userdata/config/flushing_sign`. Existing valid cache data
is preserved and malformed data raises an error. A missing file or `base_data`
creates the compatible empty cache, so passive admission does not mean a
read-only filesystem. Explicit data/configuration commands can also save data.
`SAVE_BOX_CFG` retains the factory paths `/usr/data/printer_data/config/box.cfg`
and `/mnt/UDISK/printer_data/config/box.cfg`; the data root does not redirect them.

All startup and failure checks so far use PC substitutes and temporary paths.
The live configuration fingerprint and hardware acceptance remain pending;
no printer was contacted or changed for this work.
