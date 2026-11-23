"""Check the finite speed ceiling only inside owned change/print scopes."""

from copy import deepcopy
import math
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from cfs.box import Box
from test_material_workflow import WorkflowFixture
from test_print_integration import PrintIntegrationFixture


class FlushVelocityCapTests(unittest.TestCase):
    def profile_box(self, velocity, percent, *, active=True, action='CHANGE', owner=True, printing=False):
        return SimpleNamespace(
            extrusion=SimpleNamespace(get_flush_velocity=lambda *args: (velocity, percent)),
            material_trial=SimpleNamespace(active=active, action=action, owns_dispatch=lambda: owner),
            print_trial=SimpleNamespace(owns_dispatch=lambda: printing))

    def fixture(self, *, passive=True, printing=False, speed=14):
        fixture = (PrintIntegrationFixture() if printing else
                   WorkflowFixture(loaded=True, passive=passive).prepare_toolchange(volume=200.))
        self.addCleanup(fixture.close)
        fixture.box.get_material_max_extrusion_speed = lambda tool: speed
        return fixture

    def configuration(self, fixture):
        return deepcopy({key: value for key, value in vars(fixture.box.boxcfg).items() if key != 'config'})

    def positive_feeds(self, fixture):
        return [tuple(map(float, match.groups())) for _, line in fixture.lines
                for match in [re.fullmatch(r'G[01] E([0-9.]+) F([0-9.]+)', line)] if match]

    def test_owned_profiles_cap_each_finite_velocity_and_preserve_percent_and_input(self):
        velocity, percent = (120, 360, 840), (.2, .3, .5)
        for printing in (False, True):
            with self.subTest(printing=printing):
                box = self.profile_box(velocity, percent, active=not printing, printing=printing)
                values, ratios = Box.get_flush_velocity(box, 'T1A', 'T1C')
                self.assertEqual(values, [120, 360, 360])
                self.assertIs(ratios, percent)
                self.assertEqual(velocity, (120, 360, 840))

    def test_other_actions_inactive_and_unrelated_dispatch_keep_original_profile_objects(self):
        cases = ({'active': False}, {'owner': False}, {'action': 'LOAD'}, {'action': 'UNLOAD'})
        for case in cases:
            with self.subTest(case=case):
                velocity, percent = [840, 240], [.75, .25]
                box = self.profile_box(velocity, percent, **case)
                values, ratios = Box.get_flush_velocity(box, 'T1A', 'T1C')
                self.assertIs(values, velocity)
                self.assertIs(ratios, percent)

    def test_cap_does_not_normalize_invalid_profile_values(self):
        for value in (float('nan'), float('inf'), -float('inf'), 0, -20, True, '840'):
            with self.subTest(value=value):
                velocity, percent = [value], [1]
                box = self.profile_box(velocity, percent)
                values, ratios = Box.get_flush_velocity(box, 'T1A', 'T1C')
                if isinstance(value, float) and math.isnan(value):
                    self.assertTrue(math.isnan(values[0]))
                else:
                    self.assertEqual(values[0], value)
                self.assertIs(ratios, percent)

    def test_real_ordinary_getter_retains_material_speed_without_motion_or_writes(self):
        fixture = self.fixture(passive=False)
        before = self.configuration(fixture), list(fixture.printer.position), deepcopy(fixture.box.box_state.Tnn_map)
        self.assertEqual(fixture.box.get_flush_velocity('T1A', 'T1C'), ([840], [1]))
        self.assertEqual((self.configuration(fixture), fixture.printer.position,
                          fixture.box.box_state.Tnn_map), before)
        self.assertEqual((fixture.lines, fixture.native_events), ([], []))

    def test_baseline_active_tool_change_caps_effective_speed_without_trial(self):
        fixture = self.fixture(passive=False, speed=14)
        fixture.box.gcode_move.get_status = lambda now: {"speed_factor": 2., "extrude_factor": 1.}
        fixture.command('T2')
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertIn((90., 180.), self.positive_feeds(fixture))
        self.assertIn((80., 180.), self.positive_feeds(fixture))
        self.assertEqual(fixture.script_errors, [])

    def test_real_change_preflight_and_finishing_share_cap_without_changing_xyz_or_segments(self):
        results = []
        for speed in (6, 14):
            fixture = self.fixture(speed=speed)
            config = self.configuration(fixture)
            result = fixture.command('CFS_TEST_TOOL_CHANGE TOOL=T2 CONFIRM=1')
            self.assertEqual(result['target'], 'T1C')
            purge = fixture.box.material_trial._change_purge
            self.assertEqual(purge[1:3], ((360,), (1,)))
            self.assertEqual(purge[3], (90., 80.))
            self.assertIn((90., 360.), self.positive_feeds(fixture))
            self.assertIn((80., 360.), self.positive_feeds(fixture))
            self.assertEqual(self.configuration(fixture), config)
            self.assertTrue(fixture.lifecycle.passive)
            self.assertEqual(fixture.script_errors, [])
            results.append((purge, [row['before'][:3] + row['after'][:3] for row in fixture.moves],
                            list(fixture.printer.position), list(fixture.native_events)))
        self.assertEqual(results[0], results[1])

    def test_real_print_worker_uses_same_capped_preflight_and_finishing(self):
        fixture = self.fixture(printing=True)
        config = self.configuration(fixture)
        fixture.start()
        fixture.command('BOX_START_PRINT')
        fixture.command('T0')
        fixture.command('T2')
        purge = fixture.box.print_trial._change_purge
        self.assertEqual(purge[1:3], ((360,), (1,)))
        self.assertIn((90., 360.), self.positive_feeds(fixture))
        self.assertIn((80., 360.), self.positive_feeds(fixture))
        self.assertEqual(fixture.box.last_cmd, 'T1C')
        self.assertEqual(self.configuration(fixture), config)
        self.assertTrue(fixture.lifecycle.passive)
        self.assertEqual(fixture.script_errors, [])

    def test_real_change_rejects_nonfinite_speed_before_cut_or_native_writes(self):
        for speed in (float('inf'), float('nan'), -1, 0):
            with self.subTest(speed=speed):
                fixture = self.fixture(speed=speed)
                with self.assertRaises((ValueError, RuntimeError)):
                    fixture.command('CFS_TEST_TOOL_CHANGE TOOL=T2 CONFIRM=1')
                self.assertEqual((fixture.lines, fixture.native_events), ([], []))
                self.assertEqual(fixture.box.last_cmd, 'T1A')
                self.assertTrue(fixture.lifecycle.passive)


if __name__ == '__main__':
    unittest.main()
