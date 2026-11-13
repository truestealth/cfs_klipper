"""Synthetic checks for loader, endpoint and compatible configuration edits."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cfs_box.configuration import BoxConfiguration


class FakeConfig:
    error = ValueError

    def __init__(self, values=None, sections=None):
        self.values = {key.lower(): value for key, value in (values or {}).items()}
        self.sections = sections or {
            "stepper_x": {"position_min": -10, "position_max": 300},
            "stepper_y": {"position_min": -5, "position_max": 400},
            "stepper_z": {"position_min": -2, "position_max": 400},
            "printer": {"max_velocity": 500},
        }
        self.calls = []

    def get(self, name, default=None):
        self.calls.append((name, "get", {}))
        return self.values.get(name.lower(), default)

    def _number(self, name, default, convert, **bounds):
        self.calls.append((name, convert.__name__, bounds))
        value = convert(self.values.get(name.lower(), default))
        if "minval" in bounds and value < bounds["minval"]:
            raise ValueError(name + " below minimum")
        if "maxval" in bounds and value > bounds["maxval"]:
            raise ValueError(name + " above maximum")
        return value

    def getint(self, name, default=None, **bounds):
        return self._number(name, default, int, **bounds)

    def getfloat(self, name, default=None, **bounds):
        return self._number(name, default, float, **bounds)

    def getintlist(self, name, default):
        self.calls.append((name, "intlist", {}))
        if name.lower() not in self.values:
            return default
        return tuple(int(value) for value in self.values[name.lower()].split(","))

    def getsection(self, name):
        return FakeConfig(self.sections[name])

    def get_printer(self):
        raise AssertionError("configuration parsing must not obtain printer objects")


def configuration(**values):
    settings = {"bus": "stock", "has_extrude_pos": 1}
    settings.update(values)
    return BoxConfiguration(FakeConfig(settings))


class ConfigurationTests(unittest.TestCase):
    def test_all_53_loader_options_and_defaults(self):
        """PC-CONFIG-LOAD: finite loader surface, default types and no hardware."""
        source = FakeConfig({"bus": "stock", "has_extrude_pos": 1})
        config = BoxConfiguration(source)
        matrix = json.loads((ROOT / "docs/contract-matrix.json").read_text(encoding="utf-8"))
        expected_names = {row["name"] for row in matrix["items"] if row["kind"] == "configuration"}
        self.assertEqual({name for name, _, _ in source.calls}, expected_names)
        self.assertEqual(len(expected_names), 53)
        for name, expected in {
            "version": 0, "has_extrude_pos": 1, "filament_sensor": None,
            "switch_pin": None, "box_extrude_retry_num": 5,
            "box_first_clean_length": 90, "box_need_clean_length": 80,
            "box_need_clean_length_max": 100, "pre_cut_pos_x": 0.0,
            "pre_cut_pos_y": 0.0, "cut_pos_x": -6.5, "cut_pos_y": 0.0,
            "cut_pos_offset": 0.0, "cut_find_start_offset_y": 6.0,
            "cut_velocity": 6000.0, "cut_accel": 5000.0,
            "cut_accel_to_decel": 30000.0, "cut_run_count": 1,
            "Tn_retrude": -60.0, "Tn_retrude_velocity": (360,),
            "Tn_extrude_temp": 220.0, "nozzle_volume": 200.0,
            "flush_multiplier": 1.0, "Tn_extrude": 140.0,
            "Tn_extrude_percent": (1,), "Tn_extrude_velocity": (360,),
            "buffer_empty_len": 30.0, "diff_length": -10.0,
            "extrude_material_len_for_box": 3.0,
            "extrude_material_len_for_extruder": 18.0,
            "extrude_material_velocity": 180.0, "extrude_material_times": 6,
            "Tn_line_len": 1.0, "clean_run_count": 1, "cr_machine": 1,
            "clean_left_pos_x": 150.0, "clean_left_pos_y": 368.0,
            "clean_left_pos_z": 23.0, "clean_right_pos_x": 170.0,
            "clean_right_pos_y": 368.0, "clean_right_pos_z": 23.0,
            "clean_velocity": 6000.0, "extrude_pos_x": 127.0,
            "extrude_pos_y": 368.0, "extrude_pos_z": 23.0,
            "clean_pos_min_x": 152.0, "clean_pos_min_y": 362.0,
            "clean_pos_max_x": 156.0, "clean_pos_max_y": 368.0,
            "safe_pos_y": 350.0, "retrude_position_x": 42.0,
            "retrude_position_y": 100.0,
        }.items():
            with self.subTest(option=name):
                self.assertEqual(getattr(config, name), expected)
                self.assertIs(type(getattr(config, name)), type(expected))
        self.assertEqual(config.modify_dict, {})
        self.assertEqual(source.values, {"bus": "stock", "has_extrude_pos": 1})

    def test_supplied_values_limits_and_profile_validation(self):
        config = configuration(
            version="1", Tn_extrude="150", box_first_clean_length="100",
            Tn_extrude_percent="1,2", Tn_extrude_velocity="300,600",
            Tn_retrude_velocity="400,800", extrude_material_velocity="-1",
            cut_run_count="0", clean_run_count="-3", Tn_line_len="-2",
        )
        self.assertEqual(config.version, 1)
        self.assertEqual(config.Tn_extrude, 150.0)
        self.assertEqual(config.Tn_extrude_percent, (1, 2))
        self.assertEqual(config.Tn_extrude_velocity, (300, 600))
        self.assertEqual(config.Tn_retrude_velocity, (400, 800))
        # Loader limits must not acquire the endpoint's count/line/speed limits.
        self.assertEqual((config.cut_run_count, config.clean_run_count, config.Tn_line_len), (0, -3, -2.0))
        self.assertEqual(config.extrude_material_velocity, -1.0)
        for values in (
            {"version": "2"}, {"version": "-1"}, {"box_extrude_retry_num": "6"},
            {"box_need_clean_length": "91"}, {"pre_cut_pos_x": "301"},
            {"cut_pos_offset": "6"}, {"cut_velocity": "30001"},
            {"Tn_extrude_temp": "179"}, {"buffer_empty_len": "61"},
            {"Tn_extrude_percent": "1,2"}, {"Tn_retrude_velocity": "1.5"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                configuration(**values)
        with self.assertRaises(ValueError):
            BoxConfiguration(FakeConfig())

    def test_disabled_positions_do_not_require_z_limits(self):
        source = FakeConfig({"bus": "stock"}, sections={
            "stepper_x": {"position_min": -10, "position_max": 300},
            "stepper_y": {"position_min": -5, "position_max": 400},
            "printer": {"max_velocity": 500},
        })
        config = BoxConfiguration(source)
        self.assertEqual(config.has_extrude_pos, 0)
        self.assertFalse(hasattr(config, "extrude_pos_z"))
        self.assertEqual(config.cut_pos_x, -6.5)

    def test_endpoint_exact_shape_bounds_and_derived_values(self):
        """PC-CONFIG-ENDPOINT: independent UI limits and scalar profile view."""
        config = configuration(cut_pos_y=399, cut_pos_offset=5, buffer_empty_len=60)
        response = config.get_box_cfg()
        matrix = json.loads((ROOT / "docs/contract-matrix.json").read_text(encoding="utf-8"))
        names = {row["name"].rsplit(".", 1)[1] for row in matrix["items"] if row["kind"] == "endpoint" and "." in row["name"]}
        self.assertEqual(set(response), names)
        self.assertEqual(len(response), 21)
        self.assertEqual(response["cut_pos_y"], {"value": 399.9, "min": -5.0, "max": 400.0})
        self.assertEqual(response["Tn_retrude_velocity"], {"value": 360, "min": 0, "max": 99999})
        self.assertEqual(response["buffer_empty_len"], {"value": 60.0, "min": 0, "max": 30})
        self.assertEqual(response["Tn_line_len"], {"value": 1.0, "min": 1, "max": 1000})
        self.assertEqual(response["cut_velocity"]["max"], 30000.0)
        self.assertEqual(response["clean_left_pos_z"]["min"], -2.0)
        for value in (8, [], (700, 900)):
            config.Tn_retrude_velocity = value
            expected = 700 if value == (700, 900) else value
            self.assertEqual(config.get_box_cfg()["Tn_retrude_velocity"]["value"], expected)

    def test_optional_positions_are_not_fabricated(self):
        for flag in (0, 2):
            config = BoxConfiguration(FakeConfig({"bus": "stock", "has_extrude_pos": flag}))
            self.assertEqual(config.has_extrude_pos, 0)
            self.assertFalse(hasattr(config, "extrude_pos_x"))
            with self.assertRaises(AttributeError):
                config.get_box_cfg()

    def test_modify_all_supported_keys_and_raw_falsey_rules(self):
        """PC-CONFIG-MODIFY: uppercase names, Tn spelling and no loader clamp."""
        config = configuration()
        keys = {
            "PRE_CUT_POS_X", "PRE_CUT_POS_Y", "CUT_POS_X", "CUT_POS_Y", "CUT_VELOCITY",
            "TN_RETRUDE", "TN_RETRUDE_VELOCITY", "BUFFER_EMPTY_LEN", "CLEAN_LEFT_POS_X",
            "CLEAN_LEFT_POS_Y", "CLEAN_RIGHT_POS_X", "CLEAN_RIGHT_POS_Y", "CLEAN_LEFT_POS_Z",
            "CLEAN_RIGHT_POS_Z", "CLEAN_VELOCITY", "EXTRUDE_POS_X", "EXTRUDE_POS_Y",
            "EXTRUDE_POS_Z", "TN_LINE_LEN", "CLEAN_RUN_COUNT", "CUT_RUN_COUNT",
        }
        pending = config.modify({key: "2" for key in keys})
        self.assertEqual(set(pending), keys)
        for key in keys:
            name = "T" + key.lower()[1:] if key.startswith("T") else key.lower()
            self.assertEqual(getattr(config, name), 2)
            for expression, expected in (("0", 0), ("False", False), ("None", None)):
                single = configuration()
                with self.subTest(key=key, expression=expression):
                    self.assertEqual(single.modify({key: expression}), {key: expected})
                    self.assertEqual(getattr(single, name), expected)
            single = configuration()
            with self.subTest(key=key), self.assertRaises(ValueError):
                single.modify({key: "unknown_name"})
            self.assertEqual(single.modify_dict, {})
        config.modify({"BUFFER_EMPTY_LEN": "0", "CUT_RUN_COUNT": "-20", "TN_LINE_LEN": "5000"})
        self.assertEqual(config.modify_dict["BUFFER_EMPTY_LEN"], 0)
        self.assertEqual(config.cut_run_count, -20)
        self.assertEqual(config.Tn_line_len, 5000)
        self.assertEqual(config.modify({"BUFFER_EMPTY_LEN": "", "CUT_POS_X": None, "CUT_POS_Y": 0}), config.modify_dict)
        config.modify({"TN_RETRUDE_VELOCITY": "[360, 180 * 4]", "CUT_VELOCITY": "60 * (100 + 2)"})
        self.assertEqual(config.Tn_retrude_velocity, [360, 720])
        self.assertEqual(config.cut_velocity, 6120)
        pending["CUT_POS_X"] = 999
        self.assertEqual(config.cut_pos_x, 2)
        # Unsupported command names are ignored rather than added as attributes.
        config.modify({"BUS": "'other'", "CUT_POS_OFFSET": "100"})
        self.assertEqual(config.bus, "stock")
        self.assertEqual(config.cut_pos_offset, 0.0)

    def test_modify_rejects_code_and_preserves_earlier_edits_on_error(self):
        for expression in ("__import__('os')", "self.cut_pos_x", "sum([1])", "x", "1/0", "{}", "1 +", "2 ** 8"):
            config = configuration()
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                config.modify({"PRE_CUT_POS_X": "8", "CUT_VELOCITY": expression})
            self.assertEqual(config.pre_cut_pos_x, 8)
            self.assertEqual(config.modify_dict, {"PRE_CUT_POS_X": 8})

    def test_save_path_preference_backup_and_other_sections(self):
        """PC-CONFIG-SAVE: temp-only save retains wrappers and existing settings."""
        with tempfile.TemporaryDirectory() as directory:
            first, second = (Path(directory) / name for name in ("primary.cfg", "fallback.cfg"))
            wrappers = b"[gcode_macro CUT]\r\nrename_existing: CUT_99\r\ngcode:\r\n  CUT_99 {rawparams}\r\ncut_pos_x: 333\r\n# \xff\r\n"
            original = b"[box]\ncut_pos_x: 1 # existing comment\nTn_retrude_velocity: 360\nuntouched: 77\n\n" + wrappers
            first.write_bytes(original)
            first.with_name(first.name + ".1").write_bytes(b"old backup")
            second.write_text("fallback", encoding="utf-8")
            config = configuration()
            config.modify({"CUT_POS_X": "-8.5", "TN_RETRUDE_VELOCITY": "(360, 720)", "CLEAN_RUN_COUNT": "3"})
            calls = []
            self.assertEqual(config.save((first, second), sync=lambda: calls.append(True)), first)
            self.assertEqual(first.with_name(first.name + ".1").read_bytes(), original)
            changed = first.read_bytes()
            self.assertIn(b"cut_pos_x:-8.5# existing comment\n", changed)
            self.assertIn(b"Tn_retrude_velocity:360,720\n", changed)
            self.assertIn(b"clean_run_count:3\n", changed)
            self.assertTrue(changed.endswith(wrappers))
            self.assertEqual(second.read_text(encoding="utf-8"), "fallback")
            self.assertEqual(config.modify_dict, {})
            self.assertEqual(len(calls), 2)
            self.assertIsNone(config.save((first, second), sync=lambda: self.fail("no changes")))
            first.unlink()
            second.write_text("[box]\ncut_pos_x: 1\n[next]\n", encoding="utf-8")
            config.modify({"CUT_POS_X": "-4"})
            self.assertEqual(config.save((first, second), sync=lambda: None), second)
            self.assertIn("cut_pos_x:-4", second.read_text(encoding="utf-8"))

    def test_save_updates_all_21_fields_using_the_file_spelling(self):
        matrix = json.loads((ROOT / "docs/contract-matrix.json").read_text(encoding="utf-8"))
        names = [row["name"].rsplit(".", 1)[1] for row in matrix["items"] if row["kind"] == "endpoint" and "." in row["name"]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "box.cfg"
            original = "[box]\n" + "".join(name + ":7\n" for name in names) + "[next]\nkeep: 99\n"
            path.write_text(original, encoding="utf-8")
            config = configuration()
            config.modify({name.upper(): "2" for name in names})
            config.save((path,), sync=lambda: None)
            expected = "[box]\n" + "".join(name + ":2\n" for name in names) + "[next]\nkeep: 99\n"
            self.assertEqual(path.read_text(encoding="utf-8"), expected)
            self.assertEqual(config.modify_dict, {})

    def test_save_zero_prefix_and_eof_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "box.cfg"
            config = configuration()
            path.write_text("[box]\nbuffer_empty_len: 10\ncut_velocity_extra: 5\n[next]\n", encoding="utf-8")
            config.modify({"BUFFER_EMPTY_LEN": "0", "CUT_VELOCITY": "3000"})
            config.save((path,), sync=lambda: None)
            self.assertEqual(config.buffer_empty_len, 0)
            self.assertIn("buffer_empty_len: 10", path.read_text(encoding="utf-8"))
            self.assertIn("cut_velocity:3000", path.read_text(encoding="utf-8"))
            self.assertNotIn("cut_velocity_extra", path.read_text(encoding="utf-8"))
            path.write_text("[box]\ncut_velocity: 6000\n", encoding="utf-8")
            config.modify({"CUT_VELOCITY": "4000", "CLEAN_RUN_COUNT": "4"})
            config.save((path,), sync=lambda: None)
            self.assertEqual(path.read_text(encoding="utf-8"), "[box]\ncut_velocity:4000\n")
            self.assertEqual(config.modify_dict, {"CUT_VELOCITY": None, "CLEAN_RUN_COUNT": 4})

    def test_failed_save_preserves_original_and_pending_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "box.cfg"
            original = "[box]\ncut_pos_x: 1\n[next]\n"
            path.write_text(original, encoding="utf-8")
            config = configuration()
            config.modify({"CUT_POS_X": "-3"})
            with patch("cfs_box.configuration.os.replace", side_effect=OSError("blocked")):
                with self.assertRaises(OSError):
                    config.save((path,), sync=lambda: None)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(config.modify_dict, {"CUT_POS_X": -3})
            self.assertEqual(sorted(item.name for item in path.parent.iterdir()), ["box.cfg", "box.cfg.1"])
            with self.assertRaises(FileNotFoundError):
                config.save((path.parent / "absent.cfg",), sync=lambda: None)

    def test_save_rejects_multiline_values_before_any_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "box.cfg"
            original = b"[box]\ncut_pos_x:1\n[gcode_macro KEEP]\ngcode:\n  KEEP {rawparams}\n"
            backup = path.with_name(path.name + ".1")
            for separator in ("\n", "\r", "\r\n"):
                for key, value in (
                    ("CUT_POS_X", "2" + separator + "[printer]" + separator + "max_velocity:999"),
                    ("TN_RETRUDE_VELOCITY", ["360" + separator + "[gcode_macro INJECT]"]),
                ):
                    with self.subTest(separator=repr(separator), key=key):
                        path.write_bytes(original)
                        backup.write_bytes(b"previous backup")
                        config = configuration()
                        pending = config.modify({key: repr(value)})
                        with patch("cfs_box.configuration.shutil.copy2") as copy:
                            with self.assertRaisesRegex(ValueError, "line breaks"):
                                config.save((path,), sync=lambda: self.fail("must not sync"))
                            copy.assert_not_called()
                        self.assertEqual(path.read_bytes(), original)
                        self.assertEqual(backup.read_bytes(), b"previous backup")
                        self.assertEqual(config.modify_dict, pending)
                        self.assertEqual(sorted(item.name for item in path.parent.iterdir()), ["box.cfg", "box.cfg.1"])


if __name__ == "__main__":
    unittest.main()
