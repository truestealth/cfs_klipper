"""Compose tool changes and print commands with real phase implementations."""

import json
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.lifecycle import LifecycleStopped
from cfs.operations import OperationCancelled
import test_phase_actions
from test_native import reply


class ToolActionTests(unittest.TestCase):
    def make_box(self):
        self.fixture = test_phase_actions.PhaseActionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.make_box()
        for name in ("box", "native", "printer", "gcode", "reactor", "lifecycle"):
            setattr(self, name, getattr(self.fixture, name))
        self.box.toolhead.max_velocity = 300
        sd = self.box.virtual_sdcard
        sd.gcode_metadata = {}
        sd.first_tnn_finished = True
        sd.get_gcode_flush_para = lambda: None
        sd.get_first_Tn = lambda default: default
        self.masks = {1: (1, 0)}
        self.versions = {}
        self.wheels = {}
        self.wheel_step = 20
        self.on_native = None
        self.clear_head_after_cut = False
        original = self.native.cmd_send_data_with_response

        def exchange(body, timeout, *args):
            raw = original(body, timeout, *args)
            address, command, data = body[0], body[3], body[4:]
            if command == 0x14 and address in self.versions:
                raw = reply(address, command, data=self.versions[address] + bytes((64 + address,)) * 19)
            elif command == 0x0A:
                profile = self.box.device_versions.profile(address)
                if profile and profile.state_data_length == 6:
                    raw = reply(address, command, data=bytes((20, 30, 0, self.native.mode, 1, 1)))
            elif command == 0x08 and address in self.masks:
                material, connections = self.masks[address]
                raw = reply(address, command, data=bytes((material if data == b"\x00" else connections,)))
            elif command == 0x0E:
                import struct
                value = self.wheels.setdefault(address, 100)
                self.wheels[address] -= self.wheel_step
                raw = reply(address, command, data=struct.pack(">f", value))
            if self.on_native:
                self.on_native(address, command, data)
            return raw

        self.native.cmd_send_data_with_response = exchange
        original_script = self.gcode.on_script

        def script_effect(script):
            if original_script:
                original_script(script)
            if self.clear_head_after_cut and script.startswith("SET_VELOCITY_LIMIT ACCEL=1500.000"):
                self.fixture.set_head(False)
                self.clear_head_after_cut = False
            if script == "G90\n G0 Y%.2f F24000\n M400" % (self.box.boxcfg.safe_pos_y + 1):
                self.printer.position[1] = self.box.boxcfg.safe_pos_y + 1

        self.gcode.on_script = script_effect
        self.lifecycle.aggregate_state()
        self.gcode.scripts.clear()

    def second_box(self):
        self.masks[2] = (2, 0)
        self.versions[2] = b"152"
        self.box.device_versions.set_family(2, "cfs0")
        self.lifecycle.address_table[1].online = 1
        self.lifecycle.get_connect_state()
        self.native.calls.clear()
        self.gcode.scripts.clear()

    def command(self, name, **parameters):
        return self.fixture.command(name, **parameters)

    def replace_device(self, address):
        payload = b"113" + b"z" * 19
        self.box.invalidate_device(address)
        self.box._accept_identity(address, payload, reply(address, 0x14, data=payload))
        self.box.device_versions.set_family(address, "cfs0")

    def test_all_generated_bindings_keep_raw_request_and_single_logical_lookup(self):
        self.make_box()
        matrix = json.loads((Path(__file__).resolve().parents[1] / "docs/contract-matrix.json").read_text(encoding="utf-8"))
        names = {row["name"] for row in matrix["items"] if row["kind"] == "gcode" and row["when"] == "generated"}
        seen = []
        self.box.Tn_action = seen.append
        for name in names:
            self.assertIsNone(self.command(name))
            self.assertEqual(self.box.t_command, name)
            self.assertEqual(seen[-1], self.box.box_state.Tn_map.get(name, name))
        self.assertEqual(len(names), 32)
        self.assertTrue(names.issubset(self.gcode.commands))
        self.assertEqual(self.native.calls, [])

    def test_first_tool_tray_cycle_composes_phases_and_restores_context(self):
        self.make_box()
        self.assertIsNone(self.command("T0"))
        self.assertEqual(self.box.t_command, "T0")
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.fixture.stages(), [0, 4, 6, 5])
        commands = self.native.commands()
        print_index = commands.index((1, 0x04, b"\x01\x00"))
        wheel_index = next(i for i, (_, cmd, _) in enumerate(commands) if cmd == 0x0E)
        self.assertLess(print_index, wheel_index)
        self.assertIn("G91\n G1 E-2.50 F4800\n G90", self.gcode.scripts)
        self.assertIsNone(self.box.move_z)
        self.assertIsNone(self.box.max_velocity)
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.box.heart_process_enable[0])
        self.assertIn({"tn_cur_tnn": "T1A"}, [kwargs for args, kwargs in self.printer.records])

    def test_known_source_to_another_box_uses_each_firmware_profile(self):
        self.make_box()
        self.second_box()
        self.box.last_cmd = "T1A"
        self.masks[1] = (1, 1)
        self.fixture.set_head(True)
        self.clear_head_after_cut = True
        self.command("T5")
        self.assertEqual(self.box.last_cmd, "T2B")
        self.assertEqual(self.box.t_command, "T5")
        self.assertEqual(self.box.device_versions.profile(1).version, "1.1.3")
        self.assertEqual(self.box.device_versions.profile(2).version, "1.5.2")
        self.assertIn((1, 0x11, b"\x01\x01"), self.native.commands())
        self.assertIn((2, 0x04, b"\x02\x00"), self.native.commands())
        self.assertTrue(all(address == 2 for address, cmd, _ in self.native.commands() if cmd == 0x0E))

    def test_alias_of_same_source_skips_cut_unload_and_load(self):
        self.make_box()
        self.second_box()
        self.box.last_cmd = "T2B"
        self.box.box_state.Tnn_map["T1A"] = "T2B"
        self.fixture.set_head(True)
        self.command("T0")
        self.assertEqual(self.native.commands(), [(2, 0x04, b"\x02\x00")])
        self.assertEqual(self.box.last_cmd, "T2B")
        self.assertEqual(self.box.t_command, "T0")

    def test_retained_continue_flag_uses_its_distinct_route(self):
        self.make_box()
        self.box.last_cmd = "T1A"
        self.box.box_state.is_continue_print = True
        self.fixture.set_head(True)
        self.command("T1A")
        self.assertFalse(self.box.box_state.is_continue_print)
        self.assertFalse(any(command in (0x10, 0x11) for _, command, _ in self.native.commands()))
        self.assertIn("G0 E140.00 F360.00", self.gcode.scripts)

    def test_printing_prime_tower_omits_the_host_tray_purge(self):
        self.make_box()
        self.box.print_stats.state = "printing"
        self.box.virtual_sdcard.gcode_metadata = {"metadata": {"slicer": "Creality", "slicer_version": "5.1.7.10471"}}
        self.box.virtual_sdcard.get_gcode_flush_para = lambda: {"purge_in_prime_tower": 1}
        self.command("T0")
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(any(cmd == 0x0E for _, cmd, _ in self.native.commands()))
        self.assertNotIn("G4 P3000", self.gcode.scripts)
        self.assertIn("G90\n G0 Y351.00 F24000\n M400", self.gcode.scripts)

    def test_first_tool_metadata_dispatch_allows_owned_child_and_consumes_retraction(self):
        self.make_box()
        self.box.virtual_sdcard.get_first_Tn = lambda default: "T0"
        self.command("BOX_START_PRINT_EXTRUDE_MATERIAL", START_PRINT="4")
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.start_print_retraction_distance, 0)
        self.assertIn("G91\n G1 E-0 F4800\n G90", self.gcode.scripts)
        self.assertFalse(self.box.operations.active)

    def test_print_end_resets_mapping_and_hints_but_preserves_cache_and_source(self):
        self.make_box()
        self.box.last_cmd = "T1A"
        self.command("BOX_MODIFY_TN", T1A="T2B")
        self.box.store.update("last_cmd", last_cmd="T1A")
        self.box.store.update("enable", enable=0)
        before = json.loads(self.box.store.path.read_text(encoding="utf-8"))["base_data"]
        self.box.preloading_en = True
        self.command("BOX_END_PRINT")
        document = json.loads(self.box.store.path.read_text(encoding="utf-8"))
        self.assertEqual(document["base_data"], before)
        self.assertFalse({"enable", "tnn_map", "last_cmd"} & set(document))
        self.assertEqual(self.box.box_state.Tnn_map["T1A"], "T1A")
        self.assertEqual(self.box.get_status(0)["enable"], 1)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertIn("SET_FILAMENT_SENSOR SENSOR=head ENABLE=0", self.gcode.scripts)

    def test_box_end_runs_actual_cut_unload_and_safe_position(self):
        self.make_box()
        self.box.last_cmd = "T1A"
        self.fixture.set_head(True)
        self.clear_head_after_cut = True
        self.command("BOX_END")
        self.assertIsNone(self.box.last_cmd)
        self.assertIn((1, 0x11, b"\x01\x01"), self.native.commands())
        self.assertIn("G0 Y350.00 F20000.00", self.gcode.scripts)

    def test_ending_material_drains_before_composed_tool_loading(self):
        self.make_box()
        self.box.is_use_ending_material = True
        self.command("T0")
        # Two drain segments and one segment in the later normal purge.
        self.assertEqual(self.gcode.scripts.count("G0 E80.00 F360.00"), 3)
        self.assertFalse(self.box.is_use_ending_material)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(self.box.operations.active)

    def test_late_ending_latch_runs_drain_then_tool_loading(self):
        self.make_box()
        previous = self.gcode.on_script
        def latch(script):
            previous(script)
            if "G1 E-4 F4800" in script:
                self.box.is_use_ending_material = True
        self.gcode.on_script = latch
        self.command("T0")
        self.assertEqual(self.gcode.scripts.count("G0 E80.00 F360.00"), 3)
        self.assertFalse(self.box.is_use_ending_material)
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(self.box.operations.active)

    def test_box_end_source_replacement_cannot_unload_the_replacement(self):
        self.make_box()
        self.box.last_cmd = "T1A"
        self.fixture.set_head(True)
        previous = self.gcode.on_script
        def replace(script):
            previous(script)
            if script.startswith("SET_VELOCITY_LIMIT ACCEL=1500.000"):
                self.replace_device(1)
                self.fixture.set_head(False)
        self.gcode.on_script = replace
        with self.assertRaises(LifecycleStopped):
            self.command("BOX_END")
        self.assertFalse(any(cmd == 0x11 for _, cmd, _ in self.native.commands()))
        self.assertEqual(self.box.last_cmd, "T1A")

    def test_standalone_purge_replacement_during_heating_stops_before_feed(self):
        self.make_box()
        previous = self.gcode.on_script
        def replace(script):
            previous(script)
            if script == "M104 S220":
                self.replace_device(1)
        self.gcode.on_script = replace
        with self.assertRaises(LifecycleStopped):
            self.command("BOX_MATERIAL_CHANGE_FLUSH", LAST_TNN="T1B", TNN="T1A")
        self.assertEqual(self.gcode.scripts, ["M104 S220"])
        self.assertEqual(self.native.calls, [])

    def test_failed_purge_keeps_loaded_source_and_records_flush_error(self):
        self.make_box()
        self.wheel_step = 0
        self.command("T0")
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.error, "flush_err")
        self.assertIn('"code":"key845"', self.box.last_err)
        self.assertEqual(sum(cmd == 0x0E for _, cmd, _ in self.native.commands()), 9)
        self.assertEqual(self.fixture.stages(), [0, 4, 6, 5])
        self.assertFalse(self.box.operations.active)

    def test_slicer_matrix_volume_drives_target_aware_tray_flush(self):
        self.make_box()
        self.box.last_cmd = "T1A"
        self.fixture.set_head(False)
        self.masks[1] = (3, 0)
        self.box.print_stats.state = "printing"
        self.box.virtual_sdcard.gcode_metadata = {"metadata": {"slicer": "Other"}}
        volume = 30 * 0.765625 * math.pi
        self.box.virtual_sdcard.get_gcode_flush_para = lambda: {"flush_multiplier": 1,
            "flush_volumes_matrix": [0, volume, volume, 0]}
        self.command("T1")
        self.assertEqual(self.box.last_cmd, "T1B")
        self.assertIn("G0 E30.00 F360.00", self.gcode.scripts)
        self.assertFalse(any(cmd == 0x0E for _, cmd, _ in self.native.commands()))

    def test_nozzle_clear_service_reads_banks_and_selects_highest_connection_bit(self):
        self.make_box()
        self.fixture.set_head(True)
        self.masks[1] = (5, 5)
        self.command("SET_BOX_MODE_PRINT_WHEN_NOZZLE_CLEAR")
        self.assertEqual(self.native.commands(), [(1, 0x08, b"\x01"), (1, 0x08, b"\x00"),
                                                  (1, 0x04, b"\x04\x00")])

    def test_source_replacement_during_nested_load_stops_the_next_phase(self):
        self.make_box()
        self.second_box()
        self.box.last_cmd = "T1A"
        self.fixture.set_head(False)
        self.on_native = lambda address, command, data: self.box.invalidate_device(1) if (address, command, data[1:] if data else b"") == (2, 0x10, b"\x04\x00") else None
        with self.assertRaises((OperationCancelled, LifecycleStopped)):
            self.command("T5")
        stages = [data[1] for address, cmd, data in self.native.commands() if address == 2 and cmd == 0x10]
        self.assertEqual(stages, [0, 4, 6])
        self.assertIsNone(self.box.last_cmd)
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.box.heart_process_enable[1])


if __name__ == "__main__":
    unittest.main()
