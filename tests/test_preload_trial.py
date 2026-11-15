"""One explicit preload admission with synthetic owners; no printer or native .so."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.box import Box, load_config
from cfs.native import UnsupportedRequest, build_request, diagnostic_request
from test_box_data import CommandError
from test_box_native import NativeCommand
from test_configuration import FakeConfig
from test_lifecycle import Native, Printer, Reactor
from test_native import reply


TAG = "01234567890" + "100001" + "0ABCDEF" + "x" * 16


class PreloadTrialTests(unittest.TestCase):
    def make_box(self, target="T1A", head="head", passive=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.address, self.slot = int(target[1]), target[2]
        self.mask = 1 << "ABCD".index(self.slot)
        values = {"bus": "custom", "version": 1}
        if head is not None:
            values["filament_sensor"] = head
        self.box = Box(FakeConfig(values), self.root)
        self.native, self.reactor = Native(), Reactor()
        self.native.mode = 0
        self.printer = Printer(self.native, self.reactor, head)
        self.printer.objects["virtual_sdcard"].is_active = lambda: False
        self.printer.objects["pause_resume"] = SimpleNamespace(is_paused=False)
        self.dispatch = object()
        self.port_present = True
        self.table = [SimpleNamespace(online=int(index + 1 == self.address)) for index in range(4)]
        self.lifecycle = self.box.register_lifecycle(
            self.printer, passive=passive,
            families=dict.fromkeys(range(1, 5), "cfs0"), address_table=self.table,
            port_exists=lambda path: self.port_present,
            current_dispatch=lambda: self.dispatch)
        self.printer.send_event("klippy:ready")
        self.lifecycle.get_connect_state()
        self.native.calls.clear()
        self.printer.gcode.scripts.clear()
        self.target = target
        self.queue_success()
        return self.box.preload_trial

    def queue_success(self, rfid=TAG):
        self.native.responses = {
            (self.address, 0x08): [reply(self.address, 0x08, data=bytes((self.mask,))),
                                     reply(self.address, 0x08, data=b"\x00")],
            (self.address, 0x02): [reply(self.address, 0x02,
                                       data=(self.slot + ":" + rfid + ";").encode("latin-1"))],
            (self.address, 0x03): [reply(self.address, 0x03, data=bytes((11, 22, 33, 44)))],
        }

    def assert_released(self):
        self.assertFalse(self.box.preload_trial.active)
        self.assertTrue(self.lifecycle.passive)
        self.assertEqual(self.printer.gcode.scripts, [])
        if self.box.native is not None:
            before = len(self.native.calls)
            with self.assertRaises(UnsupportedRequest):
                self.box.send_native(build_request(self.address, "SET_PRE_LOADING", bytes((self.mask, 2))))
            self.assertEqual(len(self.native.calls), before)

    def run_trial(self):
        return self.box.preload_trial.run(self.target, confirmed=True)

    def run_count(self):
        return sum(command == 0x0D for _, command, _ in self.native.commands())

    def test_real_entry_is_passive_and_registers_an_explicit_extension_command(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        native, reactor = Native(), Reactor()
        printer = Printer(native, reactor, "head")
        printer.load_object = lambda config, name: printer.lookup_object(name)
        config = FakeConfig({"bus": "custom", "version": 1, "filament_sensor": "head"})
        config.get_printer = lambda: printer
        base_info = ModuleType("extras.base_info")
        base_info.base_dir = directory.name
        with patch.dict(sys.modules, {"extras.base_info": base_info}):
            box = load_config(config)
        self.assertTrue(box.lifecycle.passive)
        self.assertFalse(box.preload_trial.active)
        self.assertIn("CFS_TEST_PRELOAD", printer.gcode.commands)
        self.assertEqual(native.calls, [])
        self.assertEqual(reactor.timers, [])
        self.assertEqual(printer.gcode.scripts, [])
        for name in ("BOX_SET_PRE_LOADING", "BOX_EXTRUDE_MATERIAL", "BOX_CUT_MATERIAL", "T0"):
            with self.subTest(command=name), self.assertRaises(CommandError):
                printer.gcode.commands[name](NativeCommand({}))
        self.assertEqual(native.calls, [])

    def test_confirmation_and_physical_target_are_required_before_native_calls(self):
        trial = self.make_box()
        for target, confirmed in (("T1A", False), ("T1A", 1), ("T1A", "1"),
                                  (None, True), ("T0", True), ("T5A", True),
                                  ("T1AA", True), ("t1a", True), ("T1A\nT0", True)):
            with self.subTest(target=target, confirmed=confirmed), self.assertRaises(ValueError):
                trial.run(target, confirmed=confirmed)
            self.assertEqual(self.native.calls, [])
            self.assert_released()
        for parameters in ({"TNN": "T1A"}, {"TNN": "T1A", "CONFIRM": "0"},
                           {"TNN": "T1A", "CONFIRM": "2"}, {"CONFIRM": "1"}):
            with self.subTest(parameters=parameters), self.assertRaises(CommandError):
                self.printer.gcode.commands["CFS_TEST_PRELOAD"](NativeCommand(parameters))
        self.assertEqual(self.native.calls, [])

    def test_selected_physical_slot_ignores_remap_and_preserves_other_metadata(self):
        self.make_box("T2C")
        self.box.box_state.Tnn_map["T2C"] = "T1A"
        preferences = deepcopy((self.box.preloading_en, self.box.preloading_poweron,
                                self.box.last_cmd, self.box.last_cmd_tmp, self.box.extrude_tnn,
                                self.box.heart_process_enable, self.box.box_state.Tnn_map))
        before = deepcopy(self.box.store.base_data)
        result = self.run_trial()
        self.assertEqual(result, {"target": "T2C", "rfid": TAG, "remaining": 33})
        self.assertEqual(self.native.commands(), [(2, 0x0A, b""), (2, 0x08, b"\x00"),
                         (2, 0x08, b"\x01"), (2, 0x0D, b"\x04\x02"), (2, 0x0A, b""),
                         (2, 0x02, b"\x04"), (2, 0x03, b"\x04")])
        self.assertEqual(self.native.calls[3][1:], (300, (False,)))
        self.assertTrue(all(arguments == (False,) for _, _, arguments in self.native.calls))
        self.assertTrue(all(0 < timeout <= 2 for body, timeout, _ in self.native.calls if body[3] != 0x0D))
        for address in range(1, 5):
            for field in ("vender", "color_value", "material_type", "remain_len"):
                for index in range(4):
                    if (address, index) != (2, 2):
                        self.assertEqual(self.box.store.base_data["T%d" % address][field][index],
                                         before["T%d" % address][field][index])
        self.assertEqual(self.box.store.base_data["T2"]["vender"][2], TAG)
        self.assertEqual(self.box.store.base_data["T2"]["remain_len"][2], "33")
        self.assertEqual((self.box.preloading_en, self.box.preloading_poweron, self.box.last_cmd,
                          self.box.last_cmd_tmp, self.box.extrude_tnn,
                          self.box.heart_process_enable, self.box.box_state.Tnn_map), preferences)
        self.assert_released()

    def test_explicit_command_returns_observations_without_enabling_future_commands(self):
        self.make_box()
        result = self.printer.gcode.commands["CFS_TEST_PRELOAD"](
            NativeCommand({"TNN": "T1A", "CONFIRM": "1"}))
        self.assertEqual(result, {"target": "T1A", "rfid": TAG, "remaining": 11})
        self.assertTrue(any("observations" in message for message in self.printer.gcode.messages))
        self.assert_released()

    def test_unknown_and_none_keep_the_original_cache_distinction(self):
        for value in ("unknown", "none"):
            with self.subTest(rfid=value):
                self.make_box()
                for part, data in (("vender", TAG), ("color_value", "0FFFFFF"),
                                   ("material_type", "100002")):
                    self.box.box_state.modify_Tn_data(1, data, part, "A")
                self.queue_success(value)
                result = self.run_trial()
                self.assertEqual(result["rfid"], value)
                self.assertEqual(self.box.box_state.Tn_data["T1"]["vender"][0], value)
                self.assertEqual(self.box.store.base_data["T1"]["vender"][0],
                                 "unknown" if value == "unknown" else TAG)
                self.assertEqual(self.box.store.base_data["T1"]["color_value"][0], "0FFFFFF")
                self.assert_released()

    def test_unsafe_host_states_refuse_before_any_request(self):
        changes = {
            "printing": lambda: setattr(self.box.print_stats, "state", "printing"),
            "paused": lambda: setattr(self.box.print_stats, "state", "paused"),
            "active_sd": lambda: setattr(self.box.virtual_sdcard, "is_active", lambda: True),
            "pause_owner": lambda: setattr(self.printer.objects["pause_resume"], "is_paused", True),
            "error": lambda: setattr(self.box, "error", "filament_err"),
            "error_handler": lambda: setattr(self.box, "inside_error", True),
            "operation": lambda: self.box.operations.begin(None, "T1A", 1),
            "head_present": lambda: setattr(self.lifecycle.head_sensor.runout_helper, "filament_present", True),
            "head_unknown": lambda: setattr(self.lifecycle.head_sensor.runout_helper, "filament_present", None),
            "native_offline": lambda: setattr(self.native, "is_connected", False),
            "address_offline": lambda: setattr(self.table[0], "online", 0),
            "port_missing": lambda: setattr(self, "port_present", False),
            "no_owner": lambda: setattr(self, "dispatch", None),
        }
        for name, change in changes.items():
            with self.subTest(state=name):
                self.make_box()
                change()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assert_released()

    def test_baseline_encoding_and_actual_head_sensor_are_required(self):
        for case in ("encoding", "version", "family", "no_sensor", "active_lifecycle", "not_ready"):
            with self.subTest(case=case):
                self.make_box(head=None if case == "no_sensor" else "head")
                if case == "encoding":
                    self.box.boxcfg.version = 0
                elif case == "version":
                    self.box.device_versions.update(1, b"152" + self.native.serial)
                elif case == "family":
                    self.box.device_versions.set_family(1, "cfs6")
                elif case == "active_lifecycle":
                    self.lifecycle.passive = False
                elif case == "not_ready":
                    self.printer.send_event("klippy:shutdown")
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertFalse(self.box.preload_trial.active)
                self.assertEqual(self.printer.gcode.scripts, [])

    def test_missing_observations_produce_command_errors_before_any_request(self):
        changes = {
            "pause_owner": lambda: self.printer.objects.pop("pause_resume"),
            "pause_flag": lambda: delattr(self.printer.objects["pause_resume"], "is_paused"),
            "sd_observation": lambda: delattr(self.box.virtual_sdcard, "is_active"),
            "sd_not_callable": lambda: setattr(self.box.virtual_sdcard, "is_active", False),
            "head_helper": lambda: delattr(self.lifecycle.head_sensor, "runout_helper"),
            "head_observation": lambda: delattr(self.lifecycle.head_sensor.runout_helper, "filament_present"),
        }
        for name, change in changes.items():
            with self.subTest(missing=name):
                self.make_box()
                change()
                with self.assertRaises(CommandError):
                    self.printer.gcode.commands["CFS_TEST_PRELOAD"](
                        NativeCommand({"TNN": "T1A", "CONFIRM": "1"}))
                self.assertEqual(self.native.calls, [])
                self.assert_released()

    def test_fresh_material_and_empty_shared_connections_are_required(self):
        for material, connections in ((0, 0), (2, 0), (1, 1), (1, 8), (1, 15)):
            with self.subTest(material=material, connections=connections):
                self.make_box()
                self.native.responses[(1, 0x08)] = [reply(1, 0x08, data=bytes((material,))),
                                                    reply(1, 0x08, data=bytes((connections,)))]
                with self.assertRaises(RuntimeError):
                    self.run_trial()
                self.assertEqual(self.run_count(), 0)
                self.assert_released()

    def test_only_fresh_normal_idle_states_allow_preload_and_followup_reads(self):
        for phase in ("before", "after"):
            for mode, status in ((1, 0), (4, 0), (255, 0), (0, 0x30)):
                with self.subTest(phase=phase, mode=mode, status=status):
                    self.make_box()
                    bad = reply(1, 0x0A, status, bytes((20, 30, 0, mode)))
                    self.native.responses[(1, 0x0A)] = ([bad] if phase == "before" else
                        [reply(1, 0x0A, data=bytes((20, 30, 0, 0))), bad])
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.run_trial()
                    self.assertEqual(self.run_count(), int(phase == "after"))
                    self.assertFalse(any(command in (2, 3) for _, command, _ in self.native.commands()))
                    self.assert_released()

    def test_run_missing_event_error_and_malformed_ack_never_trigger_a_host_resend(self):
        bad_crc = bytearray(reply(1, 0x0D))
        bad_crc[-1] ^= 1
        outcomes = (None, bytes(bad_crc), b"short", reply(1, 0x0D, 2), reply(1, 0x0D, 4),
                    reply(1, 0x0D, 0x30, b"\x00" * 4), reply(1, 0x0D, data=b"unexpected"))
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.make_box()
                self.native.responses[(1, 0x0D)] = [outcome, reply(1, 0x0D)]
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.run_count(), 1)
                self.assertEqual(len(self.native.responses[(1, 0x0D)]), 1)
                self.assertEqual(len(self.native.calls), 4)
                self.assert_released()

    def test_rfid_must_contain_only_the_selected_record_before_persistence(self):
        bad_values = (b"A:busy;", b"B:unknown;", b"A:unknown;B:unknown;",
                      b"A:unknown;A:unknown;", b"A:unknown", b"", b"A:" + b"x" * 39 + b";")
        for data in bad_values:
            with self.subTest(data=data):
                self.make_box()
                before = self.box.store.path.read_bytes()
                self.native.responses[(1, 0x02)] = [reply(1, 0x02, data=data)]
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.box.store.path.read_bytes(), before)
                self.assertFalse(any(command == 3 for _, command, _ in self.native.commands()))
                self.assert_released()

    def test_later_failure_retains_real_rfid_update_without_fabricating_remaining(self):
        self.make_box()
        old_remaining = deepcopy(self.box.store.base_data["T1"]["remain_len"])
        self.native.responses[(1, 0x03)] = [reply(1, 0x03, data=b"\x01\x02\x03")]
        with self.assertRaises(ValueError):
            self.run_trial()
        self.assertEqual(self.box.store.base_data["T1"]["vender"][0], TAG)
        self.assertEqual(self.box.store.base_data["T1"]["remain_len"], old_remaining)
        self.assert_released()

    def test_host_commands_diagnostics_and_background_actions_cannot_inherit_the_grant(self):
        self.make_box()
        captured = []
        original_send = self.box.send_native

        def capture(request, *args, **kwargs):
            if request.command == 0x0D:
                captured.append(request)
            return original_send(request, *args, **kwargs)

        def reenter():
            self.assertTrue(self.box.preload_trial.active)
            self.assertTrue(self.lifecycle.passive)
            for name in ("BOX_SET_PRE_LOADING", "BOX_EXTRUDE_MATERIAL", "BOX_CUT_MATERIAL", "T0"):
                with self.subTest(command=name), self.assertRaises(CommandError):
                    self.printer.gcode.commands[name](NativeCommand({"ACTION": "OPEN"}))
            with self.assertRaises(RuntimeError):
                self.box.host_guard()
            for request in (build_request(1, "SET_PRE_LOADING", b"\x01\x02"),
                            diagnostic_request(1, 255, 13, b"\x01\x02"), captured[0]):
                with self.assertRaises((RuntimeError, ValueError)):
                    original_send(request)
            self.printer.send_event("box:extrude_process_stage7")
            self.lifecycle.observe_state_event(1, bytes((0, 0, 0, 0)))
            self.lifecycle.schedule_tighten_up()
            self.lifecycle.run_gcode("G0 E100")
            self.lifecycle.report_error("key_timeout", {"address": 1})
            self.assertIsNone(self.box.last_err)
            return reply(1, 0x0D)

        self.box.send_native = capture
        self.native.responses[(1, 0x0D)] = [reenter]
        self.run_trial()
        self.assertEqual(self.run_count(), 1)
        self.assert_released()

    def test_nested_and_other_dispatch_invocations_do_not_acquire_permission(self):
        self.make_box()
        owner = self.dispatch

        def reenter():
            with self.assertRaises(RuntimeError):
                self.run_trial()
            self.dispatch = object()
            try:
                with self.assertRaises(RuntimeError):
                    self.run_trial()
                with self.assertRaises(UnsupportedRequest):
                    self.box.send_native(build_request(1, "SET_PRE_LOADING", b"\x01\x02"))
            finally:
                self.dispatch = owner
            return reply(1, 0x0D)

        self.native.responses[(1, 0x0D)] = [reenter]
        self.run_trial()
        self.assertEqual(self.run_count(), 1)
        self.assert_released()

    def test_shutdown_unbind_disconnect_and_identity_change_revoke_inflight_permission(self):
        changes = {
            "shutdown": lambda: self.printer.send_event("klippy:shutdown"),
            "unbind": lambda: self.box.unbind_native(),
            "disconnect": lambda: self.lifecycle.disconnect(1),
            "replacement": lambda: self.box._accept_identity(1, b"113" + b"B" * 19, b"replacement"),
            "version": lambda: self.box._accept_identity(1, b"152" + self.native.serial, b"changed"),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.make_box()

                def interrupt():
                    change()
                    return reply(1, 0x0D)

                self.native.responses[(1, 0x0D)] = [interrupt]
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(len(self.native.calls), 4)
                self.assert_released()

    def test_error_idle_head_owner_and_deadline_changes_stop_followup_requests(self):
        changes = {
            "error": lambda: setattr(self.box, "error", "filament_err"),
            "printing": lambda: setattr(self.box.print_stats, "state", "printing"),
            "head": lambda: setattr(self.lifecycle.head_sensor.runout_helper, "filament_present", True),
            "owner": lambda: setattr(self, "dispatch", object()),
            "deadline": lambda: setattr(self.reactor, "time", self.reactor.time + 300),
            "cancel": lambda: self.box.preload_trial.cancel(),
            "passive_mode": lambda: setattr(self.lifecycle, "passive", False),
            "encoding": lambda: setattr(self.box.boxcfg, "version", 0),
            "lifecycle_owner": lambda: setattr(self.box, "lifecycle", None),
            "pause_observation_lost": lambda: delattr(self.printer.objects["pause_resume"], "is_paused"),
            "sd_observation_lost": lambda: delattr(self.box.virtual_sdcard, "is_active"),
            "head_observation_lost": lambda: delattr(self.lifecycle.head_sensor, "runout_helper"),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.make_box()

                def interrupt():
                    change()
                    return reply(1, 0x0D)

                self.native.responses[(1, 0x0D)] = [interrupt]
                with self.assertRaises(RuntimeError):
                    self.run_trial()
                self.assertEqual(len(self.native.calls), 4)
                # The injected context mutation is external to the trial.
                # Restore the fixture only after proving it stopped the old call.
                self.box.lifecycle, self.lifecycle.passive = self.lifecycle, True
                self.assert_released()

    def test_cancellation_keeps_the_old_native_call_owned_until_it_returns(self):
        self.make_box()

        def interrupt():
            self.box.preload_trial.cancel()
            self.assertTrue(self.box.preload_trial.active)
            with self.assertRaises(RuntimeError):
                self.run_trial()
            return reply(1, 0x0D)

        self.native.responses[(1, 0x0D)] = [interrupt]
        with self.assertRaises(RuntimeError):
            self.run_trial()
        self.assertEqual(self.run_count(), 1)
        self.assert_released()
        self.queue_success()
        self.assertEqual(self.run_trial()["target"], "T1A")
        self.assertEqual(self.run_count(), 2)
        self.assert_released()

    def test_native_exception_unwinds_admission_without_mechanical_cleanup(self):
        self.make_box()

        def fail():
            raise OSError("synthetic transport failure")

        self.native.responses[(1, 0x0D)] = [fail]
        with self.assertRaises(OSError):
            self.run_trial()
        self.assertEqual(self.run_count(), 1)
        self.assertEqual(len(self.native.calls), 4)
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
