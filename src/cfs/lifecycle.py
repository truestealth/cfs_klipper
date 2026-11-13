"""Own Klipper scheduling while borrowing the stock CFS transport and objects."""

import importlib
import logging
import os

from .errors import ErrorHandler
from .native import build_request
from .operations import OperationCancelled


class LifecycleStopped(RuntimeError):
    """A cooperative callback no longer belongs to the current lifecycle."""


class Lifecycle:
    def __init__(self, box, printer, families=None, address_table=None,
                 port_exists=os.path.exists, current_dispatch=None,
                 startup_refresh=None, move_head=None, stage6=None):
        self.box, self.printer = box, printer
        self.families, self.address_table = families, address_table
        self.port_exists, self.current_dispatch = port_exists, current_dispatch
        self.startup_refresh = startup_refresh if startup_refresh is not None else box.startup_refresh
        self.move_head = move_head if move_head is not None else box.loading.move_head
        self.stage6 = stage6 if stage6 is not None else box.loading.stage6
        self.reactor = printer.get_reactor()
        self.running = self.registered = False
        self.is_shutdown = True
        self._generation = 0
        self._timer = None
        self._callbacks = set()
        self._initial_wait_done = False
        self.address = 0
        self.counters = [5] * 4
        self._versions = None
        self._initialized = {}

    def register(self):
        """Register functional base commands before macro rename_existing runs."""
        if self.registered:
            return
        box = self.box
        box.gcode = self.printer.lookup_object("gcode")
        box.register_data_commands(box.gcode, self.printer.lookup_object("webhooks"))
        box.register_native_commands(box.gcode)
        box.register_host_commands(self.printer)
        box.gcode.register_command("BOX_CREATE_CONNECT", box.cmd_create_connect)
        box.gcode.register_command("DO_AFTER_PAUSE", box.cmd_do_after_pause)
        self.printer.register_event_handler("klippy:ready", self.ready)
        self.printer.register_event_handler("klippy:shutdown", self.shutdown)
        self.printer.register_event_handler("box:extrude_process_stage7", self.head_event)
        self.registered = True

    def ready(self):
        if self.running:
            return
        box = self.box
        # Resolve dependencies before starting any extension-owned work.
        for name in ("toolhead", "gcode_move", "print_stats", "virtual_sdcard"):
            setattr(box, name, self.printer.lookup_object(name))
        self.head_sensor = None
        if box.boxcfg.filament_sensor is not None:
            self.head_sensor = self.printer.lookup_object(
                "filament_switch_sensor " + box.boxcfg.filament_sensor)
        if self.address_table is None:
            self.address_table = importlib.import_module(
                "extras.auto_addr_wrapper").addr_manager_table_mb
        if len(self.address_table) != 4:
            raise ValueError("the retained address table must contain four CFS entries")
        if self.current_dispatch is None:
            self.current_dispatch = importlib.import_module("greenlet").getcurrent
        if not callable(self.current_dispatch):
            raise ValueError("the Klipper dispatch reader must be callable")
        self.errors = ErrorHandler(box, box.gcode, self.reactor, box.print_stats,
                                   box.virtual_sdcard, self.current_dispatch)
        box.bind_native(self.printer, families=self.families, effect_handlers={
            "report_error": self.report_error,
            "record_error": self.record_error,
            "timeout_process": self.timeout_process,
            "state_events": self.observe_state_event,
            "schedule_tighten_up": box.runout.schedule_tighten_up,
            "run_gcode": self.run_gcode,
        })
        if self._versions is not None:
            # Keep replacement fingerprints/family revocations across ready cycles.
            box.device_versions = self._versions
        self._versions = box.device_versions
        # No is_connected projection: presence comes from the address table.
        try:
            self.serial_port = box.native.native.serial_port
        except Exception:
            box.unbind_native()
            raise
        self._generation += 1
        self.is_shutdown = False
        self.running = True
        box.cut_sensor.activate()
        self.address, self.counters = 0, [5] * 4
        self._initialized.clear()
        self._initial_wait_done = False
        generation = self._generation
        try:
            self.printer.send_event("box:ready")
            self.check(generation)
            self._timer = self.reactor.register_timer(
                lambda eventtime: self._heartbeat(eventtime, generation), self.reactor.monotonic())
        except Exception:
            if generation == self._generation:
                self.shutdown()
            raise

    def check(self, generation=None):
        if (not self.running or self.is_shutdown or self.printer.is_shutdown()
                or (generation is not None and generation != self._generation)):
            raise LifecycleStopped("CFS lifecycle stopped or changed")

    def shutdown(self):
        self.running, self.is_shutdown = False, True
        self.box.resume.shutdown()
        # These describe extension-owned work, which cannot survive shutdown.
        # Error/source hints remain available for explicit recovery after ready.
        self.box.inside_auto_refill = False
        self.box.inside_extrusion_all_materials = False
        if self.box.cut_sensor is not None:
            self.box.cut_sensor.invalidate()
        self._generation += 1
        if self._timer is not None:
            self.reactor.unregister_timer(self._timer)
            self._timer = None
        for timer in tuple(self._callbacks):
            self.reactor.unregister_timer(timer)
        self._callbacks.clear()
        self._initialized.clear()
        self._versions = self.box.device_versions
        for address in range(1, 5):
            if self.box.box_state.get_Tn_data(address, "state") == "connect":
                self.box.box_state.modify_Tn_data(address, "disconnect", "state")
        self.box.box_state.modify_Tn_data(0, "disconnect", uppart="state")
        self.box.unbind_native()

    def schedule(self, callback):
        """Schedule removable own work, never a borrowed native timer."""
        self.check()
        generation = self._generation
        holder = []

        def run(eventtime):
            timer = holder[0]
            if timer in self._callbacks:
                self._callbacks.discard(timer)
                self.reactor.unregister_timer(timer)
            if self.running and generation == self._generation:
                callback(eventtime)
            return self.reactor.NEVER

        timer = self.reactor.register_timer(run, self.reactor.monotonic())
        holder.append(timer)
        self._callbacks.add(timer)

    def online(self, address):
        return self.address_table[address - 1].online == 1

    def head_present(self):
        """Preserve the stock no-sensor True fallback without claiming a reading."""
        if self.head_sensor is None:
            return True
        return self.head_sensor.runout_helper.filament_present == 1

    def _request(self, address, command, data=b""):
        self.check()
        self.box.print_state = self.box.print_stats.state
        return self.box.send_native(build_request(address, command, data))

    def disconnect(self, address):
        self._initialized.pop(address, None)
        self.box.box_state.modify_Tn_data(address, "disconnect", "state")
        self.box.invalidate_device(address)
        self.counters[address - 1] = 5

    def guard_device(self, address):
        """Stop a request if its previously connected address disappeared."""
        if (self.box.box_state.get_Tn_data(address, "state") == "connect"
                and (not self.online(address) or not self.port_exists(self.serial_port))):
            self.disconnect(address)

    def get_connect_state(self):
        guard = self._report_guard(operation=False)
        guard()
        for address in range(1, 5):
            guard()
            state = self.box.box_state.get_Tn_data(address, "state")
            if not self.online(address) or not self.port_exists(self.serial_port):
                if state == "connect":
                    self.disconnect(address)
                continue
            if state not in ("None", "disconnect"):
                # A newly confirmed family/version needs startup even when
                # the address was already present. Unknown profiles stay gated.
                if (state != "connect" or self.box.device_versions.profile(address) is None
                        or self._initialized.get(address) == self.box.device_versions.generation(address)):
                    continue
            self.box.box_state.modify_Tn_data(address, "connect", "state")
            self.counters[address - 1] = 5
            # Deliberately identify first: 0A decoding needs this device's layout.
            self.box.invalidate_device(address)
            self._request(address, "GET_VERSION_SN")
            guard()
            if self.box.device_versions.profile(address) is None:
                continue
            first = self._request(address, "GET_BOX_STATE")
            guard()
            mode = self.box.box_state.get_box_mode(address)
            if first is None or mode not in ("0", "1", "2", "3", "4", "5"):
                self._request(address, "GET_BOX_STATE")
                generation = self.box.device_versions.generation(address)
                self.reactor.pause(self.reactor.monotonic() + 0.5)
                guard()
                if generation != self.box.device_versions.generation(address):
                    raise LifecycleStopped("CFS changed while reading its mode")
                mode = self.box.box_state.get_box_mode(address)
            # Reported mode encoding differs from SET_BOX_MODE's action byte.
            if mode != "1":
                self._request(address, "SET_BOX_MODE", b"\x00\x01")
                guard()
            self.preloading_permission(address)
            guard()
            if self.box.preloading_poweron and self.head_present() is False:
                if not callable(self.startup_refresh):
                    raise RuntimeError("CFS power-on preload/refresh is not implemented")
                device = self.box.device_versions.generation(address)
                self.startup_refresh(address)
                guard()
                if device != self.box.device_versions.generation(address):
                    raise LifecycleStopped("CFS changed during power-on refresh")
            elif state == "None":
                result = self._request(address, "GET_FILAMENT_SENSOR_STATE", b"\x00")
                guard()
                frame = self.box.last_replies.get(address)
                if result is True and frame is not None and frame.status == 0:
                    mask = self.box.box_state.get_Tn_inner_data(
                        address, "filament_sensor", subpart="material")
                    self.box.box_state.sync_tn_data("T%d" % address, mask)
                    self.box._schedule_groups(build_request(address, "GET_RFID", b"\x0f"))
            if (self.box.device_versions.profile(address) is not None
                    and self.box.box_state.get_Tn_data(address, "state") == "connect"):
                # This records a completed host startup attempt, not motion.
                self._initialized[address] = self.box.device_versions.generation(address)

    def preloading_permission(self, address):
        state = self.box.print_stats.state
        if state in ("printing", "pause"):
            return None
        action = 1 if self.box.preloading_en else 0
        return self._request(address, "SET_PRE_LOADING", bytes((15, action)))

    def aggregate_state(self):
        state = self.box.box_state.Tn_data
        connected = any(state["T%d" % address]["state"] == "connect" for address in range(1, 5))
        if not connected:
            state["state"] = "disconnect"
            self.box.set_head_runout_enabled(1)
        elif state["state"] in ("None", "disconnect"):
            state["state"] = "connect"
            self.box.set_head_runout_enabled(1 - state.get("enable", 1))

    def _heartbeat(self, eventtime, generation=None):
        generation = self._generation if generation is None else generation
        try:
            self.check(generation)
            if not self._initial_wait_done:
                for _ in range(10):
                    if self.box.preloading_poweron is not None:
                        break
                    self.reactor.pause(self.reactor.monotonic() + 0.5)
                    self.check(generation)
                self._initial_wait_done = True
            self.get_connect_state()
            self.check(generation)
            self.address = self.address % 4 + 1
            address = self.address
            if (self.box.box_state.get_Tn_data(address, "state") == "connect"
                    and self.box.heart_process_enable[address - 1]
                    and self.box.device_versions.profile(address) is not None):
                result = self._request(address, "GET_BOX_STATE")
                self.check(generation)
                self.counters[address - 1] = self.counters[address - 1] - 1 if result is None else 5
                if self.counters[address - 1] <= 0:
                    self.timeout_process(address)
            self.aggregate_state()
            self.check(generation)
            return self.reactor.monotonic() + 1
        except (LifecycleStopped, OperationCancelled):
            return self.reactor.NEVER
        except Exception:
            # Match the recovered terminal exception path; no automatic restart.
            logging.exception("CFS heartbeat terminated")
            return self.reactor.NEVER

    def _report_guard(self, address=None, operation=True):
        generation, binding = self._generation, self.box._binding_generation
        device = self.box.device_versions.generation(address) if address else None
        serial = self.box.operations._serial
        active_operation = operation and self.box.operations.active

        def check():
            self.check(generation)
            if (binding != self.box._binding_generation or
                    (operation and serial != self.box.operations._serial) or
                    (address is not None and device != self.box.device_versions.generation(address))):
                raise LifecycleStopped("CFS context changed during a cooperative action")
            if active_operation:
                # Explicit result/error branches consume changed errors; a
                # different target or device cannot inherit the old action.
                self.box.operations._check(serial, check_error=False)
        return check

    def report_error(self, key, facts):
        self.box.print_state = self.box.print_stats.state
        self.errors.report_error(key, facts, check_context=self._report_guard(facts.get("address")))

    def observe_state_event(self, address, data):
        """Retain slot events separately from ordinary state observations."""
        self.check()
        self.box.slot_events[address] = {
            "data": bytes(data), "generation": self.box.device_versions.generation(address),
        }
        self.box.events.process(address, data)

    def record_error(self, reason, context):
        guard = self._report_guard(context.get("addr"))
        guard()
        self.errors.record_error(reason, context)
        guard()

    def run_gcode(self, script):
        guard = self._report_guard(operation=False)
        guard()
        self.box.gcode.run_script_from_command(script)
        guard()

    def timeout_process(self, address):
        state = self.box.box_state.get_Tn_data(address, "state")
        if state != "connect" and self.box.native.is_connected:
            return None
        guard = self._report_guard(address)
        self.report_error("key_timeout", {"address": address})
        guard()
        if state == "connect":
            self.disconnect(address)
            return self.box.device_versions.generation(address)
        return None

    def do_after_pause(self):
        guard = self._report_guard()
        guard()
        dispatch = self.current_dispatch()
        self.box.dispatch = dispatch
        while (self.box.print_stats.state != "paused" and
               dispatch != self.box.virtual_sdcard.work_handler_dispatch):
            self.reactor.pause(self.reactor.monotonic() + 1)
            guard()
        guard()
        self.box.gcode.run_script_from_command("WAIT_PAUSE")

    def head_event(self):
        if not self.running:
            return
        try:
            self.box.operations.head(self.move_head, self.stage6)
        except OperationCancelled:
            return
