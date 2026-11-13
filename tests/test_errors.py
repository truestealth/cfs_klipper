import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.errors import ErrorHandler, format_error


class GCode:
    def __init__(self, box):
        self.box = box
        self.actions = []
        self.fail = None

    def respond_raw(self, message):
        self.actions.append(("report", message, self.box.inside_error))

    def run_script_from_command(self, command):
        self.actions.append(command)
        if command == self.fail:
            raise RuntimeError("synthetic failure")


class ErrorsTest(unittest.TestCase):
    def setUp(self):
        self.box = SimpleNamespace(last_err=None, error=None, error_tnn={}, inside_error=False,
                                   move_z=None, max_velocity=None, max_accel=None, print_state="ready")
        self.gcode = GCode(self.box)
        self.box.record_tn_move_z = lambda value: self.gcode.actions.append(("record_z", value))
        self.box.set_head_runout_enabled = lambda enabled: self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=configured_head ENABLE=%d" % enabled)
        self.stats = SimpleNamespace(state="paused")
        self.sd = SimpleNamespace(work_handler_dispatch="sd")
        self.reactor = SimpleNamespace(monotonic=lambda: 42, pause=lambda when: self.gcode.actions.append(("wait", when)))
        self.errors = ErrorHandler(self.box, self.gcode, self.reactor, self.stats, self.sd, lambda: "other")

    def test_format_and_duplicate(self):
        facts = {"address": 2, "request_address": 2, "slot": "B"}
        expected = '!! {"code":"key839", "msg":"filament error, no filament detected at box extrude position", "values": [2, "B"]}'
        self.assertEqual(format_error("key_filament_err", facts), expected)
        self.errors.report_error("key_filament_err", facts)
        self.errors.report_error("key_filament_err", facts)
        self.assertEqual(self.gcode.actions, [("report", expected, True)])
        self.assertFalse(self.box.inside_error)

    def test_other_sender_and_timeout_formats(self):
        facts = {"address": 2, "request_address": 2, "command": 4,
                 "data_send": b"\x02\x04\xff\x04\x04", "slot": None}
        self.assertEqual(format_error("key_state_err", facts),
                         "!! {\"code\":\"key840\", \"msg\":\"box switch state error, check it's current action, addr: 2, cmd: 0x4\", \"values\": []}")
        self.assertEqual(format_error("key_extrude_err1", facts),
                         "!! {\"code\":\"key835\", \"msg\":\"extrude error, maybe it's blocked at the connections\", \"values\": [2, \"C\"]}")
        self.assertEqual(format_error("key_timeout", facts),
                         '!! {"code":"key831", "msg":"serial_485 communication timeout", "values": [2]}')
        with self.assertRaises(KeyError):
            format_error("unknown", facts)

    def test_printing_pause_restore_and_order(self):
        self.box.move_z, self.box.max_velocity, self.box.max_accel = 3.125, 120, 1000
        self.errors.report_message("ordinary", "printing")
        self.assertEqual(self.gcode.actions, [
            ("report", "ordinary", True), "PAUSE POST_WORK=0", "M400", ("record_z", 0),
            "BOX_MOVE_TO_SAFE_POS", "M400", "G91", "G0 Z-3.12 F600", "G90",
            "G0 F120.00", "M204 S1000", "SAVE_GCODE_STATE NAME=PAUSE_STATE", "DO_AFTER_PAUSE"])
        self.assertIsNone(self.box.move_z)
        self.assertIsNone(self.box.max_velocity)
        self.assertIsNone(self.box.max_accel)

    def test_special_key_and_exception_release_guard(self):
        self.box.move_z = 2
        self.errors.report_message("contains key843", "printing")
        self.assertEqual(len(self.gcode.actions), 1)
        self.assertEqual(self.box.move_z, 2)
        self.gcode.fail = "PAUSE POST_WORK=0"
        with self.assertLogs(level="ERROR"):
            self.errors.report_message("other", "printing")
        self.assertEqual(self.box.last_err, "other")
        self.assertFalse(self.box.inside_error)

    def test_wait_and_same_dispatch(self):
        self.stats.state = "printing"
        self.reactor.pause = lambda when: (self.gcode.actions.append(("wait", when)), setattr(self.stats, "state", "paused"))
        self.errors.report_message("first", "printing")
        self.assertIn(("wait", 43), self.gcode.actions)
        self.stats.state = "printing"
        self.errors.current_dispatch = lambda: "sd"
        self.errors.report_message("second", "printing")
        self.assertEqual(self.gcode.actions.count(("wait", 43)), 1)

    def test_shutdown_during_pause_wait_stops_remaining_work(self):
        self.stats.state = "printing"
        self.box.move_z = 2
        active = [True]
        def shutdown(when):
            self.gcode.actions.append(("wait", when))
            active[0] = False
        def check_context():
            if not active[0]:
                raise RuntimeError("binding invalidated")
        self.reactor.pause = shutdown
        with self.assertLogs(level="ERROR"):
            self.errors.report_message("ordinary", "printing", check_context)
        self.assertEqual(self.gcode.actions, [("report", "ordinary", True),
                         "PAUSE POST_WORK=0", "M400", ("wait", 43)])
        self.assertEqual(self.box.move_z, 2)
        self.assertFalse(self.box.inside_error)

    def test_existing_inside_error_does_not_suppress_report(self):
        self.box.inside_error = True
        self.errors.report_message("nested", "ready")
        self.assertEqual(self.gcode.actions, [("report", "nested", True)])
        self.assertFalse(self.box.inside_error)

    def test_record_guard_truthy_context_and_sensor(self):
        self.errors.record_error("filament_err", {"last_tnn": "T1A", "addr": 1, "vtnn": "T0"})
        self.assertEqual(self.box.error_tnn, {"last_tnn": "T1A", "addr": 1, "vtnn": "T0"})
        self.assertEqual(self.gcode.actions, ["SET_FILAMENT_SENSOR SENSOR=configured_head ENABLE=1"])
        with self.assertLogs(level="WARNING"):
            self.errors.record_error("state_err", {"addr": 2})
        self.assertEqual(self.box.error, "filament_err")
        self.errors.record_error("state_err", {"cover": True, "addr": 0, "tnn": "T2B", "vtnn": ""})
        self.assertEqual(self.box.error_tnn, {"last_tnn": "T1A", "addr": 1, "tnn": "T2B", "vtnn": "T0"})
        with self.assertLogs(level="WARNING"):
            self.errors.record_error("invented", {"cover": True})
        self.assertEqual(self.box.error, "state_err")

    def test_sensor_failure_propagates_after_record(self):
        self.gcode.fail = "SET_FILAMENT_SENSOR SENSOR=configured_head ENABLE=1"
        with self.assertRaises(RuntimeError):
            self.errors.record_error("filament_err", {"addr": 2})
        self.assertEqual((self.box.error, self.box.error_tnn["addr"]), ("filament_err", 2))


if __name__ == "__main__":
    unittest.main()
