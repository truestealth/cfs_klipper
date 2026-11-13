"""Synthetic runout sequences, deferred ownership and refill error tails."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.firmware import DeviceVersions
from cfs.lifecycle import Lifecycle, LifecycleStopped
from cfs.operations import OperationContext
from cfs.runout import Runout
from cfs.state import BoxState


class RunoutTests(unittest.TestCase):
    def setUp(self):
        self.events, self.results, self.callbacks = [], {}, []
        self.position = [0, 0, 30, 0]
        self.running = True
        state = BoxState()
        versions = DeviceVersions({1: "cfs0", 2: "cfs0"})
        for address in (1, 2):
            versions.update(address, b"113" + b"a" * 19)
            state.modify_Tn_data(address, "connect", "state")
        self.box = box = SimpleNamespace(
            box_state=state, device_versions=versions, _binding_generation=1,
            last_cmd="T1A", last_cmd_tmp=None, error=None, last_err=None,
            error_tnn={"addr": None}, error_next_macro=[], move_z=None,
            is_use_ending_material=True, box_filament_err=True,
            inside_auto_refill=False, inside_extrusion_all_materials=False,
            material_auto_refill_flag=False, heart_process_enable=[True] * 4,
            native=object(), print_stats=SimpleNamespace(state="printing"),
            boxcfg=SimpleNamespace(Tn_extrude_velocity=[120, 90]),
            store=SimpleNamespace(get_material_name=lambda kind: "name"),
            toolhead=SimpleNamespace(get_position=self.get_position),
            gcode=SimpleNamespace(run_script_from_command=self.script,
                                  respond_info=self.action("info")),
            _reactor=SimpleNamespace(monotonic=lambda: 10, pause=self.action("pause")))
        box.operations = OperationContext(box)
        lifecycle = SimpleNamespace(_generation=1, box=box, check=self.check,
                                    record_error=self.record_error,
                                    report_error=self.action("report"),
                                    schedule=self.callbacks.append)
        lifecycle._report_guard = lambda address=None, operation=True: Lifecycle._report_guard(
            lifecycle, address, operation)
        box.lifecycle = lifecycle
        box.host_guard = lifecycle._report_guard
        box.motion = SimpleNamespace(go_to_extrude_pos=self.action("tray"), z_down=self.z_down)
        for name in ("quickly_wait_heating", "record_tn_move_z", "nozzle_clean",
                     "set_cool_temp", "communication_set_box_mode",
                     "communication_get_box_state", "set_head_runout_enabled",
                     "communication_tighten_up_enable", "update_Tnn_map"):
            setattr(box, name, self.action(name))
        box.get_filament_sensor_detect = self.action("head", False)
        box.get_material_target_temp = self.action("temperature", 220)
        box.get_material_max_extrusion_speed = self.action("speed", None)
        box.check_and_extrude = self.action("extrude", True)
        box.check_connect = self.action("connected", False)
        box.is_material_available = self.action("available", True)
        box.clear_err = self.clear_error
        box.retraction = SimpleNamespace(_sample=self.sample)
        self.runout = Runout(box)

    def check(self, generation=None):
        if not self.running:
            raise LifecycleStopped("stopped")

    def action(self, name, default=None):
        def action(*args, **kwargs):
            self.events.append((name, args, kwargs))
            result = self.results.get(name, default)
            if isinstance(result, list):
                result = result.pop(0)
            if isinstance(result, Exception):
                raise result
            return result() if callable(result) else result
        return action

    def get_position(self):
        return self.position[:]

    def z_down(self, **kwargs):
        self.events.append(("z_down", (), kwargs))
        self.box.move_z = kwargs["distance"]

    def clear_error(self):
        self.events.append(("clear", (), {}))
        self.box.error = self.box.last_err = None
        self.box.error_tnn = {"addr": None}

    def record_error(self, reason, context):
        self.events.append(("error", (reason, context), {}))
        self.box.error, self.box.error_tnn = reason, dict(context)

    def script(self, command):
        result = self.action("script")(command)
        if command.startswith("BOX_MODIFY_TN "):
            name, target = command.split()[1].split("=")
            self.box.box_state.Tnn_map[name] = target
        return result

    def sample(self, call, address, command, data):
        return call(self.action("sample", 0), address, data)

    def calls(self, name):
        return [(args, kwargs) for event, args, kwargs in self.events if event == name]

    def candidates(self, tools):
        state = self.box.box_state
        for tool in ["T1A"] + tools:
            content = state.get_Tnn_content(tool)
            for field, value in (("material_type", "PLA"), ("color_value", "#112233"),
                                 ("vender", "vendor")):
                state.modify_Tn_data(content["addr"], value, field, content["num"])
        state.Tn_data["same_material"] = [["PLA", "#112233", tools, "PLA"]]

    def test_connection_helper_and_state_request_retain_normal_results(self):
        self.assertTrue(self.runout.check_connect(1))
        self.assertFalse(self.runout.check_connect(3))
        requests = []
        self.box.send_native = lambda request: requests.append(request)
        self.assertIsNone(self.runout.communication_get_box_state(1))
        self.assertEqual(requests[0].command, 10)
        self.assertEqual(requests[0].data, b"")

    def test_cooling_saves_hot_paused_target_before_setting_140(self):
        self.box.print_stats.state = "paused"
        heater = SimpleNamespace(target_temp=220, min_extrude_temp=170)
        self.box.toolhead.extruder = SimpleNamespace(get_heater=lambda: heater)
        self.runout.set_cool_temp()
        self.assertEqual(self.calls("script"), [
            (("SET_GCODE_VARIABLE MACRO=PRINTER_PARAM VARIABLE=hotend_temp VALUE=220",), {}),
            (("M104 S140",), {})])

    def test_cooling_cold_pause_skips_save_printing_skips_all_idle_disables(self):
        self.box.print_stats.state = "paused"
        heater = SimpleNamespace(target_temp=0, min_extrude_temp=170)
        self.box.toolhead.extruder = SimpleNamespace(get_heater=lambda: heater)
        self.runout.set_cool_temp()
        self.box.print_stats.state = "printing"
        self.runout.set_cool_temp()
        self.box.print_stats.state = "standby"
        self.runout.set_cool_temp()
        self.assertEqual(self.calls("script"), [(("M104 S140",), {}), (("M104 S0",), {})])

    def test_update_map_accepts_single_candidate_despite_stock_log_text(self):
        self.candidates(["T2B"])
        self.box.box_state.Tnn_map["T1D"] = "T1A"
        self.runout.update_Tnn_map()
        self.assertEqual(self.calls("script"), [(("BOX_MODIFY_TN T1A=T2B",), {}),
                                               (("BOX_MODIFY_TN T1D=T2B",), {})])
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(self.box.material_auto_refill_flag)

    def test_update_map_no_available_peer_has_no_side_effects(self):
        self.candidates(["T2B"])
        self.results["available"] = False
        self.runout.update_Tnn_map()
        self.assertFalse(self.calls("script"))
        self.assertEqual(self.box.last_cmd, "T1A")

    def test_tightening_stops_if_source_changes_during_idle(self):
        self.results["sample"] = 0
        self.results["pause"] = lambda: self.position.__setitem__(3, 120)
        self.results["communication_set_box_mode"] = lambda: setattr(self.box, "last_cmd", "T2A")
        with self.assertRaises(LifecycleStopped):
            self.runout.filament_err_tighten_up_event(0)
        self.assertFalse(self.calls("communication_tighten_up_enable"))

    def test_tightening_paused_heartbeat_does_not_use_cached_material(self):
        self.candidates(["T1A"])
        self.results["sample"] = 0
        self.box.heart_process_enable[0] = False
        calls = []
        def pause():
            calls.append(1)
            if len(calls) == 1:
                self.position[3] = 120
            else:
                self.box.print_stats.state = "complete"
        self.results["pause"] = pause
        self.runout.filament_err_tighten_up_event(0)
        self.assertEqual(len(self.calls("sample")), 1)
        self.assertEqual(self.calls("communication_tighten_up_enable"), [((1, "ENABLE"), {})])

    def test_drain_adds_one_segment_after_exact_false_and_uses_default_speed(self):
        self.assertIs(self.runout.extrusion_all_materials(), True)
        self.assertEqual(self.calls("extrude"), [((80, 120), {"wait": False})] * 2)
        self.assertEqual(self.calls("head"), [((), {})])
        self.assertEqual(self.calls("communication_set_box_mode"), [((1, "IDLE"), {})])
        self.assertEqual(self.box.last_cmd, "T1A")

    def test_drain_falsey_zero_is_not_exact_false_and_cap_is_normal_tail(self):
        self.results["head"] = 0
        self.assertIs(self.runout.extrusion_all_materials(), True)
        self.assertEqual(len(self.calls("extrude")), 38)
        self.assertEqual(len(self.calls("head")), 37)
        self.assertEqual(len(self.calls("set_cool_temp")), 1)

    def test_drain_low_z_requires_present_head_before_lift_and_persistence(self):
        self.position[2] = 5
        self.results["head"] = [True, False]
        self.results["speed"] = 3.5
        self.runout.extrusion_all_materials()
        self.assertEqual(self.calls("z_down"), [((), {"distance": 25, "absolute": True, "wait": True})])
        self.assertEqual(self.calls("record_tn_move_z"), [((25,), {})])
        self.assertEqual(self.calls("extrude")[0][0], (80, 210))

    def test_drain_queries_connected_addresses_before_exact_false_tail(self):
        self.results["connected"] = [False, True, False, True]
        self.results["extrude"] = False
        self.assertIs(self.runout.extrusion_all_materials(), False)
        self.assertEqual(self.calls("communication_get_box_state"), [((2,), {}), ((4,), {})])
        self.assertEqual(self.calls("pause"), [((0.2,), {}), ((0.2,), {})])
        self.assertFalse(self.calls("nozzle_clean"))
        self.assertFalse(self.calls("set_cool_temp"))

    def test_drain_none_extrusion_result_continues(self):
        self.results["extrude"] = None
        self.assertIs(self.runout.extrusion_all_materials(), True)
        self.assertEqual(len(self.calls("nozzle_clean")), 2)

    def test_drain_stops_on_source_device_replacement_during_heat(self):
        self.results["quickly_wait_heating"] = lambda: self.box.device_versions.invalidate(1)
        with self.assertRaises(LifecycleStopped):
            self.runout.extrusion_all_materials()
        self.assertFalse(self.calls("extrude"))

    def test_ending_wrapper_clears_vendor_map_error_and_source_on_normal_result(self):
        self.candidates(["T1A", "T2B"])
        self.runout.extrusion_all_materials = lambda: None
        self.assertIsNone(self.runout.is_use_ending_material_flush(err="z_err_extrusion_all_materials_err", vtnn="T1B"))
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertIsNone(self.box.last_cmd)
        self.assertFalse(self.box.is_use_ending_material)
        self.assertFalse(self.box.box_filament_err)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "vender", "A"), "none")
        self.assertEqual(self.calls("update_Tnn_map"), [((), {})])

    def test_ending_failure_retains_source_and_records_caller_context(self):
        self.runout.extrusion_all_materials = lambda: False
        self.box.box_state.extrude_err = True
        self.assertIs(self.runout.is_use_ending_material_flush(err="z_err_extrusion_all_materials_err", vtnn="T2B"), False)
        self.assertEqual(self.box.error, "z_err_extrusion_all_materials_err")
        self.assertEqual(self.box.error_tnn, {"vtnn": "T2B"})
        self.assertFalse(self.box.box_state.extrude_err)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertTrue(self.box.is_use_ending_material)

    def test_drain_command_failure_and_success_have_different_bookkeeping(self):
        self.runout.extrusion_all_materials = lambda: False
        self.runout.cmd_extrusion_all_materials(None)
        self.assertEqual(self.box.error, "macro_extrusion_all_materials_err")
        self.assertEqual(self.box.last_cmd, "T1A")
        self.runout.extrusion_all_materials = lambda: None
        self.runout.cmd_extrusion_all_materials(None)
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.calls("set_head_runout_enabled"), [((False,), {})])

    def test_refill_check_missing_error_address_does_nothing(self):
        self.box.error = "filament_err"
        self.runout.check_material_refill()
        self.assertFalse(self.callbacks)
        self.assertEqual(self.events, [])

    def test_refill_check_clears_error_idles_and_schedules_once(self):
        self.box.error, self.box.error_tnn = "filament_err", {"addr": 2}
        self.runout.check_material_refill()
        self.assertEqual(self.calls("communication_set_box_mode"), [((2, "IDLE"), {})])
        self.assertIsNone(self.box.error)
        self.assertFalse(self.box.is_use_ending_material)
        self.assertEqual(len(self.callbacks), 1)

    def test_refill_check_without_ending_only_disables_enabled_box_sensor(self):
        self.box.is_use_ending_material = False
        self.runout.check_material_refill()
        self.assertEqual(self.calls("set_head_runout_enabled"), [((False,), {})])
        self.assertFalse(self.callbacks)

    def test_deferred_refill_drops_changed_source(self):
        self.runout.check_material_refill()
        self.box.last_cmd = "T2B"
        self.events.clear()
        self.callbacks[0](1)
        self.assertEqual(self.events, [])

    def test_deferred_tightening_rejects_replaced_device(self):
        self.runout.schedule_tighten_up()
        self.box.device_versions.invalidate(1)
        with self.assertRaises(LifecycleStopped):
            self.callbacks[0](1)
        self.assertFalse(self.calls("sample"))

    def test_disabled_refill_leaves_source_and_error(self):
        self.box.box_state.Tn_data["auto_refill"] = 0
        self.box.error = "filament_err"
        self.runout.material_auto_refill(0)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.error, "filament_err")
        self.assertEqual(self.box.box_state.Tn_data["filament"], 0)

    def test_refill_missing_match_reports_and_records_recoverable_context(self):
        self.runout.material_auto_refill(0)
        self.assertEqual(self.calls("info"), [(("no identical supplies",), {})])
        self.assertEqual(self.box.error, "filament_err")
        self.assertEqual(self.box.error_tnn, {"last_tnn": "T1A", "tnn": "T1A", "addr": 1, "filament_sensor_enable": False})
        self.assertIsNone(self.box.last_cmd)

    def test_refill_unavailable_candidates_report_distinct_failure(self):
        self.box.last_cmd_tmp = "T4D"
        self.candidates(["T1A", "T2B"])
        self.results["available"] = False
        self.runout.material_auto_refill(0)
        self.assertEqual(self.calls("available"), [(("T1A",), {}), (("T2B",), {})])
        self.assertEqual(self.calls("info"), [(("no tray with ingredients found",), {})])
        self.assertEqual(self.box.last_cmd_tmp, "T4D")
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.error_tnn["last_tnn"], "T4D")
        self.assertEqual(self.box.error_tnn["tnn"], "T4D")

    def test_refill_selects_first_available_and_updates_every_source_mapping(self):
        self.candidates(["T1A", "T2B", "T2C"])
        self.box.box_state.Tnn_map["T1D"] = "T1A"
        self.results["available"] = [False, True]
        self.runout.material_auto_refill(0)
        self.assertEqual(self.calls("script"), [(("BOX_MODIFY_TN T1A=T2B",), {}),
                                               (("BOX_MODIFY_TN T1D=T2B",), {}), (("T1A",), {})])
        self.assertTrue(self.box.material_auto_refill_flag)
        self.assertFalse(self.box.inside_auto_refill)
        self.assertIsNone(self.box.last_cmd)

    def test_refill_without_resulting_alias_returns_after_source_bookkeeping(self):
        self.candidates(["T2B"])
        self.box.box_state.Tnn_map = {"T1C": "T3D"}
        self.assertIsNone(self.runout.material_auto_refill(0))
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertFalse(self.box.material_auto_refill_flag)
        self.assertFalse(self.box.inside_auto_refill)
        self.assertFalse(self.calls("script"))

    def test_refill_does_not_mutate_metadata_after_unavailable_observation(self):
        self.candidates(["T2B"])
        self.results["available"] = RuntimeError("no fresh observation")
        with self.assertRaisesRegex(RuntimeError, "fresh"):
            self.runout.material_auto_refill(0)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.box_state.get_Tn_data(1, "vender", "A"), "vendor")
        self.assertFalse(self.calls("script"))

    def test_refill_source_change_during_candidate_query_stops_before_remapping(self):
        self.candidates(["T2B"])
        def available():
            self.box.last_cmd = "T3C"
            return True
        self.results["available"] = available
        with self.assertRaisesRegex(LifecycleStopped, "refill source"):
            self.runout.material_auto_refill(0)
        self.assertEqual(self.box.last_cmd, "T3C")
        self.assertIsNone(self.box.last_cmd_tmp)
        self.assertFalse(self.calls("script"))

    def test_refill_source_change_during_alias_write_prevents_child_load(self):
        self.candidates(["T2B"])
        self.results["script"] = lambda: setattr(self.box, "last_cmd", "T3C")
        with self.assertRaisesRegex(LifecycleStopped, "refill source"):
            self.runout.material_auto_refill(0)
        self.assertEqual(len(self.calls("script")), 1)
        self.assertFalse(self.box.material_auto_refill_flag)
        self.assertFalse(self.box.inside_auto_refill)

    def test_refill_child_can_commit_selected_source_after_handoff(self):
        self.candidates(["T2B"])
        original = self.box.gcode.run_script_from_command
        def script(command):
            original(command)
            if not command.startswith("BOX_MODIFY_TN"):
                self.box.last_cmd = "T2B"
                self.box.operations.cancel()
        self.box.gcode.run_script_from_command = script
        self.runout.material_auto_refill(0)
        self.assertEqual(self.box.last_cmd, "T2B")
        self.assertFalse(self.box.inside_auto_refill)

    def test_update_map_source_change_during_candidate_query_prevents_alias_write(self):
        self.candidates(["T2B"])
        def available():
            self.box.last_cmd = "T3C"
            return True
        self.results["available"] = available
        with self.assertRaisesRegex(LifecycleStopped, "remapping source"):
            self.runout.update_Tnn_map()
        self.assertFalse(self.calls("script"))

    def test_update_map_keeps_earlier_device_guards_during_later_candidate_query(self):
        self.candidates(["T2A", "T1B"])
        def second():
            self.box.device_versions.invalidate(2)
            return True
        self.results["available"] = [False, second]
        with self.assertRaises(LifecycleStopped):
            self.runout.update_Tnn_map()
        self.assertFalse(self.calls("script"))

    def test_refill_target_replacement_stops_before_bookkeeping(self):
        self.candidates(["T2B"])
        self.results["available"] = lambda: self.box.device_versions.invalidate(2)
        with self.assertRaises(LifecycleStopped):
            self.runout.material_auto_refill(0)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(self.calls("script"))

    def test_refill_permits_legitimate_child_operation_serial_changes(self):
        self.candidates(["T2B"])
        self.results["script"] = lambda: self.box.operations.cancel()
        self.runout.material_auto_refill(0)
        self.assertFalse(self.box.inside_auto_refill)

    def test_refill_exception_retains_original_inside_flag_tail(self):
        self.candidates(["T2B"])
        original = self.box.gcode.run_script_from_command
        def run(command):
            if not command.startswith("BOX_MODIFY_TN"):
                raise ValueError("failed tool command")
            original(command)
        self.box.gcode.run_script_from_command = run
        with self.assertRaisesRegex(ValueError, "tool"):
            self.runout.material_auto_refill(0)
        self.assertTrue(self.box.inside_auto_refill)

    def test_tightening_requires_clear_connections_then_120_commanded_e(self):
        self.candidates(["T1A"])
        self.results["sample"] = [1, 0, 1]
        def pause():
            self.position[3] += 60
        self.results["pause"] = pause
        self.runout.filament_err_tighten_up_event(0)
        self.assertEqual(self.calls("pause"), [((15,), {}), ((13.0,), {}), ((13.0,), {})])
        self.assertEqual(self.calls("communication_tighten_up_enable"),
                         [((1, "ENABLE"), {}), ((1, "DISABLE"), {})])
        self.assertEqual(self.box.box_state.Tn_data["filament"], 1)
        self.assertFalse(self.calls("extrude"))

    def test_tightening_rejects_nonfinite_commanded_position(self):
        self.position[3] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite commanded E"):
            self.runout.filament_err_tighten_up_event(0)
        self.assertFalse(self.calls("communication_tighten_up_enable"))

    def test_tightening_wait_cancels_on_lifecycle_stop(self):
        self.results["sample"] = 1
        self.results["pause"] = lambda: setattr(self, "running", False)
        with self.assertRaises(LifecycleStopped):
            self.runout.filament_err_tighten_up_event(0)
        self.assertEqual(len(self.calls("pause")), 1)
        self.assertFalse(self.calls("communication_set_box_mode"))

    def test_tightening_freshness_failure_never_uses_cached_mask(self):
        self.results["sample"] = None
        with self.assertRaisesRegex(RuntimeError, "fresh ordinary"):
            self.runout.filament_err_tighten_up_event(0)
        self.assertFalse(self.calls("communication_tighten_up_enable"))

    def test_tightening_wait_stops_on_source_change(self):
        self.results["sample"] = 1
        self.results["pause"] = lambda: setattr(self.box, "last_cmd", "T2A")
        with self.assertRaises(LifecycleStopped):
            self.runout.filament_err_tighten_up_event(0)
        self.assertFalse(self.calls("communication_set_box_mode"))

    def test_wait_command_allows_nested_operation_but_stops_on_binding_change(self):
        self.box.inside_auto_refill = True
        def finish():
            self.box.operations.cancel()
            self.box.inside_auto_refill = False
        self.results["pause"] = finish
        self.runout.cmd_wait_extrusion_all_materials(None)
        self.box.inside_extrusion_all_materials = True
        self.results["pause"] = lambda: setattr(self.box, "_binding_generation", 2)
        with self.assertRaises(LifecycleStopped):
            self.runout.cmd_wait_extrusion_all_materials(None)


if __name__ == "__main__":
    unittest.main()
