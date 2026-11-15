"""Exercise isolated load/unload admission with real phases and PC substitutes."""

from copy import deepcopy
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.box import Box
from cfs.native import UnsupportedRequest, build_request, diagnostic_request
from test_box_data import CommandError
from test_configuration import FakeConfig
from test_host_actions import HostCommand, HostPrinter
from test_lifecycle import Native, Reactor
from test_native import reply


class MaterialTrialTests(unittest.TestCase):
    def make_box(self, action="LOAD", target="T1A", **settings):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        values = {"bus": "custom", "version": 1, "has_extrude_pos": 1,
                  "filament_sensor": "head", "pre_cut_pos_x": 4,
                  "pre_cut_pos_y": 210, "cut_pos_y": 225,
                  "switch_pin": "synthetic_pin" if action == "UNLOAD" else None}
        values.update(settings)
        self.box = Box(FakeConfig(values), self.root)
        self.native, self.reactor = Native(), Reactor()
        self.printer = HostPrinter(self.native, self.reactor)
        self.gcode = self.printer.gcode
        self.address, self.slot = int(target[1]), target[2]
        self.mask = 1 << "ABCD".index(self.slot)
        self.target, self.action = target, action
        self.native.mode = 0 if action == "LOAD" else 2
        self.printer.heater.min_temp, self.printer.heater.max_temp = 0, 300
        self.printer.heater.target = 0
        self.printer.heater.min_extrude_temp = 170
        self.printer.heater.get_temp = lambda eventtime: (self.printer.heater.smoothed_temp,
                                                        self.printer.heater.target)
        self.printer.heater.get_status = lambda eventtime: {
            "temperature": self.printer.heater.smoothed_temp,
            "target": self.printer.heater.target}
        self.printer.objects["virtual_sdcard"].is_active = lambda: False
        self.printer.objects["pause_resume"] = SimpleNamespace(is_paused=False)
        self.dispatch = object()
        self.port_present = True
        self.homed_axes = "xyz"
        self.printer.objects["toolhead"].get_status = lambda now: {"homed_axes": self.homed_axes}
        self.table = [SimpleNamespace(online=int(index + 1 == self.address)) for index in range(4)]
        self.lifecycle = self.box.register_lifecycle(
            self.printer, passive=True,
            families=dict.fromkeys(range(1, 5), "cfs0"), address_table=self.table,
            port_exists=lambda path: self.port_present,
            current_dispatch=lambda: self.dispatch)
        self.printer.send_event("klippy:ready")
        self.lifecycle.get_connect_state()
        self.material = self.mask
        self.connections = 0 if action == "LOAD" else self.mask
        self.buffer = 2
        self.fire_head = True
        self.fire_cut = self.release_cut = True
        self.full_buffer_after_head = False
        self.clear_head_on_retract = True
        self.events, self.on_native = [], None
        self.outcomes = {}
        self.set_head(action == "UNLOAD")
        if action == "UNLOAD":
            self.box.last_cmd = target
            self.box.update_filament_pos(target, target)
        original = self.native.cmd_send_data_with_response

        def exchange(body, timeout, *args):
            address, command, data = body[0], body[3], body[4:]
            self.events.append(("native", command, data))
            raw = original(body, timeout, *args)
            if self.on_native is not None:
                self.on_native(address, command, data)
            key = (command, data)
            if key in self.outcomes:
                outcome = self.outcomes[key]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome() if callable(outcome) else outcome
            if command == 0x0A:
                return reply(address, command, data=bytes((20, 30, 0, self.native.mode)))
            if command == 0x08:
                return reply(address, command,
                             data=bytes((self.material if data == b"\x00" else self.connections,)))
            if command == 0x05:
                return reply(address, command, data=bytes((self.buffer,)))
            if command == 0x04:
                self.native.mode = 2 if data[1] == 0 else 0
            if command == 0x10:
                if data[1] == 0:
                    self.connections = self.mask
                if data[1] == 4 and self.fire_head:
                    self.set_head(True)
                    if self.full_buffer_after_head:
                        self.buffer = 1
                    owner, self.dispatch = self.dispatch, object()
                    try:
                        self.printer.send_event("box:extrude_process_stage7")
                        self.printer.send_event("box:extrude_process_stage7")
                    finally:
                        self.dispatch = owner
                if data[1] == 7:
                    self.buffer = 2
                if data[1] == 0:
                    return reply(address, command, data=bytes((self.native.mode,)))
                if data[1] == 5:
                    return reply(address, command, data=struct.pack(">f", -42.0))
            if command == 0x11:
                if self.clear_head_on_retract:
                    self.set_head(False)
                if data[1] == 1:
                    self.connections, self.buffer = 0, 2
            return raw

        def script(value):
            self.events.append(("gcode", value))
            if self.printer.buttons:
                callback = self.printer.buttons[0][1]
                cfg = self.box.boxcfg
                if (self.fire_cut and value == "G0 Y%.2f F%.2f\n M400" %
                        (cfg.get_real_cut_pos_y(), cfg.cut_velocity)):
                    callback(self.reactor.monotonic(), True)
                if (self.release_cut and value == "G90\n G0 Y%.2f F20000.00" %
                        cfg.retrude_position_y):
                    callback(self.reactor.monotonic(), False)
            for line in value.splitlines():
                fields = line.split()
                if fields and fields[0] in ("M104", "M109"):
                    for field in fields[1:]:
                        if field.startswith("S"):
                            self.printer.heater.target = float(field[1:])

        self.native.cmd_send_data_with_response = exchange
        self.gcode.on_script = script
        self.native.calls.clear()
        self.gcode.scripts.clear()
        self.reactor.pauses.clear()
        return self.box.material_trial

    def set_head(self, present):
        self.printer.objects["filament_switch_sensor head"].runout_helper.filament_present = present

    def run_trial(self, action=None, target=None):
        return self.box.material_trial.run(action or self.action, target or self.target,
                                           confirmed=True)

    def stages(self):
        return [data[1] for _, command, data in self.native.commands() if command == 0x10]

    def assert_released(self):
        self.assertFalse(self.box.material_trial.active)
        self.assertFalse(self.box.operations.active)
        self.assertFalse(self.box.filament_sensor_enable)
        self.assertTrue(self.lifecycle.passive)
        self.assertTrue(self.box.heart_process_enable[self.address - 1])
        if self.box.native is not None and self.lifecycle.running:
            with self.assertRaises(RuntimeError):
                self.box.host_guard()
            before = len(self.native.calls)
            with self.assertRaises(UnsupportedRequest):
                self.box.send_native(build_request(self.address, "EXTRUDE_PROCESS",
                                                  bytes((self.mask, 0, 0))))
            self.assertEqual(len(self.native.calls), before)

    def test_passive_registration_and_confirmation_do_not_enable_ordinary_commands(self):
        trial = self.make_box()
        self.assertIn("CFS_TEST_LOAD", self.gcode.commands)
        self.assertIn("CFS_TEST_UNLOAD", self.gcode.commands)
        for name in ("CFS_TEST_LOAD", "CFS_TEST_UNLOAD"):
            for parameters in ({"TNN": "T1A"}, {"TNN": "T1A", "CONFIRM": "0"},
                               {"TNN": "T1A", "CONFIRM": "2"}, {"CONFIRM": "1"}):
                with self.subTest(name=name, parameters=parameters), self.assertRaises(CommandError):
                    self.gcode.commands[name](HostCommand(parameters))
        for action, target, confirmed in (("LOAD", "T1A", False), ("LOAD", "T1A", 1),
                                          ("LOAD", "T0", True), ("LOAD", "T5A", True),
                                          ("LOAD", "T1A\nT0", True), ("PRINT", "T1A", True)):
            with self.subTest(action=action, target=target), self.assertRaises((RuntimeError, ValueError)):
                trial.run(action, target, confirmed=confirmed)
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])
        self.assert_released()

    def test_complete_load_uses_real_head_phase_and_physical_target_without_unloading(self):
        self.make_box(target="T2C")
        self.box.box_state.Tnn_map[self.target] = "T1A"
        before = deepcopy(self.box.store.base_data)
        result = self.run_trial()
        self.assertEqual(result["target"], "T2C")
        self.assertEqual(self.stages(), [0, 4, 6, 5])
        self.assertEqual(self.box.last_cmd, "T2C")
        self.assertEqual(self.box.box_state.get_Tn_data(2, "filament"), "T2C")
        self.assertTrue(self.lifecycle.head_present())
        self.assertEqual(self.connections, 4)
        self.assertEqual(self.native.mode, 2)
        self.assertEqual(self.gcode.scripts.count("M83\n G0 E5 F60"), 1)
        self.assertFalse(any(command in (0x0D, 0x11) for _, command, _ in self.native.commands()))
        self.assertTrue(all(address == 2 for address, _, _ in self.native.commands()))
        self.assertTrue(all(data[0] == 4 for _, command, data in self.native.commands() if command == 0x10))
        self.assertEqual(self.printer.heater.target, 0)
        for name in ("T1", "T3", "T4"):
            self.assertEqual(self.box.store.base_data[name], before[name])
        self.assert_released()

    def test_complete_unload_cuts_before_retraction_and_uses_only_the_known_source(self):
        self.make_box("UNLOAD")
        result = self.run_trial()
        self.assertEqual(result["target"], "T1A")
        self.assertEqual([data for _, command, data in self.native.commands() if command == 0x11],
                         [b"\x01\x00", b"\x01\x01"])
        cut = next(index for index, event in enumerate(self.events)
                   if event[0] == "gcode" and event[1].startswith("SET_VELOCITY_LIMIT ACCEL=5000"))
        retract = next(index for index, event in enumerate(self.events)
                       if event[0:2] == ("native", 0x11))
        self.assertLess(cut, retract)
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "None")
        self.assertFalse(self.lifecycle.head_present())
        self.assertEqual((self.connections, self.native.mode, self.printer.heater.target), (0, 0, 0))
        self.assertFalse(self.box.cut_sensor.cut_present)
        self.assertIn("G90\n G0 Y100.00 F20000.00", self.gcode.scripts)
        self.assertIn("G90\n G0 X42.00 F20000.00", self.gcode.scripts)
        self.assertFalse(any(command in (0x0D, 0x10) for _, command, _ in self.native.commands()))
        self.assert_released()

    def test_full_buffer_final_feed_keeps_the_integral_configured_amount(self):
        self.make_box(extrude_material_len_for_box="3")
        self.full_buffer_after_head = True
        self.run_trial()
        self.assertEqual(self.stages(), [0, 4, 6, 5, 7])
        self.assertIn((1, 0x10, b"\x01\x07\x03"), self.native.commands())
        self.assertEqual(self.box.boxcfg.extrude_material_len_for_box, 3.0)
        self.assertIs(type(self.box.boxcfg.extrude_material_len_for_box), float)
        self.assert_released()

    def test_unsafe_host_states_fail_before_requests_or_physical_gcode(self):
        changes = {
            "printing": lambda: setattr(self.box.print_stats, "state", "printing"),
            "paused": lambda: setattr(self.printer.objects["pause_resume"], "is_paused", True),
            "active_sd": lambda: setattr(self.box.virtual_sdcard, "is_active", lambda: True),
            "resume": lambda: setattr(self.box.virtual_sdcard, "do_resume_status", True),
            "error": lambda: setattr(self.box, "error", "filament_err"),
            "inside_error": lambda: setattr(self.box, "inside_error", True),
            "head_present": lambda: self.set_head(True),
            "head_unavailable": lambda: self.set_head(None),
            "known_source": lambda: setattr(self.box, "last_cmd", "T1A"),
            "not_homed": lambda: setattr(self, "homed_axes", "xy"),
            "disconnected": lambda: setattr(self.native, "is_connected", False),
            "missing_port": lambda: setattr(self, "port_present", False),
            "other_online": lambda: setattr(self.table[1], "online", 1),
            "encoding": lambda: setattr(self.box.boxcfg, "version", 0),
            "firmware": lambda: self.box._accept_identity(1, b"152" + self.native.serial, b"changed"),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.make_box()
                change()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assertFalse(self.box.material_trial.active)

    def test_unload_never_guesses_a_missing_or_conflicting_source(self):
        for source, filament, head in ((None, "T1A", True), ("T1B", "T1A", True),
                                       ("T1A", "None", True), ("T1A", "T1B", True),
                                       ("T1A", "T1A", False)):
            with self.subTest(source=source, filament=filament, head=head):
                self.make_box("UNLOAD")
                self.box.last_cmd = source
                self.box.box_state.modify_Tn_data(1, filament, "filament")
                self.set_head(head)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])

    def test_missing_observations_fail_closed_before_requests(self):
        changes = {
            "pause": lambda: self.printer.objects.pop("pause_resume"),
            "pause_flag": lambda: delattr(self.printer.objects["pause_resume"], "is_paused"),
            "virtual_sd": lambda: delattr(self.box.virtual_sdcard, "is_active"),
            "resume": lambda: delattr(self.box.virtual_sdcard, "do_resume_status"),
            "head_helper": lambda: delattr(self.lifecycle.head_sensor, "runout_helper"),
            "head": lambda: delattr(self.lifecycle.head_sensor.runout_helper, "filament_present"),
            "heater_limit": lambda: delattr(self.printer.heater, "max_temp"),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.make_box()
                change()
                with self.assertRaises(CommandError):
                    self.gcode.commands["CFS_TEST_LOAD"](HostCommand({"TNN": "T1A", "CONFIRM": "1"}))
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assertFalse(self.box.material_trial.active)

    def test_invalid_geometry_amounts_and_temperature_fail_before_effects(self):
        values = (("safe_pos_y", float("nan")), ("extrude_pos_x", 9999),
                  ("extrude_material_len_for_box", 3.5), ("extrude_material_times", 0),
                  ("extrude_material_velocity", 0), ("Tn_extrude_velocity", (0,)),
                  ("Tn_extrude_temp", float("inf")))
        for name, value in values:
            with self.subTest(name=name, value=value):
                self.make_box()
                setattr(self.box.boxcfg, name, value)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
        self.make_box("UNLOAD")
        self.box.boxcfg.cut_run_count = 0
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])

    def test_fresh_preflight_masks_modes_and_buffer_gate_all_writes(self):
        for action, mode, material, connections, buffer in (
                ("LOAD", 1, 1, 0, 2), ("LOAD", 0, 0, 0, 2),
                ("LOAD", 0, 1, 2, 2), ("LOAD", 0, 1, 0, 1),
                ("UNLOAD", 1, 1, 1, 2), ("UNLOAD", 2, 1, 0, 2),
                ("UNLOAD", 2, 1, 3, 2)):
            with self.subTest(action=action, mode=mode, material=material,
                              connections=connections, buffer=buffer):
                self.make_box(action)
                self.native.mode, self.material = mode, material
                self.connections, self.buffer = connections, buffer
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertFalse(any(command not in (5, 8, 10) for _, command, _ in self.native.commands()))
                self.assertEqual(self.gcode.scripts, [])
                self.assert_released()

    def test_bad_normal_or_event_reply_is_rejected_before_decode_or_mechanics(self):
        outcomes = (None, reply(1, 0x08, status=0x30, data=b"\x01\x02\x03\x04"),
                    reply(1, 0x08, data=b"\x01\x02"), reply(1, 0x08, data=b"\x80"))
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.make_box()
                before = deepcopy(self.box.box_state.get_Tn_inner_data(1, "filament_sensor", subpart="material"))
                self.outcomes[(0x08, b"\x00")] = outcome
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.box.box_state.get_Tn_inner_data(1, "filament_sensor", subpart="material"), before)
                self.assertEqual(self.gcode.scripts, [])
                self.assertEqual(self.box.slot_events, {})
                self.assert_released()

    def test_first_feed_failure_stops_without_retry_cut_or_retraction(self):
        self.make_box()
        self.outcomes[(0x10, b"\x01\x00\x00")] = reply(1, 0x10, status=0x03)
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual(self.stages(), [0])
        self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
        self.assertFalse(any(script.startswith("SET_VELOCITY_LIMIT") for script in self.gcode.scripts))
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_stage_zero_mode_and_stage_five_wheel_reply_shapes_are_required(self):
        for stage, malformed_values in ((0, (b"", b"\x00\x00", b"\x01", b"\x03", b"\xff")),
                                        (5, (b"", b"\x00\x00\x00", b"\x00" * 5))):
            for payload in malformed_values:
                with self.subTest(stage=stage, payload=payload):
                    self.make_box()
                    self.outcomes[(0x10, bytes((1, stage, 0)))] = reply(1, 0x10, data=payload)
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.run_trial()
                    self.assertEqual(self.stages(), [0] if stage == 0 else [0, 4, 6, 5])
                    self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
                    self.assertNotIn("M104 S0", self.gcode.scripts)
                    self.assertIsNone(self.box.last_cmd)
                    self.assert_released()

    def test_phase_false_results_do_not_enter_connection_or_gear_recovery(self):
        for phase in ("connection", "gear"):
            with self.subTest(phase=phase):
                self.make_box()
                if phase == "connection":
                    original = self.box.communication_extrude_process

                    def fail_first_feed(address, slot, **kwargs):
                        if kwargs.get("stage") == 0:
                            return False
                        return original(address, slot, **kwargs)

                    self.box.communication_extrude_process = fail_first_feed
                else:
                    self.box.loading.box_extrude_material_stage8 = lambda target: False
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
                self.assertFalse(any(script.startswith("SET_VELOCITY_LIMIT") for script in self.gcode.scripts))
                self.assert_released()

    def test_first_missed_cut_switch_stops_before_repositioning_or_retraction(self):
        self.make_box("UNLOAD", switch_pin="synthetic_pin")
        self.fire_cut = False
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        strokes = [script for script in self.gcode.scripts
                   if script == "G0 Y225.00 F6000.00\n M400"]
        self.assertEqual(len(strokes), 1)
        self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assert_released()

    def test_missing_inactive_or_stuck_cut_switch_rejects_unload_before_effects(self):
        changes = {
            "unconfigured": lambda: setattr(self.box.cut_sensor, "switch_pin", None),
            "inactive": lambda: setattr(self.box.cut_sensor, "active", False),
            "stuck": lambda: self.printer.buttons[0][1](self.reactor.monotonic(), True),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.make_box("UNLOAD")
                change()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assertEqual(self.box.last_cmd, "T1A")
                self.assert_released()

    def test_unreleased_cut_switch_after_return_cannot_claim_success(self):
        self.make_box("UNLOAD")
        self.release_cut = False
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertTrue(self.box.cut_sensor.cut_present)
        self.assertIsNone(self.box.last_cmd)
        self.assertFalse(self.lifecycle.head_present())
        self.assertIn("G90\n G0 Y100.00 F20000.00", self.gcode.scripts)
        self.assertNotIn("M104 S0", self.gcode.scripts)
        self.assert_released()

    def test_head_arrival_during_preflight_prevents_heating_and_native_writes(self):
        self.make_box()

        def arrive_during_buffer_read(address, command, data):
            if command == 0x05:
                self.set_head(True)

        self.on_native = arrive_during_buffer_read
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertFalse(any(command not in (5, 8, 10) for _, command, _ in self.native.commands()))
        self.assertEqual(self.gcode.scripts, [])
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_head_arrival_during_heating_prevents_first_feed(self):
        self.make_box()
        original = self.gcode.on_script

        def arrive_during_heating(script):
            original(script)
            if script.startswith("M104 S"):
                self.set_head(True)

        self.gcode.on_script = arrive_during_heating
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual(self.stages(), [])
        self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_temperature_below_retained_extrusion_limit_rejects_before_effects(self):
        self.make_box()
        self.box.boxcfg.Tn_extrude_temp = 100
        self.box.get_material_target_temp = lambda target: 100
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])
        self.assert_released()

    def test_head_loss_during_final_feed_revokes_load_without_recovery(self):
        self.make_box()
        original = self.gcode.on_script

        def lose_head_during_stage8(script):
            original(script)
            if script.startswith("G91\n G0 E18.00"):
                self.set_head(False)

        self.gcode.on_script = lose_head_during_stage8
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual(self.stages(), [0, 4, 6, 5])
        self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
        self.assertFalse(any(script.startswith("SET_VELOCITY_LIMIT") for script in self.gcode.scripts))
        self.assertNotIn("M104 S0", self.gcode.scripts)
        self.assert_released()

    def test_head_callback_failure_revokes_main_phase_without_followup_motion(self):
        self.make_box()
        self.outcomes[(0x10, b"\x01\x06\x00")] = reply(1, 0x10, status=0x0D)
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual(self.stages(), [0, 4, 6])
        self.assertEqual(self.gcode.scripts.count("M83\n G0 E5 F60"), 1)
        self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_native_crc_error_is_not_resent_and_does_not_start_recovery(self):
        for status in (2, 4):
            with self.subTest(status=status):
                self.make_box()
                self.outcomes[(0x10, b"\x01\x00\x00")] = reply(1, 0x10, status=status)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.stages(), [0])
                self.assertTrue(all(arguments == (False,) for _, _, arguments in self.native.calls))
                self.assertTrue(all(0 < timeout <= 150 for _, timeout, _ in self.native.calls))
                self.assert_released()

    def test_remaining_cooperative_budget_bounds_later_native_waits(self):
        self.make_box()

        def consume_budget(address, command, data):
            if command == 0x10 and data[1] == 0:
                self.reactor.time += 895

        self.on_native = consume_budget
        self.run_trial()
        stage4 = next(timeout for body, timeout, _ in self.native.calls
                      if body[3:] == b"\x10\x01\x04\x00")
        self.assertEqual(stage4, 5)
        final_enable = [timeout for body, timeout, _ in self.native.calls
                        if body[3:] == b"\x0f\x00"][-1]
        self.assertLessEqual(final_enable, 1)
        self.assert_released()

    def test_ordinary_commands_other_dispatch_and_raw_requests_cannot_inherit_scope(self):
        self.make_box()
        observed = []

        def reenter(address, command, data):
            if command != 0x10 or data[1] != 0:
                return
            observed.append(True)
            self.assertTrue(self.lifecycle.passive)
            for name in ("BOX_EXTRUDE_MATERIAL", "BOX_RETRUDE_MATERIAL", "BOX_CUT_MATERIAL", "T0"):
                with self.subTest(command=name), self.assertRaises(CommandError):
                    self.gcode.commands[name](HostCommand({"TNN": "T1A"}))
            with self.assertRaises(RuntimeError):
                self.run_trial("UNLOAD")
            with self.assertRaises(RuntimeError):
                self.box.preload_trial.run("T1A", confirmed=True)
            for request in (build_request(2, "EXTRUDE_PROCESS", b"\x01\x00\x00"),
                            build_request(1, "SET_PRE_LOADING", b"\x01\x02"),
                            diagnostic_request(1, 255, 16, b"\x01\x00\x00")):
                with self.assertRaises((RuntimeError, ValueError)):
                    self.box.send_native(request)
            owner, self.dispatch = self.dispatch, object()
            try:
                with self.assertRaises(RuntimeError):
                    self.box.host_guard()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.box.send_native(build_request(1, "EXTRUDE_PROCESS", b"\x01\x00\x00"))
            finally:
                self.dispatch = owner

        self.on_native = reenter
        self.run_trial()
        self.assertEqual(observed, [True])
        self.assertEqual(self.stages(), [0, 4, 6, 5])
        self.assert_released()

    def test_context_changes_revoke_permission_before_any_followup_effect(self):
        changes = {
            "shutdown": lambda: self.printer.send_event("klippy:shutdown"),
            "disconnect": lambda: self.lifecycle.disconnect(1),
            "replacement": lambda: self.box._accept_identity(1, b"113" + b"B" * 19, b"replacement"),
            "error": lambda: setattr(self.box, "error", "filament_err"),
            "printing": lambda: setattr(self.box.print_stats, "state", "printing"),
            "deadline": lambda: setattr(self.reactor, "time", self.reactor.time + 900),
            "cancel": lambda: self.box.material_trial.cancel(),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.make_box()

                def interrupt(address, command, data):
                    if command == 0x10 and data[1] == 0:
                        change()

                self.on_native = interrupt
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.stages(), [0])
                self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
                self.assertIsNone(self.box.last_cmd)
                self.assert_released()

    def test_cancelled_native_call_keeps_admission_occupied_until_it_unwinds(self):
        self.make_box()

        def interrupt(address, command, data):
            if command == 0x10 and data[1] == 0:
                self.box.material_trial.cancel()
                self.assertTrue(self.box.material_trial.active)
                with self.assertRaises(RuntimeError):
                    self.run_trial()

        self.on_native = interrupt
        with self.assertRaises(RuntimeError):
            self.run_trial()
        self.assertEqual(self.stages(), [0])
        self.assert_released()

    def test_transport_exception_preserves_observed_source_without_cleanup(self):
        self.make_box("UNLOAD")
        self.outcomes[(0x11, b"\x01\x00")] = OSError("synthetic transport failure")
        with self.assertRaises(OSError):
            self.run_trial()
        retracts = [data for _, command, data in self.native.commands() if command == 0x11]
        self.assertEqual(retracts, [b"\x01\x00"])
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "T1A")
        self.assertNotIn("M104 S0", self.gcode.scripts)
        self.assert_released()

    def test_present_head_after_retract_is_failure_without_an_automatic_retry(self):
        self.make_box("UNLOAD")
        self.clear_head_on_retract = False
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual([data for _, command, data in self.native.commands() if command == 0x11],
                         [b"\x01\x00", b"\x01\x01"])
        # The existing normal-tail bookkeeping runs before the final head check.
        # Retaining that observed state is not a claim that material was cleared.
        self.assertIsNone(self.box.last_cmd)
        self.assertTrue(self.lifecycle.head_present())
        self.assertNotIn("M104 S0", self.gcode.scripts)
        self.assert_released()

    def test_failed_final_load_observation_does_not_rewind_live_source_or_auto_unload(self):
        self.make_box()

        def lose_connection(address, command, data):
            if command == 0x04 and data == b"\x01\x00":
                self.connections = 0

        self.on_native = lose_connection
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertTrue(self.lifecycle.head_present())
        self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
        self.assertNotIn("M104 S0", self.gcode.scripts)
        self.assert_released()

    def test_unsolicited_head_events_do_not_move_a_passive_box(self):
        self.make_box()
        self.printer.send_event("box:extrude_process_stage7")
        self.set_head(True)
        self.printer.send_event("box:extrude_process_stage7")
        self.assertEqual(self.gcode.scripts, [])
        self.assertEqual(self.native.calls, [])
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
