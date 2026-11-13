"""Integrate the borrowed native API, decoder and device identities on a PC."""

from pathlib import Path
import struct
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cfs_box.box import Box, MissingProtocolEffect, StaleReplyError
from cfs_box.native import build_request
from test_box_data import Command, CommandError, Gcode, Webhooks
from test_configuration import FakeConfig
from test_native import reply


class Native:
    def __init__(self):
        self.calls = []
        self.replies = []
        self.is_connected = True
        self.handlers = {}

    def cmd_send_data_with_response(self, *arguments):
        self.calls.append(arguments)
        value = self.replies.pop(0)
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value

    def register_response(self, callback, address, command):
        self.handlers[address, command] = callback

    def _disconnect(self):
        raise AssertionError("the extension must not disconnect borrowed serial")

    def raw_send(self, data):
        raise AssertionError("the extension must use the stock response API")


class Reactor:
    def __init__(self):
        self.now = 10.0
        self.pauses = []
        self.callbacks = []

    def monotonic(self):
        return self.now

    def pause(self, deadline):
        self.pauses.append(deadline)
        self.now = deadline

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def run_callbacks(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback(self.now)


class Printer:
    def __init__(self, native, reactor):
        self.native, self.reactor = native, reactor
        self.lookups = []

    def lookup_object(self, name):
        self.lookups.append(name)
        if name != "serial_485 custom_bus":
            raise AssertionError("configured bus must be used")
        return self.native

    def get_reactor(self):
        return self.reactor


class NativeCommand(Command):
    def get_int(self, name, default=Command._missing, **bounds):
        if name not in self.parameters and default is not Command._missing:
            return default
        return super().get_int(name, default, **bounds)

    def get_float(self, name, default, minval=None, maxval=None):
        value = float(self.parameters.get(name, default))
        if minval is not None and value < minval:
            raise self.error("below " + name)
        if maxval is not None and value > maxval:
            raise self.error("above " + name)
        return value


class BoxNativeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.box = Box(FakeConfig({"bus": "custom_bus", "version": 1}), self.directory.name)
        self.gcode = Gcode()
        self.box.register_data_commands(self.gcode, Webhooks())
        self.box.register_native_commands(self.gcode)
        self.native = Native()
        self.reactor = Reactor()
        self.printer = Printer(self.native, self.reactor)
        self.box.bind_native(self.printer, families={address: "cfs0" for address in range(1, 5)})

    def identify(self, address=1, version="113", serial=None):
        if serial is None:
            serial = bytes((64 + address,)) * 19
        self.native.replies.append(reply(address, 0x14, data=version.encode("ascii") + serial))
        self.assertIs(self.box.send_native(build_request(address, "GET_VERSION_SN")), True)

    def command(self, name, **parameters):
        return self.gcode.commands[name](NativeCommand(parameters))

    def test_binding_uses_configured_bus_without_connecting_or_projecting_presence(self):
        self.assertEqual(self.printer.lookups, ["serial_485 custom_bus"])
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.native.handlers, {})
        self.assertEqual(len(self.gcode.commands), 21)
        binding = self.box.native
        with self.assertRaises(ValueError):
            self.box.bind_native(self.printer, effect_handlers={"identity": lambda *args: None})
        self.assertIs(self.box.native, binding)
        self.native.is_connected = False
        self.assertIs(self.box.native.is_connected, False)
        self.assertEqual(self.box.get_status(0)["state"], "None")
        self.assertTrue(all(self.box.get_status(0)["T%d" % address]["state"] == "None"
                            for address in range(1, 5)))
        self.box.unbind_native()
        self.assertIsNone(self.box.native)
        self.assertFalse(self.native.is_connected)

    def test_unknown_hardware_or_version_never_inherits_baseline_behavior(self):
        self.box.bind_native(self.printer)
        self.identify()
        self.assertIsNone(self.box.device_versions.profile(1))
        calls = len(self.native.calls)
        for command, data in (("GET_BOX_STATE", b""), ("SET_PRE_LOADING", b"\x01\x02")):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.box.send_native(build_request(1, command, data))
        self.assertEqual(len(self.native.calls), calls)
        self.box.device_versions.set_family(1, "cfs0")
        self.assertEqual(self.box.device_versions.require_supported(1).version, "1.1.3")
        self.identify(version="999")
        self.assertIsNone(self.box.device_versions.profile(1))
        with self.assertRaises(ValueError):
            self.box.send_native(build_request(1, "GET_BUFFER_STATE"))

    def test_rebinding_clears_public_identity_without_resetting_preferences(self):
        self.identify()
        self.box.box_state.modify_Tn_data(0, 0, uppart="enable")
        before = self.box.store.path.read_bytes()
        self.box.bind_native(self.printer, families={1: "cfs0"})
        self.assertIsNone(self.box.device_versions.get(1))
        self.assertEqual(self.box.get_status(0)["T1"]["version"], "-1")
        self.assertEqual(self.box.get_status(0)["T1"]["sn"], "-1")
        self.assertEqual(self.box.get_status(0)["enable"], 0)
        self.assertEqual(self.box.store.path.read_bytes(), before)

    def test_mixed_addresses_select_four_and_six_byte_layouts_independently(self):
        versions = ("113", "142", "152", "153")
        for address, version in enumerate(versions, 1):
            self.identify(address, version)
            data = bytes((20 + address, 40 + address, 1, address))
            if address > 1:
                data += bytes((8 + address, 16 + address))
            self.native.replies.append(reply(address, 0x0A, data=data))
            self.assertIs(self.box.send_native(build_request(address, "GET_BOX_STATE")), True)
        status = self.box.get_status(0)
        for address, version in enumerate(versions, 1):
            row = status["T%d" % address]
            self.assertEqual(row["version"], ".".join(version))
            self.assertEqual(row["temperature"], str(20 + address))
            self.assertEqual(row["mode"], str(address))
            self.assertEqual(row["state"], "None")
        self.assertNotIn(1, self.box.state_extensions)
        self.assertEqual(self.box.state_extensions[2], {"connection_mask": 10, "tag_predicate_mask": 18})
        self.assertTrue(all(len(arguments) == 2 for arguments in self.native.calls))
        self.assertEqual([arguments[1] for arguments in self.native.calls[1::2]], [3600] * 4)

    def test_command_data_updates_keep_buffers_banks_modes_and_source_distinct(self):
        self.identify()
        scenarios = (
            ("GET_BUFFER_STATE", b"", b"\x02"),
            ("GET_FILAMENT_SENSOR_STATE", b"\x00", b"\x05"),
            ("GET_FILAMENT_SENSOR_STATE", b"\x01", b"\x08"),
            ("GET_REMAIN_LEN", b"\x05", bytes((10, 11, 12, 13))),
            ("MEASURING_WHEEL", b"\x01", struct.pack(">f", 1.25)),
            ("SET_BOX_MODE", b"\x01\x00", b""),
        )
        for name, request_data, data in scenarios:
            request = build_request(1, name, request_data)
            self.native.replies.append(reply(1, request.command, data=data))
            self.assertIs(self.box.send_native(request), True)
        inner = self.box.box_state.Tn_inner_data
        self.assertEqual(inner["buffer"], 2)
        self.assertEqual(inner["T1"]["filament_sensor"], {"material": 5, "connections": 8})
        self.assertEqual(inner["T4"]["filament_sensor"], {"material": 0, "connections": 0})
        self.assertEqual(self.box.get_status(0)["T1"]["remain_len"], ["10", "-1", "12", "-1"])
        self.assertEqual(self.box.get_status(0)["T1"]["measuring_wheel"], 1.25)
        self.assertEqual(self.box.get_status(0)["T1"]["mode"], "None")
        self.assertIsNone(self.box.last_cmd)

    def test_malformed_frames_or_payloads_cannot_refresh_valid_observations(self):
        self.identify()
        request = build_request(1, "GET_BOX_STATE")
        before = self.box.get_status(0)
        for raw in (reply(2, 0x0A, data=b"\x01" * 4), reply(1, 0x0A, data=b"\x01" * 3), b"broken"):
            self.native.replies.append(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.box.send_native(request)
            self.assertEqual(self.box.get_status(0), before)
        self.native.replies.append(reply(1, 0x14, data=b"bad" + b"A" * 19))
        with self.assertRaises(ValueError):
            self.box.send_native(build_request(1, "GET_VERSION_SN"))
        self.assertIsNone(self.box.device_versions.profile(1))
        self.assertEqual(self.box.get_status(0)["T1"]["version"], "-1")
        self.assertEqual(self.box.device_versions.last_raw_reply(1),
                         reply(1, 0x14, data=b"bad" + b"A" * 19))

    def test_reconnect_replacement_and_late_replies_invalidate_only_their_address(self):
        self.identify(1)
        self.identify(2)
        self.box.invalidate_device(1)
        self.identify(1)
        self.assertIsNotNone(self.box.device_versions.profile(1))
        self.identify(1, serial=b"replacement-device!")
        self.assertIsNone(self.box.device_versions.profile(1))
        self.assertIsNotNone(self.box.device_versions.profile(2))
        self.box.device_versions.set_family(1, "cfs0")
        before = self.box.get_status(0)["T1"]["temperature"]

        def late():
            self.box.invalidate_device(1)
            return reply(1, 0x0A, data=b"\x63\x02\x03\x04")

        self.native.replies.append(late)
        with self.assertRaises(StaleReplyError):
            self.box.send_native(build_request(1, "GET_BOX_STATE"))
        self.assertEqual(self.box.get_status(0)["T1"]["temperature"], before)
        self.assertIsNotNone(self.box.device_versions.profile(2))

    def test_error_effects_require_handlers_and_preserve_snapshot_and_order(self):
        self.identify()
        request = build_request(1, "EXTRUDE_PROCESS", b"\x01\x04\x00")
        self.native.replies.append(reply(1, 0x10, status=0x01))
        with self.assertRaises(MissingProtocolEffect):
            self.box.send_native(request)
        self.assertIsNone(self.box.extrude_process_state)
        events = []
        self.box._effect_handlers["report_error"] = lambda key, facts: events.append(
            (key, facts["address"], self.box.extrude_process_state))
        self.native.replies.append(reply(1, 0x10, status=0x01))
        self.assertIs(self.box.send_native(request), False)
        self.assertEqual(events, [("key_params_err", 1, "PARAMS_ERR")])
        self.native.replies.append(reply(1, 0x10, status=0x03))
        with self.assertLogs(level="WARNING"):
            self.assertIs(self.box.send_native(request), True)
        self.assertEqual(self.box.extrude_process_state, "STATE_ERR")

    def test_retries_stop_before_sending_with_a_changed_device_context(self):
        self.identify()
        calls = len(self.native.calls)

        def replaced():
            self.box.invalidate_device(1)
            return reply(1, 0x10, status=0x02)

        self.native.replies.extend((replaced, reply(1, 0x10)))
        with self.assertRaises(StaleReplyError):
            self.box.send_native(build_request(1, "EXTRUDE_PROCESS", b"\x01\x04\x00"))
        self.assertEqual(len(self.native.calls), calls + 1)
        self.assertEqual(len(self.native.replies), 1)
        self.assertIsNone(self.box.extrude_process_state)

    def test_old_reply_failure_cannot_invalidate_a_new_binding_identity(self):
        self.identify()

        def rebound():
            self.box.bind_native(self.printer, families={1: "cfs0"})
            payload = b"142" + b"B" * 19
            self.box._accept_identity(1, payload, reply(1, 0x14, data=payload))
            return b"broken old reply"

        self.native.replies.append(rebound)
        with self.assertRaises(StaleReplyError):
            self.box.send_native(build_request(1, "GET_VERSION_SN"))
        self.assertEqual(self.box.device_versions.require_supported(1).version, "1.4.2")
        self.assertEqual(self.box.get_status(0)["T1"]["version"], "1.4.2")

    def test_detach_inside_an_error_hook_stops_remaining_metadata_effects(self):
        self.identify()
        before = self.box.store.path.read_bytes()
        self.box._effect_handlers["report_error"] = lambda key, facts: self.box.unbind_native()
        self.native.replies.append(reply(1, 0x02, data=b"A:busy;"))
        with self.assertRaises(StaleReplyError):
            self.box.send_native(build_request(1, "GET_RFID", b"\x01"))
        self.assertEqual(self.box.get_status(0)["T1"]["vender"], ["-1"] * 4)
        self.assertEqual(self.box.store.path.read_bytes(), before)
        self.assertEqual(self.reactor.callbacks, [])

    def test_failed_identity_reads_keep_timeout_and_error_outcomes_after_invalidation(self):
        seen = []
        self.box._effect_handlers.update({
            "timeout_process": lambda address: seen.append(("timeout", address)),
            "report_error": lambda key, facts: seen.append((key, facts["address"])),
        })
        for raw in (None, reply(1, 0x14, status=1)):
            self.identify()
            self.native.replies.append(raw)
            self.assertIs(self.box.send_native(build_request(1, "GET_VERSION_SN")), False)
            self.assertIsNone(self.box.device_versions.get(1))
            self.assertEqual(self.box.get_status(0)["T1"]["version"], "-1")
        self.assertEqual(seen, [("timeout", 1), ("key_params_err", 1)])

    def test_timeouts_events_and_communication_bytes_have_separate_outcomes(self):
        self.identify()
        self.native.replies.append(None)
        self.assertIsNone(self.box.send_native(build_request(1, "GET_BOX_STATE")))
        self.native.replies.append(None)
        with self.assertRaises(MissingProtocolEffect):
            self.box.send_native(build_request(1, "GET_BUFFER_STATE"))
        seen = []
        self.box._effect_handlers["timeout_process"] = lambda address: seen.append(("timeout", address))
        self.native.replies.append(None)
        self.assertIs(self.box.send_native(build_request(1, "GET_BUFFER_STATE")), False)
        before = self.box.get_status(0)
        self.box._effect_handlers["state_events"] = lambda address, data: seen.append(("event", address, data))
        self.native.replies.append(reply(1, 0x0A, status=0x30, data=b"\x01\x02\x03\x04"))
        self.assertIs(self.box.send_native(build_request(1, "GET_BOX_STATE")), True)
        self.assertEqual(self.box.get_status(0), before)
        self.assertEqual(seen, [("timeout", 1), ("event", 1, b"\x01\x02\x03\x04")])
        self.native.replies.append(reply(1, 0x55, status=2, data=b"\x00"))
        self.assertIs(type(self.box.send_native(build_request(1, "COMMUNICATION_TEST", b"\xff"))), int)

    def test_rfid_metadata_and_deferred_groups_are_cancelled_on_unbind(self):
        self.identify()
        self.native.replies.append(reply(1, 0x02, data=b"A:unknown;B:none;"))
        self.assertIs(self.box.send_native(build_request(1, "GET_RFID", b"\x03")), True)
        self.assertEqual(self.box.get_status(0)["T1"]["vender"][:2], ["unknown", "none"])
        self.assertEqual(len(self.reactor.callbacks), 1)
        self.box.box_state.Tn_data["same_material"] = ["unchanged-after-unbind"]
        self.box.unbind_native()
        self.reactor.run_callbacks()
        self.assertEqual(self.box.box_state.Tn_data["same_material"], ["unchanged-after-unbind"])

    def test_raw_diagnostic_keeps_digit_encoding_and_explicit_missing_field_paths(self):
        self.identify()
        self.native.replies.append(reply(1, 0x0A, data=b"\x01\x02\x03\x04"))
        self.assertIsNone(self.command("BOX_SEND_DATA", ADDR="1", STATE="255", CMD="10", DATA="15"))
        self.assertEqual(self.native.calls[-1], (b"\x01\x05\xff\x0a\x01\x05", 5))
        self.native.replies.append(reply(1, 0x0A, data=b"\x01\x02\x03\x04"))
        self.command("BOX_SEND_DATA", ADDR="1")
        self.assertEqual(self.native.calls[-1], (b"\x01\x03\x00\x0a", 5))
        calls = len(self.native.calls)
        with self.assertLogs(level="INFO"), self.assertRaises(CommandError):
            self.command("BOX_SEND_DATA")
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(self.gcode.messages[-1], "state is None")

    def test_communication_test_counts_messages_and_uses_cooperative_intervals(self):
        self.native.replies.extend((reply(1, 0x55, data=b"\x00"),
                                    reply(1, 0x55, data=b"\x02"), None))
        self.assertIsNone(self.command("BOX_COMMUNICATION_TEST", ADDR="1", NUM="255", COUNT="3"))
        self.assertEqual([arguments[0][-1] for arguments in self.native.calls], [255] * 3)
        self.assertEqual([arguments[1] for arguments in self.native.calls], [0.1] * 3)
        self.assertEqual(len(self.reactor.pauses), 3)
        self.assertIn("1.get return data(0x2) error, send data: 0xff", self.gcode.messages)
        self.assertIn("1.timeout, TIMEOUT=0.10, send data: 0xff", self.gcode.messages)
        self.assertEqual(self.gcode.messages[-1], "2. addr[1] test finish")
        before = len(self.gcode.messages)
        self.command("BOX_COMMUNICATION_TEST", ADDR="1", NUM="0", COUNT="0")
        self.assertEqual(len(self.gcode.messages), before)

    def test_cli_required_fields_enums_masks_and_cached_five_way_presentation(self):
        self.identify()
        for name in ("BOX_GET_RFID", "BOX_GET_REMAIN_LEN"):
            with self.subTest(name=name), self.assertRaises(CommandError):
                self.command(name, ADDR="1")
        for name, key in (("BOX_MEASURING_WHEEL", "ACTION"), ("BOX_SET_BOX_MODE", "MODE"),
                          ("BOX_GET_FILAMENT_SENSOR_STATE", "POSITION")):
            with self.subTest(name=name), self.assertLogs(level="WARNING"):
                self.command(name, ADDR="1", **{key: "invalid"})
        self.native.replies.append(reply(1, 0x04))
        self.command("BOX_SET_BOX_MODE", ADDR="1", MODE="PRINT", NUM="D")
        self.assertEqual(self.native.calls[-1][0][-2:], b"\x08\x00")
        self.box.box_state.modify_Tn_data(1, "connect", "state")
        self.box.box_state.modify_Tn_inner_data(1, 10, "filament_sensor", subpart="connections")
        timeouts = []
        self.box._effect_handlers["timeout_process"] = timeouts.append
        self.native.replies.append(None)
        self.command("BOX_GET_FIVE_WAY_STATE")
        self.assertEqual(self.box.get_status(0)["T1"]["filament"], "B")
        self.assertEqual(self.native.calls[-1][0][-1], 1)
        self.assertEqual(timeouts, [1])

    def test_registered_native_handlers_match_their_body_and_reply_contracts(self):
        payload = b"113" + b"B" * 19
        cases = (
            ("BOX_GET_VERSION_SN", {}, 0x14, b"", payload, 2),
            ("BOX_CTRL_CONNECTION_MOTOR_ACTION", {"ACTION": "RETRUDE"}, 0x07, b"\x02", b"", 2),
            ("BOX_GET_FILAMENT_SENSOR_STATE", {"POSITION": "MATERIAL"}, 0x08, b"\x00", b"\x03", 2),
            ("BOX_SET_BOX_MODE", {"MODE": "IDLE"}, 0x04, b"\x00\x01", b"", 2),
            ("BOX_GET_BUFFER_STATE", {}, 0x05, b"", b"\x01", 2),
            ("BOX_GET_RFID", {"NUM": "1"}, 0x02, b"\x01", b"A:unknown;", 2),
            ("BOX_GET_REMAIN_LEN", {"NUM": "5"}, 0x03, b"\x05", b"\x01\x02\x03\x04", 2),
            ("BOX_GET_BOX_STATE", {}, 0x0A, b"", b"\x10\x20\x01\x02", 3600),
            ("BOX_MEASURING_WHEEL", {"ACTION": "GET"}, 0x0E, b"\x01", struct.pack(">f", 2.5), 2),
        )
        for name, parameters, command, data, response, timeout in cases:
            with self.subTest(name=name):
                self.native.replies.append(reply(2, command, data=response))
                self.assertIsNone(self.command(name, ADDR="2", **parameters))
                body = bytes((2, len(data) + 3, 255, command)) + data
                self.assertEqual(self.native.calls[-1], (body, timeout))
        self.assertEqual(self.box.get_status(0)["T2"]["measuring_wheel"], 2.5)
        self.assertEqual(self.gcode.scripts, [])


if __name__ == "__main__":
    unittest.main()
