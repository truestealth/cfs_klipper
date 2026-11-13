"""Synthetic complete unloading traces, freshness and lifecycle cancellation."""

from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.errors import format_error
from cfs.native import Frame
from cfs.retraction import Retraction
from cfs.state import BoxState


class Fixture:
    def __init__(self, source="T2C", heads=(False, False)):
        self.box_state = BoxState()
        self.boxcfg = SimpleNamespace(Tn_retrude=23, Tn_retrude_velocity=[420], Tn_extrude_temp=231)
        self.last_cmd, self.last_cmd_tmp = source, "old"
        self.error, self.error_tnn = None, {}
        self.is_use_ending_material = False
        self.last_replies, self.events = {}, []
        self.responses = defaultdict(deque)
        self.heads = deque(heads)
        self.head = False
        self.generation = defaultdict(int)
        self.binding = 0
        self.hook = None
        self.wait_result = True
        self.extrude_results = deque()
        self.heater = SimpleNamespace(can_extrude=True)
        self.toolhead = SimpleNamespace(extruder=SimpleNamespace(get_heater=lambda: self.heater))
        self.gcode = SimpleNamespace(run_script_from_command=lambda text: self.event(("gcode", text)))
        self.motion = SimpleNamespace(go_to_extrude_pos=lambda: self.event(("position",)))
        self.lifecycle = SimpleNamespace(report_error=lambda key, facts: self.event(("error", key, facts)))

    def event(self, event):
        self.events.append(event)
        if self.hook:
            self.hook(event)

    def host_guard(self, address=None):
        generation, binding = self.generation[address], self.binding
        def guard():
            if generation != self.generation[address] or binding != self.binding:
                raise RuntimeError("stale lifecycle")
        guard()
        return guard

    def get_filament_sensor_detect(self):
        if self.heads:
            self.head = self.heads.popleft()
        self.event(("head", self.head))
        return self.head

    def send_native(self, request):
        self.event(("wire", request.address, request.command, request.data))
        queue = self.responses[request.address, request.command, request.data]
        result = queue.popleft() if queue else True
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, tuple):
            value, status, payload = result
            self.last_replies[request.address] = Frame(request.address, request.command, status, payload, b"")
            return value
        return result

    def observation(self, address, bank, mask, value=True, status=0):
        self.responses[address, 8, bytes((bank,))].append((value, status, bytes((mask,))))

    def buffer(self, value):
        self.responses[2, 5, b""].append((True, 0, bytes((value,))))

    def communication_retrude_process(self, address, slot, trigger, report_err=True):
        self.event(("retract", address, slot, trigger, report_err))
        queue = self.responses[address, slot, trigger]
        result = queue.popleft() if queue else True
        if isinstance(result, BaseException):
            raise result
        return result

    def communication_extrude_process(self, address, slot, stage):
        self.event(("stage", address, slot, stage))
        return None

    def wait_box_preloading(self, addr):
        self.event(("wait", addr))
        return self.wait_result

    def get_flush_velocity(self, old, new):
        self.event(("speed", old, new))
        return [611], [1]

    def get_flush_temp(self, old, new):
        self.event(("temperature", old, new))
        return 220, 221, 222

    def Tn_Extrude(self, *args):
        self.event(("extrude",) + args)
        return self.extrude_results.popleft() if self.extrude_results else True

    def quickly_wait_heating(self, temperature):
        self.event(("heat", temperature))

    def make_material_loose(self):
        self.event(("loose",))

    def update_filament_pos(self, source, data):
        self.event(("filament", source, data))
        if source is not None:
            self.box_state.modify_Tn_data(int(source[1]), "None", "filament")

    def connected(self, *addresses):
        for address in addresses:
            self.box_state.modify_Tn_data(address, "connect", "state")


class RetractionTests(unittest.TestCase):
    def test_known_clear_uses_exact_source_and_updates_only_normal_tail(self):
        box = Fixture()
        self.assertIs(Retraction(box).box_retrude_material(), True)
        self.assertEqual([e for e in box.events if e[0] == "retract"], [("retract", 2, "C", "MATERIAL", False)])
        self.assertEqual(box.last_cmd_tmp, "T2C")
        self.assertIsNone(box.last_cmd)
        self.assertEqual(box.box_state.Tn_data["T2"]["filament"], "None")

    def test_known_head_assist_ignores_false_extruder_return(self):
        box = Fixture(heads=(True, False))
        box.extrude_results.append(False)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertIn(("extrude", -20, [611], 222), box.events)
        self.assertIn(("gcode", "G0 E-30 F2400"), box.events)
        self.assertEqual([e[3] for e in box.events if e[0] == "retract"], ["BUFFER", "MATERIAL"])

    def test_known_none_is_not_false_and_head_is_checked_after_clear(self):
        box = Fixture(heads=(False, True))
        box.responses[2, "C", "MATERIAL"].append(None)
        self.assertFalse(Retraction(box).box_retrude_material())
        self.assertIsNone(box.last_cmd)
        self.assertEqual(box.last_cmd_tmp, "T2C")
        self.assertIn(("error", "key_retrude_err4", {}), box.events)

    def test_no_report_false_material_has_no_retry_or_clear(self):
        box = Fixture()
        box.responses[2, "C", "MATERIAL"].append(False)
        self.assertFalse(Retraction(box).box_retrude_material(False))
        self.assertEqual(box.last_cmd, "T2C")
        self.assertNotIn(("loose",), box.events)

    def test_wait_failure_and_ending_material_bypass(self):
        box = Fixture()
        box.wait_result = None
        self.assertFalse(Retraction(box).box_retrude_material())
        self.assertEqual(box.events, [("wait", 2)])
        box.is_use_ending_material = True
        self.assertTrue(Retraction(box).box_retrude_material())

    def test_initial_idle_false_stops_but_none_continues(self):
        for result, success in ((False, False), (None, True)):
            box = Fixture()
            box.responses[2, 4, b"\x00\x01"].append(result)
            self.assertIs(Retraction(box).box_retrude_material(), success)

    def test_failed_initial_buffer_requires_clear_head_and_second_idle(self):
        box = Fixture(heads=(True, False, False))
        box.responses[2, "C", "BUFFER"].append(False)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertEqual(len([e for e in box.events if e[:3] == ("wire", 2, 4)]), 2)
        box = Fixture(heads=(True, True))
        box.responses[2, "C", "BUFFER"].append(False)
        self.assertFalse(Retraction(box).box_retrude_material())
        self.assertEqual(box.last_cmd, "T2C")
        self.assertIn(("error", "key_retrude_err3", {}), box.events)

    def test_material_retry_buffer_one_skips_stage_relief(self):
        box = Fixture(heads=(False, False, False))
        box.responses[2, "C", "MATERIAL"].extend((False, True))
        box.buffer(1)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertNotIn(("stage", 2, "C", 0), box.events)
        self.assertIn(("loose",), box.events)
        self.assertIn(("gcode", "G0 E-10 F60"), box.events)

    def test_material_retry_buffer_not_one_sends_stage_zero_six(self):
        for buffer in (0, 2, 3):
            box = Fixture(heads=(False, False, False))
            box.responses[2, "C", "MATERIAL"].extend((False, True))
            box.buffer(buffer)
            box.buffer(1)
            self.assertTrue(Retraction(box).box_retrude_material())
            self.assertEqual([e[3] for e in box.events if e[0] == "stage"], [0, 6])
            self.assertIn(("gcode", "G91\n G0 E5 F60\n G90"), box.events)

    def test_head_recovery_has_three_buffer_attempts(self):
        box = Fixture(heads=(False, True, True, True, True, False))
        box.responses[2, "C", "MATERIAL"].extend((False, True))
        box.buffer(1)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertEqual(len([e for e in box.events if e[0] == "retract" and e[3] == "BUFFER"]), 3)
        self.assertEqual(len([e for e in box.events if e[0] == "extrude"]), 3)

    def test_recovery_extrusion_false_clears_only_e_error(self):
        box = Fixture(heads=(False, True, True))
        box.responses[2, "C", "MATERIAL"].append(False)
        box.buffer(1)
        box.box_state.extrude_err = True
        box.extrude_results.append(False)
        self.assertFalse(Retraction(box).box_retrude_material())
        self.assertFalse(box.box_state.extrude_err)
        self.assertEqual(box.last_cmd, "T2C")

    def test_recovery_failed_buffer_state_error_stops_without_extra_head(self):
        box = Fixture(heads=(False, True, False))
        box.responses[2, "C", "MATERIAL"].append(False)
        box.responses[2, "C", "BUFFER"].append(False)
        box.buffer(1)
        box.error = "state_err"
        self.assertFalse(Retraction(box).box_retrude_material())
        self.assertEqual(len(box.heads), 1)

    def test_missing_or_event_buffer_does_not_consume_cache(self):
        for response in (None, True, (True, 0x30, b"\x01")):
            box = Fixture()
            box.responses[2, "C", "MATERIAL"].append(False)
            box.box_state.modify_Tn_inner_data(0, 1, uppart="buffer")
            box.responses[2, 5, b""].append(response)
            self.assertFalse(Retraction(box).box_retrude_material())
            self.assertNotIn(("loose",), box.events)

    def test_unknown_clear_intersection_only_across_addresses(self):
        box = Fixture(None, heads=(False,))
        box.connected(1, 3)
        box.observation(1, 1, 5)
        box.observation(1, 0, 6)
        box.observation(3, 1, 8)
        box.observation(3, 0, 8)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertEqual([e for e in box.events if e[0] == "retract"],
                         [("retract", 1, "C", "MATERIAL", True), ("retract", 3, "D", "MATERIAL", True)])

    def test_unknown_missing_sensor_stops_without_invented_owner(self):
        box = Fixture(None, heads=(False,))
        box.connected(1)
        box.box_state.modify_Tn_inner_data(1, 15, "filament_sensor", subpart="connections")
        box.box_state.modify_Tn_inner_data(1, 15, "filament_sensor", subpart="material")
        self.assertFalse(Retraction(box).box_retrude_material())
        self.assertFalse(any(e[0] == "retract" for e in box.events))

    def test_unknown_head_shared_buffer_then_first_intersection(self):
        box = Fixture(None, heads=(True,))
        box.connected(1, 2)
        box.observation(1, 1, 0)
        box.observation(1, 0, 4)
        box.observation(2, 1, 10)
        box.observation(2, 0, 10)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertEqual([e for e in box.events if e[0] == "retract"],
                         [("retract", 2, "0", "BUFFER", True), ("retract", 2, "B", "MATERIAL", True)])
        self.assertIn(("extrude", -20, [420], 231), box.events)
        self.assertEqual(box.last_cmd_tmp, "old")

    def test_unknown_head_cold_heater_uses_configured_temperature(self):
        box = Fixture(None, heads=(True,))
        box.connected(1)
        box.heater.can_extrude = False
        box.observation(1, 1, 1)
        box.observation(1, 0, 1)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertIn(("gcode", "M109 S231.00"), box.events)
        self.assertIn(("heat", 231), box.events)

    def test_unknown_no_owner_reports_sensor_error(self):
        box = Fixture(None, heads=(True, True))
        box.connected(1)
        box.observation(1, 1, 0)
        box.observation(1, 0, 1)
        self.assertFalse(Retraction(box).box_retrude_material())
        self.assertIn(("error", "key_sensor_err", {}), box.events)

    def test_unknown_failed_buffer_cleanup_continues_to_final_head(self):
        box = Fixture(None, heads=(True, False, False, False))
        box.connected(1)
        box.observation(1, 1, 1)
        box.observation(1, 0, 3)
        box.responses[1, "0", "BUFFER"].append(False)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertEqual([e for e in box.events if e[0] == "retract"][-1], ("retract", 1, "A", "MATERIAL", True))
        self.assertFalse(any(e[0] == "error" for e in box.events))

    def test_unknown_failed_buffer_connection_only_reports_and_retains_last_result(self):
        box = Fixture(None, heads=(True, False))
        box.connected(1)
        box.observation(1, 1, 3)
        box.observation(1, 0, 1)
        box.responses[1, "0", "BUFFER"].append(False)
        self.assertTrue(Retraction(box).box_retrude_material())
        self.assertIn(("error", "key_material_err", {"address": 1, "request_address": 1, "slot": "B"}), box.events)
        self.assertEqual(box.error_tnn, {"addr": 1})
        event = next(event for event in box.events if event[0] == "error")
        self.assertIn('"values": [1, "B"]', format_error(event[1], event[2]))

    def test_reconnect_of_prior_scanned_address_stops_later_effects(self):
        box = Fixture(None, heads=(False,))
        box.connected(1, 2)
        box.observation(1, 1, 0)
        box.observation(1, 0, 0)
        box.observation(2, 1, 1)
        box.observation(2, 0, 1)
        def reconnect(event):
            if event[:3] == ("wire", 2, 8):
                box.generation[1] += 1
        box.hook = reconnect
        with self.assertRaisesRegex(RuntimeError, "stale"):
            Retraction(box).box_retrude_material()
        self.assertFalse(any(e[0] == "retract" for e in box.events))

    def test_exception_and_known_device_change_preserve_source(self):
        box = Fixture()
        box.responses[2, "C", "MATERIAL"].append(ValueError("transport"))
        with self.assertRaisesRegex(ValueError, "transport"):
            Retraction(box).box_retrude_material()
        self.assertEqual(box.last_cmd, "T2C")
        box = Fixture()
        box.hook = lambda event: box.generation.__setitem__(2, 1) if event[0] == "retract" else None
        with self.assertRaisesRegex(RuntimeError, "stale"):
            Retraction(box).box_retrude_material()
        self.assertEqual(box.last_cmd, "T2C")

    def test_with_tnn_ignores_normal_failures_and_uses_connection_stop(self):
        box = Fixture(heads=(True,))
        box.extrude_results.append(False)
        box.responses[3, "D", "MATERIAL"].append(False)
        self.assertIsNone(Retraction(box).retrude_material_with_tnn("T3D"))
        self.assertEqual(box.events[0], ("position",))
        self.assertIn(("wire", 3, 7, b"\x00"), box.events)
        self.assertIn(("gcode", "G0 E-15 F120"), box.events)
        self.assertEqual(box.last_cmd_tmp, "T2C")
        self.assertIsNone(box.last_cmd)

    def test_with_no_tnn_uses_all_connected_zero_selectors(self):
        box = Fixture(heads=(False,))
        box.connected(1, 4)
        self.assertIsNone(Retraction(box).retrude_material_with_tnn())
        self.assertEqual([e for e in box.events if e[0] == "retract"],
                         [("retract", 1, "0", "MATERIAL", True), ("retract", 4, "0", "MATERIAL", True)])
        self.assertFalse(any(e[:3] == ("wire", 1, 7) for e in box.events))

    def test_with_tnn_invalid_source_fails_after_original_initial_motion(self):
        box = Fixture(heads=(False,))
        with self.assertRaises(KeyError):
            Retraction(box).retrude_material_with_tnn("invalid")
        self.assertEqual(box.events, [("position",), ("head", False)])
        self.assertEqual(box.last_cmd, "T2C")


if __name__ == "__main__":
    unittest.main()
