"""Synthetic retained-boundary checks; no vendor module runs in this suite."""

import json
from types import SimpleNamespace
import unittest

import test_phase_actions


class HeadSignal:
    """Own fixture for a producer that records a level before notifying Box."""

    def __init__(self, printer):
        self.printer = printer
        self.runout_helper = SimpleNamespace(filament_present=False)

    def _button_handler(self, eventtime, state):
        self.runout_helper.filament_present = state
        if state:
            self.printer.send_event("box:extrude_process_stage7")


class RetainedIntegrationTests(unittest.TestCase):
    def make_sensor(self):
        return HeadSignal(self.printer)

    def make_box(self):
        fixture = test_phase_actions.PhaseActionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.make_box()
        fixture.fire_head = False
        self.fixture = fixture
        for name in ("box", "native", "printer", "gcode", "reactor", "lifecycle"):
            setattr(self, name, getattr(fixture, name))
        self.sensor = self.make_sensor()
        self.printer.objects["filament_switch_sensor head"] = self.sensor
        self.lifecycle.head_sensor = self.sensor
        self.printer.objects["box"] = self.box
        original = self.native.cmd_send_data_with_response

        def exchange(body, timeout, *args):
            raw = original(body, timeout, *args)
            if body[3] == 0x10 and body[5] == 4:
                self.sensor._button_handler(self.reactor.monotonic(), True)
                self.sensor._button_handler(self.reactor.monotonic(), True)
            return raw

        self.native.cmd_send_data_with_response = exchange

    def test_head_producer_completes_full_load_once(self):
        self.make_box()
        self.assertIs(self.box.box_extrude_material("T1A"), True)
        self.assertEqual(self.fixture.stages(), [0, 4, 6, 5])
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.gcode.scripts.count("M83\n G0 E5 F60"), 1)
        self.assertFalse(self.box.extrude_process_stage7_flag)
        self.assertFalse(self.box.operations.active)
        self.assertTrue(all(self.box.heart_process_enable))

    def test_false_head_result_is_consumed_after_stage4_success(self):
        self.make_box()
        self.fixture.head_status = 0x05
        self.assertIs(self.box.box_extrude_material("T1A"), False)
        self.assertEqual(self.fixture.stages(), [0, 4, 6, 5])
        self.assertEqual(self.box.extrude_process_stage7_ret,
                         {"ret": False, "extrude_process_ret_state": "EXTRUDE_ERR1"})
        self.assertEqual(self.box.extrude_process_state, "OK")
        self.assertFalse(self.box.extrude_process_stage7_flag)
        self.assertIsNone(self.box.last_cmd)
        self.assertFalse(any(command == 0x05 for _, command, _ in self.native.commands()))
        self.assertFalse(self.box.operations.active)
        self.assertTrue(all(self.box.heart_process_enable))

    def test_unowned_and_shutdown_head_events_cannot_feed(self):
        self.make_box()
        self.sensor._button_handler(10, True)
        self.box.operations.begin(None, "T1A", 1)
        self.box.operations.arm_head()
        self.sensor._button_handler(10, False)
        self.assertEqual(self.fixture.stages(), [])
        self.printer.send_event("klippy:shutdown")
        self.sensor._button_handler(10, True)
        self.assertEqual(self.fixture.stages(), [])
        self.assertFalse(self.box.operations.active)
        self.assertTrue(all(self.box.heart_process_enable))

    def test_direct_consumer_paths_share_live_state_and_saved_metadata(self):
        self.make_box()
        box = self.printer.lookup_object("box")
        self.assertIs(box.box_action, box)
        self.assertIs(box.box_action.box_save, box)
        box.box_action.box_state.is_continue_print = "1"
        self.assertEqual(box.box_state.is_continue_print, "1")
        box.inside_error = True
        box.box_action.heart_process_enable[0] = False
        box.box_action.box_save.move_z = 3
        box.box_action.box_save.last_err = "synthetic-error"
        self.assertTrue(box.inside_error)
        self.assertFalse(all(box.heart_process_enable))
        self.assertEqual((box.move_z, box.last_err), (3, "synthetic-error"))
        box.record_tn_move_z(0)
        self.assertEqual(self.printer.records[-1][1], {"tn_move_z": 0})
        self.assertTrue(box.store.update("enable", enable=1))
        with open(box.box_state.tn_save_data_path, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["enable"], 1)


if __name__ == "__main__":
    unittest.main()
