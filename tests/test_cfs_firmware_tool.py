import contextlib
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cfs_firmware as firmware
from cfs.native import crc8


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
        self.directory = directory
        self.catalog = {}
        for loader, applications in firmware.CATALOG.items():
            self.catalog[loader] = {}
            for application in applications:
                data = ("synthetic " + application).encode("ascii")
                self.image_path(loader, application).write_bytes(data)
                self.catalog[loader][application] = digest(data)
        self.backend.write_bytes(b"test backend")
        self.device.write_bytes(b"")
        self.serial = "TEST-DEVICE-0000000"
        images = patch.object(firmware, "CATALOG", self.catalog)
        backend = patch.object(firmware, "BACKEND_SHA256", digest(b"test backend"))
        for override in (images, backend):
            override.start()
            self.addCleanup(override.stop)
        bus = patch.object(firmware, "bus_lock", side_effect=lambda device: contextlib.nullcontext())
        bus.start()
        self.addCleanup(bus.stop)

    def image_path(self, loader, application):
        return self.directory / ("%s-%s.bin" % (loader, application))

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
            self.assertIn("direction", self.cli("downgrade", self.original, self.original)[1])
            self.original.write_bytes(b"untrusted rollback")
            self.assertIn("SHA-256 mismatch", self.cli("update", self.original, self.target)[1])
            backend.assert_not_called()

    def test_catalog_and_inspect_cover_every_canonical_image(self):
        code, output = self.cli("catalog")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(output.splitlines()), 21)
        self.assertEqual(output.count("hardware workflow available"), 9)
        with patch.object(firmware, "run_backend") as backend:
            for loader, applications in self.catalog.items():
                for application, checksum in applications.items():
                    with self.subTest(loader=loader, application=application):
                        path = self.image_path(loader, application)
                        self.assertIn("%s  %s" % (path.name, checksum), output)
                        code, details = self.cli("inspect", path)
                        self.assertEqual(code, 0, details)
                        self.assertIn("Loader: " + loader, details)
                        self.assertIn("Application: " + application, details)
                        self.assertIn("SHA-256: " + checksum, details)
            backend.assert_not_called()

    def test_known_g30_108_alias_matches_exact_original_name(self):
        original = self.image_path("cfs0_050_G30", "cfs0_000_108")
        alias = self.directory / "cfs0_050_G30-cfs0_000_108 .bin"
        alias.write_bytes(original.read_bytes())
        self.assertEqual(self.cli("inspect", original), self.cli("inspect", alias))
        alias.write_bytes(b"corruption")
        self.assertIn("SHA-256 mismatch", self.cli("inspect", alias)[1])

    def test_same_loader_cfs6_plan_keeps_native_application_names(self):
        original = self.image_path("cfs6_100_G31", "cfs6_220_000")
        target = self.image_path("cfs6_100_G31", "cfs6_236_000")
        code, output = self.cli("update", original, target)
        self.assertEqual(code, 0, output)
        self.assertIn("cfs6_220_000 -> cfs6_236_000", output)
        self.assertIn("offline only", output)
        self.assertEqual(self.cli("downgrade", target, original)[0], 0)

    def test_cross_loader_plans_fail_even_for_identical_application_bytes(self):
        g30 = self.image_path("cfs0_050_G30", "cfs0_000_113")
        g31 = self.image_path("cfs6_100_G31", "cfs6_220_000")
        g51 = self.image_path("cfs6_100_G51", "cfs6_237_000")
        self.assertEqual(self.original.read_bytes(), g30.read_bytes())
        for original, target in ((self.original, g30), (g31, g51)):
            with self.subTest(original=original.name, target=target.name):
                code, output = self.cli("update", original, target)
                self.assertEqual(code, 1)
                self.assertIn("same CFS loader identity", output)

    def test_offline_only_images_are_refused_before_backend_or_port_access(self):
        cases = (
            (self.image_path("cfs0_050_G30", "cfs0_000_113"), self.image_path("cfs0_050_G30", "cfs0_000_152")),
            (self.image_path("cfs6_100_G31", "cfs6_220_000"), self.image_path("cfs6_100_G31", "cfs6_236_000")),
        )
        self.backend.unlink()
        self.device.unlink()
        for original, target in cases:
            with self.subTest(original=original.name):
                code, output = self.cli("update", original, target, "--apply", *self.hardware())
                self.assertEqual(code, 1)
                self.assertIn("hardware workflow is limited", output)
        g51 = self.image_path("cfs6_100_G51", "cfs6_237_000")
        self.assertIn("hardware workflow is limited", self.cli("verify", g51, *self.hardware())[1])
        firmware.bus_lock.assert_not_called()

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
        self.assert_apply_transition("113", "153")

    def test_all_three_early_versions_are_eligible_in_both_directions(self):
        for early in ("108", "109", "112"):
            with self.subTest(early=early):
                self.assert_apply_transition(early, "113")
                self.assert_apply_transition("113", early)

    def assert_apply_transition(self, current, target):
        original = self.image_path(firmware.BOOT, firmware.APP + current)
        destination = self.image_path(firmware.BOOT, firmware.APP + target)
        plan = "  -> current version: '%s'\nPLAN: %s  ->  %s   (test)\n" % (
            firmware.BOOT + "-" + firmware.APP + current,
            firmware.version_text(current), firmware.version_text(target))
        history = Mock()
        history.flash.side_effect = [plan + "DRY RUN complete. NOTHING was written.",
                                     plan + "=== FLASH END ===\n"]
        action = "update" if current < target else "downgrade"
        ack = [] if action == "update" else ["--ack-untested-downgrade"]
        with patch.object(firmware, "run_backend", history.flash), \
                patch.object(firmware, "verify_app_identity", history.identity), \
                patch.object(firmware.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="APPLY"):
            code, output = self.cli(action, original, destination, "--apply", *self.hardware(), *ack)
        self.assertEqual(code, 0, output)
        self.assertIn("then run verify", output)
        self.assertEqual([call[0] for call in history.mock_calls], ["identity", "flash", "identity", "flash"])
        self.assertEqual([call.args[-2:] for call in history.identity.call_args_list],
                         [(current, self.serial.encode("ascii"))] * 2)
        self.assertEqual(history.flash.call_args_list[0].args[-1].name, destination.name)
        self.assertEqual(history.flash.call_args.args[-1], "--flash")
        self.assertEqual(history.flash.call_args.kwargs, {"expected": (current, target, destination.name)})

    def test_backend_failure_with_zero_exit_never_counts_as_a_success(self):
        with patch.object(firmware, "verify_app_identity"), \
                patch.object(firmware, "run_backend", return_value="ABORT: no sector size.") as backend, \
                patch.object(firmware.sys.stdin, "isatty", return_value=True), patch("builtins.input") as confirm:
            code, output = self.cli("update", self.original, self.target, "--apply", *self.hardware())
            self.assertEqual(code, 1)
            self.assertIn("expected completion report", output)
            self.assertEqual(backend.call_count, 1)
            confirm.assert_not_called()

    def test_no_application_after_dry_run_blocks_actual_writer(self):
        plan = ("  -> current version: 'cfs0_050_G32-cfs0_000_113'\n"
                "PLAN: 1.1.3  ->  1.5.3   (test)\n"
                "DRY RUN complete. NOTHING was written.")
        with patch.object(firmware, "verify_app_identity", side_effect=[
                None, ValueError("missing APP identity")]) as identity, \
                patch.object(firmware, "run_backend", return_value=plan) as backend, \
                patch.object(firmware.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", return_value="APPLY"):
            code, output = self.cli("update", self.original, self.target, "--apply", *self.hardware())
        self.assertEqual(code, 1)
        self.assertIn("missing APP identity", output)
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(backend.call_count, 1)
        self.assertNotIn("--flash", backend.call_args.args)

    def identity_frame(self, version, serial=None, *, address=1, status=0, command=0x14):
        if serial is None:
            serial = self.serial.encode("ascii")
        body = bytes((3 + len(version) + len(serial), status, command)) + version.encode("ascii") + serial
        return bytes((0xF7, address)) + body + bytes((crc8(body),))

    def verify_replies(self, image, frames):
        transport = {"open_port": Mock(return_value=41), "build": Mock(return_value=b"\xf7\x01\x03\xff\x14\x06"),
                     "collect": Mock(return_value=[{"raw": packet} for packet in frames]), "crc8": crc8}
        with patch.object(firmware, "check_bus"), patch.object(firmware.runpy, "run_path", return_value=transport), \
                patch.object(firmware.os, "close") as close, patch.object(firmware, "run_backend") as backend:
            result = self.cli("verify", image, *self.hardware())
        transport["open_port"].assert_called_once_with(str(self.device.resolve()))
        transport["build"].assert_called_once_with(1, 0x14, status=0xFF)
        transport["collect"].assert_called_once_with(41, b"\xf7\x01\x03\xff\x14\x06")
        close.assert_called_once_with(41)
        backend.assert_not_called()
        return result

    def test_verify_accepts_one_exact_identity_with_no_other_commands(self):
        for version in ("108", "109", "112", "113", "153"):
            with self.subTest(version=version):
                image = self.image_path(firmware.BOOT, firmware.APP + version)
                code, output = self.verify_replies(image, [self.identity_frame(version)])
                self.assertEqual(code, 0, output)
                self.assertIn("verified on the expected serial", output)

    def test_verify_rejects_short_corrupt_foreign_multiple_and_stale_replies(self):
        good = self.identity_frame("153")
        examples = {
            "timeout": [], "short": [good[:-1]], "crc": [good[:-1] + bytes((good[-1] ^ 1,))],
            "other address": [self.identity_frame("153", address=2)],
            "other opcode": [self.identity_frame("153", command=0x15)],
            "error status": [self.identity_frame("153", status=0x05)],
            "multiple": [good, good], "stale version": [self.identity_frame("113")],
            "foreign serial": [self.identity_frame("153", b"X" * 19)],
            "zero serial byte": [self.identity_frame("153", b"\0" + self.serial.encode("ascii")[1:])],
        }
        for case, frames in examples.items():
            with self.subTest(case=case):
                code, output = self.verify_replies(self.target, frames)
                self.assertEqual(code, 1, output)
                self.assertNotIn("verified on the expected serial", output)

    def test_verify_refuses_inexact_serial_before_accessing_bus(self):
        for serial in ("(none)", "X" * 18, "X" * 19 + " ", "A" * 18 + "\t", "A" * 18 + "ё"):
            with self.subTest(serial=serial):
                code, output = self.cli("verify", self.target, *self.hardware(), "--serial", serial)
                self.assertEqual(code, 1, output)
                self.assertIn("exact 19 printable ASCII", output)
        firmware.bus_lock.assert_not_called()

    def test_backend_invocation_disables_vendor_file_edits(self):
        with patch.object(firmware, "check_bus"), patch.object(firmware.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b"dry run"
            with contextlib.redirect_stdout(io.StringIO()):
                firmware.run_backend(self.backend, self.device, "flash", self.target)
            self.assertEqual(run.call_args.args[0][-5:], ["flash", str(self.target), "--no-json", "--dev", str(self.device)])

    def exercise_backend_prompt(self, observed, reply, plan_target="1.5.3", loader=firmware.BOOT):
        self.backend.write_text(
            "import sys\n"
            "print(\"  -> current version: %r\")\n" % (loader + "-" + firmware.APP + observed) +
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
        cases = [("142", "1.5.3", firmware.BOOT), ("113", "1.5.2", firmware.BOOT),
                 ("113", "1.5.3", "cfs0_050_G30"), ("113", "1.5.3", "cfs0_051_G32")]
        for version, target, loader in cases:
            with self.subTest(version=version, target=target, loader=loader):
                output, failure, asks = self.exercise_backend_prompt(version, "YES", target, loader)
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
