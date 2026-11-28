"""File/dispatch/session checks with real temporary files and synthetic hardware."""

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import sys
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cfs.native import Frame, UnsupportedRequest, build_request
from cfs.box import Box
from cfs.operations import OperationContext
from cfs.print_trial import PrintTrial
from cfs.state import BoxState
from cfs.toolchange import ToolChange


class Command:
    def __init__(self, **params):
        self.params = params

    def get_int(self, key, default=None):
        return int(self.params.get(key, default))

    def get_command_parameters(self):
        return self.params.copy()


class PrintTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "two-colour.gcode"
        self.path.write_text("START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55\nT0\nG1 X100 Y100\nT2\nEND_PRINT\n", encoding="utf-8")
        self.clock, self.current = 0.0, object()
        self.worker = self.current
        self.online = {i: i == 1 for i in range(1, 5)}
        self.device_generation, self.identity = 1, ("1.1.3", "synthetic identity")
        self.mode, self.material, self.connections, self.buffer = 0, 15, 0, 2
        self.start_status = 0
        self.sent = []
        self.configuration = {"config": {"box": {"version": "1"}}, "settings": {"box": {"version": 1}}}
        self.sd = SimpleNamespace(sdcard_dirname=str(self.root), current_file=None,
            work_handler_dispatch=None, cmd_from_sd=False, gcode_metadata={"metadata": {"slicer": "Creality"}})
        self.sd.get_first_Tn = self.first_tool
        state = BoxState()
        state.modify_Tn_data(1, "connect", "state")
        for tool in ("T1A", "T1C"):
            state.modify_Tn_data(1, "100001", "material_type", tool[2])
            state.modify_Tn_data(1, "0FFFFFF", "color_value", tool[2])
        self.lifecycle = SimpleNamespace(passive=True, _generation=2,
            current_dispatch=lambda: self.current, online=lambda address: self.online[address],
            head_sensor=SimpleNamespace(runout_helper=SimpleNamespace(filament_present=False)))
        self.lifecycle.check = self.check_lifecycle
        config_status = SimpleNamespace(get_status=lambda eventtime: deepcopy(self.configuration))
        versions = SimpleNamespace(require_supported=lambda address: SimpleNamespace(version="1.1.3"),
            generation=lambda address: self.device_generation, get=lambda address: self.identity,
            observation_only=lambda address: False, has_observation_only=lambda: False)
        heater = SimpleNamespace(can_extrude=True, min_extrude_temp=170, max_temp=300,
            get_temp=lambda eventtime: (220.0, 220.0))
        self.factors = {"speed_factor": 1.0, "extrude_factor": 1.0}
        self.box = SimpleNamespace(lifecycle=self.lifecycle,
            boxcfg=SimpleNamespace(version=1, extrude_material_len_for_box=4),
            _printer=SimpleNamespace(lookup_object=lambda name: config_status),
            _reactor=SimpleNamespace(monotonic=lambda: self.clock), box_state=state,
            native=SimpleNamespace(is_connected=True), device_versions=versions, _binding_generation=3,
            virtual_sdcard=self.sd, print_stats=SimpleNamespace(state="standby"), error=None,
            operations=SimpleNamespace(active=False, address=1, target="T1A"), material_trial=SimpleNamespace(active=False),
            preload_trial=SimpleNamespace(active=False), filament_sensor_enable=False,
            extrude_process_stage7_flag=False, last_cmd=None, extrude_tnn="T1A", last_replies={},
            gcode_move=SimpleNamespace(get_status=lambda eventtime: self.factors.copy()),
            toolhead=SimpleNamespace(flush_step_generation=lambda: None,
                extruder=SimpleNamespace(get_heater=lambda: heater)))
        self.trial = PrintTrial(self.box)
        self.box.send_native = self.send_native

    def check_lifecycle(self, generation=None):
        if generation is not None and generation != self.lifecycle._generation:
            raise RuntimeError("synthetic changed lifecycle")

    def first_tool(self, unused):
        text = Path(self.sd.current_file.name).read_text(encoding="utf-8")[:512 * 1024]
        match = re.search(r"(?m)^\s*T(\d+)\s*$", text)
        return "T" + match[1] if match else None

    def send_native(self, request, **unused):
        self.sent.append(request.body)
        answers = {4: b"", 10: bytes((20, 30, 0, self.mode)), 5: bytes((self.buffer,))}
        if request.command == 8:
            answer = bytes((self.connections if request.data == b"\x01" else self.material,))
        else:
            answer = answers[request.command]
        status = self.start_status if request.command == 10 else 0
        self.trial.admit(request)
        bounded = self.trial.bounded_request(request)
        frame = self.frame(request.command, answer, status)
        self.trial.validate_reply(bounded, frame)
        self.box.last_replies[request.address] = frame
        return status == 0

    def arm(self, **options):
        values = dict(filename=self.path.name,
            sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
            tools=("T1A", "T1C"), confirmed=True)
        values.update(options)
        return self.trial.arm(**values)

    def start(self, **params):
        self.arm()
        self.sd.current_file = self.path.open("r", encoding="utf-8")
        self.addCleanup(self.sd.current_file.close)
        self.sd.work_handler_dispatch = self.worker
        self.sd.cmd_from_sd = True
        self.box.print_stats.state = "printing"
        return self.trial.start_guard(Command(**params))

    def frame(self, command, data=b"", status=0):
        return Frame(1, command, status, data, b"synthetic checked frame")

    def exchange(self, command, data=b"", answer=b"", status=0):
        request = build_request(1, command, data)
        self.assertIsNotNone(self.trial.admit(request))
        request = self.trial.bounded_request(request)
        self.trial.validate_reply(request, self.frame(command, answer, status))
        return request

    def command(self, name, action=lambda gcmd: None, **params):
        gcmd = Command(**params)
        self.assertTrue(self.trial.allows_command(name, gcmd))
        return self.trial.call_command(action, gcmd)

    def loaded_coordinator(self, continuing=False):
        """Use the real stock coordinator with named external heat/motion effects."""
        self.events = []
        box = self.box
        box.box_state.modify_Tn_data(0, "connect", uppart="state")
        box.box_state.modify_Tn_data(1, "T1A", "filament")
        box.box_state.is_continue_print = continuing
        box.boxcfg.Tn_extrude, box.boxcfg.safe_pos_y = 150, 180
        box.last_cmd, box.last_cmd_tmp, box.move_z = "T1A", None, None
        box.error_tnn, box.heart_process_enable = {}, [True] * 4
        box.operations = OperationContext(box)
        box.print_trial = self.trial
        box.host_guard = lambda address=None: self.trial.guard(address)
        box.record_tn_cur_tnn = lambda value: self.events.append(("metadata", value))
        box.record_tn_move_z = lambda value: self.events.append(("record_z", value))
        box.get_purge_in_prime_tower = lambda: False
        box.get_filament_sensor_detect = lambda: self.lifecycle.head_sensor.runout_helper.filament_present
        box.communication_set_box_mode = lambda address, mode, slot: Box.communication_set_box_mode(box, address, mode, slot)
        box.get_material_target_temp = lambda target: 220
        box.quickly_wait_heating = lambda temperature: self.events.append(("heat", temperature)) or True
        box.get_flush_velocity = lambda source, target: ([360], [1])
        box.get_flush_temp = lambda source, target: (220, 220, 220)
        box.Tn_Extrude = lambda length, *args: self.events.append(("extrude", length)) or True
        box.nozzle_clean = lambda: self.events.append(("clean",)) or True
        box.move_to_cut = lambda: self.events.append(("cut",)) or True
        box.material_change_flush = lambda *args: self.events.append(("colour_purge",)) or True
        box.gcode = SimpleNamespace(run_script_from_command=lambda command: self.events.append(("gcode", command)))

        def down(**values):
            self.events.append(("z_down", values))
            box.move_z = values["distance"]
        def restore():
            self.events.append(("z_restore",))
            box.move_z = None
        box.motion = SimpleNamespace(_positions=lambda *names: tuple(getattr(box.boxcfg, name) for name in names),
            z_down=down, z_restore=restore,
            go_to_extrude_pos=lambda: self.events.append(("extrude_pos",)),
            move_to_safe_pos=lambda: self.events.append(("safe_pos",)))
        self.lifecycle.head_sensor.runout_helper.filament_present = True
        self.mode, self.connections, self.buffer = 2, 1, 0
        return ToolChange(box)

    def test_arm_has_no_mechanical_or_source_effect_and_start_uses_real_file(self):
        before = deepcopy(self.box.box_state.get_status())
        result = self.arm()
        self.assertFalse(result["mechanics"])
        self.assertEqual(self.box.box_state.get_status(), before)
        self.assertIsNone(self.box.last_cmd)
        self.assertTrue(self.lifecycle.passive)
        self.assertFalse(self.trial.owns_dispatch())

    def test_filename_hash_tools_confirmation_and_finite_job_are_required(self):
        for values in ({"confirmed": False}, {"sha256": "0" * 64},
            {"filename": "../two-colour.gcode"}, {"tools": ("T1A", "T2A")},
            {"tools": ("T1A", "T1A")}, {"tools": ([], [])}):
            with self.subTest(values=values), self.assertRaises((ValueError, RuntimeError)):
                self.arm(**values)
        self.path.write_text("START_PRINT\nT0\n" + "T2\n" * 8 + "END_PRINT\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.arm()

    def test_arm_accepts_another_actual_pair_on_its_selected_hub(self):
        self.path.write_text("START_PRINT\nT4\nT5\nEND_PRINT\n", encoding="utf-8")
        self.online = {i: i == 2 for i in range(1, 5)}
        self.arm(tools=("T2A", "T2B"))
        self.assertEqual(self.trial.address, 2)

    def test_current_worker_and_initial_tool_are_verified_without_status_alias(self):
        self.assertEqual(self.start(INITIAL_TOOL="0"), "T1A")
        self.assertTrue(self.trial.owns_dispatch())
        self.assertNotIn("Tnn_map", self.box.box_state.get_status())
        self.trial.guard()()
        self.assertEqual(self.sent, [bytes.fromhex(value) for value in (
            "0103ff0a", "0104ff0800", "0104ff0801", "0103ff05")])

    def test_start_requires_both_fresh_material_bits_not_cached_labels(self):
        self.material = 1
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertEqual(self.box.box_state.get_Tn_data(1, "material_type", "C"), "100001")
        self.assertFalse(self.trial.active)

    def test_start_accepts_real_known_loaded_source_without_reassigning_it(self):
        self.box.last_cmd = "T1A"
        self.box.box_state.modify_Tn_data(1, "T1A", "filament")
        self.lifecycle.head_sensor.runout_helper.filament_present = True
        self.mode, self.connections, self.buffer = 2, 1, 0
        self.assertEqual(self.start(), "T1A")
        self.assertEqual(self.box.last_cmd, "T1A")

    def test_unknown_loaded_head_is_rejected_before_print_preparation(self):
        self.lifecycle.head_sensor.runout_helper.filament_present = True
        self.connections = 1
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "None")

    def test_conflicting_source_hints_and_missing_raw_head_are_not_repaired(self):
        self.box.last_cmd = "T1A"
        self.box.box_state.modify_Tn_data(1, "T1C", "filament")
        self.lifecycle.head_sensor.runout_helper.filament_present = None
        self.connections = 1
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "T1C")

    def test_start_runout_status_is_not_accepted_as_normal_presence(self):
        self.start_status = 0x50
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertFalse(self.trial.active)
        self.assertFalse(self.trial._runout)

    def test_start_cannot_reuse_an_older_reply(self):
        self.box.send_native = lambda request, **kwargs: True
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertFalse(self.trial.active)

    def test_disabled_cfs_and_fractional_stage7_amount_are_rejected_at_arm(self):
        self.box.box_state.modify_Tn_data(0, 0, uppart="enable")
        with self.assertRaises(RuntimeError):
            self.arm()
        self.box.box_state.modify_Tn_data(0, 1, uppart="enable")
        self.box.boxcfg.extrude_material_len_for_box = 4.5
        with self.assertRaises(ValueError):
            self.arm()

    def test_conflicting_initial_tool_or_wrong_open_file_revokes(self):
        with self.assertRaises(ValueError):
            self.start(INITIAL_TOOL="2")
        self.assertFalse(self.trial.active)

    def test_unrelated_dispatch_cannot_borrow_worker_or_claimed_commands(self):
        self.start()
        self.current = object()
        self.assertFalse(self.trial.owns_dispatch())
        self.assertFalse(self.trial.allows_command("BOX_END", Command()))
        self.assertIsNone(self.trial.capture())
        self.assertIsNone(self.trial.admit(build_request(1, "SET_BOX_MODE", b"\x00\x01")))
        with self.assertRaises(UnsupportedRequest):
            self.trial.guard()

    def test_paused_open_job_retains_lease_and_new_actual_worker_resumes(self):
        self.start()
        self.box.print_stats.state = "paused"
        self.sd.cmd_from_sd = False
        self.sd.work_handler_dispatch = None
        self.current = object()
        self.assertTrue(self.trial.poll())
        self.assertFalse(self.trial.owns_dispatch())
        self.trial.control("RESUME", lambda: self.assertTrue(self.trial.owns_dispatch()))
        self.sd.work_handler_dispatch = self.current
        self.sd.cmd_from_sd = True
        self.box.print_stats.state = "printing"
        self.trial.guard()()

    def test_old_scheduled_child_cannot_borrow_or_cancel_new_lease(self):
        self.start()
        old = self.trial.capture()
        self.trial.cancel()
        self.sd.current_file.close()
        self.sd.current_file = None
        self.sd.work_handler_dispatch = None
        self.sd.cmd_from_sd = False
        self.box.print_stats.state = "standby"
        self.arm()
        self.current = object()
        with self.assertRaises(RuntimeError), old():
            self.fail("old callback ran")
        self.assertTrue(self.trial.active)

    def test_finite_callback_scope_and_captured_guard_catch_revocation(self):
        self.start()
        child = self.trial.capture()
        check = self.trial.context_guard()
        self.current = object()
        self.assertIsNone(self.trial.context_guard())
        with self.assertRaises(RuntimeError), child():
            self.assertTrue(self.trial.owns_dispatch())
            self.trial.cancel()
            check()
        self.assertFalse(self.trial.owns_dispatch())

    def test_actual_pin_changes_cancel_before_more_effects(self):
        self.start()
        check = self.trial.guard()
        self.identity = ("1.1.3", "replacement")
        with self.assertRaises(RuntimeError):
            check()
        self.assertFalse(self.trial.active)

    def test_configuration_mapping_material_or_file_changes_are_not_hidden(self):
        self.start()
        check = self.trial.guard()
        self.box.box_state.modify_Tn_data(1, "100002", "material_type", "A")
        with self.assertRaises(RuntimeError):
            check()
        self.assertFalse(self.trial.active)

    def test_configuration_and_mapping_pins_reject_changes(self):
        self.start()
        self.configuration["settings"]["box"]["version"] = 2
        with self.assertRaises(RuntimeError):
            self.trial.guard()

    def test_mutable_mapping_cannot_change_during_a_job(self):
        self.start()
        self.box.box_state.Tnn_map["T1A"] = "T1C"
        with self.assertRaises(RuntimeError):
            self.trial.guard()

    def test_file_and_metadata_pins_reject_changes(self):
        self.start()
        self.sd.gcode_metadata["metadata"]["slicer"] = "different"
        with self.assertRaises(RuntimeError):
            self.trial.guard()

    def test_changed_file_is_rejected_before_another_request(self):
        self.start()
        self.path.write_text(self.path.read_text(encoding="utf-8").replace("220", "230"), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.trial.guard()

    def test_wrong_current_file_cannot_pass_start_guard(self):
        self.arm()
        other = self.root / "other.gcode"
        other.write_bytes(self.path.read_bytes())
        self.sd.current_file = other.open("r", encoding="utf-8")
        self.addCleanup(self.sd.current_file.close)
        self.sd.work_handler_dispatch, self.sd.cmd_from_sd = self.worker, True
        self.box.print_stats.state = "printing"
        with self.assertRaises(RuntimeError):
            self.trial.start_guard(Command())
        self.assertFalse(self.trial.active)

    def test_timeout_and_terminal_status_cancel_but_do_not_move_or_cleanup(self):
        self.start()
        self.clock = self.trial.TIMEOUT
        self.assertFalse(self.trial.poll())
        self.assertIsNotNone(self.sd.current_file)
        self.assertIsNone(self.box.last_cmd)

    def test_offline_guard_is_not_a_native_grant_and_rechecks_connection(self):
        self.start()
        guard = self.trial.guard(2)
        self.box.box_state.modify_Tn_data(2, "connect", "state")
        with self.assertRaises(RuntimeError):
            guard()

    def test_another_address_and_preload_run_are_rejected(self):
        self.start()
        with self.assertRaises(UnsupportedRequest):
            self.trial.admit(build_request(2, "SET_BOX_MODE", b"\x00\x01"))

    def test_preload_run_is_not_admitted_even_inside_start_policy(self):
        self.start()
        with self.assertRaises(UnsupportedRequest):
            self.command("BOX_START_PRINT", lambda gcmd:
                self.trial.admit(build_request(1, "SET_PRE_LOADING", b"\x01\x02")))
        self.assertFalse(self.trial.active)

    def test_start_end_policy_is_scoped_and_reply_is_consumed_once(self):
        self.start()
        saved = []

        def start(gcmd):
            request = build_request(1, "SET_PRE_LOADING", b"\x0f\x00")
            self.trial.admit(request)
            bounded = self.trial.bounded_request(request)
            self.trial.validate_reply(bounded, self.frame(13))
            saved.append(request)
        self.command("BOX_START_PRINT", start)
        self.assertLessEqual(len(self.trial._requests), self.trial.MAX_REQUESTS)
        with self.assertRaises(UnsupportedRequest):
            self.trial.validate_reply(saved[0], self.frame(13))

    def test_normal_stage_shapes_and_head_stage_ownership(self):
        self.start()
        self.exchange(16, b"\x01\x00\x00", b"\x00")
        self.exchange(16, b"\x01\x05\x00", b"\x00" * 4)
        self.box.operations.active = self.box.filament_sensor_enable = True
        self.lifecycle.head_sensor.runout_helper.filament_present = True
        self.current = object()
        with self.trial.execution("head"):
            self.exchange(16, b"\x01\x06\x00")
        self.assertFalse(self.trial.owns_dispatch())

    def test_print_action_and_stage7_use_wire_body_not_telemetry_mode(self):
        self.start()
        self.exchange(4, b"\x01\x00")
        self.exchange(16, b"\x01\x07\x04")
        self.assertTrue(self.trial.active)

    def test_nonfinite_wheel_value_is_not_a_successful_observation(self):
        self.start()
        with self.assertRaises(ValueError):
            self.exchange(14, b"\x01", struct.pack(">f", float("nan")))
        self.assertFalse(self.trial.active)

    def test_nonfinite_stage5_value_stops_loading(self):
        self.start()
        with self.assertRaises(ValueError):
            self.exchange(16, b"\x01\x05\x00", struct.pack(">f", float("inf")))
        self.assertFalse(self.trial.active)

    def test_error50_remains_a_runout_frame_and_scopes_real_protocol_effects(self):
        self.start()
        request = build_request(1, "GET_BOX_STATE")
        protocol = self.trial.capture_protocol(request)
        self.current = object()
        frame = self.frame(10, b"\x14\x1e\x00\x02", 0x50)
        with protocol(frame):
            self.assertTrue(self.trial.owns_dispatch())
            self.box.error = "filament_err"
            child = self.trial.capture()
        self.assertEqual(frame.status, 0x50)
        self.assertFalse(self.trial.owns_dispatch())
        with child():
            self.assertTrue(self.trial.owns_dispatch())
        self.assertTrue(self.trial.active)

    def test_normal_background_frame_does_not_enable_effects(self):
        self.start()
        scope = self.trial.capture_protocol(build_request(1, "GET_BOX_STATE"))
        self.current = object()
        with scope(self.frame(10, b"\x14\x1e\x00\x02")):
            self.assertFalse(self.trial.owns_dispatch())

    def test_malformed_and_other_error_replies_revoke(self):
        self.start()
        with self.assertRaises(RuntimeError):
            self.exchange(17, b"\x01\x01", status=0x19)
        self.assertFalse(self.trial.active)

    def test_stage_specific_malformed_reply_is_not_an_empty_ack_success(self):
        self.start()
        with self.assertRaises(ValueError):
            self.exchange(16, b"\x01\x00\x00", b"")
        self.assertFalse(self.trial.active)

    def test_stage6_cannot_be_sent_from_the_virtual_sd_worker(self):
        self.start()
        with self.assertRaises(UnsupportedRequest):
            self.trial.admit(build_request(1, "EXTRUDE_PROCESS", b"\x01\x06\x00"))
        self.assertFalse(self.trial.active)

    def test_false_command_or_phase_result_stops_the_session(self):
        self.start()
        with self.assertRaises(RuntimeError):
            self.command("BOX_START_PRINT", lambda gcmd: False)
        self.assertFalse(self.trial.active)

    def test_purge_preflight_failure_precedes_the_coordinator(self):
        self.start()
        seen = []

        def invalid():
            raise ValueError("synthetic invalid purge")
        self.trial._change_flush_profile = invalid

        def coordinator(gcmd):
            self.trial.prepare_change("T1A")
            seen.append("coordinator cut")
        with self.assertRaises(ValueError):
            self.command("T0", coordinator)
        self.assertEqual(seen, [])
        self.assertFalse(self.trial.active)

    def test_print_uses_computed_stock_purge_instead_of_fixed150(self):
        self.box.boxcfg.buffer_empty_len = 200
        self.box.boxcfg.Tn_extrude = 150
        self.box.finishing = SimpleNamespace(get_flush_len=lambda source, target: 300)
        self.box.get_flush_velocity = lambda source, target: ([360], [1])
        self.start()
        seen = []

        def action(gcmd):
            self.trial.prepare_change("T1A")
            self.box.last_cmd = "T1A"
            self.box.box_state.modify_Tn_data(1, "T1A", "filament")
            self.lifecycle.head_sensor.runout_helper.filament_present = True
            self.mode, self.connections, self.buffer = 2, 1, 0

            def flush():
                seen.append(self.trial._change_purge)
                return True
            self.trial.change_phase("FLUSH", flush)
        self.command("T0", action)
        self.assertEqual(seen, [(300, (360,), (1,), (300,))])
        self.assertTrue(self.trial.active)

    def test_duplicate_initial_t0_uses_real_same_source_without_cut_or_matrix_purge(self):
        coordinator = self.loaded_coordinator()
        self.start()

        def forbidden():
            raise AssertionError("same-source selection must not calculate the zero diagonal")
        self.trial._change_flush_profile = forbidden
        for unused in range(2):
            self.assertTrue(self.command("T0", lambda gcmd: coordinator.Tn_action("T1A")))
            self.assertIsNone(self.trial._change_purge)
        self.assertEqual(self.trial._tool_calls, 2)
        self.assertEqual([name for name, *args in self.events], ["metadata", "heat", "metadata", "heat"])
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(self.box.operations.active)

    def test_same_source_continuation_preserves_configured150_without_diagonal_purge(self):
        coordinator = self.loaded_coordinator(continuing=True)
        self.start()

        def forbidden():
            raise AssertionError("continuation uses configured extrusion rather than the matrix diagonal")
        self.trial._change_flush_profile = forbidden
        self.assertTrue(self.command("T0", lambda gcmd: coordinator.Tn_action("T1A")))
        self.assertIn(("extrude", 150), self.events)
        self.assertFalse(any(row[0] in ("cut", "colour_purge") for row in self.events))
        self.assertEqual(self.trial._tool_calls, 1)
        self.assertIsNone(self.trial._change_purge)
        self.assertFalse(self.box.box_state.is_continue_print)

    def test_direct_recovery_child_prepares_actual_source_and_target_without_a_t_wrapper(self):
        self.box.boxcfg.buffer_empty_len = 200
        self.box.boxcfg.Tn_extrude = 150
        self.box.finishing = SimpleNamespace(get_flush_len=lambda source, target: 300)
        self.box.get_flush_velocity = lambda source, target: ([360], [1])
        self.start()
        self.box.last_cmd = "T1A"
        self.box.box_state.modify_Tn_data(1, "T1A", "filament")
        self.lifecycle.head_sensor.runout_helper.filament_present = True
        child = self.trial.capture()
        self.current = object()
        with child():
            self.assertEqual(self.trial.prepare_change("T1C"), "T1C")
        self.assertEqual((self.trial.source, self.trial.target), ("T1A", "T1C"))
        self.assertEqual(self.trial._change_purge[0], 300)
        self.assertEqual(self.trial._tool_calls, 1)
        self.assertEqual(self.box.last_cmd, "T1A")

    def test_known_unload_phase_is_bounded_and_does_not_substitute_source(self):
        self.start()
        self.box.last_cmd = "T1A"
        self.box.box_state.modify_Tn_data(1, "T1A", "filament")
        seen = []

        def unload():
            self.trial._bounded_profile(True)
            seen.append(self.box.last_cmd)
            self.box.last_cmd = None
            self.box.box_state.modify_Tn_data(1, "None", "filament")
            return True
        self.trial.change_phase("UNLOAD", unload)
        self.assertEqual(seen, ["T1A"])
        self.assertIsNone(self.box.last_cmd)
        self.assertFalse(self.trial.owns_phase("UNLOAD", "BOX_RETRUDE_MATERIAL"))

    def test_known_source_loss_stops_before_coordinated_tail(self):
        self.start()
        self.box.last_cmd = "T1A"
        self.box.box_state.modify_Tn_data(1, "T1A", "filament")

        def changed():
            self.box.last_cmd = "T1C"
            self.trial._bounded_profile(True)
        with self.assertRaises(RuntimeError):
            self.trial.change_phase("UNLOAD", changed)
        self.assertFalse(self.trial.active)

    def test_unload_ack_does_not_replace_fresh_empty_head_and_lane(self):
        self.start()
        self.box.last_cmd = "T1A"
        self.box.box_state.modify_Tn_data(1, "T1A", "filament")
        self.lifecycle.head_sensor.runout_helper.filament_present = True
        self.mode, self.connections = 2, 1
        with self.assertRaises(RuntimeError):
            self.trial.change_phase("UNLOAD", lambda: True)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(self.trial.active)

    def test_empty_initial_unload_keeps_neutral_buffer_as_observed(self):
        self.buffer = 0
        self.start()
        self.assertTrue(self.trial.change_phase("UNLOAD", lambda: True))
        self.assertEqual(self.buffer, 0)
        self.assertIsNone(self.box.last_cmd)

    def test_cancelled_finite_callback_cannot_rearm_until_unwound(self):
        self.start()
        child = self.trial.capture()
        self.current = object()
        with self.assertRaises(RuntimeError), child():
            self.trial.cancel()
            with self.assertRaisesRegex(RuntimeError, "previous owner"):
                self.arm()

    def test_end_map_reset_is_allowed_only_inside_its_finite_command(self):
        self.box.box_state.Tnn_map["T1B"] = "T1A"
        self.start()

        def finish(gcmd):
            self.box.box_state.Tnn_map = {name: name for name in self.box.box_state.Tnn_map}
            self.trial.guard()()
        self.command("BOX_END_PRINT", finish)
        self.assertFalse(self.trial.allows_command("BOX_START_PRINT", Command()))
        self.sd.current_file.close()
        self.sd.current_file = None
        self.box.print_stats.state = "complete"
        self.assertFalse(self.trial.poll())

    def test_full_buffer_after_flush_keeps_load_readiness_strict(self):
        self.start()
        self.trial.target = self.box.last_cmd = "T1A"
        self.box.box_state.modify_Tn_data(1, "T1A", "filament")
        self.lifecycle.head_sensor.runout_helper.filament_present = True
        self.mode, self.connections, self.buffer = 2, 1, 1
        with self.assertRaisesRegex(RuntimeError, "phase"):
            self.trial._phase_observations("LOAD")
        self.trial._phase_observations("FLUSH")
        self.assertEqual(self.buffer, 1)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertTrue(self.trial.active)

    def test_exhausted_source_predicate_keeps_live_hint_without_repair(self):
        self.start()
        # Predicate unit only: the integration suite obtains these latches
        # through the actual decoder and drain, rather than this fixture setup.
        self.trial._runout, self.trial._unknown_source = True, True
        self.trial.source, self.trial.target = "T1A", "T1C"
        self.box.last_cmd, self.box.last_cmd_tmp = None, "T1A"
        self.box.is_use_ending_material = self.box.box_filament_err = False
        self.box.box_state.modify_Tn_data(1, "T1A", "filament")
        self.box.box_state.modify_Tn_data(1, "none", "vender", "A")
        acknowledged = []
        self.box.operations = SimpleNamespace(active=True, _serial=1, _error="filament_err",
            source="T1A", target="T1C", address=1,
            acknowledge_error_clear=lambda *args: acknowledged.append(args))
        self.mode, self.material, self.connections, self.buffer = 0, 14, 0, 2
        self.trial._phase_observations("UNLOAD", 1)
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "T1A")
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertEqual(acknowledged, [(1, "T1A", "T1C", 1)])
        for owner, field, value in ((self.box, "last_cmd_tmp", "T1C"),
                (self.box, "is_use_ending_material", True),
                (self.box, "box_filament_err", True), (self.trial, "_runout", False)):
            before = getattr(owner, field)
            setattr(owner, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "phase"):
                self.trial._phase_observations("UNLOAD", 1)
            setattr(owner, field, before)
        for field, value in (("mode", 2), ("connections", 1), ("buffer", 0), ("buffer", 1)):
            before = getattr(self, field)
            setattr(self, field, value)
            with self.subTest(field=field, value=value), self.assertRaisesRegex(RuntimeError, "phase"):
                self.trial._phase_observations("UNLOAD", 1)
            setattr(self, field, before)
        self.box.box_state.modify_Tn_data(1, "unknown", "vender", "A")
        with self.assertRaisesRegex(RuntimeError, "phase"):
            self.trial._phase_observations("UNLOAD", 1)
        self.box.box_state.modify_Tn_data(1, "none", "vender", "A")
        with self.assertRaisesRegex(RuntimeError, "operation"):
            self.trial._phase_observations("UNLOAD", 0)
        self.box.operations._error = "empty_print"
        with self.assertRaisesRegex(RuntimeError, "captured error"):
            self.trial._phase_observations("UNLOAD", 1)
        self.box.operations._error = "filament_err"
        self.lifecycle.head_sensor.runout_helper.filament_present = True
        with self.assertRaisesRegex(RuntimeError, "phase"):
            self.trial._phase_observations("UNLOAD", 1)
        self.assertEqual(acknowledged, [(1, "T1A", "T1C", 1)])


if __name__ == "__main__":
    unittest.main()
