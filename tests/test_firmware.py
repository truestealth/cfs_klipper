"""Synthetic per-device identities; no native modules or printer are loaded."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cfs.firmware import BASELINE_VERSION, DeviceVersions, SLOT_EVENT_DATA_LENGTH, parse_identity


def payload(version="113", serial=b"SYNTHETIC-SERIAL-01"):
    return version.encode("ascii") + serial


class FirmwareTests(unittest.TestCase):
    def test_shared_identity_parser_validates_before_any_device_update(self):
        serial = b"\0\xff\x80" + b"SYNTHETIC-SERIAL"
        raw = b"validated frame"
        parsed = parse_identity(payload("153", serial), raw)
        self.assertEqual((parsed.version, parsed.sn), ("1.5.3", serial.decode("latin1")))
        self.assertEqual(parsed.raw_reply, raw)
        self.assertEqual(parsed.raw_data, payload("153", serial))
        for data in (payload()[:-1], payload()+b"x", b"\xff13"+serial, b"1x3"+serial, None):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_identity(data, raw)

    def test_all_six_exact_profiles_and_layout_boundaries(self):
        """PC-IDENTITY-VERSIONS and PC-STATE-LAYOUTS: explicit known entries."""
        devices = DeviceVersions({1: "cfs0"})
        self.assertEqual(BASELINE_VERSION, "1.1.3")
        expected = {
            "113": (4, False, False, False),
            "122": (4, False, False, False),
            "142": (6, True, False, False),
            "150": (6, True, True, True),
            "152": (6, True, True, True),
            "153": (6, True, True, True),
        }
        for digits, fields in expected.items():
            with self.subTest(version=digits):
                identity = devices.update(1, payload(digits))
                profile = devices.require_supported(1)
                self.assertEqual(identity.version, ".".join(digits))
                self.assertEqual(identity.version_digits, digits)
                self.assertEqual(profile.version, identity.version)
                self.assertEqual((profile.state_data_length, profile.rfid_lifecycle,
                    profile.rfid_tag_filter, profile.autonomous_buffer_recovery), fields)
                self.assertEqual(SLOT_EVENT_DATA_LENGTH, 4)
        self.assertEqual(devices.get(1).version, "1.5.3")

    def test_four_addresses_keep_independent_identity_layout_and_raw_frames(self):
        devices = DeviceVersions({address: "cfs0" for address in range(1, 5)})
        for address, digits in enumerate(("113", "142", "150", "153"), 1):
            serial = ("SYNTHETIC-SERIAL-%02d" % address).encode("ascii")
            data = payload(digits, serial)
            raw = b"frame:" + bytes([address]) + data
            identity = devices.update(address, data, raw_reply=raw)
            self.assertEqual(identity.raw_reply, raw)
            self.assertEqual(identity.raw_data, data)
            self.assertEqual(identity.sn, serial.decode("ascii"))
            self.assertEqual(devices.last_raw_reply(address), raw)
        self.assertEqual([devices.profile(address).state_data_length for address in range(1, 5)], [4, 6, 6, 6])
        first = devices.get(1)
        devices.invalidate(4)
        self.assertIs(devices.get(1), first)
        self.assertIsNone(devices.get(4))

    def test_identity_does_not_infer_family_or_fall_back_for_unknown_versions(self):
        devices = DeviceVersions()
        self.assertIsNone(devices.get(1))
        self.assertIsNone(devices.profile(1))
        devices.update(1, payload())
        with self.assertRaises(ValueError):
            devices.require_supported(1)
        devices.set_family(1, "cfs6")
        self.assertIsNone(devices.profile(1))
        devices.set_family(1, "cfs0")
        self.assertEqual(devices.require_supported(1).version, "1.1.3")
        for unknown in ("000", "151", "154", "199", "999"):
            with self.subTest(version=unknown):
                self.assertEqual(devices.update(1, payload(unknown)).version_digits, unknown)
                self.assertIsNone(devices.profile(1))
                with self.assertRaises(ValueError):
                    devices.require_supported(1)

    def test_malformed_payload_invalidates_old_identity_and_retains_raw_diagnostics(self):
        devices = DeviceVersions({1: "cfs0", 2: "cfs0"})
        devices.update(2, payload("142"))
        other = devices.get(2)
        for malformed in (b"", payload()[:-1], payload()+b"x", b"1x3"+payload()[3:], b"\xff13"+payload()[3:]):
            with self.subTest(malformed=malformed):
                devices.update(1, payload())
                before = devices.generation(1)
                raw = b"received:" + malformed
                with self.assertRaises(ValueError):
                    devices.update(1, malformed, raw_reply=raw)
                self.assertIsNone(devices.get(1))
                self.assertIsNone(devices.profile(1))
                self.assertEqual(devices.generation(1), before+1)
                self.assertEqual(devices.last_raw_reply(1), raw)
                self.assertIs(devices.get(2), other)

    def test_serial_conversion_preserves_all_nineteen_bytes_and_immutable_copies(self):
        devices = DeviceVersions({1: "cfs0"})
        serial = b"\0\xff\x80" + b"SYNTHETIC-SERIAL"
        self.assertEqual(len(serial), 19)
        mutable = bytearray(payload(serial=serial))
        raw = bytearray(b"frame:" + mutable)
        identity = devices.update(1, mutable, raw_reply=raw)
        expected_data, expected_raw = bytes(mutable), bytes(raw)
        mutable[0] = ord("9")
        raw[0] = 0
        self.assertEqual(identity.sn, serial.decode("latin1"))
        self.assertEqual(identity.raw_data, expected_data)
        self.assertEqual(identity.raw_reply, expected_raw)
        self.assertEqual(len(identity.sn), 19)
        with self.assertRaises(FrozenInstanceError):
            identity.version = "1.5.3"
        with self.assertRaises(FrozenInstanceError):
            devices.profile(1).state_data_length = 6

    def test_reconnect_requires_new_identity_and_replacement_clears_family_confirmation(self):
        devices = DeviceVersions({1: "cfs0"})
        original = devices.update(1, payload())
        generation = devices.generation(1)
        devices.invalidate(1)
        self.assertIsNone(devices.profile(1))
        self.assertEqual(devices.generation(1), generation+1)
        devices.update(1, original.raw_data)
        self.assertEqual(devices.require_supported(1).version, "1.1.3")
        devices.invalidate(1)
        generation = devices.generation(1)
        replacement = payload("153", b"SYNTHETIC-SERIAL-02")
        devices.update(1, replacement)
        self.assertEqual(devices.generation(1), generation+1)
        self.assertIsNone(devices.profile(1))
        devices.set_family(1, "cfs0")
        self.assertEqual(devices.require_supported(1).version, "1.5.3")

    def test_generation_tracks_only_the_affected_identity_or_family_context(self):
        devices = DeviceVersions({1: "cfs0", 2: "cfs0"})
        self.assertEqual(devices.generation(1), 0)
        devices.update(1, payload())
        self.assertEqual(devices.generation(1), 0)
        devices.update(1, payload())
        self.assertEqual(devices.generation(1), 0)
        devices.update(1, payload("142"))
        self.assertEqual(devices.generation(1), 1)
        self.assertEqual(devices.generation(2), 0)
        devices.set_family(1, "cfs0")
        self.assertEqual(devices.generation(1), 1)
        devices.set_family(1, None)
        self.assertEqual(devices.generation(1), 2)
        self.assertIsNone(devices.profile(1))
        devices.invalidate(1)
        devices.invalidate(1)
        self.assertEqual(devices.generation(1), 4)
        self.assertEqual(devices.generation(2), 0)

    def test_invalidation_can_retain_a_failed_raw_frame_without_updating_identity(self):
        devices = DeviceVersions({1: "cfs0", 2: "cfs0"})
        first = devices.update(1, payload())
        other = devices.update(2, payload("142"))
        generation = devices.generation(1)
        raw = bytearray(b"synthetic failed identity frame")
        expected = bytes(raw)
        devices.invalidate(1, raw_reply=raw)
        raw[0] = 0
        self.assertIsNone(devices.get(1))
        self.assertIsNone(devices.profile(1))
        self.assertEqual(devices.last_raw_reply(1), expected)
        self.assertEqual(devices.generation(1), generation+1)
        self.assertIs(devices.get(2), other)
        self.assertEqual(devices.generation(2), 0)
        devices.invalidate(1)
        self.assertEqual(devices.last_raw_reply(1), expected)
        devices.update(1, first.raw_data)
        identity, generation = devices.get(1), devices.generation(1)
        for invalid in ("text", 22, False, [1, 2]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                devices.invalidate(1, raw_reply=invalid)
            self.assertIs(devices.get(1), identity)
            self.assertEqual(devices.generation(1), generation)
            self.assertEqual(devices.last_raw_reply(1), first.raw_reply)

    def test_invalid_address_and_nonbuffer_input_cannot_select_a_profile(self):
        devices = DeviceVersions({1: "cfs0"})
        for address in (0, 5, -1, True, "1", None):
            with self.subTest(address=address), self.assertRaises(ValueError):
                devices.update(address, payload())
        devices.update(1, payload())
        for invalid in (None, 22, "113" + "x"*19, list(payload())):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                devices.update(1, invalid)
            self.assertIsNone(devices.profile(1))
        with self.assertRaises(ValueError):
            devices.update(1, None, raw_reply=b"synthetic malformed identity frame")
        self.assertEqual(devices.last_raw_reply(1), b"synthetic malformed identity frame")
        with self.assertRaises(ValueError):
            devices.set_family(1, 113)


if __name__ == "__main__":
    unittest.main()
