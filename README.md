# cfs_box

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
reply decoding and per-device version handling are implemented and PC-tested.
Mechanical CFS operations are pending. The entry point refuses activation before
accessing printer objects. No installer or printer-ready release is provided.

## Project layout

```text
src/cfs_box/     Extension implementation
klipper/box.py  Thin Klipper entry point
tests/          PC tests
docs/           Architecture, protocol and compatibility
```

Stage 1 produced the full contract matrix. Stage 2 added state, configuration,
storage and nine data-command handlers. Stage 3 adds calls through the retained
native API, validated replies, exact firmware profiles and twelve native-command
handlers, tested through Klipper substitutes. Lifecycle and operation handling
follow in later stages.

- [Architecture](docs/architecture.md): responsibilities and Klipper layout.
- [Protocol](docs/protocol.md): framing, native API boundary and open questions.
- [Compatibility](docs/compatibility.md): implementation and validation coverage.
- [Implementation plan](docs/implementation-plan.md): stages and acceptance criteria for the full `box` contract.
- [Contract matrix](docs/contract-matrix.md): complete inventory and bindings to planned PC/hardware checks, including existing G-code macros.

## Quick start

Tests run directly from the checkout using standard-library `unittest`, without
installing the extension or additional Python dependencies. Source targets Python
3.8 or newer. Native MIPS/printer compatibility remains unverified.

Windows / PowerShell:

```powershell
python -m unittest discover -s tests -v
```

Linux / macOS:

```sh
python3 -m unittest discover -s tests -v
```

Tests cover state snapshots and remapping, all 53 settings and 21 endpoint
fields, data commands, temporary-file persistence, native requests and replies,
mixed device versions and stale work, as well as bootstrap and matrix integrity.
They do not execute the stock native binary or operate hardware. See the
[coverage and deliberate storage differences](docs/compatibility.md#state-configuration-and-storage)
and [native integration limits](docs/protocol.md#native-integration).

## Development

The [CI workflow](.github/workflows/ci.yml) is configured to run the same tests
on Python 3.8 and 3.11. Installation and distribution builds are not part of CI.
See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

For bug reports, include the commit, environment, relevant host/MCU versions and
reproduction steps. Remove secrets, personal identifiers and private captures.

## Support the project

<a href="https://nowpayments.io/donation?api_key=608dff38-32c9-4d19-8f85-0c8a46408f3f" target="_blank" rel="noreferrer noopener">
   <img src="https://nowpayments.io/images/embeds/donation-button-white.svg" alt="Cryptocurrency & Bitcoin donation button by NOWPayments">
</a>
