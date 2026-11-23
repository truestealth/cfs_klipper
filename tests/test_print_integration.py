"""Compose print admission with actual Box, lifecycle, decoder and tool handlers."""

from copy import deepcopy
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from cfs.lifecycle import LifecycleStopped
from cfs.native import UnsupportedRequest, build_request
from test_box_data import CommandError
from test_host_actions import HostCommand
from test_material_workflow import WorkflowFixture
from test_native import reply


class PrintIntegrationFixture(WorkflowFixture):
    """Keep file/worker/thermal observations external; use production CFS objects."""

    def __init__(self, *, loaded=True, passive=True):
        super().__init__(loaded=loaded, passive=passive)
        self.prepare_toolchange()
        if not passive:
            self.base.homed_axes = 'xyz'
            self.box.gcode_move.get_status = lambda now: {'speed_factor': 1., 'extrude_factor': 1.}
        self.printer.objects['toolhead'].get_status = lambda now: {
            'homed_axes': self.base.homed_axes, 'position': list(self.printer.position)}
        root = self.base.root if passive else self.base.config_path.parent
        self.path = root / 'approved.gcode'
        self.path.write_text('START_PRINT\nT0\nT2\nT0\nEND_PRINT\n', encoding='utf-8')
        self.configuration = {'config': {'box': {'version': '1'}},
                              'settings': {'box': {'version': 1}}}
        self.printer.objects['configfile'] = SimpleNamespace(
            get_status=lambda now: deepcopy(self.configuration))
        sd = self.box.virtual_sdcard
        sd.sdcard_dirname = str(root)
        sd.current_file = None
        sd.work_handler_dispatch = None
        sd.cmd_from_sd = False
        sd.must_pause_work = False
        sd.first_tnn_finished = True
        sd.gcode_metadata = {'metadata': {'slicer': 'Creality', 'slicer_version': '6.3.0.0'}}
        sd.get_gcode_flush_para = lambda: {'purge_in_prime_tower': 0,
            'flush_volumes_matrix': None, 'flush_multiplier': 1}
        sd.get_first_Tn = lambda unused: re.search(r'(?m)^T\d+$',
            Path(sd.current_file.name).read_text(encoding='utf-8'))[0]
        self.pause_calls = 0
        self.opened = []
        sd.do_pause = self.pause_job
        if not passive:
            self.base.dispatch = object()
            self.lifecycle.current_dispatch = lambda: self.base.dispatch
        self.worker = self.base.dispatch
        self.box.preloading_en = True
        # Drain genuine connect-time metadata callbacks before measuring work
        # scheduled by the later print; they carry no mechanical permission.
        for timer in tuple(self.lifecycle._callbacks):
            self.reactor.fire(timer)

    def close(self):
        for handle in self.opened:
            handle.close()
        super().close()

    def pause_job(self):
        self.pause_calls += 1
        self.box.print_stats.state = 'paused'
        self.box.virtual_sdcard.cmd_from_sd = False
        self.box.virtual_sdcard.work_handler_dispatch = None

    def arm(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return self.command('CFS_TEST_PRINT_ARM FILENAME=approved.gcode SHA256=%s TOOLS=T1A,T1C CONFIRM=1' % digest)

    def open_job(self):
        handle = self.path.open('r', encoding='utf-8')
        self.opened.append(handle)
        self.box.virtual_sdcard.current_file = handle
        self.box.virtual_sdcard.file_position = self.box.virtual_sdcard.next_file_position = 0
        self.box.virtual_sdcard.file_size = self.path.stat().st_size
        self.box.virtual_sdcard.work_handler_dispatch = self.worker
        self.box.virtual_sdcard.cmd_from_sd = True
        self.box.print_stats.state = 'printing'
        self.base.dispatch = self.worker

    def start(self):
        self.arm()
        self.open_job()
        return self.command('CFS_PRINT_START_GUARD')

    def finish_file(self):
        self.box.virtual_sdcard.current_file.close()
        self.box.virtual_sdcard.current_file = None
        self.box.virtual_sdcard.cmd_from_sd = False
        self.box.virtual_sdcard.work_handler_dispatch = None
        self.box.print_stats.state = 'standby'

    def primitive(self, name, command):
        if name == 'SET_FILAMENT_SENSOR':
            values = command.get_command_parameters()
            self.printer.objects['filament_switch_sensor ' + values['SENSOR']].enabled = int(values['ENABLE'])
        else:
            super().primitive(name, command)


class PrintIntegrationTests(unittest.TestCase):
    def fixture(self):
        fixture = PrintIntegrationFixture()
        self.addCleanup(fixture.close)
        return fixture

    def assert_passive_closed(self, fixture):
        self.assertTrue(fixture.lifecycle.passive)
        self.assertFalse(fixture.box.print_trial.owns_dispatch())
        before = len(fixture.lines), len(fixture.native_events)
        with self.assertRaises(CommandError):
            fixture.command('BOX_START_PRINT')
        with self.assertRaises(UnsupportedRequest):
            fixture.box.send_native(build_request(1, 'SET_BOX_MODE', b'\x00\x01'))
        self.assertEqual((len(fixture.lines), len(fixture.native_events)), before)

    def ending_a_fixture(self):
        """External fixture observations, real decoder/drain/coordinator effects."""
        fixture = self.fixture()
        metadata = {'slicer': 'Creality', 'slicer_version': '7.3.1.6378',
            'flush_para': {'purge_in_prime_tower': 0, 'flush_multiplier': 1.3,
                'flush_volumes_matrix': [0, 297, 382, 530, 0, 401, 660, 533, 0]}}
        fixture.box.virtual_sdcard.gcode_metadata = {'metadata': metadata}
        fixture.box.virtual_sdcard.get_gcode_flush_para = lambda: metadata['flush_para']
        fixture.box.get_material_max_extrusion_speed = lambda target: 14.
        fixture.start()
        fixture.material, fixture.connections, fixture.buffer = 14, 0, 2
        exchange = fixture.native.cmd_send_data_with_response
        pending = [True]
        def ended_source(body, timeout, *args):
            result = exchange(body, timeout, *args)
            if pending[0] and body[3:] == b'\x0a':
                pending[0] = False
                return reply(1, 10, status=0x50, data=b'\x14\x1e\x00\x02')
            return result
        fixture.native.cmd_send_data_with_response = ended_source
        fixture.box.send_native(build_request(1, 'GET_BOX_STATE'))
        self.assertEqual(fixture.box.last_replies[1].status, 0x50)
        self.assertTrue(fixture.box.print_trial._runout)
        self.assertTrue(fixture.box.is_use_ending_material)
        self.assertEqual(fixture.box.error, 'filament_err')
        self.assertEqual(fixture.box.last_cmd, 'T1A')
        self.assertTrue(fixture.lifecycle.head_present())
        self.assertEqual(fixture.box.box_state.Tn_data['auto_refill'], 1)
        callbacks = tuple(fixture.lifecycle._callbacks)
        observed = {'draining': False, 'positive_e': 0., 'clear_edges': 0}
        primitive, drain = fixture.primitive, fixture.box.runout.extrusion_all_materials
        def movement(name, command):
            before = fixture.printer.position[3]
            result = primitive(name, command)
            if observed['draining']:
                observed['positive_e'] += max(0., fixture.printer.position[3] - before)
                if observed['positive_e'] >= 160 and fixture.lifecycle.head_present():
                    fixture.set_head(False)
                    observed['clear_edges'] += 1
            return result
        def draining():
            observed['draining'] = True
            try:
                return drain()
            finally:
                observed['draining'] = False
        fixture.primitive = movement
        fixture.box.runout.extrusion_all_materials = draining
        return fixture, observed, callbacks

    def test_decoder_runout_drain_then_t2_loads_c_without_repairing_old_live_hint(self):
        fixture, observed, callbacks = self.ending_a_fixture()
        error_history = deepcopy(fixture.box.error_tnn)
        empty_observations = []
        retract = fixture.box.box_retrude_material
        def observe_empty(*args, **kwargs):
            empty_observations.append((fixture.box.last_cmd, fixture.box.last_cmd_tmp,
                fixture.box.box_state.get_Tn_data(1, 'filament')))
            return retract(*args, **kwargs)
        fixture.box.box_retrude_material = observe_empty
        before = len(fixture.native_events)
        fixture.command('T2')
        self.assertEqual(observed['positive_e'], 240.)
        self.assertEqual(observed['clear_edges'], 1)
        self.assertEqual(sum(line == 'G0 E80.00 F360.00' for _, line in fixture.lines), 3)
        self.assertFalse(any(line == 'G0 E80.00 F840.00' for _, line in fixture.lines))
        self.assertEqual(empty_observations, [(None, 'T1A', 'T1A')])
        self.assertFalse(fixture.box.is_use_ending_material)
        self.assertFalse(fixture.box.box_filament_err)
        self.assertIsNone(fixture.box.error)
        self.assertIsNone(fixture.box.operations._error)
        self.assertEqual(fixture.box.operations.error_context, error_history)
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertEqual(fixture.box.box_state.get_Tn_data(1, 'filament'), 'T1C')
        self.assertEqual(fixture.box.box_state.get_Tn_data(1, 'vender', 'A'), 'none')
        self.assertEqual((fixture.material, fixture.connections, fixture.buffer), (14, 4, 0))
        self.assertTrue(fixture.lifecycle.head_present())
        self.assertFalse(any(opcode == 17 for _, opcode, _ in fixture.native_events[before:]))
        self.assertFalse(fixture.box.cut_sensor.cut_happened)
        self.assertEqual([data for _, opcode, data in fixture.native_events[before:] if opcode == 16],
            [b'\x04\x00\x00', b'\x04\x04\x00', b'\x04\x06\x00', b'\x04\x05\x00'])
        self.assertIsNone(fixture.box.move_z)
        self.assertEqual(fixture.e_origin, fixture.printer.position[3])
        self.assertEqual(fixture.script_errors, [])
        before = len(fixture.lines), len(fixture.native_events)
        for timer in callbacks:
            fixture.reactor.fire(timer)
        self.assertEqual((len(fixture.lines), len(fixture.native_events)), before)
        fixture.command('BOX_END')
        fixture.command('BOX_END_PRINT')
        fixture.finish_file()
        fixture.reactor.fire(fixture.lifecycle._print_trial_timer)
        self.assertFalse(fixture.box.print_trial.active)
        self.assertIsNone(fixture.box.last_cmd)
        self.assert_passive_closed(fixture)

    def test_decoder_runout_drain_rejects_conflicting_exhausted_hint_before_c_load(self):
        fixture, observed, _ = self.ending_a_fixture()
        retract = fixture.box.box_retrude_material
        def conflicting_hint(*args, **kwargs):
            # A negative PC fixture fault after the real drain. It must never
            # be normalized into the source prepared by this coordinator.
            fixture.box.last_cmd_tmp = 'T1C'
            return retract(*args, **kwargs)
        fixture.box.box_retrude_material = conflicting_hint
        before = len(fixture.native_events)
        with self.assertRaisesRegex(RuntimeError, 'phase'):
            fixture.command('T2')
        self.assertEqual(observed['positive_e'], 240.)
        self.assertIsNone(fixture.box.last_cmd)
        self.assertEqual(fixture.box.box_state.get_Tn_data(1, 'filament'), 'T1A')
        self.assertEqual(fixture.box.last_cmd_tmp, 'T1C')
        self.assertFalse(any(opcode == 16 for _, opcode, _ in fixture.native_events[before:]))
        self.assertFalse(fixture.box.print_trial.active)
        self.assertEqual(fixture.box.print_stats.state, 'paused')

    def test_ordinary_runout_keeps_material_speed_outside_owned_print(self):
        from test_runout import RunoutTests
        ordinary = RunoutTests()
        ordinary.setUp()
        ordinary.results['speed'] = 14.
        ordinary.box.print_trial = SimpleNamespace(owns_dispatch=lambda: False,
            context_guard=lambda: None)
        ordinary.runout.extrusion_all_materials()
        self.assertEqual(ordinary.calls('extrude'), [((80, 840.), {'wait': False})] * 2)

    def test_passive_registration_and_arming_have_no_mechanical_effect(self):
        fixture = self.fixture()
        expected = {'CFS_TEST_PRINT_ARM', 'CFS_PRINT_START_GUARD',
                    'CFS_TEST_PRINT_CONTROL', 'CFS_TEST_PRINT_DISARM'}
        self.assertTrue(expected.issubset(fixture.gcode.ready_gcode_handlers))
        before = deepcopy(fixture.box.box_state.get_status()), list(fixture.printer.position)
        result = fixture.arm()
        self.assertFalse(result['mechanics'])
        self.assertEqual((fixture.box.box_state.get_status(), fixture.printer.position), before)
        self.assertEqual(fixture.native_events, [])
        self.assertEqual(fixture.lines, [])
        self.assertIsNotNone(fixture.lifecycle._print_trial_timer)
        self.assert_passive_closed(fixture)

    def test_only_actual_open_file_worker_can_claim_registered_start_guard(self):
        fixture = self.fixture()
        fixture.arm()
        fixture.open_job()
        fixture.base.dispatch = object()
        with self.assertRaises(CommandError):
            fixture.command('CFS_PRINT_START_GUARD')
        self.assertFalse(fixture.box.print_trial.owns_dispatch())
        self.assertEqual(fixture.pause_calls, 1)
        self.assertTrue(fixture.lifecycle.passive)
        self.assertFalse(any(opcode not in (5, 8, 10, 14) for _, opcode, _ in fixture.native_events))

    def test_real_registered_print_start_and_same_source_t_use_selected_native_scope(self):
        fixture = self.fixture()
        self.assertEqual(fixture.start(), 'T1A')
        self.assertTrue(fixture.box.print_trial.owns_dispatch())
        fixture.command('BOX_START_PRINT')
        fixture.command('T0')
        writes = [(opcode, data) for _, opcode, data in fixture.native_events if opcode not in (5, 8, 10, 14)]
        # The retained helper suppresses preload CLOSE in actual printing
        # state even with force=True; DISABLE is the tighten action byte 1.
        self.assertEqual(writes, [(15, b'\x01'), (4, b'\x01\x00')])
        self.assertEqual(fixture.box.last_cmd, 'T1A')
        self.assertTrue(fixture.lifecycle.passive)

    def test_retained_traditional_tool_tokens_preserve_parameter_and_raw_body_contract(self):
        fixture = self.fixture()
        fixture.start()
        # Traditional GCodeCommand keeps the T command token itself in params;
        # its raw-body API excludes that token but preserves inline comments.
        first = HostCommand({'T': '0'}, 'T0')
        second = HostCommand({'T': '2'}, 'T2 ; selected second reviewed tool')
        self.assertEqual(first.get_raw_command_parameters(), '')
        self.assertEqual(second.get_raw_command_parameters(), '; selected second reviewed tool')
        fixture.gcode.ready_gcode_handlers['T0'](first)
        fixture.gcode.ready_gcode_handlers['T2'](second)
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 17],
                         [b'\x01\x00', b'\x01\x01'])
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 16],
                         [b'\x04\x00\x00', b'\x04\x04\x00', b'\x04\x06\x00', b'\x04\x05\x00'])
        self.assertTrue(fixture.lifecycle.passive)
        for case, parameters, raw in (
                ('extra_feed', {'T': '0', 'F': '100'}, 'F100'),
                ('duplicate_same', {'T': '0'}, 'T0'),
                ('duplicate_other', {'T': '2'}, 'T2'),
                ('wrong_token', {'T': '2'}, ''),
                ('nonstring_token', {'T': 0}, ''),
                ('missing_raw_api', {'T': '0'}, '')):
            with self.subTest(case=case):
                rejected = self.fixture()
                rejected.start()
                command = HostCommand(parameters, 'T0' + (' ' + raw if raw else ''))
                if case == 'missing_raw_api':
                    command.get_raw_command_parameters = None
                before = len(rejected.lines), len(rejected.native_events), len(rejected.hints), rejected.box.t_command
                with self.assertRaises(CommandError):
                    rejected.gcode.ready_gcode_handlers['T0'](command)
                self.assertEqual((len(rejected.lines), len(rejected.native_events), len(rejected.hints), rejected.box.t_command), before)
                self.assertTrue(rejected.box.print_trial.active)

    def test_unrelated_dispatch_cannot_borrow_live_job_permissions(self):
        fixture = self.fixture()
        fixture.start()
        fixture.base.dispatch = object()
        self.assert_passive_closed(fixture)
        self.assertTrue(fixture.box.print_trial.active)
        self.assertIsNone(fixture.box.print_trial.capture())

    def test_retained_physical_resume_hint_runs_only_until_the_worker_clears_it(self):
        fixture = self.fixture()
        fixture.start()
        fixture.command('T0')
        saved = next(values['tn_cur_tnn'] for _, values in reversed(fixture.hints) if 'tn_cur_tnn' in values)
        self.assertEqual(saved, 'T1A')
        self.assertNotIn(saved, fixture.box.print_trial._logical)
        # Retained RESUME_BASE transfers this physical-alias metadata unchanged
        # to virtual_sdcard; its worker clears it only after normal dispatch.
        fixture.box.virtual_sdcard.resume_tnn = saved
        before = len(fixture.native_events)
        fixture.command(fixture.box.virtual_sdcard.resume_tnn)
        self.assertEqual(fixture.box.t_command, saved)
        self.assertEqual(fixture.box.last_cmd, 'T1A')
        self.assertEqual([event[1:] for event in fixture.native_events[before:]], [(4, b'\x01\x00')])
        self.assertFalse(any(opcode in (16, 17) for _, opcode, _ in fixture.native_events[before:]))
        fixture.box.virtual_sdcard.resume_tnn = None
        before = len(fixture.lines), len(fixture.native_events), len(fixture.hints)
        with self.assertRaises(CommandError):
            fixture.command(saved)
        self.assertEqual((len(fixture.lines), len(fixture.native_events), len(fixture.hints)), before)
        self.assertTrue(fixture.box.print_trial.active)

    def test_physical_resume_alias_rejects_unrelated_worker_target_and_parameters(self):
        for case in ('external', 'not_from_sd', 'mismatched_alias', 'unapproved_target', 'parameters'):
            with self.subTest(case=case):
                fixture = self.fixture()
                fixture.start()
                fixture.box.virtual_sdcard.resume_tnn = 'T1A'
                command = 'T1A'
                if case == 'external':
                    fixture.base.dispatch = object()
                elif case == 'not_from_sd':
                    fixture.box.virtual_sdcard.cmd_from_sd = False
                elif case == 'mismatched_alias':
                    command = 'T1C'
                elif case == 'unapproved_target':
                    fixture.box.virtual_sdcard.resume_tnn = command = 'T1B'
                else:
                    command = 'T1A EXTRA=1'
                before = len(fixture.lines), len(fixture.native_events), len(fixture.hints), fixture.box.t_command
                with self.assertRaises(CommandError):
                    fixture.command(command)
                self.assertEqual((len(fixture.lines), len(fixture.native_events), len(fixture.hints), fixture.box.t_command), before)
                self.assertEqual(fixture.box.last_cmd, 'T1A')

    def test_start_guard_checks_actual_selected_sensor_frames_without_source_substitution(self):
        for case in ('missing_material', 'conflicting_lane', 'unknown_loaded_head'):
            with self.subTest(case=case):
                fixture = self.fixture()
                if case == 'missing_material':
                    fixture.material = 1
                elif case == 'conflicting_lane':
                    fixture.connections = 4
                else:
                    fixture.box.update_filament_pos('T1A', None)
                    fixture.box.last_cmd = None
                source = fixture.box.last_cmd
                with self.assertRaises(CommandError):
                    fixture.start()
                self.assertEqual(fixture.box.last_cmd, source)
                self.assertEqual(fixture.pause_calls, 1)
                self.assertFalse(fixture.box.print_trial.active)
                self.assertEqual(fixture.lines, [])
                self.assertTrue(all(opcode in (5, 8, 10, 14) for _, opcode, _ in fixture.native_events))
                self.assert_passive_closed(fixture)

    def test_actual_t_route_head_callback_and_end_release_scopes(self):
        fixture = self.fixture()
        fixture.start()
        fixture.command('BOX_START_PRINT')
        fixture.command('T0')
        fixture.command('T2')
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 17],
                         [b'\x01\x00', b'\x01\x01'])
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 16],
                         [b'\x04\x00\x00', b'\x04\x04\x00', b'\x04\x06\x00', b'\x04\x05\x00'])
        self.assertEqual(fixture.script_errors, [])
        fixture.command('BOX_END')
        self.assertIsNone(fixture.box.last_cmd)
        self.assertFalse(fixture.lifecycle.head_present())
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 17],
                         [b'\x01\x00', b'\x01\x01', b'\x04\x00', b'\x04\x01'])
        self.assertEqual(len([event for event in fixture.events if event[0] == 'submit']), 2)
        fixture.command('BOX_END_PRINT')
        fixture.finish_file()
        fixture.reactor.fire(fixture.lifecycle._print_trial_timer)
        self.assertFalse(fixture.box.print_trial.active)
        self.assertFalse(fixture.box.operations.active)
        self.assert_passive_closed(fixture)

    def test_real_loading_head_event_uses_a_separate_dispatch_and_one_target_stage6(self):
        fixture = self.fixture()
        fixture.start()
        fixture.head_callback = False
        original = fixture.native.cmd_send_data_with_response
        owners = []
        def separate_head(body, timeout, *args):
            if body[3:] == b'\x10\x04\x06\x00':
                owners.append(fixture.base.dispatch)
            result = original(body, timeout, *args)
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
        fixture.command('T2')
        self.assertEqual(len(owners), 1)
        self.assertIsNot(owners[0], fixture.worker)
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertEqual(fixture.script_errors, [])
        fixture.box.print_trial.cancel()
        self.assert_passive_closed(fixture)

    def test_worker_cannot_send_an_unarmed_head_stage(self):
        fixture = self.fixture()
        fixture.start()
        before = len(fixture.native_events)
        with self.assertRaises(UnsupportedRequest):
            fixture.box.communication_extrude_process(1, 'A', stage=6)
        self.assertEqual(len(fixture.native_events), before)
        self.assertFalse(fixture.box.print_trial.active)
        self.assert_passive_closed(fixture)

    def test_invalid_or_failed_real_head_callback_revokes_and_pauses_without_raising(self):
        for case in ('invalid_observation', 'failed_stage6'):
            with self.subTest(case=case):
                fixture = self.fixture()
                fixture.start()
                fixture.box.operations.begin('T1A', 'T1C', 1)
                fixture.box.operations.arm_head()
                if case == 'invalid_observation':
                    fixture.set_head(False)
                else:
                    fixture.faults[(16, b'\x04\x06\x00')] = 2
                before = list(fixture.printer.position), len(fixture.native_events)
                fixture.base.dispatch = object()
                fixture.printer.send_event('box:extrude_process_stage7')
                self.assertFalse(fixture.box.print_trial.active)
                self.assertEqual(fixture.pause_calls, 1)
                self.assertEqual(fixture.box.print_stats.state, 'paused')
                if case == 'invalid_observation':
                    self.assertEqual((fixture.printer.position, len(fixture.native_events)), before)
                else:
                    self.assertEqual(fixture.native_events[-1][1:], (16, b'\x04\x06\x00'))
                    self.assertEqual(fixture.printer.position[3], before[0][3] + 5)
                    self.assertFalse(fixture.box.operations.active)
                self.assert_passive_closed(fixture)

    def test_old_head_exception_after_scope_handoff_cannot_revoke_or_pause_new_job(self):
        fixture = self.fixture()
        fixture.start()
        old_token = fixture.box.print_trial._token
        fixture.box.operations.begin('T1A', 'T1C', 1)
        fixture.box.operations.arm_head()
        trial = fixture.box.print_trial
        original = trial.execution
        new_tokens = []

        @contextmanager
        def delayed_head_exit(kind='callback', token=None):
            if kind != 'head':
                with original(kind, token):
                    yield
                return
            try:
                with original(kind, token):
                    trial.cancel()
                    raise RuntimeError('synthetic delayed old head failure')
            except RuntimeError:
                # Re-arm through the real API only after the old scope has
                # released its busy count; delay its exception to the caller.
                fixture.box.operations.cancel()
                fixture.finish_file()
                fixture.worker = object()
                fixture.start()
                new_tokens.append(trial._token)
                raise

        trial.execution = delayed_head_exit
        fixture.base.dispatch = object()
        before = fixture.pause_calls
        fixture.printer.send_event('box:extrude_process_stage7')
        self.assertEqual(len(new_tokens), 1)
        self.assertIsNot(new_tokens[0], old_token)
        self.assertIs(trial._token, new_tokens[0])
        self.assertEqual(fixture.pause_calls, before)
        self.assertEqual(fixture.box.print_stats.state, 'printing')
        self.assertTrue(trial.active)
        self.assertFalse(any(opcode == 16 for _, opcode, _ in fixture.native_events))

    def test_registered_ui_pause_resume_and_disarm_keep_control_scopes_finite(self):
        fixture = self.fixture()
        fixture.start()
        observed = []
        def pause(command):
            observed.append(('pause', fixture.box.print_trial.owns_dispatch()))
            fixture.pause_job()
            fixture.printer.objects['pause_resume'].is_paused = True
        def resume(command):
            observed.append(('resume', fixture.box.print_trial.owns_dispatch()))
            fixture.printer.objects['pause_resume'].is_paused = False
            fixture.worker = object()
            fixture.box.virtual_sdcard.work_handler_dispatch = fixture.worker
            fixture.box.virtual_sdcard.cmd_from_sd = True
            fixture.box.print_stats.state = 'printing'
        fixture.gcode.register_command('PAUSE', pause)
        fixture.gcode.register_command('RESUME', resume)
        fixture.base.dispatch = object()
        before = list(fixture.printer.position)
        fixture.command('CFS_TEST_PRINT_CONTROL ACTION=PAUSE CONFIRM=1')
        self.assertEqual(fixture.box.print_stats.state, 'paused')
        self.assertFalse(fixture.box.print_trial.owns_dispatch())
        fixture.command('CFS_TEST_PRINT_CONTROL ACTION=RESUME CONFIRM=1')
        self.assertEqual(observed, [('pause', True), ('resume', True)])
        self.assertEqual(fixture.box.print_stats.state, 'printing')
        self.assertEqual(fixture.printer.position, before)
        self.assert_passive_closed(fixture)
        fixture.base.dispatch = fixture.worker
        self.assertTrue(fixture.box.print_trial.owns_dispatch())
        fixture.command('CFS_TEST_PRINT_DISARM CONFIRM=1')
        self.assertFalse(fixture.box.print_trial.active)
        self.assertEqual(fixture.box.print_stats.state, 'paused')
        self.assert_passive_closed(fixture)

    def test_ui_pause_rejects_a_cutter_side_pose_or_active_cfs_operation(self):
        for case in ('unsafe_y', 'active_operation'):
            with self.subTest(case=case):
                fixture = self.fixture()
                fixture.start()
                entered = []
                fixture.gcode.register_command('PAUSE', lambda command: entered.append(True))
                if case == 'unsafe_y':
                    fixture.printer.position[1] = fixture.box.boxcfg.safe_pos_y + 1
                else:
                    fixture.box.operations.begin('T1A', 'T1C', 1)
                fixture.base.dispatch = object()
                before = list(fixture.printer.position), len(fixture.native_events)
                with self.assertRaises(CommandError):
                    fixture.command('CFS_TEST_PRINT_CONTROL ACTION=PAUSE CONFIRM=1')
                self.assertEqual(entered, [])
                self.assertEqual((fixture.printer.position, len(fixture.native_events)), before)

    def test_real_runout_then_head_clear_admits_one_registered_refill_producer(self):
        for case in ('no_runout', 'head_present', 'clear_head'):
            with self.subTest(case=case):
                fixture = self.fixture()
                fixture.start()
                if case != 'no_runout':
                    original = fixture.native.cmd_send_data_with_response
                    def runout(body, timeout, *args):
                        result = original(body, timeout, *args)
                        return reply(1, 10, status=0x50, data=b'\x14\x1e\x00\x02') if body[3:] == b'\x0a' else result
                    fixture.native.cmd_send_data_with_response = runout
                    fixture.base.dispatch = object()
                    fixture.lifecycle.address = 0
                    self.assertNotEqual(fixture.lifecycle._heartbeat(fixture.reactor.monotonic()), fixture.reactor.NEVER)
                    fixture.native.cmd_send_data_with_response = original
                fixture.set_head(case == 'head_present')
                fixture.base.dispatch = object()
                before = len(fixture.lines), len(fixture.native_events), len(fixture.lifecycle._callbacks)
                if case == 'no_runout':
                    self.assertIsNone(fixture.command('BOX_CHECK_MATERIAL_REFILL'))
                    self.assertEqual((len(fixture.lines), len(fixture.native_events), len(fixture.lifecycle._callbacks)), before)
                elif case != 'clear_head':
                    with self.assertRaises(CommandError):
                        fixture.command('BOX_CHECK_MATERIAL_REFILL')
                    self.assertEqual((len(fixture.lines), len(fixture.native_events), len(fixture.lifecycle._callbacks)), before)
                else:
                    fixture.command('BOX_CHECK_MATERIAL_REFILL')
                    self.assertFalse(fixture.box.is_use_ending_material)
                    self.assertIsNone(fixture.box.error)
                    self.assertEqual(len(fixture.lifecycle._callbacks), before[2] + 1)
                    self.assertIn((4, b'\x00\x01'), [event[1:] for event in fixture.native_events[before[1]:]])
                    self.assertFalse(fixture.box.print_trial.owns_dispatch())
                    after = len(fixture.lines), len(fixture.native_events), len(fixture.lifecycle._callbacks)
                    with self.assertRaises(CommandError):
                        fixture.command('BOX_CHECK_MATERIAL_REFILL')
                    self.assertEqual((len(fixture.lines), len(fixture.native_events), len(fixture.lifecycle._callbacks)), after)

    def test_deferred_normal_withdrawal_callback_after_loading_is_a_scope_free_noop(self):
        fixture = self.fixture()
        fixture.box.last_cmd = 'T1C'
        fixture.box.update_filament_pos('T1C', 'T1C')
        fixture.connections, fixture.native.mode = 4, 2
        pending = []
        original = fixture.set_head

        def deferred_runout(eventtime):
            # The retained helper dispatches its command later, independently
            # of the Box operation's worker and callback-token scopes.
            fixture.dispatch('BOX_CHECK_MATERIAL_REFILL\nM400')

        def set_head(present):
            previous = fixture.lifecycle.head_sensor.runout_helper.filament_present
            original(present)
            if previous and not present:
                pending.append(fixture.reactor.register_timer(deferred_runout, fixture.reactor.monotonic()))
        fixture.set_head = set_head
        fixture.start()
        fixture.command('BOX_START_PRINT_EXTRUDE_MATERIAL')
        self.assertEqual(fixture.box.last_cmd, 'T1A')
        self.assertEqual(len(pending), 1)
        self.assertFalse(fixture.box.operations.active)
        sensor = fixture.lifecycle.head_sensor
        sensor.enabled = 1
        trial = fixture.box.print_trial
        token = trial._token
        before = (deepcopy(fixture.box.box_state.get_status()), list(fixture.printer.position),
                  fixture.box.last_cmd, fixture.box.last_cmd_tmp, fixture.box.error,
                  dict(fixture.box.error_tnn), len(fixture.native_events), len(fixture.lifecycle._callbacks))
        fixture.base.dispatch = object()
        fixture.reactor.fire(pending[0])
        self.assertEqual((fixture.box.box_state.get_status(), fixture.printer.position,
                          fixture.box.last_cmd, fixture.box.last_cmd_tmp, fixture.box.error,
                          fixture.box.error_tnn, len(fixture.native_events), len(fixture.lifecycle._callbacks)), before)
        self.assertEqual(sensor.enabled, 1)
        self.assertIs(trial._token, token)
        self.assertEqual(fixture.pause_calls, 0)
        self.assertEqual(fixture.script_errors, [])
        self.assertFalse(trial.owns_dispatch())
        self.assertFalse(trial._runout)
        self.assertFalse(fixture.box.is_use_ending_material)
        self.assert_passive_closed(fixture)

    def test_stale_refill_noop_rejects_parameters_unstarted_jobs_and_pending_work(self):
        cases = ('parameters', 'unstarted', 'inactive', 'paused', 'error', 'runout_latch',
                 'ending', 'filament_error', 'inside_error', 'refill_pending', 'inside_refill', 'inside_drain')
        for case in cases:
            with self.subTest(case=case):
                fixture = self.fixture()
                if case == 'unstarted':
                    fixture.arm()
                elif case != 'inactive':
                    fixture.start()
                if case == 'paused': fixture.box.print_stats.state = 'paused'
                elif case == 'error': fixture.box.error = 'filament_err'
                elif case == 'runout_latch': fixture.box.print_trial._runout = True
                elif case == 'ending': fixture.box.is_use_ending_material = True
                elif case == 'filament_error': fixture.box.box_filament_err = True
                elif case == 'inside_error': fixture.box.inside_error = True
                elif case == 'refill_pending': fixture.box.material_auto_refill_flag = True
                elif case == 'inside_refill': fixture.box.inside_auto_refill = True
                elif case == 'inside_drain': fixture.box.inside_extrusion_all_materials = True
                fixture.base.dispatch = object()
                sensor = fixture.lifecycle.head_sensor
                sensor.enabled = 1
                token = fixture.box.print_trial._token
                before = (list(fixture.printer.position), fixture.box.last_cmd,
                          fixture.box.error, dict(fixture.box.error_tnn), len(fixture.native_events),
                          len(fixture.lifecycle._callbacks))
                command = 'BOX_CHECK_MATERIAL_REFILL' + (' EXTRA=1' if case == 'parameters' else '')
                with self.assertRaises(CommandError):
                    fixture.command(command)
                self.assertEqual((fixture.printer.position, fixture.box.last_cmd, fixture.box.error,
                                  fixture.box.error_tnn, len(fixture.native_events), len(fixture.lifecycle._callbacks)), before)
                self.assertIs(fixture.box.print_trial._token, token)
                self.assertEqual(sensor.enabled, 1)

    def test_genuine_heartbeat_runout_frame_gets_protocol_and_scheduled_scopes(self):
        fixture = self.fixture()
        fixture.start()
        fixture.box.box_state.modify_Tn_data(0, 0, uppart='filament_useup')
        original = fixture.native.cmd_send_data_with_response
        def runout(body, timeout, *args):
            result = original(body, timeout, *args)
            return reply(1, 10, status=0x50, data=b'\x14\x1e\x00\x02') if body[3:] == b'\x0a' else result
        fixture.native.cmd_send_data_with_response = runout
        fixture.base.dispatch = object()
        fixture.lifecycle.address = 0
        before = list(fixture.printer.position)
        outcome = fixture.lifecycle._heartbeat(fixture.reactor.monotonic())
        self.assertNotEqual(outcome, fixture.reactor.NEVER)
        self.assertTrue(fixture.box.print_trial._runout)
        self.assertTrue(fixture.box.is_use_ending_material)
        self.assertEqual(fixture.box.error, 'filament_err')
        self.assertEqual(fixture.box.box_state.get_Tn_data(0, uppart='filament_useup'), 1)
        self.assertEqual(len(fixture.lifecycle._callbacks), 1)
        self.assertEqual(fixture.printer.position, before)
        self.assertFalse(fixture.box.print_trial.owns_dispatch())
        self.assertTrue(fixture.box.print_trial.active)
        self.assertTrue(all(opcode in (5, 8, 10, 14) for _, opcode, _ in fixture.native_events))

    def test_unrelated_lifecycle_calls_cannot_forge_runout_or_schedule_mechanics(self):
        fixture = self.fixture()
        fixture.start()
        fixture.base.dispatch = object()
        before = len(fixture.lines), len(fixture.native_events), len(fixture.lifecycle._callbacks)
        fixture.lifecycle.record_error('filament_err', {'addr': 1, 'last_tnn': 'T1A', 'tnn': 'T1A'})
        fixture.lifecycle.schedule_tighten_up()
        fixture.lifecycle.run_gcode('G0 X100')
        self.assertEqual(fixture.box.error, 'filament_err')
        self.assertFalse(fixture.box.print_trial._runout)
        self.assertFalse(fixture.box.filament_sensor_enable)
        self.assertEqual((len(fixture.lines), len(fixture.native_events), len(fixture.lifecycle._callbacks)), before)
        self.assert_passive_closed(fixture)

    def test_scheduled_callback_temporarily_owns_native_and_releases_dispatch(self):
        fixture = self.fixture()
        fixture.start()
        seen = []
        fixture.lifecycle.schedule(lambda when: (seen.append(fixture.box.print_trial.owns_dispatch()),
                                                 fixture.box.communication_set_box_mode(1, 'IDLE')))
        timer = next(iter(fixture.lifecycle._callbacks))
        fixture.base.dispatch = object()
        self.assertEqual(fixture.reactor.fire(timer), fixture.reactor.NEVER)
        self.assertEqual(seen, [True])
        self.assertEqual(fixture.native_events[-1][1:], (4, b'\x00\x01'))
        self.assertEqual(fixture.lifecycle._callbacks, set())
        self.assert_passive_closed(fixture)

    def test_old_scheduled_callback_neither_executes_nor_pauses_rearmed_job(self):
        fixture = self.fixture()
        fixture.start()
        seen = []
        fixture.lifecycle.schedule(lambda when: seen.append('old callback'))
        old_timer = next(iter(fixture.lifecycle._callbacks))
        fixture.box.print_trial.cancel()
        fixture.finish_file()
        fixture.worker = object()
        fixture.start()
        new_token = fixture.box.print_trial._token
        fixture.base.dispatch = object()
        before = fixture.pause_calls
        fixture.reactor.fire(old_timer)
        self.assertEqual(seen, [])
        self.assertIs(fixture.box.print_trial._token, new_token)
        self.assertEqual(fixture.pause_calls, before)
        self.assertEqual(fixture.box.print_stats.state, 'printing')

    def test_report_guard_stops_after_yield_when_print_permission_is_revoked(self):
        fixture = self.fixture()
        fixture.start()
        fixture.printer.heater.smoothed_temp = 20
        fixture.printer.heater.can_extrude = False
        fixture.reactor.on_pause = fixture.box.print_trial.cancel
        before = len(fixture.native_events), list(fixture.printer.position)
        with self.assertRaises(RuntimeError):
            fixture.box.quickly_wait_heating(220)
        self.assertEqual([line for _, line in fixture.lines if line.startswith('M104')], ['M104 S220'])
        self.assertEqual((len(fixture.native_events), fixture.printer.position), before)
        self.assertFalse(fixture.box.print_trial.active)
        self.assert_passive_closed(fixture)

    def test_report_guard_and_watchdog_stop_on_lease_or_file_change(self):
        fixture = self.fixture()
        fixture.start()
        check = fixture.lifecycle._report_guard(operation=False)
        fixture.path.write_text('START_PRINT\nT0\nT2\nG1 X101\nEND_PRINT\n', encoding='utf-8')
        fixture.reactor.pause(fixture.reactor.monotonic() + .1)
        with self.assertRaises(RuntimeError):
            check()
        fixture.reactor.fire(fixture.lifecycle._print_trial_timer)
        self.assertEqual(fixture.pause_calls, 1)
        self.assertFalse(fixture.box.print_trial.active)
        self.assert_passive_closed(fixture)

    def test_shutdown_revokes_job_and_removes_owned_timers_without_motion(self):
        fixture = self.fixture()
        fixture.start()
        check = fixture.lifecycle._report_guard(operation=False)
        before = list(fixture.printer.position), len(fixture.lines)
        fixture.printer.send_event('klippy:shutdown')
        self.assertFalse(fixture.box.print_trial.active)
        self.assertIsNone(fixture.lifecycle._print_trial_timer)
        self.assertEqual((fixture.printer.position, len(fixture.lines)), before)
        with self.assertRaises(LifecycleStopped):
            check()
        self.assertTrue(fixture.lifecycle.passive)


class ActivePrintIntegrationTests(unittest.TestCase):
    def fixture(self):
        fixture = PrintIntegrationFixture(loaded=False, passive=False)
        self.addCleanup(fixture.close)
        fixture.path.write_bytes(b'START_PRINT INITIAL_TOOL=0\nT0\nT2\nT3\nEND_PRINT\n')
        metadata = {'slicer': 'OrcaSlicer', 'slicer_version': '2.5.2', 'flush_para': {
            'purge_in_prime_tower': 1, 'flush_multiplier': 1,
            'flush_volumes_matrix': [0, 149, 191, 155, 265, 0, 201, 287,
                                     330, 267, 0, 353, 130, 155, 201, 0]}}
        fixture.box.virtual_sdcard.gcode_metadata = {'metadata': metadata}
        fixture.box.virtual_sdcard.get_gcode_flush_para = lambda: metadata['flush_para']
        fixture.box.box_state.modify_Tn_data(1, '000FF00', 'color_value', 'D')
        # Model the retained START_PRINT order for prepare=1 and adaptive mesh.
        # The mesh primitive is external; all CFS commands use actual handlers.
        fixture.host.macro('START_PRINT', {'gcode':
            'CFS_PRINT_START_GUARD INITIAL_TOOL={params.INITIAL_TOOL|default(-1)}\n'
            'BOX_START_PRINT\n_USER_MODEL_BED_MESH\nM109 S215\n'
            'BOX_START_PRINT_EXTRUDE_MATERIAL'})
        fixture.host.macro('END_PRINT', {'gcode': 'BOX_END\nBOX_END_PRINT'})
        meshes = []
        fixture.gcode.register_command('_USER_MODEL_BED_MESH', lambda command: meshes.append(True))
        return fixture, meshes

    def test_regular_orca_start_changes_three_slots_and_unloads_at_end_without_arming(self):
        fixture, meshes = self.fixture()
        # Orca sends these as ordinary commands before its virtual-SD request.
        fixture.command('BOX_MODIFY_TN T1A=T1A T1B=T1B T1C=T1C T1D=T1D')
        fixture.command('BOX_ENABLE_CFS_PRINT ENABLE=1')
        fixture.open_job()
        seen = []
        for line in fixture.path.read_text(encoding='utf-8').splitlines():
            fixture.dispatch(line)
            seen.append(fixture.box.last_cmd)
        fixture.finish_file()

        self.assertEqual(fixture.script_errors, [])
        self.assertEqual(meshes, [True])
        self.assertEqual(seen, ['T1A', 'T1A', 'T1C', 'T1D', None])
        self.assertEqual([data[0] for _, opcode, data in fixture.native_events
                          if opcode == 0x10 and data[1] == 0], [1, 4, 8])
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 0x11],
                         [b'\x01\x00', b'\x01\x01', b'\x04\x00', b'\x04\x01',
                          b'\x08\x00', b'\x08\x01'])
        self.assertFalse(fixture.box.print_trial.active)
        self.assertFalse(fixture.lifecycle.head_present())

    def test_wrong_initial_tool_stops_before_mesh_heating_or_cfs_commands(self):
        fixture, meshes = self.fixture()
        fixture.open_job()
        with self.assertRaises(CommandError):
            fixture.dispatch('START_PRINT INITIAL_TOOL=3')
        self.assertEqual(meshes, [])
        self.assertEqual(fixture.native_events, [])
        self.assertEqual(fixture.printer.heater.target, 0)
        self.assertIsNone(fixture.box.last_cmd)

    def test_active_failed_tool_selection_cannot_continue_file_without_recovery_pause(self):
        fixture, _ = self.fixture()
        fixture.open_job()
        fixture.box.Tn_action = lambda tool: False
        with self.assertRaisesRegex(CommandError, 'could not select tool T0'):
            fixture.command('T0')
        fixture.box.virtual_sdcard.must_pause_work = True
        self.assertIsNone(fixture.command('T0'))
        self.assertTrue(fixture.box.virtual_sdcard.must_pause_work)

    def paused_t2_fixture(self):
        fixture, _ = self.fixture()
        box, sd = fixture.box, fixture.box.virtual_sdcard
        pause_resume = fixture.printer.objects['pause_resume'] = SimpleNamespace(is_paused=False)
        fixture.lifecycle.errors.current_dispatch = fixture.lifecycle.current_dispatch
        fixture.printer.heater.target_temp = 0.
        fixture.printer.heater.min_extrude_temp = 170.

        def request_pause(command):
            sd.must_pause_work = True
            pause_resume.is_paused = True

        fixture.gcode.register_command('PAUSE', request_pause)
        fixture.gcode.register_command('WAIT_PAUSE', lambda command: None)
        fixture.gcode.register_command('SAVE_GCODE_STATE', lambda command: None)
        fixture.open_job()
        fixture.dispatch('START_PRINT INITIAL_TOOL=0\nT0')
        self.assertEqual(box.last_cmd, 'T1A')

        # Reproduce the real pause: A returns normally, then C's feed reports
        # EXTRUDE_ERR5 before the head switch. Retained virtual SD consumes T2.
        file_bytes = fixture.path.read_bytes()
        sd.file_position = file_bytes.index(b'T2\n')
        sd.next_file_position = sd.file_position + len(b'T2\n')
        sd.file_size = len(file_bytes)
        sd.resume_tnn = None
        fixture.faults[(0x10, b'\x04\x04\x00')] = 0x09
        fixture.dispatch('T2')
        self.assertEqual(fixture.script_errors, [])
        self.assertTrue(sd.must_pause_work)
        self.assertEqual(box.error, 'box_extrude_err')
        self.assertEqual((box.error_tnn['last_tnn'], box.error_tnn['tnn']), ('T1A', 'T1C'))
        self.assertIsNone(box.last_cmd)
        self.assertFalse(fixture.lifecycle.head_present())
        del fixture.faults[(0x10, b'\x04\x04\x00')]
        sd.file_position = sd.next_file_position
        fixture.pause_job()
        fixture.base.dispatch = object()

        # Keep the retained critical order. Only the eventual SD restart is a
        # test observation, with the real CFS clear and resume hooks dispatched.
        resumed = []

        def resume_base(command):
            resumed.append((sd.file_position, box.last_cmd, fixture.lifecycle.head_present()))
            box.print_stats.state = 'printing'
            pause_resume.is_paused = False
            sd.must_pause_work = False

        fixture.host.macro('RESUME', {'gcode': 'BOX_ERROR_CLEAR\nBOX_RESUME_EXTRUDE\nRESUME_BASE'})
        fixture.gcode.register_command('RESUME_BASE', resume_base)
        fixture.native_events.clear()
        return fixture, resumed

    def test_paused_t2_direct_resume_holds_then_retry_loads_c_before_sd_restarts(self):
        fixture, resumed = self.paused_t2_fixture()
        box, sd = fixture.box, fixture.box.virtual_sdcard
        stopped_at = sd.file_position
        with self.assertRaises(CommandError):
            fixture.dispatch('RESUME')
        self.assertIsNone(box.error)  # BOX_ERROR_CLEAR ran before the refusal.
        self.assertEqual(resumed, [])
        self.assertFalse(any(opcode == 0x10 for _, opcode, _ in fixture.native_events))
        fixture.script_errors.clear()  # The deliberate command refusal above.

        fixture.material = 0x0B  # C is absent in the live MATERIAL mask.
        fixture.native_events.clear()
        fixture.command('BOX_TNN_RETRY_PROCESS')
        self.assertIn((None, 0x08, b'\x00'), fixture.native_events)
        self.assertFalse(any(opcode == 0x10 for _, opcode, _ in fixture.native_events))
        self.assertEqual(resumed, [])
        self.assertEqual(box.print_stats.state, 'paused')

        fixture.material = 0x0F
        fixture.native_events.clear()
        fixture.command('BOX_TNN_RETRY_PROCESS')
        self.assertEqual(fixture.script_errors, [])
        self.assertEqual(resumed, [(stopped_at, 'T1C', True)])
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 0x10],
                         [b'\x04\x00\x00', b'\x04\x04\x00', b'\x04\x06\x00', b'\x04\x05\x00'])
        self.assertFalse(any(opcode == 0x11 for _, opcode, _ in fixture.native_events))

    def test_paused_t2_retry_keeps_job_paused_if_head_clears_after_purge(self):
        fixture, resumed = self.paused_t2_fixture()
        phase = fixture.box.material_change_flush

        def lose_head_after_flush(*args, **kwargs):
            result = phase(*args, **kwargs)
            fixture.set_head(False)
            return result

        fixture.box.material_change_flush = lose_head_after_flush
        fixture.command('BOX_TNN_RETRY_PROCESS')
        self.assertIn((None, 0x10, b'\x04\x06\x00'), fixture.native_events)
        self.assertFalse(fixture.lifecycle.head_present())
        self.assertEqual(fixture.box.print_stats.state, 'paused')
        self.assertEqual(resumed, [])
        with self.assertRaises(CommandError):
            fixture.dispatch('RESUME')
        self.assertEqual(resumed, [])

    def test_paused_t2_retry_refuses_changed_file_mapping_or_position(self):
        for change in ('file', 'mapping', 'position'):
            with self.subTest(change=change):
                fixture, resumed = self.paused_t2_fixture()
                box, sd = fixture.box, fixture.box.virtual_sdcard
                fixture.command('BOX_ERROR_CLEAR')
                fixture.native_events.clear()
                if change == 'file':
                    # Reopening the same name cannot inherit the paused handle.
                    sd.current_file.close()
                    sd.current_file = replacement = fixture.path.open('r', encoding='utf-8')
                    fixture.opened.append(replacement)
                elif change == 'mapping':
                    fixture.command('BOX_MODIFY_TN T1C=T1D')
                else:
                    sd.file_position += 1
                before = list(fixture.printer.position), len(fixture.moves)
                if change == 'file':
                    fixture.command('BOX_TNN_RETRY_PROCESS')
                    fixture.command('BOX_TNN_RETRY_PROCESS')
                else:
                    with self.assertRaises(CommandError):
                        fixture.command('BOX_TNN_RETRY_PROCESS')
                self.assertEqual(fixture.native_events, [])
                self.assertEqual((fixture.printer.position, len(fixture.moves)), before)
                self.assertEqual(box.print_stats.state, 'paused')
                self.assertEqual(resumed, [])

    def ending_fixture(self, connections_after_drain):
        fixture, _ = self.fixture()
        fixture.open_job()
        fixture.dispatch('START_PRINT INITIAL_TOOL=0')
        self.assertEqual(fixture.box.last_cmd, 'T1A')
        self.assertTrue(fixture.lifecycle.head_present())
        fixture.material, fixture.connections, fixture.buffer = 14, connections_after_drain, 2
        exchange = fixture.native.cmd_send_data_with_response

        def exhausted(body, timeout, *args):
            result = exchange(body, timeout, *args)
            if body[3:] == b'\x0a':
                fixture.native.cmd_send_data_with_response = exchange
                return reply(1, 0x0A, status=0x50, data=b'\x14\x1e\x00\x02')
            return result

        fixture.native.cmd_send_data_with_response = exhausted
        fixture.box.send_native(build_request(1, 'GET_BOX_STATE'))
        self.assertTrue(fixture.box.is_use_ending_material)
        self.assertEqual(fixture.box.error, 'filament_err')

        # The head sensor clears once the simulated tail reaches the nozzle.
        # Connection/buffer/STATE observations still come from the native fixture.
        primitive = fixture.primitive

        def movement(name, command):
            result = primitive(name, command)
            if command.get_commandline().startswith('G0 E80.00 '):
                fixture.set_head(False)
            return result

        fixture.primitive = movement
        fixture.native_events.clear()
        return fixture

    def test_active_replaces_exhausted_a_after_fresh_empty_observations(self):
        fixture = self.ending_fixture(connections_after_drain=0)
        fixture.dispatch('T2')
        self.assertEqual(fixture.script_errors, [])
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertIsNone(fixture.box.error)
        self.assertFalse(fixture.box.is_use_ending_material)
        self.assertTrue(fixture.lifecycle.head_present())
        writes = [row[1:] for row in fixture.native_events]
        idle = writes.index((4, b'\x00\x01'))
        first_feed = writes.index((16, b'\x04\x00\x00'))
        self.assertLess(idle, first_feed)
        for observation in ((10, b''), (8, b'\x01'), (5, b'')):
            self.assertIn(observation, writes[idle + 1:first_feed])
        self.assertEqual([data for opcode, data in writes if opcode == 16],
                         [b'\x04\x00\x00', b'\x04\x04\x00', b'\x04\x06\x00', b'\x04\x05\x00'])
        self.assertFalse(any(opcode == 17 for opcode, _ in writes))

    def test_active_drain_with_lingering_connection_cannot_clear_source_or_feed(self):
        fixture = self.ending_fixture(connections_after_drain=1)
        with self.assertRaises((CommandError, RuntimeError)):
            fixture.command('T2')
        self.assertEqual(fixture.box.last_cmd, 'T1A')
        self.assertEqual(fixture.box.box_state.get_Tn_data(1, 'filament'), 'T1A')
        self.assertFalse(any(opcode == 16 for _, opcode, _ in fixture.native_events))


if __name__ == '__main__':
    unittest.main()
