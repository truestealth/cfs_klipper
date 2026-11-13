"""Exercise data commands against bounded Klipper substitutes and temporary files."""

import ast
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cfs_box.box import Box
from test_configuration import FakeConfig


class CommandError(RuntimeError):
    pass


class Command:
    error = CommandError
    _missing = object()

    def __init__(self, parameters):
        self.parameters = parameters

    def get_command_parameters(self):
        return dict(self.parameters)

    def get_int(self, name, default=_missing, minval=None, maxval=None):
        value = self.parameters.get(name, default)
        if value is self._missing:
            raise self.error("missing " + name)
        try:
            value = int(value)
        except (ValueError, TypeError) as error:
            raise self.error("invalid " + name) from error
        if minval is not None and value < minval:
            raise self.error("below " + name)
        if maxval is not None and value > maxval:
            raise self.error("above " + name)
        return value


class Gcode:
    def __init__(self):
        self.commands = {}
        self.scripts = []
        self.messages = []

    def register_command(self, name, handler):
        if name in self.commands:
            raise AssertionError("duplicate command " + name)
        self.commands[name] = handler

    def run_script_from_command(self, script):
        self.scripts.append(script)

    def respond_info(self, message):
        self.messages.append(message)


class Webhooks:
    def __init__(self):
        self.endpoints = {}

    def register_endpoint(self, name, handler):
        self.endpoints[name] = handler


class BoxDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base_dir = Path(self.directory.name)
        self.config_path = self.base_dir / "box.cfg"
        self.config_path.write_text(
            "[box]\nTn_retrude:-60\ncut_pos_y:10\n\n"
            "[gcode_macro USER_WRAPPER]\ngcode:\n    M117 preserve\n",
            encoding="utf-8",
        )
        self.box = Box(
            FakeConfig({"bus": "stock", "has_extrude_pos": 1, "Tn_extrude": "150"}),
            self.base_dir, config_paths=[self.config_path],
        )
        self.gcode = Gcode()
        self.webhooks = Webhooks()
        self.box.register_data_commands(self.gcode, self.webhooks)

    def command(self, name, **parameters):
        return self.gcode.commands[name](Command(parameters))

    def saved(self):
        return json.loads(self.box.store.path.read_text(encoding="utf-8"))

    def test_data_registration_and_client_snapshot_have_no_startup_actions(self):
        """Partial PC-CMD-REGISTRATION; PC-STATE-SCHEMA and PC-STATE-ORDER."""
        self.assertEqual(set(self.gcode.commands), {
            "BOX_MODIFY_TN", "BOX_MODIFY_TN_DATA", "BOX_MODIFY_TN_INNER_DATA",
            "BOX_SHOW_TNN_INNER_DATA", "BOX_ENABLE_AUTO_REFILL", "BOX_ENABLE_CFS_PRINT",
            "BOX_UPDATE_SAME_MATERIAL_LIST", "SAVE_BOX_CFG", "MODIFY_BOX_CFG",
        })
        self.assertEqual(set(self.webhooks.endpoints), {"box/get_box_cfg"})
        self.assertEqual(self.gcode.scripts, [])
        self.assertEqual(self.box.boxcfg.Tn_extrude, 150.0)
        self.assertIs(self.box.box_action.box_state, self.box.box_state)
        self.box.box_state.is_continue_print = True
        self.assertTrue(self.box.box_action.box_state.is_continue_print)
        status = self.box.get_status(0.0)
        status["T1"]["vender"][0] = "changed-client-copy"
        self.assertEqual(self.box.get_status(1.0)["T1"]["vender"][0], "-1")
        serialized = json.dumps(self.box.get_status(0.0))
        self.assertLess(serialized.index('"state"'), serialized.index('"T1"'))
        self.assertLess(serialized.index('"T1"'), serialized.index('"T4"'))
        self.box.box_state.Tn_data["state"] = "connect"
        for address in range(1, 5):
            self.box.box_state.Tn_data["T%d" % address]["state"] = "disconnect"
            self.box.box_state.Tn_data["T%d" % address]["version"] = str(address)
        serialized = json.dumps(self.box.get_status(0))
        # Bounded substitute for consumers that scan serialized values in order.
        self.assertEqual(re.findall(r'"state":\s*"([^"]*)"', serialized)[0], "connect")
        self.assertEqual(re.findall(r'"version":\s*"([^"]*)"', serialized), ["1", "2", "3", "4"])

    def test_preload_preferences_are_independent_ram_values(self):
        self.assertIsNone(self.box.preloading_en)
        self.assertIsNone(self.box.preloading_poweron)
        before = self.box.store.path.read_bytes()
        for permission in (None, False, True):
            for poweron in (None, False, True):
                self.box.preloading_en = permission
                self.box.preloading_poweron = poweron
                self.assertIs(self.box.box_action.box_save.preloading_en, permission)
                self.assertIs(self.box.box_action.box_save.preloading_poweron, poweron)
        self.assertEqual(self.box.store.path.read_bytes(), before)
        self.assertEqual(self.gcode.scripts, [])

    def test_metadata_roundtrip_keeps_cached_labels_when_live_slot_is_empty(self):
        """PC-CMD-DATA: client edits, persistence exclusions and no feed."""
        for part, data in (("vender", "known"), ("material_type", "G12345"), ("color_value", "0FFFFFF")):
            self.assertIsNone(self.command(
                "BOX_MODIFY_TN_DATA", ADDR="4", NUM="D", PART=part, DATA=data,
            ))
        self.assertEqual(self.box.get_status(0)["T4"]["material_type"][3], "G12345")
        self.command("BOX_MODIFY_TN_DATA", ADDR="4", NUM="D", PART="vender", DATA="none")
        self.assertEqual(self.box.get_status(0)["T4"]["vender"][3], "none")
        self.assertEqual(self.saved()["base_data"]["T4"]["vender"][3], "known")
        self.assertIsNone(self.box.box_state.Tn_inner_data["buffer"])
        self.assertEqual(self.gcode.scripts, [])

    def test_mapping_persists_all_entries_without_selecting_or_feeding(self):
        """Partial PC-CMD-MAPPING: current remap differs from requested source."""
        self.assertIsNone(self.command("BOX_MODIFY_TN", T1A="T4D", T2B="T3C", INVALID="T1B"))
        mapping = self.saved()["tnn_map"]
        self.assertEqual(len(mapping), 16)
        self.assertEqual(mapping["T1A"], "T4D")
        self.assertEqual(mapping["T2B"], "T3C")
        self.assertEqual(self.box.box_state.resolve_tool("T0"), "T4D")
        self.assertEqual(self.box.t_command, "")
        self.assertIsNone(self.box.last_cmd)
        self.assertEqual(self.gcode.scripts, [])

    def test_public_parameters_keep_command_specific_bounds_and_defaults(self):
        self.assertIsNone(self.command("BOX_ENABLE_AUTO_REFILL"))
        self.assertEqual(self.box.get_status(0)["auto_refill"], 1)
        self.command("BOX_ENABLE_AUTO_REFILL", ENABLE="-2", IGNORED="yes")
        self.assertEqual(self.box.get_status(0)["auto_refill"], -2)
        self.assertNotIn("auto_refill", self.saved())
        for value in ({}, {"ENABLE": "-1"}, {"ENABLE": "2"}, {"ENABLE": "bad"}):
            with self.subTest(value=value), self.assertRaises(CommandError):
                self.command("BOX_ENABLE_CFS_PRINT", **value)
        for name in ("BOX_MODIFY_TN_DATA", "BOX_MODIFY_TN_INNER_DATA"):
            for value in ({}, {"ADDR": "0"}, {"ADDR": "5"}, {"ADDR": "bad"}):
                with self.subTest(command=name, value=value), self.assertRaises(CommandError):
                    self.command(name, **value)
        self.assertEqual(self.gcode.scripts, [])

    def test_cfs_enable_persists_and_controls_the_retained_runout_helper(self):
        for enabled in (0, 1):
            with self.subTest(enabled=enabled):
                self.assertIsNone(self.command("BOX_ENABLE_CFS_PRINT", ENABLE=str(enabled)))
                self.assertEqual(self.box.box_action.box_state.Tn_data["enable"], enabled)
                self.assertEqual(self.saved()["enable"], enabled)
                self.assertEqual(self.gcode.scripts[-1],
                    "SET_FILAMENT_SENSOR SENSOR=filament_sensor_2 ENABLE=%d" % (1 - enabled))

    def test_failed_command_writes_report_failure_and_preserve_existing_files(self):
        before = self.box.store.path.read_bytes()
        with patch("cfs_box.storage.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertLogs(level="ERROR"), self.assertRaises(CommandError):
                self.command("BOX_ENABLE_CFS_PRINT", ENABLE="0")
        self.assertEqual(self.box.store.path.read_bytes(), before)
        # The original order changes the live preference before writing the file.
        self.assertEqual(self.box.box_state.Tn_data["enable"], 0)

    def test_config_endpoint_modify_and_save_preserve_the_existing_wrapper(self):
        class Request:
            def send(request, response):
                request.response = response

        request = Request()
        self.webhooks.endpoints["box/get_box_cfg"](request)
        self.assertEqual(len(request.response), 21)
        self.assertEqual(request.response["buffer_empty_len"]["max"], 30)
        before = self.config_path.read_bytes()
        self.assertIsNone(self.command("MODIFY_BOX_CFG", TN_RETRUDE="-50 - 5", CUT_POS_Y="20"))
        self.assertEqual(self.box.boxcfg.Tn_retrude, -55)
        self.assertEqual(self.gcode.messages[-1],
            "MODIFY_BOX_CFG: success, cut_pos_y=20,\nTn_retrude=-55,\n"
            " please use SAVE_BOX_CFG to save box.cfg")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertIsNone(self.command("SAVE_BOX_CFG"))
        self.assertEqual(self.gcode.messages[-1], "SAVE_BOX_CFG ok: cut_pos_y=20,Tn_retrude=-55")
        self.assertEqual(self.config_path.with_suffix(".cfg.1").read_bytes(), before)
        updated = self.config_path.read_text(encoding="utf-8")
        self.assertIn("Tn_retrude:-55", updated)
        self.assertIn("cut_pos_y:20", updated)
        self.assertEqual(
            self.config_path.read_bytes().split(b"[gcode_macro", 1)[1],
            before.split(b"[gcode_macro", 1)[1],
        )
        self.assertEqual(self.gcode.scripts, [])

    def test_config_command_messages_distinguish_empty_edits_and_failures(self):
        self.command("SAVE_BOX_CFG")
        self.assertEqual(self.gcode.messages[-1], "SAVE_BOX_CFG ok:no save")
        self.command("MODIFY_BOX_CFG", BUFFER_EMPTY_LEN="0")
        self.command("SAVE_BOX_CFG")
        self.assertEqual(self.gcode.messages[-1], "SAVE_BOX_CFG ok: ")
        with self.assertRaisesRegex(CommandError, "MODIFY_BOX_CFG: failed"):
            self.command("MODIFY_BOX_CFG", CUT_VELOCITY="unknown_name")
        self.command("MODIFY_BOX_CFG", CUT_VELOCITY="3000")
        before = self.config_path.read_bytes()
        with patch("cfs_box.configuration.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaisesRegex(CommandError, "SAVE_BOX_CFG error"):
                self.command("SAVE_BOX_CFG")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.gcode.messages[-1].startswith("SAVE_BOX_CFG ok"))

    def test_endpoint_discards_partial_fields_but_does_not_hide_send_errors(self):
        box = Box(FakeConfig({"bus": "stock"}), self.base_dir)

        class Request:
            def send(request, response):
                request.response = response

        request = Request()
        with self.assertLogs(level="ERROR"):
            returned = box._handle_get_box_cfg_request(request)
        self.assertEqual(returned, {})
        self.assertIs(request.response, returned)
        self.assertFalse(hasattr(box.boxcfg, "clean_left_pos_x"))

        class FailedRequest:
            def send(request, response):
                raise OSError("synthetic send failure")

        with self.assertRaisesRegex(OSError, "synthetic send failure"):
            self.box._handle_get_box_cfg_request(FailedRequest())

    def test_diagnostic_presentation_and_inner_command_do_not_replace_live_values(self):
        self.command("BOX_MODIFY_TN_DATA", ADDR="1", PART="color_value", NUM="A", DATA="0FFFFFF")
        self.command("BOX_SHOW_TNN_INNER_DATA")
        displayed = ast.literal_eval(self.gcode.messages[-1].split(": ", 1)[1])
        self.assertEqual(displayed["T1"]["color_value"][0], "白色")
        self.assertEqual(self.box.box_state.Tn_data["T1"]["color_value"][0], "0FFFFFF")
        before = self.box.store.path.read_bytes()
        with self.assertLogs(level="WARNING"):
            self.command("BOX_MODIFY_TN_INNER_DATA", ADDR="1", PART="buffer", DATA="False")
        self.assertIsNone(self.box.box_state.Tn_inner_data["buffer"])
        self.assertEqual(self.box.store.path.read_bytes(), before)
        self.assertEqual(self.gcode.scripts, [])

    def test_grouping_uses_metadata_and_never_substitutes_for_live_sensors(self):
        for addr in (1, 4):
            for part, data in (("vender", "unknown"), ("material_type", "G12345"), ("color_value", "0FFFFFF")):
                self.command("BOX_MODIFY_TN_DATA", ADDR=str(addr), PART=part, NUM="A", DATA=data)
        self.box.store.material_database.write_text(json.dumps({"result": {"list": [{
            "base": {"id": "12345", "meterialType": "Synthetic material"},
        }]}}), encoding="utf-8")
        self.assertIsNone(self.command("BOX_UPDATE_SAME_MATERIAL_LIST"))
        self.assertEqual(self.box.get_status(0)["same_material"], [[
            "G12345", "0FFFFFF", ["T1A", "T4A"], "Synthetic material",
        ]])
        self.assertEqual(self.box.box_state.Tn_inner_data["T1"]["filament_sensor"]["material"], 0)
        self.assertIsNone(self.box.box_state.Tn_inner_data["buffer"])
        self.assertEqual(self.gcode.scripts, [])


if __name__ == "__main__":
    unittest.main()
