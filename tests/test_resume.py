import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.resume import Resume
from cfs.lifecycle import Lifecycle, LifecycleStopped


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.sd = SimpleNamespace(resume_tnn=None, print_file_name_path="missing")
        self.move = SimpleNamespace(speed=12)
        self.fan = SimpleNamespace(last_value=0.5, scale=255)
        self.state = SimpleNamespace(Tnn_map={"T1A": "T1A"},
            get_Tnn_content=lambda t: {"addr": 1, "num": "A"},
            get_Tn_data=lambda *args: "connect",
            clear_e_err=lambda: self.calls.append("clear"))
        self.life = SimpleNamespace(_report_guard=lambda *a, **k: lambda: None,
            record_error=lambda *args: self.calls.append(args))
        objects = {"virtual_sdcard": self.sd, "gcode_move": self.move, "output_pin fan0": self.fan}
        self.box = SimpleNamespace(box_state=self.state, lifecycle=self.life,
            _printer=SimpleNamespace(lookup_object=lambda name, default=None: objects.get(name, default)),
            gcode=SimpleNamespace(respond_info=self.calls.append, run_script_from_command=self.calls.append),
            store=SimpleNamespace(recovery_hints=lambda: {}), last_cmd="T1A", error=None,
            get_filament_sensor_detect=lambda: True, move_z=2,
            record_tn_move_z=lambda x: self.calls.append(("z", x)),
            motion=SimpleNamespace(z_down=lambda **k: self.calls.append("down"),
                go_to_extrude_pos=lambda: self.calls.append("position"),
                move_to_safe_pos=lambda: self.calls.append("safe"),
                z_restore=lambda: self.calls.append("restore")),
            communication_set_box_mode=lambda *a: self.calls.append(a),
            get_flush_velocity=lambda *a: (2, 3), get_flush_temp=lambda *a: (4, 5, 6),
            quickly_wait_heating=lambda x: self.calls.append(("heat", x)),
            Tn_Extrude=lambda *a: True, boxcfg=SimpleNamespace(Tn_extrude=8),
            nozzle_clean=lambda: self.calls.append("clean"))
        self.resume = Resume(self.box)

    def test_queue_and_shutdown_preserve_replacement(self):
        cmd = SimpleNamespace(get_command_parameters=lambda: {"TNN": "T1A"})
        self.resume.cmd_add_tnn(cmd)
        self.assertEqual(self.sd.resume_tnn, "T1A")
        self.sd.resume_tnn = "T2A"
        self.resume.shutdown()
        self.assertEqual(self.sd.resume_tnn, "T2A")
        self.resume.cmd_add_tnn(cmd)
        self.resume.shutdown()
        self.assertIsNone(self.sd.resume_tnn)

    def test_restore_hints_and_enable(self):
        self.box.store.recovery_hints = lambda: {"enable": 1, "tnn_map": {"T1A": "T2A"}, "last_cmd": "T2A"}
        self.resume.cmd_power_loss_restore(None)
        self.assertEqual(self.calls, ["BOX_ENABLE_CFS_PRINT ENABLE=1"])
        self.assertEqual(self.state.Tnn_map["T1A"], "T2A")
        self.assertEqual(self.box.last_cmd, "T2A")

    def test_resume_requires_fresh_head_and_completes_normal_tail(self):
        self.sd.resume_tnn = "T1A"
        self.box.get_filament_sensor_detect = lambda: False
        self.resume.cmd_box_resume_extrude(None)
        self.assertEqual(self.calls, [])
        self.box.get_filament_sensor_detect = lambda: True
        self.resume.cmd_box_resume_extrude(None)
        self.assertIn("SET_PIN PIN=fan0 VALUE=127.50", self.calls)
        self.assertEqual(self.calls[-1], "restore")
        self.assertEqual(self.move.speed, 12)

    def test_exact_false_records_error_without_normal_tail(self):
        self.sd.resume_tnn = "T1A"
        self.box.Tn_Extrude = lambda *a: False
        self.assertIs(self.resume.cmd_box_resume_extrude(None), False)
        self.assertIn("clear", self.calls)
        self.assertNotIn("restore", self.calls)

    def test_noop_and_error_branches(self):
        self.assertIs(self.resume.cmd_box_resume_extrude(None), False)
        self.sd.resume_tnn = "T1A"
        self.box.last_cmd = None
        self.state.get_Tnn_content = lambda t: (_ for _ in ()).throw(KeyError(t)) if t is None else {"addr": 1, "num": "A"}
        with self.assertRaises(KeyError):
            self.resume.cmd_box_resume_extrude(None)
        self.box.last_cmd = "T1A"
        self.state.get_Tn_data = lambda *a: "disconnect"
        self.assertIs(self.resume.cmd_box_resume_extrude(None), False)
        self.state.get_Tn_data = lambda *a: "connect"
        self.box.error = "cut_err"
        self.assertIsNone(self.resume.cmd_box_resume_extrude(None))
        self.box.error = None
        self.state.Tnn_map["T1A"] = "T2A"
        self.state.get_Tnn_content = lambda t: {"addr": int(t[1]), "num": "A"}
        self.assertIsNone(self.resume.cmd_box_resume_extrude(None))
        self.assertEqual(self.calls, [])

    def test_invalid_metadata_and_lookup_errors_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "print.json"
            path.write_text("{", encoding="utf-8")
            self.sd.print_file_name_path = str(path)
            with self.assertRaises(ValueError):
                self.resume.cmd_box_resume_extrude(None)
        self.box._printer.lookup_object = lambda name: (_ for _ in ()).throw(LookupError(name))
        with self.assertRaises(LookupError):
            self.resume.cmd_box_resume_extrude(None)

    def test_real_lifecycle_guard_rejects_replacement_and_shutdown(self):
        for stage in ("head", "heat", "fan", "shutdown"):
            with self.subTest(stage=stage):
                self.setUp()
                self.sd.resume_tnn = "T1A"
                generation = [1]
                self.box.device_versions = SimpleNamespace(generation=lambda address: generation[0])
                self.box._binding_generation = 1
                self.box.operations = SimpleNamespace(_serial=0, active=False)
                self.life.box = self.box
                self.life._generation = 1
                self.life.check = lambda value: (_ for _ in ()).throw(LifecycleStopped()) if value != self.life._generation else None
                self.life._report_guard = lambda *a, **kw: Lifecycle._report_guard(self.life, *a, **kw)
                def change():
                    if stage == "shutdown":
                        self.life._generation += 1
                    else:
                        generation[0] += 1
                if stage in ("head", "shutdown"):
                    self.box.get_filament_sensor_detect = lambda: (change(), True)[1]
                elif stage == "heat":
                    self.box.quickly_wait_heating = lambda temp: change()
                else:
                    lookup = self.box._printer.lookup_object
                    def changed_lookup(name, *args):
                        result = lookup(name, *args)
                        if name == "output_pin fan0": change()
                        return result
                    self.box._printer.lookup_object = changed_lookup
                with self.assertRaises(LifecycleStopped):
                    self.resume.cmd_box_resume_extrude(None)
                self.assertNotIn("SET_PIN PIN=fan0 VALUE=0", self.calls)

    def test_invalid_queued_tool_rejected(self):
        cmd = SimpleNamespace(get_command_parameters=lambda: {"TNN": "T1A\nM112"})
        with self.assertRaises(ValueError):
            self.resume.cmd_add_tnn(cmd)
        self.assertIsNone(self.sd.resume_tnn)

    def test_falsey_target_falls_back_and_missing_fan_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "print.json"
            path.write_text('{"tn_cur_tnn":"T1A"}', encoding="utf-8")
            self.sd.print_file_name_path = str(path)
            self.sd.resume_tnn = ""
            lookup = self.box._printer.lookup_object
            self.box._printer.lookup_object = lambda name, *args: None if name == "output_pin fan0" else lookup(name, *args)
            self.box.get_filament_sensor_detect = lambda: 1
            self.resume.cmd_box_resume_extrude(None)
            self.assertIn("restore", self.calls)
            self.assertIn("SET_PIN PIN=fan0 VALUE=0", self.calls)
            self.assertFalse(any(x == "SET_PIN PIN=fan0 VALUE=127.50" for x in self.calls))

    def test_file_metadata_fallback_and_none_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "print.json"
            path.write_text('{"tn_cur_tnn":"T1A"}', encoding="utf-8")
            self.sd.print_file_name_path = str(path)
            self.box.Tn_Extrude = lambda *a: None
            self.resume.cmd_box_resume_extrude(None)
            self.assertIn("restore", self.calls)


if __name__ == "__main__":
    unittest.main()
