"""Admission observes synthetic devices without starting printer mechanics."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.lifecycle import LifecycleStopped
from cfs.native import UnsupportedRequest, build_request, diagnostic_request
from test_box_data import CommandError
from test_host_actions import HostCommand
from test_native import reply
import test_lifecycle


class PassiveLifecycleTests(unittest.TestCase):
    def make_box(self, **options):
        fixture = test_lifecycle.LifecycleTests()
        self.addCleanup(fixture.doCleanups)
        fixture.make_box(passive=True, **options)
        self.fixture = fixture
        for name in ("box", "native", "printer", "reactor", "lifecycle", "table"):
            setattr(self, name, getattr(fixture, name))
        self.printer.gcode.scripts.clear()

    def ready(self):
        return self.fixture.ready()

    def test_startup_and_reconnect_only_read_even_with_poweron_preload(self):
        self.make_box()
        self.box.preloading_en = self.box.preloading_poweron = True
        self.native.mode = 3
        timer = self.ready()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.reactor.fire(timer), self.reactor.monotonic() + 1)
        self.assertEqual(self.native.commands(), [(1, 0x14, b""), (1, 0x0A, b""),
                                                 (1, 0x08, b"\x00"), (1, 0x0A, b"")])
        self.assertEqual(self.box.box_state.get_box_mode(1), "3")
        self.assertEqual(self.box.box_state.Tn_data["state"], "connect")
        self.assertEqual(self.printer.gcode.scripts, [])
        self.assertEqual(self.reactor.pauses, [])
        self.table[0].online = 0
        self.lifecycle.get_connect_state()
        self.table[0].online = 1
        self.native.calls.clear()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b""), (1, 0x0A, b""),
                                                 (1, 0x08, b"\x00")])
        self.assertEqual(self.printer.gcode.scripts, [])

    def test_passive_startup_does_not_wait_for_ui_preferences_or_toggle_sensor(self):
        self.make_box(online=(0, 0, 0, 0))
        self.box.preloading_poweron = None
        self.reactor.fire(self.ready())
        self.lifecycle.aggregate_state()
        self.assertEqual(self.reactor.pauses, [])
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.printer.gcode.scripts, [])
        self.assertEqual(self.box.box_state.Tn_data["state"], "disconnect")

    def test_retained_preconnected_state_still_requires_identity(self):
        self.make_box()
        self.box.box_state.modify_Tn_data(1, "connect", "state")
        self.box.box_state.tn_save_data["T1"]["color_value"][0] = "123456"
        original = self.box.store.path.read_bytes()
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b""), (1, 0x0A, b""),
                                                 (1, 0x08, b"\x00")])
        self.assertEqual(self.box.device_versions.get(1).version, "1.1.3")
        self.assertEqual(self.box.box_state.Tn_data["T1"]["color_value"][0], "123456")
        self.assertEqual(self.box.store.path.read_bytes(), original)

    def test_unsupported_identity_is_read_once_until_context_changes(self):
        self.make_box()
        self.box.box_state.modify_Tn_data(1, "connect", "state")
        self.native.version = b"999"
        self.ready()
        self.lifecycle.get_connect_state()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b"")])
        self.assertIsNone(self.box.device_versions.profile(1))
        self.box.device_versions.set_family(1, None)
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b""), (1, 0x14, b"")])

    def test_missing_identity_disconnects_and_retries_without_mechanics(self):
        self.make_box()
        self.native.responses[(1, 0x14)] = [None]
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertIsNone(self.box.device_versions.get(1))
        self.assertEqual(self.box.box_state.get_Tn_data(1, "state"), "disconnect")
        self.assertNotIn(1, self.lifecycle._initialized)
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b""), (1, 0x14, b""),
                                                 (1, 0x0A, b""), (1, 0x08, b"\x00")])
        self.assertIsNotNone(self.box.device_versions.profile(1))
        self.assertEqual(self.printer.gcode.scripts, [])

    def test_runout_report_preserves_recovery_state_without_pause_or_drain(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.box.last_cmd, self.box.move_z = "T1A", 12
        self.box.max_velocity, self.box.max_accel = 300, 1500
        self.box.print_stats.state = "printing"
        self.box.box_state.Tn_data["auto_refill"] = 0
        queued = set(self.lifecycle._callbacks)
        self.native.calls.clear()
        self.native.responses[(1, 0x0A)] = [reply(1, 0x0A, status=0x50, data=b"\x14\x1e\x00\x01")]
        self.lifecycle._request(1, "GET_BOX_STATE")
        self.assertEqual(self.box.error, "filament_err")
        self.assertEqual(self.box.error_tnn, {"last_tnn": "T1A", "tnn": "T1A", "addr": 1, "vtnn": None})
        self.assertEqual((self.box.move_z, self.box.max_velocity, self.box.max_accel), (12, 300, 1500))
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.print_stats.state, "printing")
        self.assertFalse(self.box.inside_error)
        self.assertFalse(self.box.inside_extrusion_all_materials)
        self.assertFalse(self.box.operations.active)
        self.assertIsNone(self.box.last_err)
        self.assertIn('"code":"key839"', self.lifecycle.last_error)
        self.assertEqual(set(self.lifecycle._callbacks), queued)
        self.assertEqual(self.native.commands(), [(1, 0x0A, b"")])
        self.assertEqual(self.printer.gcode.scripts, [])
        self.assertTrue(any('"code":"key839"' in item for item in self.printer.gcode.messages))

    def test_diagnostic_dedup_does_not_change_retained_pause_hint(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.box.last_err = "existing recovery hint"
        before = len(self.printer.gcode.messages)
        self.lifecycle.report_error("key_timeout", {"address": 1})
        self.lifecycle.report_error("key_timeout", {"address": 1})
        self.assertEqual(len(self.printer.gcode.messages), before + 1)
        self.assertEqual(self.box.last_err, "existing recovery hint")
        self.assertIn('"code":"key831"', self.lifecycle.last_error)
        self.assertEqual(self.printer.gcode.scripts, [])

    def test_slot_reports_are_diagnostics_without_automatic_event_actions(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        before = deepcopy(self.box.box_state.tn_save_data)
        self.box.events.process = lambda *args: self.fail("passive admission must not process slot actions")
        self.native.responses[(1, 0x0A)] = [reply(1, 0x0A, status=0x30, data=b"\x01\x02\x03\x00")]
        self.lifecycle._request(1, "GET_BOX_STATE")
        self.assertEqual(self.box.slot_events[1]["data"], b"\x01\x02\x03\x00")
        self.assertEqual(self.box.box_state.tn_save_data, before)
        self.assertEqual(self.printer.gcode.scripts, [])

    def test_timeout_invalidates_device_without_motion_or_clearing_hints(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.box.last_cmd, self.box.move_z = "T1A", 5
        self.box.max_velocity, self.box.max_accel = 300, 1500
        self.box.print_stats.state = "printing"
        self.native.responses[(1, 0x05)] = [None]
        self.assertIs(self.lifecycle._request(1, "GET_BUFFER_STATE"), False)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "state"), "disconnect")
        self.assertIsNone(self.box.device_versions.get(1))
        self.assertEqual((self.box.last_cmd, self.box.move_z, self.box.max_velocity,
                          self.box.max_accel), ("T1A", 5, 300, 1500))
        self.assertEqual(self.box.print_stats.state, "printing")
        self.assertEqual(self.printer.gcode.scripts, [])

    def test_head_event_cannot_start_phase_even_if_context_was_armed(self):
        moves = []
        self.make_box(move_head=lambda: moves.append("head"),
                      stage6=lambda *args: moves.append("stage6"))
        self.ready()
        self.lifecycle.get_connect_state()
        self.box.operations.begin("T1A", "T1B", 1)
        self.box.operations.arm_head()
        before = len(self.native.calls)
        self.printer.send_event("box:extrude_process_stage7")
        self.assertEqual(moves, [])
        self.assertFalse(self.box.extrude_process_stage7_flag)
        self.assertIsNone(self.box.extrude_process_stage7_ret)
        self.assertEqual(len(self.native.calls), before)
        self.printer.send_event("klippy:shutdown")
        self.assertFalse(self.box.operations.active)
        self.assertEqual(self.box.heart_process_enable, [True] * 4)

    def test_host_entry_rejects_before_mutating_tool_or_resume_state(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.box.virtual_sdcard.resume_tnn = "T2"
        before = (self.box.t_command, self.box.last_cmd,
                  deepcopy(self.box.error_tnn), self.box.virtual_sdcard.resume_tnn,
                  self.box.box_state.Tn_data["enable"])
        for name, params in (("T0", {}), ("BOX_ADD_TNN", {"TNN": "T1"}),
                             ("BOX_EXTRUSION_ALL_MATERIALS", {}), ("DO_AFTER_PAUSE", {}),
                             ("BOX_ENABLE_CFS_PRINT", {"ENABLE": 0})):
            with self.subTest(command=name):
                with self.assertRaisesRegex((RuntimeError, CommandError), "admission"):
                    self.printer.gcode.commands[name](HostCommand(params))
                self.assertEqual((self.box.t_command, self.box.last_cmd,
                                  self.box.error_tnn, self.box.virtual_sdcard.resume_tnn,
                                  self.box.box_state.Tn_data["enable"]), before)
        self.assertFalse(self.box.inside_extrusion_all_materials)
        self.assertEqual(self.printer.gcode.scripts, [])

    def test_native_and_direct_host_bypasses_are_rejected_before_io(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        before = len(self.native.calls)
        for request in (build_request(1, "SET_PRE_LOADING", b"\x0f\x02"),
                        build_request(1, "CREATE_CONNECT"),
                        diagnostic_request(1, 0xFF, 0x0A)):
            with self.subTest(request=request):
                with self.assertRaisesRegex((RuntimeError, UnsupportedRequest), "admission"):
                    self.box.send_native(request)
        with self.assertRaisesRegex(RuntimeError, "admission"):
            self.box.host_guard()
        with self.assertRaisesRegex(RuntimeError, "admission"):
            self.box.set_head_runout_enabled(True)
        self.assertEqual(len(self.native.calls), before)

    def test_dependency_failure_and_retry_leave_no_owned_work(self):
        self.make_box()
        sensor = self.printer.objects.pop("filament_switch_sensor head")
        with self.assertRaises(KeyError):
            self.ready()
        self.assertFalse(self.lifecycle.running)
        self.assertTrue(self.lifecycle.is_shutdown)
        self.assertFalse(self.box.cut_sensor.active)
        self.assertIsNone(self.box.native)
        self.assertEqual(self.reactor.timers, [])
        self.assertEqual(self.native.calls, [])
        self.printer.objects["filament_switch_sensor head"] = sensor
        self.assertIsNotNone(self.ready())
        self.assertTrue(self.box.cut_sensor.active)
        self.assertEqual(self.native.calls, [])

    def test_activation_failure_cleans_binding_and_can_retry(self):
        self.make_box()
        activate = self.box.cut_sensor.activate
        self.box.cut_sensor.activate = lambda: (_ for _ in ()).throw(RuntimeError("synthetic activation failure"))
        with self.assertRaisesRegex(RuntimeError, "synthetic activation failure"):
            self.ready()
        self.assertFalse(self.lifecycle.running)
        self.assertIsNone(self.box.native)
        self.assertFalse(self.box.cut_sensor.active)
        self.assertEqual(self.reactor.timers, [])
        self.assertEqual(self.native.calls, [])
        self.box.cut_sensor.activate = activate
        self.assertIsNotNone(self.ready())

    def test_ready_restart_does_not_clean_up_the_new_lifecycle(self):
        self.make_box()
        def restart():
            self.printer.events["box:ready"].remove(restart)
            self.printer.send_event("klippy:shutdown")
            self.printer.send_event("klippy:ready")
        self.printer.events.setdefault("box:ready", []).append(restart)
        with self.assertRaises(LifecycleStopped):
            self.ready()
        self.assertTrue(self.lifecycle.running)
        self.assertIsNotNone(self.box.native)
        self.assertEqual(self.reactor.timers, [self.lifecycle._timer])
        self.assertTrue(self.native.is_connected)


if __name__ == "__main__":
    unittest.main()
