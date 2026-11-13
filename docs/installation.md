# Local installation preparation

[Русский](installation.ru.md)

This PC-tested tool copies the development scaffold to a local Klipper `extras`
directory. **The bootstrap still refuses activation.** Component tests do not
establish printer readiness; a future printer trial requires separate
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
