"""Compose phase executors, real Box handlers and synthetic native replies."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.lifecycle import LifecycleStopped
from test_box_data import CommandError
import test_host_actions
from test_host_actions import HostCommand
from test_native import reply


class PhaseActionTests(unittest.TestCase):
    def make_box(self, version=b"113", identify=True, **settings):
        fixture = test_host_actions.HostActionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.make_box(**settings)
        self.fixture = fixture
        for name in ("box", "native", "printer", "gcode", "reactor", "lifecycle"):
            setattr(self, name, getattr(fixture, name))
        self.native.version, self.native.mode = version, 0
        self.material, self.connections, self.buffer = 1, 1, 0
        self.buffer_event = self.material_event = False
        self.head_status, self.fire_head = 0, True
        self.empty_after_stage7 = False
        self.set_head(False)
        original = self.native.cmd_send_data_with_response

        def exchange(body, timeout, *args):
            raw = original(body, timeout, *args)
            address, command, data = body[0], body[3], body[4:]
            if command == 0x0A and version not in (b"113", b"122"):
                raw = reply(address, command, data=bytes((20, 30, 0, self.native.mode, 1, 1)))
            elif command == 0x08:
                if self.material_event and data == b"\x00":
                    raw = reply(address, command, status=0x30, data=b"\x01\x02\x03\x04")
                else:
                    raw = reply(address, command, data=bytes((self.material if data == b"\x00" else self.connections,)))
            elif command == 0x05:
                raw = (reply(address, command, status=0x30, data=b"\x01\x02\x03\x04")
                       if self.buffer_event else reply(address, command, data=bytes((self.buffer,))))
            elif command == 0x03:
                raw = reply(address, command, data=b"\x01\x02\x03\x04")
            elif command == 0x10:
                if data[1] == 4 and self.fire_head:
                    self.set_head(True)
                    self.printer.send_event("box:extrude_process_stage7")
                    self.printer.send_event("box:extrude_process_stage7")
                elif data[1] == 6:
                    raw = reply(address, command, status=self.head_status)
                elif data[1] == 7 and self.empty_after_stage7:
                    self.buffer = 0
            return raw

        self.native.cmd_send_data_with_response = exchange
        self.gcode.on_script = lambda script: setattr(self.box.print_stats, "state", "paused") if script == "PAUSE POST_WORK=0" else None
        fixture.ready(identify=identify)
        self.native.calls.clear()
        self.gcode.scripts.clear()
        self.reactor.pauses.clear()

    def set_head(self, value):
        self.printer.objects["filament_switch_sensor head"].runout_helper.filament_present = value

    def command(self, name, **params):
        return self.fixture.command(name, **params)

    def stages(self):
        return [data[1] for address, command, data in self.native.commands() if command == 0x10]

    def test_complete_load_uses_head_snapshot_and_each_version_profile(self):
        for version in (b"113", b"152"):
            with self.subTest(version=version):
                self.make_box(version)
                self.assertIsNone(self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A"))
                self.assertEqual(self.stages(), [0, 4, 6, 5])
                self.assertEqual(self.box.last_cmd, "T1A")
                self.assertEqual(self.box.get_status(0)["T1"]["filament"], "T1A")
                self.assertFalse(self.box.operations.active)
                self.assertTrue(self.box.heart_process_enable[0])
                self.assertFalse(self.box.filament_sensor_enable)
                self.assertEqual(self.box.error_next_macro, [])
                self.assertEqual(self.gcode.scripts.count("M83\n G0 E5 F60"), 1)
                self.assertIn("M82", self.gcode.scripts)

    def test_nested_load_preserves_its_parent_operation(self):
        self.make_box()
        self.box.operations.begin("T1B", "T1A", 1)
        serial = self.box.operations._serial
        self.assertTrue(self.box.box_extrude_material("T1A"))
        self.assertTrue(self.box.operations.active)
        self.assertEqual(self.box.operations._serial, serial)
        self.assertTrue(self.box.heart_process_enable[0])
        self.assertFalse(self.box.filament_sensor_enable)
        self.box.operations.cancel()

    def test_nested_load_rejects_another_target_before_effects(self):
        self.make_box()
        self.box.operations.begin(None, "T1B", 1)
        with self.assertRaisesRegex(RuntimeError, "owning operation"):
            self.box.box_extrude_material("T1A")
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.gcode.scripts, [])
        self.assertTrue(self.box.operations.active)
        self.box.operations.cancel()

    def test_failed_head_result_is_not_hidden_by_outer_stage4_success(self):
        self.make_box()
        self.head_status = 0x0D
        self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A")
        self.assertEqual(self.box.error, "macro_box_extrude_err")
        self.assertIn('"code":"key864"', self.box.last_err)
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.error_next_macro, ["BOX_EXTRUDE_MATERIAL TNN=T1A"])
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.box.heart_process_enable[0])

    def test_full_buffer_encodes_integral_configured_length_without_mutation(self):
        self.make_box(extrude_material_len_for_box="3")
        self.buffer, self.empty_after_stage7 = 1, True
        self.assertIs(type(self.box.boxcfg.extrude_material_len_for_box), float)
        self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A")
        self.assertEqual(self.stages(), [0, 4, 6, 5, 7])
        self.assertIn((1, 0x10, b"\x01\x07\x03"), self.native.commands())
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertEqual(self.box.boxcfg.extrude_material_len_for_box, 3.0)
        self.assertIs(type(self.box.boxcfg.extrude_material_len_for_box), float)

    def test_fractional_box_amount_fails_before_final_feed_effects(self):
        self.make_box()
        self.box.boxcfg.extrude_material_len_for_box = 3.5
        with self.assertRaises(ValueError):
            self.box.loading.box_extrude_material_stage8("T1A")
        self.assertEqual(self.gcode.scripts, [])
        self.assertEqual(self.native.calls, [])

    def test_unknown_buffer_stops_without_gear_cut_or_retract(self):
        self.make_box()
        self.buffer_event = True
        with self.assertRaisesRegex(RuntimeError, "buffer observation"):
            self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A")
        self.assertEqual(self.stages(), [0, 4, 6, 5])
        self.assertFalse(any(command == 0x11 for _, command, _ in self.native.commands()))
        self.assertIsNone(self.box.error)
        self.assertIsNone(self.box.last_cmd)
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.box.heart_process_enable[0])

    def test_unknown_material_does_not_report_a_confirmed_empty_slot(self):
        self.make_box()
        self.material_event = True
        with self.assertRaisesRegex(RuntimeError, "material-sensor observation"):
            self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A")
        self.assertEqual(self.stages(), [])
        self.assertIsNone(self.box.last_err)
        self.assertIsNone(self.box.error)
        self.assertFalse(self.box.operations.active)

    def test_resume_hint_precedes_load_and_does_not_change_live_source(self):
        self.make_box()
        self.box.print_stats.state = "printing"
        self.material = 0
        self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A")
        self.assertEqual(self.box.store.recovery_hints()["last_cmd"], "T1A")
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.error, "filament_err")
        self.assertIn('"code":"key839"', self.box.last_err)

    def test_raw_target_is_not_remapped_twice_and_pickup_accepts_none(self):
        self.make_box()
        self.command("BOX_MODIFY_TN", T1A="T1B")
        self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A")
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertTrue(all(data[0] == 1 for _, command, data in self.native.commands() if command == 0x10))
        before = len(self.native.calls)
        self.command("BOX_EXTRUDER_EXTRUDE")
        self.assertEqual(self.box.box_state.get_Tn_data(0, uppart="filament_useup"), 0)
        self.assertEqual(len(self.native.calls), before)

    def test_known_unload_updates_source_only_on_its_normal_tail(self):
        self.make_box()
        self.box.last_cmd = "T1A"
        self.box.update_filament_pos("T1A", "T1A")
        self.command("BOX_RETRUDE_MATERIAL")
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertEqual(self.box.get_status(0)["T1"]["filament"], "None")
        self.assertIn((1, 0x11, b"\x01\x01"), self.native.commands())

    def test_unknown_clear_unload_uses_intersection_of_fresh_masks(self):
        self.make_box()
        self.material, self.connections = 5, 1
        self.command("BOX_RETRUDE_MATERIAL")
        retracts = [data for _, command, data in self.native.commands() if command == 0x11]
        self.assertEqual(retracts, [b"\x01\x01"])
        self.assertIsNone(self.box.error)

    def test_unknown_present_unload_uses_shared_buffer_then_selected_material(self):
        self.make_box()
        self.set_head(True)
        self.box.last_cmd_tmp = "T3B"
        self.command("BOX_RETRUDE_MATERIAL")
        retracts = [data for _, command, data in self.native.commands() if command == 0x11]
        self.assertEqual(retracts, [b"\x00\x00", b"\x01\x01"])
        self.assertIn("G0 E-20.00 F360.00", self.gcode.scripts)
        self.assertIn("G1 E-30 F2400", self.gcode.scripts)
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.last_cmd_tmp, "T3B")

    def test_sensor_retry_defers_head_event_during_loose_motion(self):
        self.make_box()
        self.fire_head = False
        self.box.virtual_sdcard.gcode_metadata = {}
        self.native.responses[(1, 0x10)] = [reply(1, 0x10), reply(1, 0x10),
                                           reply(1, 0x10, status=0x0A)]
        observed = []
        def head_during_loose(script):
            if self.box.extrude_process_inside_retry_loose and not observed:
                self.set_head(True)
                self.printer.send_event("box:extrude_process_stage7")
                observed.append(self.box.extrude_process_stage7_flag_when_retry_loose)
        self.gcode.on_script = head_during_loose
        self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A")
        self.assertEqual(observed, [True])
        self.assertEqual(self.stages(), [0, 4, 5, 4, 6])
        self.assertEqual(self.box.last_cmd, "T1A")
        self.assertFalse(self.box.extrude_process_inside_retry_loose)
        self.assertFalse(self.box.extrude_process_stage7_flag_when_retry_loose)
        self.assertTrue(self.box.heart_process_enable[0])

    def test_explicit_retract_retains_its_distinct_resume_and_return_policy(self):
        self.make_box()
        self.box.last_cmd = "T1A"
        self.box.virtual_sdcard.do_resume_status = True
        self.native.responses[(1, 0x11)] = [reply(1, 0x11, status=0x13)]
        self.command("BOX_RETRUDE_MATERIAL_WITH_TNN", TNN="T1A")
        self.assertIn((1, 0x11, b"\x01\x01"), self.native.commands())
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.box.last_cmd_tmp, "T1A")
        self.assertEqual(self.box.error_next_macro, [])

    def test_phase_resume_and_existing_error_capture_precede_target_parsing(self):
        self.make_box()
        names = ("BOX_EXTRUDE_MATERIAL", "BOX_EXTRUDER_EXTRUDE", "BOX_RETRUDE_MATERIAL")
        self.box.virtual_sdcard.do_resume_status = True
        for name in names:
            with self.subTest(name=name), self.assertLogs(level="WARNING"):
                self.gcode.commands[name](object())
        self.assertEqual(self.native.calls, [])
        self.box.virtual_sdcard.do_resume_status = False
        self.box.error = "cut_err"
        for name in names:
            self.gcode.commands[name](HostCommand({}, name))
        self.assertEqual(self.box.error_next_macro, list(names))
        self.assertEqual(self.native.calls, [])

    def test_direct_feed_keeps_stage_binding_and_legacy_execution_gates(self):
        self.make_box()
        self.command("BOX_EXTRUDE_PROCESS", ADDR="1", NUM="A", STAGE="5")
        self.assertEqual(self.stages(), [2])
        before = len(self.native.calls)
        with self.assertRaisesRegex(CommandError, "unresolved"):
            self.command("BOX_EXTRUDE_2_PROCESS", ADDR="1", NUM="A", TRIGGER="BUFFER")
        self.box.boxcfg.version = 0
        with self.assertRaisesRegex(ValueError, "encoding-0"):
            self.command("BOX_EXTRUDE_PROCESS", ADDR="1", NUM="A")
        self.assertEqual(len(self.native.calls), before)

    def test_shutdown_in_final_feed_stops_old_phase_and_restores_permission(self):
        self.make_box()
        self.reactor.on_pause = lambda: self.printer.send_event("klippy:shutdown")
        with self.assertRaises(LifecycleStopped):
            self.command("BOX_EXTRUDE_MATERIAL", TNN="T1A")
        self.assertIsNone(self.box.last_cmd)
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.box.heart_process_enable[0])

    def test_retained_prime_tower_policy_uses_slicer_version_and_first_tool(self):
        self.make_box()
        sd = self.box.virtual_sdcard
        self.box.print_stats.state = "printing"
        sd.first_tnn_finished = True
        sd.get_gcode_flush_para = lambda: {"purge_in_prime_tower": 1}
        for version, expected in (("5.1.7.10470", None), ("5.1.7.10471", 1), ("5.2", 1)):
            sd.gcode_metadata = {"metadata": {"slicer": "creality", "slicer_version": version}}
            with self.subTest(version=version):
                self.assertEqual(self.box.get_purge_in_prime_tower(), expected)
        sd.first_tnn_finished = False
        self.assertIsNone(self.box.get_purge_in_prime_tower())
        sd.first_tnn_finished = True
        sd.gcode_metadata["metadata"]["slicer"] = "other"
        self.assertIsNone(self.box.get_purge_in_prime_tower())


if __name__ == "__main__":
    unittest.main()
