# Creality Filament System (CFS) Box for Klipper — Open Source Edition

English | [Русский](README.ru.md)

An open replacement for the stock Klipper `[box]` module on the Creality K1C
with the K1 Series CFS Upgrade. It retains the stock `serial_485` library,
CFS MCU firmware, touchscreen, Creality Print and user settings.

The scope covers the full `box` contract: initialization, loading, unloading,
cutting, tool changes, purging, print lifecycle, recovery, sensors, RFID and spool
data. Firmware **1.1.3** is the baseline; examined differences through 1.5.3 are
documented in the [firmware history](docs/compatibility.md#firmware-history).

The host implementation has PC tests. On the reference K1C, preload was verified
for all four slots, and two consecutive full T1C load/unload cycles completed
without manual assistance. Printing and additional firmware versions still
require hardware validation.

Startup is **passive by default**: identity and telemetry are available, while
ordinary mechanical actions are blocked. Explicit
[test commands](docs/installation.md#material-operation-trial) permit individual
operations without changing that default.

## Project layout

```text
src/cfs/        Extension implementation
klipper/box.py  Klipper entry point
tests/          PC tests
docs/           Architecture, protocol and compatibility
```

- [Installation and rollback](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Protocol](docs/protocol.md)
- [Compatibility](docs/compatibility.md)
- [Implementation plan](docs/implementation-plan.md)
- [Contract matrix](docs/contract-matrix.md)

<a id="quick-start"></a>

## Running tests

**Python 3.8** is the project default, set in [.python-version](.python-version).
Tests use standard-library `unittest` and test-only Jinja2 from
[requirements-test.txt](requirements-test.txt). The extension requires no package
build or installation.

Windows / PowerShell:

```powershell
py -3.8 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-test.txt
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Linux / macOS:

```sh
python3.8 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
```

[CI](.github/workflows/ci.yml) runs these checks on Python 3.8, including
installation and rollback in temporary directories. Tests do not operate a printer.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines. Bug reports
should include the commit, host/MCU versions and reproduction steps, with private
data removed.

## Support the project

<a href="https://nowpayments.io/donation?api_key=608dff38-32c9-4d19-8f85-0c8a46408f3f" target="_blank" rel="noreferrer noopener">
   <img src="https://nowpayments.io/images/embeds/donation-button-white.svg" alt="Cryptocurrency & Bitcoin donation button by NOWPayments">
</a>
