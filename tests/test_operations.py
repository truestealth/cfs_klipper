"""Synthetic cooperative operation and head callback checks."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.box import Box
from cfs.native import build_request
from cfs.operations import OperationCancelled
from test_box_native import Native, Printer, Reactor
from test_configuration import FakeConfig
from test_native import reply


class OperationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.box = Box(FakeConfig({"bus": "custom_bus", "version": 1}), directory.name)
        self.native, self.reactor = Native(), Reactor()
        self.box.bind_native(Printer(self.native, self.reactor), families={1: "cfs0"})
        self.native.replies.append(reply(1, 0x14, data=b"113" + b"A" * 19))
        self.box.send_native(build_request(1, "GET_VERSION_SN"))
        self.context = self.box.operations
        self.context.begin("T1A", "T1B", 1)

    def test_head_reenters_native_stage4_and_saves_failed_stage6(self):
        self.context.arm_head()
        moves = []
        self.box._effect_handlers["report_error"] = lambda *args: setattr(self.box, "error", "phase_failure")

        def stage6(address, target):
            self.assertEqual((address, target), (1, "T1B"))
            return self.box.send_native(build_request(address, "EXTRUDE_PROCESS", b"\x02\x06\x00"))

        def during_stage4():
            self.context.head(lambda: moves.append("E5"), stage6)
            self.context.head(lambda: moves.append("duplicate"), stage6)
            return reply(1, 0x10)

        self.native.replies.extend((during_stage4, reply(1, 0x10, status=0x01)))
        self.box.send_native(build_request(1, "EXTRUDE_PROCESS", b"\x02\x04\x00"))
        self.assertEqual(moves, ["E5"])
        self.assertTrue(self.box.extrude_process_stage7_flag)
        with self.assertRaises(OperationCancelled):
            self.context.wait(lambda: True)
        self.assertFalse(self.box.filament_sensor_enable)
        self.assertEqual(self.box.extrude_process_stage7_ret,
                         {"ret": False, "extrude_process_ret_state": "PARAMS_ERR"})

    def test_permission_retry_and_missing_actions(self):
        self.context.head(None, None)
        self.context.arm_head()
        self.box.extrude_process_inside_retry_loose = True
        self.context.head(None, None)
        self.assertTrue(self.box.extrude_process_stage7_flag_when_retry_loose)
        self.box.extrude_process_inside_retry_loose = False
        with self.assertRaises(RuntimeError):
            self.context.head(None, None)
        self.assertFalse(self.box.extrude_process_stage7_flag)
        self.assertFalse(self.context.active)
        self.assertTrue(self.box.heart_process_enable[0])

    def test_action_and_wait_exceptions_release_ownership_without_rollback(self):
        def fail(*args):
            raise OSError("synthetic phase failure")
        for move, stage6 in ((fail, lambda *args: True), (lambda: None, fail)):
            self.context.cancel()
            self.context.begin("T1A", "T1B", 1)
            self.context.arm_head()
            with self.assertRaises(OSError):
                self.context.head(move, stage6)
            self.assertFalse(self.context.active)
            self.assertFalse(self.box.filament_sensor_enable)
            self.assertTrue(self.box.heart_process_enable[0])
        self.context.begin("T1A", "T1B", 1)
        self.context.arm_head()
        with self.assertRaises(OSError):
            self.context.wait(fail)
        self.assertFalse(self.context.active)
        self.assertTrue(self.box.heart_process_enable[0])

    def test_cancel_and_replacement_stop_wait_and_reentrant_effects(self):
        self.context.arm_head()
        moves = []
        with self.assertRaises(OperationCancelled):
            self.context.head(lambda: self.box.invalidate_device(1),
                              lambda *args: moves.append("stage6"))
        self.assertEqual(moves, [])
        self.assertFalse(self.box.extrude_process_stage7_flag)
        self.assertFalse(self.context.active)
        self.assertFalse(self.box.filament_sensor_enable)
        self.context.cancel()
        self.native.replies.append(reply(1, 0x14, data=b"113" + b"A" * 19))
        self.box.send_native(build_request(1, "GET_VERSION_SN"))
        self.context.begin("T1A", "T1B", 1)
        self.reactor.pause = lambda deadline: self.context.cancel()
        with self.assertRaises(OperationCancelled):
            self.context.wait(lambda: False)

    def test_head_owns_only_target_heartbeat_and_preserves_previous_source(self):
        self.context.cancel()
        self.box.last_cmd = "T1C"
        flags = self.box.heart_process_enable
        flags[1] = False
        self.context.begin("T1A", "T1B", 1)
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertEqual(self.box.last_cmd, "T1C")
        self.assertEqual(flags, [True, False, True, True])
        self.context.arm_head()
        self.context.arm_head()
        self.assertIs(self.box.heart_process_enable, flags)
        self.assertEqual(flags, [False, False, True, True])
        self.context.cancel()
        self.assertEqual(flags, [True, False, True, True])
        self.assertEqual(self.box.last_cmd, "T1C")
        flags[0] = False
        self.context.begin("T1A", "T1B", 1)
        self.context.arm_head()
        self.context.cancel()
        self.assertEqual(flags, [False, False, True, True])

    def test_unbind_restores_target_heartbeat(self):
        flags = self.box.heart_process_enable
        self.context.arm_head()
        self.box.unbind_native()
        self.assertEqual(flags, [True, True, True, True])
        self.assertIs(self.box.heart_process_enable, flags)
        self.assertFalse(self.context.active)
        self.assertFalse(self.box.filament_sensor_enable)
    def test_stale_operation_restores_target_heartbeat(self):
        flags = self.box.heart_process_enable
        self.context.arm_head()
        self.box.device_versions.invalidate(1)
        with self.assertRaises(OperationCancelled):
            self.context.wait(lambda: False)
        self.assertIs(self.box.heart_process_enable, flags)
        self.assertEqual(flags, [True, True, True, True])
        self.assertFalse(self.box.filament_sensor_enable)
    def test_begin_requires_supported_identity_and_matching_physical_target(self):
        self.context.cancel()
        for target, address in (("T2A", 1), ("T0", 1), ("T1E", 1), ("T1A", True)):
            with self.subTest(target=target, address=address):
                with self.assertRaises(ValueError):
                    self.context.begin(None, target, address)
        self.box.invalidate_device(1)
        with self.assertRaises(ValueError):
            self.context.begin(None, "T1A", 1)
        self.assertFalse(self.context.active)
        self.assertFalse(self.box.filament_sensor_enable)
        self.box.device_versions.update(1, b"999" + b"A" * 19)
        with self.assertRaises(ValueError):
            self.context.begin(None, "T1A", 1)
        self.box.device_versions.update(1, b"113" + b"A" * 19)
        self.box.device_versions.set_family(1, None)
        with self.assertRaises(ValueError):
            self.context.begin(None, "T1A", 1)

    def test_wait_rejects_nonfinite_and_nonpositive_intervals(self):
        for interval in (float("nan"), float("inf"), float("-inf"), 0, -1):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    self.context.wait(lambda: False, interval)
        self.assertEqual(self.reactor.pauses, [])
    def test_snapshots_and_error_cancel(self):
        self.context.cancel()
        self.box.error_tnn.update(last_tnn="T1A")
        self.context.begin("T1A", "T1B", 1)
        self.box.error_tnn["last_tnn"] = "T1C"
        self.assertEqual(self.context.error_context["last_tnn"], "T1A")
        self.context.arm_head()
        self.box.error = "load_error"
        with self.assertRaises(OperationCancelled):
            self.context.wait(lambda: True)
        self.assertFalse(self.context.active)
        self.assertFalse(self.box.filament_sensor_enable)


if __name__ == "__main__":
    unittest.main()
