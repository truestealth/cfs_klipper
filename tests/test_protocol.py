"""Synthetic validated replies: no UART, native library or printer execution."""

from copy import deepcopy
import json
import math
from pathlib import Path
import struct
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cfs_box.firmware import DeviceVersions
from cfs_box.native import Request, build_request, crc8, validate_reply
from cfs_box.protocol import DecodeContext, Effect, STATUS_NAMES, decode_reply
from cfs_box.state import BoxState


def reply(request, status=0, data=b""):
    body = bytes((len(data) + 3, status, request.command)) + data
    raw = bytes((0xF7, request.address)) + body + bytes((crc8(body),))
    return validate_reply(raw, request.address, request.command)


def profile(version="113", address=1):
    versions = DeviceVersions({address: "cfs0"})
    versions.update(address, version.encode("ascii") + b"0123456789012345678")
    return versions.require_supported(address)


def apply_slots(state, effects):
    """Bounded data-effect executor; operation effects are deliberately not run."""
    for effect in effects:
        if effect.name == "set_slot":
            address, slot, part, value = effect.args
            state.modify_Tn_data(address, value, part, slot)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.state = BoxState()

    def decode(self, command, status=0, data=b"", request_data=b"", context=None,
               version="113", address=1):
        request = Request(address, command, request_data, 2)
        frame = reply(request, status, data)
        return decode_reply(self.state, frame, request,
                            profile=profile(version, address), context=context)

    def test_all_25_host_statuses_have_exact_default_sender_outcomes(self):
        matrix = json.loads((ROOT / "docs/contract-matrix.json").read_text(encoding="utf-8"))
        expected = {row["code"]: row["name"] for row in matrix["items"] if row["kind"] == "response"}
        self.assertEqual(STATUS_NAMES, expected)
        self.assertEqual(len(STATUS_NAMES), 25)
        true_statuses = {0x00, 0x03, 0x16, 0x17, 0x30, 0x50}
        for status in STATUS_NAMES:
            with self.subTest(status=status):
                result = self.decode(0x10, status, b"\x00" * 4 if status == 0x30 else b"",
                                     b"\x01\x04\x00")
                self.assertIs(result.value, status in true_statuses)
                self.assertEqual(result.effects[0], Effect("snapshot_extrude_status", (STATUS_NAMES[status],)))
        result = self.decode(0x10, 0xFE, request_data=b"\x01\x04\x00")
        self.assertIs(result.value, False)
        self.assertEqual(result.effects, (Effect("snapshot_extrude_status", (None,)),))

    def test_state_error_and_parameters_ignore_the_generic_report_suppression(self):
        result = self.decode(0x04, 0x03, request_data=b"\x01\x00",
                             context=DecodeContext(report_err=False))
        self.assertIs(result.value, False)
        self.assertEqual([effect.name for effect in result.effects],
                         ["report_error", "record_error", "error_address"])
        self.assertEqual(result.effects[0].args[0], "key_state_err")
        self.assertEqual(result.effects[1].args, ("state_err", {"addr": 1}))
        for report in (False, True):
            result = self.decode(0x04, 0x01, request_data=b"\x01\x00",
                                 context=DecodeContext(report_err=report))
            self.assertEqual(result.effects[0].args[0], "key_params_err")
        result = self.decode(0x10, 0x03, request_data=b"\x01\x04\x00",
                             context=DecodeContext(report_err=False))
        self.assertIs(result.value, False)
        self.assertEqual(result.effects[1].args[0], "key_state_err")

    def test_report_guards_are_command_specific_and_not_a_blanket_nonzero_rule(self):
        guarded = (0x05, 0x09, 0x0B, 0x14, 0x15, 0x19, 0x1A)
        for status in guarded:
            for command in (0x04, 0x0A):
                for report in (False, True):
                    with self.subTest(status=status, command=command, report=report):
                        result = self.decode(command, status, request_data=b"\x01\x00",
                                             context=DecodeContext(report_err=report))
                        self.assertIs(result.value, False)
                        self.assertEqual(bool(result.effects), command != 0x0A and report)
        result = self.decode(0x04, 0x09, request_data=b"\x01\x00")
        self.assertEqual(result.effects[0].args[0], "key_extrude_err4")
        result = self.decode(0x04, 0x08, request_data=b"\x04\x00",
                             context=DecodeContext(report_err=False))
        self.assertEqual(result.effects[0].args[1]["slot"], "C")
        self.assertIn(Effect("set_global", ("filament", 0)), result.effects)
        self.assertEqual(self.decode(0x0A, 0x08).effects, ())
        self.assertEqual(self.decode(0x04, 0x22, context=DecodeContext(report_err=False)).effects, ())

    def test_retraction_errors_distinguish_missing_source_and_preserve_false_report_guard(self):
        for status in (0x16, 0x17):
            for source in (None, ""):
                self.assertIs(self.decode(0x11, status, context=DecodeContext(last_cmd=source)).value, True)
            result = self.decode(0x11, status, context=DecodeContext(last_cmd="T3D", report_err=False))
            self.assertIs(result.value, False)
            self.assertEqual(result.effects[0].args[0], "key_filament_err")
            self.assertEqual(result.effects[0].args[1]["slot"], "D")
            self.assertEqual(result.effects[-1], Effect("error_address", (1,)))
        self.assertEqual(self.decode(0x11, 0x13, context=DecodeContext(report_err=False)).effects, ())
        self.assertEqual(self.decode(0x11, 0x13).effects[0].name, "warning")

    def test_filament_error_bookkeeping_and_exact_false_guards(self):
        self.state.Tn_data["filament_useup"] = 0
        result = self.decode(0x04, 0x50, context=DecodeContext(last_cmd="T1A", print_state="printing"))
        self.assertIs(result.value, True)
        self.assertEqual([effect.name for effect in result.effects], [
            "set_global", "set_context", "schedule_tighten_up", "set_context", "record_error",
        ])
        self.assertEqual(result.effects[0], Effect("set_global", ("filament_useup", 1)))
        self.assertEqual(result.effects[-1].args, ("filament_err", {
            "last_tnn": "T1A", "tnn": "T1A", "addr": 1,
        }))
        for flag in (None, 0, True):
            context = DecodeContext(last_cmd="T1A", print_state="paused",
                                    box_filament_err=flag, is_use_ending_material=flag)
            result = self.decode(0x04, 0x50, context=context)
            self.assertEqual(result.effects, (Effect("set_global", ("filament_useup", 1)),))
        self.state.Tn_data["filament_useup"] = "0"
        result = self.decode(0x04, 0x50)
        self.assertNotIn("set_global", [effect.name for effect in result.effects])

    def test_direct_filament_error_duplicate_check_and_drain_effect_order(self):
        self.state.Tn_data["auto_refill"] = 0
        context = DecodeContext(last_cmd="T2B", box_filament_err=True)
        result = self.decode(0x04, 0x50, context=context)
        self.assertIs(result.value, False)
        self.assertEqual([effect.name for effect in result.effects], [
            "report_error", "set_global", "record_error", "set_context", "run_gcode", "set_context",
        ])
        self.assertEqual(result.effects[-2], Effect("run_gcode", ("BOX_EXTRUSION_ALL_MATERIALS",)))
        context.last_err = "same error"
        with self.assertRaises(ValueError):
            self.decode(0x04, 0x50, context=context)
        context.filament_error_message = "same error"
        self.assertEqual(self.decode(0x04, 0x50, context=context).effects, ())
        context.filament_error_message = "other error"
        self.assertEqual(self.decode(0x04, 0x50, context=context).effects[0].name, "report_error")

    def test_speed_and_enwind_error_order_and_raw_none_source_guard(self):
        for status in (0x51, 0x52):
            for context in (DecodeContext(last_cmd=None), DecodeContext(last_cmd="T1A", error="filament_err")):
                self.assertEqual(self.decode(0x04, status, context=context).effects, ())
        context = DecodeContext(last_cmd="T1A", report_err=False)
        self.state.Tn_data["filament_useup"] = 0
        speed = self.decode(0x04, 0x51, context=context)
        enwind = self.decode(0x04, 0x52, context=context)
        self.assertEqual([effect.name for effect in speed.effects], ["report_error", "record_error"])
        self.assertEqual([effect.name for effect in enwind.effects], ["set_global", "record_error", "report_error"])
        self.assertEqual(enwind.effects[0], Effect("set_global", ("filament_useup", 1)))
        self.assertTrue(self.decode(0x04, 0x51, context=DecodeContext(last_cmd="")).effects)

    def test_missing_reply_echo_zero_and_exhausted_crc_are_distinct(self):
        for command, expected in ((0x0A, None), (0x55, None), (0x04, False)):
            request = Request(2, command, b"", 2)
            result = decode_reply(self.state, None, request)
            self.assertIs(result.value, expected)
            self.assertEqual(result.effects, () if expected is None else (Effect("timeout_process", (2,)),))
        echo = self.decode(0x55, 0x02, b"\x00", b"\x00")
        self.assertEqual(echo.value, 0)
        self.assertIs(type(echo.value), int)
        self.assertEqual(echo.effects, ())
        self.assertIs(self.decode(0x04, 0x02).value, False)
        self.assertIs(self.decode(0x04, 0x04).value, False)

    def test_state_layouts_by_exact_version_and_slot_events_do_not_refresh_telemetry(self):
        for version in ("113", "122", "142", "150", "152", "153"):
            size = 4 if version in ("113", "122") else 6
            data = bytes((37, 15, 0xAA, 2, 5, 0x81))[:size]
            result = self.decode(0x0A, data=data, version=version)
            self.assertIs(result.value, True)
            self.assertEqual(self.state.Tn_data["T1"]["temperature"], "37")
            self.assertEqual(self.state.Tn_data["T1"]["dry_and_humidity"], "15")
            self.assertEqual(self.state.Tn_data["T1"]["mode"], "2")
            self.assertEqual(result.effects, () if size == 4 else (Effect("state_extension", (1, 5, 0x81)),))
        before = deepcopy(self.state.Tn_data)
        event = self.decode(0x0A, 0x30, b"\x01\x02\x03\x00", version="153")
        self.assertEqual(event.effects, (Effect("state_events", (1, b"\x01\x02\x03\x00")),))
        self.assertEqual(self.state.Tn_data, before)
        fault = self.decode(0x0A, 0x50, b"\x10\x20\x03\x04\x05\x06", version="153")
        self.assertEqual(fault.effects[0], Effect("state_extension", (1, 5, 6)))
        self.assertEqual(self.state.Tn_data, before)

    def test_buffer_sensor_remaining_and_wheel_use_command_specific_types(self):
        self.decode(0x05, data=b"\x02", address=3)
        self.assertEqual(self.state.Tn_inner_data["buffer"], 2)
        for bank in (0, 1):
            self.decode(0x08, data=b"\x0A", request_data=bytes((bank,)), address=3)
        self.assertEqual(self.state.Tn_inner_data["T3"]["filament_sensor"], {"material": 10, "connections": 10})
        self.assertEqual(self.state.Tn_inner_data["T1"]["filament_sensor"], {"material": 0, "connections": 0})
        self.decode(0x03, data=bytes((10, 20, 30, 40)), request_data=b"\x05", address=3)
        self.assertEqual(self.state.Tn_data["T3"]["remain_len"], ["10", "-1", "30", "-1"])
        self.decode(0x0E, data=struct.pack(">f", 12.5), request_data=b"\x01", address=3)
        self.assertEqual(self.state.Tn_data["T3"]["measuring_wheel"], 12.5)
        self.decode(0x0E, data=b"", request_data=b"\x00", address=3)
        self.assertIs(type(self.state.Tn_data["T3"]["measuring_wheel"]), int)
        self.assertEqual(self.state.Tn_data["T3"]["measuring_wheel"], 0)
        self.decode(0x0E, data=bytes.fromhex("7fc00000"), request_data=b"\x01", address=3)
        self.assertTrue(math.isnan(self.state.Tn_data["T3"]["measuring_wheel"]))

    def test_identity_is_validated_before_its_effect_and_keeps_raw_serial_bytes(self):
        request = build_request(1, 0x14)
        data = b"113" + b"\x00\x80\xff" + b"a" * 16
        frame = reply(request, data=data)
        result = decode_reply(self.state, frame, request)
        self.assertEqual(result.effects, (Effect("identity", (1, data, frame.raw)),))
        self.assertEqual(self.state.Tn_data["T1"]["version"], "-1")
        for data in (b"113short", b"1x3" + b"a" * 19):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_reply(self.state, reply(request, data=data), request)

    def test_rfid_complete_records_cached_unknowns_and_empty_identity(self):
        tag = "01234567890" + "G12345" + "0ABCDEF" + "z" * 16
        self.assertEqual(len(tag), 40)
        self.state.modify_Tn_data(1, "existing", "vender", "B")
        self.state.modify_Tn_data(1, "0FFFFFF", "color_value", "B")
        self.state.tn_save_data["T1"]["color_value"][2] = "0000000"
        self.state.tn_save_data["T1"]["material_type"][2] = "G54321"
        result = self.decode(0x02, data=("A:" + tag + ";B:none;C:unknown;").encode(), request_data=b"\x0F")
        self.assertIs(result.value, True)
        self.assertEqual(result.effects[-1], Effect("schedule_groups"))
        apply_slots(self.state, result.effects)
        live = self.state.Tn_data["T1"]
        self.assertEqual(live["vender"][:3], [tag, "none", "unknown"])
        self.assertEqual(live["color_value"][:3], ["0ABCDEF", "none", "0000000"])
        self.assertEqual(live["material_type"][:3], ["G12345", "none", "G54321"])
        self.assertEqual(self.state.tn_save_data["T1"]["vender"][1], "existing")
        self.assertEqual(self.state.tn_save_data["T1"]["color_value"][1], "0FFFFFF")

    def test_busy_rfid_is_an_error_and_not_an_empty_slot(self):
        self.state.tn_save_data["T1"]["vender"][0] = "unknown"
        self.state.tn_save_data["T1"]["color_value"][0] = "0FFFFFF"
        self.state.tn_save_data["T1"]["material_type"][0] = "G12345"
        result = self.decode(0x02, data=b"A:busy;", request_data=b"\x01")
        self.assertIs(result.value, True)
        self.assertEqual(result.effects[0].name, "report_error")
        self.assertEqual(result.effects[0].args[0], "key_rfid_err")
        self.assertEqual(result.effects[0].args[1]["rfid"], "busy")
        apply_slots(self.state, result.effects)
        self.assertEqual(self.state.Tn_data["T1"]["vender"][0], "unknown")
        self.assertEqual(self.state.Tn_data["T1"]["color_value"][0], "0FFFFFF")

    def test_malformed_payloads_never_partially_update_state_or_cache(self):
        for command, data, request_data, version in (
            (0x0A, b"\x01\x02\x03", b"", "113"),
            (0x0A, b"\x01\x02\x03\x04", b"", "153"),
            (0x05, b"", b"", "113"), (0x08, b"\x01\x02", b"\x00", "113"),
            (0x03, b"\x01\x02", b"\x0F", "113"),
            (0x0E, b"\x00\x01\x02", b"\x01", "113"),
            (0x02, b"A:unknown;B:missing-terminator", b"\x0F", "113"),
        ):
            before = deepcopy((self.state.Tn_data, self.state.Tn_inner_data, self.state.tn_save_data))
            with self.subTest(command=command, version=version), self.assertRaises(ValueError):
                self.decode(command, data=data, request_data=request_data, version=version)
            self.assertEqual((self.state.Tn_data, self.state.Tn_inner_data, self.state.tn_save_data), before)
        request = build_request(1, 0x0A)
        with self.assertRaises(ValueError):
            decode_reply(self.state, reply(request, data=b"\x01\x02\x03\x04"), request)
        with self.assertRaises(ValueError):
            decode_reply(self.state, reply(request, 0x30, b"\x00" * 6), request)
        other = build_request(2, 0x0A)
        with self.assertRaises(ValueError):
            decode_reply(self.state, reply(other, data=b"\x00" * 4), request, profile=profile())

    def test_positive_mode_ack_does_not_fabricate_an_observed_mode_or_connection(self):
        before = deepcopy(self.state.Tn_data)
        self.assertIs(self.decode(0x04, request_data=b"\x01\x00").value, True)
        self.assertIs(self.decode(0x01).value, True)
        self.assertEqual(self.state.Tn_data, before)


if __name__ == "__main__":
    unittest.main()
