"""Synthetic host extrusion, purge calculations and result-boundary checks."""

from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from cfs.extrusion import Extrusion


class ExtrusionTests(unittest.TestCase):
    def test_heat_repeats_until_strict_temperature_window(self):
        calls = []
        heater = SimpleNamespace(smoothed_temp=198)
        reactor = SimpleNamespace(monotonic=lambda: 12)
        def pause(deadline):
            calls.append(("pause", deadline))
            heater.smoothed_temp = 199
        reactor.pause = pause
        box = SimpleNamespace(
            gcode=SimpleNamespace(run_script_from_command=lambda command: calls.append(command)),
            toolhead=SimpleNamespace(extruder=SimpleNamespace(get_heater=lambda: heater)),
            lifecycle=SimpleNamespace(reactor=reactor, is_shutdown=False),
            host_guard=lambda: lambda: None)
        self.assertIsNone(Extrusion(box).quickly_wait_heating(200))
        self.assertEqual(calls, ["M104 S200", ("pause", 13), "M104 S200"])

    def test_shutdown_still_sets_target_once(self):
        calls = []
        box = SimpleNamespace(
            gcode=SimpleNamespace(run_script_from_command=calls.append),
            toolhead=SimpleNamespace(extruder=SimpleNamespace(
                get_heater=lambda: SimpleNamespace(smoothed_temp=20))),
            lifecycle=SimpleNamespace(is_shutdown=True), host_guard=lambda: lambda: None)
        Extrusion(box).quickly_wait_heating(200)
        self.assertEqual(calls, ["M104 S200"])

    def test_configured_wrapper_forwards_and_discards_result(self):
        calls = []
        cfg = SimpleNamespace(Tn_extrude=20, Tn_extrude_velocity=(30, 40),
                              Tn_extrude_temp=210, Tn_extrude_percent=(1, 2))
        box = SimpleNamespace(boxcfg=cfg, host_guard=lambda: lambda: None, Tn_Extrude=lambda *args: calls.append(args) or False)
        self.assertIsNone(Extrusion(box).cmd_Tn_Extrude(object()))
        self.assertEqual(calls, [(20, (30, 40), 210, (1, 2))])


class ProfileTests(unittest.TestCase):
    def test_speed_profiles_and_forwarding(self):
        calls, messages = [], []
        speeds = {"a": 2, "b": 3, "unknown": None}
        cfg = SimpleNamespace(Tn_extrude_velocity=(31, 42), Tn_extrude_percent=(1, 2))
        box = SimpleNamespace(boxcfg=cfg, host_guard=lambda: lambda: None, gcode=SimpleNamespace(respond_info=messages.append),
                              get_material_max_extrusion_speed=lambda t: calls.append(t) or speeds[t])
        action = Extrusion(box)
        self.assertEqual(action.get_flush_velocity("a", "b"), ([180], [1]))
        self.assertEqual(messages, ["num: 0, velocity: 180.000000, percent 1.000000"])
        self.assertEqual(action.get_flush_velocity("unknown", "b"), ((31, 42), (1, 2)))
        self.assertIsNone(action.cmd_get_flush_velocity_test({"LAST": "a", "NEXT": "b"}))
        self.assertEqual(calls, ["a", "b", "unknown", "b", "a", "b"])

    def test_temperature_profile(self):
        messages = []
        box = SimpleNamespace(host_guard=lambda: lambda: None, get_material_target_temp={"a": 220, "b": 200}.get,
                              gcode=SimpleNamespace(respond_info=messages.append))
        self.assertEqual(Extrusion(box).get_flush_temp("a", "b"), (220, 200, 220))
        self.assertEqual(messages, ["flush_temp: 220"])




class ColorPurgeTests(unittest.TestCase):
    def test_native_volume_and_conversion(self):
        class RGB:
            def __getitem__(self, index):
                return (self.r, self.g, self.b)
        events = []
        box = SimpleNamespace(
            host_guard=lambda: lambda: events.append('guard'),
            ffi_main=SimpleNamespace(new=lambda kind: RGB()),
            ffi_lib=SimpleNamespace(get_flushing_volume=lambda a, b: events.append((a, b)) or 100),
            boxcfg=SimpleNamespace(nozzle_volume=20, flush_multiplier=2),
            gcode=SimpleNamespace(respond_info=events.append))
        length = Extrusion(box).get_flush_len((1, 2, 3), (4, 5, 6))
        import math
        self.assertAlmostEqual(length, 240 / (0.765625 * math.pi))
        self.assertIn(((1, 2, 3), (4, 5, 6)), events)
        self.assertIn('flush_volume: 100, nozzle_volume: 20', events)

    def test_invalid_color_skips_native_call(self):
        rgb = SimpleNamespace()
        box = SimpleNamespace(host_guard=lambda: lambda: None,
                              ffi_main=SimpleNamespace(new=lambda kind: rgb))
        self.assertIsNone(Extrusion(box).get_flush_len((1,), (1, 2, 3)))


class ColorCommandTests(unittest.TestCase):
    def test_color_arguments_and_short_input(self):
        messages, calls = [], []
        box = SimpleNamespace(host_guard=lambda: lambda: None,
                              gcode=SimpleNamespace(respond_info=messages.append))
        action = Extrusion(box)
        action.get_flush_len = lambda *args: calls.append(args)
        cmd = SimpleNamespace(get_command_parameters=lambda: {"SCV": "1020ff", "TCV": "a0b0c0"})
        self.assertIsNone(action.cmd_get_flush_len(cmd))
        self.assertEqual(calls, [([16, 32, 255], [160, 176, 192])])
        self.assertEqual(messages, ['scv: [16, 32, 255]', 'tcv: [160, 176, 192]'])
        cmd.get_command_parameters = lambda: {"SCV": "bad"}
        action.cmd_get_flush_len(cmd)
        self.assertEqual(messages[-1], 'warning, the length of "SCV" is not 6')
        self.assertEqual(len(calls), 1)


class ArrayCommandTests(unittest.TestCase):
    def test_array_parsing_and_short_matrix_failure(self):
        messages = []
        box = SimpleNamespace(host_guard=lambda: lambda: None,
                              gcode=SimpleNamespace(respond_info=messages.append))
        action = Extrusion(box)
        cmd = SimpleNamespace(get_command_parameters=lambda: {"FLUSH_ARRAY": "1,,2,3,4_5,6,7,8_9,10,11,12_13,14,15,16"})
        self.assertIsNone(action.cmd_generate_flush_array(cmd))
        self.assertEqual(messages[1], "flush_array: [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13, 14, 15, 16]]")
        cmd.get_command_parameters = lambda: {"FLUSH_ARRAY": "1,2"}
        with self.assertRaises(IndexError):
            action.cmd_generate_flush_array(cmd)


class MaterialFlushTests(unittest.TestCase):
    def test_configured_flush_order_and_coordinate_restore(self):
        calls = []
        cfg = SimpleNamespace(Tn_retrude=-20, Tn_extrude_velocity=(40,),
                              Tn_extrude_temp=220, Tn_extrude_percent=(1,),
                              extrude_pos_x=1, extrude_pos_y=2, safe_pos_y=3)
        move = SimpleNamespace(absolute_coord=True)
        def script(value):
            calls.append(value)
            move.absolute_coord = False
        box = SimpleNamespace(boxcfg=cfg, host_guard=lambda: lambda: None,
                              if_in_resume=lambda: False, gcode_move=move,
                              Tn_Extrude=lambda *a, **kw: calls.append((a, kw)) or False,
                              nozzle_clean=lambda: calls.append('clean'),
                              gcode=SimpleNamespace(run_script_from_command=script, respond_info=calls.append))
        cmd = SimpleNamespace(get_int=lambda *a, **kw: calls.append((a, kw)) or 30,
                              get_command_parameters=lambda: {"VELOCITY": "10,20", "TEMP": "210", "PERCENT": "1,2"})
        Extrusion(box).cmd_material_flush(cmd)
        self.assertEqual(calls, [(('LEN', -20), {'minval': 0, 'maxval': 200}),
                                 ((30, [10, 20], 210.0, [1, 2]), {'tnn': None, 'error_report': True}),
                                 'clean', 'G91\n G0 E-5 F600\n M400'])
        self.assertTrue(move.absolute_coord)

    def test_resume_skips_flush(self):
        box = SimpleNamespace(host_guard=lambda: lambda: None, if_in_resume=lambda: True)
        self.assertIsNone(Extrusion(box).cmd_material_flush(object()))


class ExtrusionActionTests(unittest.TestCase):
    def make_action(self, readings=()):
        events = []
        readings = iter(readings)
        cfg = SimpleNamespace(buffer_empty_len=10, extrude_pos_x=12, extrude_pos_y=34, safe_pos_y=30, diff_length=-2)
        box = SimpleNamespace(
            boxcfg=cfg, host_guard=lambda: lambda: None,
            toolhead=SimpleNamespace(extruder=SimpleNamespace(get_heater=lambda: SimpleNamespace(can_extrude=True))),
            print_stats=SimpleNamespace(state='printing'),
            gcode=SimpleNamespace(run_script_from_command=events.append),
            box_state=SimpleNamespace(get_Tnn_content=lambda t: {'addr': 1}),
            read_measuring_wheel=lambda address: next(readings),
            check_and_extrude=lambda length, speed: events.append(('feed', length, speed)),
            cal_flush_list=lambda length, output: output.extend([15, 15]),
            nozzle_clean=lambda: events.append('clean'),
            lifecycle=SimpleNamespace(report_error=lambda *args: events.append(('error', args))),
            error=None, is_use_ending_material=False, start_print_retraction_distance=4)
        return Extrusion(box), box, events

    def test_short_feed_and_failure_preserve_mode(self):
        action, box, events = self.make_action()
        self.assertTrue(action.Tn_Extrude(8, [60], 220, [2], tnn='T0'))
        self.assertEqual(events, ['G91', ('feed', 16, 60), 'G90', 'M400'])
        box.check_and_extrude = lambda *args: False
        events.clear()
        self.assertFalse(action.Tn_Extrude(8, [60], 220, [1]))
        self.assertEqual(events, ['G91'])

    def test_long_feed_and_reset_before_retract(self):
        action, box, events = self.make_action([(True, 100), (True, 95), (True, 90)])
        self.assertTrue(action.Tn_Extrude(30, [60], 220, [1], tnn='T0'))
        self.assertEqual(events.count('clean'), 2)
        self.assertIn('G91\n G1 E-0 F4800\n G90', events)
        self.assertEqual(box.start_print_retraction_distance, 0)
        self.assertEqual(events[-2:], ['G90', 'M400'])

    def test_fresh_wheel_failure_and_blocked_error(self):
        action, box, events = self.make_action([(False, None)])
        self.assertFalse(action.Tn_Extrude(30, [60], 220, [1], tnn='T0'))
        self.assertEqual(events, ['G91'])
        action, box, events = self.make_action([(True, 100), (False, None)])
        self.assertFalse(action.Tn_Extrude(30, [60], 220, [1], tnn='T0'))
        self.assertFalse(any(isinstance(item, tuple) and item[0] == 'error' for item in events))
        action, box, events = self.make_action([(True, 100), (True, 100), (True, 100)])
        self.assertFalse(action.Tn_Extrude(30, [60], 220, [1], tnn='T0'))
        self.assertIn(('error', ('key_nozzle_blocked_err', {})), events)

    def test_multi_profile_threshold_and_unsampled_tail(self):
        action, box, events = self.make_action([(True, 100), (True, 95)])
        self.assertTrue(action.Tn_Extrude(20, [60, 90, 120], 220, [.5, .5, .2], tnn='T0'))
        self.assertEqual(events, ['G91', 'G0 E10.00 F60.00', 'M400',
                                  'G0 E10.00 F90.00', 'M400',
                                  'G0 E4.00 F120.00', 'M400', 'G90', 'M400'])
        action, box, events = self.make_action()
        self.assertTrue(action.Tn_Extrude(10, [60, 90], 220, [.5, .5]))
        self.assertEqual(events, ['G91', 'G0 E5.00 F60.00', 'M400',
                                  'G0 E5.00 F90.00', 'M400', 'G90', 'M400'])

    def test_heat_and_pause_paths(self):
        action, box, events = self.make_action()
        box.toolhead.extruder.get_heater = lambda: SimpleNamespace(can_extrude=False)
        action.Tn_Extrude(1, [60], 220, [1])
        self.assertEqual(events[:2], ['M109 S220.00', 'G91'])
        events.clear()
        box.toolhead.extruder.get_heater = lambda: SimpleNamespace(can_extrude=True)
        box.print_stats.state = 'pause'
        action.Tn_Extrude(1, [60], 220, [1])
        self.assertEqual(events[:2], ['M104 S220.00', 'G91'])


class GuardTests(unittest.TestCase):
    def test_action_stops_after_reentrant_gcode_invalidates_guard(self):
        action, box, events = ExtrusionActionTests().make_action()
        valid = [True]
        def guard():
            if not valid[0]:
                raise RuntimeError('stale')
        box.host_guard = lambda: guard
        def script(command):
            events.append(command)
            valid[0] = False
        box.gcode.run_script_from_command = script
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            action.Tn_Extrude(8, [60], 220, [1])
        self.assertEqual(events, ['G91'])


class SegmentationTests(unittest.TestCase):
    def test_segment_tail_branches(self):
        cfg = SimpleNamespace(box_first_clean_length=90, box_need_clean_length=80,
                              box_need_clean_length_max=100)
        action = Extrusion(SimpleNamespace(boxcfg=cfg))
        for length, expected in ((20, [90]), (95, [95]), (110, [90, 80]),
                                 (180, [90, 90]), (250, [90, 80, 80]),
                                 (270, [90, 80, 100]), (275, [100, 87.5, 87.5])):
            segments = []
            self.assertIsNone(action.cal_flush_list(length, segments))
            self.assertEqual(segments, expected)

    def test_zero_segment_size_rejects_nonterminating_branch(self):
        cfg = SimpleNamespace(box_first_clean_length=90, box_need_clean_length=0,
                              box_need_clean_length_max=100)
        with self.assertRaises(ValueError):
            Extrusion(SimpleNamespace(boxcfg=cfg)).cal_flush_list(101, [])


class FlushFallbackTests(unittest.TestCase):
    def test_bad_profile_values_use_configured_values(self):
        calls = []
        cfg = SimpleNamespace(Tn_retrude=-20, Tn_extrude_velocity=(50,),
                              Tn_extrude_temp=220, Tn_extrude_percent=(1,),
                              extrude_pos_x=1, extrude_pos_y=2, safe_pos_y=3)
        box = SimpleNamespace(
            boxcfg=cfg, host_guard=lambda: lambda: None, if_in_resume=lambda: False,
            gcode=SimpleNamespace(respond_info=calls.append, run_script_from_command=calls.append),
            gcode_move=SimpleNamespace(absolute_coord=True),
            Tn_Extrude=lambda *args, **kwargs: calls.append((args, kwargs)),
            nozzle_clean=lambda: None)
        cmd = SimpleNamespace(get_int=lambda *args, **kwargs: 40,
                              get_command_parameters=lambda: {'VELOCITY': 'bad', 'TEMP': '300', 'PERCENT': 'oops'})
        Extrusion(box).cmd_material_flush(cmd)
        self.assertIn(((40, (50,), 220, (1,)), {'tnn': None, 'error_report': True}), calls)
        self.assertEqual(calls[:2], ['warning: get velocity failed, velocity: None',
                                     'warning: get percent failed, percent: None'])


class PositionPreflightTests(unittest.TestCase):
    def test_missing_tray_position_stops_before_gcode(self):
        action, box, events = ExtrusionActionTests().make_action()
        del box.boxcfg.extrude_pos_x
        with self.assertRaisesRegex(ValueError, "extrude_pos_x"):
            action.Tn_Extrude(30, [60], 220, [1], tnn='T0')
        self.assertEqual(events, [])


class BoxIntegrationTests(unittest.TestCase):
    def test_short_action_uses_real_box_extrusion_check(self):
        import tempfile
        from cfs.box import Box
        from test_configuration import FakeConfig
        events = []
        with tempfile.TemporaryDirectory() as directory:
            box = Box(FakeConfig({'bus': 'custom_bus', 'version': 1}), directory)
            box.lifecycle = SimpleNamespace(running=True, _report_guard=lambda: lambda: None)
            box.gcode = SimpleNamespace(run_script_from_command=events.append)
            box.toolhead = SimpleNamespace(extruder=SimpleNamespace(
                get_heater=lambda: SimpleNamespace(can_extrude=True)))
            box.print_stats = SimpleNamespace(state='printing')
            self.assertTrue(Extrusion(box).Tn_Extrude(12, [60], 220, [1]))
            self.assertEqual(events, ['G91', 'G91', 'G0 E12.00 F60.00', 'G90',
                                      'M400', 'G90', 'M400'])
            box.box_state.extrude_err = True
            events.clear()
            self.assertFalse(Extrusion(box).Tn_Extrude(12, [60], 220, [1]))
            self.assertEqual(events, ['G91'])


class CleanPreflightTests(unittest.TestCase):
    def test_segmented_feed_requires_safe_y_before_heating(self):
        action, box, events = ExtrusionActionTests().make_action()
        del box.boxcfg.safe_pos_y
        with self.assertRaisesRegex(ValueError, 'safe_pos_y'):
            action.Tn_Extrude(30, [60], 220, [1], tnn='T0')
        self.assertEqual(events, [])

    def test_material_flush_requires_clean_positions_before_feed(self):
        calls = []
        cfg = SimpleNamespace(Tn_retrude=-20, Tn_extrude_velocity=(50,),
                              Tn_extrude_temp=220, Tn_extrude_percent=(1,))
        box = SimpleNamespace(boxcfg=cfg, host_guard=lambda: lambda: None,
                              if_in_resume=lambda: False,
                              gcode=SimpleNamespace(respond_info=calls.append),
                              Tn_Extrude=lambda *a, **kw: calls.append('feed'))
        cmd = SimpleNamespace(get_int=lambda *a, **kw: 40,
                              get_command_parameters=lambda: {'VELOCITY': '50', 'TEMP': '220', 'PERCENT': '1'})
        with self.assertRaisesRegex(ValueError, 'safe_pos_y'):
            Extrusion(box).cmd_material_flush(cmd)
        self.assertEqual(calls, [])
        box.if_in_resume = lambda: True
        self.assertIsNone(Extrusion(box).cmd_material_flush(object()))


if __name__ == '__main__':
    unittest.main()
