"""Configured movement, fan context and calibration without printer effects."""

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.motion import Motion


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.pins = {"output_pin fan0": SimpleNamespace(last_value=0.5, scale=255),
                     "output_pin fan2": SimpleNamespace(last_value=0, scale=255)}
        printer = SimpleNamespace(lookup_object=lambda name, default=None: self.pins.get(name, default))
        self.box = SimpleNamespace(
            gcode=SimpleNamespace(run_script_from_command=self.commands.append,
                                  respond_info=lambda value: None),
            lifecycle=SimpleNamespace(printer=printer, reactor=SimpleNamespace(monotonic=lambda: 1)),
            toolhead=SimpleNamespace(get_position=lambda: [0, 0, 20, 0],
                                     get_status=lambda time: {"homed_axes": "xyz"}),
            boxcfg=SimpleNamespace(retrude_position_x=42, retrude_position_y=100),
            host_guard=lambda: (lambda: None), move_z=None,
            max_velocity=None, max_accel=None)
        self.motion = Motion(self.box)

    def test_fans_save_once_restore_zero_and_blow(self):
        self.motion.save_fan()
        self.pins["output_pin fan0"].last_value = 0
        self.motion.save_fan()
        self.motion.restore_fan()
        self.assertEqual(self.commands[-2:], ["SET_PIN PIN=fan0 VALUE=127.50",
                                              "SET_PIN PIN=fan2 VALUE=0.00"])
        self.assertIsNone(self.motion.fan0_last_value)
        self.motion.blow()
        self.assertEqual(self.commands[-4:], ["SET_PIN PIN=fan0 VALUE=255", "G4 P3000",
                                               "M400", "SET_PIN PIN=fan0 VALUE=0"])

    def test_fan_restore_failure_retains_saved_value(self):
        self.motion.close_model_fan()
        def fail(command):
            raise RuntimeError("pin error")
        self.box.gcode.run_script_from_command = fail
        with self.assertRaisesRegex(RuntimeError, "pin error"):
            self.motion.restore_fan()
        self.assertEqual(self.motion.fan0_last_value, 127.5)

    def test_optional_fan_combinations_and_empty_restore(self):
        for available in ((), (0,), (2,), (0, 2)):
            with self.subTest(available=available):
                self.setUp()
                self.pins = {name: pin for name, pin in self.pins.items()
                             if int(name[-1]) in available}
                self.motion.save_fan()
                self.motion.restore_fan()
                expected = ["SET_PIN PIN=fan%d VALUE=0.0" % n for n in available]
                expected += ["SET_PIN PIN=fan%d VALUE=%.2f" % (n, 127.5 if n == 0 else 0)
                             for n in available]
                self.assertEqual(self.commands, expected)
                self.motion.restore_fan()
                self.assertEqual(self.commands, expected)
                self.assertIsNone(self.motion.fan0_last_value)
                self.assertIsNone(self.motion.fan2_last_value)

    def test_z_move_and_missing_positions(self):
        self.motion.z_move()
        self.assertEqual(self.box.move_z, 30)
        self.assertEqual(self.commands, ["G91", "G0 Z30.00 F600", "G90"])
        self.commands.clear()
        self.box.boxcfg = SimpleNamespace()
        with self.assertRaisesRegex(ValueError, "retrude_position_x"):
            self.motion.go_to_box_extrude_pos()
        self.assertEqual(self.commands, [])

    def test_homing_and_restoring_limits(self):
        self.box.toolhead.get_status = lambda time: {"homed_axes": ""}
        self.motion.go_to_box_extrude_pos()
        self.assertEqual(self.commands[:2], ["G28 X Y", "BED_MESH_CLEAR"])
        self.motion.save_printer_accel(1200, 3000)
        self.motion.restore_printer_accel()
        self.assertEqual(self.commands[-2:], ["G0 F1200.00", "M204 S3000"])
        self.assertIsNone(self.box.max_velocity)

    def test_context_change_stops_further_commands(self):
        calls = []
        def guard():
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("stale")
        self.box.host_guard = lambda: guard
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.motion.blow()
        self.assertEqual(self.commands, ["SET_PIN PIN=fan0 VALUE=255"])

    def test_z_down_bounds_accumulation_and_restore(self):
        section = SimpleNamespace(getfloat=lambda name: 200)
        self.box.boxcfg.config = SimpleNamespace(getsection=lambda name: section)
        self.motion.z_down(distance=25, absolute=True)
        self.assertEqual(self.box.move_z, 5)
        self.motion.z_down(distance=4, wait=False)
        self.assertEqual(self.box.move_z, 9)
        self.motion.z_down(distance=181)
        self.assertEqual(self.box.move_z, 9)
        self.motion.z_restore()
        self.assertEqual(self.commands[-3:], ["G91", "G0 Z-9.00 F600", "G90"])
        self.assertIsNone(self.box.move_z)

    def test_custom_save_uses_both_current_coordinates_if_one_missing(self):
        gcmd = SimpleNamespace(get_command_parameters=lambda: {"X": "99"})
        self.motion.cmd_save_extrude_pos(gcmd)
        self.assertEqual(self.commands, ["MODIFY_BOX_CFG EXTRUDE_POS_X=0.00",
                                         "MODIFY_BOX_CFG EXTRUDE_POS_Y=0.00",
                                         "SAVE_BOX_CFG"])

    def test_calibration_and_resume_guard(self):
        self.box.boxcfg.pre_cut_pos_x = 10
        self.box.boxcfg.pre_cut_pos_y = 20
        self.box.boxcfg.cut_velocity = 6000
        self.box.toolhead.get_status = lambda time: {"homed_axes": ""}
        self.motion.cmd_pre_cut_pos(None)
        self.assertEqual(self.commands, ["G28", "BED_MESH_CLEAR", "M400",
                                         "G1 X10.00 Y20.00 F6000.00", "M400"])
        self.commands.clear()
        self.box.virtual_sdcard = SimpleNamespace(do_resume_status=True)
        self.motion.cmd_go_to_extrude_pos(None)
        self.motion.cmd_restore_position(None)
        self.assertEqual(self.commands, [])

    def test_custom_prepare_checks_positions_before_mutation(self):
        self.box.boxcfg = SimpleNamespace()
        self.box.custom_command_result = "old"
        gcmd = SimpleNamespace(get_command_parameters=lambda: {"CMD": "Y_SAFE"},
                               error=ValueError)
        with self.assertRaisesRegex(ValueError, "safe_pos_y"):
            self.motion.cmd_custom_command(gcmd)
        self.assertEqual(self.box.custom_command_result, "old")
        self.assertEqual(self.commands, [])

    def test_safe_position_skips_unhomed_axes_without_coordinates(self):
        self.box.boxcfg = SimpleNamespace()
        self.box.toolhead.get_status = lambda time: {"homed_axes": "z"}
        self.motion.move_to_safe_pos()
        self.assertEqual(self.commands, [])

    def test_calibration_positions_and_all_custom_completion_markers(self):
        self.box.boxcfg = SimpleNamespace(
            safe_pos_y=200, extrude_pos_x=120, extrude_pos_y=220,
            extrude_pos_z=25, cut_pos_x=-5, cut_velocity=6000,
            get_real_cut_pos_y=lambda: 225.5)
        self.motion.cmd_cut_pos(None)
        self.assertEqual(self.commands, ["G1 X-5.00 Y225.50 F6000.00", "M400"])
        self.commands.clear()
        self.motion.cmd_test_extrude(None)
        self.assertEqual(self.commands, ["G1 X120.00 Y220.00 Z25.00", "M400"])
        cases = {
            "XYZ_ZERO": ["G28"],
            "COORDINATES_ADJUST_PREPARE": ["G0 Z25", "G0 Y200.00 F6000",
                                            "G0 X120.00 F6000", "M400"],
            "COORDINATES_ADJUST_SAVE_POS": ["MODIFY_BOX_CFG EXTRUDE_POS_X=0.00",
                                             "MODIFY_BOX_CFG EXTRUDE_POS_Y=0.00", "SAVE_BOX_CFG"],
            "Y_SAFE": ["G0 Y200.00 F6000"],
        }
        for name, commands in cases.items():
            with self.subTest(command=name):
                self.commands.clear()
                gcmd = SimpleNamespace(get_command_parameters=lambda: {"CMD": name})
                self.motion.cmd_custom_command(gcmd)
                self.assertEqual(self.commands, commands)
                self.assertEqual(self.box.custom_command_result, name + "=0")
        self.commands.clear()
        self.motion.cmd_custom_command(SimpleNamespace(get_command_parameters=lambda: {"CMD": "UNKNOWN"}))
        self.assertEqual(self.commands, [])
        self.assertEqual(self.box.custom_command_result, "Y_SAFE=0")


if __name__ == "__main__":
    unittest.main()
