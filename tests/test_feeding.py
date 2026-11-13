from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from types import SimpleNamespace

from cfs.feeding import Feeding
from cfs.native import UnsupportedRequest


class FakeBox:
    def __init__(self):
        self.print_stats = SimpleNamespace(state="idle")
        self.boxcfg = SimpleNamespace(version=1)
        self.last_cmd = self.error = self.last_err = None
        self.box_filament_err = self.is_use_ending_material = False
        self.filament_error_message = None
        self.head = False
        self.requests = []
        self.box_state = SimpleNamespace(get_box_mode=lambda addr: self.mode,
                                         Tn_data={"T%d" % i: {"state": "connect"} for i in range(1, 5)})
        self.mode = "1"
        self._reactor = SimpleNamespace(monotonic=lambda: 0, pause=lambda deadline: None)
    def host_guard(self, addr=None):
        return lambda: None
    def get_filament_sensor_detect(self):
        return self.head
    def send_native(self, request, context=None, report_err=True):
        self.requests.append((request, SimpleNamespace(report_err=report_err)))
        return True


class FeedingTests(unittest.TestCase):
    def setUp(self):
        self.box = FakeBox()
        self.feeding = Feeding(self.box)

    def test_preload_gates_and_timeout(self):
        for state, force, sent in (("printing", True, False), ("pause", False, False),
                                   ("pause", True, True), ("paused", False, True)):
            self.box.print_stats.state = state
            before = len(self.box.requests)
            self.feeding.communication_set_pre_loading(1, "OPEN", force=force)
            self.assertEqual(len(self.box.requests) > before, sent)
        self.box.print_stats.state = "idle"
        self.box.head = True
        self.assertIsNone(self.feeding.communication_set_pre_loading(1, "RUN", 3))
        self.box.head = False
        self.feeding.communication_set_pre_loading(1, "RUN", 3)
        self.assertEqual(self.box.requests[-1][0].timeout, 600)
        self.assertEqual(self.box.requests[-1][0].data, b"\x03\x02")

    def test_wait_and_native_payloads(self):
        self.assertTrue(self.feeding.wait_box_preloading(1))
        self.assertEqual(len(self.box.requests), 31)
        self.feeding.communication_extrude_process(1, "C", stage=6, report_err=False, extrude=7)
        request, context = self.box.requests[-1]
        self.assertEqual(request.data, b"\x04\x06\x07")
        self.assertFalse(context.report_err)
        self.feeding.communication_retrude_process(1, "0", "MATERIAL")
        self.assertEqual(self.box.requests[-1][0].data, b"\x00\x01")
        self.feeding.communication_tighten_up_enable(1, "ENABLE")
        self.assertEqual(self.box.requests[-1][0].data, b"\x00")
        self.box.boxcfg.version = 0
        with self.assertRaises(UnsupportedRequest):
            self.feeding.communication_extrude_process(1, "A")


class CommandTests(unittest.TestCase):
    def test_preference_before_skipped_uart_and_stage_positional(self):
        class Gcmd:
            def __init__(self, params): self.params = params
            def get_int(self, name, default=None, **kwargs): return int(self.params.get(name, default))
            def get_command_parameters(self): return self.params
        box = FakeBox()
        box.print_stats.state = "printing"
        feeding = Feeding(box)
        feeding.cmd_set_pre_loading(Gcmd({"ACTION": "OPEN", "POWER_ON": "DISABLE"}))
        self.assertTrue(box.preloading_en)
        self.assertFalse(box.preloading_poweron)
        self.assertEqual(box.requests, [])
        feeding.cmd_extrude_process(Gcmd({"ADDR": 1, "NUM": "A", "STAGE": 7}))
        self.assertEqual(box.requests[-1][0].data, b"\x01\x02\x00")
        with self.assertRaises(UnsupportedRequest):
            feeding.cmd_extrude_2_process(Gcmd({"ADDR": 1, "NUM": "A", "TRIGGER": "BUFFER"}))
        self.assertEqual(len(box.requests), 1)


class LooseTests(unittest.TestCase):
    def test_noop_and_motion_normal_tail(self):
        box = FakeBox()
        moves = []
        box.get_purge_in_prime_tower = lambda: True
        feeding = Feeding(box)
        feeding.make_material_loose()
        box.get_purge_in_prime_tower = lambda: False
        box.boxcfg.config = SimpleNamespace(getsection=lambda name: SimpleNamespace(getfloat=lambda name: 250))
        box.motion = SimpleNamespace(_positions=lambda *names: (100, 2000),
                                     go_to_extrude_pos=lambda: moves.append("return"))
        box.gcode = SimpleNamespace(run_script_from_command=moves.append)
        feeding.make_material_loose()
        self.assertEqual(moves, ["G90\n G0 Y100.00 F5000.00", "M400",
                                 "G90\n G0 X240.00 Y90.00 F2000.00",
                                 "G90\n G0 X240.00 Y45.00 F2000.00",
                                 "G90\n G0 X240.00 Y90.00 F2000.00", "return"])
        self.assertFalse(box.extrude_process_inside_retry_loose)

    def test_failed_query_returns_immediately(self):
        box = FakeBox()
        box.send_native = lambda request: False
        self.assertIs(Feeding(box).wait_box_preloading(1), False)

class BranchTests(unittest.TestCase):
    class Gcmd:
        def __init__(self, **params): self.params = params
        def get_int(self, name, default=None, **kwargs): return int(self.params.get(name, default))
        def get_command_parameters(self): return self.params

    def test_preload_all_print_and_sensor_branches(self):
        for state in ("printing", "pause", "paused", "idle"):
            for force in (False, True):
                box = FakeBox()
                box.print_stats.state = state
                Feeding(box).communication_set_pre_loading(1, 1, force=force)
                expected = state != "printing" and (state != "pause" or force)
                self.assertEqual(bool(box.requests), expected, (state, force))
        for action in (2, 3):
            for present in (False, True):
                box = FakeBox()
                box.head = present
                Feeding(box).communication_set_pre_loading(1, action, 15)
                self.assertEqual(bool(box.requests), not present)
                if not present:
                    self.assertEqual(box.requests[-1][0].timeout, 1200)
        for mask, seconds in ((0, 0), (1, 300), (5, 600), (15, 1200)):
            box = FakeBox()
            Feeding(box).communication_set_pre_loading(1, 2, mask)
            self.assertEqual(box.requests[0][0].timeout, seconds)

    def test_broadcast_and_preference_before_failure(self):
        box = FakeBox()
        box.box_state.Tn_data["T2"]["state"] = "disconnect"
        feeding = Feeding(box)
        feeding.cmd_set_pre_loading(self.Gcmd(ADDR=3, ACTION="OPEN", NUM=5))
        self.assertEqual([r.address for r, _ in box.requests], [1, 3, 4])
        self.assertTrue(box.preloading_en)
        box.requests.clear()
        feeding.cmd_set_pre_loading(self.Gcmd(ADDR=3, ACTION="CLOSE"))
        self.assertEqual([r.address for r, _ in box.requests], [1, 3, 4])
        self.assertFalse(box.preloading_en)
        box.host_guard = lambda addr=None: (_ for _ in ()).throw(RuntimeError("stale"))
        with self.assertRaisesRegex(RuntimeError, "stale"):
            feeding.cmd_set_pre_loading(self.Gcmd(ACTION="OPEN", POWER_ON="ENABLE"))
        self.assertTrue(box.preloading_en)
        self.assertTrue(box.preloading_poweron)

    def test_retract_and_tighten_return_sender_results(self):
        for result in (True, False, None):
            box = FakeBox()
            calls = []
            def send(request, **kwargs):
                calls.append((request, kwargs))
                return result
            box.send_native = send
            feeding = Feeding(box)
            self.assertIs(feeding.communication_retrude_process(1, "B", "BUFFER", False), result)
            self.assertEqual(calls[-1][0].data, b"\x02\x00")
            self.assertFalse(calls[-1][1]["report_err"])
            self.assertIs(feeding.communication_tighten_up_enable(1, "DISABLE"), result)
            self.assertEqual(calls[-1][0].data, b"\x01")

    def test_wait_query_event_mode_and_guard(self):
        box = FakeBox()
        pauses = []
        box._reactor.pause = pauses.append
        def send(request):
            box.requests.append(request)
            if len(box.requests) == 2: box.mode = "0"
            return True
        box.send_native = send
        self.assertTrue(Feeding(box).wait_box_preloading(1))
        self.assertEqual(len(box.requests), 2)
        self.assertEqual(len(pauses), 1)
        box = FakeBox()
        box.send_native = lambda request: None
        self.assertIsNone(Feeding(box).wait_box_preloading(1))
        box = FakeBox()
        checks = []
        def guard(addr):
            def check():
                checks.append(addr)
                raise RuntimeError("stale")
            return check
        box.host_guard = guard
        with self.assertRaisesRegex(RuntimeError, "stale"):
            Feeding(box).wait_box_preloading(1)
        self.assertEqual(len(box.requests), 1)
        self.assertEqual(checks, [1])

    def test_loose_failure_preserves_latch_and_stops(self):
        box = FakeBox()
        box.get_purge_in_prime_tower = lambda: False
        box.boxcfg.config = SimpleNamespace(getsection=lambda name: SimpleNamespace(getfloat=lambda name: 250))
        box.motion = SimpleNamespace(_positions=lambda *names: (100, 2000), go_to_extrude_pos=lambda: None)
        calls = []
        def fail(command):
            calls.append(command)
            raise RuntimeError("motion failed")
        box.gcode = SimpleNamespace(run_script_from_command=fail)
        with self.assertRaisesRegex(RuntimeError, "motion failed"):
            Feeding(box).make_material_loose()
        self.assertTrue(box.extrude_process_inside_retry_loose)
        self.assertEqual(len(calls), 1)

    def test_native_blocks_unresolved_stages_without_transport(self):
        from cfs.native import NativeSerial
        calls = []
        native = NativeSerial(SimpleNamespace(cmd_send_data_with_response=lambda *args: calls.append(args)))
        box = FakeBox()
        box.send_native = lambda request, **kwargs: native.exchange(request)
        feeding = Feeding(box)
        for stage in (8, 9):
            with self.assertRaises(UnsupportedRequest):
                feeding.communication_extrude_process(1, "A", stage=stage)
        box.boxcfg.version = 0
        with self.assertRaises(UnsupportedRequest):
            feeding.communication_extrude_process(1, "A")
        self.assertEqual(calls, [])

    def test_truthy_noninteger_amount_is_not_coerced(self):
        box = FakeBox()
        feeding = Feeding(box)
        for amount in (1.5, "7"):
            with self.assertRaises(TypeError):
                feeding.communication_extrude_process(1, "A", extrude=amount)
        self.assertEqual(box.requests, [])
    def test_wait_event_keeps_cached_mode_until_later_success(self):
        box = FakeBox()
        calls = []
        def send(request):
            calls.append(request)
            if len(calls) == 3: box.mode = "0"
            return True
        box.send_native = send
        self.assertTrue(Feeding(box).wait_box_preloading(1))
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
