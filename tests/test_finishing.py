from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from cfs.finishing import Finishing


class FinishingTests(unittest.TestCase):
    def test_flush_failure_still_moves_safe_and_does_not_restore_fan(self):
        calls = []
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None,
            Tn_Extrude=lambda *a, **kw: False,
            gcode=SimpleNamespace(run_script_from_command=calls.append),
            boxcfg=SimpleNamespace(safe_pos_y=10),
        )
        self.assertIs(Finishing(box).material_flush(1, [2], 200), False)
        self.assertEqual(calls, ["G90\n G0 Y10.00 F15000.00\n M400"])

    def test_flush_fan_restoration_depends_on_homing(self):
        for axes, expected, restores in (("xyz", True, 1), ("z", None, 0)):
            with self.subTest(axes=axes):
                calls = []
                box = SimpleNamespace(
                    host_guard=lambda *args: lambda: None,
                    Tn_Extrude=lambda *a, **kw: True,
                    gcode=SimpleNamespace(run_script_from_command=lambda script: None),
                    boxcfg=SimpleNamespace(safe_pos_y=10),
                    toolhead=SimpleNamespace(get_status=lambda now: {"homed_axes": axes}),
                    _reactor=SimpleNamespace(monotonic=lambda: 0),
                    motion=SimpleNamespace(restore_fan=lambda: calls.append(True)),
                )
                self.assertIs(Finishing(box).material_flush(1, [2], 200), expected)
                self.assertEqual(len(calls), restores)


    def test_matrix_uses_virtual_tool_indices_after_remapping(self):
        from cfs.state import BoxState
        state = BoxState()
        state.Tnn_map["T1A"] = "T1B"
        state.Tnn_map["T1B"] = "T1A"
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, box_state=state,
            _printer=SimpleNamespace(lookup_object=lambda name: SimpleNamespace(
                get_gcode_flush_para=lambda: {"flush_multiplier": None,
                                             "flush_volumes_matrix": [1, 2, 3, 4]})),
            extrusion=SimpleNamespace(material_volume_to_length=lambda volume: volume),
            gcode=SimpleNamespace(respond_info=lambda message: None),
        )
        self.assertEqual(Finishing(box).get_flush_length_from_gcode("T1B", "T1A"), 2)

    def test_change_flush_uses_target_temperature_only_after_success(self):
        calls = []
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, boxcfg=SimpleNamespace(safe_pos_y=10),
            box_state=SimpleNamespace(get_Tnn_content=lambda tnn: {"addr": 1}),
            get_flush_velocity=lambda source, target: ([90], [1]),
            get_flush_temp=lambda source, target: (210, 220, 230),
            quickly_wait_heating=lambda temp: calls.append(("heat", temp)),
            gcode=SimpleNamespace(run_script_from_command=lambda command: calls.append(command)),
        )
        finishing = Finishing(box)
        finishing.get_flush_len = lambda source, target: 12
        finishing.material_flush = lambda *a, **kw: False
        self.assertIs(finishing.material_change_flush("T1A", "T1B"), False)
        self.assertEqual(calls, [("heat", 230)])
        finishing.material_flush = lambda *a, **kw: None
        self.assertIs(finishing.material_change_flush("T1A", "T1B"), True)
        self.assertEqual(calls[-1], "M104 S220")

    def test_ending_material_drain_is_explicitly_unavailable(self):
        from cfs.box import MissingProtocolEffect
        with self.assertRaises(MissingProtocolEffect):
            Finishing(SimpleNamespace()).is_use_ending_material_flush(err="err", vtnn="T0")

    def test_nozzle_clear_uses_first_nonzero_connection_bank(self):
        calls = []
        class Retraction:
            def _calls(self, addresses):
                return lambda fn, *a, **kw: fn(*a, **kw)
            def _masks(self, call, address):
                calls.append(address)
                return (0, 0) if address == 1 else (10, 1)
        box = SimpleNamespace(
            box_state=SimpleNamespace(Tn_data={"enable": 1, "state": "connect"}),
            get_filament_sensor_detect=lambda: True, retraction=Retraction(),
            communication_set_box_mode=lambda *a, **kw: calls.append((a, kw)),
        )
        Finishing(box).cmd_set_box_mode_print_when_nozzle_clear(None)
        self.assertEqual(calls, [1, 2, ((2, "PRINT"), {"num": "D"})])

    def test_box_end_false_cut_still_runs_outer_safe_move(self):
        calls = []
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, last_err=None, last_cmd="T1A",
            is_use_ending_material=False,
            box_state=SimpleNamespace(Tn_data={"enable": 1},
                                      get_Tnn_content=lambda tnn: {"addr": 1}),
            motion=SimpleNamespace(z_down=lambda **kw: calls.append(("z", kw)),
                                   move_to_safe_pos=lambda: calls.append("safe")),
            get_filament_sensor_detect=lambda: True,
            move_to_cut=lambda **kw: False,
        )
        Finishing(box).cmd_box_end(None)
        self.assertEqual(calls, [("z", {"distance": 0.8}), "safe"])


    def test_flush_uses_colors_when_matrix_is_unavailable(self):
        from cfs.state import BoxState
        state = BoxState()
        state.modify_Tn_data(1, "#112233", "color_value", "A")
        state.modify_Tn_data(1, "#AABBCC", "color_value", "B")
        seen = []
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, box_state=state,
            boxcfg=SimpleNamespace(Tn_extrude=30),
            print_stats=SimpleNamespace(state="standby"),
            get_flush_len=lambda *colors: seen.append(colors) or 42,
            gcode=SimpleNamespace(respond_info=lambda message: None),
        )
        finishing = Finishing(box)
        self.assertEqual(finishing.get_flush_len("T1A", "T1B"), 42)
        self.assertEqual(seen, [([17, 34, 51], [170, 187, 204])])
        self.assertEqual(finishing.get_flush_len(None, "T1B"), 30)
        state.modify_Tn_data(1, "unknown", "color_value", "A")
        self.assertEqual(finishing.get_flush_len("T1A", "T1B"), 30)

    def test_macro_failure_is_captured_and_existing_error_defers(self):
        calls = []
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, error=None, last_cmd="T1B",
            last_cmd_tmp="T1A", error_next_macro=[],
            box_state=SimpleNamespace(Tnn_map={"T1A": "T1A", "T1B": "T1B"}),
            lifecycle=SimpleNamespace(record_error=lambda *args: calls.append(args)),
        )
        finishing = Finishing(box)
        finishing.material_change_flush = lambda *args: calls.append(args) or False
        gcmd = SimpleNamespace(get_command_parameters=lambda: {},
                               get_commandline=lambda: "BOX_MATERIAL_CHANGE_FLUSH")
        finishing.cmd_material_change_flush(gcmd)
        self.assertEqual(calls, [("T1A", "T1B"), ("macro_flush_err", {})])
        self.assertEqual(len(box.error_next_macro), 1)
        box.error = "active"
        finishing.cmd_material_change_flush(gcmd)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(box.error_next_macro), 2)

    def test_nozzle_clear_requires_fresh_sensor_banks(self):
        class Retraction:
            def _calls(self, addresses):
                return lambda fn, *a, **kw: fn(*a, **kw)
            def _masks(self, call, address):
                return None
        box = SimpleNamespace(
            box_state=SimpleNamespace(Tn_data={"enable": 1, "state": "connect"}),
            get_filament_sensor_detect=lambda: True, retraction=Retraction(),
        )
        with self.assertRaisesRegex(RuntimeError, "fresh"):
            Finishing(box).cmd_set_box_mode_print_when_nozzle_clear(None)

    def test_end_retract_failure_idles_current_source_and_clears_error(self):
        calls = []
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, last_err=None, last_cmd="T1A",
            is_use_ending_material=False,
            box_state=SimpleNamespace(Tn_data={"enable": 1},
                                      get_Tnn_content=lambda tnn: {"addr": 1}),
            motion=SimpleNamespace(z_down=lambda **kw: calls.append("z")),
            get_filament_sensor_detect=lambda: False,
            box_retrude_material=lambda **kw: False,
            communication_set_box_mode=lambda *args: calls.append(args),
        )
        self.assertIs(Finishing(box).box_end(), False)
        self.assertEqual(calls, ["z", (1, "IDLE")])
        self.assertEqual(box.last_cmd, "T1A")

    def test_end_success_preserves_source_return_bookkeeping(self):
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, last_err=None, last_cmd="T1A",
            last_cmd_tmp=None, is_use_ending_material=False,
            box_state=SimpleNamespace(Tn_data={"enable": 1},
                                      get_Tnn_content=lambda tnn: {"addr": 1}),
            motion=SimpleNamespace(z_down=lambda **kw: None),
            get_filament_sensor_detect=lambda: False,
            box_retrude_material=lambda **kw: True,
        )
        self.assertIsNone(Finishing(box).box_end())
        self.assertEqual(box.last_cmd_tmp, "T1A")
        self.assertIsNone(box.last_cmd)

    def test_matrix_missing_required_field_propagates(self):
        box = SimpleNamespace(host_guard=lambda *args: lambda: None,
            _printer=SimpleNamespace(lookup_object=lambda name: SimpleNamespace(
                get_gcode_flush_para=lambda: {})))
        with self.assertRaises(KeyError):
            Finishing(box).get_flush_length_from_gcode("T1A", "T1B")

    def test_missing_safe_y_fails_before_extrusion(self):
        calls = []
        box = SimpleNamespace(host_guard=lambda *args: lambda: None,
                              boxcfg=SimpleNamespace(safe_pos_y=None),
                              Tn_Extrude=lambda *a, **kw: calls.append("extrude"))
        with self.assertRaisesRegex(ValueError, "safe_pos_y"):
            Finishing(box).material_flush(10, [100], 200)
        self.assertEqual(calls, [])

    def test_end_drain_gate_precedes_motion_only_on_drain_branch(self):
        from cfs.box import MissingProtocolEffect
        calls = []
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, last_err=None, last_cmd="T1A",
            is_use_ending_material=True,
            box_state=SimpleNamespace(Tn_data={"enable": 1},
                                      get_Tnn_content=lambda tnn: {"addr": 1}),
            motion=SimpleNamespace(z_down=lambda **kw: calls.append("z"),
                                   move_to_safe_pos=lambda: calls.append("safe")),
        )
        with self.assertRaises(MissingProtocolEffect):
            Finishing(box).box_end()
        self.assertEqual(calls, [])
        box.box_state.Tn_data["enable"] = 0
        self.assertIsNone(Finishing(box).box_end())
        self.assertEqual(calls, ["z", "safe"])
        calls.clear()
        box.last_err = "error"
        self.assertIsNone(Finishing(box).box_end())
        self.assertEqual(calls, [])

    def test_change_flush_preflights_before_heating(self):
        calls = []
        box = SimpleNamespace(host_guard=lambda *args: lambda: None,
                              boxcfg=SimpleNamespace(safe_pos_y=None),
                              box_state=SimpleNamespace(get_Tnn_content=lambda tnn: {"addr": 1}),
                              quickly_wait_heating=lambda temp: calls.append(temp))
        with self.assertRaisesRegex(ValueError, "safe_pos_y"):
            Finishing(box).material_change_flush(None, "T1A")
        self.assertEqual(calls, [])

    def test_end_device_change_during_cut_stops_before_retraction(self):
        epoch = [0]
        calls = []
        def host_guard(address=None):
            captured = epoch[0]
            def check():
                if address is not None and captured != epoch[0]:
                    raise RuntimeError("device changed")
            return check
        def cut(**kwargs):
            calls.append("cut")
            epoch[0] += 1
            return True
        box = SimpleNamespace(
            host_guard=host_guard, last_err=None, last_cmd="T1A",
            is_use_ending_material=False,
            box_state=SimpleNamespace(Tn_data={"enable": 1},
                                      get_Tnn_content=lambda tnn: {"addr": 1}),
            motion=SimpleNamespace(z_down=lambda **kw: calls.append("z"),
                                   move_to_safe_pos=lambda: calls.append("safe")),
            get_filament_sensor_detect=lambda: True, move_to_cut=cut,
            box_retrude_material=lambda **kw: calls.append("retract"),
        )
        with self.assertRaisesRegex(RuntimeError, "device changed"):
            Finishing(box).cmd_box_end(None)
        self.assertEqual(calls, ["z", "cut"])
        self.assertEqual(box.last_cmd, "T1A")

    def test_outer_end_guard_covers_safe_move(self):
        epoch = [0]
        def host_guard(address=None):
            captured = epoch[0]
            def check():
                if address and captured != epoch[0]:
                    raise RuntimeError("device changed")
            return check
        box = SimpleNamespace(
            host_guard=host_guard, last_err=None, last_cmd="T1A",
            is_use_ending_material=False,
            box_state=SimpleNamespace(Tn_data={"enable": 1},
                                      get_Tnn_content=lambda tnn: {"addr": 1}),
            motion=SimpleNamespace(move_to_safe_pos=lambda: epoch.__setitem__(0, 1)),
        )
        finishing = Finishing(box)
        finishing.box_end = lambda: None
        with self.assertRaisesRegex(RuntimeError, "device changed"):
            finishing.cmd_box_end(None)

    def test_end_late_ending_flag_stops_before_cut_and_retract(self):
        from cfs.box import MissingProtocolEffect
        calls = []
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, last_err=None, last_cmd="T1A",
            is_use_ending_material=False,
            box_state=SimpleNamespace(Tn_data={"enable": 1},
                                      get_Tnn_content=lambda tnn: {"addr": 1}),
            get_filament_sensor_detect=lambda: calls.append("head") or True,
            move_to_cut=lambda **kw: calls.append("cut"),
            box_retrude_material=lambda **kw: calls.append("retract"),
        )
        def z_down(**kwargs):
            calls.append("z")
            box.is_use_ending_material = True
        box.motion = SimpleNamespace(z_down=z_down)
        with self.assertRaises(MissingProtocolEffect):
            Finishing(box).box_end()
        self.assertEqual(calls, ["z"])
        self.assertEqual(box.last_cmd, "T1A")

    def test_change_flush_device_replacement_during_metadata_or_heat_stops(self):
        for changed_at in ("metadata", "heat"):
            with self.subTest(changed_at=changed_at):
                epochs = {1: 0, 2: 0}
                calls = []
                def host_guard(address=None):
                    captured = epochs.get(address)
                    def check():
                        if address is not None and epochs[address] != captured:
                            raise RuntimeError("device changed")
                    return check
                def heat(temp):
                    calls.append("heat")
                    if changed_at == "heat":
                        epochs[2] += 1
                box = SimpleNamespace(
                    host_guard=host_guard, boxcfg=SimpleNamespace(safe_pos_y=1),
                    box_state=SimpleNamespace(get_Tnn_content=lambda tnn: {
                        "addr": {"T1A": 1, "T2A": 2}[tnn]}),
                    get_flush_velocity=lambda *args: ([100], [1]),
                    get_flush_temp=lambda *args: (200, 210, 210),
                    quickly_wait_heating=heat,
                )
                finishing = Finishing(box)
                def metadata(*args):
                    if changed_at == "metadata":
                        epochs[1] += 1
                    return 5
                finishing.get_flush_len = metadata
                finishing.material_flush = lambda *args, **kwargs: calls.append("extrude")
                with self.assertRaisesRegex(RuntimeError, "device changed"):
                    finishing.material_change_flush("T1A", "T2A")
                self.assertNotIn("extrude", calls)
                if changed_at == "metadata":
                    self.assertNotIn("heat", calls)

    def test_material_flush_checks_target_after_extrusion(self):
        epoch = [0]
        calls = []
        def host_guard(address=None):
            captured = epoch[0]
            def check():
                if address is not None and captured != epoch[0]:
                    raise RuntimeError("device changed")
            return check
        def extrude(*args, **kwargs):
            epoch[0] += 1
            return True
        box = SimpleNamespace(
            host_guard=host_guard, boxcfg=SimpleNamespace(safe_pos_y=1),
            box_state=SimpleNamespace(get_Tnn_content=lambda tnn: {"addr": 1}),
            Tn_Extrude=extrude,
            gcode=SimpleNamespace(run_script_from_command=lambda script: calls.append(script)),
        )
        with self.assertRaisesRegex(RuntimeError, "device changed"):
            Finishing(box).material_flush(5, [100], 200, tnn="T1A")
        self.assertEqual(calls, [])
