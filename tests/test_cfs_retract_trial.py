"""One native BUFFER attempt, using synthetic dispatch/frames and no native .so."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_preload_trial as preload
from cfs.native import UnsupportedRequest, build_request, diagnostic_request
from cfs.protocol import decode_reply
from test_box_data import CommandError
from test_box_native import NativeCommand
from test_native import reply


class RetractTrialTests(unittest.TestCase):
    def make_box(self, target="T1C", **options):
        # Reuse setup without inheriting or collecting preload's test methods.
        preload.PreloadTrialTests.make_box(self, target, **options)
        self.box.virtual_sdcard.do_resume_status = False
        if getattr(self.lifecycle, "head_sensor", None) is not None:
            self.lifecycle.head_sensor.runout_helper.filament_present = True
        self.printer.gcode.messages.clear()
        return self.box.preload_trial

    def queue_success(self, initial_status=0, initial_mode=2, head_after=True):
        self.native.responses = {
            (self.address, 0x0A): [reply(self.address, 0x0A, initial_status,
                                        bytes((20, 30, 0, initial_mode))),
                                    reply(self.address, 0x0A, data=b"\x14\x1e\x00\x00"),
                                    reply(self.address, 0x0A, data=b"\x14\x1e\x00\x00")],
            (self.address, 0x08): [reply(self.address, 0x08, data=bytes((15,))),
                                    reply(self.address, 0x08, data=bytes((self.mask,))),
                                    reply(self.address, 0x08, data=bytes((15,))),
                                    reply(self.address, 0x08, data=bytes((self.mask,)))],
            (self.address, 0x05): [reply(self.address, 0x05, data=b"\x00"),
                                    reply(self.address, 0x05, data=b"\x02")],
        }

        def buffer_result():
            self.lifecycle.head_sensor.runout_helper.filament_present = head_after
            return reply(self.address, 0x11)

        self.native.responses[(self.address, 0x11)] = [buffer_result]

    def run_trial(self):
        return self.box.preload_trial.run_retract(self.target, confirmed=True)

    def queue_material(self, **options):
        self.queue_success(head_after=False, **options)
        self.native.responses[(self.address, 8)][3] = reply(self.address, 8, data=b"\x00")

    def assert_released(self):
        self.assertFalse(self.box.preload_trial.active)
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.lifecycle.passive)
        self.assertEqual(self.printer.gcode.scripts, [])
        if self.box.native is not None:
            before = len(self.native.calls)
            for name, data in (("SET_BOX_MODE", b"\x00\x01"),
                               ("RETRUDE_PROCESS", bytes((self.mask, 0)))):
                with self.assertRaises(UnsupportedRequest):
                    self.box.send_native(build_request(self.address, name, data))
            self.assertEqual(len(self.native.calls), before)

    def test_exact_sequence_preserves_sources_preferences_and_metadata_without_host_calls(self):
        for known, head_after in ((False, True), (True, False)):
            with self.subTest(known=known, head_after=head_after):
                self.make_box()
                self.queue_success(head_after=head_after)
                self.box.last_cmd_tmp = "T3B"
                self.box.move_z = 13.94
                if known:
                    self.box.last_cmd = self.target
                    self.box.box_state.modify_Tn_data(1, self.target, "filament")
                before = deepcopy((self.box.last_cmd, self.box.last_cmd_tmp,
                    self.box.box_state.get_Tn_data(1, "filament"), self.box.preloading_en,
                    self.box.preloading_poweron, self.box.extrude_tnn, self.box.heart_process_enable,
                    self.box.box_state.Tnn_map, self.box.move_z, self.box.store.base_data))
                stored_bytes = self.box.store.path.read_bytes()
                # No XYZ/cutter/temperature object is needed by this diagnostic.
                self.box.cut_sensor = SimpleNamespace(active=False, cut_present=1)
                with patch.object(self.box.operations, "begin", side_effect=AssertionError("host operation")):
                    result = self.run_trial()
                self.assertEqual(result, {"target": "T1C", "head": head_after, "mode": 0,
                    "material": 15, "connections": 4, "buffer": 2})
                self.assertEqual(self.native.commands(), [(1, 0x0A, b""), (1, 8, b"\x00"),
                    (1, 8, b"\x01"), (1, 5, b""), (1, 4, b"\x00\x01"), (1, 10, b""),
                    (1, 17, b"\x04\x00"), (1, 10, b""), (1, 8, b"\x00"),
                    (1, 8, b"\x01"), (1, 5, b"")])
                self.assertEqual(self.native.calls[4][0].hex(), "0105ff040001")
                self.assertEqual(self.native.calls[6][0].hex(), "0105ff110400")
                self.assertEqual(self.native.calls[6][1], 10)
                self.assertTrue(all(0 < timeout <= 2 for body, timeout, _ in self.native.calls if body[3] != 17))
                self.assertTrue(all(args == (False,) for _, _, args in self.native.calls))
                self.assertEqual((self.box.last_cmd, self.box.last_cmd_tmp,
                    self.box.box_state.get_Tn_data(1, "filament"), self.box.preloading_en,
                    self.box.preloading_poweron, self.box.extrude_tnn, self.box.heart_process_enable,
                    self.box.box_state.Tnn_map, self.box.move_z, self.box.store.base_data), before)
                self.assertEqual(self.box.store.path.read_bytes(), stored_bytes)
                self.assert_released()

    def test_initial_error19_is_decoded_as_error_without_normal_state_or_warning(self):
        self.make_box()
        self.queue_success(initial_status=0x13, initial_mode=4)
        self.box.box_state.modify_Tn_data(1, "2", "mode")
        initial = []

        def decode(*args, **kwargs):
            result = decode_reply(*args, **kwargs)
            if args[1].status == 0x13:
                initial.append((result.value, result.effects, self.box.box_state.get_Tn_data(1, "mode")))
            return result

        with patch("cfs.box.decode_reply", side_effect=decode):
            self.run_trial()
        self.assertEqual(initial, [(False, (), "2")])
        self.assertEqual(self.printer.gcode.messages, [])
        self.assert_released()

    def test_passive_command_registration_requires_explicit_confirmation_and_physical_target(self):
        trial = self.make_box()
        command = self.printer.gcode.commands["CFS_TEST_RETRACT"]
        for params in ({"TNN": self.target}, {"TNN": self.target, "CONFIRM": "2"},
                       {"TNN": "T0", "CONFIRM": "1"}, {"CONFIRM": "1"}):
            with self.subTest(params=params), self.assertRaises(CommandError):
                command(NativeCommand(params))
            self.assertEqual(self.native.calls, [])
        for target, confirmed in ((self.target, 1), (self.target, "1"), (None, True)):
            with self.assertRaises(ValueError):
                trial.run_retract(target, confirmed)
        result = command(NativeCommand({"TNN": self.target, "CONFIRM": "1"}))
        self.assertEqual(result["head"], True)
        self.assertIn("not full unload", self.printer.gcode.messages[-1])
        self.assert_released()
        self.make_box(passive=False)
        self.assertNotIn("CFS_TEST_RETRACT", self.printer.gcode.commands)

    def test_only_normal_idle_or_print_and_exact_initial_error_are_admitted(self):
        for status, mode in ((0, 0), (0, 2), (0x13, 4), (0, 4), (0x13, 0),
                             (0x13, 2), (1, 4), (0x30, 0), (0, 1)):
            with self.subTest(status=status, mode=mode):
                self.make_box()
                self.queue_success(initial_status=status, initial_mode=mode)
                if (status, mode) in ((0, 0), (0, 2), (0x13, 4)):
                    self.run_trial()
                else:
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.run_trial()
                    self.assertEqual(len(self.native.calls), 1)
                self.assert_released()

    def test_every_abnormal_write_or_later_state_stops_without_retry_or_followups(self):
        for command, index, count in ((4, 0, 5), (10, 1, 6), (17, 0, 7), (10, 2, 8)):
            for outcome in ("error19", "status2", "status4", "crc", "missing", "payload"):
                with self.subTest(command=command, outcome=outcome, index=index):
                    self.make_box()
                    data = b"\x14\x1e\x00\x00" if command == 10 else b""
                    bad = reply(1, command, 0x13, b"\x14\x1e\x00\x04" if command == 10 else b"")
                    if outcome == "crc":
                        corrupted = bytearray(reply(1, command, data=data))
                        corrupted[-1] ^= 1
                        bad = bytes(corrupted)
                    elif outcome == "missing":
                        bad = None
                    elif outcome in ("status2", "status4"):
                        bad = reply(1, command, int(outcome[-1]), data)
                    elif outcome == "payload":
                        bad = reply(1, command, data=b"unexpected")
                    responses = self.native.responses.setdefault((1, command), [reply(1, command)])
                    responses[index] = bad
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.run_trial()
                    self.assertEqual(len(self.native.calls), count)
                    self.assert_released()

    def test_preflight_material_connections_and_buffer_are_fresh_and_strict(self):
        for command, index, value, count in ((8, 0, 0, 2), (8, 0, 2, 2),
                (8, 1, 0, 3), (8, 1, 2, 3), (8, 1, 12, 3), (5, 0, 3, 4)):
            with self.subTest(command=command, index=index, value=value):
                self.make_box()
                self.native.responses[(1, command)][index] = reply(1, command, data=bytes((value,)))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_trial()
                self.assertEqual(len(self.native.calls), count)
                self.assert_released()

    def test_normal_ack_alone_does_not_establish_buffer_result(self):
        for command, index, data, count in ((10, 2, b"\x14\x1e\x00\x02", 8),
                (8, 2, b"\x00", 9), (8, 3, b"\x08", 10), (5, 1, b"\x00", 11),
                (5, 1, b"\x01", 11)):
            with self.subTest(command=command, index=index, data=data):
                self.make_box()
                self.native.responses[(1, command)][index] = reply(1, command, data=data)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_trial()
                self.assertEqual(len(self.native.calls), count)
                self.assert_released()

    def test_empty_post_connections_and_initial_buffer_states_are_observations(self):
        for before in (0, 1, 2):
            with self.subTest(buffer=before):
                self.make_box()
                self.native.responses[(1, 5)][0] = reply(1, 5, data=bytes((before,)))
                self.native.responses[(1, 8)][3] = reply(1, 8, data=b"\x00")
                self.assertEqual(self.run_trial()["connections"], 0)
                self.assert_released()

    def test_retained_button_integer_head_values_are_accepted_and_reported_as_boolean(self):
        for initial, after in ((0, 0), (0, 1), (1, 1), (1, 0)):
            with self.subTest(initial=initial, after=after):
                self.make_box()
                self.lifecycle.head_sensor.runout_helper.filament_present = initial
                self.queue_success(head_after=after)
                result = self.run_trial()
                self.assertIs(result["head"], bool(after))
                self.assertEqual(len(self.native.calls), 11)
                self.assert_released()

    def test_head_values_outside_exact_boolean_or_button_integer_refuse_without_requests(self):
        for value in (None, 2, -1, 0.0, 1.0, "0", "1", "", [], object()):
            with self.subTest(value=value):
                self.make_box()
                self.lifecycle.head_sensor.runout_helper.filament_present = value
                with self.assertRaises(RuntimeError):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assert_released()

    def test_sources_wrong_slot_and_nonbaseline_devices_refuse_before_any_call(self):
        changes = {
            "last_source": lambda: setattr(self.box, "last_cmd", "T1A"),
            "live_source": lambda: self.box.box_state.modify_Tn_data(1, "T1A", "filament"),
            "half_source": lambda: setattr(self.box, "last_cmd", self.target),
            "encoding": lambda: setattr(self.box.boxcfg, "version", 0),
            "version": lambda: self.box.device_versions.update(1, b"152" + self.native.serial),
            "family": lambda: self.box.device_versions.set_family(1, "cfs6"),
            "other_hub": lambda: setattr(self.table[1], "online", 1),
            "cached_other_hub": lambda: self.box.box_state.modify_Tn_data(2, "connect", "state"),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.make_box()
                change()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assert_released()
        self.make_box("T2C")
        self.assertEqual(self.run_trial()["target"], "T2C")
        self.assertTrue(all(body[0] == 2 for body, _, _ in self.native.calls))
        self.make_box()
        with self.assertRaises(RuntimeError):
            self.box.preload_trial.run_retract("T1A", confirmed=True)
        self.assertEqual(len(self.native.calls), 3)
        self.assert_released()

    def test_host_safety_and_missing_observations_refuse_before_any_call(self):
        changes = {
            "printing": lambda: setattr(self.box.print_stats, "state", "printing"),
            "paused": lambda: setattr(self.printer.objects["pause_resume"], "is_paused", True),
            "sd": lambda: setattr(self.box.virtual_sdcard, "is_active", lambda: True),
            "resume": lambda: setattr(self.box.virtual_sdcard, "do_resume_status", True),
            "error": lambda: setattr(self.box, "error", "filament_err"),
            "inside_error": lambda: setattr(self.box, "inside_error", True),
            "extrude_error": lambda: setattr(self.box.box_state, "extrude_err", True),
            "refill": lambda: setattr(self.box, "inside_auto_refill", True),
            "ending_material": lambda: setattr(self.box, "is_use_ending_material", True),
            "all_materials": lambda: setattr(self.box, "inside_extrusion_all_materials", True),
            "operation": lambda: self.box.operations.begin(None, self.target, 1),
            "material_trial": lambda: setattr(self.box.material_trial, "_token", object()),
            "native_offline": lambda: setattr(self.native, "is_connected", False),
            "offline": lambda: setattr(self.table[0], "online", 0),
            "no_owner": lambda: setattr(self, "dispatch", None),
            "head_unknown": lambda: setattr(self.lifecycle.head_sensor.runout_helper, "filament_present", None),
            "missing_head": lambda: setattr(self.lifecycle, "head_sensor", None),
            "missing_pause": lambda: delattr(self.printer.objects["pause_resume"], "is_paused"),
            "missing_sd": lambda: delattr(self.box.virtual_sdcard, "do_resume_status"),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.make_box()
                change()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertFalse(self.box.preload_trial.active)
                self.assertEqual(self.printer.gcode.scripts, [])

    def test_head_must_remain_stable_until_buffer_is_issued(self):
        for command, index, count in ((10, 0, 1), (5, 0, 4), (4, 0, 5), (10, 1, 6)):
            with self.subTest(command=command, index=index):
                self.make_box()

                def change():
                    self.lifecycle.head_sensor.runout_helper.filament_present = False
                    return reply(1, command, data=b"\x14\x1e\x00\x00" if command == 10 else b"")

                self.native.responses.setdefault((1, command), [None])[index] = change
                with self.assertRaises(RuntimeError):
                    self.run_trial()
                self.assertEqual(len(self.native.calls), count)
                self.assert_released()

    def test_owner_context_configuration_and_source_changes_stop_inflight_followups(self):
        changes = {
            "shutdown": lambda: self.printer.send_event("klippy:shutdown"),
            "unbind": lambda: self.box.unbind_native(),
            "disconnect": lambda: self.lifecycle.disconnect(1),
            "identity": lambda: self.box._accept_identity(1, b"113" + b"B" * 19, b"replacement"),
            "binding": lambda: setattr(self.box, "_binding_generation", self.box._binding_generation + 1),
            "owner": lambda: setattr(self, "dispatch", object()),
            "configuration": lambda: setattr(self.box.boxcfg, "flush_volume", 99),
            "source": lambda: setattr(self.box, "last_cmd", self.target),
            "deadline": lambda: setattr(self.reactor, "time", self.reactor.time + 60),
            "cancel": lambda: self.box.preload_trial.cancel(),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.make_box()

                def interrupt():
                    change()
                    return reply(1, 17)

                self.native.responses[(1, 17)] = [interrupt]
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(len(self.native.calls), 7)
                self.assert_released()

    def test_other_callbacks_cannot_borrow_reenter_duplicate_or_reorder_requests(self):
        self.make_box()
        original = self.box.send_native
        captured = []

        def capture(request, *args, **kwargs):
            if request.command == 17:
                captured.append(request)
            return original(request, *args, **kwargs)

        def reenter():
            with self.assertRaises(RuntimeError):
                self.run_trial()
            with self.assertRaises(RuntimeError):
                self.box.preload_trial.run(self.target, confirmed=True)
            with self.assertRaises(RuntimeError):
                self.box.material_trial.run("UNLOAD", self.target, confirmed=True)
            for request in (captured[0], build_request(1, "RETRUDE_PROCESS", b"\x04\x00"),
                    build_request(1, "SET_BOX_MODE", b"\x00\x01"),
                    diagnostic_request(1, 255, 17, b"\x04\x00")):
                with self.assertRaises((RuntimeError, ValueError)):
                    original(request)
            with self.assertRaises(RuntimeError):
                self.box.host_guard()
            with self.assertRaises(UnsupportedRequest):
                self.box.preload_trial._send("GET_BOX_STATE")
            return reply(1, 17)

        self.box.send_native = capture
        self.native.responses[(1, 17)] = [reenter]
        self.run_trial()
        self.assertEqual(len(self.native.calls), 11)
        self.assert_released()

    def test_a_different_dispatch_cannot_reuse_the_exact_pending_request(self):
        self.make_box()
        original = self.box.send_native
        captured = []

        def capture(request, *args, **kwargs):
            captured[:] = [request]
            return original(request, *args, **kwargs)

        def borrow():
            owner, self.dispatch = self.dispatch, object()
            try:
                with self.assertRaises(RuntimeError):
                    original(captured[0])
            finally:
                self.dispatch = owner
            return reply(1, 17)

        self.box.send_native = capture
        self.native.responses[(1, 17)] = [borrow]
        with self.assertRaises(RuntimeError):
            self.run_trial()
        self.assertEqual(len(self.native.calls), 7)
        self.assert_released()

    def test_native_fault_releases_permission_without_cleanup(self):
        self.make_box()

        def fail():
            self.box.preload_trial.cancel()
            self.assertTrue(self.box.preload_trial.active)
            with self.assertRaises(RuntimeError):
                self.run_trial()
            raise OSError("synthetic native failure")

        self.native.responses[(1, 17)] = [fail]
        with self.assertRaises((OSError, RuntimeError)):
            self.run_trial()
        self.assertEqual(len(self.native.calls), 7)
        self.assert_released()
        self.queue_success()
        self.run_trial()
        self.assert_released()

    def test_explicit_material_command_issues_only_one_selected_material_with_stock_timeout(self):
        for known_source in (False, True):
            with self.subTest(known_source=known_source):
                self.make_box()
                self.queue_material(initial_status=0x13, initial_mode=4)
                self.lifecycle.head_sensor.runout_helper.filament_present = 1
                self.box.last_cmd_tmp = "T3B"
                if known_source:
                    self.box.last_cmd = self.target
                    self.box.box_state.modify_Tn_data(1, self.target, "filament")
                sources = (self.box.last_cmd, self.box.last_cmd_tmp, self.box.box_state.get_Tn_data(1, "filament"))
                stored = self.box.store.path.read_bytes()
                result = self.printer.gcode.commands["CFS_TEST_RETRACT"](NativeCommand(
                    {"TNN": "T1C", "TRIGGER": "MATERIAL", "CONFIRM": "1"}))
                self.assertEqual(result, {"target": "T1C", "head": False, "mode": 0,
                    "material": 15, "connections": 0, "buffer": 2})
                self.assertEqual(self.native.commands(), [(1, 10, b""), (1, 8, b"\x00"),
                    (1, 8, b"\x01"), (1, 5, b""), (1, 4, b"\x00\x01"), (1, 10, b""),
                    (1, 17, b"\x04\x01"), (1, 10, b""), (1, 8, b"\x00"),
                    (1, 8, b"\x01"), (1, 5, b"")])
                self.assertEqual(self.native.calls[6][0].hex(), "0105ff110401")
                self.assertEqual(self.native.calls[6][1:], (150, (False,)))
                self.assertEqual((self.box.last_cmd, self.box.last_cmd_tmp,
                    self.box.box_state.get_Tn_data(1, "filament")), sources)
                self.assertEqual(self.box.store.path.read_bytes(), stored)
                self.assertIn("MATERIAL observations", self.printer.gcode.messages[-1])
                self.assert_released()

    def test_invalid_trigger_refuses_before_native_calls(self):
        self.make_box()
        for trigger in (None, 0, True, "", "buffer", "material", "MATERIAL\nCUT", "BOTH"):
            with self.subTest(trigger=trigger), self.assertRaises(ValueError):
                self.box.preload_trial.run_retract(self.target, confirmed=True, trigger=trigger)
            self.assertEqual(self.native.calls, [])
        with self.assertRaises(CommandError):
            self.printer.gcode.commands["CFS_TEST_RETRACT"](NativeCommand(
                {"TNN": "T1C", "TRIGGER": "BOTH", "CONFIRM": "1"}))
        self.assert_released()

    def test_material_abnormal_or_malformed_ack_stops_at_one_call_without_post_reads(self):
        for status, data in ((0x13, b""), (2, b""), (4, b""), (0, b"unexpected")):
            with self.subTest(status=status, data=data):
                self.make_box()
                self.queue_material()
                self.native.responses[(1, 17)] = [reply(1, 17, status, data), reply(1, 17)]
                with self.assertRaises((RuntimeError, ValueError)):
                    self.box.preload_trial.run_retract(self.target, confirmed=True, trigger="MATERIAL")
                self.assertEqual(len(self.native.calls), 7)
                self.assertEqual(self.native.commands()[-1], (1, 17, b"\x04\x01"))
                self.assertEqual(len(self.native.responses[(1, 17)]), 1)
                self.assert_released()

    def test_initial_error25_material_retract_keeps_error_semantics_and_issues_one_reset_and_pull(self):
        self.make_box()
        self.queue_material(initial_status=0x19, initial_mode=4)
        self.lifecycle.head_sensor.runout_helper.filament_present = 0
        self.box.last_cmd = self.target
        self.box.box_state.modify_Tn_data(1, self.target, "filament")
        self.box.box_state.modify_Tn_data(1, "2", "mode")
        observed_error = []

        def decode(*args, **kwargs):
            result = decode_reply(*args, **kwargs)
            if args[1].status == 0x19:
                observed_error.append((result.value, result.effects, self.box.box_state.get_Tn_data(1, "mode")))
            return result

        with patch("cfs.box.decode_reply", side_effect=decode):
            result = self.box.preload_trial.run_retract(self.target, confirmed=True, trigger="MATERIAL")
        self.assertEqual(observed_error, [(False, (), "2")])
        self.assertEqual(result["head"], False)
        self.assertEqual([(command, data) for _, command, data in self.native.commands() if command in (4, 17)],
                         [(4, b"\x00\x01"), (17, b"\x04\x01")])
        self.assertEqual(len(self.native.calls), 11)
        self.assertEqual(self.box.last_cmd, self.target)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), self.target)
        self.assert_released()

    def test_error25_on_writes_or_later_state_stops_before_any_additional_native_call(self):
        for command, index, count in ((4, 0, 5), (10, 1, 6), (17, 0, 7), (10, 2, 8)):
            with self.subTest(command=command, index=index):
                self.make_box()
                self.queue_material(initial_status=0x19, initial_mode=4)
                data = b"\x14\x1e\x00\x04" if command == 10 else b""
                responses = self.native.responses.setdefault((1, command), [reply(1, command)])
                responses[index] = reply(1, command, 0x19, data)
                with self.assertRaises(RuntimeError):
                    self.box.preload_trial.run_retract(self.target, confirmed=True, trigger="MATERIAL")
                self.assertEqual(len(self.native.calls), count)
                self.assert_released()

    def test_initial_material_error_recovery_rejects_unknown_status_wrong_mode_and_wrong_shape(self):
        for status, data in ((0x18, b"\x14\x1e\x00\x04"), (0x1A, b"\x14\x1e\x00\x04"),
                (0x50, b"\x14\x1e\x00\x04"), (0x19, b"\x14\x1e\x00\x00"),
                (0x19, b"\x14\x1e\x00\x02"), (0x19, b"\x14\x1e\x04"),
                (0x19, b"\x14\x1e\x00\x04\x00")):
            with self.subTest(status=status, data=data):
                self.make_box()
                self.queue_material()
                self.native.responses[(1, 10)][0] = reply(1, 10, status, data)
                with self.assertRaises(RuntimeError):
                    self.box.preload_trial.run_retract(self.target, confirmed=True, trigger="MATERIAL")
                self.assertEqual(len(self.native.calls), 1)
                self.assert_released()

    def test_material_requires_fresh_empty_head_connections_buffer_and_idle(self):
        for case, count in (("head", 11), ("connections", 10), ("buffer", 11), ("mode", 8), ("material", 9)):
            with self.subTest(case=case):
                self.make_box()
                self.queue_material()
                if case == "head":
                    self.native.responses[(1, 17)] = [reply(1, 17)]
                else:
                    command, index, data = {"connections": (8, 3, b"\x04"), "buffer": (5, 1, b"\x00"),
                        "mode": (10, 2, b"\x14\x1e\x00\x02"), "material": (8, 2, b"\x00")}[case]
                    self.native.responses[(1, command)][index] = reply(1, command, data=data)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.box.preload_trial.run_retract(self.target, confirmed=True, trigger="MATERIAL")
                self.assertEqual(len(self.native.calls), count)
                self.assert_released()

    def test_material_uses_240_second_cooperative_budget_and_bounds_remaining_native_wait(self):
        for expire in (False, True):
            with self.subTest(expire=expire):
                self.make_box()
                self.queue_material()

                def consume_budget():
                    self.reactor.time += 240 if expire else 180
                    return reply(1, 10, data=b"\x14\x1e\x00\x00")

                self.native.responses[(1, 10)][1] = consume_budget
                if expire:
                    with self.assertRaises(RuntimeError):
                        self.box.preload_trial.run_retract(self.target, confirmed=True, trigger="MATERIAL")
                    self.assertEqual(len(self.native.calls), 6)
                else:
                    self.box.preload_trial.run_retract(self.target, confirmed=True, trigger="MATERIAL")
                    self.assertEqual(self.native.calls[6][1], 60)
                self.assert_released()

    def test_material_keeps_head_stable_until_issued_and_never_follows_buffer_automatically(self):
        self.make_box()
        self.queue_material()

        def changed_head():
            self.lifecycle.head_sensor.runout_helper.filament_present = False
            return reply(1, 10, data=b"\x14\x1e\x00\x00")

        self.native.responses[(1, 10)][1] = changed_head
        with self.assertRaises(RuntimeError):
            self.box.preload_trial.run_retract(self.target, confirmed=True, trigger="MATERIAL")
        self.assertEqual(len(self.native.calls), 6)
        self.assert_released()
        self.make_box()
        self.native.responses[(1, 17)] = [reply(1, 17, 0x13)]
        with self.assertRaises(RuntimeError):
            self.run_trial()
        self.assertEqual([data for _, command, data in self.native.commands() if command == 17], [b"\x04\x00"])
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
