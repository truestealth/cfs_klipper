"""Synthetic host state cases; no native module or printer is loaded."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cfs.state import BOX_FIELDS, BoxState
from cfs.storage import CACHE_FIELDS, SpoolStore


class StateTests(unittest.TestCase):
    def test_initial_status_schema_order_and_client_isolation(self):
        state = BoxState()
        status = state.get_status()
        self.assertEqual(list(status), [
            "filament", "state", "auto_refill", "enable", "filament_useup",
            "same_material", "T1", "T2", "T3", "T4", "cut_pos",
            "t_command", "custom_command_result",
        ])
        for address in range(1, 5):
            row = status["T%d" % address]
            self.assertEqual(list(row), list(BOX_FIELDS))
            for field in BOX_FIELDS:
                expected = ["-1"] * 4 if field in CACHE_FIELDS else "-1" if field in ("version", "sn") else "None"
                self.assertEqual(row[field], expected)
        self.assertEqual(status["filament"], 1)
        self.assertIs(type(status["filament"]), int)
        self.assertEqual(status["cut_pos"], 0.0)
        self.assertEqual(status["t_command"], "")
        status["T1"]["vender"][0] = "client edit"
        status["same_material"].append(["PLA", "FFFFFF", ["T1A"], "label"])
        self.assertEqual(state.Tn_data["T1"]["vender"], ["-1"] * 4)
        self.assertEqual(state.Tn_data["same_material"], [])
        state.modify_Tn_data(1, "300", "temperature")
        status = state.get_status(1.5, "T15", "result")
        self.assertEqual(status["T1"]["temperature"], "300")
        self.assertEqual((status["cut_pos"], status["t_command"], status["custom_command_result"]), (1.5, "T15", "result"))
        self.assertEqual(state.Tn_data["T2"]["temperature"], "None")

    def test_raw_getters_and_distinct_sentinels(self):
        state = BoxState()
        self.assertEqual(state.get_Tn_data(0, 0, uppart="state"), "None")
        self.assertEqual(state.get_Tn_data(0, 0, uppart="filament"), 1)
        for value in ("None", "none", "unknown", "-1", None, 0, False):
            state.modify_Tn_data(4, value, "vender", "D")
            actual = state.get_Tn_data(4, "vender", "D")
            self.assertEqual(actual, value)
            self.assertIs(type(actual), type(value))
        state.modify_Tn_data(1, "2", "mode")
        self.assertEqual(state.get_box_mode(1), "2")
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(state.get_Tn_data(1, "vender"))
            self.assertIsNone(state.get_Tn_data(1, "vender", "AB"))
            self.assertIsNone(state.get_Tn_data(5, "state"))
            self.assertIsNone(state.get_Tn_data(1, "not_a_field"))

    def test_virtual_mapping_is_separate_from_mutable_physical_aliases(self):
        state = BoxState()
        self.assertEqual(state.resolve_tool("T0"), "T1A")
        self.assertEqual(state.resolve_tool("T15"), "T4D")
        self.assertEqual(state.get_Tnn_content("T4D"), {"addr": 4, "num": "D"})
        before = deepcopy(state.Tn_data)
        self.assertIsNone(state.modify_mapping({"T1A": "T4D", "T2B": "T4D", "T3C": "T0", "T16": "T1B"}))
        self.assertEqual(state.resolve_tool("T0"), "T4D")
        self.assertEqual(state.resolve_tool("T5"), "T4D")
        self.assertEqual(state.resolve_tool("T1A"), "T4D")
        self.assertEqual(state.Tn_map["T0"], "T1A")
        self.assertEqual(state.Tnn_map["T3C"], "T3C")
        self.assertEqual(len(state.Tnn_map), 16)
        self.assertEqual(state.Tn_data, before)
        with self.assertRaises(ValueError):
            state.resolve_tool("T16")
        with self.assertRaises(KeyError):
            state.get_Tnn_content("T0")

    def test_shared_buffer_and_independent_sensor_banks(self):
        state = BoxState()
        self.assertIsNone(state.get_Tn_inner_data(0, 0, uppart="buffer"))
        state.modify_Tn_inner_data(4, 42, uppart="buffer")
        self.assertEqual(state.get_Tn_inner_data(1, 0, uppart="buffer"), 42)
        state.modify_Tn_inner_data(1, 5, "filament_sensor", subpart="material")
        state.modify_Tn_inner_data(4, 10, "filament_sensor", subpart="connections")
        self.assertEqual(state.get_Tn_inner_data(1, "filament_sensor", subpart="material"), 5)
        self.assertEqual(state.get_Tn_inner_data(4, "filament_sensor", subpart="connections"), 10)
        self.assertEqual(state.get_Tn_inner_data(4, "filament_sensor", subpart="material"), 0)
        before = deepcopy(state.Tn_inner_data)
        with self.assertLogs(level="WARNING"):
            state.modify_Tn_inner_data(1, "3", "filament_sensor", "A")
            self.assertIsNone(state.get_Tn_inner_data(1, "filament_sensor"))
        self.assertEqual(state.Tn_inner_data, before)
        state.e_err_set()
        self.assertIs(state.extrude_err, True)
        state.clear_e_err()
        self.assertIs(state.extrude_err, False)

    def test_metadata_setters_validate_fields_without_coercion(self):
        state = BoxState()
        before = deepcopy(state.Tn_data)
        with self.assertLogs(level="WARNING"):
            state.modify_Tn_data(0, "value", "state")
            state.modify_Tn_data(5, "value", "state")
            state.modify_Tn_data(1, "value", "not_a_field")
            state.modify_Tn_data(1, "value", "vender")
            state.modify_Tn_data(1, "value", "vender", 0)
        self.assertEqual(state.Tn_data, before)
        state.modify_Tn_data(0, 0, uppart="enable")
        state.modify_Tn_data(0, -3, uppart="auto_refill")
        self.assertEqual((state.Tn_data["enable"], state.Tn_data["auto_refill"]), (0, -3))
        state.modify_Tn_data(4, "7", "remain_len", "D")
        self.assertEqual(state.Tn_data["T4"]["remain_len"][3], "7")
        self.assertEqual(state.tn_save_data["T4"]["remain_len"][3], "7")

    def test_saved_cache_is_loaded_separately_and_needs_presence_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SpoolStore(directory)
            store.load()
            for part, value in (("vender", "vendor"), ("remain_len", "120"), ("color_value", "FFFFFF"), ("material_type", "A12345")):
                self.assertTrue(store.update_metadata(1, part, "A", value))
            self.assertTrue(store.update_metadata(1, "color_value", "B", "000000"))
            self.assertTrue(store.update_metadata(1, "material_type", "B", "A54321"))
            self.assertTrue(store.update_metadata(1, "material_type", "C", "A99999"))
            state = BoxState(store)
            self.assertEqual(state.Tn_data["T1"]["vender"], ["-1"] * 4)
            self.assertEqual(state.tn_save_data["T1"]["vender"][0], "vendor")
            self.assertEqual(state.tn_save_data_path, store.tn_save_data_path)
            state.sync_tn_data("T1", 3)
            self.assertEqual(state.Tn_data["T1"]["vender"], ["vendor", "unknown", "none", "none"])
            self.assertEqual(state.Tn_data["T1"]["remain_len"], ["120", "-1", "-1", "-1"])
            self.assertEqual(state.Tn_data["T1"]["color_value"][:2], ["FFFFFF", "000000"])
            state.sync_tn_data("T1", 7)
            self.assertEqual(state.Tn_data["T1"]["vender"][2], "unknown")
            self.assertEqual(state.Tn_data["T1"]["material_type"][2], "-1")
            state.modify_Tn_data(1, "none", "vender", "A")
            self.assertEqual(SpoolStore(directory).load()["T1"]["vender"][0], "vendor")
            state.sync_tn_data("T1", 0)
            self.assertEqual(state.Tn_data["T1"]["color_value"][0], "FFFFFF")
            self.assertEqual(store.get_slot(1, "vender", "A"), "vendor")

    def test_grouping_uses_type_and_colour_without_claiming_sensor_presence(self):
        state = BoxState()
        rows = [
            ("T1A", "known", "A12345", "FFFFFF"),
            ("T2B", "unknown", "A12345", "FFFFFF"),
            ("T3C", "known", "A12345", "000000"),
            ("T4D", "none", "A12345", "FFFFFF"),
            ("T4A", "known", "unknown", "FFFFFF"),
            ("T4B", "-1", "A12345", "FFFFFF"),
            ("T4C", "known", "A12345", "none"),
        ]
        for tool, vendor, kind, color in rows:
            for part, value in (("vender", vendor), ("material_type", kind), ("color_value", color)):
                state.modify_Tn_data(int(tool[1]), value, part, tool[2])
        state.modify_mapping({"T1A": "T4D"})
        self.assertIsNone(state.update_same_material_list(material_name=lambda kind: "PLA"))
        self.assertEqual(state.Tn_data["same_material"], [
            ["A12345", "FFFFFF", ["T1A", "T2B"], "PLA"],
            ["A12345", "000000", ["T3C"], "PLA"],
        ])
        self.assertEqual(state.get_Tn_inner_data(2, "filament_sensor", subpart="material"), 0)
        state.modify_Tn_data(1, "none", "vender", "A")
        state.update_same_material_list()
        self.assertEqual(state.Tn_data["same_material"][0], ["A12345", "FFFFFF", ["T2B"], ""])

    def test_persistence_failures_do_not_claim_a_saved_or_rolled_back_change(self):
        with tempfile.TemporaryDirectory() as directory:
            state = BoxState(SpoolStore(directory))
            with patch.object(state.store, "save_mapping", return_value=False):
                with self.assertRaises(OSError):
                    state.modify_mapping({"T1A": "T4D"})
            self.assertEqual(state.Tnn_map["T1A"], "T4D")
            with patch.object(state.store, "update_metadata", return_value=False):
                with self.assertRaises(OSError):
                    state.modify_Tn_data(1, "new", "vender", "A")
            self.assertEqual(state.Tn_data["T1"]["vender"][0], "new")
            self.assertEqual(SpoolStore(directory).load()["T1"]["vender"][0], "-1")


if __name__ == "__main__":
    unittest.main()
