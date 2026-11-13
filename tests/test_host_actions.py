"""Run registered host commands through real Box components on PC substitutes."""

import json
import math
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.box import Box
from cfs.lifecycle import LifecycleStopped
from cfs.operations import OperationCancelled
from test_box_data import CommandError
from test_box_native import NativeCommand
from test_configuration import FakeConfig
from test_lifecycle import GCode, Native, Printer, Reactor
from test_native import reply


class HostCommand(NativeCommand):
    def __init__(self, parameters=None, line=None):
        super().__init__(parameters or {})
        self.line = line

    def get(self, name, default=NativeCommand._missing):
        if name not in self.parameters and default is self._missing:
            raise self.error("missing " + name)
        return self.parameters.get(name, default)

    def get_commandline(self):
        return self.line


class HostGCode(GCode):
    """Record physical G-code; dispatch only the extension's Python handlers."""

    def __init__(self, printer):
        super().__init__()
        self.printer, self.on_script = printer, None

    def run_script_from_command(self, script):
        super().run_script_from_command(script)
        for line in script.splitlines():
            parts = line.split()
            if not parts:
                continue
            move = self.printer.objects["gcode_move"]
            if parts[0] in ("G90", "G91"):
                move.absolute_coord = parts[0] == "G90"
            if parts[0] in self.commands:
                parameters = dict(item.split("=", 1) for item in parts[1:])
                self.commands[parts[0]](HostCommand(parameters, line))
        if self.on_script is not None:
            self.on_script(script)


class HostPrinter(Printer):
    def __init__(self, native, reactor):
        super().__init__(native, reactor, "head")
        self.gcode = self.objects["gcode"] = HostGCode(self)
        self.buttons = []
        self.heater = SimpleNamespace(can_extrude=True, smoothed_temp=220)
        self.position = [10, 20, 40, 0]
        self.objects["toolhead"] = SimpleNamespace(
            get_position=lambda: list(self.position),
            get_status=lambda eventtime: {"homed_axes": "xyz"},
            extruder=SimpleNamespace(get_heater=lambda: self.heater),
            max_accel=1500, max_accel_to_decel=1000)
        move = self.objects["gcode_move"]
        move.absolute_coord = True
        move.last_position = list(self.position)
        self.objects["virtual_sdcard"].do_resume_status = False
        self.objects["filament_switch_sensor head"].runout_helper.filament_present = True
        self.objects["output_pin fan0"] = SimpleNamespace(last_value=0.25, scale=255)
        self.objects["output_pin fan2"] = SimpleNamespace(last_value=0, scale=255)

    def load_object(self, config, name):
        if name != "buttons":
            raise AssertionError("unexpected retained dependency: " + name)
        return SimpleNamespace(register_buttons=lambda pins, callback: self.buttons.append((pins, callback)))


class HostActionTests(unittest.TestCase):
    def make_box(self, switch=None, **settings):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        values = {"bus": "custom", "version": 1, "has_extrude_pos": 1,
                  "filament_sensor": "head", "switch_pin": switch}
        values.update(settings)
        self.config_path = Path(directory.name) / "box.cfg"
        self.config_path.write_text("[box]\nextrude_pos_x:127\nextrude_pos_y:368\n\n[other]\nkeep:1\n", encoding="utf-8")
        self.box = Box(FakeConfig(values), directory.name, config_paths=[self.config_path])
        self.native, self.reactor = Native(), Reactor()
        self.printer = HostPrinter(self.native, self.reactor)
        self.gcode = self.printer.gcode
        self.lifecycle = self.box.register_lifecycle(
            self.printer, families={1: "cfs0"},
            address_table=[SimpleNamespace(online=int(index == 0)) for index in range(4)],
            port_exists=lambda port: True, current_dispatch=lambda: "other")
        self.box.preloading_poweron = False

    def ready(self, identify=False):
        self.printer.send_event("klippy:ready")
        if identify:
            self.lifecycle.get_connect_state()

    def command(self, name, **parameters):
        line = " ".join([name] + ["%s=%s" % item for item in parameters.items()])
        return self.gcode.commands[name](HostCommand(parameters, line))

    def test_stage5_registration_alias_and_truthy_hall_condition_before_ready(self):
        matrix = json.loads((Path(__file__).resolve().parents[1] / "docs/contract-matrix.json").read_text(encoding="utf-8"))
        required = {row["name"] for row in matrix["items"] if row["kind"] == "gcode" and row["stage"] == 5}
        hall = {"BOX_CUT_HALL_ZERO", "BOX_CUT_HALL_TEST"}
        for switch in (None, "", "synthetic_pin"):
            with self.subTest(switch=switch):
                self.make_box(switch=switch)
                expected = required if switch else required - hall
                self.assertEqual(set(self.gcode.commands) & required, expected)
                self.assertEqual(len(self.gcode.commands), 48 if switch else 46)
                self.assertIs(self.gcode.commands["BOX_NOZZLE_CLEAN"], self.gcode.commands["TEST_BOX_CLEAN"])
                self.assertEqual(len(self.printer.buttons), int(bool(switch)))
                self.assertNotIn("BOX_EXTRUDER_EXTRUDE", self.gcode.commands)
                self.assertIsNone(self.box._ffi_pair)
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    self.command("BOX_BLOW")

    def test_registered_cut_sensor_keeps_initial_observation_and_invalidates_shutdown(self):
        self.make_box(switch="synthetic_pin")
        pins, callback = self.printer.buttons[0]
        self.assertEqual(pins, ["synthetic_pin"])
        callback(0, True)
        self.ready()
        self.assertTrue(self.box.cut_sensor.cut_happened)
        self.assertIsNone(self.command("BOX_CUT_STATE"))
        self.assertIn("cut sensor detected", self.gcode.messages[-1])
        self.printer.send_event("klippy:shutdown")
        callback(1, True)
        self.assertFalse(self.box.cut_sensor.cut_present)
        self.assertFalse(self.box.cut_sensor.cut_happened)
        self.ready()
        callback(2, True)
        callback(3, False)
        self.assertFalse(self.box.cut_sensor.cut_present)
        self.assertTrue(self.box.cut_sensor.cut_happened)
        self.assertEqual(len(self.printer.buttons), 1)

    def test_resume_guards_precede_parameter_access_and_all_effects(self):
        self.make_box()
        self.ready()
        self.box.virtual_sdcard.do_resume_status = True
        for name in ("BOX_GO_TO_EXTRUDE_POS", "RESTORE_POSITION",
                     "BOX_CUT_MATERIAL", "BOX_MATERIAL_FLUSH"):
            with self.subTest(name=name), self.assertLogs(level="WARNING"):
                self.assertIsNone(self.gcode.commands[name](object()))
        self.assertEqual(self.gcode.scripts, [])
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.box.error_next_macro, [])

    def test_position_disabled_commands_fail_before_required_actions(self):
        self.make_box(has_extrude_pos=0)
        self.ready()
        for name in ("BOX_NOZZLE_CLEAN", "TEST_BOX_CLEAN", "BOX_MOVE_TO_SAFE_POS",
                     "BOX_GO_TO_EXTRUDE_POS", "BOX_GO_TO_BOX_EXTRUDE_POS",
                     "TEST_BOX_EXTRUDE", "RESTORE_POSITION", "BOX_MATERIAL_FLUSH"):
            with self.subTest(name=name), self.assertRaises((ValueError, CommandError)):
                self.command(name)
            self.assertEqual(self.gcode.scripts, [])

    def test_registered_configured_extrusion_and_raw_error_flag(self):
        self.make_box(Tn_extrude=12)
        self.ready()
        self.assertIsNone(self.command("BOX_TN_EXTRUDE"))
        self.assertEqual(self.gcode.scripts, ["G91", "G91", "G0 E12.00 F360.00",
                                              "G90", "M400", "G90", "M400"])
        self.gcode.scripts.clear()
        self.box.box_state.extrude_err = True
        self.assertIsNone(self.command("BOX_TN_EXTRUDE"))
        self.assertEqual(self.gcode.scripts, ["G91"])
        self.assertFalse(self.box.gcode_move.absolute_coord)

    def test_standalone_flush_keeps_negative_default_and_restores_coordinate_mode(self):
        self.make_box()
        self.ready()
        self.assertIsNone(self.command("BOX_MATERIAL_FLUSH"))
        self.assertIn("G0 E-60.00 F360.00", self.gcode.scripts)
        self.assertEqual(self.gcode.scripts[-1], "G91\n G0 E-5 F600\n M400")
        self.assertTrue(self.box.gcode_move.absolute_coord)
        for invalid in ("-1", "201", "bad"):
            before = len(self.gcode.scripts)
            with self.subTest(value=invalid), self.assertRaises(CommandError):
                self.command("BOX_MATERIAL_FLUSH", LEN=invalid)
            self.assertEqual(len(self.gcode.scripts), before)

    def test_material_flush_base_survives_retained_macro_alias_and_parameters(self):
        self.make_box()
        base = self.gcode.register_command("BOX_MATERIAL_FLUSH", None)
        self.gcode.register_command("BOX_MATERIAL_FLUSH_99", base)
        # A bounded alias substitute: real retained Jinja rendering is a
        # separate acceptance check, not supplied by this Python dispatcher.
        def wrapper(gcmd):
            self.gcode.run_script_from_command("M109 S215")
            return self.gcode.commands["BOX_MATERIAL_FLUSH_99"](gcmd)
        self.gcode.register_command("BOX_MATERIAL_FLUSH", wrapper)
        self.ready()
        self.assertIsNone(self.command("BOX_MATERIAL_FLUSH", LEN="5", VELOCITY="90", TEMP="215", PERCENT="2"))
        self.assertEqual(self.gcode.scripts[0], "M109 S215")
        self.assertIn("G0 E10.00 F90.00", self.gcode.scripts)
        self.assertIs(self.gcode.commands["BOX_MATERIAL_FLUSH_99"], base)

    def test_fan_context_is_shared_with_cut_and_restored_once(self):
        self.make_box()
        self.ready()
        self.command("BOX_SAVE_FAN")
        self.assertEqual(self.box.motion.fan0_last_value, 63.75)
        self.assertEqual(self.box.motion.fan2_last_value, 0)
        self.printer.objects["filament_switch_sensor head"].runout_helper.filament_present = False
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(self.command("BOX_CUT_MATERIAL"))
        self.assertEqual(self.box.motion.fan0_last_value, 63.75)
        self.assertIsNone(self.box.error)
        self.command("BOX_RESTORE_FAN")
        self.assertEqual(self.gcode.scripts[-2:], ["SET_PIN PIN=fan0 VALUE=63.75", "SET_PIN PIN=fan2 VALUE=0.00"])
        self.assertIsNone(self.box.motion.fan0_last_value)

    def test_failed_cut_records_macro_context_with_real_shared_reporter(self):
        self.make_box(switch="synthetic_pin")
        self.ready()
        self.assertIsNone(self.command("BOX_CUT_MATERIAL"))
        self.assertEqual(self.box.error, "macro_cut_err")
        self.assertEqual(self.box.error_next_macro, ["BOX_CUT_MATERIAL"])
        self.assertEqual(self.reactor.pauses, [1] * 5)
        self.assertEqual(self.box.last_err, '!! {"code":"key841", "msg":"cut error, cut sensor not detected, cutting not rebound", "values": []}')
        self.assertFalse(self.box.inside_error)
        # The failed sensed path has no normal acceleration-restore tail.
        self.assertFalse(any("ACCEL=1500.000" in script for script in self.gcode.scripts))

    def test_native_wheel_requires_fresh_successful_finite_sample(self):
        self.make_box()
        self.ready(identify=True)
        for data, expected in ((1.25, (True, 1.25)), (float("nan"), (False, None)),
                               (float("inf"), (False, None)), (float("-inf"), (False, None))):
            with self.subTest(data=data):
                self.native.responses[(1, 0x0E)] = [reply(1, 0x0E, data=struct.pack(">f", data))]
                self.assertEqual(self.box.read_measuring_wheel(1), expected)
        self.box.box_state.modify_Tn_data(1, 123, "measuring_wheel")
        self.native.responses[(1, 0x0E)] = [reply(1, 0x0E, status=0x30, data=b"\x01\x02\x03\x04")]
        self.assertEqual(self.box.read_measuring_wheel(1), (False, None))
        self.native.responses[(1, 0x0E)] = [None]
        self.assertEqual(self.box.read_measuring_wheel(1), (False, None))
        self.assertEqual(self.box.box_state.get_Tn_data(1, "measuring_wheel"), 123)

    def test_segmented_feed_uses_native_wheel_and_real_nozzle_clean(self):
        self.make_box()
        self.ready(identify=True)
        self.native.responses[(1, 0x0E)] = [reply(1, 0x0E, data=struct.pack(">f", value))
                                           for value in (100, 80, 60)]
        self.assertTrue(self.box.Tn_Extrude(140, [360], 220, [1], "T1A"))
        self.assertIn("G0 E90.00 F360.00", self.gcode.scripts)
        self.assertIn("G0 E80.00 F360.00", self.gcode.scripts)
        self.assertEqual(self.gcode.scripts.count("SET_PIN PIN=fan0 VALUE=255"), 2)
        self.assertEqual(self.gcode.scripts.count("G4 P200"), 10)
        self.assertEqual([entry for entry in self.native.commands() if entry[1] == 0x0E], [(1, 0x0E, b"\x01")] * 3)
        self.assertEqual(self.gcode.scripts[-2:], ["G90", "M400"])
        self.assertIsNone(self.box.last_err)

    def test_blocked_nozzle_and_missing_wheel_have_distinct_results(self):
        self.make_box()
        self.ready(identify=True)
        self.native.responses[(1, 0x0E)] = [reply(1, 0x0E, data=struct.pack(">f", 100))] * 3
        self.assertFalse(self.box.Tn_Extrude(140, [360], 220, [1], "T1A"))
        self.assertEqual(self.box.last_err, '!! {"code":"key845", "msg":"the nozzle is blocked", "values": []}')
        self.box.last_err = None
        self.gcode.scripts.clear()
        self.native.responses[(1, 0x0E)] = [reply(1, 0x0E, status=0x30, data=b"\x01\x02\x03\x04")]
        self.assertFalse(self.box.Tn_Extrude(140, [360], 220, [1], "T1A"))
        self.assertEqual(self.gcode.scripts, ["G91"])
        self.assertIsNone(self.box.last_err)

    def test_shutdown_or_target_change_stops_next_host_effect(self):
        for change in ("shutdown", "target", "cancel"):
            with self.subTest(change=change):
                self.make_box()
                self.ready(identify=True)
                self.box.operations.begin("T1B", "T1A", 1)
                self.box.operations.arm_head()
                def invalidate(script):
                    self.gcode.on_script = None
                    if change == "shutdown":
                        self.printer.send_event("klippy:shutdown")
                    elif change == "target":
                        self.box.extrude_tnn = "T1B"
                    else:
                        self.box.operations.cancel()
                self.gcode.on_script = invalidate
                with self.assertRaises((LifecycleStopped, OperationCancelled)):
                    self.box.Tn_Extrude(12, [360], 220)
                self.assertEqual(self.gcode.scripts, ["G91"])
                self.assertFalse(self.box.operations.active)
                self.assertTrue(self.box.heart_process_enable[0])

    def test_heating_wait_cannot_continue_after_shutdown_and_ready(self):
        self.make_box()
        self.ready()
        self.printer.heater.smoothed_temp = 20
        def restart():
            self.reactor.on_pause = None
            self.printer.send_event("klippy:shutdown")
            self.ready()
        self.reactor.on_pause = restart
        with self.assertRaises(LifecycleStopped):
            self.box.quickly_wait_heating(220)
        self.assertEqual(self.gcode.scripts, ["M104 S220"])
        self.assertTrue(self.lifecycle.running)

    def test_material_metadata_profiles_and_retained_helper_are_read_only(self):
        self.make_box()
        self.ready()
        for tool, material in (("T1A", "G12345"), ("T1B", "G67890")):
            content = self.box.box_state.get_Tnn_content(tool)
            self.box.box_state.modify_Tn_data(1, material, "material_type", content["num"])
        self.box.store.material_database.write_text(json.dumps({"result": {"list": [
            {"base": {"id": "12345"}, "kvParam": {"nozzle_temperature": "200", "filament_max_volumetric_speed": "2"}},
            {"base": {"id": "67890"}, "kvParam": {"nozzle_temperature": "220", "filament_max_volumetric_speed": "3"}},
        ]}}), encoding="utf-8")
        before = self.box.store.material_database.read_bytes()
        self.assertEqual(self.box.get_flush_temp("T1A", "T1B"), (200, 220, 220))
        self.assertEqual(self.box.get_flush_velocity("T1A", "T1B"), ([180], [1]))
        self.assertEqual(self.box.get_material_target_temp(None), 220)
        class RGB:
            def __getitem__(self, index):
                return (self.r, self.g, self.b)
        colors = []
        helper = SimpleNamespace(get_ffi=lambda: (
            SimpleNamespace(new=lambda kind: RGB()),
            SimpleNamespace(get_flushing_volume=lambda a, b: colors.append((a, b)) or 100)))
        with patch("cfs.box.importlib.import_module", return_value=helper) as load:
            self.assertAlmostEqual(self.box.get_flush_len((1, 2, 3), (4, 5, 6)), 300 / (0.765625 * math.pi))
            self.assertIsNone(self.command("BOX_GET_FLUSH_LEN", SCV="010203", TCV="040506"))
            load.assert_called_once_with("chelper")
        self.assertEqual(colors, [((1, 2, 3), (4, 5, 6))] * 2)
        self.assertEqual(self.box.store.material_database.read_bytes(), before)
        self.assertEqual(self.native.calls, [])

    def test_calibration_saves_through_existing_data_handlers(self):
        self.make_box()
        self.ready()
        self.assertIsNone(self.command("BOX_SAVE_EXTRUDE_POS", X="120", Y="365"))
        saved = self.config_path.read_text(encoding="utf-8")
        self.assertIn("extrude_pos_x:120.0", saved)
        self.assertIn("extrude_pos_y:365.0", saved)
        self.assertIn("[other]\nkeep:1", saved)
        self.assertEqual(self.box.boxcfg.modify_dict, {})

    def test_shared_motion_context_and_print_metadata_restore(self):
        self.make_box()
        self.ready()
        self.printer.position[2] = 10
        self.assertIsNone(self.box.speed_factor)
        self.box.motion.save_printer_accel(1200, 3000)
        self.command("BOX_GO_TO_EXTRUDE_POS")
        self.assertEqual(self.box.box_action.box_save.move_z, 15)
        self.assertEqual(self.printer.records[-1][1], {"tn_move_z": 15})
        self.command("RESTORE_POSITION")
        self.assertIsNone(self.box.move_z)
        self.assertIn("G0 Z-15.00 F600", self.gcode.scripts)
        self.assertEqual(self.printer.records[-1][1], {"tn_move_z": 0})
        self.box.motion.restore_printer_accel()
        self.assertEqual(self.gcode.scripts[-2:], ["G0 F1200.00", "M204 S3000"])
        self.assertIsNone(self.box.max_velocity)
        self.assertIsNone(self.box.max_accel)

    def test_shared_error_handler_consumes_real_saved_motion_context(self):
        self.make_box()
        self.ready()
        self.printer.position[2] = 10
        self.box.motion.z_move()
        self.box.motion.save_printer_accel(1200, 3000)
        self.lifecycle.report_error("key_nozzle_blocked_err", {})
        self.assertIsNone(self.box.move_z)
        self.assertIsNone(self.box.max_velocity)
        self.assertIsNone(self.box.max_accel)
        self.assertIn("BOX_MOVE_TO_SAFE_POS", self.gcode.scripts)
        self.assertIn("G0 Z-40.00 F600", self.gcode.scripts)
        self.assertEqual(self.printer.records[-1][1], {"tn_move_z": 0})
        self.assertEqual(self.gcode.scripts[-2:], ["G0 F1200.00", "M204 S3000"])
        self.assertFalse(self.box.inside_error)

    def test_report_target_change_stops_inner_error_effects(self):
        self.make_box()
        self.ready(identify=True)
        self.box.operations.begin("T1B", "T1A", 1)
        self.box.operations.arm_head()
        self.box.print_stats.state = "printing"
        original = self.gcode.respond_raw
        def change_target(message):
            original(message)
            self.box.extrude_tnn = "T1B"
        self.gcode.respond_raw = change_target
        with self.assertLogs(level="ERROR"):
            self.lifecycle.report_error("key_nozzle_blocked_err", {})
        self.assertEqual(self.gcode.scripts, [])
        self.assertFalse(self.box.inside_error)
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.box.heart_process_enable[0])


if __name__ == "__main__":
    unittest.main()
