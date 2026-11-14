"""Validate the deployed Klipper layout and bootstrap configuration errors."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def test_klipper_layout_imports_and_requires_bus_before_printer_access(self):
        with tempfile.TemporaryDirectory() as directory:
            extras = Path(directory) / "extras"
            extras.mkdir()
            (extras / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy2(ROOT / "klipper/box.py", extras / "box.py")
            shutil.copytree(
                ROOT / "src/cfs",
                extras / "cfs",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            result = subprocess.run(
                [sys.executable, "-c", """
from extras.box import load_config

class Config:
    error = RuntimeError

    def get(self, name, default=None):
        return default

    def get_printer(self):
        raise AssertionError("Missing bus must fail before printer lookup")

try:
    load_config(Config())
except RuntimeError as error:
    print(error)
else:
    raise AssertionError("The bootstrap must require a configured bus")
"""],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("set the serial_485 section name", result.stdout)


if __name__ == "__main__":
    unittest.main()
