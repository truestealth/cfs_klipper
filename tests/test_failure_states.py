"""Assert composed Box state after phase failures and recovery exits on a PC."""

import json
import unittest

import test_recovery_actions
import test_tool_actions


class FailureStateTests(unittest.TestCase):
    def make_box(self, recovery=False):
        fixture = (test_recovery_actions.RecoveryActionTests() if recovery
                   else test_tool_actions.ToolActionTests())
        self.addCleanup(fixture.doCleanups)
        fixture.make_box()
        self.fixture = fixture
        self.tool = fixture.fixture if recovery else fixture
        for name in ("box", "native", "printer", "gcode"):
            setattr(self, name, getattr(fixture, name))

    def assert_released(self, heartbeat=True):
        box = self.box
        self.assertFalse(box.operations.active)
        self.assertFalse(box.operations._head_running)
        self.assertIsNone(box.operations._heartbeat_saved)
        self.assertEqual(box.heart_process_enable, [heartbeat, True, True, True])
        self.assertFalse(box.filament_sensor_enable)
        self.assertFalse(box.inside_error)
        self.assertFalse(box.inside_auto_refill)
        self.assertFalse(box.inside_extrusion_all_materials)

    def force_phase_return(self, phase, result):
        # Run the real phase first so the caller sees its actual state changes.
        name = {"cut": "move_to_cut", "load": "box_extrude_material",
                "flush": "material_change_flush"}[phase]
        original = getattr(self.box, name)
        self.phase_calls = 0

        def forced(*args, **kwargs):
            self.phase_calls += 1
            original(*args, **kwargs)
            return result

        setattr(self.box, name, forced)
        if phase == "cut":
            self.tool.fixture.set_head(True)
            self.tool.clear_head_after_cut = True

    def fail_effect(self, phase):
        original_script = self.gcode.on_script
        original_send = self.native.cmd_send_data_with_response

        def script(command):
            original_script(command)
            if ((phase == "cut" and command.startswith("SET_VELOCITY_LIMIT ACCEL=5000"))
                    or (phase == "load" and command == "M83\n G0 E5 F60")
                    or (phase == "filament" and command == "WAIT_EXTRUSION_ALL_MATERIALS")
                    or (phase == "empty" and command == "M104 S220")
                    or (phase == "end" and command == "END_PRINT")):
                raise RuntimeError("injected " + phase)

        def exchange(body, timeout, *args):
            if ((phase == "flush" and body[3] == 0x0E)
                    or (phase == "retract" and body[3] == 0x04)):
                raise RuntimeError("injected " + phase)
            return original_send(body, timeout, *args)

        self.gcode.on_script = script
        self.native.cmd_send_data_with_response = exchange
        if phase == "cut":
            self.tool.fixture.set_head(True)

    def test_tool_exact_false_keeps_phase_state_and_typed_recovery_context(self):
        for phase, reason, z, source, fan in (
                ("cut", "cut_err", 10.0, None, 63.75),
                ("load", "box_extrude_err", 0.8, "T1A", 63.75),
                ("flush", "flush_err", 0.8, "T1A", None)):
            with self.subTest(phase=phase):
                self.make_box()
                self.force_phase_return(phase, False)
                self.assertIsNone(self.fixture.command("T0"))
                self.assert_released()
                self.assertEqual(self.box.error, reason)
                self.assertEqual(self.box.move_z, z)
                self.assertEqual((self.box.max_velocity, self.box.max_accel), (300, 1500))
                self.assertEqual(self.box.motion.fan0_last_value, fan)
                self.assertEqual(self.box.last_cmd, source)
                self.assertEqual(self.box.t_command, "T0")
                expected = dict(last_tnn=None, tnn=None, addr=None, vtnn=None)
                expected["vtnn" if phase == "cut" else "tnn"] = "T1A"
                self.assertEqual(self.box.error_tnn, expected)
                self.assertNotIn("G92 E0", self.gcode.scripts)
                self.assertEqual(self.phase_calls, 3 if phase == "flush" else 1)

    def test_tool_none_reaches_success_tail_and_clears_shared_motion_context(self):
        for phase in ("cut", "load", "flush"):
            with self.subTest(phase=phase):
                self.make_box()
                self.force_phase_return(phase, None)
                self.assertIsNone(self.fixture.command("T0"))
                self.assert_released()
                self.assertIsNone(self.box.error)
                self.assertIsNone(self.box.move_z)
                self.assertEqual((self.box.max_velocity, self.box.max_accel), (None, None))
                self.assertIsNone(self.box.motion.fan0_last_value)
                self.assertEqual(self.box.last_cmd, "T1A")
                self.assertIn("G92 E0", self.gcode.scripts)
                self.assertEqual(self.phase_calls, 3 if phase == "flush" else 1)

    def test_nested_tool_exceptions_release_ownership_without_motion_rollback(self):
        for phase, z, source in (("cut", 10.0, None), ("load", 0.8, None),
                                  ("flush", 0.8, "T1A")):
            for heartbeat in (True, False):
                with self.subTest(phase=phase, heartbeat=heartbeat):
                    self.make_box()
                    self.box.heart_process_enable[0] = heartbeat
                    self.fail_effect(phase)
                    with self.assertRaisesRegex(RuntimeError, "injected " + phase):
                        self.fixture.command("T0")
                    self.assert_released(heartbeat)
                    self.assertEqual(self.box.move_z, z)
                    self.assertEqual((self.box.max_velocity, self.box.max_accel), (300, 1500))
                    self.assertEqual(self.box.motion.fan0_last_value, 63.75)
                    self.assertEqual(self.box.last_cmd, source)
                    self.assertIsNone(self.box.error)
                    self.assertIsNone(self.box.last_err)
                    self.assertNotIn("G92 E0", self.gcode.scripts)

    def prepare_recovery(self, reason):
        self.make_box(recovery=True)
        box = self.box
        box.print_stats.state = "paused"
        box.error = reason
        box.error_tnn.update(last_tnn=None, tnn="T1A", addr=1, vtnn="T1A")
        box.move_z, box.max_velocity, box.max_accel = 4, 300, 1500
        box.speed_factor = 0.7

    def test_each_recovery_leaf_has_its_own_successful_shared_context_tail(self):
        for reason in ("cut_err", "retrude_err", "box_extrude_err", "extruder_extrude_err",
                       "flush_err", "filament_err", "empty_print", "print_end_err"):
            with self.subTest(reason=reason):
                self.prepare_recovery(reason)
                self.box.Tnn_retry_process()
                self.assert_released()
                self.assertIsNone(self.box.move_z)
                limits = (None, None) if reason in ("cut_err", "flush_err") else (300, 1500)
                self.assertEqual((self.box.max_velocity, self.box.max_accel), limits)
                self.assertEqual(self.box.speed_factor, 0.7)
                self.assertIsNone(self.box.error)
                self.assertEqual(self.box.error_tnn, dict.fromkeys(("last_tnn", "tnn", "addr", "vtnn")))
                self.assertEqual(self.gcode.scripts[-1], "RESUME")
                self.assertIn({"tn_move_z": 0}, [kwargs for args, kwargs in self.printer.records])

    def test_each_recovery_leaf_exception_preserves_the_state_at_its_failure(self):
        for reason, phase in (("cut_err", "cut"), ("retrude_err", "retract"),
                              ("box_extrude_err", "load"), ("extruder_extrude_err", "flush"),
                              ("flush_err", "flush"), ("filament_err", "filament"),
                              ("empty_print", "empty"), ("print_end_err", "end")):
            with self.subTest(reason=reason):
                self.prepare_recovery(reason)
                self.box.motion.fan0_last_value = 63.75
                original_context = dict(self.box.error_tnn)
                self.fail_effect(phase)
                with self.assertRaisesRegex(RuntimeError, "injected " + phase):
                    self.box.Tnn_retry_process()
                self.assert_released()
                self.assertEqual(self.box.move_z, 10.0 if phase == "cut" else 4)
                self.assertEqual((self.box.max_velocity, self.box.max_accel), (300, 1500))
                self.assertEqual(self.box.speed_factor, 0.7)
                self.assertEqual(self.box.motion.fan0_last_value, 63.75)
                retained = phase in ("retract", "filament")
                self.assertEqual(self.box.error, reason if retained else None)
                expected = original_context if retained else dict.fromkeys(original_context)
                self.assertEqual(self.box.error_tnn, expected)
                self.assertIsNone(self.box.last_cmd)
                self.assertEqual(self.box.print_stats.state, "paused")
                self.assertNotIn("RESUME", self.gcode.scripts)
                self.assertNotIn("M104 S140", self.gcode.scripts)
                if phase == "load":
                    document = json.loads(self.box.store.path.read_text(encoding="utf-8"))
                    # A requested persistent target does not prove live loading.
                    self.assertEqual(document["last_cmd"], "T1A")


if __name__ == "__main__":
    unittest.main()
