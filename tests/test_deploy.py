"""Exercise deployment transitions without running Klipper or contacting a printer."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import deploy


class FakeKlipper:
    def __init__(self):
        self.config = {}
        self.pid = 23
        self.stopped = []
        self.started = []
        self.fail_verify = False

    def ready(self):
        return self.config

    def process(self, _extras):
        return self.pid, str(self.pid), [b"/python", b"/klippy.py"], "/", {b"TEST": b"1"}

    def start_time(self, pid):
        return str(pid)

    def stop(self, pid, start):
        self.stopped.append((pid, start))

    def start(self, *launch):
        self.started.append(launch)
        self.pid += 1
        return SimpleNamespace(pid=self.pid, poll=lambda: None)

    def verify(self, _proc, config):
        if self.fail_verify:
            self.fail_verify = False
            raise RuntimeError("rejected startup")
        if self.config != config:
            raise RuntimeError("different configuration")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.extras = self.root / "extras"
        self.state = self.root / "state"
        self.extras.mkdir()
        self.state.mkdir()
        self.stock = b"from .box_wrapper import MultiColorMeterialBoxWrapper\n# stock fixture\n"
        stock_hash = patch.object(deploy, "STOCK_SHA256", deploy.install.digest(self.stock))
        stock_hash.start()
        self.addCleanup(stock_hash.stop)
        (self.extras / "box.py").write_bytes(self.stock)
        (self.extras / "box.pyc").write_bytes(b"stock cache")
        (self.extras / "serial_485.py").write_bytes(b"serial fixture")
        self.host = FakeKlipper()

    def perform(self, action, **options):
        values = dict(action=action, previous_release=None, previous_backup=None, check=False)
        values.update(options)
        deploy.deploy(SimpleNamespace(**values), self.host, self.extras, self.state)

    def current(self):
        return json.loads((self.state / "current.json").read_text(encoding="utf-8"))

    def test_install_update_and_stock_rollback(self):
        self.perform("install")
        original_release = self.current()
        files = deploy.install.sources()
        files["box.py"] += b"\n# new release\n"
        with patch.object(deploy.install, "sources", return_value=files):
            self.perform("update")
        self.assertNotEqual(self.current(), original_release)
        self.assertEqual((self.extras / "box.py").read_bytes(), files["box.py"])
        self.perform("rollback")
        self.assertEqual((self.extras / "box.py").read_bytes(), self.stock)
        self.assertEqual((self.extras / "box.pyc").read_bytes(), b"stock cache")
        self.assertEqual((self.extras / "serial_485.py").read_bytes(), b"serial fixture")
        self.assertFalse((self.extras / "cfs").exists())
        self.assertEqual(len(self.host.started), 3)

    def test_failed_activation_reinstates_previous_release(self):
        self.perform("install")
        previous = self.current()
        box_before = (self.extras / "box.py").read_bytes()
        files = deploy.install.sources()
        files["box.py"] += b"\n# bad startup\n"
        self.host.fail_verify = True
        with patch.object(deploy.install, "sources", return_value=files):
            with self.assertRaisesRegex(RuntimeError, "rejected startup"):
                self.perform("update")
        self.assertEqual(self.current(), previous)
        self.assertEqual((self.extras / "box.py").read_bytes(), box_before)
        self.assertFalse((self.state / "pending.json").exists())
        self.assertEqual(len(self.host.started), 3)

    def test_factory_macro_guard_refuses_before_stop(self):
        self.perform("install")
        self.host.config = {"gcode_macro START_PRINT": {"gcode": "\n CFS_PRINT_START_GUARD INITIAL_TOOL=0\n"}}
        with self.assertRaisesRegex(ValueError, "matching stock-compatible macro"):
            self.perform("rollback")
        self.assertEqual(len(self.host.stopped), 1)
        self.assertTrue((self.extras / "cfs").exists())

    def test_check_does_not_stop_or_write_box_files(self):
        self.perform("install", check=True)
        self.assertEqual(self.host.stopped, [])
        self.assertEqual((self.extras / "box.py").read_bytes(), self.stock)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_adopts_existing_v3_install_without_running_previous_installer(self):
        release = self.root / "previous-release"
        backup = self.root / "previous-backup"
        deploy.stage_release(release, deploy.install.sources())
        installer = release / "tools" / "install.py"
        installer.write_text("raise RuntimeError('previous installer executed')\n", encoding="utf-8")
        deploy.install.install(self.extras, backup)
        self.perform("update", previous_release=str(release), previous_backup=str(backup))
        self.assertNotEqual(self.current()["backup"], str(backup))
        self.assertEqual((backup / "box.py.original").read_bytes(), self.stock)

    def test_factory_install_rejects_altered_stock_wrapper_with_same_import(self):
        (self.extras / "box.py").write_bytes(self.stock + b"# a modification\n")
        with self.assertRaisesRegex(ValueError, "differs from the verified K1C stock wrapper"):
            self.perform("install")
        self.assertEqual(self.host.stopped, [])

    def test_journal_failure_after_stop_does_not_prevent_restart(self):
        real_write = deploy.write_json
        failed = []

        def write(path, value):
            if path.name == "pending.json" and value["phase"] in ("activate", "reinstate"):
                failed.append(value["phase"])
                raise OSError("journal write failed")
            return real_write(path, value)

        with patch.object(deploy, "write_json", side_effect=write):
            with self.assertRaisesRegex(RuntimeError, "previous deployment restored and ready"):
                self.perform("install")
        self.assertEqual(failed, ["activate", "reinstate"])
        self.assertEqual(len(self.host.started), 1)
        self.assertEqual((self.extras / "box.py").read_bytes(), self.stock)
        self.assertFalse((self.extras / "cfs").exists())
        report, = self.state.glob("*/failed.json")
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["phase"], "previous_ready")

    def test_interrupted_activation_recovers_previous(self):
        verification = self.host.verify

        def interrupt(proc, config):
            self.host.verify = verification
            deploy.interrupt_deployment(deploy.signal.SIGTERM, None)

        self.host.verify = interrupt
        with self.assertRaisesRegex(RuntimeError, "previous deployment restored and ready.*signal"):
            self.perform("install")
        self.assertEqual(len(self.host.started), 2)
        self.assertEqual((self.extras / "box.py").read_bytes(), self.stock)

    def test_interrupt_during_stop_retains_pending_and_does_not_restart(self):
        with patch.object(self.host, "stop", side_effect=KeyboardInterrupt):
            with self.assertRaisesRegex(RuntimeError, "stop was not confirmed"):
                self.perform("install")
        self.assertEqual(self.host.started, [])
        self.assertEqual((self.extras / "box.py").read_bytes(), self.stock)
        report = self.state / "pending.json"
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["phase"], "stop_uncertain")

    def test_signal_during_launch_keeps_spawn_identity_for_recovery(self):
        launch = self.host.start

        def interrupt(*args):
            self.host.start = launch
            proc = launch(*args)
            handler = deploy.signal.getsignal(deploy.signal.SIGTERM)
            handler(deploy.signal.SIGTERM, None)
            return proc

        self.host.start = interrupt
        with self.assertRaisesRegex(RuntimeError, "previous deployment restored and ready.*signal"):
            self.perform("install")
        self.assertEqual(self.host.stopped, [(23, "23"), (24, "24")])
        self.assertEqual(len(self.host.started), 2)
        self.assertEqual((self.extras / "box.py").read_bytes(), self.stock)
