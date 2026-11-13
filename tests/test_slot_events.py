"""Slot identity events, persistence order and owned metadata callbacks."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.native import build_request
from cfs.lifecycle import LifecycleStopped
from test_native import reply
import test_lifecycle


class SlotEventTests(unittest.TestCase):
    def make_box(self):
        self.fixture = test_lifecycle.LifecycleTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.make_box()
        self.fixture.ready()
        for name in ("box", "native", "reactor", "printer", "lifecycle"):
            setattr(self, name, getattr(self.fixture, name))
        self.lifecycle.get_connect_state()
        for timer in tuple(self.lifecycle._callbacks):
            self.reactor.fire(timer)
        self.state = self.box.box_state
        self.native.calls.clear()

    def seed(self, vendor="brand", color="#123456", kind="PLA", remain="500"):
        for field, value in (("vender", vendor), ("color_value", color),
                             ("material_type", kind), ("remain_len", remain)):
            self.state.modify_Tn_data(1, value, field, "A")
        for field in ("vender", "color_value", "material_type", "remain_len"):
            self.state.Tn_data["T1"][field][0] = "old-" + field

    def test_printing_restoration_cases_for_insert_and_completed(self):
        for preload in (False, True):
            for printing in ("printing", "paused"):
                for event in (1, 3):
                    with self.subTest(preload=preload, state=printing, event=event):
                        self.make_box()
                        self.seed()
                        self.box.preloading_en = preload
                        self.box.print_stats.state = printing
                        self.box.events.process(1, bytes((event, 0, 0, 0)))
                        self.assertEqual([self.state.Tn_data["T1"][field][0] for field in
                                          ("vender", "remain_len", "color_value", "material_type")],
                                         ["brand", "500", "#123456", "PLA"])
                        self.assertEqual(self.native.calls, [])

    def test_missing_saved_vendor_sets_three_unknowns_without_remaining(self):
        self.make_box()
        self.seed(vendor="-1")
        self.box.print_stats.state = "printing"
        self.box.events.process(1, b"\x01\x00\x00\x00")
        live = self.state.Tn_data["T1"]
        self.assertEqual([live[field][0] for field in ("vender", "color_value", "material_type")],
                         ["unknown"] * 3)
        self.assertEqual(live["remain_len"][0], "old-remain_len")

    def test_unknown_saved_vendor_invalid_color_preserves_live_color_and_type(self):
        for color in ("-1", "unknown"):
            with self.subTest(color=color):
                self.make_box()
                self.seed(vendor="unknown", color=color)
                self.box.print_stats.state = "paused"
                self.box.events.process(1, b"\x03\x00\x00\x00")
                live = self.state.Tn_data["T1"]
                self.assertEqual(live["vender"][0], "unknown")
                self.assertEqual(live["color_value"][0], "old-color_value")
                self.assertEqual(live["material_type"][0], "old-material_type")

    def test_unknown_saved_vendor_valid_color_restores_without_remaining(self):
        self.make_box()
        self.seed(vendor="unknown")
        self.box.print_stats.state = "printing"
        self.box.events.process(1, b"\x01\x00\x00\x00")
        self.assertEqual(self.state.Tn_data["T1"]["color_value"][0], "#123456")
        self.assertEqual(self.state.Tn_data["T1"]["material_type"][0], "PLA")
        self.assertEqual(self.state.Tn_data["T1"]["remain_len"][0], "old-remain_len")

    def test_extract_preserves_cache_and_other_fields(self):
        self.make_box()
        self.seed()
        saved = deepcopy(self.state.tn_save_data)
        self.box.events.process(1, b"\x02\x00\x00\x00")
        self.assertEqual(self.state.Tn_data["T1"]["vender"][0], "none")
        self.assertEqual(self.state.Tn_data["T1"]["color_value"][0], "old-color_value")
        self.assertEqual(self.state.tn_save_data, saved)

    def test_idle_insert_with_preload_changes_only_vendor(self):
        self.make_box()
        self.seed()
        self.box.preloading_en = True
        self.box.events.process(1, b"\x01\x00\x00\x00")
        self.assertEqual(self.state.tn_save_data["T1"]["vender"][0], "unknown")
        self.assertEqual(self.state.Tn_data["T1"]["color_value"][0], "old-color_value")

    def test_idle_without_preload_reads_cache_after_vendor_setter(self):
        for color, expected in (("#123456", "#123456"), ("-1", "unknown"),
                                ("unknown", "unknown")):
            with self.subTest(color=color):
                self.make_box()
                self.seed(vendor="brand", color=color)
                self.box.preloading_en = False
                self.box.events.process(1, b"\x03\x00\x00\x00")
                self.assertEqual(self.state.Tn_data["T1"]["color_value"][0], expected)
                self.assertEqual(self.state.tn_save_data["T1"]["vender"][0], "unknown")
                self.assertEqual(self.native.calls, [])

    def test_completed_preload_schedules_metadata_and_discards_normal_false(self):
        self.make_box()
        self.box.preloading_en = True
        self.box.events.process(1, b"\x00\x00\x03\x00")
        timer = next(iter(self.lifecycle._callbacks))
        self.assertEqual(self.native.calls, [])
        seen = []
        self.box.send_native = lambda request: seen.append((request.command, request.data)) or False
        self.reactor.fire(timer)
        self.assertEqual(seen, [(2, b"\x04"), (3, b"\x04")])
        self.assertNotIn(timer, self.lifecycle._callbacks)

    def test_metadata_exception_prevents_remaining_query(self):
        self.make_box()
        self.box.preloading_en = True
        self.box.events.process(1, b"\x03\x00\x00\x00")
        timer = next(iter(self.lifecycle._callbacks))
        def fail(request):
            raise OSError("synthetic RFID failure")
        self.box.send_native = fail
        with self.assertRaises(OSError):
            self.reactor.fire(timer)
        self.assertNotIn(timer, self.lifecycle._callbacks)

    def test_queued_refresh_cannot_follow_replacement_or_shutdown(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                self.make_box()
                self.box.events.schedule_rfid(1, 1)
                timer = next(iter(self.lifecycle._callbacks))
                if shutdown:
                    self.lifecycle.shutdown()
                else:
                    self.box.invalidate_device(1)
                self.reactor.fire(timer)
                self.assertEqual(self.native.calls, [])

    def test_replacement_during_rfid_stops_second_query(self):
        self.make_box()
        self.box.events.schedule_rfid(1, 1)
        timer = next(iter(self.lifecycle._callbacks))
        seen = []
        def replace(request):
            seen.append(request.command)
            self.box.invalidate_device(1)
        self.box.send_native = replace
        with self.assertRaises(LifecycleStopped):
            self.reactor.fire(timer)
        self.assertEqual(seen, [2])

    def test_wire_event_is_separate_from_mode_and_rebuilds_groups(self):
        self.make_box()
        self.state.Tn_data["same_material"] = ["stale"]
        mode = self.state.get_box_mode(1)
        self.native.responses[(1, 0x0A)] = [reply(1, 0x0A, status=0x30, data=b"\x00\x7f\x00\x00")]
        self.assertTrue(self.box.send_native(build_request(1, "GET_BOX_STATE")))
        self.assertEqual(self.state.get_box_mode(1), mode)
        self.assertEqual(self.state.Tn_data["same_material"], [])
        self.assertEqual(self.box.slot_events[1]["data"], b"\x00\x7f\x00\x00")


if __name__ == "__main__":
    unittest.main()
