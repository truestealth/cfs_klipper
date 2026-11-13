"""Exercise coordinator branches with real state and operation ownership."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.firmware import DeviceVersions
from cfs.box import MissingProtocolEffect
from cfs.lifecycle import Lifecycle, LifecycleStopped
from cfs.operations import OperationContext, OperationCancelled
from cfs.state import BoxState
from cfs.toolchange import ToolChange


class ToolChangeTests(unittest.TestCase):
    def setUp(self):
        self.events, self.results = [], {}
        state = BoxState()
        state.modify_Tn_data(0, "connect", uppart="state")
        for address in (1, 2):
            state.modify_Tn_data(address, "connect", "state")
        versions = DeviceVersions({1: "cfs0", 2: "cfs0"})
        for address in (1, 2):
            versions.update(address, b"113" + b"a" * 19)
        self.heater = SimpleNamespace(can_extrude=True)
        self.box = box = SimpleNamespace(
            box_state=state, device_versions=versions, native=object(),
            _binding_generation=1, last_cmd="T1A", last_cmd_tmp=None,
            error=None, error_tnn={}, last_err=None, move_z=None,
            is_use_ending_material=False,
            material_auto_refill_flag=False, heart_process_enable=[True] * 4,
            boxcfg=SimpleNamespace(Tn_extrude=90, tn_final_extrude_len=-2, safe_pos_y=50),
            toolhead=SimpleNamespace(max_velocity=300, max_accel=4000,
                extruder=SimpleNamespace(get_heater=lambda: self.heater)),
            gcode=SimpleNamespace(run_script_from_command=self.action("gcode")),
            _reactor=SimpleNamespace(monotonic=lambda: 10, pause=self.action("pause")))
        box.operations = OperationContext(box)
        lifecycle = SimpleNamespace(_generation=1, box=box, check=lambda generation: None,
                                    record_error=self.record_error)
        box.lifecycle = lifecycle
        box.host_guard = lambda address=None: Lifecycle._report_guard(lifecycle, address)
        box.motion = SimpleNamespace(**{name: self.action(name) for name in (
            "go_to_extrude_pos", "move_to_safe_pos", "save_printer_accel",
            "close_model_fan", "restore_fan", "restore_printer_accel")})
        box.motion.z_down = self.z_down
        box.motion.z_restore = self.z_restore
        box.motion._positions = lambda *names: tuple(getattr(box.boxcfg, name) for name in names)
        box.cutter = SimpleNamespace(strike_clean_box=self.action("strike"))
        for name in ("record_tn_cur_tnn", "record_tn_move_z", "communication_set_box_mode",
                     "quickly_wait_heating", "Tn_Extrude", "nozzle_clean", "move_to_cut",
                     "box_retrude_material", "box_extrude_material", "extruder_extrude",
                     "material_change_flush", "is_use_ending_material_flush"):
            setattr(box, name, self.action(name))
        box.get_purge_in_prime_tower = self.action("tower", False)
        box.get_filament_sensor_detect = self.action("head", False)
        box.get_flush_velocity = self.action("velocity", (5, 6))
        box.get_flush_temp = self.action("temperature", (200, 210, 220))
        box.get_material_target_temp = self.action("target_temperature", 230)
        self.change = ToolChange(box)

    def action(self, name, default=True):
        def action(*args, **kwargs):
            self.events.append((name, args, kwargs))
            result = self.results.get(name, default)
            if isinstance(result, list):
                result = result.pop(0)
            if isinstance(result, Exception):
                raise result
            if callable(result):
                return result()
            return result
        return action

    def record_error(self, reason, context):
        self.events.append(("error", (reason, context), {}))
        self.box.error = reason
        self.box.error_tnn = context

    def z_down(self, **kwargs):
        self.events.append(("z_down", (), kwargs))
        self.box.move_z = (self.box.move_z or 0) + kwargs["distance"]

    def z_restore(self):
        self.events.append(("z_restore", (), {}))
        self.box.move_z = None

    def names(self):
        return [name for name, _, _ in self.events]

    def calls(self, name):
        return [(args, kwargs) for event, args, kwargs in self.events if event == name]

    def test_disabled_and_aggregate_disconnect_do_no_selection_work(self):
        self.box.box_state.modify_Tn_data(0, 0, uppart="enable")
        self.assertIs(self.change.Tn_action("bad"), True)
        self.box.box_state.modify_Tn_data(0, 1, uppart="enable")
        self.box.box_state.modify_Tn_data(0, "disconnect", uppart="state")
        self.assertIs(self.change.Tn_action("bad"), False)
        self.assertEqual(self.events, [])

    def test_requested_logical_tool_is_recorded_before_mapping_validation(self):
        with self.assertRaises(KeyError):
            self.change.Tn_action("missing")
        self.assertEqual(self.names(), ["record_tn_cur_tnn", "tower"])
        self.assertFalse(self.box.operations.active)

    def test_target_disconnection_retains_request_but_stops_motion(self):
        self.assertIs(self.change.Tn_action("T4D"), False)
        self.assertEqual(self.names(), ["record_tn_cur_tnn", "tower"])

    def test_remapping_is_applied_once(self):
        self.box.box_state.Tnn_map.update(T1B="T2C", T2C="T2D")
        self.assertIs(self.change.Tn_action("T1B"), True)
        self.assertEqual(self.calls("box_extrude_material"), [(("T2C",), {})])
        self.assertEqual(self.calls("record_tn_cur_tnn"), [(("T1B",), {})])

    def test_full_tray_sequence_restores_only_at_successful_tail(self):
        self.assertIs(self.change.Tn_action("T1B"), True)
        self.assertEqual(self.names(), ["record_tn_cur_tnn", "tower", "save_printer_accel",
            "close_model_fan", "gcode", "z_down", "record_tn_move_z", "move_to_cut",
            "z_down", "record_tn_move_z", "box_retrude_material", "gcode",
            "go_to_extrude_pos", "strike", "box_extrude_material", "gcode",
            "extruder_extrude", "gcode", "material_change_flush", "restore_fan", "gcode",
            "z_restore", "record_tn_move_z", "gcode", "gcode", "restore_printer_accel", "gcode"])
        self.assertIn((("G91\n G1 E-2.00 F4800\n G90",), {}), self.calls("gcode"))
        self.assertEqual(self.calls("record_tn_move_z"), [((0.8,), {}), ((25.8,), {}), ((0,), {})])
        self.assertEqual(self.calls("strike"), [((), {"wait": False})])
        self.assertFalse(self.box.operations.active)

    def test_tower_skips_tray_height_strike_and_host_flush(self):
        self.results["tower"] = True
        self.assertIs(self.change.Tn_action("T1B"), True)
        self.assertEqual(self.calls("z_down"), [((), {"distance": 0.8})])
        self.assertNotIn("strike", self.names())
        self.assertNotIn("material_change_flush", self.names())
        self.assertIn((("G90\n G0 Y51.00 F24000\n M400",), {}), self.calls("gcode"))

    def test_same_content_uses_equality_and_ignores_normal_mode_heat_failure(self):
        self.box.box_state.Tnn_content["T1B"] = {"num": "A", "addr": 1}
        self.results.update(head=True, communication_set_box_mode=False, quickly_wait_heating=False)
        self.assertIs(self.change.Tn_action("T1B"), True)
        self.assertEqual(self.names(), ["record_tn_cur_tnn", "tower", "head",
            "communication_set_box_mode", "target_temperature", "quickly_wait_heating"])
        self.assertEqual(self.box.last_cmd, "T1A")

    def test_unrelated_error_disables_same_content_shortcut(self):
        self.box.error = "cut_err"
        self.results["head"] = True
        self.assertTrue(self.change.Tn_action("T1A"))
        self.assertIn("box_retrude_material", self.names())

    def test_filament_error_allows_same_content_shortcut(self):
        self.box.error = "filament_err"
        self.results["head"] = True
        self.assertTrue(self.change.Tn_action("T1A"))
        self.assertNotIn("move_to_cut", self.names())
        self.assertEqual(self.box.error, "filament_err")

    def test_head_must_be_exact_true_for_same_content_shortcut(self):
        self.results["head"] = 1
        self.assertTrue(self.change.Tn_action("T1A"))
        self.assertIn("box_extrude_material", self.names())

    def test_continuation_uses_old_old_profile_and_has_distinct_restore_tail(self):
        self.box.box_state.is_continue_print = True
        self.results.update(head=True, Tn_Extrude=None, nozzle_clean=False)
        self.assertTrue(self.change.Tn_action("T1A"))
        self.assertEqual(self.calls("velocity"), [(("T1A", "T1A"), {})])
        self.assertEqual(self.calls("temperature"), [(("T1A", "T1A"), {})])
        self.assertEqual(self.calls("Tn_Extrude"), [((90, 5, 220), {})])
        self.assertEqual(self.names()[-3:], ["nozzle_clean", "move_to_safe_pos", "z_restore"])
        self.assertFalse(self.box.box_state.is_continue_print)
        self.assertNotIn("restore_fan", self.names())

    def test_continuation_false_records_reason_without_clearing_continue_or_restoring_z(self):
        self.box.box_state.is_continue_print = True
        self.box.box_state.extrude_err = True
        self.results.update(head=True, Tn_Extrude=False)
        self.assertIs(self.change.Tn_action("T1A"), False)
        self.assertEqual(self.box.error, "e_err_tn_extrude_err")
        self.assertEqual(self.box.error_tnn, {"vtnn": "T1A"})
        self.assertFalse(self.box.box_state.extrude_err)
        self.assertTrue(self.box.box_state.is_continue_print)
        self.assertEqual(self.box.move_z, 25)
        self.assertFalse(self.box.operations.active)

    def test_continuation_rechecks_head_before_falling_back(self):
        self.box.box_state.is_continue_print = True
        self.results["head"] = [False, True]
        self.assertTrue(self.change.Tn_action("T1A"))
        self.assertEqual(len(self.calls("head")), 2)
        self.assertNotIn("Tn_Extrude", self.names())
        self.assertTrue(self.box.box_state.is_continue_print)

    def test_no_source_no_head_skips_cut_but_still_unloads_unknown_path(self):
        self.box.last_cmd = None
        self.assertTrue(self.change.Tn_action("T1B"))
        self.assertNotIn("move_to_cut", self.names())
        self.assertIn("box_retrude_material", self.names())

    def test_ending_material_stops_before_any_physical_effect(self):
        self.box.is_use_ending_material = True
        with self.assertRaisesRegex(MissingProtocolEffect, "ending-material drain"):
            self.change.Tn_action("T1B")
        self.assertEqual(self.names(), ["record_tn_cur_tnn", "tower"])
        self.assertFalse(self.box.operations.active)

    def test_ending_material_preflight_preserves_supported_same_content_paths(self):
        self.box.is_use_ending_material = True
        self.results["head"] = True
        self.assertTrue(self.change.Tn_action("T1A"))
        self.assertNotIn("is_use_ending_material_flush", self.names())
        self.box.box_state.is_continue_print = True
        self.assertTrue(self.change.Tn_action("T1A"))
        self.assertIn("Tn_Extrude", self.names())
        self.assertFalse(self.box.operations.active)

    def test_exact_false_phase_failures_have_typed_context_and_no_success_restoration(self):
        for phase, reason in (("move_to_cut", "cut_err"), ("box_retrude_material", "retrude_err"),
                              ("box_extrude_material", "box_extrude_err"),
                              ("extruder_extrude", "extruder_extrude_err")):
            with self.subTest(phase=phase):
                self.setUp()
                self.results[phase] = False
                self.assertIs(self.change.Tn_action("T1B"), False)
                self.assertEqual(self.box.error, reason)
                self.assertEqual(self.box.error_tnn, {"vtnn": "T1B"} if phase == "move_to_cut"
                                 else {"last_tnn": "T1A", "tnn": "T1B"})
                self.assertNotIn("restore_fan", self.names())
                self.assertNotIn("z_restore", self.names())
                self.assertFalse(self.box.operations.active)

    def test_none_phase_results_are_not_normalized_to_failure(self):
        self.results.update({name: None for name in ("move_to_cut", "box_retrude_material",
                                                   "box_extrude_material", "extruder_extrude")})
        self.assertTrue(self.change.Tn_action("T1B"))
        self.assertIsNone(self.box.error)

    def test_flush_retries_key845_and_enables_reporting_only_on_last_attempt(self):
        self.box.last_err = '{"code":"key845"}'
        self.results["material_change_flush"] = [False, False, True]
        self.assertTrue(self.change.Tn_action("T1B"))
        self.assertEqual(self.calls("material_change_flush"), [
            (("T1A", "T1B"), {"error_report": value}) for value in (False, False, True)])
        self.assertEqual(self.calls("pause"), [((11,), {}), ((11,), {})])
        self.assertEqual(self.calls("gcode").count((("BOX_MOVE_TO_SAFE_POS\n M400",), {})), 2)

    def test_flush_empty_error_retries_but_unrelated_error_breaks(self):
        self.results["material_change_flush"] = False
        self.assertIs(self.change.Tn_action("T1B"), False)
        self.assertEqual(len(self.calls("material_change_flush")), 3)
        self.assertEqual(self.box.error_tnn, {"last_tnn": "T1A", "tnn": "T1B", "cover": True})
        self.assertNotIn("restore_fan", self.names())
        self.events.clear()
        self.box.last_err = "key999"
        self.assertIs(self.change.Tn_action("T1B"), False)
        self.assertEqual(len(self.calls("material_change_flush")), 1)
        self.assertNotIn("pause", self.names())

    def test_nonfalse_falsy_flush_passes_final_identity_check(self):
        for value in (None, 0, ""):
            with self.subTest(value=value):
                self.setUp()
                self.results["material_change_flush"] = value
                self.assertIs(self.change.Tn_action("T1B"), True)
                self.assertEqual(len(self.calls("material_change_flush")), 3)
                self.assertIsNone(self.box.error)

    def test_exceptions_release_ownership_without_mechanical_cleanup(self):
        self.results["box_extrude_material"] = RuntimeError("load failed")
        with self.assertRaisesRegex(RuntimeError, "load failed"):
            self.change.Tn_action("T1B")
        self.assertNotIn("restore_fan", self.names())
        self.assertNotIn("z_restore", self.names())
        self.assertFalse(self.box.operations.active)

    def test_source_and_target_generations_guard_later_effects(self):
        for address in (1, 2):
            with self.subTest(address=address):
                self.setUp()
                self.results["move_to_cut"] = lambda: self.box.device_versions.invalidate(address)
                with self.assertRaises((LifecycleStopped, OperationCancelled)):
                    self.change.Tn_action("T2B")
                self.assertNotIn("box_retrude_material", self.names())
                self.assertNotIn("restore_fan", self.names())
                self.assertFalse(self.box.operations.active)

    def test_failed_guard_acquisition_releases_new_operation(self):
        original = self.box.host_guard
        def guard(address=None):
            if self.box.operations.active:
                raise RuntimeError("binding unavailable")
            return original(address)
        self.box.host_guard = guard
        with self.assertRaisesRegex(RuntimeError, "binding unavailable"):
            self.change.Tn_action("T1B")
        self.assertFalse(self.box.operations.active)

    def test_cold_heater_skips_both_e_moves_and_refill_resumes_after_restore(self):
        self.heater.can_extrude = False
        self.box.material_auto_refill_flag = True
        self.assertTrue(self.change.Tn_action("T1B"))
        self.assertFalse(any("G1 E" in args[0] for args, _ in self.calls("gcode")))
        self.assertEqual(self.events[-1], ("gcode", ("RESUME",), {}))
        self.assertFalse(self.box.material_auto_refill_flag)


if __name__ == "__main__":
    unittest.main()
