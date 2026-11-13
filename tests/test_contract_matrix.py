"""Check contract inventory integrity, not the pending printer implementation."""

from collections import Counter
import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContractMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = json.loads(
            (ROOT / "docs/contract-matrix.json").read_text(encoding="utf-8")
        )
        cls.items = cls.matrix["items"]
        cls.checks = cls.matrix["checks"]

    def test_fixed_external_surfaces_are_complete(self):
        commands = {row["name"]: row for row in self.items if row["kind"] == "gcode"}
        tools = {"T%d" % number for number in range(16)} | {
            "T%d%s" % (address, slot)
            for address in range(1, 5)
            for slot in "ABCD"
        }
        generated = {name for name, row in commands.items() if row["when"] == "generated"}
        self.assertEqual(generated, tools)
        self.assertEqual(len(commands.keys() - tools), 71)
        conditional = {
            name for name, row in commands.items()
            if row["when"] == "configured switch_pin"
        }
        self.assertEqual(conditional, {"BOX_CUT_HALL_ZERO", "BOX_CUT_HALL_TEST"})
        self.assertEqual(commands["TEST_BOX_CLEAN"]["alias_of"], "BOX_NOZZLE_CLEAN")

        counts = Counter(row["kind"] for row in self.items)
        for kind, expected in {
            "gcode": 103, "configuration": 54, "endpoint": 22,
            "wire": 19, "response": 25, "firmware": 6, "macro": 26,
        }.items():
            with self.subTest(kind=kind):
                self.assertEqual(counts[kind], expected)
        self.assertEqual(self.matrix["domains"]["Tn"], ["T1", "T2", "T3", "T4"])
        self.assertEqual(self.matrix["domains"]["slots"], list("ABCD"))
        self.assertEqual(self.matrix["baseline"]["mcu"], "1.1.3")
        for kind in ("wire", "response"):
            codes = [row["code"] for row in self.items if row["kind"] == kind]
            self.assertEqual(len(codes), len(set(codes)))
            self.assertTrue(all(type(code) is int and 0 <= code <= 255 for code in codes))

        exposed = {row["name"]: row for row in self.items if row["kind"] == "dependency"}
        for path in {
            "box.box_state.is_continue_print",
            "box.box_action.box_state.Tn_data['enable']",
            "box.box_action.box_save.move_z",
            "box.box_action.box_save.last_err",
            "box.record_tn_move_z",
            "box.box_state.tn_save_data_path",
            "box.inside_error",
            "box.box_action.heart_process_enable",
            "virtual_sdcard.resume_tnn",
        }:
            with self.subTest(external_path=path):
                self.assertIn(path, exposed)
                self.assertIn("PC-RETAINED-BOX-API", exposed[path]["checks"])

    def test_every_requirement_has_a_stage_and_defined_checks(self):
        identifiers = [row["id"] for row in self.items]
        case_ids = [row["id"] for row in self.checks]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(len(case_ids), len(set(case_ids)))
        cases = {row["id"]: row for row in self.checks}
        referenced = set()
        for row in self.items:
            with self.subTest(requirement=row["id"]):
                self.assertRegex(row["id"], r"^[A-Z][A-Z0-9_.-]+$")
                self.assertIn(str(row["stage"]), self.matrix["stages"])
                self.assertIs(type(row["stage"]), int)
                self.assertTrue(row["name"] and row["contract"] and row["when"])
                self.assertTrue(row["checks"])
                self.assertEqual(len(row["checks"]), len(set(row["checks"])))
                self.assertTrue(set(row["checks"]).issubset(cases))
                self.assertTrue(any(cases[name]["level"] == "pc" for name in row["checks"]))
                if "alias_of" in row:
                    self.assertIn(row["alias_of"], {item["name"] for item in self.items})
                referenced.update(row["checks"])
        self.assertEqual(referenced, set(cases))
        for reference in self.matrix["references"]:
            self.assertTrue(set(reference["directly_supports"]).issubset(identifiers))
            self.assertRegex(reference["commit"], r"^[0-9a-f]{40}$")

    def test_planned_cases_are_not_reported_as_passed_tests(self):
        for case in self.checks:
            with self.subTest(case=case["id"]):
                self.assertIn(case["level"], {"pc", "hardware"})
                prefix = "PC-" if case["level"] == "pc" else "HW-"
                self.assertTrue(case["id"].startswith(prefix))
                self.assertTrue(case["assertions"])
                self.assertTrue(all(isinstance(value, str) and value for value in case["assertions"]))
                self.assertIn(case["status"], {"planned", "partial", "implemented"})
                if case["status"] == "planned":
                    self.assertIsNone(case["test"])
                else:
                    self.assertTrue(case["test"])
                    self.assertIsInstance(case["test"], list)
                    for reference in case["test"]:
                        filename, qualified = reference.split("::")
                        owner, name = qualified.split(".")
                        path = (ROOT / filename).resolve()
                        self.assertEqual(path.parent, ROOT / "tests")
                        tree = ast.parse(path.read_text(encoding="utf-8"))
                        self.assertTrue(any(
                            isinstance(node, ast.ClassDef) and node.name == owner
                            and any(isinstance(method, ast.FunctionDef) and method.name == name
                                    for method in node.body)
                            for node in tree.body
                        ), reference)
                if case["status"] == "partial":
                    self.assertTrue(case["remaining"])
        for item in self.items:
            self.assertIn(item["evidence"], {"static", "partial", "hardware"})
            self.assertIn(item["implementation"], {"pending", "partial", "implemented"})
            if item["implementation"] != "pending":
                cases = {case["id"]: case for case in self.checks}
                self.assertTrue(any(cases[name]["test"] for name in item["checks"]))

    def test_gaps_and_coverage_summary_match_the_inventory(self):
        gaps = {row["id"]: row for row in self.matrix["gaps"]}
        self.assertEqual(len(gaps), len(self.matrix["gaps"]))
        for item in self.items:
            self.assertTrue(set(item["blockers"]).issubset(gaps))
        for identifier, gap in gaps.items():
            with self.subTest(gap=identifier):
                expected = {row["id"] for row in self.items if identifier in row["blockers"]}
                self.assertTrue(expected)
                self.assertEqual(set(gap["items"]), expected)
                self.assertTrue(gap["observation"] and gap["close_when"])
                self.assertIn(gap["status"], {"open", "resolved"})
        summary = self.matrix["coverage"]
        self.assertEqual(summary["requirements"], len(self.items))
        self.assertEqual(summary["checks"], len(self.checks))
        self.assertEqual(summary["kinds"], dict(Counter(row["kind"] for row in self.items)))
        self.assertEqual(summary["check_levels"], dict(Counter(row["level"] for row in self.checks)))
        self.assertEqual(summary["implementations"], dict(Counter(row["implementation"] for row in self.items)))
        self.assertEqual(summary["check_statuses"], dict(Counter(row["status"] for row in self.checks)))


if __name__ == "__main__":
    unittest.main()
