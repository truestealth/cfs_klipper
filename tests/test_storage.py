"""Check compatible JSON storage with synthetic files, without printer access."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cfs_box.storage import CACHE_FIELDS, SpoolStore, TOOLS, new_base_data


class SpoolStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SpoolStore(self.directory.name)

    def write_document(self, document):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(json.dumps(document), encoding="utf-8")

    def read_document(self):
        return json.loads(self.store.path.read_text(encoding="utf-8"))

    def test_initialization_is_explicit_and_missing_cache_is_complete(self):
        self.assertFalse(self.store.path.exists())
        self.assertIsNone(self.store.base_data)
        self.assertEqual(self.store.tn_save_data_path, str(self.store.path))
        data = self.store.load()
        self.assertEqual(set(data), {"T1", "T2", "T3", "T4"})
        for box in data.values():
            self.assertEqual(set(box), set(CACHE_FIELDS))
            for slots in box.values():
                self.assertEqual(slots, ["-1"] * 4)
        self.assertEqual(self.read_document(), {"base_data": data})
        data["T1"]["vender"][0] = "different"
        self.assertEqual(data["T4"]["vender"][0], "-1")
        self.assertEqual(data["T1"]["color_value"][0], "-1")

    def test_existing_data_round_trips_and_none_does_not_erase_cached_identity(self):
        data = new_base_data()
        data["T1"]["vender"][0] = "existing-vendor"
        data["T4"]["color_value"][3] = "#102030"
        data["T1"]["future_field"] = {"keep": True}
        original = {"base_data": data, "screen_extension": {"keep": "yes"}, "enable": 0}
        self.write_document(original)
        cache = self.store.load()
        before = self.store.path.read_bytes()
        self.assertTrue(self.store.update_metadata(1, "vender", "A", "none"))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get_slot(1, "vender", "A"), "existing-vendor")
        self.assertTrue(self.store.update_metadata(4, "material_type", "D", "G12345"))
        self.assertIs(self.store.base_data, cache)
        self.assertEqual(self.store.get_slot(4, "material_type", "D"), "G12345")
        saved = self.read_document()
        self.assertEqual(saved["screen_extension"], original["screen_extension"])
        self.assertEqual(saved["base_data"]["T1"]["future_field"], {"keep": True})
        self.assertEqual(saved["base_data"]["T4"]["color_value"][3], "#102030")
        self.assertEqual(saved["enable"], 0)
        self.assertEqual(SpoolStore(self.directory.name).load(), saved["base_data"])
        for value in (None, "None", "unknown", "-1", 0, False):
            with self.subTest(value=value):
                self.assertTrue(self.store.update_metadata(1, "vender", "B", value))
                result = self.read_document()["base_data"]["T1"]["vender"][1]
                self.assertIs(type(result), type(value))
                self.assertEqual(result, value)

    def test_reload_keeps_the_cache_object_shared_with_state_consumers(self):
        cache = self.store.load()
        document = self.read_document()
        document["base_data"]["T3"]["color_value"][2] = "0102030"
        self.write_document(document)
        self.assertIs(self.store.load(), cache)
        self.assertEqual(cache["T3"]["color_value"][2], "0102030")

    def test_missing_base_data_keeps_other_existing_keys(self):
        self.write_document({"enable": 1, "future": [1, "two"]})
        self.store.load()
        self.assertEqual(self.read_document(), {
            "enable": 1, "future": [1, "two"], "base_data": new_base_data(),
        })

    def test_slot_update_merges_data_added_to_disk_since_load(self):
        cache = self.store.load()
        document = self.read_document()
        document["base_data"]["T2"]["color_value"][2] = "external-colour"
        document["base_data"]["T4"]["new-field"] = {"keep": 1}
        document["new-root-field"] = ["external"]
        self.write_document(document)
        self.assertTrue(self.store.update_metadata(1, "vender", "A", "our-change"))
        document["base_data"]["T1"]["vender"][0] = "our-change"
        self.assertEqual(self.read_document(), document)
        self.assertIs(self.store.base_data, cache)
        self.assertEqual(cache, document["base_data"])

    def test_recovery_hints_are_separate_and_cleanup_keeps_cache_and_unknown_keys(self):
        mapping = {name: name for name in TOOLS}
        mapping["T1A"] = "T4D"
        original = {"base_data": new_base_data(), "future": {"owned_elsewhere": 1}}
        self.write_document(original)
        self.store.load()
        self.assertTrue(self.store.update("enable", enable=1))
        self.assertTrue(self.store.save_mapping(mapping))
        self.assertTrue(self.store.update("last_cmd", last_cmd="T4D"))
        hints = self.store.recovery_hints()
        self.assertEqual(hints, {"enable": 1, "tnn_map": mapping, "last_cmd": "T4D"})
        hints["tnn_map"]["T1A"] = "T2B"
        self.assertEqual(self.store.recovery_hints()["tnn_map"]["T1A"], "T4D")
        self.assertNotIn("filament_detected", self.store.base_data["T4"])
        self.assertTrue(self.store.cleanup_recovery())
        self.assertEqual(self.read_document(), original)
        self.assertEqual(self.store.recovery_hints(), {})

    def test_invalid_hints_and_slot_updates_never_write(self):
        self.store.load()
        original = self.store.path.read_bytes()
        invalid = [
            lambda: self.store.update("enable", enable=True),
            lambda: self.store.update("enable", enable=2),
            lambda: self.store.update("last_cmd", last_cmd="T0"),
            lambda: self.store.save_mapping({"T1A": "T1B"}),
            lambda: self.store.save_mapping({name: "T5A" for name in TOOLS}),
            lambda: self.store.update("unknown"),
            lambda: self.store.update_metadata(0, "vender", "A", "value"),
            lambda: self.store.update_metadata(1, "not_saved", "A", "value"),
            lambda: self.store.update_metadata(1, "vender", "E", "value"),
        ]
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()
            self.assertEqual(self.store.path.read_bytes(), original)
        document = self.read_document()
        document["tnn_map"] = {"T1A": "T1B"}
        self.write_document(document)
        with self.assertRaises(ValueError):
            self.store.recovery_hints()

    def test_malformed_existing_files_are_preserved_without_automatic_reset(self):
        self.store.load()
        variants = ["{broken", "[]", '{"base_data": {}}']
        malformed = new_base_data()
        malformed["T2"]["vender"] = ["only-one"]
        variants.append(json.dumps({"base_data": malformed}))
        for text in variants:
            with self.subTest(text=text):
                self.store.path.write_text(text, encoding="utf-8")
                before = self.store.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.store.load()
                with self.assertRaises(ValueError):
                    self.store.update("enable", enable=0)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_failed_writes_preserve_disk_and_cache_and_remove_temporary_files(self):
        self.store.load()
        before = self.store.path.read_bytes()
        cache = deepcopy(self.store.base_data)
        for target in ("os.fsync", "os.replace"):
            with self.subTest(target=target):
                with patch("cfs_box.storage." + target, side_effect=OSError("synthetic I/O failure")):
                    with self.assertLogs(level="ERROR"):
                        self.assertFalse(self.store.update_metadata(1, "vender", "A", "new-value"))
                self.assertEqual(self.store.path.read_bytes(), before)
                self.assertEqual(self.store.base_data, cache)
                self.assertEqual(list(self.store.path.parent.glob("*.tmp")), [])
        with patch.object(Path, "open", side_effect=PermissionError("synthetic read failure")):
            with self.assertRaises(OSError):
                self.store.load()
            with self.assertLogs(level="ERROR"):
                self.assertFalse(self.store.update("enable", enable=0))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_material_catalogue_metadata_and_missing_data_fallback_are_read_only(self):
        self.store.load()
        owned_files = {
            name: self.store.path.parent / name
            for name in ("material_box_info.json", "material_modify_info.json", "material_option.json")
        }
        for path in owned_files.values():
            path.write_bytes(b'{"owner": "screen"}\n')
        catalogue = {"result": {"list": [
            {"base": {"id": "12345", "meterialType": "Synthetic material"},
             "kvParam": {"nozzle_temperature": "235", "filament_max_volumetric_speed": "18.5"}},
            {"base": {"id": "54321"}, "kvParam": {"nozzle_temperature": "bad", "filament_max_volumetric_speed": []}},
            {"base": {"id": "99999"}},
            {"base": {"id": "33333", "meterialType": 0}},
            {"kvParam": {}}, None,
        ]}}
        self.store.material_database.write_text(json.dumps(catalogue), encoding="utf-8")
        before = self.store.material_database.read_bytes()
        self.assertEqual(self.store.load_material_database(), catalogue["result"]["list"])
        self.assertEqual(self.store.get_material_name("G12345suffix"), "Synthetic material")
        self.assertEqual(self.store.get_material_name("G33333"), 0)
        self.assertEqual(self.store.get_material_temperature("G12345suffix", 220), 235)
        self.assertEqual(self.store.get_material_max_speed("G12345suffix"), 18.5)
        self.assertEqual(self.store.get_material_temperature("G54321", 218), 218)
        self.assertIsNone(self.store.get_material_max_speed("G54321"))
        for identity in (None, "none", "G99999", "G00000"):
            with self.subTest(identity=identity):
                self.assertEqual(self.store.get_material_name(identity), "")
                self.assertEqual(self.store.get_material_temperature(identity, 219), 219)
                self.assertIsNone(self.store.get_material_max_speed(identity))
        self.assertEqual(self.store.material_database.read_bytes(), before)
        for path in owned_files.values():
            self.assertEqual(path.read_bytes(), b'{"owner": "screen"}\n')
        for invalid in ("{broken", "[]", '{"result": null}', '{"result": {"list": 2}}'):
            with self.subTest(invalid=invalid):
                self.store.material_database.write_text(invalid, encoding="utf-8")
                self.assertEqual(self.store.load_material_database(), [])
                self.assertEqual(self.store.get_material_name("G12345"), "")
                self.assertEqual(self.store.get_material_temperature("G12345", 217), 217)
                self.assertIsNone(self.store.get_material_max_speed("G12345"))
                self.assertEqual(self.store.material_database.read_text(encoding="utf-8"), invalid)
        self.store.material_database.unlink()
        self.assertEqual(self.store.load_material_database(), [])


if __name__ == "__main__":
    unittest.main()
