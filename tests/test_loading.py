from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.loading import Loading
from cfs.state import BoxState


class LoadingTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.stages = []
        self.move = SimpleNamespace(absolute_extrude=True)
        self.box = SimpleNamespace(
            box_state=BoxState(),
            host_guard=lambda *args: lambda: None,
            _printer=SimpleNamespace(lookup_object=lambda name: self.move),
            gcode=SimpleNamespace(run_script_from_command=self.commands.append),
            communication_extrude_process=lambda *args, **kwargs: self.stages.append((args, kwargs)) or False,
        )
        self.loading = Loading(self.box)

    def test_head_restores_only_absolute_extrusion(self):
        self.loading.move_head()
        self.assertEqual(self.commands, ["M83\n G0 E5 F60", "M82"])
        self.commands.clear()
        self.move.absolute_extrude = False
        self.loading.move_head()
        self.assertEqual(self.commands, ["M83\n G0 E5 F60"])

    def test_stage6_preserves_failure_and_target(self):
        self.assertIs(self.loading.stage6(2, "T2C"), False)
        self.assertEqual(self.stages, [((2, "C"), {"stage": 6})])
        with self.assertRaises(ValueError):
            self.loading.stage6(1, "T2C")

    def test_pickup_clears_useup(self):
        self.assertTrue(self.loading.extruder_extrude(None))
        self.assertEqual(self.box.box_state.get_Tn_data(0, uppart="filament_useup"), 0)

class Stage8Tests(unittest.TestCase):
    def setUp(self):
        self.commands, self.waits, self.requests, self.calls = [], [], [], []
        self.state = BoxState()
        self.state.modify_Tn_inner_data(0, 1, uppart="buffer")
        self.box = SimpleNamespace(
            box_state=self.state, boxcfg=SimpleNamespace(extrude_material_times=2,
                extrude_material_len_for_extruder=18, extrude_material_velocity=180,
                extrude_material_len_for_box=3),
            host_guard=lambda *args: lambda: None,
            _reactor=SimpleNamespace(monotonic=lambda: 10, pause=self.waits.append),
            gcode=SimpleNamespace(run_script_from_command=self.commands.append),
            send_native=self.send, last_replies={},
            communication_extrude_process=self.stage,
            extrude_process_state=None)
        self.result = True
        self.loading = Loading(self.box)

    def send(self, request):
        self.requests.append(request)
        self.box.last_replies[1] = SimpleNamespace(command=5, status=0, data=b"\x01")
        return True

    def stage(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result

    def test_integral_float_amount_encodes_exact_byte(self):
        self.box.boxcfg.extrude_material_len_for_box = 3.0
        self.assertIs(self.loading.box_extrude_material_stage8("T1A"), False)
        amount = self.calls[0][1]["extrude"]
        self.assertEqual(amount, 3)
        self.assertIs(type(amount), int)
        self.assertIs(type(self.box.boxcfg.extrude_material_len_for_box), float)

    def test_fractional_amount_fails_before_head_motion(self):
        self.box.boxcfg.extrude_material_len_for_box = 3.5
        with self.assertRaisesRegex(ValueError, "integral byte"):
            self.loading.box_extrude_material_stage8("T1A")
        self.assertEqual((self.commands, self.waits, self.requests, self.calls), ([], [], [], []))

    def test_full_buffer_exhausts_configured_attempts(self):
        self.assertIs(self.loading.box_extrude_material_stage8("T1A"), False)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], ((1, "A"), {"stage": 7, "extrude": 3, "report_err": False}))
        self.assertEqual(self.waits, [14, 10.5, 14, 10.5])

    def test_failed_query_does_not_accept_cached_buffer(self):
        self.state.modify_Tn_inner_data(0, 0, uppart="buffer")
        self.box.send_native = lambda request: False
        with self.assertRaisesRegex(RuntimeError, "fresh ordinary buffer observation required"):
            self.loading.box_extrude_material_stage8("T1A")
        self.assertEqual(self.calls, [])

    def test_err9_retries_five_times_then_fails(self):
        self.result = False
        self.box.extrude_process_state = "EXTRUDE_ERR9"
        self.assertIs(self.loading.box_extrude_material_stage8("T1A"), False)
        self.assertEqual(self.commands.count("G0 E6 F180"), 5)
        self.assertEqual(len(self.requests), 6)

class PartTests(unittest.TestCase):
    def setUp(self):
        self.stages, self.errors = [], []
        self.box = SimpleNamespace(
            box_state=BoxState(), host_guard=lambda *args: lambda: None,
            extrude_tnn=None, extrude_process_stage7_flag_when_retry_loose=False,
            extrude_process_stage7_flag=False, extrude_process_stage7_ret=None,
            extrude_process_state=None, error_tnn={},
            lifecycle=SimpleNamespace(report_error=lambda key, facts: self.errors.append(key)),
            _reactor=SimpleNamespace(monotonic=lambda: 0, pause=lambda _: None),
            communication_extrude_process=self.stage)
        self.loading = Loading(self.box)
        self.result = True

    def stage(self, address, slot, stage, **kwargs):
        self.stages.append(stage)
        return self.result

    def test_timeout_is_90_passes_and_stage5_even_only(self):
        self.assertIs(self.loading.box_extrude_material_part("T1A", [0, 0, 0, 0]), False)
        self.assertEqual(self.stages, [4] + [5] * 45)
        self.assertEqual(self.errors, ["key_extrude_err2"])

    def test_stage6_failure_snapshot_is_consumed(self):
        self.box.extrude_process_stage7_flag = True
        self.box.extrude_process_stage7_ret = {"ret": False, "extrude_process_ret_state": "EXTRUDE_ERR10"}
        self.assertIs(self.loading.box_extrude_material_part("T1A", [0, 0, 0, 0]), False)
        self.assertEqual(self.errors, ["key_extrude_err5"])
        self.assertIsNone(self.box.extrude_process_stage7_ret["extrude_process_ret_state"])

    def test_stage5_sensor_reason_invokes_matching_retry(self):
        def stage(address, slot, stage, **kwargs):
            if stage == 5:
                self.box.extrude_process_state = "EXTRUDE_ERR6"
                return False
            return True
        self.box.communication_extrude_process = stage
        self.loading.extrude_process_auto_retry_process = lambda target, reason, retry: (target, reason, retry)
        retry = [0, 0, 0, 0]
        self.assertEqual(self.loading.box_extrude_material_part("T1A", retry),
                         ("T1A", "auto_retry_filament_sensor", retry))

class LoadTests(unittest.TestCase):
    def test_success_sequences_stage0_part_print_and_ownership(self):
        events = []
        state = BoxState()
        state.modify_Tn_data(1, "connect", "state")
        operations = SimpleNamespace(_heartbeat_saved=None, _serial=1, active=True)
        operations.begin = lambda source, target, addr: events.append(("begin", source, target, addr))
        operations.arm_head = lambda: events.append("arm")
        operations.cancel = lambda: events.append("cancel")
        box = SimpleNamespace(
            host_guard=lambda *args: lambda: None, print_stats=SimpleNamespace(state="standby"),
            box_state=state, last_cmd=None, last_cmd_tmp=None, operations=operations,
            is_use_ending_material=False, heart_process_enable=[True] * 4,
            toolhead=SimpleNamespace(get_position=lambda: [0, 100, 0]),
            boxcfg=SimpleNamespace(safe_pos_y=50),
            motion=SimpleNamespace(go_to_extrude_pos=lambda: events.append("position")),
            wait_box_preloading=lambda addr: True,
            communication_tighten_up_enable=lambda addr, value: events.append(("tighten", addr, value)),
            is_material_available=lambda tnn: True,
            get_flush_velocity=lambda a, b: (1, 1), get_flush_temp=lambda a, b: (200, 210, 210),
            quickly_wait_heating=lambda temp: events.append(("heat", temp)),
            communication_extrude_process=lambda addr, slot, **kw: events.append(("feed", kw)) or True,
            communication_set_box_mode=lambda addr, mode, slot: events.append(("mode", mode, slot)) or True,
            update_filament_pos=lambda a, b: events.append(("filament", a, b)))
        loading = Loading(box)
        loading.box_extrude_material_part = lambda tnn, retry: events.append(("part", retry[:])) or True
        self.assertTrue(loading.box_extrude_material("T1A"))
        self.assertEqual(events, [("begin", None, "T1A", 1), ("tighten", 1, "DISABLE"),
            ("heat", 210), ("feed", {"stage": 0, "report_err": False}), "arm", ("part", [0, 0, 0, 0]),
            ("mode", "PRINT", "A"), ("tighten", 1, "ENABLE"), ("filament", "T1A", "T1A"), "cancel"])
        self.assertEqual(box.last_cmd, "T1A")

class RetryTests(unittest.TestCase):
    def test_second_sensor_mutates_counters_and_runs_ordered_recovery(self):
        actions = []
        box = SimpleNamespace(box_state=BoxState(), host_guard=lambda *args: lambda: None,
            communication_set_box_mode=lambda *args: actions.append(("mode", args)) or True,
            communication_retrude_process=lambda *args: actions.append(("retract", args)) or True,
            communication_extrude_process=lambda *args, **kw: actions.append(("feed", args, kw)) or True)
        loading = Loading(box)
        loading.box_extrude_material_part = lambda tnn, retry: actions.append(("part", retry[:])) or True
        retry = [0, 1, 0, 1]
        self.assertTrue(loading.extrude_process_auto_retry_process("T2C", "auto_retry_filament_sensor_second", retry))
        self.assertEqual(retry, [0, 0, 1, 1])
        self.assertEqual(actions, [("mode", (2, "IDLE")), ("retract", (2, "C", "MATERIAL")),
            ("feed", (2, "C"), {"stage": 0}), ("part", [0, 0, 1, 1])])

    def test_gear_retry_continues_after_false_retract(self):
        actions = []
        box = SimpleNamespace(box_state=BoxState(), host_guard=lambda *args: lambda: None,
            cutter=SimpleNamespace(move_to_cut=lambda: actions.append("cut")),
            box_retrude_material=lambda: actions.append("retract") or False,
            gcode=SimpleNamespace(run_script_from_command=actions.append),
            motion=SimpleNamespace(go_to_extrude_pos=lambda: actions.append("position")),
            communication_extrude_process=lambda *args, **kw: actions.append("feed") or True)
        loading = Loading(box)
        loading.box_extrude_material_part = lambda *args: actions.append("part") or True
        retry = [0, 1, 1, 0]
        self.assertTrue(loading.extrude_process_auto_retry_process("T1A", "auto_retry_extruder_gear", retry))
        self.assertEqual(retry, [0, 0, 0, 1])
        self.assertEqual(actions, ["cut", "retract", "M400", "position", "feed", "part"])

class HeadArrivalTests(unittest.TestCase):
    def test_event_during_stage4_consumes_success_snapshot(self):
        box = SimpleNamespace(box_state=BoxState(), host_guard=lambda *args: lambda: None,
            extrude_tnn=None, extrude_process_stage7_flag_when_retry_loose=False,
            extrude_process_stage7_flag=False, extrude_process_stage7_ret=None,
            extrude_process_state=None, error_tnn={})
        def feed(address, slot, stage, **kwargs):
            if stage == 4:
                box.extrude_process_stage7_flag = True
                box.extrude_process_stage7_ret = {"ret": True, "extrude_process_ret_state": None}
            return True
        box.communication_extrude_process = feed
        loading = Loading(box)
        loading.box_extrude_material_stage8 = lambda tnn: True
        self.assertTrue(loading.box_extrude_material_part("T1A", [0, 0, 0, 0]))
        self.assertFalse(box.extrude_process_stage7_flag)


class ExtraLoadingTests(unittest.TestCase):
    def test_connection_retry_uses_default_reports_and_keeps_counters(self):
        calls = []
        box = SimpleNamespace(box_state=BoxState(), host_guard=lambda *args: lambda: None,
            communication_set_box_mode=lambda *args: calls.append(("mode", args)) or True,
            communication_retrude_process=lambda *args: calls.append(("retract", args)) or True,
            communication_extrude_process=lambda *args, **kwargs: calls.append(("feed", args, kwargs)) or True)
        retry = [0, 0, 0, 0]
        self.assertTrue(Loading(box).extrude_process_auto_retry_process("T1A", "auto_retry_connection", retry))
        self.assertEqual(retry, [0, 0, 0, 0])
        self.assertEqual(calls, [("mode", (1, "IDLE")), ("retract", (1, "A", "MATERIAL")),
                                 ("feed", (1, "A"), {"stage": 0})])

    def test_first_sensor_increments_only_its_counter_after_loose(self):
        events = []
        box = SimpleNamespace(box_state=BoxState(), host_guard=lambda *args: lambda: None,
            make_material_loose=lambda: events.append("loose"))
        loading = Loading(box)
        loading.box_extrude_material_part = lambda target, retry: events.append(retry[:]) or True
        retry = [0, 0, 1, 1]
        self.assertTrue(loading.extrude_process_auto_retry_process("T1A", "auto_retry_filament_sensor", retry))
        self.assertEqual(events, ["loose", [0, 1, 1, 1]])

    def test_callback_during_pause_consumes_saved_state_not_clobbered_global(self):
        errors = []
        box = SimpleNamespace(box_state=BoxState(), host_guard=lambda *args: lambda: None,
            extrude_tnn=None, extrude_process_stage7_flag_when_retry_loose=False,
            extrude_process_stage7_flag=False, extrude_process_stage7_ret=None,
            extrude_process_state=None, error_tnn={},
            lifecycle=SimpleNamespace(report_error=lambda key, facts: errors.append(key)),
            communication_extrude_process=lambda *args, **kwargs: True)
        def pause(until):
            box.extrude_process_stage7_flag = True
            box.extrude_process_stage7_ret = {"ret": False, "extrude_process_ret_state": "EXTRUDE_ERR10"}
            box.extrude_process_state = "OTHER"
        box._reactor = SimpleNamespace(monotonic=lambda: 0, pause=pause)
        self.assertIs(Loading(box).box_extrude_material_part("T1A", [0, 0, 0, 0]), False)
        self.assertEqual(errors, ["key_extrude_err5"])


class UnavailableBufferTests(unittest.TestCase):
    setUp = Stage8Tests.setUp
    send = Stage8Tests.send
    stage = Stage8Tests.stage

    def test_event_reply_stops_without_gear_retry(self):
        self.box.last_replies[1] = SimpleNamespace(command=5, status=0, data=b"\x00")
        # Event delivery does not replace the last ordinary reply.
        self.box.send_native = lambda request: True
        self.box.extrude_tnn = None
        self.box.extrude_process_stage7_flag_when_retry_loose = False
        self.box.extrude_process_stage7_flag = True
        self.box.extrude_process_stage7_ret = {"ret": True, "extrude_process_ret_state": None}
        self.box.communication_extrude_process = lambda *args, **kwargs: True
        self.loading.extrude_process_auto_retry_process = lambda *args: self.fail("gear retry started")
        with self.assertRaisesRegex(RuntimeError, "fresh ordinary buffer observation required"):
            self.loading.box_extrude_material_part("T1A", [0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main()
