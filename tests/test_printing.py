import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.printing import Printing
from cfs.lifecycle import LifecycleStopped
import test_host_actions


class State:
    def __init__(self):
        self.Tnn_map = {"T1A": "T2A", "T2A": "T1A"}
        self.enabled = None

    def get_Tn_data(self, address, part):
        return "connect" if address in (1, 3) else "disconnect"

    def modify_Tn_data(self, address, value, uppart=None):
        self.enabled = value


class PrintingTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.state = State()
        self.sd = SimpleNamespace(current_file=None, print_file_name_path="file.json",
                                  get_first_Tn=lambda default: "T0")
        self.move = SimpleNamespace(recordPrintFileName=lambda *a, **kw: self.calls.append((a, kw)))
        objects = {"virtual_sdcard": self.sd, "gcode_move": self.move}
        self.box = SimpleNamespace(
            box_state=self.state, preloading_en=True, start_print_retraction_distance=0,
            host_guard=lambda: lambda: None,
            lifecycle=SimpleNamespace(_report_guard=lambda **kw: lambda: None),
            communication_set_pre_loading=lambda *a, **kw: self.calls.append((a, kw)),
            communication_tighten_up_enable=lambda *a: self.calls.append(a),
            store=SimpleNamespace(cleanup_recovery=lambda: True),
            gcode=SimpleNamespace(run_script_from_command=lambda s: self.calls.append(s)),
            set_head_runout_enabled=lambda value: self.calls.append(("sensor", value)),
            _printer=SimpleNamespace(lookup_object=objects.__getitem__))
        self.printing = Printing(self.box)

    def test_start_and_end_connected_units_and_cleanup(self):
        self.printing.cmd_box_start_print(None)
        self.assertEqual(self.calls, [((1, "CLOSE"), {"force": True}), (1, "DISABLE"),
                                      ((3, "CLOSE"), {"force": True}), (3, "DISABLE")])
        self.calls.clear()
        self.printing.cmd_box_end_print(None)
        self.assertEqual(self.calls, [((1, "OPEN"), {"force": True}), (1, "ENABLE"),
                                      ((3, "OPEN"), {"force": True}), (3, "ENABLE"),
                                      ("sensor", 0)])
        self.assertEqual(self.state.Tnn_map, {"T1A": "T1A", "T2A": "T2A"})
        self.assertEqual(self.state.enabled, 1)

    def test_disabled_preload_and_cleanup_failure(self):
        self.box.preloading_en = False
        self.printing.cmd_box_start_print(None)
        self.assertEqual(self.calls, [])
        self.box.store.cleanup_recovery = lambda: False
        with self.assertRaises(OSError):
            self.printing.cmd_box_end_print(None)
        self.assertEqual(self.calls, [(1, "ENABLE"), (3, "ENABLE")])
        self.assertIsNone(self.state.enabled)

    def test_first_tool_and_metadata(self):
        command = SimpleNamespace(get_int=lambda name, default: 8)
        self.printing.cmd_box_start_print_extrude_material(command)
        self.assertEqual(self.box.start_print_retraction_distance, 8)
        self.assertEqual(self.calls, ["T0"])
        self.calls.clear()
        self.sd.get_first_Tn = lambda default: None
        self.printing.cmd_box_start_print_extrude_material(command)
        self.assertEqual(self.calls, [])
        self.printing.record_tn_cur_tnn("T1A")
        self.assertEqual(self.calls, [(("file.json", ""), {"tn_cur_tnn": "T1A"})])
        self.sd.current_file = SimpleNamespace(name="model.gcode")
        self.printing.record_tn_cur_tnn("T2B")
        self.assertEqual(self.calls[-1], (("file.json", "model.gcode"), {"tn_cur_tnn": "T2B"}))

    def test_missing_first_tool_does_not_read_parameter(self):
        for first in (None, False, ""):
            self.sd.get_first_Tn = lambda default, first=first: first
            command = SimpleNamespace(get_int=lambda *args: self.fail("parameter read"))
            self.printing.cmd_box_start_print_extrude_material(command)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.box.start_print_retraction_distance, 0)

    def test_start_parameter_name_and_default(self):
        seen = []
        command = SimpleNamespace(get_int=lambda *args: seen.append(args) or args[1])
        self.printing.cmd_box_start_print_extrude_material(command)
        self.assertEqual(seen, [("START_PRINT", 0)])
        self.assertEqual(self.box.start_print_retraction_distance, 0)

    def test_metadata_record_failure_logged_but_lookup_failure_propagates(self):
        self.move.recordPrintFileName = lambda *a, **kw: (_ for _ in ()).throw(OSError("write"))
        with self.assertLogs(level="ERROR"):
            self.printing.record_tn_cur_tnn("T1A")
        self.box._printer.lookup_object = lambda name: (_ for _ in ()).throw(KeyError(name))
        with self.assertRaises(KeyError):
            self.printing.record_tn_cur_tnn("T1A")

    def test_marker_remove_failure_propagates(self):
        self.box.flushing_sign = "marker"
        with patch("cfs.printing.os.path.exists", return_value=True), patch(
                "cfs.printing.os.remove", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.printing.has_flushing_sign()

    def test_marker_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            self.box.flushing_sign = os.path.join(directory, "marker")
            self.assertFalse(self.printing.has_flushing_sign())
            with open(self.box.flushing_sign, "w") as marker:
                marker.write("1")
            self.assertTrue(self.printing.has_flushing_sign())
            self.assertFalse(self.printing.has_flushing_sign())


class PrintingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_host_actions.HostActionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.make_box()
        self.fixture.ready(identify=True)
        self.box = self.fixture.box
        self.printing = Printing(self.box)
        self.sd = self.fixture.printer.objects["virtual_sdcard"]
        self.sd.get_first_Tn = lambda default: "T0"
        self.fixture.gcode.scripts.clear()

    def test_nested_operation_serial_change_and_shutdown(self):
        box = self.box
        def complete_nested(script):
            if script == "T0":
                box.operations.begin(None, "T1A", 1)
                box.operations.cancel()
        self.fixture.gcode.on_script = complete_nested
        serial = box.operations._serial
        self.printing.cmd_box_start_print_extrude_material(test_host_actions.HostCommand({"START_PRINT": "8"}))
        self.assertGreater(box.operations._serial, serial)
        self.assertEqual(box.start_print_retraction_distance, 8)
        self.assertFalse(box.operations.active)
        self.fixture.gcode.on_script = lambda script: self.fixture.printer.send_event("klippy:shutdown")
        with self.assertRaises(LifecycleStopped):
            self.printing.cmd_box_start_print_extrude_material(test_host_actions.HostCommand())

    def test_start_and_end_printing_state_preload_policy(self):
        box = self.box
        calls = []
        box.feeding._send = lambda address, command, data, **kw: calls.append((address, command, data)) or True
        box.preloading_en = True
        box.print_stats.state = "printing"
        self.printing.cmd_box_start_print(None)
        # Feeding suppresses preload even with force while printing; tighten remains.
        self.assertEqual([c[1] for c in calls], ["TIGHTEN_UP_ENABLE"])
        calls.clear()
        box.print_stats.state = "pause"
        self.printing.cmd_box_end_print(None)
        self.assertEqual([c[1] for c in calls], ["SET_PRE_LOADING", "TIGHTEN_UP_ENABLE"])
        self.assertEqual(calls[0][2], bytes((15, 1)))
        calls.clear()
        box.preloading_en = False
        self.printing.cmd_box_start_print(None)
        self.assertEqual(calls, [])
        self.printing.cmd_box_end_print(None)
        self.assertEqual([c[1] for c in calls], ["TIGHTEN_UP_ENABLE"])

    def test_end_uses_configured_head_and_omits_absent_sensor(self):
        self.box.preloading_en = False
        self.box.communication_tighten_up_enable = lambda *args: True
        self.box.boxcfg.filament_sensor = "custom_head"
        self.printing.cmd_box_end_print(None)
        self.assertIn("SET_FILAMENT_SENSOR SENSOR=custom_head ENABLE=0", self.fixture.gcode.scripts)
        self.fixture.gcode.scripts.clear()
        self.box.boxcfg.filament_sensor = None
        self.printing.cmd_box_end_print(None)
        self.assertFalse(any("SET_FILAMENT_SENSOR" in command for command in self.fixture.gcode.scripts))

    def test_shutdown_in_preload_stops_later_effects(self):
        self.box.preloading_en = True
        self.box.print_stats.state = "pause"
        def stop(*args, **kwargs):
            self.fixture.printer.send_event("klippy:shutdown")
        self.box.communication_set_pre_loading = stop
        self.box.communication_tighten_up_enable = lambda *args: self.fail("tighten after shutdown")
        with self.assertRaises(LifecycleStopped):
            self.printing.cmd_box_start_print(None)
        self.assertEqual(self.box.box_state.Tnn_map["T1A"], "T1A")

    def test_failed_cleanup_preserves_disk_after_live_map_reset(self):
        box = self.box
        box.preloading_en = False
        box.communication_tighten_up_enable = lambda *args: True
        box.box_state.Tnn_map["T1A"] = "T1B"
        self.assertTrue(box.store.save_mapping(box.box_state.Tnn_map))
        before = box.store.path.read_bytes()
        with patch.object(box.store, "_write_document", side_effect=OSError("denied")):
            with self.assertLogs(level="ERROR"), self.assertRaises(OSError):
                self.printing.cmd_box_end_print(None)
        self.assertEqual(box.box_state.Tnn_map["T1A"], "T1A")
        self.assertEqual(box.store.path.read_bytes(), before)
        self.assertEqual(json.loads(before)["tnn_map"]["T1A"], "T1B")
        self.assertFalse(any("SET_FILAMENT_SENSOR" in command for command in self.fixture.gcode.scripts))


if __name__ == "__main__":
    unittest.main()
