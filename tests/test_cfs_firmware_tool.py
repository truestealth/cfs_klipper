import contextlib
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import cfs_firmware as firmware


def digest(data):
    return hashlib.sha256(data).hexdigest()


class CfsFirmwareToolTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.original = directory / "cfs0_050_G32-cfs0_000_113.bin"
        self.target = directory / "cfs0_050_G32-cfs0_000_153.bin"
        self.backend = directory / "test-backend.py"
        self.device = directory / "fake-device"
        originals = {self.original: b"test image 113", self.target: b"test image 153",
                     self.backend: b"test backend", self.device: b""}
        for path, data in originals.items():
            path.write_bytes(data)
        self.serial = "TEST-DEVICE"
        images = patch.object(firmware, "IMAGE_SHA256", {
            "113": digest(originals[self.original]), "153": digest(originals[self.target]),
        })
        backend = patch.object(firmware, "BACKEND_SHA256", digest(originals[self.backend]))
        for override in (images, backend):
            override.start()
            self.addCleanup(override.stop)
        bus = patch.object(firmware, "bus_lock", side_effect=lambda device: contextlib.nullcontext())
        bus.start()
        self.addCleanup(bus.stop)

    def cli(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = firmware.main(list(map(str, args)))
        return code, stream.getvalue()

    def hardware(self):
        return ["--device", self.device, "--backend", self.backend,
                "--serial", self.serial, "--isolated"]

    def test_offline_plan_validates_both_images_and_directions(self):
        with patch.object(firmware, "run_backend") as backend:
            code, output = self.cli("update", self.original, self.target)
            self.assertEqual(code, 0)
            self.assertIn("1.1.3 -> 1.5.3", output)
            self.assertEqual(self.cli("downgrade", self.target, self.original)[0], 0)
            self.assertIn("direction", self.cli("update", self.target, self.original)[1])
            self.original.write_bytes(b"untrusted rollback")
            self.assertIn("SHA-256 mismatch", self.cli("update", self.original, self.target)[1])
            backend.assert_not_called()

    def test_apply_checks_direction_related_acknowledgement_before_hardware(self):
        with patch.object(firmware, "run_backend") as backend:
            code, output = self.cli("downgrade", self.target, self.original,
                                    "--apply", *self.hardware())
            self.assertEqual(code, 1)
            self.assertIn("--ack-untested-downgrade", output)
            self.backend.write_bytes(b"untrusted backend")
            code, output = self.cli("update", self.original, self.target, "--apply", *self.hardware())
            self.assertEqual(code, 1)
            self.assertIn("SHA-256 mismatch", output)
            backend.assert_not_called()

    def test_update_dry_run_and_identity_checks_precede_interactive_apply(self):
        diag = "  Firmware      : 1.1.3\n  Serial number : %s\n" % self.serial
        plan = "  -> current version: 'cfs0_050_G32-cfs0_000_113'\nPLAN: 1.1.3  ->  1.5.3   (test)\n"
        with patch.object(firmware, "run_backend", side_effect=[
                diag, plan + "DRY RUN complete. NOTHING was written.", diag,
                plan + "=== FLASH END ===\n",
        ]) as backend, patch.object(firmware.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="APPLY"):
            code, output = self.cli("update", self.original, self.target, "--apply", *self.hardware())
            self.assertEqual(code, 0, output)
            self.assertIn("then run verify", output)
            calls = backend.call_args_list
            self.assertEqual([call.args[2] for call in calls], ["diag", "flash", "diag", "flash"])
            self.assertEqual(calls[1].args[-1].name, self.target.name)
            self.assertEqual(calls[-1].args[-1], "--flash")
            self.assertEqual(calls[-1].kwargs, {"expected": ("113", "153", self.target.name)})

    def test_backend_failure_with_zero_exit_never_counts_as_a_success(self):
        diag = "  Firmware      : 1.1.3\n  Serial number : %s\n" % self.serial
        with patch.object(firmware, "run_backend", side_effect=[diag, "ABORT: no sector size."]) as backend, \
                patch.object(firmware.sys.stdin, "isatty", return_value=True), patch("builtins.input") as confirm:
            code, output = self.cli("update", self.original, self.target, "--apply", *self.hardware())
            self.assertEqual(code, 1)
            self.assertIn("expected completion report", output)
            self.assertEqual(backend.call_count, 2)
            confirm.assert_not_called()

    def test_verify_requires_expected_application_and_serial(self):
        diag = "  Firmware      : 1.5.3\n  Serial number : %s\n" % self.serial
        with patch.object(firmware, "run_backend", side_effect=[diag, diag.replace(self.serial, "wrong")]):
            code, output = self.cli("verify", self.target, *self.hardware())
            self.assertEqual(code, 0, output)
            self.assertIn("verified on the expected serial", output)
            self.assertEqual(self.cli("verify", self.target, *self.hardware())[0], 1)

    def test_backend_invocation_disables_vendor_file_edits(self):
        with patch.object(firmware, "check_bus"), patch.object(firmware.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b"diagnostics"
            with contextlib.redirect_stdout(io.StringIO()):
                firmware.run_backend(self.backend, self.device, "diag")
            self.assertEqual(run.call_args.args[0][-4:], ["diag", "--no-json", "--dev", str(self.device)])

    def exercise_backend_prompt(self, observed, reply, plan_target="1.5.3"):
        self.backend.write_text(
            "import sys\n"
            "print(\"  -> current version: %r\")\n" % ("cfs0_050_G32-cfs0_000_" + observed) +
            "print('PLAN: 1.1.3  ->  %s   (test)')\n" % plan_target +
            "print(\"Erase and write %s now? Type YES to flash: \", end='', flush=True)\n" % self.target.name +
            "print('Received on stdin: ' + repr(sys.stdin.readline()))\n",
            encoding="utf-8",
        )
        output = io.StringIO()
        failure = None
        with patch.object(firmware, "check_bus"), contextlib.redirect_stdout(output), \
                patch("builtins.input", return_value=reply) as question:
            try:
                firmware.run_backend(self.backend, self.device, "flash", self.target,
                                     "--flash", expected=("113", "153", self.target.name))
            except ValueError as error:
                failure = str(error)
        return output.getvalue(), failure, question.call_count

    def test_wrong_actual_loader_or_plan_declines_even_after_valid_dry_run(self):
        firmware.check_transfer("  -> current version: 'cfs0_050_G32-cfs0_000_113'\n"
                                "PLAN: 1.1.3  ->  1.5.3   (test)\n"
                                "DRY RUN complete. NOTHING was written.", "113", "153", dry_run=True)
        for version, target in [("142", "1.5.3"), ("113", "1.5.2")]:
            with self.subTest(version=version, target=target):
                output, failure, asks = self.exercise_backend_prompt(version, "YES", target)
                self.assertIn("expected loader and transfer plan", failure)
                self.assertIn("Received on stdin: ''", output)
                self.assertEqual(asks, 0)

    def test_verified_prompt_forwards_only_exact_user_confirmation(self):
        for reply, accepted in [("YES", True), ("yes", False), ("YES ", False)]:
            with self.subTest(reply=reply):
                output, failure, asks = self.exercise_backend_prompt("113", reply)
                self.assertEqual(asks, 1)
                if accepted:
                    self.assertIsNone(failure)
                    self.assertIn("Received on stdin: 'YES\\n'", output)
                else:
                    self.assertIn("apply cancelled", failure)
                    self.assertIn("Received on stdin: ''", output)


if __name__ == "__main__":
    unittest.main()
