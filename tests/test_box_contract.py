"""Validate the planned Klipper layout and refusal to activate the scaffold."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def test_klipper_layout_refuses_activation_without_printer_access(self):
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

    def get_printer(self):
        raise AssertionError("The scaffold must not obtain printer objects")

try:
    load_config(Config())
except RuntimeError as error:
    print(error)
else:
    raise AssertionError("The scaffold must refuse activation")
"""],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("development scaffold", result.stdout)
        self.assertIn("printer operation is not implemented", result.stdout)


if __name__ == "__main__":
    unittest.main()
