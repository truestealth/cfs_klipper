"""Ownership of cooperative operation waits and head-event results."""

from copy import deepcopy
from math import isfinite


class OperationCancelled(RuntimeError):
    """The operation no longer owns its binding, device, or error context."""


class OperationContext:
    """Coordinate an operation without owning transport or mechanical actions."""

    def __init__(self, box):
        self.box = box
        self.active = False
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
