"""Cross-component configuration, client and persisted-hint boundaries."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.native import UnsupportedRequest, build_request
from test_box_data import CommandError
import test_host_actions


class PCIntegrationTests(unittest.TestCase):
    def make_box(self, **settings):
        fixture = test_host_actions.HostActionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.make_box(**settings)
        fixture.native.mode = 0
        fixture.ready(identify=True)
        self.fixture = fixture
        for name in ("box", "native", "printer", "gcode", "reactor", "lifecycle"):
            setattr(self, name, getattr(fixture, name))
        self.box.toolhead.max_velocity = 300
        self.box.virtual_sdcard.resume_tnn = None
        self.box.virtual_sdcard.gcode_metadata = {}
        self.box.virtual_sdcard.first_tnn_finished = True
        self.box.virtual_sdcard.get_gcode_flush_para = lambda: None
        self.box.gcode_move.speed = 25
        self.printer.objects["filament_switch_sensor head"].runout_helper.filament_present = False
        self.lifecycle.aggregate_state()
        self.native.calls.clear()
        self.gcode.scripts.clear()

    def command(self, name, **params):
        return self.fixture.command(name, **params)

    def test_position_disabled_data_and_endpoint_remain_usable(self):
        self.make_box(has_extrude_pos=0)
        sent = []
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self.box._handle_get_box_cfg_request(SimpleNamespace(send=sent.append)), {})
        self.assertEqual(sent, [{}])
        self.command("BOX_MODIFY_TN", T1A="T2B")
        status = self.box.get_status(0)
        self.assertEqual(self.box.box_state.Tnn_map["T1A"], "T2B")
        self.assertEqual(status["state"], "connect")
        self.assertFalse(hasattr(self.box.boxcfg, "safe_pos_y"))
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])

    def test_position_disabled_commands_fail_before_affected_effects(self):
        commands = (("BOX_MOVE_TO_SAFE_POS", {}), ("BOX_GO_TO_EXTRUDE_POS", {}),
                    ("BOX_GO_TO_BOX_EXTRUDE_POS", {}), ("BOX_NOZZLE_CLEAN", {}),
                    ("TEST_BOX_CLEAN", {}), ("BOX_EXTRUDE_MATERIAL", {"TNN": "T1A"}),
                    ("BOX_MATERIAL_CHANGE_FLUSH", {"TNN": "T1A"}),
                    ("BOX_EXTRUSION_ALL_MATERIALS", {}), ("BOX_END", {}), ("T0", {}))
        for name, params in commands:
            with self.subTest(command=name):
                self.make_box(has_extrude_pos=0)
                with self.assertRaisesRegex((ValueError, CommandError), "positions|requires configured"):
                    self.command(name, **params)
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assertFalse(self.box.operations.active)

    def test_position_disabled_box_end_preserves_pending_drain_and_source(self):
        self.make_box(has_extrude_pos=0)
        self.box.last_cmd = "T1A"
        self.box.is_use_ending_material = True
        with self.assertRaisesRegex(ValueError, "positions"):
            self.command("BOX_END")
        self.assertTrue(self.box.is_use_ending_material)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.gcode.scripts, [])
        self.assertEqual(self.native.calls, [])

    def test_position_disabled_resume_and_continuation_fail_before_motion(self):
        for command in ("BOX_RESUME_EXTRUDE", "T0"):
            with self.subTest(command=command):
                self.make_box(has_extrude_pos=0)
                self.box.last_cmd = "T1A"
                self.box.virtual_sdcard.resume_tnn = "T1A"
                self.box.box_state.is_continue_print = True
                self.printer.objects["filament_switch_sensor head"].runout_helper.filament_present = True
                with self.assertRaisesRegex(ValueError, "positions"):
                    self.command(command)
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])

    def test_unsupported_feed_encoding_fails_before_loading_effects(self):
        for command, params in (("T0", {}), ("BOX_EXTRUDE_MATERIAL", {"TNN": "T1A"})):
            with self.subTest(command=command):
                self.make_box(version=0)
                with self.assertRaisesRegex(UnsupportedRequest, "encoding 1"):
                    self.command(command, **params)
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assertFalse(self.box.operations.active)

    def test_no_sensor_same_content_does_not_require_tray_or_fans(self):
        self.make_box(has_extrude_pos=0, filament_sensor=None)
        self.printer.objects.pop("output_pin fan0")
        self.printer.objects.pop("output_pin fan2")
        self.box.last_cmd = "T1A"
        self.command("T0")
        self.assertEqual(self.native.commands(), [(1, 4, b"\x01\x00")])
        self.assertEqual(self.gcode.scripts, ["M104 S220"])
        self.assertIsNone(self.lifecycle.head_sensor)
        # The established no-sensor True fallback is not a measured reading.
        self.assertFalse(self.box.operations.active)

    def test_extrusion_error_guard_is_shared_and_not_a_motion_observation(self):
        self.make_box()
        self.box.box_state.e_err_set()
        self.assertIs(self.box.check_and_extrude(10, 300), False)
        self.assertEqual(self.gcode.scripts, [])
        self.box.box_state.clear_e_err()
        self.gcode.on_script = lambda script: self.box.box_state.e_err_set() if script.startswith("G0 E") else None
        self.assertIs(self.box.check_and_extrude(10, 300), False)
        self.assertEqual(self.gcode.scripts, ["G91", "G0 E10.00 F300.00", "G90", "M400"])
        self.assertTrue(self.box.box_state.extrude_err)

    def test_powerloss_hints_do_not_override_contradictory_sensor_observations(self):
        self.make_box()
        self.box.send_native(build_request(1, "GET_FILAMENT_SENSOR_STATE", b"\x00"))
        observed = self.box.box_state.get_Tn_inner_data(1, "filament_sensor", subpart="material")
        mapping = dict(self.box.box_state.Tnn_map, T1A="T1B")
        self.box.store.save_mapping(mapping)
        self.box.store.update("enable", enable=1)
        self.box.store.update("last_cmd", last_cmd="T1B")
        self.native.calls.clear()
        self.command("BOX_POWER_LOSS_RESTORE")
        self.assertEqual(self.box.last_cmd, "T1B")
        self.assertEqual(self.box.box_state.Tnn_map, mapping)
        self.assertEqual(observed, 1)  # A observed; saved hint names B.
        self.assertEqual(self.box.box_state.get_Tn_inner_data(1, "filament_sensor", subpart="material"), 1)
        self.gcode.scripts.clear()
        self.box.virtual_sdcard.resume_tnn = "T1A"
        self.command("BOX_RESUME_EXTRUDE")
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])

    def test_powerloss_rejects_corrupt_hints_without_changing_live_state(self):
        self.make_box()
        self.box.store.path.write_text('{"enable":1,"last_cmd":"invalid"}', encoding="utf-8")
        before = self.box.get_status(0)
        with self.assertRaises(ValueError):
            self.command("BOX_POWER_LOSS_RESTORE")
        self.assertEqual(self.box.get_status(0), before)
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(json.loads(self.box.store.path.read_text(encoding="utf-8")),
                         {"enable": 1, "last_cmd": "invalid"})
        self.assertEqual(self.gcode.scripts, [])


if __name__ == "__main__":
    unittest.main()
