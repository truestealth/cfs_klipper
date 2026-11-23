"""Exercise one actual registered tool change under passive admission."""

from copy import deepcopy
from pathlib import Path
import sys
import struct
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from cfs.native import UnsupportedRequest, build_request
from test_box_data import CommandError
from test_material_workflow import WorkflowFixture
from test_native import reply


class ToolChangeTrialTests(unittest.TestCase):
    def fixture(self, **kwargs):
        fixture = WorkflowFixture(loaded=True, passive=True).prepare_toolchange(**kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def run_change(self, fixture, tool='T1C'):
        return fixture.box.material_trial.run_change(tool, confirmed=True)

    def assert_released(self, fixture):
        self.assertTrue(fixture.lifecycle.passive)
        self.assertFalse(fixture.box.material_trial.active)
        self.assertFalse(fixture.box.operations.active)
        before = len(fixture.lines), len(fixture.native_events)
        with self.assertRaises(CommandError):
            fixture.command('T1B')
        with self.assertRaises(UnsupportedRequest):
            fixture.box.send_native(build_request(1, 'MEASURING_WHEEL', b'\x01'))
        self.assertEqual((len(fixture.lines), len(fixture.native_events)), before)

    def test_registered_physical_tool_executes_computed_purge_and_coordinated_change(self):
        fixture = self.fixture(volume=200.)
        original = deepcopy(fixture.box.box_state.Tnn_map)
        result = self.run_change(fixture)
        self.assertEqual((result['source'], result['target'], result['tool']), ('T1A', 'T1C', 'T1C'))
        self.assertEqual((result['mode'], result['material'], result['connections'], result['buffer']), (2, 15, 4, 0))
        self.assertEqual(fixture.box.t_command, 'T1C')
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertEqual(fixture.box.last_cmd_tmp, 'T1A')
        self.assertEqual(fixture.box.box_state.Tnn_map, original)
        pulls = [data for _, opcode, data in fixture.native_events if opcode == 0x11]
        self.assertEqual(pulls, [b'\x01\x00', b'\x01\x01'])
        stages = [data for _, opcode, data in fixture.native_events if opcode == 0x10]
        self.assertEqual(stages, [b'\x04\x00\x00', b'\x04\x04\x00',
                                  b'\x04\x06\x00', b'\x04\x05\x00'])
        self.assertTrue(any(opcode == 0x0E for _, opcode, _ in fixture.native_events))
        self.assertNotIn('BOX_MATERIAL_FLUSH LEN=150', [line for _, line in fixture.lines])
        self.assertEqual(fixture.box.material_trial._change_purge[3], (90., 80.))
        lines = [line for _, line in fixture.lines]
        self.assertIn('G0 E90.00 F360.00', lines)
        self.assertIn('G0 E80.00 F360.00', lines)
        self.assertIn('G0 E-30 F600', lines)
        submitted = next(index for index, event in enumerate(fixture.events) if event[0] == 'submit')
        material = fixture.events.index(('native', 0x11, b'\x01\x01'))
        self.assertLess(submitted, material)
        self.assertEqual(fixture.events[submitted][1][3], fixture.events[submitted][2][3] - 30)
        self.assertFalse(any(event == ('gcode', 'M400') for event in fixture.events[submitted + 1:material]))
        self.assertEqual(fixture.events[material + 1], ('gcode', 'M400'))
        self.assertEqual(lines[-3:], ['G0 F300.00', 'M204 S1500', 'M400'])
        self.assertEqual(fixture.e_origin, fixture.printer.position[3])
        self.assertEqual(fixture.completed, fixture.printer.position)
        self.assertEqual(fixture.printer.objects['output_pin fan0'].last_value, .25)
        self.assertIsNone(fixture.box.move_z)
        self.assertFalse(fixture.box.cut_sensor.cut_present)
        self.assertTrue(any(row[1].get('tn_cur_tnn') == 'T1C' for row in fixture.hints))
        self.assertTrue(any(row[1].get('tn_move_z') == 0 for row in fixture.hints))
        self.assertEqual(fixture.script_errors, [])
        self.assert_released(fixture)

    def test_two_changes_keep_actual_pose_e_origin_and_reverse_source(self):
        fixture = self.fixture()
        first = self.run_change(fixture, 'T1C')
        first_pose = list(fixture.printer.position)
        first_origin = fixture.e_origin
        second = self.run_change(fixture, 'T0')
        self.assertEqual((first['source'], first['target']), ('T1A', 'T1C'))
        self.assertEqual((second['source'], second['target']), ('T1C', 'T1A'))
        self.assertEqual([row[2] for row in fixture.native_events if row[1] == 0x11],
                         [b'\x01\x00', b'\x01\x01', b'\x04\x00', b'\x04\x01'])
        self.assertAlmostEqual(first_pose[2], 30.8)
        self.assertAlmostEqual(fixture.printer.position[2], 31.6)
        self.assertGreater(fixture.e_origin, first_origin)
        self.assertEqual(fixture.e_origin, fixture.printer.position[3])
        self.assertEqual(fixture.completed, fixture.printer.position)
        self.assertIn(((255, 0, 0), (0, 0, 255)), fixture.colour_pairs)
        self.assertIn(((0, 0, 255), (255, 0, 0)), fixture.colour_pairs)
        self.assert_released(fixture)

    def test_full_buffer_after_purge_allows_fresh_flush_and_final_loaded_results(self):
        fixture = self.fixture()
        flush = fixture.box.material_change_flush
        observed = []
        def fill_after_flush(*args, **kwargs):
            result = flush(*args, **kwargs)
            fixture.buffer = 1
            return result
        def record_buffer(opcode, data):
            trial = fixture.box.material_trial
            if opcode == 0x05 and trial._phase == 'after':
                observed.append((trial._change_phase, fixture.buffer))
        fixture.box.material_change_flush = fill_after_flush
        fixture.on_native = record_buffer
        result = self.run_change(fixture)
        self.assertEqual(observed, [('UNLOAD', 2), ('LOAD', 0), ('FLUSH', 1), (None, 1)])
        self.assertEqual((result['mode'], result['connections'], result['buffer']), (2, 4, 1))
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertIn('G92 E0', [line for _, line in fixture.lines])
        self.assertIsNone(fixture.box.move_z)
        self.assertFalse(fixture.box.cut_sensor.cut_present)
        self.assertEqual(fixture.e_origin, fixture.printer.position[3])
        self.assertEqual(fixture.completed, fixture.printer.position)
        self.assert_released(fixture)

    def test_full_buffer_at_load_result_still_stops_before_purge_and_final_tail(self):
        fixture = self.fixture()
        def fill_at_load(opcode, data):
            trial = fixture.box.material_trial
            if opcode == 0x05 and trial._phase == 'after' and trial._change_phase == 'LOAD':
                fixture.buffer = 1
        fixture.on_native = fill_at_load
        with self.assertRaisesRegex(RuntimeError, 'phase result'):
            self.run_change(fixture)
        self.assertFalse(any(opcode == 0x0E for _, opcode, _ in fixture.native_events))
        self.assertNotIn('G92 E0', [line for _, line in fixture.lines])
        self.assert_released(fixture)

    def test_unload_result_still_requires_empty_buffer_before_loading_target(self):
        for buffer in (0, 1):
            with self.subTest(buffer=buffer):
                fixture = self.fixture()
                def change_buffer(opcode, data):
                    trial = fixture.box.material_trial
                    if opcode == 0x05 and trial._phase == 'after' and trial._change_phase == 'UNLOAD':
                        fixture.buffer = buffer
                fixture.on_native = change_buffer
                with self.assertRaisesRegex(RuntimeError, 'phase result'):
                    self.run_change(fixture)
                self.assertFalse(any(opcode == 0x10 for _, opcode, _ in fixture.native_events))
                self.assert_released(fixture)

    def test_full_postflush_buffer_does_not_relax_other_observations_or_freshness(self):
        for case in ('mode', 'material', 'connections', 'head', 'source', 'stale', 'status'):
            with self.subTest(case=case):
                fixture = self.fixture()
                flush = fixture.box.material_change_flush
                send = fixture.box.send_native
                def fill_and_change(*args, **kwargs):
                    result = flush(*args, **kwargs)
                    fixture.buffer = 1
                    if case == 'mode': fixture.native.mode = 0
                    elif case == 'material': fixture.material = 1
                    elif case == 'connections': fixture.connections = 1
                    elif case == 'head': fixture.set_head(False)
                    elif case == 'source': fixture.box.last_cmd = 'T1A'
                    return result
                def observed(request, *args, **kwargs):
                    trial = fixture.box.material_trial
                    if (request.command == 0x05 and trial._phase == 'after'
                            and trial._change_phase == 'FLUSH'):
                        if case == 'stale':
                            return True
                        if case == 'status':
                            fixture.faults[(0x05, b'')] = 2
                    return send(request, *args, **kwargs)
                fixture.box.material_change_flush = fill_and_change
                fixture.box.send_native = observed
                with self.assertRaises(RuntimeError):
                    self.run_change(fixture)
                self.assertNotIn('G92 E0', [line for _, line in fixture.lines])
                self.assert_released(fixture)

    def test_logical_tool_uses_both_live_maps_once(self):
        fixture = self.fixture()
        state = fixture.box.box_state
        state.Tn_map['T7'] = 'T1B'
        state.Tnn_map['T1B'] = 'T1C'
        result = self.run_change(fixture, 'T7')
        self.assertEqual((result['tool'], result['target']), ('T7', 'T1C'))
        self.assertEqual(fixture.box.t_command, 'T7')
        self.assertEqual([row[1]['tn_cur_tnn'] for row in fixture.hints if 'tn_cur_tnn' in row[1]], ['T1B'])
        self.assert_released(fixture)

    def test_extension_command_requires_confirmation_and_uses_registered_virtual_tool(self):
        fixture = self.fixture()
        with self.assertRaises(CommandError):
            fixture.command('CFS_TEST_TOOL_CHANGE TOOL=T2')
        self.assertEqual(fixture.lines, [])
        self.assertEqual(fixture.native_events, [])
        result = fixture.command('CFS_TEST_TOOL_CHANGE TOOL=T2 CONFIRM=1')
        self.assertEqual((result['tool'], result['source'], result['target']), ('T2', 'T1A', 'T1C'))
        self.assert_released(fixture)

    def test_invalid_confirmation_source_and_target_fail_before_effects(self):
        cases = ('confirmation', 'unknown_tool', 'same_source', 'absent_source', 'cross_hub', 'source_hint')
        for case in cases:
            with self.subTest(case=case):
                fixture = self.fixture()
                tool, confirmed = 'T1C', True
                if case == 'confirmation': confirmed = False
                elif case == 'unknown_tool': tool = 'T16'
                elif case == 'same_source': tool = 'T1A'
                elif case == 'absent_source': fixture.box.last_cmd = None
                elif case == 'cross_hub': tool = 'T2C'
                elif case == 'source_hint': fixture.box.update_filament_pos('T1A', None)
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.box.material_trial.run_change(tool, confirmed=confirmed)
                self.assertEqual(fixture.lines, [])
                self.assertEqual(fixture.native_events, [])
                self.assert_released(fixture)

    def test_homing_geometry_scale_and_missing_submission_fail_before_effects(self):
        for case in ('homing', 'rear_y', 'range', 'scale', 'submission', 'paused', 'printing'):
            with self.subTest(case=case):
                fixture = self.fixture()
                if case == 'homing': fixture.base.homed_axes = 'xy'
                elif case == 'rear_y': fixture.printer.position[1] = 181
                elif case == 'range': fixture.box.boxcfg.extrude_pos_x = 10000
                elif case == 'scale': fixture.base.factors['extrude_factor'] = .5
                elif case == 'submission': fixture.printer.objects['toolhead'].flush_step_generation = None
                elif case == 'paused': fixture.printer.objects['pause_resume'].is_paused = True
                elif case == 'printing': fixture.box.print_stats.state = 'printing'
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_change(fixture)
                self.assertEqual(fixture.lines, [])
                self.assertEqual(fixture.native_events, [])
                self.assert_released(fixture)

    def test_bad_computed_purge_is_rejected_before_cutting(self):
        for volume in (float('nan'), float('inf'), -1000., 100000.):
            with self.subTest(volume=volume):
                fixture = self.fixture(volume=volume)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_change(fixture)
                self.assertEqual(fixture.lines, [])
                self.assertEqual(fixture.native_events, [])
                self.assert_released(fixture)

    def test_bad_segment_profile_is_rejected_before_cutting(self):
        for value in (0, .001, float('nan')):
            with self.subTest(value=value):
                fixture = self.fixture()
                fixture.box.boxcfg.box_need_clean_length = value
                with self.assertRaises(ValueError):
                    self.run_change(fixture)
                self.assertEqual(fixture.lines, [])
                self.assertEqual(fixture.native_events, [])
                self.assert_released(fixture)

    def test_fresh_selected_material_and_connection_preconditions(self):
        for material, connections in ((1, 1), (15, 0), (15, 5)):
            with self.subTest(material=material, connections=connections):
                fixture = self.fixture()
                fixture.material, fixture.connections = material, connections
                with self.assertRaises(RuntimeError):
                    self.run_change(fixture)
                self.assertEqual(fixture.lines, [])
                self.assertTrue(all(opcode in (0x0A, 0x08, 0x05) for _, opcode, _ in fixture.native_events))
                self.assert_released(fixture)

    def test_cutter_failure_never_enters_cfs_retraction(self):
        fixture = self.fixture()
        fixture.cut_callback = False
        with self.assertRaises(RuntimeError):
            self.run_change(fixture)
        self.assertFalse(any(opcode in (0x10, 0x11) for _, opcode, _ in fixture.native_events))
        self.assertEqual(fixture.box.last_cmd, 'T1A')
        self.assert_released(fixture)

    def test_unload_errors_stop_before_target_loading_and_return(self):
        for trigger, status in ((b'\x01\x00', 19), (b'\x01\x01', 25)):
            with self.subTest(trigger=trigger):
                fixture = self.fixture()
                fixture.faults[(0x11, trigger)] = status
                with self.assertRaises(RuntimeError):
                    self.run_change(fixture)
                pulls = [data for _, opcode, data in fixture.native_events if opcode == 0x11]
                self.assertEqual(pulls, [b'\x01\x00'] if trigger[1] == 0 else [b'\x01\x00', b'\x01\x01'])
                self.assertFalse(any(opcode == 0x10 for _, opcode, _ in fixture.native_events))
                self.assertEqual(fixture.box.last_cmd, 'T1A')
                lines = [line for _, line in fixture.lines]
                self.assertEqual('G0 E-30 F600' in lines, trigger[1] == 1)
                self.assertNotIn('G92 E0', lines)
                self.assertEqual(fixture.printer.position[:2], [40, 205])
                self.assert_released(fixture)

    def test_head_or_unload_postcondition_failure_cannot_start_target(self):
        fixture = self.fixture()
        fixture.clear_head = False
        with self.assertRaises(RuntimeError):
            self.run_change(fixture)
        self.assertFalse(any(opcode == 0x10 for _, opcode, _ in fixture.native_events))
        self.assert_released(fixture)

    def test_load_and_head_failures_leave_no_flush_or_second_cut(self):
        for case in ('stage4', 'head', 'stage6'):
            with self.subTest(case=case):
                fixture = self.fixture()
                if case == 'stage4': fixture.faults[(0x10, b'\x04\x04\x00')] = 2
                elif case == 'head': fixture.head_callback = False
                else: fixture.faults[(0x10, b'\x04\x06\x00')] = 2
                with self.assertRaises(RuntimeError):
                    self.run_change(fixture)
                self.assertFalse(any(opcode == 0x0E for _, opcode, _ in fixture.native_events))
                self.assertEqual(len([data for _, opcode, data in fixture.native_events if opcode == 0x11]), 2)
                self.assert_released(fixture)

    def test_flush_error_does_not_enter_the_coordinators_retry_loop(self):
        fixture = self.fixture()
        fixture.faults[(0x0E, b'\x01')] = 2
        with self.assertRaises(RuntimeError):
            self.run_change(fixture)
        self.assertEqual(len([row for row in fixture.native_events if row[1] == 0x0E]), 1)
        self.assertNotIn('BOX_MOVE_TO_SAFE_POS', [line for _, line in fixture.lines])
        self.assertNotIn('G92 E0', [line for _, line in fixture.lines])
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assert_released(fixture)

    def test_malformed_stage_and_wheel_replies_stop_before_following_effects(self):
        for opcode, data, payload in ((0x10, b'\x04\x00\x00', b''),
                                      (0x0E, b'\x01', b''),
                                      (0x0E, b'\x01', struct.pack('>f', float('nan')))):
            with self.subTest(opcode=opcode, payload=payload):
                fixture = self.fixture()
                exchange = fixture.native.cmd_send_data_with_response
                def malformed(body, timeout, *args):
                    result = exchange(body, timeout, *args)
                    return reply(body[0], opcode, data=payload) if body[3:] == bytes((opcode,)) + data else result
                fixture.native.cmd_send_data_with_response = malformed
                with self.assertRaises(RuntimeError):
                    self.run_change(fixture)
                if opcode == 0x10:
                    self.assertEqual([row[2] for row in fixture.native_events if row[1] == 0x10], [data])
                else:
                    self.assertEqual(len([row for row in fixture.native_events if row[1] == 0x0E]), 1)
                self.assertNotIn('G92 E0', [line for _, line in fixture.lines])
                self.assert_released(fixture)

    def test_separate_head_dispatch_owns_only_one_target_stage6(self):
        fixture = self.fixture()
        fixture.head_callback = False
        exchange = fixture.native.cmd_send_data_with_response
        stage6_owners = []
        def separate_head(body, timeout, *args):
            if body[3:] == b'\x10\x04\x06\x00':
                stage6_owners.append(fixture.base.dispatch)
                with self.assertRaises(UnsupportedRequest):
                    fixture.box.send_native(build_request(1, 'MEASURING_WHEEL', b'\x01'))
            result = exchange(body, timeout, *args)
            if body[3:] == b'\x10\x04\x04\x00':
                owner = fixture.base.dispatch
                fixture.base.dispatch = object()
                try:
                    fixture.set_head(True)
                    fixture.buffer = 0
                    fixture.printer.send_event('box:extrude_process_stage7')
                    fixture.printer.send_event('box:extrude_process_stage7')
                finally:
                    fixture.base.dispatch = owner
            return result
        fixture.native.cmd_send_data_with_response = separate_head
        owner = fixture.base.dispatch
        self.run_change(fixture)
        self.assertEqual(len(stage6_owners), 1)
        self.assertIsNot(stage6_owners[0], owner)
        self.assert_released(fixture)

    def test_changed_mapping_or_material_rejects_following_effects(self):
        for case in ('mapping', 'material', 'scale', 'binding'):
            with self.subTest(case=case):
                fixture = self.fixture()
                changed = []
                def mutate(opcode, data):
                    if changed or opcode != 0x11:
                        return
                    changed.append(True)
                    if case == 'mapping': fixture.box.box_state.Tnn_map['T1C'] = 'T1B'
                    elif case == 'material': fixture.box.box_state.modify_Tn_data(1, '000FF00', 'color_value', 'C')
                    elif case == 'scale': fixture.base.factors['speed_factor'] = .5
                    else: fixture.box._binding_generation += 1
                fixture.on_native = mutate
                with self.assertRaises(RuntimeError):
                    self.run_change(fixture)
                self.assertEqual(len([row for row in fixture.native_events if row[1] == 0x11]), 1)
                self.assertFalse(any(opcode == 0x10 for _, opcode, _ in fixture.native_events))
                self.assert_released(fixture)

    def test_unrelated_and_late_head_callbacks_cannot_borrow_permission(self):
        fixture = self.fixture()
        observed = []
        def other_command(opcode, data):
            if opcode == 0x11 and not observed:
                before = len(fixture.lines)
                with self.assertRaises(CommandError):
                    fixture.command('T1B')
                observed.append(len(fixture.lines) == before)
        fixture.on_native = other_command
        self.run_change(fixture)
        before = len(fixture.lines), len(fixture.native_events)
        fixture.printer.send_event('box:extrude_process_stage7')
        self.assertEqual((len(fixture.lines), len(fixture.native_events)), before)
        self.assertEqual(observed, [True])
        self.assert_released(fixture)


if __name__ == '__main__':
    unittest.main()
