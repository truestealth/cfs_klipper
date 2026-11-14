"""PC admission through the real entry point, using existing owner substitutes."""

import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.box import load_config
from cfs.storage import new_base_data
from test_configuration import FakeConfig
from test_lifecycle import Native, Printer, Reactor


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root / "creality/userdata/box/tn_data.json"
        self.native, self.reactor = Native(), Reactor()
        self.printer = Printer(self.native, self.reactor, None)
        self.loads = []

        def load_object(config, name):
            self.loads.append(name)
            return self.printer.lookup_object(name)

        self.printer.load_object = load_object
        self.config = FakeConfig({"bus": "custom", "version": 1})
        self.config.get_printer = lambda: self.printer
        base_info = ModuleType("extras.base_info")
        base_info.base_dir = str(self.root)
        self.base_info = base_info
        self.modules = patch.dict(sys.modules, {"extras.base_info": base_info})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_real_loader_registers_passive_lifecycle_and_borrows_configured_owner(self):
        box = load_config(self.config)
        self.assertIs(box._printer, self.printer)
        self.assertTrue(box.lifecycle.passive)
        self.assertEqual(box.lifecycle.families, dict.fromkeys(range(1, 5), "cfs0"))
        self.assertEqual(self.loads, ["serial_485 custom"])
        self.assertIsNone(box.native)
        self.assertEqual(self.reactor.timers, [])
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.printer.gcode.scripts, [])
        self.assertIn("BOX_GET_VERSION_SN", self.printer.gcode.commands)
        self.assertIn("T15", self.printer.gcode.commands)
        self.assertIn("box/get_box_cfg", self.printer.endpoints)
        self.assertIn("klippy:ready", self.printer.events)
        self.assertNotIn("toolhead", self.printer.lookups)
        self.assertEqual(box.store.path, self.data)
        self.assertEqual(box.store.material_database, self.data.with_name("material_database.json"))
        self.assertEqual(Path(box.flushing_sign), self.root / "creality/userdata/config/flushing_sign")
        self.assertIsNone(box.config_paths)  # Retain the established save paths.
        self.assertIsNone(box.preloading_poweron)

    def test_saved_settings_and_recovery_hints_are_preserved_without_restoring_live_state(self):
        document = {"base_data": new_base_data(), "enable": 0,
                    "tnn_map": {"T%d%s" % (address, slot): "T1A"
                                for address in range(1, 5) for slot in "ABCD"},
                    "last_cmd": "T4D", "screen_owned": {"keep": True}}
        document["base_data"]["T1"]["color_value"][0] = "123456"
        self.data.parent.mkdir(parents=True)
        original = json.dumps(document).encode("utf-8")
        self.data.write_bytes(original)
        box = load_config(self.config)
        self.assertEqual(self.data.read_bytes(), original)
        self.assertEqual(box.store.base_data["T1"]["color_value"][0], "123456")
        self.assertEqual(box.box_state.Tn_data["T1"]["color_value"][0], "-1")
        self.assertEqual(box.box_state.Tn_data["enable"], 1)
        self.assertEqual(box.box_state.Tnn_map["T4D"], "T4D")
        self.assertIsNone(box.last_cmd)

    def test_entry_ready_heartbeat_shutdown_and_re_ready_keep_borrowed_owners(self):
        self.data.parent.mkdir(parents=True)
        saved = new_base_data()
        saved["T1"]["color_value"][0] = "123456"
        original = json.dumps({"base_data": saved, "last_cmd": "T4D"}).encode("utf-8")
        self.data.write_bytes(original)
        port = self.root / "synthetic-port"
        port.touch()
        self.native.serial_port = str(port)
        table_module, greenlet = ModuleType("extras.auto_addr_wrapper"), ModuleType("greenlet")
        table_module.addr_manager_table_mb = [SimpleNamespace(online=value) for value in (1, 0, 0, 0)]
        greenlet.getcurrent = lambda: "pc-worker"
        with patch.dict(sys.modules, {"extras.auto_addr_wrapper": table_module, "greenlet": greenlet}):
            box = load_config(self.config)
            # The retained address owner can publish connect before Box's first
            # heartbeat. A cached connection is not a fetched device identity.
            box.box_state.modify_Tn_data(1, "connect", "state")
            self.printer.send_event("klippy:ready")
            lifecycle = box.lifecycle
            self.assertIs(lifecycle.address_table, table_module.addr_manager_table_mb)
            self.assertIs(box.native.native, self.native)
            self.assertTrue(lifecycle.running)
            self.assertEqual(self.native.calls, [])
            self.reactor.fire(lifecycle._timer)
            self.assertEqual(box.device_versions.get(1).version, "1.1.3")
            self.assertEqual(box.box_state.Tn_data["T1"]["color_value"][0], "123456")
            self.assertEqual(box.box_state.get_Tn_data(1, "state"), "connect")
            self.assertEqual(box.box_state.get_Tn_data(0, uppart="state"), "connect")
            self.assertEqual(self.native.commands()[0][:2], (1, 0x14))
            self.assertTrue(all(command in (0x02, 0x03, 0x05, 0x08, 0x0A, 0x14)
                                for _, command, _ in self.native.commands()))
            self.assertEqual(self.printer.gcode.scripts, [])
            self.printer.send_event("klippy:shutdown")
            self.assertFalse(lifecycle.running)
            self.assertIsNone(box.native)
            self.assertEqual(self.reactor.timers, [])
            self.assertIs(self.printer.objects["serial_485 custom"], self.native)
            self.assertTrue(self.native.is_connected)
            self.assertEqual(box.box_state.get_Tn_data(1, "state"), "disconnect")
            self.assertIsNone(box.device_versions.get(1))
            self.native.calls.clear()
            self.printer.send_event("klippy:ready")
            self.reactor.fire(lifecycle._timer)
            self.assertEqual(self.native.commands()[0][:2], (1, 0x14))
            self.assertEqual(box.device_versions.get(1).version, "1.1.3")
            self.assertEqual(self.loads, ["serial_485 custom"])
            self.assertEqual(self.printer.gcode.scripts, [])
            self.assertEqual(self.data.read_bytes(), original)
            self.printer.send_event("klippy:shutdown")
            self.assertEqual(self.reactor.timers, [])

    def test_missing_cache_creates_only_compatible_metadata(self):
        load_config(self.config)
        self.assertEqual(json.loads(self.data.read_text(encoding="utf-8")),
                         {"base_data": new_base_data()})
        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [self.data])

    def test_invalid_data_root_fails_before_dependencies_registration_or_writes(self):
        for value in (None, "", "relative", "bad\0path", 42):
            with self.subTest(value=value):
                self.base_info.base_dir = value
                with self.assertRaisesRegex(ValueError, "absolute data directory"):
                    load_config(self.config)
                self.assertEqual(self.loads, [])
                self.assertEqual(self.printer.events, {})
                self.assertEqual(self.printer.gcode.commands, {})
                self.assertFalse(self.data.exists())

    def test_missing_base_info_keeps_original_import_error_as_cause(self):
        with patch("cfs.box.importlib.import_module", side_effect=ImportError("base info absent")):
            with self.assertRaisesRegex(ValueError, "base info absent") as raised:
                load_config(self.config)
        self.assertIsInstance(raised.exception.__cause__, ImportError)
        self.assertEqual(self.loads, [])
        self.assertFalse(self.data.exists())

    def test_missing_configured_native_owner_fails_without_cache_or_registration(self):
        del self.printer.objects["serial_485 custom"]
        with self.assertRaisesRegex(ValueError, "serial_485 custom") as raised:
            load_config(self.config)
        self.assertIsInstance(raised.exception.__cause__, KeyError)
        self.assertEqual(self.loads, ["serial_485 custom"])
        self.assertEqual(self.printer.events, {})
        self.assertEqual(self.printer.gcode.commands, {})
        self.assertFalse(self.data.exists())
        self.assertEqual(self.native.calls, [])

    def test_missing_webhooks_does_not_load_native_or_create_cache(self):
        del self.printer.objects["webhooks"]
        with self.assertRaisesRegex(ValueError, "webhooks"):
            load_config(self.config)
        self.assertEqual(self.loads, [])
        self.assertEqual(self.printer.events, {})
        self.assertFalse(self.data.exists())

    def test_missing_bus_fails_before_base_info_or_printer_access(self):
        self.config.values.pop("bus")
        self.config.get_printer = lambda: self.fail("printer must not be acquired")
        with patch("cfs.box.importlib.import_module", side_effect=AssertionError("no import")):
            with self.assertRaisesRegex(ValueError, "set the serial_485 section name"):
                load_config(self.config)
        self.assertEqual(self.loads, [])

    def test_corrupt_saved_metadata_is_preserved_and_does_not_register_partial_box(self):
        self.data.parent.mkdir(parents=True)
        original = b"{incomplete metadata"
        self.data.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "could not initialize") as raised:
            load_config(self.config)
        self.assertIsInstance(raised.exception.__cause__, ValueError)
        self.assertEqual(self.data.read_bytes(), original)
        self.assertEqual(self.printer.events, {})
        self.assertEqual(self.printer.gcode.commands, {})
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.reactor.timers, [])


if __name__ == "__main__":
    unittest.main()
