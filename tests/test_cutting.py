"""Synthetic button and G-code sequences; no printer or native module."""

from types import SimpleNamespace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.cutting import CutSensor, Cutter


class CuttingTests(unittest.TestCase):
    def setUp(self):
        self.scripts, self.messages, self.pauses, self.errors = [], [], [], []
        self.on_script = self.on_pause = None
        self.epoch = 0
        self.head = True
        self.actual_head = True
        self.buttons = SimpleNamespace(register_buttons=self.register_buttons)
        self.gcode = SimpleNamespace(run_script_from_command=self.run_script,
                                     respond_info=self.messages.append,
                                     respond_raw=self.messages.append)
        printer = SimpleNamespace(lookup_object=lambda name: self.gcode,
                                  load_object=lambda config, name: self.buttons)
        limits = {"stepper_y": {"position_min": -5, "position_max": 230},
                  "stepper_x": {"position_min": -10, "position_max": 230}}
        self.axis_limits = limits
        config = SimpleNamespace(
            get_printer=lambda: printer,
            getsection=lambda section: SimpleNamespace(
                getfloat=lambda key, default=None: limits[section].get(key, default)))
        self.sensor = CutSensor(config, "test_pin")
        cfg = SimpleNamespace(
            config=config, has_extrude_pos=1, cut_run_count=2,
            pre_cut_pos_x=4, pre_cut_pos_y=210, cut_pos_x=-5, cut_pos_y=225,
            cut_velocity=6000, cut_accel=5000, cut_accel_to_decel=30000,
            extrude_pos_x=120, extrude_pos_y=220, extrude_pos_z=25,
            safe_pos_y=200, retrude_position_x=110, retrude_position_y=100,
            cut_find_start_offset_y=6,
            clean_run_count=9, get_real_cut_pos_y=lambda: 225.5)
        heater = SimpleNamespace(can_extrude=False)
        self.box = SimpleNamespace(
            boxcfg=cfg, gcode=self.gcode, host_guard=self.guard,
            _reactor=SimpleNamespace(monotonic=lambda: 10, pause=self.pause),
            toolhead=SimpleNamespace(max_accel=1000, max_accel_to_decel=500,
                get_status=lambda now: {"homed_axes": "xyz"},
                extruder=SimpleNamespace(get_heater=lambda: heater)),
            gcode_move=SimpleNamespace(last_position=[4, 224.3, 25, 0]),
            get_filament_sensor_detect=lambda: self.head,
            has_actual_head_sensor=lambda: self.actual_head,
            lifecycle=SimpleNamespace(report_error=lambda key, facts: self.errors.append(key)),
            set_flushing_sign=lambda: self.run_script("M400"),
            reset_flushing_sign=lambda: self.run_script("M400"))
        self.motion = SimpleNamespace(
            z_move=lambda: self.run_script("Z_MOVE"),
            go_to_box_extrude_pos=lambda: self.run_script("BOX_POSITION"),
            close_model_fan=lambda: self.run_script("CLOSE_MODEL_FAN"))
        self.cutter = Cutter(self.box, self.motion, self.sensor)

    def register_buttons(self, pins, callback):
        self.pins, self.button_callback = pins, callback

    def guard(self):
        epoch = self.epoch

        def check():
            if epoch != self.epoch:
                raise RuntimeError("host context changed")
        return check

    def run_script(self, script):
        self.scripts.append(script)
        if self.on_script:
            self.on_script(script)

    def pause(self, deadline):
        self.pauses.append(deadline)
        if self.on_pause:
            self.on_pause()

    def test_button_registration_current_and_sticky_are_distinct(self):
        explicit = CutSensor(SimpleNamespace(), "test_pin",
                             self.box.boxcfg.config.get_printer())
        self.assertIs(explicit.gcode, self.gcode)
        self.sensor = explicit
        self.assertEqual(self.pins, ["test_pin"])
        self.button_callback(1, 1)
        self.sensor.activate()
        self.assertTrue(self.sensor.cut_happened)
        self.assertTrue(self.sensor.cut_present)
        self.button_callback(2, 0)
        self.assertFalse(self.sensor.state())
        self.assertTrue(self.sensor.cut_happened)
        self.sensor.clear_cut_happened()
        self.assertFalse(self.sensor.cut_happened)
        self.sensor.invalidate()
        self.button_callback(3, 1)
        self.assertFalse(self.sensor.cut_happened)
        self.sensor.activate()
        self.button_callback(4, 1)
        self.assertTrue(self.sensor.state())

    def test_blind_three_times_count_and_hot_retraction(self):
        self.sensor.switch_pin = None
        self.box.toolhead.extruder.get_heater().can_extrude = True
        self.assertTrue(self.cutter.move_to_cut())
        self.assertEqual(self.scripts.count("G0 Y225.50 F6000.00"), 6)
        self.assertIn("G1 E-8 F4800", self.scripts)
        self.assertIn("G1 E-2 F600", self.scripts)
        self.assertFalse(self.sensor.cut_happened)
        self.assertEqual(self.scripts[-1], "SET_VELOCITY_LIMIT ACCEL=1000.000 ACCEL_TO_DECEL=500.000")

    def test_blind_cut_does_not_require_unrelated_tray_positions(self):
        self.sensor.switch_pin = None
        for name in ("extrude_pos_z", "safe_pos_y", "retrude_position_x", "retrude_position_y"):
            delattr(self.box.boxcfg, name)
        self.assertTrue(self.cutter.move_to_cut())
        self.assertIn("Z_MOVE", self.scripts)

    def test_cleaning_missing_coordinates_prevents_all_effects(self):
        del self.box.boxcfg.safe_pos_y
        with self.assertRaisesRegex(ValueError, "safe_pos_y"):
            self.cutter.nozzle_clean()
        del self.box.boxcfg.extrude_pos_y
        with self.assertRaisesRegex(ValueError, "extrude_pos_y"):
            self.cutter.strike_clean_box()
        self.assertEqual(self.scripts, [])

    def test_sensed_fresh_latch_success_restores_limits(self):
        self.on_pause = lambda: self.button_callback(11, 1)
        self.assertTrue(self.cutter.move_to_cut())
        self.assertEqual(self.scripts.count("G0 Y225.50 F6000.00\n M400"), 2)
        self.assertEqual(self.pauses, [11])
        self.assertFalse(self.sensor.cut_happened)
        self.assertEqual(self.scripts[-1], "SET_VELOCITY_LIMIT ACCEL=1000.000 ACCEL_TO_DECEL=500.000")

    def test_stale_latch_cleared_and_failure_does_not_restore_limits(self):
        self.sensor.cut_happened = True
        self.assertFalse(self.cutter.move_to_cut())
        self.assertEqual(self.scripts.count("BOX_POSITION"), 5)
        self.assertEqual(self.scripts.count("G0 Y225.50 F6000.00\n M400"), 10)
        self.assertEqual(self.errors, ["key_cut_err"])
        self.assertFalse(any("ACCEL=1000" in script for script in self.scripts))

    def test_sticky_event_then_release_still_satisfies_legacy_result(self):
        def pulse():
            self.button_callback(11, 1)
            self.button_callback(11.1, 0)
        self.on_pause = pulse
        self.assertTrue(self.cutter.move_to_cut())
        self.assertEqual(len(self.pauses), 5)
        self.assertEqual(self.scripts.count("BOX_POSITION"), 5)
        self.assertFalse(self.errors)

    def test_report_suppression_and_no_filament_skip(self):
        self.assertFalse(self.cutter.move_to_cut(report_error=False))
        self.assertFalse(self.errors)
        self.scripts.clear()
        self.head = False
        self.assertTrue(self.cutter.move_to_cut())
        self.assertFalse(self.scripts)

    def test_callback_cancellation_stops_motion_before_tail(self):
        def cancel():
            self.epoch += 1
        self.on_pause = cancel
        with self.assertRaisesRegex(RuntimeError, "context changed"):
            self.cutter.move_to_cut()
        self.assertFalse(any("ACCEL=1000" in script for script in self.scripts))
        self.assertNotIn("BOX_POSITION", self.scripts)

    def test_nozzle_sequence_preserves_four_strokes_and_fan_units(self):
        self.cutter.nozzle_clean()
        self.assertEqual(self.scripts, [
            "M400", "G90", "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=30000",
            "G90\n G0 X120 F500", "M400", "SET_PIN PIN=fan0 VALUE=255", "G4 P3000",
            "G4 P200", "G90\n G0 Y210 F8000", "G4 P200", "G90\n G0 Y220 F12000",
            "G4 P200", "G90\n G0 Y210 F12000", "G4 P200", "G90\n G0 Y220 F12000",
            "G4 P200", "SET_PIN PIN=fan0 VALUE=0", "G90\n G0 Y200.00 F12000.00", "M400",
            "SET_VELOCITY_LIMIT ACCEL=1000.000 ACCEL_TO_DECEL=500.000", "M400"])

    def test_calibration_observed_success_only_saves_measured_y(self):
        self.head = False
        def trigger(script):
            if script.startswith("G90\nG1 Y224.2"):
                self.button_callback(11, 1)
        self.on_script = trigger
        self.cutter.find_cut_pos()
        self.assertEqual(self.box.find_cut_pos, 224.3)
        self.assertIn("MODIFY_BOX_CFG CUT_POS_Y=224.30", self.scripts)
        self.assertIn("SAVE_BOX_CFG", self.scripts)
        self.assertEqual(len(self.pauses), 2)
        self.assertFalse(self.sensor.cut_happened)

    def test_calibration_missing_event_and_preconditions_never_save(self):
        self.head = False
        self.sensor.cut_happened = True
        self.cutter.find_cut_pos()
        self.assertEqual(self.box.find_cut_pos, -2)
        self.assertEqual(len(self.pauses), 60)
        self.assertFalse(any("SAVE" in script or "MODIFY" in script for script in self.scripts))
        self.scripts.clear()
        self.head = True
        self.cutter.find_cut_pos()
        self.assertEqual(self.box.find_cut_pos, -1)
        self.assertFalse(self.scripts)
        self.assertFalse(self.errors)
        self.assertIn('"code":"key854"', self.messages[-1])
        self.actual_head = False
        with self.assertRaisesRegex(RuntimeError, "head sensor"):
            self.cutter.find_cut_pos()
        self.actual_head = True
        self.sensor.switch_pin = None
        with self.assertRaisesRegex(RuntimeError, "cutter switch"):
            self.cutter.find_cut_pos()

    def test_calibration_failure_does_not_save_or_restore(self):
        self.head = False
        def fail(script):
            if script.startswith("G90\nG1"):
                raise RuntimeError("motion rejected")
        self.on_script = fail
        with self.assertRaisesRegex(RuntimeError, "motion rejected"):
            self.cutter.find_cut_pos()
        self.assertFalse(any("SAVE" in script or "ACCEL=1000" in script for script in self.scripts))

    def test_calibration_checks_actual_candidate_not_entire_search(self):
        self.head = False
        self.box.boxcfg.cut_find_start_offset_y = 0
        self.on_script = lambda script: self.button_callback(11, 1) if script.startswith("G90\nG1 Y230") else None
        self.cutter.find_cut_pos()
        self.assertIn("SAVE_BOX_CFG", self.scripts)
        self.scripts.clear()
        self.on_script = None
        with self.assertRaisesRegex(ValueError, "Y limits"):
            self.cutter.find_cut_pos()
        self.assertNotIn("SAVE_BOX_CFG", self.scripts)
        self.assertFalse(any("Y230.1" in script for script in self.scripts))

    def test_hall_counter_quirk_and_zero_bound(self):
        self.sensor.cut_present = 1
        self.cutter.hall_test()
        self.assertEqual((self.sensor.cut_return_failed, self.sensor.cut_succeed_num), (1, 1))
        self.assertEqual((self.sensor.cut_release_failed, self.sensor.cut_release_succeed), (0, 0))
        self.scripts.clear()
        self.sensor.cut_present = 0
        self.cutter.hall_zero()
        self.assertEqual(self.sensor.cut_hall_move, -4)
        self.assertEqual(self.scripts, ["G1 Y20 F6000", "G1 X-3 F6000"])

    def test_invalid_count_or_inactive_sensor_prevents_motion(self):
        self.box.boxcfg.cut_run_count = 1.5
        with self.assertRaisesRegex(ValueError, "cut_run_count"):
            self.cutter.move_to_cut()
        self.box.boxcfg.cut_run_count = 1
        self.sensor.invalidate()
        with self.assertRaisesRegex(RuntimeError, "active cutter"):
            self.cutter.move_to_cut()
        self.assertFalse(self.scripts)

    def test_loaded_counts_do_not_inherit_ui_limits(self):
        self.sensor.switch_pin = None
        self.box.boxcfg.cut_run_count = 11
        self.assertTrue(self.cutter.move_to_cut())
        self.assertEqual(self.scripts.count("G0 Y225.50 F6000.00"), 33)
        for count in (0, -1):
            self.scripts.clear()
            self.box.boxcfg.cut_run_count = count
            self.assertTrue(self.cutter.move_to_cut())
            self.assertNotIn("G0 Y225.50 F6000.00", self.scripts)
            self.assertEqual(self.scripts[-1], "SET_VELOCITY_LIMIT ACCEL=1000.000 ACCEL_TO_DECEL=500.000")

    def test_missing_cut_fallback_coordinates_prevent_all_effects(self):
        cfg = self.box.boxcfg
        self.box.toolhead.get_status = lambda now: {"homed_axes": ""}
        for tray in (0, 1):
            cfg.has_extrude_pos = tray
            for name in ("retrude_position_x", "retrude_position_y"):
                value = getattr(cfg, name)
                delattr(cfg, name)
                for action in (self.cutter.move_to_cut, self.cutter.cut_material):
                    with self.subTest(tray=tray, field=name, action=action.__name__):
                        with self.assertRaisesRegex(ValueError, name):
                            action()
                        self.assertEqual(self.scripts, [])
                setattr(cfg, name, value)

    def test_hall_zero_cannot_search_beyond_configured_x_minimum(self):
        self.sensor.cut_present = 1
        with self.assertRaisesRegex(RuntimeError, "X minimum"):
            self.cutter.hall_zero()
        strokes = self.scripts.count("G1 x-0.1 F600")
        self.assertGreater(strokes, 0)
        self.assertLessEqual(strokes, 70)

    def test_calibration_initial_positions_are_checked_before_motion(self):
        self.head = False
        cfg = self.box.boxcfg
        for field, invalid in (("cut_find_start_offset_y", -1),
                               ("retrude_position_y", 231),
                               ("pre_cut_pos_y", -6),
                               ("pre_cut_pos_x", 231)):
            value = getattr(cfg, field)
            setattr(cfg, field, invalid)
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "configured [XY] limits"):
                    self.cutter.find_cut_pos()
                self.assertEqual(self.scripts, [])
            setattr(cfg, field, value)
        self.box.toolhead.get_status = lambda now: {"homed_axes": ""}
        for axis, field, invalid in (("stepper_x", "position_min", 0),
                                     ("stepper_x", "position_max", -4),
                                     ("stepper_y", "position_max", 19)):
            limits = self.axis_limits[axis]
            value = limits[field]
            limits[field] = invalid
            with self.subTest(axis=axis, field=field):
                with self.assertRaisesRegex(ValueError, "configured [XY] limits"):
                    self.cutter.hall_zero()
                self.assertEqual(self.scripts, [])
            limits[field] = value

    def test_clean_exception_retains_source_failure_effects(self):
        def fail(script):
            if script == "G90\n G0 Y210 F8000":
                raise RuntimeError("motion rejected")
        self.on_script = fail
        with self.assertRaisesRegex(RuntimeError, "motion rejected"):
            self.cutter.nozzle_clean()
        self.assertIn("SET_PIN PIN=fan0 VALUE=255", self.scripts)
        self.assertNotIn("SET_PIN PIN=fan0 VALUE=0", self.scripts)
        self.assertFalse(any("ACCEL=1000" in script for script in self.scripts))


if __name__ == "__main__":
    unittest.main()
