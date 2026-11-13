"""Verify borrowed native calls with synthetic frames, never a printer or .so."""

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cfs_box.native import (
    COMMANDS, RESERVED_COMMANDS, FrameError, NativeSerial, UnsupportedRequest,
    build_request, crc8, diagnostic_request, slot_mask, validate_reply,
)


def reply(address=1, command=0x14, status=0, data=b""):
    content = bytes((len(data) + 3, status, command)) + data
    return bytes((0xF7, address)) + content + bytes((crc8(content),))


class Native:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls = []
        self.handlers = {}
        self.admitted = set()
        self.is_connected = False

    def cmd_send_data_with_response(self, *arguments):
        self.calls.append(arguments)
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def register_response(self, callback, address_tag, command_tag):
        key = address_tag, command_tag
        if callback is None:
            self.handlers.pop(key, None)
        else:
            self.handlers[key] = callback

    def deliver(self, raw):
        tags = ("#addr_%d" % raw[1], "#cmd_%d" % raw[4])
        if "".join(tags) in self.admitted and tags in self.handlers:
            self.handlers[tags]({"#msg": raw})

    def raw_send(self, *arguments):
        raise AssertionError("the adapter must not own the raw queue")

    def _disconnect(self):
        raise AssertionError("the adapter must not close the borrowed connection")


class NativeTests(unittest.TestCase):
    def test_all_host_identifiers_and_sixteen_typed_bodies(self):
        matrix = json.loads((ROOT / "docs/contract-matrix.json").read_text(encoding="utf-8"))
        expected = {row["name"]: row["code"] for row in matrix["items"] if row["kind"] == "wire"}
        self.assertEqual(COMMANDS, expected)
        self.assertEqual(RESERVED_COMMANDS, {0x06, 0x09, 0x15})
        fixtures = {
            "CREATE_CONNECT": (b"", 2), "GET_RFID": (b"\x0f", 2),
            "GET_REMAIN_LEN": (b"\x03", 2), "SET_BOX_MODE": (b"\x02\x00", 2),
            "GET_BUFFER_STATE": (b"", 2), "CTRL_CONNECTION_MOTOR_ACTION": (b"\x02", 2),
            "GET_FILAMENT_SENSOR_STATE": (b"\x01", 2), "GET_BOX_STATE": (b"", 3600),
            "SET_PRE_LOADING": (b"\x03\x02", 600), "MEASURING_WHEEL": (b"\x01", 2),
            "TIGHTEN_UP_ENABLE": (b"\x00", 150), "EXTRUDE_PROCESS": (b"\x08\x07\x12", 15),
            "RETRUDE_PROCESS": (b"\x04\x01", 150), "EXTRUDE_PROCESS_MODEL2": (b"\x01\x00", 150),
            "GET_VERSION_SN": (b"", 2), "COMMUNICATION_TEST": (b"\xff", 0.1),
        }
        self.assertEqual(len(fixtures), 16)
        for address in range(1, 5):
            for name, (data, timeout) in fixtures.items():
                with self.subTest(address=address, command=name):
                    request = build_request(address, name, data)
                    self.assertIs(type(request.body), bytes)
                    self.assertEqual(request.body, bytes((address, len(data) + 3, 0xFF, expected[name])) + data)
                    self.assertEqual(request.timeout, timeout)
                    self.assertIs(type(request.timeout), type(timeout))
                    self.assertFalse(request.diagnostic)
                    raw = reply(address, expected[name], data=b"checked")
                    native = Native([raw])
                    adapter = NativeSerial(native)
                    if name == "EXTRUDE_PROCESS_MODEL2":
                        with self.assertRaises(UnsupportedRequest):
                            adapter.exchange(request)
                        self.assertEqual(native.calls, [])
                    else:
                        self.assertEqual(adapter.exchange(request).raw, raw)
                        self.assertEqual(native.calls, [(
                            bytes((address, len(data) + 3, 0xFF, expected[name])) + data,
                            timeout,
                        )])

    def test_preload_masks_timeouts_and_letter_selectors(self):
        for mask in range(16):
            for action in range(4):
                request = build_request(1, "SET_PRE_LOADING", bytes((mask, action)))
                self.assertEqual(request.timeout, 2 if action < 2 else 300 * bin(mask).count("1"))
        self.assertEqual([slot_mask(letter) for letter in "ABCD"], [1, 2, 4, 8])
        self.assertEqual(slot_mask("0", allow_zero=True), 0)
        for invalid in ("0", "AB", "a", "15", 1, None):
            with self.subTest(slot=invalid), self.assertRaises(ValueError):
                slot_mask(invalid)
        for stage in range(8):
            request = build_request(1, "EXTRUDE_PROCESS", bytes((1, stage, 0)))
            self.assertEqual(request.data, bytes((1, stage, 0)))
        legacy = build_request(1, "EXTRUDE_PROCESS", b"\x01\x01", encoding=0)
        self.assertEqual(legacy.body, b"\x01\x05\xff\x10\x01\x01")

    def test_typed_builders_reject_unknown_reserved_or_invalid_payloads(self):
        invalid = [
            (0, 0x14, b""), (5, 0x14, b""), (True, 0x14, b""),
            (1, "UNKNOWN", b""), (1, 0x12, b""), (1, 0x14, ""),
            (1, 0x14, b"\x00"), (1, 0x02, b"\x10"),
            (1, 0x04, b"\x03\x00"), (1, 0x04, b"\x01\x02"),
            (1, 0x07, b"\x03"), (1, 0x08, b"\x02"),
            (1, 0x0D, b"\x01\x04"), (1, 0x0E, b"\x02"),
            (1, 0x0F, b"\x02"), (1, 0x10, b"\x03\x00\x00"),
            (1, 0x11, b"\x01\x02"), (1, 0x55, b""),
        ]
        for address, command, data in invalid:
            with self.subTest(command=command, data=data), self.assertRaises(ValueError):
                build_request(address, command, data)
        for command in RESERVED_COMMANDS:
            with self.subTest(command=command), self.assertRaises(UnsupportedRequest):
                build_request(1, command)
        for timeout in (True, -1, float("inf"), float("nan"), "2"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                build_request(1, "GET_VERSION_SN", timeout=timeout)

    def test_raw_diagnostic_is_a_body_not_a_typed_command_or_full_frame(self):
        request = diagnostic_request(1, 0, 10, bytes((1, 5)), 5)
        self.assertEqual(request.body, b"\x01\x05\x00\x0a\x01\x05")
        self.assertTrue(request.diagnostic)
        self.assertEqual(diagnostic_request(0, 0, 0).address, 0)
        self.assertEqual(len(diagnostic_request(255, 255, 32, b"x" * 252).body), 256)
        for arguments in ((1, 0, 33), (1, 256, 10), (1, 0, 10, b"x" * 253),
                          (1, 0, 10, b"", 0), (1, 0, 10, b"", 121),
                          (1, 0, 10, b"", 5.0)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                diagnostic_request(*arguments)

    def test_frame_geometry_crc_and_matching_context(self):
        raw = bytes.fromhex("F7 01 03 00 A2 DA")
        self.assertEqual(crc8(bytes.fromhex("03 00 A2")), 0xDA)
        frame = validate_reply(raw, 1, 0xA2)
        self.assertEqual((frame.address, frame.command, frame.status, frame.data, frame.raw),
                         (1, 0xA2, 0, b"", raw))
        mutable = bytearray(reply(data=b"value"))
        frame = validate_reply(mutable, 1, 0x14)
        mutable[5] = 0
        self.assertEqual(frame.data, b"value")
        with self.assertRaises(AttributeError):
            frame.status = 1

    def test_malformed_or_unrelated_replies_are_rejected_without_retry(self):
        good = reply()
        malformed = [None, [], b"", good[1:], b"\x00" + good[1:],
                     good[:2] + b"\x02" + good[3:], good + b"\x00",
                     good[:-1] + bytes((good[-1] ^ 1,)),
                     reply(address=2), reply(command=0x0A)]
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(FrameError):
                validate_reply(raw, 1, 0x14)
        native = Native([reply(address=2)])
        with self.assertRaises(FrameError):
            NativeSerial(native).exchange(build_request(1, "GET_VERSION_SN"))
        self.assertEqual(len(native.calls), 1)

    def test_sender_retry_count_and_stop_conditions_are_api_exchanges(self):
        for command, count in ((0x14, 7), (0x0A, 3)):
            for status in (0x02, 0x04):
                native = Native([reply(command=command, status=status)] * count)
                frame = NativeSerial(native).exchange(build_request(1, command))
                self.assertEqual(frame.status, status)
                self.assertEqual(len(native.calls), count)
                self.assertTrue(all(len(call) == 2 for call in native.calls))
        native = Native([reply(status=2), reply(status=4), reply(status=0)])
        self.assertEqual(NativeSerial(native).exchange(build_request(1, 0x14)).status, 0)
        self.assertEqual(len(native.calls), 3)
        native = Native([reply(status=2), None])
        self.assertIsNone(NativeSerial(native).exchange(build_request(1, 0x14)))
        self.assertEqual(len(native.calls), 2)
        native = Native([reply(status=0x51)])
        self.assertEqual(NativeSerial(native).exchange(build_request(1, 0x14)).status, 0x51)
        self.assertEqual(len(native.calls), 1)
        native = Native([reply(status=2)] * 7)
        NativeSerial(native).exchange(build_request(1, 0x14), retries_en=False)
        self.assertEqual(len(native.calls), 7)
        self.assertTrue(all(call[2] is False for call in native.calls))

    def test_default_omits_retry_argument_and_explicit_false_is_not_a_wire_count(self):
        request = build_request(1, "GET_VERSION_SN")
        for retry in (None, False, True):
            native = Native([reply()])
            NativeSerial(native).exchange(request, retries_en=retry)
            expected = (request.body, 2) if retry is None else (request.body, 2, retry)
            self.assertEqual(native.calls, [expected])
        error = OSError("synthetic native failure")
        native = Native([error])
        with self.assertRaises(OSError) as caught:
            NativeSerial(native).exchange(request)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(native.calls), 1)

    def test_generation_guard_runs_before_every_native_attempt(self):
        current = [True]

        class ChangingNative(Native):
            def cmd_send_data_with_response(native, *arguments):
                result = super().cmd_send_data_with_response(*arguments)
                current[0] = False
                return result

        error = ValueError("synthetic changed identity")

        def check_context():
            if not current[0]:
                raise error

        native = ChangingNative([reply(status=2), reply()])
        request = build_request(1, "GET_VERSION_SN")
        with self.assertRaises(ValueError) as caught:
            NativeSerial(native).exchange(request, check_context=check_context)
        self.assertIs(caught.exception, error)
        self.assertEqual(native.calls, [(request.body, 2)])
        self.assertEqual(native.replies, [reply()])

        native = Native([reply()])
        with self.assertRaises(ValueError) as caught:
            NativeSerial(native).exchange(request, check_context=check_context)
        self.assertIs(caught.exception, error)
        self.assertEqual(native.calls, [])

    def test_communication_test_bypasses_status_retry_and_keeps_falsey_none_path(self):
        request = build_request(1, "COMMUNICATION_TEST", b"\xff", timeout=0)
        native = Native([reply(command=0x55, status=2, data=b"\x00")])
        frame = NativeSerial(native).exchange(request)
        self.assertEqual(frame.data, b"\x00")
        self.assertEqual(len(native.calls), 1)
        for raw in (None, False, b""):
            native = Native([raw])
            self.assertIsNone(NativeSerial(native).exchange(request))
            self.assertEqual(native.calls[0][1], 0)

    def test_unresolved_routes_are_representable_but_cannot_execute(self):
        requests = [build_request(1, 0x13, b"\x01\x00"),
                    diagnostic_request(1, 0xFF, 0x13, b"\x01"),
                    build_request(1, 0x10, b"\x01\x00", encoding=0)]
        requests += [build_request(1, 0x10, bytes((1, stage, 0))) for stage in (8, 9)]
        native = Native()
        for request in requests:
            with self.subTest(request=request), self.assertRaises(UnsupportedRequest):
                NativeSerial(native).exchange(request)
        self.assertEqual(native.calls, [])

    def test_adapter_has_no_busy_guard_blocking_a_nested_callback_exchange(self):
        class ReentrantNative(Native):
            def cmd_send_data_with_response(native, *arguments):
                native.calls.append(arguments)
                if arguments[0][5] == 4:
                    nested = adapter.exchange(build_request(1, 0x10, b"\x01\x06\x00"))
                    self.assertEqual(nested.status, 0)
                return reply(command=0x10)

        native = ReentrantNative()
        adapter = NativeSerial(native)
        self.assertEqual(adapter.exchange(build_request(1, 0x10, b"\x01\x04\x00")).status, 0)
        self.assertEqual([call[0][5] for call in native.calls], [4, 6])

    def test_borrowed_lookup_connection_and_callback_admission_boundaries(self):
        native = Native()

        class Printer:
            def lookup_object(printer, name):
                self.assertEqual(name, "serial_485 custom_bus")
                return native

        adapter = NativeSerial.from_printer(Printer(), "custom_bus")
        self.assertFalse(adapter.is_connected)
        native.is_connected = True
        self.assertTrue(adapter.is_connected)
        seen = []
        adapter.register_response(seen.append, 1, 0x0A)
        raw = reply(command=0x0A)
        native.deliver(raw)
        self.assertEqual(seen, [])
        self.assertEqual(native.admitted, set())
        native.admitted.add("#addr_1#cmd_10")
        native.deliver(raw)
        self.assertEqual(seen, [{"#msg": raw}])
        adapter.register_response(None, 1, 0x0A)
        self.assertEqual(native.handlers, {})
        adapter.register_response(seen.append, 1, 0xA0)
        adapter.register_response(seen.append, 1, 0xA1)
        self.assertIn(("", "#cmd_160"), native.handlers)
        self.assertIn(("#addr_1", "#cmd_161"), native.handlers)


if __name__ == "__main__":
    unittest.main()
