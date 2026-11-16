"""Compose interface-prepared material commands on real Box components."""

import re
import shlex
import struct
import unittest
from copy import deepcopy

from macro_harness import Host, Model
from test_box_data import CommandError
from test_host_actions import HostCommand
import test_host_actions as host_actions
import test_material_trial as material_trial_tests
from test_native import reply


# Synthetic recipes describe the public command contract without vendor files.
LOAD_RECIPE = (
    'RESPOND MSG="synthetic load"', 'M104', 'IF_NEED_HOME', 'BOX_ERROR_CLEAR',
    'BOX_CHECK_MATERIAL', 'BOX_CUT_MATERIAL', 'BOX_RETRUDE_MATERIAL',
    'BOX_GO_TO_EXTRUDE_POS', 'BOX_NOZZLE_CLEAN',
    'BOX_EXTRUDE_MATERIAL TNN={target}', 'BOX_EXTRUDER_EXTRUDE TNN={target}',
    'BOX_MATERIAL_FLUSH', 'RESTORE_POSITION',
)
QUIT_RECIPE = (
    'RESPOND MSG="synthetic quit"', 'BOX_ERROR_CLEAR', 'BOX_CHECK_MATERIAL',
    'BOX_CUT_MATERIAL', 'BOX_RETRUDE_MATERIAL', 'BOX_GO_TO_BOX_EXTRUDE_POS',
)


class WorkflowCommand(HostCommand):
    """Keep missing-default semantics while serving both handlers and macros."""

    def __init__(self, line):
        name, _, raw = line.partition(' ')
        if re.fullmatch(r'[GMT]\d+(?:\.\d+)?', name):
            parameters = dict(re.findall(r'([A-Z])\s*([-+]?\d*\.?\d+)', raw.upper()))
        else:
            parameters = dict(word.split('=', 1) for word in shlex.split(raw))
        super().__init__(parameters, line)
        self.raw = raw.strip()

    def get_raw_command_parameters(self):
        return self.raw


class WorkflowFixture:
    """Supply only external thermal, sensor, motion and native observations."""

    def __init__(self, loaded=False, unknown_source=False, passive=False):
        if loaded and unknown_source:
            raise ValueError('choose a known or absent host source')
        positions = dict(extrude_pos_x=100,
            extrude_pos_y=200, extrude_pos_z=30, safe_pos_y=180,
            clean_right_pos_x=150, pre_cut_pos_x=40, pre_cut_pos_y=100,
            cut_pos_y=205, retrude_position_x=40, retrude_position_y=140)
        if passive:
            # Register real passive wrappers before the retained-style aliases,
            # as load_config does; changing only lifecycle.passive is not enough.
            self.base = material_trial_tests.MaterialTrialTests()
            self.base.make_box('UNLOAD' if loaded or unknown_source else 'LOAD',
                target='T1A' if loaded else 'T1C', unknown_source=unknown_source,
                switch_pin='synthetic_pin', **positions)
        else:
            self.base = host_actions.HostActionTests()
            self.base.make_box(switch='synthetic_pin', **positions)
        self.base.native.mode = 0
        if not passive:
            self.base.ready(identify=True)
        self.box, self.native = self.base.box, self.base.native
        self.printer, self.reactor = self.base.printer, self.base.reactor
        self.lifecycle = self.base.lifecycle
        self.printer.position[:] = [120., 150., 30., 0.]
        self.box.gcode_move.last_position = list(self.printer.position)
        self.completed = list(self.printer.position)
        self.material = 15
        self.connections = 4 if unknown_source else (1 if loaded else 0)
        self.buffer = 0 if unknown_source else 2
        self.head_callback, self.cut_callback, self.clear_head = True, True, True
        self.faults, self.on_native = {}, None
        self.native_events, self.lines, self.moves, self.responses = [], [], [], []
        self.script_errors, self.tokens = [], []
        self.events = []
        self.token = None
        self.fail_primitive = None
        self.printer.heater.target = 0
        self.set_head(loaded or unknown_source)
        if loaded:
            self.box.last_cmd = 'T1A'
            self.box.update_filament_pos('T1A', 'T1A')
        self.host = Host(Model)
        self.host.command_error = self.host.gcode.error = CommandError
        self.gcode = self.host.gcode
        self.gcode.ready_gcode_handlers = self.gcode.commands
        self.gcode.create_gcode_command = lambda name, line, parameters: WorkflowCommand(line)
        for name, handler in self.base.gcode.commands.items():
            if name in ('IF_NEED_HOME', 'BOX_CHECK_MATERIAL'):
                continue
            self.gcode.register_command(name, handler)
        self.host.objects['toolhead'] = self.printer.objects['toolhead']
        self.host.macro('IF_NEED_HOME', {'gcode':
            '{% if not printer.toolhead.homed_axes %}G28{% endif %}'})
        self.host.macro('BOX_CHECK_MATERIAL', {'gcode': 'WAIT_EXTRUSION_ALL_MATERIALS'})
        self.host.macro('NEXT_FILAMENT', {'variable_temp': '0', 'gcode':
            'SET_GCODE_VARIABLE MACRO=NEXT_FILAMENT VARIABLE=temp '
            'VALUE={params.TEMP|default(0)|float}'})
        for name in ('BOX_EXTRUDER_EXTRUDE', 'BOX_MATERIAL_FLUSH'):
            self.host.macro(name, {'rename_existing': name + '_99', 'gcode':
                '{% if printer["gcode_macro NEXT_FILAMENT"].temp > 0 %}'
                'M109 S{printer["gcode_macro NEXT_FILAMENT"].temp}\n{% endif %}'
                + name + '_99 {rawparams}'})
        self.host.connect()
        self.gcode.run_script_from_command = self.dispatch
        self.gcode.respond_raw = self.gcode.respond_info = lambda *args, **kwargs: None
        self.box.gcode = self.printer.gcode = self.printer.objects['gcode'] = self.gcode
        self.lifecycle.errors.gcode = self.gcode
        self.native.cmd_send_data_with_response = self.exchange
        self.native.calls.clear()
        self.reactor.pauses.clear()
        self.reactor.on_pause = self.heat

    def close(self):
        self.base.doCleanups()

    def set_head(self, present):
        self.printer.objects['filament_switch_sensor head'].runout_helper.filament_present = present

    def heat(self):
        if self.printer.heater.target:
            self.printer.heater.smoothed_temp = self.printer.heater.target
            self.printer.heater.can_extrude = self.printer.heater.target >= 170

    def dispatch(self, script):
        """Model the two retained exception boundaries without vendor imports."""
        for line in script.splitlines():
            line = line.strip()
            if not line or line.startswith(('#', ';')):
                continue
            name = line.split()[0].upper()
            command = WorkflowCommand(line)
            self.lines.append((self.token, line))
            self.events.append(('gcode', line))
            try:
                if name == 'SET_GCODE_VARIABLE':
                    self.gcode.mux[(name, command.get('MACRO'))](command)
                elif name in self.gcode.commands:
                    self.gcode.commands[name](command)
                else:
                    self.primitive(name, command)
            except CommandError as error:
                self.script_errors.append((self.token, 'command', str(error)))
                raise
            except Exception as error:
                # The examined dispatcher reports an ordinary Python exception
                # then continues; a service result alone is not operation proof.
                self.script_errors.append((self.token, 'internal', str(error)))

    def service(self, recipe, target='T1C', flush='BOX_MATERIAL_FLUSH'):
        for template in recipe:
            line = template.format(target=target)
            if line == 'BOX_MATERIAL_FLUSH':
                line = flush
            self.token = line
            self.tokens.append(line)
            try:
                self.dispatch(line)
            except CommandError as error:
                response = {'error': str(error)}
            else:
                response = {'result': {}}
            # Matched result/error completions both release the next token.
            self.responses.append((line, response))
        self.token = None

    def command(self, line):
        return self.gcode.commands[line.split()[0]](WorkflowCommand(line))

    def primitive(self, name, command):
        if self.fail_primitive == name:
            self.fail_primitive = None
            raise CommandError('synthetic primitive refusal: ' + name)
        move, position = self.box.gcode_move, self.printer.position
        params = command.get_command_parameters()
        if name in ('G90', 'G91'):
            move.absolute_coord = name == 'G90'
        elif name in ('M82', 'M83'):
            move.absolute_extrude = name == 'M82'
        elif name in ('G0', 'G1'):
            before = list(position)
            for index, axis in enumerate('XYZE'):
                if axis not in params:
                    continue
                value = float(params[axis])
                absolute = move.absolute_coord and (axis != 'E' or move.absolute_extrude)
                position[index] = value if absolute else position[index] + value
            move.last_position = list(position)
            if before != position:
                self.moves.append(dict(token=self.token, line=command.get_commandline(),
                    before=before, after=list(position), completed_before=list(self.completed)))
        elif name == 'M400':
            self.completed[:] = position
            cfg = self.box.boxcfg
            active = (self.cut_callback and position[0] == cfg.pre_cut_pos_x
                      and position[1] == cfg.get_real_cut_pos_y())
            self.box.cut_sensor._button_handler(self.reactor.monotonic(), active)
        elif name in ('M104', 'M109'):
            self.printer.heater.target = float(params.get('S', 0))
            if name == 'M109':
                self.heat()
        elif name == 'G4':
            self.reactor.time += float(params.get('P', 0)) / 1000
        elif name not in ('RESPOND', 'SET_PIN', 'SET_VELOCITY_LIMIT', 'BED_MESH_CLEAR'):
            raise AssertionError('unmodeled workflow primitive: ' + name)

    def exchange(self, body, timeout, *args):
        self.native.calls.append((body, timeout, args))
        address, opcode, data = body[0], body[3], body[4:]
        self.native_events.append((self.token, opcode, data))
        self.events.append(('native', opcode, data))
        if self.on_native is not None:
            self.on_native(opcode, data)
        fault = self.faults.get((opcode, data))
        if isinstance(fault, Exception):
            raise fault
        if fault is not None:
            return reply(address, opcode, status=fault)
        result = b''
        if opcode == 0x0A:
            result = bytes((20, 30, 0, self.native.mode))
        elif opcode == 0x08:
            result = bytes((self.material if data == b'\x00' else self.connections,))
        elif opcode == 0x05:
            result = bytes((self.buffer,))
        elif opcode == 0x04:
            self.native.mode = 2 if data[1] == 0 else 0
        elif opcode == 0x10:
            if data[1] == 0:
                self.connections = data[0]
                result = b'\x00'
            elif data[1] == 4 and self.head_callback:
                self.buffer = 0
                self.set_head(True)
                self.printer.send_event('box:extrude_process_stage7')
            elif data[1] == 5:
                result = struct.pack('>f', -12.)
            elif data[1] == 7:
                self.buffer = 2
        elif opcode == 0x11 and data[1] == 1:
            self.connections, self.buffer = 0, 2
            if self.clear_head:
                self.set_head(False)
        return reply(address, opcode, data=result)


class MaterialWorkflowTests(unittest.TestCase):
    def fixture(self, **options):
        fixture = WorkflowFixture(**options)
        self.addCleanup(fixture.close)
        return fixture

    def test_owned_full_load_uses_one_positive_configured_flush_through_retained_alias(self):
        fixture = self.fixture(passive=True)
        box = fixture.box
        box.boxcfg.Tn_extrude = 150.
        source_before = deepcopy(box.store.base_data)
        result = fixture.command('CFS_TEST_LOAD TNN=T1C CONFIRM=1')
        self.assertEqual(result['target'], 'T1C')
        self.assertEqual(fixture.printer.position[3], 168)
        self.assertEqual([line for _, line in fixture.lines if line.startswith('BOX_MATERIAL_FLUSH_99')],
                         ['BOX_MATERIAL_FLUSH_99 LEN=150'])
        self.assertEqual([line for _, line in fixture.lines if line.startswith('G0 E150')], ['G0 E150.00 F360.00'])
        self.assertEqual(sum('E-5 ' in line for _, line in fixture.lines), 1)
        self.assertEqual([data[1] for _, opcode, data in fixture.native_events if opcode == 16], [0, 4, 6, 5])
        self.assertEqual(fixture.script_errors, [])
        self.assertEqual(box.store.base_data, source_before)
        self.assertEqual(box.last_cmd, 'T1C')
        self.assertFalse(box.material_trial.active)

    def test_prepared_load_then_quit_uses_the_same_full_state(self):
        fixture = self.fixture()
        box = fixture.box
        fixture.service(LOAD_RECIPE)
        self.assertIs(box, fixture.box)
        self.assertEqual(len(fixture.tokens), 13)
        self.assertEqual(fixture.printer.position, [150, 180, 30, -42])
        self.assertEqual(box.last_cmd, 'T1C')
        self.assertEqual([data[1] for _, opcode, data in fixture.native_events if opcode == 0x10],
                         [0, 4, 6, 5])
        for name in ('BOX_EXTRUDE_MATERIAL TNN=T1C', 'BOX_EXTRUDER_EXTRUDE TNN=T1C',
                     'BOX_MATERIAL_FLUSH_99'):
            self.assertEqual(sum(line == name for _, line in fixture.lines), 1)
        self.assertEqual(fixture.script_errors, [])
        start = len(fixture.moves)
        fixture.service(QUIT_RECIPE)
        self.assertEqual(len(fixture.tokens), 19)
        first_x = next(row for row in fixture.moves[start:]
                       if row['before'][0] != row['after'][0])
        self.assertEqual(first_x['before'][1], box.boxcfg.safe_pos_y)
        self.assertEqual(first_x['after'][0], box.boxcfg.pre_cut_pos_x)
        self.assertEqual(fixture.printer.position, [40, 140, 50, -100])
        self.assertEqual(fixture.completed, fixture.printer.position)
        self.assertIsNone(box.last_cmd)
        self.assertFalse(box.get_filament_sensor_detect())
        self.assertFalse(box.operations.active)
        self.assertEqual(fixture.script_errors, [])

    def test_existing_source_is_cut_and_retracted_before_the_new_load(self):
        fixture = self.fixture(loaded=True)
        fixture.service(LOAD_RECIPE)
        feeds = [(opcode, data) for _, opcode, data in fixture.native_events]
        first_feed = next(index for index, (opcode, _) in enumerate(feeds) if opcode == 0x10)
        self.assertIn((0x11, b'\x01\x00'), feeds[:first_feed])
        self.assertIn((0x11, b'\x01\x01'), feeds[:first_feed])
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertEqual(fixture.printer.position, [150, 180, 30, -100])
        self.assertEqual(fixture.completed[2], 50)
        self.assertEqual(fixture.lines[-1][1], 'G90')
        fixture.service(QUIT_RECIPE)
        self.assertIsNone(fixture.box.last_cmd)
        self.assertEqual(fixture.printer.position[3], -158)
        self.assertEqual(fixture.script_errors, [])

    def test_passive_unknown_source_trial_keeps_the_complete_cold_stock_order(self):
        fixture = self.fixture(unknown_source=True, passive=True)
        box, heater = fixture.box, fixture.printer.heater
        heater.smoothed_temp, heater.can_extrude = 20, False
        before = deepcopy(box.store.base_data)
        observations, source_updates = [], []
        box.update_filament_pos = lambda *args: source_updates.append(args)

        def observe(opcode, data):
            observations.append((box.last_cmd, box.last_cmd_tmp,
                                 box.box_state.get_Tn_data(1, 'filament')))

        fixture.on_native = observe
        result = fixture.command('CFS_TEST_UNLOAD TNN=T1C CONFIRM=1')
        self.assertEqual(result['target'], 'T1C')
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 0x11],
                         [b'\x00\x00', b'\x04\x01'])
        self.assertTrue(all(row == (None, None, 'None') for row in observations))
        self.assertEqual(source_updates, [])
        self.assertEqual(box.store.base_data, before)
        self.assertIsNone(box.last_cmd)
        self.assertIsNone(box.last_cmd_tmp)
        self.assertEqual(box.box_state.get_Tn_data(1, 'filament'), 'None')
        self.assertEqual(fixture.printer.position, [40, 140, 50, -50])
        self.assertEqual(fixture.completed, fixture.printer.position)
        self.assertFalse(fixture.lifecycle.head_present())
        self.assertFalse(box.cut_sensor.cut_present)
        self.assertEqual((fixture.connections, fixture.buffer, fixture.native.mode), (0, 2, 0))
        self.assertEqual(heater.target, box.boxcfg.Tn_extrude_temp)
        events = fixture.events
        cut = next(index for index, event in enumerate(events)
                   if event[0] == 'gcode' and event[1].startswith('G0 Y205.00 '))
        heat = next(index for index, event in enumerate(events)
                    if event[0] == 'gcode' and event[1].startswith(
                        'M109 S%d' % box.boxcfg.Tn_extrude_temp))
        shared = events.index(('native', 0x11, b'\x00\x00'))
        assist = next(index for index, event in enumerate(events)
                      if event[0] == 'gcode' and 'E-20.00' in event[1])
        pull = events.index(('gcode', 'G1 E-30 F2400'))
        material = events.index(('native', 0x11, b'\x04\x01'))
        park = next(index for index, event in enumerate(events)
                    if event[0] == 'gcode' and event[1].startswith('G0 Y140.00 '))
        self.assertEqual(sorted((cut, heat, shared, assist, pull, material, park)),
                         [cut, heat, shared, assist, pull, material, park])
        self.assertFalse(any('E-8 ' in line or line == 'M104 S0' for _, line in fixture.lines))
        first_x = next(move for move in fixture.moves if move['before'][0] != move['after'][0])
        self.assertEqual(first_x['before'][1], 150)
        self.assertEqual(first_x['after'][:2], [40, 150])
        self.assertTrue(fixture.lifecycle.passive)
        self.assertFalse(box.material_trial.active)
        self.assertEqual(fixture.script_errors, [])
        before_effects = (len(fixture.native_events), len(fixture.lines))
        with self.assertRaises(CommandError):
            fixture.command('BOX_RETRUDE_MATERIAL')
        self.assertEqual((len(fixture.native_events), len(fixture.lines)), before_effects)

    def test_passive_unknown_source_buffer_failure_stops_before_assists_and_return(self):
        fixture = self.fixture(unknown_source=True, passive=True)
        fixture.printer.heater.smoothed_temp = 20
        fixture.printer.heater.can_extrude = False
        fixture.faults[(0x11, b'\x00\x00')] = 2
        with self.assertRaises(CommandError):
            fixture.command('CFS_TEST_UNLOAD TNN=T1C CONFIRM=1')
        self.assertEqual([data for _, opcode, data in fixture.native_events if opcode == 0x11],
                         [b'\x00\x00'])
        self.assertFalse(any('E-20' in line or 'E-30' in line for _, line in fixture.lines))
        self.assertFalse(any(line.startswith('G0 Y140.00 ') for _, line in fixture.lines))
        self.assertIsNone(fixture.box.last_cmd)
        self.assertEqual(fixture.box.box_state.get_Tn_data(1, 'filament'), 'None')
        self.assertTrue(fixture.lifecycle.head_present())
        self.assertTrue(fixture.lifecycle.passive)
        self.assertFalse(fixture.box.material_trial.active)

    def test_flush_default_and_explicit_length_keep_distinct_parser_outcomes(self):
        for raw, expected_e, error in (('BOX_MATERIAL_FLUSH', -42, False),
                                       ('BOX_MATERIAL_FLUSH LEN=40', 58, False),
                                       ('BOX_MATERIAL_FLUSH LEN=-60', 23, True)):
            with self.subTest(raw=raw):
                fixture = self.fixture()
                fixture.dispatch('NEXT_FILAMENT TEMP=215')
                fixture.service(LOAD_RECIPE, flush=raw)
                self.assertEqual(fixture.printer.position[3], expected_e)
                response = next(result for line, result in fixture.responses if line == raw)
                self.assertEqual('error' in response, error)
                if error:
                    heated = [index for index, (token, line) in enumerate(fixture.lines)
                              if token == raw and line == 'M109 S215.0']
                    self.assertTrue(heated)
                    self.assertEqual(fixture.tokens[-1], 'RESTORE_POSITION')
                self.assertEqual(fixture.box.last_cmd, 'T1C')

    def test_flush_temperature_endpoints_reach_the_real_extrusion_action(self):
        for temperature in (180, 300):
            with self.subTest(temperature=temperature):
                fixture = self.fixture()
                calls = []
                original = fixture.box.extrusion.Tn_Extrude
                def observe(*args, **kwargs):
                    calls.append(args)
                    return original(*args, **kwargs)
                fixture.box.extrusion.Tn_Extrude = observe
                fixture.service(LOAD_RECIPE,
                    flush='BOX_MATERIAL_FLUSH LEN=2 TEMP=%d' % temperature)
                self.assertEqual(calls[-1][2], temperature)
                self.assertEqual(fixture.printer.position[3], 20)
                self.assertEqual(fixture.script_errors, [])

    def test_passive_lifecycle_rejects_the_feed_phase_without_effects(self):
        fixture = self.fixture()
        fixture.lifecycle.passive = True
        with self.assertRaisesRegex(RuntimeError, 'admission'):
            fixture.command('BOX_EXTRUDE_MATERIAL TNN=T1C')
        self.assertEqual(fixture.native_events, [])
        self.assertEqual(fixture.moves, [])
        self.assertEqual(fixture.printer.heater.target, 0)

    def test_failed_head_stage_prevents_the_final_feed_and_loaded_source(self):
        fixture = self.fixture()
        fixture.faults[(0x10, b'\x04\x06\x00')] = 13
        fixture.service(LOAD_RECIPE)
        self.assertIsNone(fixture.box.last_cmd)
        self.assertFalse(any(opcode == 0x05 for _, opcode, _ in fixture.native_events))
        self.assertFalse(any('E18.00' in line for _, line in fixture.lines))
        self.assertEqual(fixture.box.error, 'macro_box_extrude_err')
        self.assertEqual(fixture.tokens[-1], 'RESTORE_POSITION')
        self.assertFalse(fixture.box.operations.active)

    def test_service_continues_after_command_errors_and_internal_exceptions(self):
        for internal in (False, True):
            with self.subTest(internal=internal):
                fixture = self.fixture()
                if internal:
                    fixture.faults[(0x10, b'\x04\x05\x00')] = RuntimeError('synthetic native refusal')
                else:
                    fixture.fail_primitive = 'M104'
                fixture.service(LOAD_RECIPE)
                self.assertEqual(len(fixture.tokens), 13)
                self.assertEqual(fixture.tokens[-1], 'RESTORE_POSITION')
                self.assertTrue(fixture.script_errors)
                self.assertEqual(any('error' in result for _, result in fixture.responses), not internal)
                self.assertFalse(fixture.box.operations.active)
                self.assertFalse(fixture.box.filament_sensor_enable)
                if internal:
                    self.assertIsNone(fixture.box.last_cmd)

    def test_final_head_failure_is_reported_after_source_bookkeeping_clears(self):
        fixture = self.fixture()
        fixture.service(LOAD_RECIPE)
        fixture.clear_head = False
        fixture.service(QUIT_RECIPE)
        self.assertIsNone(fixture.box.last_cmd)
        self.assertTrue(fixture.box.get_filament_sensor_detect())
        self.assertEqual(fixture.box.error, 'macro_retrude_err')
        self.assertIn('BOX_RETRUDE_MATERIAL', fixture.box.error_next_macro)

    def test_cancelled_or_replaced_phase_cannot_send_later_feed_stages(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                fixture = self.fixture()
                def revoke(opcode, data):
                    if opcode == 0x10 and data[1] == 0:
                        if replacement:
                            fixture.box.device_versions.invalidate(1)
                        else:
                            fixture.box.operations.cancel()
                fixture.on_native = revoke
                fixture.service(('BOX_EXTRUDE_MATERIAL TNN={target}',))
                self.assertEqual([data[1] for _, opcode, data in fixture.native_events if opcode == 0x10], [0])
                self.assertIsNone(fixture.box.last_cmd)
                self.assertFalse(any('E5 ' in line or 'E18.00' in line for _, line in fixture.lines))
                self.assertTrue(fixture.script_errors)


if __name__ == '__main__':
    unittest.main()
