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


class PoseReplay:
    """Replay command-space XYZE/modes and M400, without physical mechanics."""

    def __init__(self, position):
        self.position = position
        self.completed = tuple(position[:3])
        self.absolute = True
        self.absolute_extrude = True
        self.completed_e = position[3]
        self.moves = []

    def run(self, script):
        completed = []
        for line in script.splitlines():
            words = line.split()
            if not words:
                continue
            command = words[0]
            if command in ("G90", "G91"):
                self.absolute = command == "G90"
            elif command in ("M82", "M83"):
                self.absolute_extrude = command == "M82"
            elif command in ("G0", "G1"):
                before = tuple(self.position[:3])
                for word in words[1:]:
                    if word[0] in "XYZ":
                        index = "XYZ".index(word[0])
                        value = float(word[1:])
                        self.position[index] = value if self.absolute else self.position[index] + value
                    elif word[0] == "E":
                        value = float(word[1:])
                        self.position[3] = (value if self.absolute and self.absolute_extrude
                                            else self.position[3] + value)
                after = tuple(self.position[:3])
                if before != after:
                    self.moves.append({"line": line.strip(), "before": before, "after": after,
                                       "completed_before": self.completed})
            elif command == "M400":
                self.completed = tuple(self.position[:3])
                self.completed_e = self.position[3]
                completed.append(self.completed)
        return completed


class MaterialTrialTests(unittest.TestCase):
    def make_box(self, action="LOAD", target="T1A", unknown_source=False, **settings):
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
        load = ("RESPOND MSG=load\nM104\nIF_NEED_HOME\nBOX_ERROR_CLEAR\nBOX_CHECK_MATERIAL\n"
                "BOX_CUT_MATERIAL\nBOX_RETRUDE_MATERIAL\nBOX_GO_TO_EXTRUDE_POS\n"
                "BOX_NOZZLE_CLEAN\nBOX_EXTRUDE_MATERIAL\nBOX_EXTRUDER_EXTRUDE\n"
                "BOX_MATERIAL_FLUSH\nRESTORE_POSITION")
        unload = ("RESPOND MSG=unload\nBOX_ERROR_CLEAR\nBOX_CHECK_MATERIAL\n"
                  "BOX_CUT_MATERIAL\nBOX_RETRUDE_MATERIAL\nBOX_GO_TO_BOX_EXTRUDE_POS")
        self.box.boxcfg.config.sections.update({
            "gcode_macro BOX_LOAD_MATERIAL_WITH_MATERIAL": {"gcode": load},
            "gcode_macro BOX_QUIT_MATERIAL": {"gcode": unload}})
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
        self.factors = {"speed_factor": 1., "extrude_factor": 1.}
        self.printer.objects["gcode_move"].get_status = lambda now: dict(self.factors)
        self.table = [SimpleNamespace(online=int(index + 1 == self.address)) for index in range(4)]
        self.lifecycle = self.box.register_lifecycle(
            self.printer, passive=True,
            families=dict.fromkeys(range(1, 5), "cfs0"), address_table=self.table,
            port_exists=lambda path: self.port_present,
            current_dispatch=lambda: self.dispatch)
        self.printer.send_event("klippy:ready")
        self.lifecycle.get_connect_state()
        self.gcode.register_command("RESPOND", lambda command: None)
        self.gcode.register_command("IF_NEED_HOME", lambda command: None)
        self.gcode.register_command("M104", lambda command: setattr(
            self.printer.heater, "target", float(command.parameters.get("S", 0))))
        self.gcode.register_command("BOX_CHECK_MATERIAL", lambda command:
            self.gcode.run_script_from_command("WAIT_EXTRUSION_ALL_MATERIALS"))
        self.material = self.mask
        self.connections = 0 if action == "LOAD" else self.mask
        self.buffer = 2
        self.fire_head = True
        self.fire_cut = self.release_cut = True
        self.full_buffer_after_head = False
        self.clear_head_on_retract = True
        self.events, self.on_native = [], None
        self.pose = PoseReplay(self.printer.position)
        self.pose.submitted_e = self.printer.position[3]

        def submit_steps():
            self.events.append(("flush", tuple(self.printer.position), self.pose.completed_e))
            self.pose.submitted_e = self.printer.position[3]

        self.printer.objects["toolhead"].flush_step_generation = submit_steps
        self.outcomes = {}
        self.set_head(action == "UNLOAD")
        if action == "UNLOAD" and not unknown_source:
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
                if self.clear_head_on_retract and data[1] == 1:
                    self.set_head(False)
                if data[1] == 1:
                    self.connections, self.buffer = 0, 2
            return raw

        def script(value):
            self.events.append(("gcode", value))
            completed = self.pose.run(value)
            if self.printer.buttons:
                callback = self.printer.buttons[0][1]
                cfg = self.box.boxcfg
                for x, y, _ in completed:
                    if self.fire_cut and x == cfg.pre_cut_pos_x and y == cfg.get_real_cut_pos_y():
                        callback(self.reactor.monotonic(), True)
                    elif self.release_cut and y <= cfg.pre_cut_pos_y:
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

    def make_unknown_unload(self):
        self.make_box("UNLOAD", target="T1C", unknown_source=True)
        self.material, self.connections, self.buffer, self.native.mode = 15, 4, 0, 0
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "None")

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

    def assert_original_first_x(self, start, entry_y):
        crossing = next(move for move in self.pose.moves[start:]
                        if move["before"][0] != move["after"][0])
        self.assertEqual(crossing["before"][1], entry_y)
        self.assertEqual(crossing["after"][1], entry_y)

    def test_load_then_unload_uses_the_completed_clean_route_before_original_x_first_cut(self):
        self.make_box(target="T1C", switch_pin="synthetic_pin")
        same_box = self.box
        self.run_trial("LOAD")
        cfg = self.box.boxcfg
        self.assertEqual(self.printer.position[:2], [cfg.clean_right_pos_x, cfg.safe_pos_y])
        self.assertEqual(self.pose.completed[:2], (cfg.clean_right_pos_x, cfg.safe_pos_y))
        self.assertEqual(self.printer.position[3], 5 + 18 + cfg.Tn_extrude - 5)
        self.assertGreater(self.printer.position[1], cfg.pre_cut_pos_y)
        self.assertTrue(self.lifecycle.head_present())
        start = len(self.pose.moves)
        result = self.run_trial("UNLOAD")
        self.assertIs(self.box, same_box)
        self.assertEqual(result["target"], "T1C")
        self.assert_original_first_x(start, cfg.safe_pos_y)
        self.assertEqual(self.printer.position[:2], [cfg.retrude_position_x, cfg.retrude_position_y])
        self.assertFalse(self.box.cut_sensor.cut_present)
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_neutral_buffer_load_completes_the_full_recipe_then_known_source_unload(self):
        self.make_box(target="T1C", switch_pin="synthetic_pin")
        same_box = self.box
        self.buffer = 0
        loaded = self.run_trial("LOAD")
        self.assertEqual(loaded, {"action": "LOAD", "target": "T1C", "mode": 2,
                                  "material": 4, "connections": 4, "buffer": 0})
        self.assertEqual(self.stages(), [0, 4, 6, 5])
        self.assertEqual(self.box.last_cmd, "T1C")
        self.assertTrue(self.lifecycle.head_present())
        cfg = self.box.boxcfg
        self.assertEqual(self.pose.completed[:2], (cfg.clean_right_pos_x, cfg.safe_pos_y))
        self.assertEqual(self.printer.position[3], 5 + 18 + cfg.Tn_extrude - 5)
        self.assert_released()
        start = len(self.pose.moves)
        unloaded = self.run_trial("UNLOAD")
        self.assertIs(self.box, same_box)
        self.assertEqual(unloaded, {"action": "UNLOAD", "target": "T1C", "mode": 0,
                                    "material": 4, "connections": 0, "buffer": 2})
        self.assert_original_first_x(start, cfg.safe_pos_y)
        self.assertEqual(self.printer.position[:2], [cfg.retrude_position_x, cfg.retrude_position_y])
        self.assertFalse(self.lifecycle.head_present())
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_full_buffer_load_refuses_before_host_motion_heat_or_native_writes(self):
        self.make_box(target="T1C")
        self.buffer = 1
        before = tuple(self.printer.position), self.printer.heater.target
        with self.assertRaisesRegex(RuntimeError, "fresh material-trial preconditions"):
            self.run_trial("LOAD")
        self.assertEqual([(command, data) for _, command, data in self.native.commands()],
                         [(0x0A, b""), (0x08, b"\x00"), (0x08, b"\x01"), (0x05, b"")])
        self.assertEqual(self.gcode.scripts, [])
        self.assertEqual((tuple(self.printer.position), self.printer.heater.target), before)
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_standalone_cutter_rejects_the_old_rear_entry_with_and_without_a_tray(self):
        for tray in (0, 1):
            with self.subTest(has_extrude_pos=tray):
                self.make_box(has_extrude_pos=tray)
                self.lifecycle.passive = False
                self.set_head(True)
                self.printer.position[:3] = [127, 368, 40]
                self.pose.completed = tuple(self.printer.position[:3])
                with self.assertRaisesRegex(RuntimeError, "cut entry"):
                    self.box.cutter.move_to_cut()
                self.assertEqual(self.gcode.scripts, [])

    def test_original_cutter_uses_absolute_x_from_a_valid_relative_entry(self):
        self.make_box("UNLOAD")
        self.lifecycle.passive = False
        self.printer.position[:3] = [127, self.box.boxcfg.safe_pos_y, 40]
        self.pose.completed = tuple(self.printer.position[:3])
        self.gcode.run_script_from_command("G91")
        self.assertIs(self.box.cutter.move_to_cut(), True)
        self.assert_original_first_x(0, self.box.boxcfg.safe_pos_y)
        self.assertEqual(self.printer.position[1], self.box.boxcfg.get_real_cut_pos_y())

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
        self.assertEqual(self.printer.heater.target, self.box.boxcfg.Tn_extrude_temp)
        self.assertNotIn("M104 S0", self.gcode.scripts)
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

    def test_unknown_source_unload_uses_shared_buffer_then_selected_material_without_source_assignment(self):
        self.make_unknown_unload()
        before = deepcopy(self.box.store.base_data)
        observations, updates = [], []
        self.box.update_filament_pos = lambda *args: updates.append(args)

        def observe(address, command, data):
            observations.append((self.box.last_cmd, self.box.last_cmd_tmp,
                                 self.box.box_state.get_Tn_data(1, "filament")))

        self.on_native = observe
        result = self.run_trial()
        self.assertEqual(result["target"], "T1C")
        self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 0x11],
                         [b"\x00\x00", b"\x04\x01"])
        self.assertTrue(observations)
        self.assertTrue(all(row == (None, None, "None") for row in observations))
        self.assertEqual(updates, [])
        self.assertEqual(self.box.store.base_data, before)
        self.assertIsNone(self.box.last_cmd)
        self.assertIsNone(self.box.last_cmd_tmp)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "None")
        self.assertEqual((self.connections, self.buffer, self.native.mode), (0, 2, 0))
        self.assertFalse(self.lifecycle.head_present())
        self.assertEqual(self.printer.position[:2],
                         [self.box.boxcfg.retrude_position_x, self.box.boxcfg.retrude_position_y])
        self.assertFalse(any(opcode in (0x0D, 0x10) for _, opcode, _ in self.native.commands()))
        self.assert_released()

    def test_unknown_source_unload_rejects_ambiguous_or_wrong_fresh_lane_before_effects(self):
        for material, connections, buffer in ((15, 0, 0), (15, 1, 0), (15, 5, 0),
                                               (3, 4, 0), (15, 4, 3)):
            with self.subTest(material=material, connections=connections, buffer=buffer):
                self.make_unknown_unload()
                self.material, self.connections, self.buffer = material, connections, buffer
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertTrue(all(opcode in (5, 8, 10) for _, opcode, _ in self.native.commands()))
                self.assertEqual(self.gcode.scripts, [])
                self.assertIsNone(self.box.last_cmd)
                self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "None")
                self.assert_released()

    def test_unknown_source_requires_homed_single_idle_cfs_and_present_head(self):
        changes = {
            "unhomed": lambda: setattr(self, "homed_axes", "xy"),
            "another_cfs": lambda: setattr(self.table[1], "online", 1),
            "printing": lambda: setattr(self.box.print_stats, "state", "printing"),
            "absent_head": lambda: self.set_head(False),
            "unknown_head": lambda: self.set_head(None),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.make_unknown_unload()
                change()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assert_released()

    def test_unknown_source_assist_configuration_is_checked_before_any_effects(self):
        velocities = ((), (0,), (-1,), (float("nan"),), (float("inf"),), ("bad",), 3600, None)
        for velocity in velocities:
            with self.subTest(velocity=velocity):
                self.make_unknown_unload()
                self.box.boxcfg.Tn_retrude_velocity = velocity
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assertIsNone(self.box.last_cmd)
                self.assert_released()
        self.make_unknown_unload()
        self.box.boxcfg.Tn_extrude_temp = 180
        self.printer.heater.min_extrude_temp = 210
        self.box.get_material_target_temp = lambda target: 220
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_unknown_source_revalidates_lane_and_presence_before_shared_buffer(self):
        for bank, changed in ((b"\x01", 1), (b"\x01", 5), (b"\x00", 3)):
            with self.subTest(bank=bank, changed=changed):
                self.make_unknown_unload()
                reads = []

                def change_after_preflight(address, command, data):
                    if command == 0x08 and data == bank:
                        reads.append(True)
                        if len(reads) == 2:
                            if bank == b"\x01":
                                self.connections = changed
                            else:
                                self.material = changed

                self.on_native = change_after_preflight
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(len(reads), 2)
                self.assertFalse(any(opcode in (0x04, 0x11) for _, opcode, _ in self.native.commands()))
                self.assertNotIn("G1 E-30 F2400", self.gcode.scripts)
                self.assertIsNone(self.box.last_cmd)
                self.assert_released()

    def test_unknown_source_hint_changes_cancel_before_shared_buffer(self):
        for hint in ("last_cmd", "filament"):
            with self.subTest(hint=hint):
                self.make_unknown_unload()
                reads = []

                def inject_source(address, command, data):
                    if command == 0x08 and data == b"\x01":
                        reads.append(True)
                        if len(reads) == 2:
                            if hint == "last_cmd":
                                self.box.last_cmd = "T1C"
                            else:
                                self.box.box_state.modify_Tn_data(1, "T1C", "filament")

                self.on_native = inject_source
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertFalse(any(opcode in (0x04, 0x11) for _, opcode, _ in self.native.commands()))
                # The injected observation is retained; no cleanup invents a
                # replacement source or rewinds another callback's mutation.
                self.assertEqual(self.box.last_cmd if hint == "last_cmd" else
                    self.box.box_state.get_Tn_data(1, "filament"), "T1C")
                self.assert_released()

    def test_unknown_source_head_loss_before_buffer_stops_but_buffer_may_clear_head(self):
        for during_buffer in (False, True):
            with self.subTest(during_buffer=during_buffer):
                self.make_unknown_unload()
                reads = []

                def lose_head(address, command, data):
                    if during_buffer:
                        if command == 0x11 and data == b"\x00\x00":
                            self.set_head(False)
                    elif command == 0x08 and data == b"\x01":
                        reads.append(True)
                        if len(reads) == 2:
                            self.set_head(False)

                self.on_native = lose_head
                if during_buffer:
                    self.run_trial()
                    self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 0x11],
                                     [b"\x00\x00", b"\x04\x01"])
                else:
                    with self.assertRaises((RuntimeError, ValueError)):
                        self.run_trial()
                    self.assertFalse(any(opcode in (0x04, 0x11) for _, opcode, _ in self.native.commands()))
                self.assertIsNone(self.box.last_cmd)
                self.assert_released()

    def test_unknown_source_native_grants_reject_wrong_phase_reentry_and_repeats(self):
        self.make_unknown_unload()
        probes = []

        def denied(request):
            before = len(self.native.calls)
            with self.assertRaises((RuntimeError, ValueError)):
                self.box.send_native(request)
            self.assertEqual(len(self.native.calls), before)

        shared = build_request(1, "RETRUDE_PROCESS", b"\x00\x00")
        selected = build_request(1, "RETRUDE_PROCESS", b"\x04\x01")
        original_script = self.gcode.on_script

        def probe_before_retraction(script):
            original_script(script)
            if not probes:
                probes.append("cut phase")
                denied(shared)
                denied(selected)

        self.gcode.on_script = probe_before_retraction

        def probe_inflight(address, command, data):
            if command == 0x11 and data == b"\x00\x00":
                probes.append("shared buffer in flight")
                denied(shared)
                denied(selected)
                denied(build_request(2, "RETRUDE_PROCESS", b"\x00\x00"))
                owner, self.dispatch = self.dispatch, object()
                try:
                    denied(shared)
                finally:
                    self.dispatch = owner

        self.on_native = probe_inflight
        original_retract = self.box.communication_retrude_process

        def probe_completed(address, slot, trigger, **kwargs):
            result = original_retract(address, slot, trigger, **kwargs)
            if slot == "0":
                probes.append("shared buffer complete")
                denied(shared)
                denied(build_request(1, "RETRUDE_PROCESS", b"\x04\x00"))
            else:
                probes.append("selected material complete")
                denied(shared)
                denied(selected)
            return result

        self.box.communication_retrude_process = probe_completed
        self.run_trial()
        self.assertEqual(probes, ["cut phase", "shared buffer in flight",
                                  "shared buffer complete", "selected material complete"])
        self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 0x11],
                         [b"\x00\x00", b"\x04\x01"])
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_unknown_source_buffer_failures_stop_before_assists_or_selected_material(self):
        outcomes = (None, reply(1, 0x11, status=2), reply(1, 0x11, status=4),
                    reply(1, 0x11, data=b"\x00"), OSError("synthetic shared BUFFER failure"))
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.make_unknown_unload()
                self.outcomes[(0x11, b"\x00\x00")] = outcome
                with self.assertRaises((OSError, RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 0x11],
                                 [b"\x00\x00"])
                self.assertFalse(any("E-20" in script or "E-30" in script for script in self.gcode.scripts))
                self.assertNotIn("G90\n G0 Y100.00 F20000.00", self.gcode.scripts)
                self.assertIsNone(self.box.last_cmd)
                self.assertTrue(self.lifecycle.head_present())
                self.assert_released()

    def test_unknown_source_material_failure_does_not_retry_or_return_the_head(self):
        self.make_unknown_unload()
        self.outcomes[(0x11, b"\x04\x01")] = reply(1, 0x11, status=3)
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 0x11],
                         [b"\x00\x00", b"\x04\x01"])
        self.assertNotIn("G90\n G0 Y100.00 F20000.00", self.gcode.scripts)
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "None")
        self.assert_released()

    def test_unknown_source_omitted_retraction_sequence_blocks_the_return_move(self):
        self.make_unknown_unload()
        self.box.retraction.box_retrude_material = lambda report_error=True: True
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertFalse(any(opcode == 0x11 for _, opcode, _ in self.native.commands()))
        self.assertNotIn("G90\n G0 Y100.00 F20000.00", self.gcode.scripts)
        self.assertNotIn("G90\n G0 X42.00 F20000.00", self.gcode.scripts)
        self.assertIsNone(self.box.last_cmd)
        self.assert_released()

    def test_unknown_source_cancellation_or_replacement_during_buffer_stops_later_effects(self):
        for change in ("cancel", "replace", "owner"):
            with self.subTest(change=change):
                self.make_unknown_unload()

                def revoke(address, command, data):
                    if command == 0x11 and data == b"\x00\x00":
                        if change == "cancel":
                            self.box.material_trial.cancel()
                        elif change == "replace":
                            self.box._accept_identity(1, b"113" + b"B" * 19, b"replacement")
                        else:
                            self.dispatch = object()

                self.on_native = revoke
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 0x11],
                                 [b"\x00\x00"])
                self.assertFalse(any("E-20" in script or "E-30" in script for script in self.gcode.scripts))
                self.assertNotIn("G90\n G0 Y100.00 F20000.00", self.gcode.scripts)
                self.assertIsNone(self.box.last_cmd)
                self.assert_released()

    def test_unknown_source_postconditions_do_not_accept_head_connection_or_buffer_failure(self):
        for failure in ("head", "connections", "buffer"):
            with self.subTest(failure=failure):
                self.make_unknown_unload()
                if failure == "head":
                    self.clear_head_on_retract = False
                material_sent = []

                def change_final_observation(address, command, data):
                    if command == 0x11 and data == b"\x04\x01":
                        material_sent.append(True)
                    if command == 0x0A and material_sent:
                        if failure == "connections":
                            self.connections = 4
                        elif failure == "buffer":
                            self.buffer = 0

                self.on_native = change_final_observation
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 0x11],
                                 [b"\x00\x00", b"\x04\x01"])
                self.assertIsNone(self.box.last_cmd)
                self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), "None")
                self.assertNotIn("M104 S0", self.gcode.scripts)
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
                  ("clean_right_pos_x", 9999), ("extrude_pos_y", -4),
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
                with self.assertLogs(level="INFO") as logs, self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                status = "None" if outcome is None else "0x%02x" % outcome[3]
                self.assertTrue(any("CFS rejected reply before decoding addr=1 cmd=0x08" in line
                                    and "status=" + status in line for line in logs.output))
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
        self.assertEqual(self.gcode.scripts.count("SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=30000"), 1)
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
                self.assertEqual(self.gcode.scripts.count("SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=30000"), 1)
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

    def test_unreleased_cut_switch_rejects_the_final_result_without_inventing_a_release_move(self):
        self.make_box("UNLOAD")
        self.release_cut = False
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        self.assertTrue(self.box.cut_sensor.cut_present)
        self.assertIsNone(self.box.last_cmd)
        self.assertFalse(self.lifecycle.head_present())
        self.assertEqual(self.printer.position[1], self.box.boxcfg.retrude_position_y)
        self.assertEqual([data for _, command, data in self.native.commands() if command == 0x11],
                         [b"\x01\x00", b"\x01\x01"])
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
        self.assertEqual(self.gcode.scripts.count("SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=30000"), 1)
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

    def test_rear_unload_is_rejected_before_heat_requests_or_a_recovery_move(self):
        self.make_box("UNLOAD")
        self.printer.position[1] = self.box.boxcfg.extrude_pos_y
        with self.assertRaisesRegex(RuntimeError, "safe.?Y"):
            self.run_trial()
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])
        self.assertEqual(self.printer.heater.target, 0)
        self.assertEqual(self.box.last_cmd, self.target)
        self.assert_released()

    def test_recipe_and_registered_dependencies_are_checked_before_effects(self):
        for change in ("order", "extra_motion", "arguments", "missing_recipe", "missing_handler", "missing_factory"):
            with self.subTest(change=change):
                self.make_box()
                section = self.box.boxcfg.config.sections["gcode_macro BOX_LOAD_MATERIAL_WITH_MATERIAL"]
                if change == "order":
                    section["gcode"] = section["gcode"].replace("BOX_NOZZLE_CLEAN\n", "")
                elif change == "extra_motion":
                    section["gcode"] += "\nG0 Y399"
                elif change == "arguments":
                    section["gcode"] = section["gcode"].replace("BOX_MATERIAL_FLUSH", "BOX_MATERIAL_FLUSH LEN=99")
                elif change == "missing_recipe":
                    section.pop("gcode")
                elif change == "missing_handler":
                    self.gcode.commands.pop("BOX_NOZZLE_CLEAN")
                else:
                    self.gcode.create_gcode_command = None
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assert_released()

    def test_macro_alias_cannot_change_parameters_or_claim_a_phase_twice(self):
        self.make_box()
        base = self.gcode.commands["BOX_MATERIAL_FLUSH"]
        attempts = []
        def alias(command):
            with self.assertRaises(CommandError):
                base(HostCommand({"LEN": "99"}, "BOX_MATERIAL_FLUSH_99 LEN=99"))
            base(HostCommand(command.get_command_parameters(), command.get_commandline()))
            attempts.append(True)
            with self.assertRaises(CommandError):
                base(HostCommand(command.get_command_parameters(), command.get_commandline()))
        self.gcode.commands["BOX_MATERIAL_FLUSH"] = alias
        self.run_trial()
        self.assertEqual(attempts, [True])
        feed = "G0 E%.2f F%.2f" % (self.box.boxcfg.Tn_extrude, self.box.boxcfg.Tn_extrude_velocity[0])
        self.assertEqual(self.gcode.scripts.count(feed), 1)
        self.assert_released()

    def test_swallowed_alias_exception_revokes_scope_before_restore(self):
        self.make_box()
        base = self.gcode.commands["BOX_MATERIAL_FLUSH"]
        original_clean = self.box.nozzle_clean
        clean_calls = []
        def clean():
            clean_calls.append(True)
            if len(clean_calls) == 2:
                raise RuntimeError("synthetic failure inside the flush alias")
            return original_clean()
        self.box.nozzle_clean = clean
        def alias(command):
            try:
                base(HostCommand(command.get_command_parameters(), command.get_commandline()))
            except RuntimeError:
                pass  # The retained dispatcher can swallow generic exceptions.
        self.gcode.commands["BOX_MATERIAL_FLUSH"] = alias
        with self.assertRaises(RuntimeError):
            self.run_trial()
        self.assertEqual(len(clean_calls), 2)
        self.assertEqual(self.box.last_cmd, self.target)
        self.assertEqual(self.printer.position[1], self.box.boxcfg.extrude_pos_y)
        self.assertFalse(any(script.startswith("G0 X%.2f F20000" % self.box.boxcfg.clean_right_pos_x)
                             for script in self.gcode.scripts))
        self.assert_released()

    def test_a_macro_that_omits_its_base_handler_cannot_accept_the_trial(self):
        self.make_box()
        self.gcode.commands["BOX_MATERIAL_FLUSH"] = lambda command: None
        with self.assertRaisesRegex(RuntimeError, "did not complete"):
            self.run_trial()
        self.assertEqual(self.box.last_cmd, self.target)
        self.assertEqual(self.printer.position[1], self.box.boxcfg.extrude_pos_y)
        self.assert_released()

    def test_owned_load_profile_rejects_unbounded_length_and_actual_configured_portions_before_effects(self):
        cases = [("Tn_extrude", value) for value in (0, 201, 150.5, True, float("nan"), float("inf"))]
        cases += [("Tn_extrude_velocity", value) for value in (None, (), (0,), (float("inf"),), (360, 360))]
        cases += [("Tn_extrude_percent", value) for value in ((), (0,), (-1,), (float("nan"),), (100,), (1, 1))]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                self.make_box(Tn_extrude=150)
                setattr(self.box.boxcfg, name, value)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assert_released()

    def test_owned_load_false_positive_feed_stops_before_flush_clean_tail_and_restore(self):
        self.make_box(Tn_extrude=150)
        original_feed, original_clean = self.box.Tn_Extrude, self.box.nozzle_clean
        cleans, restored = [], []

        def feed(length, *args, **kwargs):
            return False if length == 150 else original_feed(length, *args, **kwargs)

        def clean():
            cleans.append(True)
            return original_clean()

        self.box.Tn_Extrude, self.box.nozzle_clean = feed, clean
        self.gcode.commands["RESTORE_POSITION"] = lambda command: restored.append(True)
        with self.assertRaisesRegex(RuntimeError, "positive feed"):
            self.run_trial()
        self.assertEqual(cleans, [True])  # The retained pre-clean already ran.
        self.assertEqual(restored, [])
        self.assertFalse(any("E-5 " in script for script in self.gcode.scripts))
        self.assertEqual(self.box.last_cmd, self.target)
        self.assert_released()

    def test_owned_load_unit_scaling_is_required_before_effects_and_rechecked_before_positive_queue(self):
        for when in ("preflight", "flush_entry", "inside_feed"):
            with self.subTest(when=when):
                self.make_box(Tn_extrude=150)
                if when == "preflight":
                    self.factors["extrude_factor"] = 2
                elif when == "flush_entry":
                    original = self.gcode.commands["BOX_MATERIAL_FLUSH"]

                    def alias(command):
                        self.factors["speed_factor"] = 2
                        return original(command)

                    self.gcode.commands["BOX_MATERIAL_FLUSH"] = alias
                else:
                    original_script = self.gcode.on_script

                    def script(value):
                        original_script(value)
                        if value == "G91" and self.box.material_trial.owns_phase("LOAD", "BOX_MATERIAL_FLUSH"):
                            self.factors["extrude_factor"] = 2

                    self.gcode.on_script = script
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                if when == "preflight":
                    self.assertEqual(self.native.calls, [])
                    self.assertEqual(self.gcode.scripts, [])
                self.assertFalse(any("E150.00" in script for script in self.gcode.scripts))
                self.assertFalse(any("E-5 " in script for script in self.gcode.scripts))
                self.assert_released()

    def test_coordinated_unload_requires_real_unit_scaling_and_step_submission_before_effects(self):
        for case in ("no_flush", "no_status", "speed", "extrude", "nan", "string", "boolean"):
            with self.subTest(case=case):
                self.make_box("UNLOAD")
                if case == "no_flush":
                    self.box.toolhead.flush_step_generation = None
                elif case == "no_status":
                    del self.box.gcode_move.get_status
                else:
                    key, value = {"speed": ("speed_factor", .5), "extrude": ("extrude_factor", 2),
                        "nan": ("speed_factor", float("nan")), "string": ("extrude_factor", "1"),
                        "boolean": ("speed_factor", True)}[case]
                    self.factors[key] = value
                with self.assertRaises((ValueError, RuntimeError, AttributeError)):
                    self.run_trial()
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.gcode.scripts, [])
                self.assert_released()

    def test_coordinated_tail_is_submitted_without_wait_before_material_and_completed_before_finish(self):
        self.make_box("UNLOAD")
        seen, finished = [], []
        original_finish = self.box.retraction._finish

        def native(address, command, data):
            if command == 17 and data[1] == 1:
                seen.append((self.pose.submitted_e, self.printer.position[3], self.pose.completed_e))

        def finish(call, report_error):
            finished.append((self.pose.completed_e, self.printer.position[3]))
            return original_finish(call, report_error)

        self.on_native, self.box.retraction._finish = native, finish
        self.run_trial()
        self.assertEqual(len(seen), 1)
        submitted, queued, completed = seen[0]
        self.assertEqual(submitted, queued)
        self.assertEqual(queued, completed - 30)
        self.assertEqual(finished, [(queued, queued)])
        tail = self.events.index(("gcode", "G0 E-30 F600"))
        flush = next(index for index, event in enumerate(self.events) if event[0] == "flush")
        material = self.events.index(("native", 17, b"\x01\x01"))
        self.assertLess(tail, flush)
        self.assertLess(flush, material)
        self.assertFalse(any(event == ("gcode", "M400") for event in self.events[tail:material]))
        self.assertEqual(self.events[material + 1], ("gcode", "M400"))
        self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 17],
                         [b"\x01\x00", b"\x01\x01"])
        self.assert_released()

    def test_coordinated_material_fault_leaves_only_finite_tail_pending_and_preserves_source(self):
        bad_crc = bytearray(reply(1, 17))
        bad_crc[-1] ^= 1
        outcomes = (reply(1, 17, 0x13), reply(1, 17, 0x19), reply(1, 17, data=b"bad"), None, bytes(bad_crc))
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.make_box("UNLOAD")
                self.outcomes[(17, b"\x01\x01")] = outcome
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                material = self.events.index(("native", 17, b"\x01\x01"))
                self.assertEqual(self.events[material + 1:], [])
                self.assertEqual(self.pose.submitted_e, self.printer.position[3])
                self.assertEqual(self.printer.position[3], self.pose.completed_e - 30)
                self.assertEqual(self.box.last_cmd, self.target)
                self.assertEqual(self.box.box_state.get_Tn_data(1, "filament"), self.target)
                self.assertEqual(self.gcode.scripts.count("G0 E-30 F600"), 1)
                self.assert_released()

    def test_coordinated_tail_rechecks_actual_hot_state_and_factors_after_blocking_assist(self):
        for case in ("cold_flag", "cold_actual", "overtemperature", "nan", "speed", "extrude"):
            with self.subTest(case=case):
                self.make_box("UNLOAD")
                original = self.box.Tn_Extrude

                def feed(length, *args, **kwargs):
                    result = original(length, *args, **kwargs)
                    if length == -20:
                        if case == "cold_flag":
                            self.printer.heater.can_extrude = False
                        elif case in ("cold_actual", "overtemperature", "nan"):
                            self.printer.heater.smoothed_temp = {"cold_actual": 20,
                                "overtemperature": 300, "nan": float("nan")}[case]
                        else:
                            self.factors["speed_factor" if case == "speed" else "extrude_factor"] = 2
                    return result

                self.box.Tn_Extrude = feed
                with self.assertRaises((RuntimeError, ValueError)):
                    self.run_trial()
                self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 17], [b"\x01\x00"])
                self.assertFalse(any("E-30" in script for script in self.gcode.scripts))
                self.assertFalse(any(event[0] == "flush" for event in self.events))
                self.assertEqual(self.box.last_cmd, self.target)
                self.assert_released()

    def test_coordinated_false_assist_stops_before_tail_submission_and_material(self):
        self.make_box("UNLOAD")
        self.box.Tn_Extrude = lambda *args, **kwargs: False
        returned = []
        self.gcode.commands["BOX_GO_TO_BOX_EXTRUDE_POS"] = lambda command: returned.append(True)
        with self.assertRaises(RuntimeError):
            self.run_trial()
        self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 17], [b"\x01\x00"])
        self.assertFalse(any("E-30" in script for script in self.gcode.scripts))
        self.assertFalse(any(event[0] == "flush" for event in self.events))
        self.assertEqual(self.box.last_cmd, self.target)
        self.assertEqual(returned, [])
        self.assert_released()

    def test_material_generation_loss_leaves_finite_tail_without_wait_finish_or_xyz_return(self):
        self.make_box("UNLOAD")

        def replace(address, command, data):
            if command == 17 and data[1] == 1:
                self.box._binding_generation += 1

        self.on_native = replace
        with self.assertRaises((RuntimeError, ValueError)):
            self.run_trial()
        stop = self.events.index(("native", 17, b"\x01\x01"))
        self.assertEqual(self.events[stop + 1:], [])
        self.assertEqual(self.printer.position[3], self.pose.completed_e - 30)
        self.assertEqual(self.box.last_cmd, self.target)
        self.assert_released()

    def test_submission_fault_or_generation_loss_never_enters_material_or_wait_cleanup(self):
        for fault in ("exception", "generation", "cancel"):
            with self.subTest(fault=fault):
                self.make_box("UNLOAD")

                def flush():
                    self.events.append(("flush_fault", fault))
                    if fault == "exception":
                        raise OSError("synthetic submission failure")
                    if fault == "generation":
                        self.box._binding_generation += 1
                    else:
                        self.box.material_trial.cancel()

                self.box.toolhead.flush_step_generation = flush
                with self.assertRaises((RuntimeError, ValueError, OSError)):
                    self.run_trial()
                stop = self.events.index(("flush_fault", fault))
                self.assertEqual(self.events[stop + 1:], [])
                self.assertEqual([data for _, opcode, data in self.native.commands() if opcode == 17], [b"\x01\x00"])
                self.assertEqual(self.printer.position[3], self.pose.completed_e - 30)
                self.assertEqual(self.box.last_cmd, self.target)
                self.assert_released()

    def test_other_dispatch_and_unclaimed_phase_cannot_select_coordinated_profile(self):
        self.make_box("UNLOAD")
        self.assertFalse(self.box.material_trial.owns_phase("UNLOAD", "BOX_RETRUDE_MATERIAL"))
        observed = []

        def inspect(address, command, data):
            if command != 17 or data[1] != 0:
                return
            self.assertTrue(self.box.material_trial.owns_phase("UNLOAD", "BOX_RETRUDE_MATERIAL"))
            self.assertFalse(self.box.material_trial.owns_phase("LOAD", "BOX_MATERIAL_FLUSH"))
            owner, self.dispatch = self.dispatch, object()
            try:
                observed.append(self.box.material_trial.owns_phase("UNLOAD", "BOX_RETRUDE_MATERIAL"))
                with self.assertRaises(RuntimeError):
                    self.box.host_guard()
            finally:
                self.dispatch = owner

        self.on_native = inspect
        self.run_trial()
        self.assertEqual(observed, [False])
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
