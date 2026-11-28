# Creality Filament System (CFS) Box for Klipper — Open Source Edition

English | [Русский](README.ru.md)

> [!WARNING]
> This software is provided **as is**, without any express or implied warranty.
> You install and use it at your own risk. To the extent permitted by applicable
> law, the authors and contributors accept no liability for claims, losses or
> damage arising from installation, software errors, use or inability to use it.

An open replacement for the stock Klipper `[box]` module on the Creality K1C
with the K1 Series CFS Upgrade. It retains the stock `serial_485` library,
CFS MCU firmware, touchscreen, Creality Print and user settings.

The scope covers the full `box` contract: initialization, loading, unloading,
cutting, tool changes, purging, print lifecycle, recovery, sensors, RFID and spool
data. Firmware **1.1.3** is the baseline; examined differences through 1.5.3 are
documented in the [firmware history](docs/compatibility.md#firmware-history).

The reference K1C completed four-slot preload, full load/unload cycles, C→A→C
tool changes, and two- and three-colour prints on MCU **1.1.3**. Pause/resume
was exercised during printing. The three-colour OrcaSlicer print finished after
operator recovery. Automated **Retry** after a break is covered by PC tests.
See the [reference printer results](docs/installation.md#reference-printer-evidence).

The standard entry point accepts ordinary slicer and printer commands through
Klipper virtual SD. Set `passive: true` in `[box]` for telemetry and
[scoped diagnostic commands](docs/installation.md#material-operation-trial).

## Install on the K1C

Run over SSH as root on the K1C with the CFS Upgrade. Finish any active print,
manual motion or filament operation first.

```sh
cd /usr/data
wget -O cfs-install.sh https://raw.githubusercontent.com/truestealth/cfs_klipper/main/install.sh && sh cfs-install.sh
```

The launcher downloads the current `main` archive, backs up the stock Box and
restarts Klipper through the deployment tool. It needs HTTPS access to GitHub,
`wget`, `tar` and Python 3.8 or later. The installation state and backups are
kept in `/usr/data/cfs-deployment`.

For updates and rollback, run `sh cfs-install.sh update` or
`sh cfs-install.sh rollback`. Add `--check` to an
action, for example `sh cfs-install.sh install --check`, to check prerequisites
before stopping Klipper. For a previous manual installation or a modified
`START_PRINT`, see the [installation and rollback guide](docs/installation.md).

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
