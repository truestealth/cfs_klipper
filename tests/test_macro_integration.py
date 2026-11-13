"""Exercise synthetic macro fixtures with Jinja and a bounded Klipper model."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

from macro_harness import Host, Model, load_module
from test_configuration import FakeConfig
from test_box_data import CommandError, Webhooks
import test_recovery_actions

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cfs.box import Box


class Status:
    def __init__(self, **values):
        self.values = values

    def get_status(self, eventtime):
        return self.values


class MacroIntegrationTests(unittest.TestCase):
    def setUp(self):
        path = os.environ.get('KLIPPER_GCODE_MACRO')
        self.host = Host(load_module(path) if path else Model)

    def test_connect_renames_existing_and_forwards_raw_parameters(self):
        seen = []
        self.host.gcode.register_command('FEED', lambda cmd: seen.append(cmd.raw))
        self.host.macro('FEED', {'rename_existing': 'FEED_BASE',
                                 'gcode': 'FEED_BASE {rawparams}'})
        self.assertNotIn('FEED_BASE', self.host.gcode.commands)
        self.host.connect()
        self.host.gcode.run_script_from_command('FEED LENGTH=12 RATE=3')
        self.assertEqual(seen, ['LENGTH=12 RATE=3'])

    def test_macro_is_rendered_before_commands_and_nested_macro_rerenders(self):
        self.host.macro('STATE', {'variable_value': '0', 'gcode': ''})
        self.host.macro('INNER', {'gcode': 'OBSERVE VALUE={printer["gcode_macro STATE"].value}'})
        self.host.macro('OUTER', {'gcode': '''
SET_GCODE_VARIABLE MACRO=STATE VARIABLE=value VALUE=1
OBSERVE VALUE={printer["gcode_macro STATE"].value}
INNER
'''})
        self.host.gcode.run_script_from_command('OUTER')
        self.assertEqual([raw for name, raw in self.host.gcode.lines if name == 'OBSERVE'],
                         ['VALUE=0', 'VALUE=1'])

    def test_temperature_default_and_positive_forward_raw_arguments(self):
        seen = []
        self.host.gcode.register_command('FEED', lambda cmd: seen.append(cmd.raw))
        self.host.macro('PREFS', {'variable_temp': '0', 'gcode':
            'SET_GCODE_VARIABLE MACRO=PREFS VARIABLE=temp VALUE={params.TEMP|default(0)|float}'})
        self.host.gcode.register_command('PURGE', lambda cmd: seen.append(cmd.raw))
        for name in ('FEED', 'PURGE'):
            self.host.macro(name, {'rename_existing': name + '_BASE', 'gcode':
                '{% if printer["gcode_macro PREFS"].temp > 0 %}M109 S{printer["gcode_macro PREFS"].temp}\n{% endif %}' + name + '_BASE {rawparams}'})
        self.host.connect()
        self.host.gcode.run_script_from_command('PREFS')
        self.host.gcode.run_script_from_command('FEED VALUE=1')
        self.host.gcode.run_script_from_command('PREFS TEMP=210')
        self.host.gcode.run_script_from_command('FEED VALUE=2 RATE=3')
        self.host.gcode.run_script_from_command('PURGE LENGTH=4')
        self.assertEqual(seen, ['VALUE=1', 'VALUE=2 RATE=3', 'LENGTH=4'])
        self.assertEqual([raw for name, raw in self.host.gcode.lines if name == 'M109'], ['S210.0', 'S210.0'])

    def test_synthetic_start_index_boundaries_fail_before_dispatch(self):
        self.host.macro('START', {'gcode': """{% set tool = params.TOOL|default(-1)|int %}
{% if tool < 0 or tool > 15 %}{action_raise_error('bad tool')}{% endif %}
PREPARE
T{tool}
"""})
        for raw in ('', 'TOOL=-1', 'TOOL=16'):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                self.host.gcode.run_script_from_command('START ' + raw)
        self.assertNotIn('PREPARE', [name for name, _ in self.host.gcode.lines])
        for value in range(16):
            self.host.gcode.run_script_from_command('START TOOL=' + str(value))
            self.assertIn(('T' + str(value), ''), self.host.gcode.lines)

    def test_pause_default_and_no_post_work(self):
        state = Status(paused=False)
        self.host.objects['pause_resume'] = state
        self.host.gcode.register_command('PAUSE', lambda cmd: state.values.update(paused=True))
        self.host.macro('PAUSE', {'rename_existing': 'PAUSE_BASE', 'gcode': """
{% if not printer.pause_resume.paused %}
PAUSE_BASE
{% if params.POST|default(1)|int %}AFTER_PAUSE{% endif %}
{% endif %}"""})
        self.host.connect()
        self.host.gcode.run_script_from_command('PAUSE POST=0')
        self.host.gcode.run_script_from_command('PAUSE')
        self.assertNotIn('AFTER_PAUSE', [name for name, _ in self.host.gcode.lines])
        state.values['paused'] = False
        self.host.gcode.run_script_from_command('PAUSE')
        self.assertIn('AFTER_PAUSE', [name for name, _ in self.host.gcode.lines])

    def test_fan_and_homing_use_render_snapshot(self):
        status = Status(fan=0.5, homed=True)
        self.host.objects['fixture'] = status
        self.host.gcode.register_command('CHANGE', lambda cmd: status.values.update(fan=0, homed=False))
        self.host.macro('RESTORE', {'gcode': """CHANGE
{% if printer.fixture.homed %}MOVE{% endif %}
{% if printer.fixture.fan > 0 %}FAN VALUE={printer.fixture.fan}{% endif %}"""})
        self.host.gcode.run_script_from_command('RESTORE')
        self.assertIn(('MOVE', ''), self.host.gcode.lines)
        self.assertIn(('FAN', 'VALUE=0.5'), self.host.gcode.lines)

    def test_fan_save_restore_and_conditional_homing(self):
        self.host.objects['fixture'] = Status(fan=0.5, homed=False)
        self.host.macro('FAN_CLOSE', {'variable_saved': '0', 'gcode': """
SAVE_STATE
{% if printer.fixture.fan > 0 %}
SET_GCODE_VARIABLE MACRO=FAN_CLOSE VARIABLE=saved VALUE={printer.fixture.fan}
FAN_OFF
{% endif %}"""})
        self.host.macro('FAN_RESTORE', {'gcode': """
RESTORE_STATE
{% if printer['gcode_macro FAN_CLOSE'].saved > 0 %}
FAN_ON VALUE={printer['gcode_macro FAN_CLOSE'].saved}
SET_GCODE_VARIABLE MACRO=FAN_CLOSE VARIABLE=saved VALUE=0
{% endif %}"""})
        self.host.macro('HOME_IF_NEEDED', {'gcode': '{% if not printer.fixture.homed %}G28{% endif %}'})
        self.host.gcode.run_script_from_command('FAN_CLOSE')
        self.host.gcode.run_script_from_command('FAN_RESTORE')
        self.host.gcode.run_script_from_command('HOME_IF_NEEDED')
        self.assertEqual(self.host.objects['gcode_macro FAN_CLOSE'].variables['saved'], 0)
        names = [name for name, _ in self.host.gcode.lines]
        self.assertLess(names.index('SAVE_STATE'), names.index('FAN_OFF'))
        self.assertLess(names.index('RESTORE_STATE'), names.index('FAN_ON'))
        self.assertIn(('FAN_ON', 'VALUE=0.5'), self.host.gcode.lines)
        self.assertIn(('G28', ''), self.host.gcode.lines)
        self.host.objects['fixture'].values.update(fan=0, homed=True)
        self.host.gcode.lines.clear()
        for command in ('FAN_CLOSE', 'FAN_RESTORE', 'HOME_IF_NEEDED'):
            self.host.gcode.run_script_from_command(command)
        self.assertNotIn('G28', [name for name, _ in self.host.gcode.lines])
        self.assertNotIn('FAN_ON', [name for name, _ in self.host.gcode.lines])

    def test_comments_do_not_dispatch_commands(self):
        self.host.macro('COMMENTS', {'gcode': '# IGNORED\n; IGNORED_TOO\nACTIVE'})
        self.host.gcode.run_script_from_command('COMMENTS')
        self.assertEqual(self.host.gcode.lines, [('COMMENTS', ''), ('ACTIVE', '')])

    def test_refresh_dispatches_box_native_requests_in_order(self):
        fixture = test_recovery_actions.RecoveryActionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.make_box()
        for name in ('BOX_SET_PRE_LOADING', 'BOX_GET_RFID', 'BOX_GET_REMAIN_LEN'):
            self.host.gcode.register_command(name, lambda cmd, name=name: fixture.command(name, **cmd.params))
        self.host.macro('REFRESH', {'gcode': """
BOX_SET_PRE_LOADING ADDR={params.ADDR} NUM={params.NUM} ACTION=RUN
M400
BOX_GET_RFID ADDR={params.ADDR} NUM={params.NUM}
M400
BOX_GET_REMAIN_LEN ADDR={params.ADDR} NUM={params.NUM}
"""})
        self.host.gcode.run_script_from_command('REFRESH ADDR=1 NUM=2')
        self.assertEqual(fixture.native.commands(),
                         [(1, 0x0d, b'\x02\x02'), (1, 0x02, b'\x02'), (1, 0x03, b'\x02')])
        for raw in ('ADDR=1', 'NUM=2'):
            with self.subTest(raw=raw), self.assertRaises(CommandError):
                self.host.gcode.run_script_from_command('REFRESH ' + raw)

    def test_end_and_cancel_dispatch_cleanup_before_base(self):
        self.host.gcode.register_command('CANCEL', lambda cmd: None)
        self.host.macro('CLEANUP', {'gcode': 'FAN_AUTO\nBOX_END\nBOX_END_PRINT\nCLEAN\nBOX_STATE'})
        self.host.macro('CANCEL', {'rename_existing': 'CANCEL_BASE', 'gcode': 'CLEANUP\nCANCEL_BASE'})
        self.host.macro('END', {'gcode': 'CLEANUP\nM84'})
        self.host.connect()
        self.host.gcode.run_script_from_command('CANCEL')
        names = [name for name, _ in self.host.gcode.lines]
        self.assertLess(names.index('BOX_END'), names.index('BOX_END_PRINT'))
        self.assertLess(names.index('FAN_AUTO'), names.index('BOX_END'))
        self.assertLess(names.index('BOX_STATE'), names.index('CANCEL_BASE'))
        self.host.gcode.lines.clear()
        self.host.gcode.run_script_from_command('END')
        names = [name for name, _ in self.host.gcode.lines]
        self.assertLess(names.index('BOX_STATE'), names.index('M84'))

    def test_resume_order_velocity_and_snapshot(self):
        state = Status(temp=200, fan=0.5, enable=0, safe_y=100, current_y=120, z=5)
        self.host.objects['fixture'] = state
        self.host.gcode.register_command('CLEAR', lambda cmd: state.values.update(temp=0, fan=0))
        self.host.gcode.register_command('RESUME', lambda cmd: None)
        self.host.macro('RESUME', {'rename_existing': 'RESUME_BASE', 'gcode': """
CLEAR
RECOVER
{% if printer.fixture.temp > 0 %}HEAT VALUE={printer.fixture.temp}{% endif %}
{% if printer.fixture.fan > 0 %}FAN VALUE={printer.fixture.fan}{% endif %}
{% if printer.fixture.current_y > printer.fixture.safe_y %}SAFE_Y VALUE={printer.fixture.safe_y}{% endif %}
{% if printer.fixture.z > 2 %}Z_DOWN VALUE={printer.fixture.z - 2}{% endif %}
{% if printer.fixture.enable == 0 %}ORDINARY_EXTRUDE{% endif %}
RESUME_BASE VELOCITY={params.VELOCITY|default(10)}
"""})
        self.host.connect()
        self.host.gcode.run_script_from_command('RESUME VELOCITY=25')
        lines = self.host.gcode.lines
        self.assertLess(lines.index(('CLEAR', '')), lines.index(('RECOVER', '')))
        self.assertIn(('HEAT', 'VALUE=200'), lines)
        self.assertIn(('FAN', 'VALUE=0.5'), lines)
        self.assertIn(('SAFE_Y', 'VALUE=100'), lines)
        self.assertIn(('Z_DOWN', 'VALUE=3'), lines)
        self.assertIn(('ORDINARY_EXTRUDE', ''), lines)
        self.assertEqual(lines[-1], ('RESUME_BASE', 'VELOCITY=25'))
        state.values.update(enable=1, current_y=50, z=0)
        self.host.gcode.lines.clear()
        self.host.gcode.run_script_from_command('RESUME')
        self.assertEqual(self.host.gcode.lines[-1], ('RESUME_BASE', 'VELOCITY=10'))
        self.assertNotIn('ORDINARY_EXTRUDE', [name for name, _ in self.host.gcode.lines])
        self.assertNotIn('SAFE_Y', [name for name, _ in self.host.gcode.lines])
        self.assertNotIn('Z_DOWN', [name for name, _ in self.host.gcode.lines])

    def test_primary_and_head_runout_route_by_enable(self):
        status = Status(enable=1, extrudable=True)
        self.host.objects['fixture'] = status
        self.host.macro('PRIMARY_RUNOUT', {'gcode': """
{% if printer.fixture.enable == 1 %}REFILL
{% elif printer.fixture.enable == 0 %}PAUSE
{% if printer.fixture.extrudable %}EXTRUDE{% endif %}
{% else %}{action_raise_error('unknown owner')}{% endif %}
"""})
        self.host.macro('HEAD_RUNOUT', {'gcode': 'REFILL'})
        self.host.gcode.run_script_from_command('PRIMARY_RUNOUT')
        self.host.gcode.run_script_from_command('HEAD_RUNOUT')
        self.assertEqual([name for name, _ in self.host.gcode.lines].count('REFILL'), 2)
        status.values.update(enable=0, extrudable=False)
        self.host.gcode.lines.clear()
        self.host.gcode.run_script_from_command('PRIMARY_RUNOUT')
        self.assertIn(('PAUSE', ''), self.host.gcode.lines)
        self.assertNotIn(('EXTRUDE', ''), self.host.gcode.lines)
        status.values['extrudable'] = True
        self.host.gcode.lines.clear()
        self.host.gcode.run_script_from_command('PRIMARY_RUNOUT')
        self.assertIn(('EXTRUDE', ''), self.host.gcode.lines)
        status.values['enable'] = -1
        with self.assertRaises(RuntimeError):
            self.host.gcode.run_script_from_command('PRIMARY_RUNOUT')

    def test_diagnostic_loop_uses_registered_tools_without_name_collision(self):
        fixture = test_recovery_actions.RecoveryActionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.make_box()
        called = []
        fixture.box.Tn_action = called.append
        for index in range(4):
            name = 'T' + str(index)
            self.host.gcode.register_command(name, lambda cmd, name=name: fixture.command(name, **cmd.params))
        self.assertNotIn('TEST_TOOLS', fixture.gcode.commands)
        self.host.macro('TEST_TOOLS', {'gcode': '{% for i in range(4) %}T{i}\nM400\n{% endfor %}'})
        self.host.gcode.run_script_from_command('TEST_TOOLS')
        self.assertEqual(called, [fixture.box.box_state.Tnn_map[fixture.box.box_state.Tn_map['T' + str(i)]] for i in range(4)])

    def test_manual_phase_wrappers_dispatch_in_order(self):
        self.host.macro('LOAD_PRESENT', {'gcode': 'CLEAR\nCUT\nRETRACT\nLOAD\nFLUSH'})
        self.host.macro('LOAD_EMPTY', {'gcode': 'LOAD\nFLUSH'})
        self.host.gcode.run_script_from_command('LOAD_PRESENT')
        self.assertEqual([name for name, _ in self.host.gcode.lines][1:],
                         ['CLEAR', 'CUT', 'RETRACT', 'LOAD', 'FLUSH'])
        self.host.gcode.lines.clear()
        self.host.gcode.run_script_from_command('LOAD_EMPTY')
        self.assertEqual([name for name, _ in self.host.gcode.lines][1:], ['LOAD', 'FLUSH'])

    def test_external_macro_keeps_owner_after_box_registration(self):
        fixture = test_recovery_actions.RecoveryActionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.make_box()
        self.assertNotIn('MATERIAL_ADMIN', fixture.gcode.commands)
        self.host.macro('MATERIAL_ADMIN', {'gcode': 'RUN_EXTERNAL'})
        self.host.gcode.run_script_from_command('MATERIAL_ADMIN')
        self.assertIn(('RUN_EXTERNAL', ''), self.host.gcode.lines)

    def test_macro_dispatches_registered_box_data_command(self):
        with tempfile.TemporaryDirectory() as directory:
            box = Box(FakeConfig({'bus': 'stock'}), directory, config_paths=[])
            box.register_data_commands(self.host.gcode, Webhooks())
            self.host.macro('ENABLE_BOX', {'gcode': 'BOX_ENABLE_CFS_PRINT ENABLE={params.ENABLE}'})
            self.host.gcode.run_script_from_command('ENABLE_BOX ENABLE=1')
            self.assertEqual(box.box_state.Tn_data['enable'], 1)

    def test_missing_base_fails_at_connect(self):
        self.host.macro('FEED', {'rename_existing': 'FEED_BASE', 'gcode': ''})
        with self.assertRaises(RuntimeError):
            self.host.connect()


if __name__ == '__main__':
    unittest.main()
