"""Synthetic recovery contracts, including normal False versus exceptions."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.errors import ErrorHandler, _ERRORS
from cfs.firmware import DeviceVersions
from cfs.lifecycle import Lifecycle, LifecycleStopped
from cfs.operations import OperationContext
from cfs.recovery import Recovery, _MACRO_ERRORS
from cfs.state import BoxState


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.events, self.results = [], {}
        self.box = box = SimpleNamespace(
            box_state=BoxState(), error="box_extrude_err", last_err="key835",
            error_tnn=dict(last_tnn="T1A", tnn="T2B", addr=2, vtnn="T2B"),
            error_next_macro=[], material_auto_refill_flag=True,
            last_cmd="T1A", device_versions=DeviceVersions(), _binding_generation=1,
            heart_process_enable=[True] * 4, native=object(),
            print_stats=SimpleNamespace(state="paused"),
            gcode=SimpleNamespace(run_script_from_command=self.action("gcode"),
                                  respond_info=self.action("info")))
        box.operations = OperationContext(box)
        box.lifecycle = SimpleNamespace(_generation=1, box=box, check=self.check,
                                        record_error=self.record)
        box.lifecycle._report_guard = lambda address=None, operation=True: Lifecycle._report_guard(
            box.lifecycle, address, operation)
        box.motion = SimpleNamespace(**{name: self.action(name) for name in (
            "go_to_extrude_pos", "z_restore", "restore_printer_accel")})
        for name in ("communication_set_box_mode", "do_after_extrude", "set_cool_temp",
                     "wait_box_preloading", "Tn_action", "box_retrude_material",
                     "box_extrude_material", "extruder_extrude", "material_change_flush",
                     "move_to_cut", "quickly_wait_heating"):
            setattr(box, name, self.action(name))
        box.if_in_resume = self.action("in_resume", False)
        box.get_filament_sensor_detect = self.action("head", True)
        box.get_material_target_temp = self.action("temperature", 220)
        self.recovery = Recovery(box)

    def check(self, generation=None):
        if generation is not None and generation != self.box.lifecycle._generation:
            raise LifecycleStopped("stopped")

    def action(self, name, default=True):
        def action(*args, **kwargs):
            self.events.append((name, args, kwargs))
            result = self.results.get(name, default)
            if isinstance(result, list):
                result = result.pop(0)
            if isinstance(result, Exception):
                raise result
            return result() if callable(result) else result
        return action

    def record(self, reason, context):
        self.events.append(("record", (reason, context), {}))
        ErrorHandler.record_error(SimpleNamespace(box=self.box), reason, context)

    def names(self):
        return [event[0] for event in self.events]

    def commands(self):
        return [args[0] for name, args, kwargs in self.events if name == "gcode"]

    def test_clear_bookkeeping_preserves_context_identity_and_unrelated_flags(self):
        box = self.box
        context = box.error_tnn
        context["extra"] = 123
        box.error_next_macro = ["X"]
        self.recovery.clear_err()
        self.assertIs(box.error_tnn, context)
        self.assertEqual(context, dict(last_tnn=None, tnn=None, addr=None, vtnn=None, extra=123))
        self.assertIsNone(box.error)
        self.assertIsNone(box.last_err)
        self.assertEqual(box.error_next_macro, [])
        self.assertTrue(box.material_auto_refill_flag)
        self.assertEqual(box.last_cmd, "T1A")

    def test_clear_unknown_keeps_refill_flag(self):
        self.box.error = "future_error"
        self.recovery.error_clear()
        self.assertIsNone(self.box.error)
        self.assertTrue(self.box.material_auto_refill_flag)
        self.assertEqual(self.events, [])

    def test_clear_every_recognized_reason_uses_its_device_policy(self):
        source = {"retrude_err", "macro_retrude_err", "print_end_err"}
        target = {"box_extrude_err", "extruder_extrude_err", "filament_err", "empty_print",
                  "macro_box_extrude_err", "macro_extruder_extrude_err", "state_err"}
        for reason in _ERRORS:
            with self.subTest(reason=reason):
                self.setUp()
                self.box.error = reason
                self.assertIsNone(self.recovery.error_clear())
                expected = [("communication_set_box_mode", (1 if reason in source else 2, "IDLE"), {})]
                self.assertEqual(self.events, expected if reason in source | target else [])
                self.assertIsNone(self.box.error)
                self.assertFalse(self.box.material_auto_refill_flag)

    def test_clear_exact_false_only_stops_retract_and_print_end(self):
        for reason in ("retrude_err", "macro_retrude_err", "print_end_err", "box_extrude_err",
                       "extruder_extrude_err", "filament_err", "empty_print", "state_err"):
            for result in (False, None, 0):
                with self.subTest(reason=reason, result=result):
                    self.setUp()
                    self.box.error = reason
                    self.results["communication_set_box_mode"] = result
                    actual = self.recovery.error_clear()
                    stops = result is False and reason in ("retrude_err", "macro_retrude_err", "print_end_err")
                    self.assertIs(actual, False if stops else None)
                    self.assertEqual(self.box.error, reason if stops else None)

    def test_clear_missing_target_and_falsey_retract_address(self):
        for reason in ("box_extrude_err", "extruder_extrude_err", "filament_err", "empty_print",
                       "retrude_err", "macro_retrude_err", "state_err"):
            with self.subTest(reason=reason):
                self.setUp()
                self.box.error = reason
                self.box.error_tnn.update(tnn=None, last_tnn=None, addr=None)
                self.recovery.error_clear()
                self.assertEqual(self.events, [])
                self.assertIsNone(self.box.error)

    def test_clear_zero_address_has_distinct_state_and_retract_policy(self):
        self.box.error_tnn.update(addr=0, last_tnn=None)
        self.box.error = "retrude_err"
        self.recovery.error_clear()
        self.assertEqual(self.events, [])
        self.box.error_tnn["addr"] = 0
        self.box.error = "state_err"
        self.recovery.error_clear()
        self.assertEqual(self.events[0][1], (0, "IDLE"))

    def test_clear_print_end_missing_source_keeps_original_lookup_error(self):
        self.box.error = "print_end_err"
        self.box.error_tnn["last_tnn"] = None
        with self.assertRaises(KeyError):
            self.recovery.error_clear()
        self.assertEqual(self.box.error, "print_end_err")
        self.assertFalse(self.box.material_auto_refill_flag)

    def test_command_wrappers_discard_normal_false(self):
        self.recovery.error_clear = lambda: False
        self.recovery.Tnn_retry_process = lambda: False
        self.assertIsNone(self.recovery.cmd_error_clear(object()))
        self.assertIsNone(self.recovery.cmd_Tnn_retry_process(object()))

    def test_dispatch_initial_none_still_restores_and_resumes(self):
        self.recovery.clear_err()
        self.recovery.Tnn_retry_process()
        self.assertEqual(self.names(), ["in_resume", "do_after_extrude", "gcode"])
        self.assertEqual(self.commands(), ["RESUME"])

    def test_dispatch_resume_guard_preserves_flags_and_queue(self):
        self.results["in_resume"] = "busy"
        self.box.error_next_macro = ["A"]
        self.recovery.Tnn_retry_process()
        self.assertEqual(self.names(), ["info", "in_resume"])
        self.assertTrue(self.box.material_auto_refill_flag)
        self.assertEqual(self.box.last_err, "key835")
        self.assertEqual(self.box.error_next_macro, ["A"])

    def test_dispatch_malformed_context_raises_before_resume_guard(self):
        del self.box.error_tnn["tnn"]
        self.results["in_resume"] = True
        with self.assertRaises(KeyError):
            self.recovery.Tnn_retry_process()
        self.assertNotIn("in_resume", self.names())

    def test_dispatch_last_error_and_zero_wait_policy(self):
        for message in ("key835", "prefix key847 suffix", ""):
            with self.subTest(message=message):
                self.setUp()
                self.box.error = "state_err"
                self.box.error_tnn["addr"] = 0
                self.box.last_err = message
                self.results["wait_box_preloading"] = False
                self.recovery.Tnn_retry_process()
                self.assertEqual(self.box.last_err, None if message == "key835" else message)
                self.assertIn(("wait_box_preloading", (), {"addr": 0}), self.events)
                self.assertEqual(self.names()[-2:], ["do_after_extrude", "set_cool_temp"])

    def test_dispatch_each_leaf_ignores_its_false_result(self):
        reasons = ("retrude_err", "box_extrude_err", "extruder_extrude_err", "flush_err",
                   "filament_err", "empty_print", "print_end_err")
        for reason in reasons:
            with self.subTest(reason=reason):
                self.setUp()
                self.box.error = reason
                setattr(self.recovery, reason + "_retry_process", self.action("leaf", False))
                self.recovery.Tnn_retry_process()
                self.assertEqual(self.names()[-3:], ["leaf", "do_after_extrude", "set_cool_temp"])
                self.assertEqual(self.commands(), ["WAIT_EXTRUSION_ALL_MATERIALS"] if reason == "filament_err" else [])
                self.assertEqual("go_to_extrude_pos" in self.names(), reason == "empty_print")

    def test_dispatch_macro_membership_is_exact(self):
        for reason in _MACRO_ERRORS | {"macro_extrusion_all_materials_err"}:
            with self.subTest(reason=reason):
                self.setUp()
                self.box.error = reason
                self.recovery.macro_err_retry_process = self.action("macro", False)
                self.recovery.Tnn_retry_process()
                self.assertEqual("macro" in self.names(), reason in _MACRO_ERRORS)

    def test_cut_retry_allows_child_operation_ownership_and_uses_saved_virtual(self):
        self.box.error = "cut_err"
        self.results["Tn_action"] = lambda: setattr(self.box.operations, "_serial", 9)
        self.recovery.Tnn_retry_process()
        self.assertIn(("Tn_action", ("T2B",), {}), self.events)
        self.assertIsNone(self.box.error)
        self.assertEqual(self.commands(), ["RESUME"])

    def test_dispatch_post_action_does_not_resume_nonpaused_print(self):
        self.recovery.clear_err()
        self.box.print_stats.state = "printing"
        self.recovery.Tnn_retry_process()
        self.assertEqual(self.commands(), [])
        self.assertNotIn("set_cool_temp", self.names())

    def test_dispatch_uses_stored_error_even_when_leaf_returns_false(self):
        def leaf():
            self.recovery.clear_err()
            return False
        self.recovery.box_extrude_err_retry_process = leaf
        self.recovery.Tnn_retry_process()
        self.assertEqual(self.commands(), ["RESUME"])
        self.assertNotIn("set_cool_temp", self.names())

    def test_dispatch_selects_error_after_preload_wait(self):
        self.results["wait_box_preloading"] = lambda: setattr(self.box, "error", "empty_print")
        self.recovery.empty_print_retry_process = self.action("empty", False)
        self.recovery.Tnn_retry_process()
        self.assertIn("empty", self.names())
        self.assertNotIn("box_extrude_material", self.names())

    def test_dispatch_preload_exception_has_no_restore_or_cooling_tail(self):
        self.results["wait_box_preloading"] = ValueError("wait failed")
        with self.assertRaises(ValueError):
            self.recovery.Tnn_retry_process()
        self.assertNotIn("do_after_extrude", self.names())
        self.assertNotIn("set_cool_temp", self.names())

    def test_macro_address_priority_and_empty_queue_barrier(self):
        for source, target, address, expected in (("T1A", "T2B", 3, 2), ("T1A", None, 3, 1),
                                                  (None, None, 3, 3), (None, None, 0, None)):
            with self.subTest(source=source, target=target):
                self.setUp()
                self.box.error_tnn.update(last_tnn=source, tnn=target, addr=address)
                self.assertIsNone(self.recovery.macro_err_retry_process())
                modes = [args for name, args, kwargs in self.events if name == "communication_set_box_mode"]
                self.assertEqual(modes, [(expected, "IDLE")] if expected else [])
                self.assertEqual(self.commands(), ["M400"])

    def test_macro_snapshot_replays_all_commands_even_when_error_reappears(self):
        self.box.error_next_macro = ["FIRST", "SECOND"]
        def replay():
            self.box.error_next_macro.append("NEW")
            self.box.error = "macro_cut_err"
            return False
        self.results["gcode"] = replay
        self.assertIsNone(self.recovery.macro_err_retry_process())
        self.assertEqual(self.commands(), ["FIRST", "SECOND", "M400"])
        self.assertEqual(self.box.error_next_macro, ["NEW"] * 3)

    def test_macro_idle_false_preserves_queue_and_error(self):
        self.box.error_next_macro = ["FIRST", "SECOND"]
        self.results["communication_set_box_mode"] = False
        self.assertIs(self.recovery.macro_err_retry_process(), False)
        self.assertEqual(self.box.error_next_macro, ["FIRST", "SECOND"])
        self.assertEqual(self.box.error, "box_extrude_err")
        self.assertEqual(self.commands(), [])

    def test_macro_exception_stops_without_final_barrier(self):
        self.box.error_next_macro = ["FIRST", "SECOND"]
        self.results["gcode"] = ValueError("macro failed")
        with self.assertRaises(ValueError):
            self.recovery.macro_err_retry_process()
        self.assertEqual(self.commands(), ["FIRST"])
        self.assertIsNone(self.box.error)

    def test_print_end_truthy_source_and_exact_false(self):
        for source, result in ((None, False), ("T1A", False), ("T1A", 0)):
            with self.subTest(source=source, result=result):
                self.setUp()
                self.box.error_tnn["last_tnn"] = source
                self.results["communication_set_box_mode"] = result
                stops = source is not None and result is False
                self.assertIs(self.recovery.print_end_err_retry_process(), False if stops else None)
                self.assertEqual(self.commands(), [] if stops else ["END_PRINT"])

    def test_empty_print_ignores_mode_and_heat_false(self):
        self.results.update(communication_set_box_mode=False, quickly_wait_heating=False)
        self.assertIsNone(self.recovery.empty_print_retry_process())
        self.assertEqual(self.names(), ["communication_set_box_mode"] * 2 + ["temperature", "quickly_wait_heating"])
        self.assertEqual(self.events[1], ("communication_set_box_mode", (2, "PRINT"), {"num": "B"}))
        self.assertIsNone(self.box.error)

    def test_empty_print_missing_target_clears(self):
        self.box.error_tnn["tnn"] = None
        self.assertIsNone(self.recovery.empty_print_retry_process())
        self.assertIsNone(self.box.error)
        self.assertEqual(self.events, [])

    def test_flush_only_has_distinct_restore_tail_and_ignores_modes(self):
        self.results["communication_set_box_mode"] = False
        self.assertIs(self.recovery.flush_err_retry_process(), True)
        self.assertEqual(self.names(), ["go_to_extrude_pos", "communication_set_box_mode",
            "communication_set_box_mode", "material_change_flush", "z_restore", "gcode", "gcode", "restore_printer_accel"])
        self.assertEqual(self.commands(), ["G92 E0", "G90"])

    def test_flush_only_missing_target_moves_before_lookup_error(self):
        self.box.error_tnn.update(last_tnn=None, tnn=None)
        with self.assertRaises(KeyError):
            self.recovery.flush_err_retry_process()
        self.assertEqual(self.names(), ["go_to_extrude_pos"])

    def test_material_leaf_sequences_and_normal_none_completion(self):
        expected = {
            "filament_err": ["communication_set_box_mode", "box_extrude_material", "extruder_extrude", "material_change_flush"],
            "box_extrude_err": ["communication_set_box_mode", "go_to_extrude_pos", "box_extrude_material", "go_to_extrude_pos", "extruder_extrude", "material_change_flush"],
            "extruder_extrude_err": ["communication_set_box_mode", "extruder_extrude", "material_change_flush"],
            "retrude_err": ["communication_set_box_mode", "head", "move_to_cut", "go_to_extrude_pos", "box_retrude_material", "box_extrude_material", "extruder_extrude", "material_change_flush"],
        }
        for reason, sequence in expected.items():
            with self.subTest(reason=reason):
                self.setUp()
                self.results.update({name: None for name in sequence if name != "head"})
                self.assertIs(getattr(self.recovery, reason + "_retry_process")(), True)
                self.assertEqual(self.names(), sequence + ["gcode"] * 3)
                self.assertEqual(self.commands(), ["G92 E0", "G90", "M400"])
                self.assertIsNone(self.box.error)

    def test_every_material_stage_failure_records_exact_phase_and_stops_tail(self):
        stages = {"box_retrude_material": "retrude_err", "box_extrude_material": "box_extrude_err",
                  "extruder_extrude": "extruder_extrude_err", "material_change_flush": "flush_err"}
        routes = {"retrude_err": tuple(stages), "filament_err": tuple(stages)[1:],
                  "box_extrude_err": tuple(stages)[1:], "extruder_extrude_err": tuple(stages)[2:],
                  "flush_err": ("material_change_flush",)}
        for route, failures in routes.items():
            for stage in failures:
                with self.subTest(route=route, stage=stage):
                    self.setUp()
                    self.results[stage] = False
                    self.assertIs(getattr(self.recovery, route + "_retry_process")(), False)
                    self.assertEqual(self.box.error, stages[stage])
                    self.assertEqual(self.box.error_tnn, dict(last_tnn="T1A", tnn="T2B", addr=None, vtnn=None))
                    self.assertEqual(self.names()[-2:], [stage, "record"])
                    self.assertEqual(self.commands(), [])

    def test_material_idle_false_preserves_error_and_context(self):
        for reason in ("filament_err", "box_extrude_err", "extruder_extrude_err", "retrude_err"):
            with self.subTest(reason=reason):
                self.setUp()
                self.results["communication_set_box_mode"] = False
                self.assertIs(getattr(self.recovery, reason + "_retry_process")(), False)
                self.assertEqual(self.names(), ["communication_set_box_mode"])
                self.assertEqual(self.box.error_tnn["tnn"], "T2B")

    def test_material_missing_targets_preserve_error_and_exception_distinction(self):
        for reason in ("filament_err", "box_extrude_err", "retrude_err", "extruder_extrude_err"):
            with self.subTest(reason=reason):
                self.setUp()
                self.box.error_tnn.update(last_tnn=None, tnn=None)
                self.assertIsNone(getattr(self.recovery, reason + "_retry_process")())
                self.assertEqual(self.box.error, "box_extrude_err")
                self.assertEqual(self.events, [])
        self.box.error_tnn["last_tnn"] = "T1A"
        with self.assertRaises(KeyError):
            self.recovery.extruder_extrude_err_retry_process()

    def test_retract_saved_address_and_absent_head_skip_cut_and_position(self):
        self.box.error_tnn.update(last_tnn=None, addr=3)
        self.results["head"] = False
        self.assertTrue(self.recovery.retrude_err_retry_process())
        self.assertEqual(self.events[0][1], (3, "IDLE"))
        self.assertNotIn("move_to_cut", self.names())
        self.assertNotIn("go_to_extrude_pos", self.names())
        self.assertIn(("box_retrude_material", (), {}), self.events)

    def test_retract_cut_false_is_ignored(self):
        self.results["move_to_cut"] = False
        self.assertTrue(self.recovery.retrude_err_retry_process())
        self.assertIn("box_retrude_material", self.names())

    def test_numeric_zero_stage_results_are_not_false_singleton(self):
        self.results.update(box_retrude_material=0, box_extrude_material=0,
                            extruder_extrude=0, material_change_flush=0)
        self.assertIs(self.recovery.retrude_err_retry_process(), True)
        self.assertEqual(self.commands(), ["G92 E0", "G90", "M400"])

    def test_stage_exception_propagates_with_cleared_error_and_no_final_motion(self):
        self.results["box_extrude_material"] = ValueError("stage exception")
        with self.assertRaises(ValueError):
            self.recovery.Tnn_retry_process()
        self.assertIsNone(self.box.error)
        self.assertNotIn("do_after_extrude", self.names())
        self.assertEqual(self.commands(), [])

    def test_recovery_stops_after_source_or_target_identity_change(self):
        for address in (1, 2):
            with self.subTest(address=address):
                self.setUp()
                self.results["box_extrude_material"] = lambda: self.box.device_versions.invalidate(address)
                with self.assertRaises(LifecycleStopped):
                    self.recovery.box_extrude_err_retry_process()
                self.assertEqual(self.names()[-1], "box_extrude_material")
                self.assertNotIn("extruder_extrude", self.names())

    def test_recovery_stops_after_lifecycle_or_binding_change(self):
        for target, field in (("lifecycle", "_generation"), ("box", "_binding_generation")):
            with self.subTest(target=target):
                self.setUp()
                owner = self.box.lifecycle if target == "lifecycle" else self.box
                self.results["communication_set_box_mode"] = lambda: setattr(owner, field, 2)
                with self.assertRaises(LifecycleStopped):
                    self.recovery.error_clear()
                self.assertEqual(self.box.error, "box_extrude_err")

    def test_macro_stops_before_next_command_when_identity_changes(self):
        self.box.error_next_macro = ["FIRST", "SECOND"]
        self.results["gcode"] = lambda: self.box.device_versions.invalidate(1)
        with self.assertRaises(LifecycleStopped):
            self.recovery.macro_err_retry_process()
        self.assertEqual(self.commands(), ["FIRST"])

    def test_cut_retry_guards_mapped_virtual_target(self):
        self.box.error = "cut_err"
        self.box.error_tnn.update(tnn=None, vtnn="T4D")
        self.box.box_state.Tnn_map["T4D"] = "T3A"
        self.results["Tn_action"] = lambda: self.box.device_versions.invalidate(3)
        with self.assertRaises(LifecycleStopped):
            self.recovery.Tnn_retry_process()
        self.assertNotIn("do_after_extrude", self.names())


if __name__ == "__main__":
    unittest.main()
