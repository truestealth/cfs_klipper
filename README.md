# Creality Filament System (CFS) Box for Klipper — Open Source Edition

English | [Русский](README.ru.md)

An open replacement for the stock Klipper `[box]` module on the Creality K1C
with the K1 Series CFS Upgrade. Preserve the CFS MCU firmware, touchscreen,
Creality Print, Fluidd status, slicer mapping and user settings.

The existing host-side `serial_485` library remains in use. The scope covers the
full stock `box` contract: initialization, loading, unloading, cutting, tool
changes and purging, pause/resume, errors/recovery, sensors, RFID and spool data.

The baseline is CFS MCU firmware **1.1.3** on the reference K1C. The
[firmware change history](docs/compatibility.md#firmware-history) records the
examined differences through 1.5.3 and their compatibility implications.

**Status:** state, configuration, compatible storage, native-call integration,
reply decoding, per-device versions, lifecycle, operation context and common
error handling are implemented and PC-tested.
Configured motion, heating, cutting, cleaning and purge components have
synthetic PC checks. Complete loading, tool changes, recovery and hardware
acceptance remain pending. The entry point refuses activation before accessing
printer objects. No installer or printer-ready release is provided.

## Project layout

```text
src/cfs/        Extension implementation
klipper/box.py  Thin Klipper entry point
tests/          PC tests
docs/           Architecture, protocol and compatibility
```

Stage 1 produced the full contract matrix. Stage 2 added state, configuration,
storage and nine data-command handlers. Stage 3 added calls through the retained
native API, validated replies, exact firmware profiles and twelve native-command
handlers. Stage 4 adds explicit ready/shutdown wiring, reconnect, heartbeat,
guarded operation waits and shared error reporting. Stage 5 adds configured
host motion, fans, heating, extrusion, cutting, cleaning and calibration
handlers. Explicit substitute-based registration comprises 46 handlers, plus
two Hall handlers when a cut switch is configured. Full loading, tool changes
and recovery follow in later stages.

- [Architecture](docs/architecture.md): responsibilities and Klipper layout.
- [Protocol](docs/protocol.md): framing, native API boundary and open questions.
- [Compatibility](docs/compatibility.md): implementation and validation coverage.
- [Implementation plan](docs/implementation-plan.md): stages and acceptance criteria for the full `box` contract.
- [Contract matrix](docs/contract-matrix.md): complete inventory and bindings to planned PC/hardware checks, including existing G-code macros.

## Quick start

Tests run directly from the checkout using standard-library `unittest`, without
installing the extension or additional Python dependencies. **Python 3.8 is the
project default**, recorded in [.python-version](.python-version). Create a local
environment with CPython 3.8 before running the tests. Native MIPS/printer
compatibility remains unverified.

Windows / PowerShell:

```powershell
py -3.8 -m venv .venv
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Linux / macOS:

```sh
python3.8 -m venv .venv
.venv/bin/python -m unittest discover -s tests -v
```

An existing Python 3.8 `.venv` can be reused. With `uv` installed, the environment
creation command can instead be `uv venv --python 3.8 .venv`.

Tests cover state snapshots and remapping, all 53 settings and 21 endpoint
fields, data commands, temporary-file persistence, native requests and replies,
mixed device versions and stale work, as well as bootstrap and matrix integrity.
Lifecycle cases cover targeted heartbeat, shutdown/restart, pause/reporting and
head-event re-entry while a native request is waiting. Motion and extrusion
cases use synthetic coordinates, fan pins, heaters and wheel samples; cutting
cases distinguish a fresh switch event from a cached value and exercise blind
cutting. These tests do not execute the stock native binary or operate hardware. See the
[coverage and deliberate storage differences](docs/compatibility.md#state-configuration-and-storage)
and [host-action policies](docs/compatibility.md#host-actions), as well as
[lifecycle integration limits](docs/protocol.md#lifecycle-integration).

## Development

The [CI workflow](.github/workflows/ci.yml) is configured to run the same tests
on Python 3.8, using the same version file. Installation and distribution builds
are not part of CI.
See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

For bug reports, include the commit, environment, relevant host/MCU versions and
reproduction steps. Remove secrets, personal identifiers and private captures.

## Support the project

<a href="https://nowpayments.io/donation?api_key=608dff38-32c9-4d19-8f85-0c8a46408f3f" target="_blank" rel="noreferrer noopener">
   <img src="https://nowpayments.io/images/embeds/donation-button-white.svg" alt="Cryptocurrency & Bitcoin donation button by NOWPayments">
</a>
