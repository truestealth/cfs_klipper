# Contributing

English | [Русский](CONTRIBUTING.ru.md)

1. Set up the environment using the [quick start](README.md#quick-start).
2. Create a branch for a focused change.
3. Add a small regression test for a bug fix or a test for new behavior.
4. Run the checks from the project root in the activated Python 3.8 environment:

   ```sh
   python -m unittest discover -s tests -v
   ```

5. Update affected English and Russian documentation together.
6. Open a pull request explaining the problem, the change, and how it was checked.

Source code belongs in `src/cfs/`, the Klipper entry point in `klipper/`,
and tests in `tests/`. Tests use standard-library `unittest`; test filenames start
with `test_`. Run tests directly from the checkout without package installation.
Use docstrings for non-obvious behavior and comments to explain implementation
decisions. Keep changes small and avoid unrelated refactoring.

Read the [architecture](docs/architecture.md), [protocol](docs/protocol.md), and
[compatibility coverage](docs/compatibility.md) before implementing CFS behavior.
Python 3.8 is the development and CI default, pinned in [.python-version](.python-version).
Keep the existing `serial_485` transport;
validate the exact native API path instead of opening another serial connection.
The current bootstrap intentionally refuses activation.

Add loading and native-call tests with the corresponding implementations. Cover
missing observations, negative replies and timeout paths, not only acknowledgements.
PC tests use synthetic data and do not access equipment. Do not treat a PC result
as hardware acceptance. Add modules and synthetic fixtures when verified behavior
needs them; do not add placeholder modules or an application packaging layer.

Use synthetic test data. Do not include proprietary binaries, raw analysis output,
credentials, personal information, or machine-specific configuration in a PR.
Clearly distinguish tested compatibility from assumptions and unsupported behavior.
