"""Compose runout, recovery and resume with actual Box phases on a PC."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.lifecycle import LifecycleStopped
from cfs.native import build_request
import test_tool_actions
from test_native import reply


class RecoveryActionTests(unittest.TestCase):
    def make_box(self):
        self.fixture = test_tool_actions.ToolActionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.make_box()
        for name in ("box", "native", "gcode", "reactor", "printer", "lifecycle"):
            setattr(self, name, getattr(self.fixture, name))
        for timer in tuple(self.lifecycle._callbacks):
            self.reactor.fire(timer)
        self.box.virtual_sdcard.resume_tnn = None
        self.box.gcode_move.speed = 25
        self.printer.heater.target_temp = 220
        self.printer.heater.min_extrude_temp = 170
        self.native.calls.clear()
        self.gcode.scripts.clear()

    def command(self, name, **parameters):
        return self.fixture.command(name, **parameters)

    def head(self, present):
        self.fixture.fixture.set_head(present)

    def seed(self):
        for slot in "AB":
            for field, value in (("vender", "brand"), ("color_value", "#123456"),
                                 ("material_type", "PLA"), ("remain_len", "500")):
                self.box.box_state.modify_Tn_data(1, value, field, slot)
        self.box.box_state.update_same_material_list(self.box.store.get_material_name)
        self.box.last_cmd = "T1A"
        self.fixture.masks[1] = (2, 0)
        self.head(False)

    def test_all_unconditional_contract_commands_are_registered(self):
        self.make_box()
        matrix = json.loads((Path(__file__).resolve().parents[1] / "docs/contract-matrix.json").read_text(encoding="utf-8"))
        names = {row["name"] for row in matrix["items"] if row["kind"] == "gcode"
                 and row["when"] != "configured switch_pin"}
        self.assertEqual(set(self.gcode.commands), names)
        self.assertEqual(len(names), 101)

    def test_native_runout_schedules_refill_then_completes_real_mapped_tool(self):
        self.make_box()
        self.seed()
        self.box.box_state.Tnn_map["T1C"] = "T1A"
        self.box.print_stats.state = "printing"
        self.native.responses[(1, 0x0A)] = [reply(1, 0x0A, status=0x50, data=bytes((20, 30, 0, 0)))]
        self.assertTrue(self.box.send_native(build_request(1, "GET_BOX_STATE")))
        self.assertEqual(self.box.error, "filament_err")
        self.assertTrue(self.box.is_use_ending_material)
        self.assertTrue(self.box.box_filament_err)
        self.assertNotIn("RESUME", self.gcode.scripts)
        before = set(self.lifecycle._callbacks)
        self.command("BOX_CHECK_MATERIAL_REFILL")
        queued = self.lifecycle._callbacks - before
        self.assertEqual(len(queued), 1)
        self.assertFalse(self.box.is_use_ending_material)
        self.assertFalse(self.box.inside_auto_refill)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.reactor.fire(queued.pop())
        self.assertEqual(self.box.last_cmd, "T1B")
        self.assertEqual(self.box.box_state.Tnn_map["T1A"], "T1B")
        self.assertEqual(self.box.box_state.Tnn_map["T1C"], "T1B")
        self.assertFalse(self.box.inside_auto_refill)
        self.assertFalse(self.box.material_auto_refill_flag)
        self.assertIsNone(self.box.error)
        self.assertEqual(self.gcode.scripts.count("RESUME"), 1)
        self.assertIn((1, 0x10, b"\x02\x04\x00"), self.native.commands())
        saved = json.loads(self.box.store.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["tnn_map"]["T1C"], "T1B")
        self.assertEqual(saved["base_data"]["T1"]["vender"][0], "brand")

    def test_refill_without_candidate_records_error_and_preserves_saved_labels(self):
        self.make_box()
        self.seed()
        self.box.box_state.Tn_data["same_material"] = []
        self.box.runout.material_auto_refill(0)
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertEqual(self.box.error, "filament_err")
        self.assertEqual(self.box.get_status(0)["filament"], 0)
        self.assertEqual(self.box.box_state.tn_save_data["T1"]["vender"][0], "brand")
        self.assertFalse(any(body[3] == 0x10 for body, _, _ in self.native.calls))

    def test_native_runout_without_auto_refill_pauses_and_runs_plain_drain(self):
        self.make_box()
        self.seed()
        self.box.box_state.Tn_data["auto_refill"] = 0
        self.box.print_stats.state = "printing"
        self.native.responses[(1, 0x0A)] = [reply(1, 0x0A, status=0x50, data=bytes((20, 30, 0, 0)))]
        self.assertIs(self.box.send_native(build_request(1, "GET_BOX_STATE")), False)
        self.assertEqual(self.box.print_stats.state, "paused")
        self.assertEqual(self.box.error, "filament_err")
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.gcode.scripts.count("G0 E80.00 F360.00"), 2)
        self.assertIn("PAUSE POST_WORK=0", self.gcode.scripts)
        self.assertIn("M104 S140", self.gcode.scripts)
        self.assertFalse(self.box.inside_extrusion_all_materials)
        self.assertNotIn("RESUME", self.gcode.scripts)

    def test_box_end_drains_residue_then_runs_actual_unload(self):
        self.make_box()
        self.seed()
        self.box.is_use_ending_material = True
        self.command("BOX_END")
        self.assertEqual(self.gcode.scripts.count("G0 E80.00 F360.00"), 2)
        self.assertFalse(self.box.is_use_ending_material)
        self.assertIsNone(self.box.last_cmd)
        self.assertIn((1, 0x11, b"\x01\x01"), self.native.commands())
        self.assertIn("G0 Y350.00 F20000.00", self.gcode.scripts)

    def test_unavailable_refill_sensor_cannot_remap_or_load(self):
        self.make_box()
        self.seed()
        original = self.native.cmd_send_data_with_response
        def event_reply(body, timeout, *args):
            raw = original(body, timeout, *args)
            return reply(1, 8, status=0x30, data=b"\x00" * 4) if body[3] == 8 else raw
        self.native.cmd_send_data_with_response = event_reply
        with self.assertRaisesRegex(RuntimeError, "fresh ordinary"):
            self.box.runout.material_auto_refill(0)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.box_state.Tnn_map["T1A"], "T1A")
        self.assertFalse(any(body[3] == 0x10 for body, _, _ in self.native.calls))

    def test_tightening_waits_for_commanded_e_then_observes_material(self):
        self.make_box()
        self.seed()
        self.box.print_stats.state = "printing"
        self.fixture.masks[1] = (1, 0)
        self.box.box_state.modify_Tn_data(1, "unknown", "vender", "A")
        self.reactor.on_pause = lambda: self.printer.position.__setitem__(3, 120)
        self.box.runout.filament_err_tighten_up_event(0)
        self.assertEqual(self.reactor.pauses, [3])
        self.assertIn((1, 0x0F, b"\x00"), self.native.commands())
        self.assertEqual(self.native.commands()[-1], (1, 0x0F, b"\x01"))
        self.assertEqual(self.box.box_state.Tn_data["filament"], 1)

    def test_queued_runout_callback_does_not_attach_to_replacement(self):
        self.make_box()
        self.seed()
        self.box.runout.schedule_tighten_up()
        timer = next(iter(self.lifecycle._callbacks))
        self.fixture.replace_device(1)
        with self.assertRaises(LifecycleStopped):
            self.reactor.fire(timer)
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])

    def test_error_clear_retraction_false_preserves_reason_and_queue(self):
        self.make_box()
        self.box.error = "retrude_err"
        self.box.error_tnn.update(last_tnn="T1A", tnn="T1B", addr=1)
        self.box.error_next_macro = ["BOX_GO_TO_EXTRUDE_POS"]
        self.native.responses[(1, 4)] = [reply(1, 4, status=0x0D)]
        self.command("BOX_ERROR_CLEAR")
        self.assertEqual(self.box.error, "retrude_err")
        self.assertEqual(self.box.error_next_macro, ["BOX_GO_TO_EXTRUDE_POS"])

    def test_retry_box_loading_runs_real_phases_and_resumes_from_error_state(self):
        self.make_box()
        self.box.print_stats.state = "paused"
        self.box.error = "box_extrude_err"
        self.box.error_tnn.update(last_tnn=None, tnn="T1A", addr=1)
        self.command("BOX_TNN_RETRY_PROCESS")
        self.assertIsNone(self.box.error)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(self.box.operations.active)
        self.assertIn((1, 0x10, b"\x01\x04\x00"), self.native.commands())
        self.assertEqual(self.gcode.scripts[-1], "RESUME")

    def test_retry_failure_preserves_first_nested_reason_and_cools_without_resume(self):
        self.make_box()
        self.box.print_stats.state = "paused"
        self.box.error = "box_extrude_err"
        self.box.error_tnn.update(last_tnn=None, tnn="T1A", addr=1)
        self.fixture.masks[1] = (0, 0)
        self.command("BOX_TNN_RETRY_PROCESS")
        self.assertEqual(self.box.error, "filament_err")
        self.assertNotIn("RESUME", self.gcode.scripts)
        self.assertEqual(self.gcode.scripts[-1], "M104 S140")
        self.assertIsNone(self.box.last_cmd)

    def test_macro_retry_replays_recorded_commands_through_registered_actions(self):
        self.make_box()
        self.box.error = "macro_flush_err"
        self.box.error_tnn.update(tnn="T1A", addr=1)
        self.box.error_next_macro = ["BOX_MATERIAL_CHANGE_FLUSH TNN=T1A"]
        self.command("BOX_TNN_RETRY_PROCESS")
        self.assertIsNone(self.box.error)
        self.assertEqual(self.box.error_next_macro, [])
        self.assertIn("BOX_MATERIAL_CHANGE_FLUSH TNN=T1A", self.gcode.scripts)
        self.assertIn("G0 E80.00 F360.00", self.gcode.scripts)

    def test_powerloss_restores_hints_and_enable_without_loading(self):
        self.make_box()
        mapping = dict(self.box.box_state.Tnn_map, T1A="T1B")
        self.box.store.update("enable", enable=0)
        self.box.store.save_mapping(mapping)
        self.box.store.update("last_cmd", last_cmd="T1B")
        self.command("BOX_POWER_LOSS_RESTORE")
        self.assertEqual(self.box.get_status(0)["enable"], 0)
        self.assertEqual(self.box.box_state.Tnn_map, mapping)
        self.assertEqual(self.box.last_cmd, "T1B")
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.box.box_state.Tn_inner_data["buffer"], None)
        self.assertEqual(self.gcode.scripts, ["BOX_ENABLE_CFS_PRINT ENABLE=0",
                                             "SET_FILAMENT_SENSOR SENSOR=head ENABLE=1"])

    def test_resume_same_content_composes_real_purge_and_restores_speed(self):
        self.make_box()
        self.box.last_cmd = "T1A"
        self.box.virtual_sdcard.resume_tnn = "T1A"
        self.head(True)
        self.command("BOX_RESUME_EXTRUDE")
        self.assertEqual(self.box.gcode_move.speed, 25)
        self.assertIn((1, 4, b"\x01\x00"), self.native.commands())
        self.assertIn("G0 E140.00 F360.00", self.gcode.scripts)
        self.assertIn("SET_PIN PIN=fan0 VALUE=63.75", self.gcode.scripts)
        self.assertNotIn("RESUME", self.gcode.scripts)

    def test_queued_resume_target_clears_on_shutdown_but_does_not_execute_early(self):
        self.make_box()
        self.command("BOX_ADD_TNN", TNN="T1B")
        self.assertEqual(self.box.virtual_sdcard.resume_tnn, "T1B")
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])
        self.lifecycle.shutdown()
        self.assertIsNone(self.box.virtual_sdcard.resume_tnn)

    def test_wait_for_owned_work_stops_at_shutdown(self):
        self.make_box()
        self.box.inside_auto_refill = True
        self.reactor.on_pause = self.lifecycle.shutdown
        with self.assertRaises(LifecycleStopped):
            self.command("WAIT_EXTRUSION_ALL_MATERIALS")
        self.assertEqual(self.reactor.pauses, [1])
        self.assertFalse(self.box.inside_auto_refill)
        self.assertFalse(self.box.inside_extrusion_all_materials)


if __name__ == "__main__":
    unittest.main()
