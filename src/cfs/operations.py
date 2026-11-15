"""Ownership of cooperative operation waits and head-event results."""

from copy import deepcopy
from math import isfinite

from .native import UnsupportedRequest, build_request, slot_mask


class OperationCancelled(RuntimeError):
    """The operation no longer owns its binding, device, or error context."""


class PreloadTrial:
    """Admit one explicitly confirmed preload without enabling host mechanics.

    Permission belongs to the current cooperative dispatch and exact request
    object. It cannot stop a command already executing inside the stock library
    or MCU. Native retry/physical-completion limits remain separate.
    """

    def __init__(self, box):
        self.box = box
        self._token = self._request = None
        self._claimed = self._cancelled = False
        self.address = None

    @property
    def active(self):
        return self._token is not None

    def cancel(self):
        # Keep ownership occupied until its caller unwinds; a new trial cannot
        # overlap an old native call which has not returned yet.
        self._cancelled = True

    def admit(self, request):
        if request is not self._request:
            return None
        self._guard()
        if self._claimed:
            raise UnsupportedRequest("the trial request has already been admitted")
        self._claimed = True
        return self._guard

    def validate_reply(self, request, frame):
        """Reject unexpected trial data before ordinary decoder persistence."""
        self._guard()
        if frame is None or frame.status != 0:
            raise RuntimeError("test preload needs a fresh normal reply, not an ACK/event fallback")
        if request.command == 0x0D:
            valid = frame.data == b""
        elif request.command == 0x0A:
            valid = len(frame.data) == 4 and frame.data[3] == 0
        elif request.command == 0x08:
            valid = len(frame.data) == 1 and frame.data[0] <= 15
        elif request.command == 0x02:
            # The general decoder accepts multiple records. A trial must not
            # persist metadata from unselected slots or silently fold duplicates.
            prefix = self.target[2].encode("ascii") + b":"
            parts = frame.data.split(b";")
            valid = len(parts) == 2 and parts[1] == b"" and parts[0].startswith(prefix)
            value = parts[0][2:] if valid else b""
            valid = valid and (value in (b"none", b"unknown") or len(value) == 40)
        else:
            valid = request.command == 0x03 and len(frame.data) == 4
        if not valid:
            raise ValueError("unexpected reply data or non-IDLE state in test preload")

    def _send(self, command, data=b""):
        self._guard()
        timeout = min(300 if command == "SET_PRE_LOADING" else 2,
                      self._deadline - self.box._reactor.monotonic())
        request = build_request(self.address, command, data, timeout=timeout)
        previous = self.box.last_replies.get(self.address)
        self._request, self._claimed = request, False
        try:
            result = self.box.send_native(request, retries_en=False)
            self._guard()
            frame = self.box.last_replies.get(self.address)
            if (result is not True or frame is None or frame is previous
                    or frame.command != request.command or frame.address != self.address):
                raise RuntimeError("test preload did not receive its own fresh reply")
            return frame
        finally:
            self._request = None

    def run(self, tnn, confirmed=False):
        box, lifecycle = self.box, self.box.lifecycle
        if confirmed is not True or not isinstance(tnn, str) or tnn not in box.box_state.Tnn_content:
            raise ValueError("test preload requires a physical TNN and explicit CONFIRM=1")
        if self.active:
            raise RuntimeError("a test preload is already running")
        if lifecycle is None or not lifecycle.passive:
            raise RuntimeError("test preload requires the passive lifecycle")
        lifecycle.check()
        address = box.box_state.Tnn_content[tnn]["addr"]
        if (box.native is None or box.boxcfg.version != 1
                or box.device_versions.require_supported(address).version != "1.1.3"):
            raise RuntimeError("test preload requires encoding 1 and confirmed cfs0 MCU 1.1.3")
        pause = box._printer.lookup_object("pause_resume", None)
        if (pause is None or not hasattr(pause, "is_paused")
                or not callable(getattr(box.virtual_sdcard, "is_active", None))):
            raise RuntimeError("test preload needs retained pause and virtual-SD observations")
        owner = lifecycle.current_dispatch()
        if owner is None:
            raise RuntimeError("test preload needs a cooperative dispatch owner")
        context = lifecycle._report_guard(address)
        token = object()
        self._deadline = deadline = lifecycle.reactor.monotonic() + 300

        def check():
            context()
            lifecycle.guard_device(address)
            context()
            try:
                unsafe = (self._token is not token or self._cancelled
                    or box.lifecycle is not lifecycle or not lifecycle.passive
                    or box.boxcfg.version != 1
                    or lifecycle.current_dispatch() is not owner
                    or lifecycle.reactor.monotonic() >= deadline
                    or box.box_state.get_Tn_data(address, "state") != "connect"
                    or not lifecycle.online(address) or not box.native.is_connected
                    or box.operations.active or box.error is not None or box.inside_error
                    or box.print_stats.state != "standby" or box.virtual_sdcard.is_active()
                    or pause.is_paused or lifecycle.head_sensor is None
                    or lifecycle.head_sensor.runout_helper.filament_present != 0)
            except (AttributeError, TypeError):
                self.cancel()
                raise OperationCancelled("test preload observations became unavailable")
            if unsafe:
                self.cancel()
                raise OperationCancelled("test preload ownership, idle state or head observation changed")

        self._token, self._cancelled = token, False
        self.address, self.target, self._guard = address, tnn, check
        mask = slot_mask(tnn[2])
        try:
            check()
            self._send("GET_BOX_STATE")
            material = self._send("GET_FILAMENT_SENSOR_STATE", b"\x00")
            if not material.data[0] & mask:
                raise RuntimeError("the selected slot has no fresh material observation")
            connections = self._send("GET_FILAMENT_SENSOR_STATE", b"\x01")
            if connections.data[0]:
                # MCU 1.1.3 prepares the shared hub before its selected-slot
                # loop. Do not enter that path with any observed occupied lane.
                raise RuntimeError("test preload requires an empty CONNECTIONS mask")
            self._send("SET_PRE_LOADING", bytes((mask, 2)))
            # The MCU may acknowledge after its internal wait expires. A normal
            # IDLE reply is required before consuming RFID/remaining data.
            self._send("GET_BOX_STATE")
            rfid = self._send("GET_RFID", bytes((mask,)))
            remaining = self._send("GET_REMAIN_LEN", bytes((mask,)))
            return {"target": tnn, "rfid": rfid.data[2:-1].decode("latin-1"),
                    "remaining": remaining.data["ABCD".index(tnn[2])]}
        finally:
            self.cancel()
            self._request = self._token = None


class OperationContext:
    """Coordinate an operation without owning transport or mechanical actions."""

    def __init__(self, box):
        self.box = box
        self.active = False
        self.source_address = None
        self._serial = 0
        self._head_running = False
        self._heartbeat_saved = None
        box.filament_sensor_enable = False
        box.extrude_tnn = None
        box.extrude_process_stage7_flag = False
        box.extrude_process_stage7_ret = None
        box.extrude_process_inside_retry_loose = False
        box.extrude_process_stage7_flag_when_retry_loose = False

    def begin(self, source, target, address):
        if self.active:
            raise RuntimeError("a CFS operation is already active")
        if (self.box.native is None or type(address) is not int
                or address not in range(1, 5) or not isinstance(target, str)
                or target not in self.box.box_state.Tnn_content
                or self.box.box_state.Tnn_content[target]["addr"] != address):
            raise ValueError("an operation needs a bound address and matching physical target")
        self.box.device_versions.require_supported(address)
        self._serial += 1
        self.source, self.target, self.address = deepcopy(source), deepcopy(target), address
        self.error_context = deepcopy(self.box.error_tnn)
        self._error = deepcopy(self.box.error)
        self._binding = self.box._binding_generation
        self._device = self.box.device_versions.generation(address)
        source_content = self.box.box_state.Tnn_content.get(source) if isinstance(source, str) else None
        self.source_address = source_content["addr"] if source_content else None
        self._source_device = (self.box.device_versions.generation(self.source_address)
                               if self.source_address is not None else None)
        self.box.last_cmd_tmp = deepcopy(source)
        self.box.extrude_tnn = deepcopy(target)
        self.box.filament_sensor_enable = False
        self.box.extrude_process_stage7_flag = False
        self.box.extrude_process_stage7_ret = None
        self.box.extrude_process_inside_retry_loose = False
        self.box.extrude_process_stage7_flag_when_retry_loose = False
        self._head_running = False
        self.active = True

    def arm_head(self):
        """Allow head events only while the loading stage owns the target."""
        self._check(self._serial)
        if self._heartbeat_saved is None:
            self._heartbeat_saved = (self.address - 1,
                                     self.box.heart_process_enable[self.address - 1])
            self.box.heart_process_enable[self.address - 1] = False
        self.box.filament_sensor_enable = True

    def cancel(self):
        if self._heartbeat_saved is not None:
            index, value = self._heartbeat_saved
            self.box.heart_process_enable[index] = value
            self._heartbeat_saved = None
        self.active = False
        self._head_running = False
        self.box.filament_sensor_enable = False
        self._serial += 1

    def _check(self, serial, check_error=True):
        if (not self.active or serial != self._serial or self.box.native is None
                or self._binding != self.box._binding_generation
                or self._device != self.box.device_versions.generation(self.address)
                or (self.source_address is not None and self._source_device !=
                    self.box.device_versions.generation(self.source_address))
                or (check_error and self._error != self.box.error)
                or self.target != self.box.extrude_tnn):
            if self.active and serial == self._serial:
                self.cancel()
            raise OperationCancelled("CFS operation context changed")

    def wait(self, predicate, interval=1.0):
        """Wait cooperatively, checking ownership after every re-entrant call."""
        if not isfinite(interval) or interval <= 0:
            raise ValueError("wait interval must be finite and positive")
        serial = self._serial
        try:
            while True:
                self._check(serial)
                done = predicate()
                self._check(serial)
                if done:
                    return
                reactor = self.box._reactor
                reactor.pause(reactor.monotonic() + interval)
        except Exception:
            if self.active and serial == self._serial:
                self.cancel()
            raise

    def head(self, move_head, stage6):
        """Handle one permitted head event using required actual phase actions."""
        if not self.active or not self.box.filament_sensor_enable:
            return
        serial = self._serial
        if self.box.extrude_process_stage7_flag or self._head_running:
            return
        self._check(serial)
        if self.box.extrude_process_inside_retry_loose:
            self.box.extrude_process_stage7_flag_when_retry_loose = True
            return
        if not callable(move_head) or not callable(stage6):
            self.cancel()
            raise RuntimeError("the active CFS head phase is not implemented")
        self._head_running = True
        try:
            move_head()
            self._check(serial)
            result = stage6(self.address, self.target)
            self._check(serial, check_error=False)
            self.box.extrude_process_stage7_flag = True
            self.box.extrude_process_stage7_ret = {
                "ret": deepcopy(result),
                "extrude_process_ret_state": deepcopy(self.box.extrude_process_state),
            }
            if self._error != self.box.error:
                self.cancel()
        except Exception:
            if self.active and serial == self._serial:
                self.cancel()
            raise
        finally:
            # A new operation may have started during either action.
            if serial == self._serial:
                self._head_running = False
