"""PC-only lifecycle checks using framed native replies and a cooperative reactor."""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfs.box import Box, MissingProtocolEffect
from cfs.lifecycle import LifecycleStopped
from cfs.native import build_request
from cfs.protocol import STATUS_NAMES
from test_configuration import FakeConfig
from test_native import reply
from test_box_native import NativeCommand
from test_box_data import CommandError


class Timer:
    def __init__(self, callback, when):
        self.callback, self.when = callback, when


class Reactor:
    NEVER = float("inf")

    def __init__(self):
        self.time = 10.0
        self.timers, self.removed, self.pauses = [], [], []
        self.on_pause = None

    def monotonic(self):
        return self.time

    def pause(self, deadline):
        self.pauses.append(deadline - self.time)
        self.time = deadline
        if self.on_pause is not None:
            self.on_pause()
        return deadline

    def register_timer(self, callback, when):
        timer = Timer(callback, when)
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        self.removed.append(timer)
        if timer in self.timers:
            self.timers.remove(timer)

    def fire(self, timer):
        timer.when = timer.callback(self.time)
        return timer.when


class GCode:
    def __init__(self):
        self.commands, self.scripts, self.messages = {}, [], []

    def register_command(self, name, handler, **kwargs):
        old = self.commands.pop(name, None)
        if handler is not None:
            if old is not None:
                raise ValueError("duplicate command: " + name)
            self.commands[name] = handler
        return old

    def run_script_from_command(self, script):
        self.scripts.append(script)

    def respond_raw(self, message):
        self.messages.append(message)

    def respond_info(self, message):
        self.messages.append(message)


class Native:
    serial_port = "synthetic-port"
    is_connected = True

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.mode = 1
        self.serial = b"A" * 19
        self.version = b"113"

    def cmd_send_data_with_response(self, body, timeout, *args):
        self.calls.append((body, timeout, args))
        address, command = body[0], body[3]
        queued = self.responses.get((address, command), [])
        if queued:
            response = queued.pop(0)
            return response() if callable(response) else response
        data = {0x14: self.version + self.serial,
                0x0A: bytes((20, 30, 0, self.mode)),
                0x08: b"\x01"}.get(command, b"")
        return reply(address, command, data=data)

    def commands(self):
        return [(body[0], body[3], body[4:]) for body, _, _ in self.calls]


class Printer:
    def __init__(self, native, reactor, head_name):
        self.reactor, self.events, self.sent, self.lookups = reactor, {}, [], []
        self.stopped = False
        self.gcode = GCode()
        self.gcode.printer = self
        self.endpoints = {}
        self.records = []
        self.objects = {
            "gcode": self.gcode,
            "webhooks": SimpleNamespace(register_endpoint=self.endpoints.__setitem__),
            "toolhead": object(),
            "gcode_move": SimpleNamespace(recordPrintFileName=lambda *args, **kwargs: self.records.append((args, kwargs))),
            "print_stats": SimpleNamespace(state="standby"),
            "virtual_sdcard": SimpleNamespace(work_handler_dispatch="sd", print_file_name_path="synthetic.json", current_file=SimpleNamespace(name="synthetic.gcode")),
            "serial_485 custom": native,
        }
        if head_name is not None:
            self.objects["filament_switch_sensor " + head_name] = SimpleNamespace(
                runout_helper=SimpleNamespace(filament_present=False))

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, *default):
        self.lookups.append(name)
        if name in self.objects:
            return self.objects[name]
        if default:
            return default[0]
        raise KeyError(name)

    def register_event_handler(self, name, callback):
        self.events.setdefault(name, []).append(callback)

    def send_event(self, name):
        self.sent.append(name)
        for callback in tuple(self.events.get(name, ())):
            callback()

    def is_shutdown(self):
        return self.stopped


class LifecycleTests(unittest.TestCase):
    def make_box(self, head="head", online=(1, 0, 0, 0), **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = {"bus": "custom", "version": 1}
        if head is not None:
            config["filament_sensor"] = head
        self.box = Box(FakeConfig(config), directory.name)
        self.native, self.reactor = Native(), Reactor()
        self.printer = Printer(self.native, self.reactor, head)
        self.table = [SimpleNamespace(online=value) for value in online]
        self.port_present = True
        self.lifecycle = self.box.register_lifecycle(
            self.printer, families={address: "cfs0" for address in range(1, 5)},
            address_table=self.table, port_exists=lambda path: self.port_present,
            current_dispatch=lambda: "other", **options)
        self.box.preloading_poweron = False

    def ready(self):
        self.printer.send_event("klippy:ready")
        return self.lifecycle._timer

    def test_registration_aliases_ready_order_and_borrowed_transport(self):
        self.make_box()
        self.assertEqual(len(self.printer.gcode.commands), 93)
        original = self.printer.gcode.register_command("BOX_GET_BOX_STATE", None)
        self.printer.gcode.register_command("BOX_GET_BOX_STATE_OLD", original)
        wrapper = lambda gcmd: None
        self.printer.gcode.register_command("BOX_GET_BOX_STATE", wrapper)
        seen = []
        self.printer.events.setdefault("box:ready", []).append(
            lambda: seen.append((self.box.native.native, list(self.reactor.timers))))
        timer = self.ready()
        self.ready()
        self.assertEqual(seen, [(self.native, [])])
        self.assertIs(self.lifecycle._timer, timer)
        self.assertIs(self.printer.gcode.commands["BOX_GET_BOX_STATE"], wrapper)
        self.assertIs(self.printer.gcode.commands["BOX_GET_BOX_STATE_OLD"], original)
        self.assertIn("serial_485 custom", self.printer.lookups)
        self.assertNotIn("serial_485 serial485", self.printer.lookups)
        self.assertIn("box/get_box_cfg", self.printer.endpoints)
        self.assertEqual(self.native.calls, [])

    def test_identity_first_preserves_preloading_and_initial_metadata(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b""), (1, 0x0A, b""),
                                                 (1, 0x0D, b"\x0f\x00"), (1, 0x08, b"\x00")])
        self.assertEqual(self.box.box_state.get_box_mode(1), "1")
        self.assertEqual(self.box.box_state.get_Tn_data(1, "state"), "connect")
        self.assertTrue(self.lifecycle._callbacks)

    def test_unknown_mode_and_missing_first_reply_retry_once(self):
        for first in (None, reply(1, 0x0A, data=b"\x14\x1e\x00\xff")):
            with self.subTest(first=first):
                self.make_box()
                self.box.box_state.modify_Tn_data(1, "1", "mode")
                self.native.responses[(1, 0x0A)] = [first]
                self.ready()
                self.lifecycle.get_connect_state()
                self.assertEqual([command for _, command, _ in self.native.commands()].count(0x0A), 2)
                self.assertEqual(self.reactor.pauses, [0.5])
                self.assertNotIn(0x04, [command for _, command, _ in self.native.commands()])

    def test_nonpreloading_mode_idle_and_permission_print_guards(self):
        self.make_box()
        self.native.mode = 3
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertIn((1, 0x04, b"\x00\x01"), self.native.commands())
        for state in ("printing", "pause"):
            self.box.print_stats.state = state
            before = len(self.native.calls)
            self.assertIsNone(self.lifecycle.preloading_permission(1))
            self.assertEqual(len(self.native.calls), before)
        self.box.print_stats.state = "paused"
        self.box.preloading_en = True
        self.lifecycle.preloading_permission(1)
        self.assertEqual(self.native.commands()[-1], (1, 0x0D, b"\x0f\x01"))

    def test_exact_online_values_port_and_unconfirmed_profile(self):
        self.make_box(online=(0, 2, 3, 1))
        self.ready()
        self.port_present = False
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.calls, [])
        self.port_present = True
        self.native.version = b"999"
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(4, 0x14, b"")])
        self.assertIsNone(self.box.device_versions.profile(4))

    def test_round_robin_external_permissions_and_timeout_return_boundaries(self):
        self.make_box(online=(1, 1, 1, 1))
        timer = self.ready()
        self.lifecycle.get_connect_state()
        self.native.calls.clear()
        self.box.box_action.heart_process_enable[1] = False
        for _ in range(4):
            self.assertEqual(self.reactor.fire(timer), self.reactor.monotonic() + 1)
        self.assertEqual([(a, c) for a, c, _ in self.native.commands()], [(1, 0x0A), (3, 0x0A), (4, 0x0A)])
        self.native.responses[(1, 0x0A)] = [None] * 4 + [reply(1, 0x0A, status=0x06)]
        for expected in (4, 3, 2, 1, 5):
            self.lifecycle.address = 0
            self.reactor.fire(timer)
            self.assertEqual(self.lifecycle.counters[0], expected)
        self.native.responses[(1, 0x0A)] = [None] * 5
        for _ in range(5):
            self.lifecycle.address = 0
            self.reactor.fire(timer)
        self.assertEqual(self.box.box_state.get_Tn_data(1, "state"), "disconnect")
        self.assertIsNone(self.box.device_versions.get(1))
        self.assertTrue(any('"code":"key831"' in message for message in self.printer.gcode.messages))
        self.assertIsNotNone(self.box.device_versions.get(3))

    def test_event_reply_resets_counter_without_refreshing_ordinary_mode(self):
        self.make_box()
        timer = self.ready()
        self.lifecycle.get_connect_state()
        self.lifecycle.counters[0] = 1
        self.native.responses[(1, 0x0A)] = [reply(1, 0x0A, status=0x30, data=b"\x01\x02\x03\x04")]
        self.reactor.fire(timer)
        self.assertEqual(self.lifecycle.counters[0], 5)
        self.assertEqual(self.box.box_state.get_box_mode(1), "1")
        self.assertEqual(self.box.slot_events[1]["data"], bytes((1, 2, 3, 4)))
        self.assertEqual(self.box.slot_events[1]["generation"], self.box.device_versions.generation(1))
        self.lifecycle.disconnect(1)
        self.assertNotIn(1, self.box.slot_events)

    def test_offline_during_reply_does_not_decode_old_observation(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        def offline_reply():
            self.table[0].online = 0
            return reply(1, 0x0A, data=bytes((20, 30, 0, 2)))
        self.native.responses[(1, 0x0A)] = [offline_reply]
        with self.assertRaises(ValueError):
            self.lifecycle._request(1, "GET_BOX_STATE")
        self.assertEqual(self.box.box_state.get_box_mode(1), "1")
        self.assertEqual(self.box.box_state.get_Tn_data(1, "state"), "disconnect")
        self.assertIsNone(self.box.device_versions.profile(1))

    def test_startup_wait_is_bounded_and_false_skips_it(self):
        self.make_box(online=(0, 0, 0, 0))
        self.box.preloading_poweron = None
        timer = self.ready()
        self.reactor.fire(timer)
        self.assertEqual(self.reactor.pauses, [0.5] * 10)
        self.reactor.fire(timer)
        self.assertEqual(self.reactor.pauses, [0.5] * 10)
        self.make_box(online=(0, 0, 0, 0))
        self.reactor.fire(self.ready())
        self.assertEqual(self.reactor.pauses, [])

    def test_shutdown_clears_own_work_and_old_timer_cannot_restart(self):
        self.make_box()
        timer = self.ready()
        self.lifecycle.get_connect_state()
        queued = tuple(self.lifecycle._callbacks)
        self.printer.send_event("klippy:shutdown")
        self.assertIsNone(self.box.native)
        self.assertNotIn(timer, self.reactor.timers)
        self.assertTrue(all(item not in self.reactor.timers for item in queued))
        self.assertTrue(self.native.is_connected)
        self.ready()
        before = len(self.native.calls)
        self.assertEqual(self.reactor.fire(timer), self.reactor.NEVER)
        self.assertEqual(len(self.native.calls), before)
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands()[before][1], 0x14)

    def test_shutdown_restart_during_startup_wait_stops_old_callback(self):
        self.make_box()
        self.box.preloading_poweron = None
        timer = self.ready()
        def restart():
            self.reactor.on_pause = None
            self.printer.send_event("klippy:shutdown")
            self.printer.send_event("klippy:ready")
        self.reactor.on_pause = restart
        self.assertEqual(self.reactor.fire(timer), self.reactor.NEVER)
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.reactor.pauses, [0.5])

    def test_reconnect_requires_fresh_identity_and_replacement_family(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.table[0].online = 0
        self.lifecycle.get_connect_state()
        self.assertIsNone(self.box.device_versions.profile(1))
        self.table[0].online = 1
        self.native.serial = b"B" * 19
        self.native.calls.clear()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b"")])
        self.assertIsNone(self.box.device_versions.profile(1))

    def test_shutdown_ready_replacement_keeps_family_revoked(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertIsNotNone(self.box.device_versions.profile(1))
        self.printer.send_event("klippy:shutdown")
        self.native.serial = b"B" * 19
        self.native.calls.clear()
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b"")])
        self.assertEqual(self.box.device_versions.get(1).sn, "B" * 19)
        self.assertIsNone(self.box.device_versions.profile(1))
        self.printer.send_event("klippy:shutdown")
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertIsNone(self.box.device_versions.profile(1))

    def test_missing_buffer_reply_returns_false_after_own_disconnect(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        generation = self.box.device_versions.generation(1)
        self.native.responses[(1, 0x05)] = [None]
        self.assertIs(self.lifecycle._request(1, "GET_BUFFER_STATE"), False)
        self.assertEqual(self.box.device_versions.generation(1), generation + 1)
        self.assertIsNone(self.box.device_versions.get(1))
        self.assertEqual(self.box.box_state.get_Tn_data(1, "state"), "disconnect")
        self.assertTrue(any('"code":"key831"' in message for message in self.printer.gcode.messages))

    def test_timeout_report_restart_does_not_disconnect_new_identity(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.native.responses[(1, 0x05)] = [None]
        original_report = self.printer.gcode.respond_raw
        replacement_generations = []
        def restart_on_report(message):
            original_report(message)
            self.printer.gcode.respond_raw = original_report
            self.printer.send_event("klippy:shutdown")
            self.native.serial = b"B" * 19
            self.ready()
            self.lifecycle.get_connect_state()
            replacement_generations.append(self.box.device_versions.generation(1))
        self.printer.gcode.respond_raw = restart_on_report
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(LifecycleStopped):
                self.lifecycle._request(1, "GET_BUFFER_STATE")
        self.assertTrue(self.lifecycle.running)
        self.assertEqual(self.box.device_versions.get(1).sn, "B" * 19)
        self.assertEqual(self.box.device_versions.generation(1), replacement_generations[0])
        self.assertEqual(self.box.box_state.get_Tn_data(1, "state"), "connect")

    def test_native_exception_terminates_heartbeat_without_auto_restart(self):
        self.make_box()
        timer = self.ready()
        self.lifecycle.get_connect_state()
        def fail_native():
            raise OSError("synthetic native failure")
        self.native.responses[(1, 0x0A)] = [fail_native]
        timers = list(self.reactor.timers)
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self.reactor.fire(timer), self.reactor.NEVER)
        self.assertEqual(self.reactor.timers, timers)
        calls = len(self.native.calls)
        self.assertTrue(self.lifecycle.running)
        self.ready()
        self.assertIs(self.lifecycle._timer, timer)
        self.assertEqual(self.reactor.timers, timers)
        self.assertEqual(len(self.native.calls), calls)
        self.assertEqual(timer.when, self.reactor.NEVER)

    def test_poweron_requires_real_refresh_hook_and_optional_head_fallback(self):
        self.make_box()
        self.box.preloading_poweron = True
        self.ready()
        self.native.responses[(1, 0x03)] = [reply(1, 0x03, data=b"\x01\x02\x03\x04")]
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands()[-3:], [(1, 0x0D, b"\x0f\x02"),
                                                      (1, 0x02, b"\x0f"), (1, 0x03, b"\x0f")])
        self.make_box(head=None)
        self.box.preloading_poweron = True
        self.ready()
        self.assertIs(self.lifecycle.head_present(), True)
        self.lifecycle.get_connect_state()
        self.assertNotIn("filament_switch_sensor None", self.printer.lookups)
        self.assertFalse(any(c == 0x0D and data[1] == 2 for _, c, data in self.native.commands()))

    def test_explicit_poweron_hook_and_head_event_use_current_operation(self):
        refreshes, head_moves = [], []
        self.make_box(startup_refresh=lambda address: refreshes.append(address),
                      move_head=lambda: head_moves.append("E5 F60"),
                      stage6=lambda address, target: self.box.send_native(
                          build_request(address, "EXTRUDE_PROCESS", bytes((2, 6, 0)))))
        self.box.preloading_poweron = True
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertEqual(refreshes, [1])
        self.printer.send_event("box:extrude_process_stage7")
        self.assertEqual(head_moves, [])
        self.box.operations.begin("T1A", "T1B", 1)
        self.box.operations.arm_head()
        self.printer.send_event("box:extrude_process_stage7")
        self.printer.send_event("box:extrude_process_stage7")
        self.assertEqual(head_moves, ["E5 F60"])
        self.assertEqual(self.native.commands()[-1], (1, 0x10, bytes((2, 6, 0))))
        self.assertTrue(self.box.extrude_process_stage7_flag)
        self.printer.send_event("klippy:shutdown")
        self.assertFalse(self.box.filament_sensor_enable)
        self.printer.send_event("box:extrude_process_stage7")
        self.assertEqual(head_moves, ["E5 F60"])

    def test_dependency_failure_and_ready_event_failure_do_not_leave_live_work(self):
        self.make_box()
        del self.printer.objects["filament_switch_sensor head"]
        with self.assertRaises(KeyError):
            self.ready()
        self.assertFalse(self.lifecycle.running)
        self.assertEqual(self.reactor.timers, [])
        self.make_box()
        def fail_ready():
            raise RuntimeError("synthetic ready failure")
        self.printer.events["box:ready"] = [fail_ready]
        with self.assertRaisesRegex(RuntimeError, "synthetic ready failure"):
            self.ready()
        self.assertFalse(self.lifecycle.running)
        self.assertEqual(self.reactor.timers, [])
        self.assertIsNone(self.box.native)
        self.printer.events["box:ready"] = []
        self.assertIsNotNone(self.ready())

    def test_failed_presence_query_does_not_hydrate_saved_metadata(self):
        self.make_box()
        saved = self.box.box_state.tn_save_data["T1"]
        saved["vender"][0] = "saved vendor"
        self.native.responses[(1, 0x08)] = [reply(1, 0x08, status=0x06)]
        self.ready()
        before = list(self.box.box_state.Tn_data["T1"]["vender"])
        self.lifecycle.get_connect_state()
        self.assertEqual(self.box.box_state.Tn_data["T1"]["vender"], before)
        self.assertFalse(self.lifecycle._callbacks)

    def test_aggregate_sensor_policy_and_after_pause_metadata_hook(self):
        self.make_box()
        self.ready()
        self.lifecycle.aggregate_state()
        self.lifecycle.aggregate_state()
        self.assertEqual(self.printer.gcode.scripts[-2:], ["SET_FILAMENT_SENSOR SENSOR=head ENABLE=1"] * 2)
        self.lifecycle.get_connect_state()
        self.lifecycle.aggregate_state()
        self.assertEqual(self.printer.gcode.scripts[-1], "SET_FILAMENT_SENSOR SENSOR=head ENABLE=0")
        self.box.print_stats.state = "printing"
        self.reactor.on_pause = lambda: setattr(self.box.print_stats, "state", "paused")
        self.lifecycle.do_after_pause()
        self.assertEqual(self.printer.gcode.scripts[-1], "WAIT_PAUSE")
        self.assertEqual(self.reactor.pauses[-1], 1)
        self.box.record_tn_move_z(0)
        self.assertEqual(self.printer.records, [(("synthetic.json", "synthetic.gcode"), {"tn_move_z": 0})])

    def test_create_connect_command_print_gate_and_parameter_contract(self):
        self.make_box()
        self.ready()
        command = self.printer.gcode.commands["BOX_CREATE_CONNECT"]
        self.box.print_stats.state = "printing"
        command(NativeCommand({"ADDR": 1}))
        self.assertEqual(self.native.calls, [])
        self.box.print_stats.state = "paused"
        command(NativeCommand({"ADDR": 2}))
        self.assertEqual(self.native.commands(), [(2, 0x01, b"")])
        self.assertEqual(self.box.box_state.get_Tn_data(2, "state"), "None")
        for parameters in ({}, {"ADDR": 0}, {"ADDR": 5}):
            with self.subTest(parameters=parameters), self.assertRaises(CommandError):
                command(NativeCommand(parameters))

    def test_head_reenters_stage4_native_wait_and_keeps_failed_stage6_snapshot(self):
        moves = []
        self.make_box(move_head=lambda: moves.append("head"),
                      stage6=lambda address, target: self.box.send_native(
                          build_request(address, "EXTRUDE_PROCESS", bytes((2, 6, 0)))))
        self.ready()
        self.lifecycle.get_connect_state()
        self.box.operations.begin("T1A", "T1B", 1)
        self.box.operations.arm_head()
        def stage4_reply():
            self.printer.send_event("box:extrude_process_stage7")
            self.printer.send_event("box:extrude_process_stage7")
            return reply(1, 0x10)
        self.native.responses[(1, 0x10)] = [stage4_reply, reply(1, 0x10, status=0x05)]
        self.assertIs(self.box.send_native(build_request(1, "EXTRUDE_PROCESS", b"\x02\x04\x00")), True)
        self.assertEqual(self.native.commands()[-2:], [(1, 0x10, b"\x02\x04\x00"), (1, 0x10, b"\x02\x06\x00")])
        self.assertEqual(moves, ["head"])
        self.assertIs(self.box.extrude_process_stage7_flag, True)
        self.assertEqual(self.box.extrude_process_stage7_ret,
                         {"ret": False, "extrude_process_ret_state": "EXTRUDE_ERR1"})
        self.assertEqual(self.box.extrude_process_state, "OK")
        self.assertFalse(self.box.heart_process_enable[0])

    def test_metadata_hook_catches_record_errors_but_not_dependency_lookup(self):
        self.make_box()
        self.ready()
        self.box.virtual_sdcard.current_file = None
        with self.assertLogs(level="ERROR"):
            self.assertIsNone(self.box.record_tn_move_z(0))
        self.assertEqual(self.printer.records, [])
        del self.printer.objects["gcode_move"]
        with self.assertRaises(KeyError):
            self.box.record_tn_move_z(0)

    def test_operation_cancel_stops_error_followup_without_a_link_change(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.box.operations.begin("T1A", "T1B", 1)
        self.box.operations.arm_head()
        self.box.print_stats.state = "printing"
        self.reactor.on_pause = self.box.operations.cancel
        before = len(self.printer.gcode.scripts)
        with self.assertLogs(level="ERROR"):
            self.lifecycle.report_error("key_timeout", {"address": 1})
        self.assertEqual(self.printer.gcode.scripts[before:], ["PAUSE POST_WORK=0", "M400"])
        self.assertEqual(self.reactor.pauses, [1])
        self.assertFalse(self.box.inside_error)
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.box.heart_process_enable[0])
        self.assertIsNotNone(self.box.device_versions.profile(1))

    def test_native_status_table_uses_real_lifecycle_error_handlers(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        request = build_request(1, "EXTRUDE_PROCESS", b"\x01\x04\x00")
        for status in STATUS_NAMES:
            with self.subTest(status=status):
                self.box.last_cmd, self.box.last_err, self.box.error = "T1A", None, None
                self.box.error_tnn.clear()
                self.box.box_filament_err = False
                self.box.is_use_ending_material = False
                raw = reply(1, 0x10, status=status,
                            data=b"\x01\x02\x03\x04" if status == 0x30 else b"")
                self.native.responses[(1, 0x10)] = [raw] * (7 if status in (2, 4) else 1)
                if status == 0x50:
                    with self.assertRaisesRegex(MissingProtocolEffect, "schedule_tighten_up"):
                        self.box.send_native(request)
                    self.assertFalse(self.box.box_filament_err)
                else:
                    if status in (3, 0x13):
                        with self.assertLogs(level="WARNING"):
                            result = self.box.send_native(request)
                    else:
                        result = self.box.send_native(request)
                    self.assertIs(result, status in (0, 3, 0x30))
                    self.assertFalse(self.box.inside_error)

    def test_new_family_confirmation_reenters_initialization_before_polling(self):
        self.make_box()
        self.lifecycle.families = {}
        self.ready()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b"")])
        self.box.device_versions.set_family(1, "cfs0")
        self.native.calls.clear()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b""), (1, 0x0A, b""),
                                                 (1, 0x0D, b"\x0f\x00")])
        self.native.calls.clear()
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.calls, [])

    def test_live_version_change_cancels_operation_and_refreshes_layout(self):
        self.make_box()
        self.ready()
        self.lifecycle.get_connect_state()
        self.box.operations.begin("T1A", "T1B", 1)
        self.box.operations.arm_head()
        self.box.slot_events[1] = {"data": bytes((1, 2, 3, 4))}
        self.native.version = b"142"
        self.box.send_native(build_request(1, "GET_VERSION_SN"))
        self.assertFalse(self.box.operations.active)
        self.assertTrue(self.box.heart_process_enable[0])
        self.assertNotIn(1, self.box.slot_events)
        self.assertEqual(self.box.device_versions.profile(1).state_data_length, 6)
        self.native.calls.clear()
        self.native.responses[(1, 0x0A)] = [reply(1, 0x0A, data=bytes((20, 30, 0, 1, 3, 5)))]
        self.lifecycle.get_connect_state()
        self.assertEqual(self.native.commands(), [(1, 0x14, b""), (1, 0x0A, b""), (1, 0x0D, b"\x0f\x00")])
        self.assertEqual(self.box.state_extensions[1], {"connection_mask": 3, "tag_predicate_mask": 5})


if __name__ == "__main__":
    unittest.main()
