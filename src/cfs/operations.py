"""Ownership of cooperative operation waits and head-event results."""

from copy import deepcopy
from dataclasses import replace
from math import isfinite

from .native import UnsupportedRequest, build_request, slot_mask


class OperationCancelled(RuntimeError):
    """The operation no longer owns its binding, device, or error context."""


class PreloadTrial:
    """Admit one confirmed native preload or retraction trial without host mechanics.

    Permission belongs to the current cooperative dispatch and exact request
    object. It cannot stop a command already executing inside the stock library
    or MCU. Native retry/physical-completion limits remain separate.
    """

    def __init__(self, box):
        self.box = box
        self._token = self._request = None
        self._claimed = self._cancelled = False
        self._retract = False
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
        if self._retract:
            command, data = self._retract_requests[self._retract_step]
            if (request.address != self.address or request.command != command or request.data != data):
                raise UnsupportedRequest("the retract trial request is out of order")
            if request.command == 0x11:
                self._retract_started = True
        self._claimed = True
        return self._guard

    def validate_reply(self, request, frame):
        """Reject unexpected trial data before ordinary decoder persistence."""
        self._guard()
        if self._retract:
            # The retained error is an initial observation, never normal data
            # for the decoder. Every write and subsequent read must be normal.
            if (self._retract_step == 0 and frame is not None and frame.status in (0x13, 0x19)
                    and len(frame.data) == 4 and frame.data[3] == 4):
                return
            if frame is None or frame.status != 0:
                raise RuntimeError("test retract requires a fresh normal reply after its initial observation")
            if request.command == 0x0A:
                valid = len(frame.data) == 4 and frame.data[3] in ((0, 2) if self._retract_step == 0 else (0,))
            elif request.command in (0x04, 0x11):
                valid = frame.data == b""
            else:
                valid = (len(frame.data) == 1
                         and frame.data[0] <= (2 if request.command == 0x05 else 15))
            if not valid:
                raise ValueError("unexpected reply data or mode in test retract")
            return
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
        if self._request is not None:
            raise UnsupportedRequest("another trial request is still pending")
        timeout = 300 if command == "SET_PRE_LOADING" else 2
        if command == 0x11:
            timeout = 150 if data[1] else 10
        timeout = min(timeout, self._deadline - self.box._reactor.monotonic())
        request = build_request(self.address, command, data, timeout=timeout)
        previous = self.box.last_replies.get(self.address)
        self._request, self._claimed = request, False
        try:
            initial_retract = self._retract and self._retract_step == 0
            result = self.box.send_native(request, retries_en=False, report_err=not initial_retract)
            self._guard()
            frame = self.box.last_replies.get(self.address)
            initial_error = (initial_retract and frame is not None and frame.status in (0x13, 0x19))
            if ((result is not True and not (initial_error and result is False)) or frame is None or frame is previous
                    or frame.command != request.command or frame.address != self.address):
                raise RuntimeError("native trial did not receive its own fresh reply")
            return frame
        finally:
            self._request = None

    def run(self, tnn, confirmed=False):
        box, lifecycle = self.box, self.box.lifecycle
        if confirmed is not True or not isinstance(tnn, str) or tnn not in box.box_state.Tnn_content:
            raise ValueError("test preload requires a physical TNN and explicit CONFIRM=1")
        if self.active or box.material_trial.active:
            raise RuntimeError("a test preload is already running")
        self._retract = False
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

    def run_retract(self, tnn, confirmed=False, trigger="BUFFER"):
        """Observe one explicit selected pull, with no host motion or source edits.

        BUFFER establishes only empty-buffer observations. MATERIAL additionally
        requires an absent head and empty connections; it is never an automatic
        continuation. The native library may retry internally; the cooperative
        deadline cannot interrupt an in-flight MCU action.
        """
        box, lifecycle = self.box, self.box.lifecycle
        if (confirmed is not True or not isinstance(tnn, str) or tnn not in box.box_state.Tnn_content
                or trigger not in ("BUFFER", "MATERIAL")):
            raise ValueError("test retract requires a physical TNN, BUFFER/MATERIAL trigger and CONFIRM=1")
        if self.active or box.material_trial.active or box.operations.active:
            raise RuntimeError("another CFS trial or operation is still owned")
        if lifecycle is None or not lifecycle.passive:
            raise RuntimeError("test retract requires the passive lifecycle")
        lifecycle.check()
        address = box.box_state.Tnn_content[tnn]["addr"]
        if (box.native is None or box.boxcfg.version != 1
                or box.device_versions.require_supported(address).version != "1.1.3"):
            raise RuntimeError("test retract requires confirmed cfs0 MCU 1.1.3 and encoding 1")
        pause = box._printer.lookup_object("pause_resume", None)
        owner = lifecycle.current_dispatch()
        if pause is None or owner is None:
            raise RuntimeError("test retract requires retained pause state and a dispatch owner")
        source = (box.last_cmd, box.box_state.get_Tn_data(address, "filament"))
        if not (source[0] is None and source[1] in (None, "None")) and source != (tnn, tnn):
            raise RuntimeError("test retract source hints conflict with the selected target")
        context = lifecycle._report_guard(address)
        configuration = {key: deepcopy(value) for key, value in vars(box.boxcfg).items() if key != "config"}
        head = getattr(getattr(getattr(lifecycle, "head_sensor", None), "runout_helper", None),
                       "filament_present", None)
        token = object()
        self._deadline = deadline = lifecycle.reactor.monotonic() + (240 if trigger == "MATERIAL" else 60)
        self.address, self.target = address, tnn
        mask = slot_mask(tnn[2])
        self._retract_requests = ((0x0A, b""), (0x08, b"\x00"), (0x08, b"\x01"), (0x05, b""),
            (0x04, b"\x00\x01"), (0x0A, b""), (0x11, bytes((mask, int(trigger == "MATERIAL")))),
            (0x0A, b""), (0x08, b"\x00"), (0x08, b"\x01"), (0x05, b""))
        self._retract, self._retract_started, self._retract_step = True, False, 0

        def check():
            context()
            lifecycle.guard_device(address)
            context()
            try:
                current_head = lifecycle.head_sensor.runout_helper.filament_present
                unsafe = (self._token is not token or self._cancelled
                    or box.lifecycle is not lifecycle or not lifecycle.passive
                    or lifecycle.current_dispatch() is not owner
                    or configuration != {key: value for key, value in vars(box.boxcfg).items() if key != "config"}
                    or lifecycle.reactor.monotonic() >= deadline
                    or box.box_state.get_Tn_data(address, "state") != "connect"
                    or not lifecycle.online(address) or not box.native.is_connected
                    or any(lifecycle.online(other) or box.box_state.get_Tn_data(other, "state") == "connect"
                           for other in range(1, 5) if other != address)
                    or box.operations.active or box.material_trial.active
                    or box.error is not None or box.inside_error or box.box_state.extrude_err
                    or box.is_use_ending_material or box.inside_auto_refill or box.inside_extrusion_all_materials
                    or box.print_stats.state != "standby" or box.virtual_sdcard.is_active()
                    or box.virtual_sdcard.do_resume_status or pause.is_paused
                    # Retained button callbacks store integer 0/1; their
                    # public status projection converts those values to bool.
                    or type(head) not in (bool, int) or head not in (0, 1)
                    or type(current_head) not in (bool, int) or current_head not in (0, 1)
                    or (not self._retract_started and current_head != head)
                    or source != (box.last_cmd, box.box_state.get_Tn_data(address, "filament")))
            except (AttributeError, TypeError, KeyError):
                self.cancel()
                raise OperationCancelled("test retract observations became unavailable")
            if unsafe:
                self.cancel()
                raise OperationCancelled("test retract ownership or observations changed")

        self._token, self._cancelled, self._guard = token, False, check
        try:
            observations = []
            for step, (command, data) in enumerate(self._retract_requests):
                self._retract_step = step
                frame = self._send(command, data)
                observations.append(frame)
                if step in (1, 8) and not frame.data[0] & mask:
                    raise RuntimeError("test retract selected slot has no fresh material observation")
                if step == 2 and frame.data[0] != mask:
                    raise RuntimeError("test retract requires only the selected CONNECTIONS lane")
                if step == 9 and frame.data[0] not in ((0,) if trigger == "MATERIAL" else (0, mask)):
                    raise RuntimeError("test retract final CONNECTIONS do not match its trigger")
            if observations[-1].data[0] != 2:
                raise RuntimeError("fresh observations do not establish an empty BUFFER result")
            if trigger == "MATERIAL" and lifecycle.head_sensor.runout_helper.filament_present != 0:
                raise RuntimeError("test MATERIAL retract still observes filament at the head")
            return {"target": tnn, "head": bool(lifecycle.head_sensor.runout_helper.filament_present),
                    "mode": observations[7].data[3], "material": observations[8].data[0],
                    "connections": observations[9].data[0], "buffer": observations[10].data[0]}
        finally:
            self.cancel()
            self._request = self._token = None


class MaterialTrial:
    """Own one confirmed baseline load or cut/unload while startup stays passive.

    Existing phase executors supply the mechanics. Only their owning dispatch
    and one armed head callback may use the scoped host/native permission.
    Faults stop before automatic recovery; revocation cannot interrupt a native
    call, an MCU action or already queued Klipper motion.
    """

    def __init__(self, box):
        self.box = box
        self._token = self._head_owner = None
        self._cancelled = self._root_done = False
        self.address = None
        self._command = None
        self._command_name = None
        self._command_claimed = self._command_completed = False
        self._unknown_source = False
        self._unknown_retract_step = 0
        self._unknown_retract_pending = None

    @property
    def active(self):
        return self._token is not None

    def cancel(self):
        self._cancelled = True

    def owns_dispatch(self):
        lifecycle = self.box.lifecycle
        if not self.active or lifecycle is not self._lifecycle:
            return False
        current = lifecycle.current_dispatch()
        return current is self._owner or (self._head_owner is not None and current is self._head_owner)

    def guard(self, address=None):
        if not self.active:
            raise RuntimeError("CFS admission mode blocks host actions")
        if not self.owns_dispatch():
            raise RuntimeError("this dispatch does not own the material trial")
        if address is not None and address != self.address:
            raise UnsupportedRequest("the material trial owns only its selected CFS")
        self._guard()
        return self._guard

    def owns_phase(self, action, name):
        """Identify the owning root's claimed, incomplete recipe phase."""
        if (not self.owns_dispatch() or self._lifecycle.current_dispatch() is not self._owner
                or self.action != action or self._phase != action.lower()
                or self._command_name != name or not self._command_claimed or self._command_completed):
            return False
        self._guard()
        if (action == "UNLOAD" and not self._unknown_source
                and (self.source != self.target or self.box.last_cmd != self.target
                     or self.box.box_state.get_Tn_data(self.address, "filament") != self.target)):
            self.cancel()
            raise OperationCancelled("coordinated unload lost its known selected source")
        return True

    def _bounded_profile(self, hot=False):
        """Bound owned feed/withdrawal without changing printer scaling."""
        self._guard()
        box = self.box
        if self.action == "UNLOAD" and not callable(getattr(box.toolhead, "flush_step_generation", None)):
            raise RuntimeError("coordinated unload requires retained step-generation submission")
        status = box.gcode_move.get_status(box._reactor.monotonic())
        if any(type(status[key]) not in (int, float) or status[key] != 1
               for key in ("speed_factor", "extrude_factor")):
            raise ValueError("bounded material trial requires actual speed/extrude factors of 1")
        if hot:
            if not self.owns_phase("UNLOAD", "BOX_RETRUDE_MATERIAL") or self._unknown_source:
                raise RuntimeError("this phase does not own coordinated withdrawal")
            heater = box.toolhead.extruder.get_heater()
            actual, _ = heater.get_temp(box._reactor.monotonic())
            if (heater.can_extrude is not True or type(actual) not in (int, float)
                    or not isfinite(actual) or not heater.min_extrude_temp <= actual < heater.max_temp):
                raise RuntimeError("coordinated unload requires an actually hot extrusion-safe heater")
        self._guard()

    def allows_command(self, name, gcmd):
        """Admit one exact recipe phase, including its retained macro alias."""
        if not self.owns_dispatch() or self._command is None or self._command_claimed:
            return False
        expected = ("WAIT_EXTRUSION_ALL_MATERIALS" if self._command_name == "BOX_CHECK_MATERIAL"
                    else self._command_name)
        if name != expected:
            return False
        if gcmd is not self._command:
            # These macros create a new command for the base handler. Their
            # parameters must be exactly the ones supplied by this recipe.
            aliases = ("BOX_MATERIAL_FLUSH", "BOX_EXTRUDER_EXTRUDE",
                       "WAIT_EXTRUSION_ALL_MATERIALS")
            if (name not in aliases or gcmd.get_command_parameters()
                    != self._command.get_command_parameters()):
                return False
        self._guard()
        return True

    def call_command(self, action, gcmd):
        """Revoke even if a retained macro dispatcher swallows an exception."""
        self._command_claimed = True
        try:
            result = action(gcmd)
            self._guard()
        except Exception:
            self.cancel()
            raise
        self._command_completed = True
        return result

    def admit(self, request):
        if not self.owns_dispatch():
            return None
        self._guard()
        mask = self.mask
        read = {0x0A: (b"",), 0x05: (b"",), 0x08: (b"\x00", b"\x01")}
        allowed = request.data in read.get(request.command, ())
        if self._phase == "load":
            allowed |= (request.command == 0x0F and request.data in (b"\x00", b"\x01"))
            allowed |= (request.command == 0x04 and request.data == bytes((mask, 0)))
            allowed |= (request.command == 0x10 and len(request.data) == 3
                        and request.data[0] == mask and request.data[1] in (0, 4, 5, 7)
                        and request.data[2] == (int(self.box.boxcfg.extrude_material_len_for_box)
                                                if request.data[1] == 7 else 0))
        elif self._phase == "unload":
            if self._unknown_source:
                # The shared BUFFER selector belongs only to this initial
                # unknown-source sequence, never to a general unload grant.
                steps = ((0x08, b"\x01"), (0x08, b"\x00"), (0x0A, b""),
                         (0x04, b"\x00\x01"), (0x11, b"\x00\x00"),
                         (0x11, bytes((mask, 1))))
                allowed = (self._command_name == "BOX_RETRUDE_MATERIAL"
                           and self._command_claimed and not self._command_completed
                           and self._unknown_retract_step < len(steps)
                           and (request.command, request.data) == steps[self._unknown_retract_step]
                           and self._unknown_retract_pending is None)
            else:
                allowed |= (request.command == 0x04 and request.data == b"\x00\x01")
                allowed |= (request.command == 0x11 and request.data in
                            (bytes((mask, 0)), bytes((mask, 1))))
        if self.box.lifecycle.current_dispatch() is self._head_owner:
            allowed = request.command == 0x10 and request.data == bytes((mask, 6, 0))
        if (request.diagnostic or request.request_state != 0xFF
                or request.address != self.address or not allowed
                or (self._phase in ("before", "after") and request is not self._reading)):
            raise UnsupportedRequest("request is outside the selected material trial phase")
        if self._unknown_source and self._phase == "unload":
            # bounded_request() copies the Request with a shorter timeout;
            # bind its unchanged body while allowing only one call in flight.
            self._unknown_retract_pending = request.body
        return self._guard

    def bounded_request(self, request):
        limit = 2 if request.command in (0x05, 0x08, 0x0A) else request.timeout
        return replace(request, timeout=min(limit, self._deadline - self.box._reactor.monotonic()))

    def validate_reply(self, request, frame):
        self._guard()
        valid = frame is not None and frame.status == 0
        if valid:
            if request.command == 0x0A:
                valid = len(frame.data) == 4 and frame.data[3] in (0, 2)
            elif request.command == 0x08:
                valid = len(frame.data) == 1 and frame.data[0] <= 15
                if valid and self._unknown_source:
                    if request.data == b"\x01":
                        valid = frame.data[0] == (0 if self._phase == "after" else self.mask)
                    else:
                        valid = bool(frame.data[0] & self.mask)
            elif request.command == 0x05:
                valid = len(frame.data) == 1 and frame.data[0] in (0, 1, 2)
            elif request.command == 0x10:
                # Baseline feed stages have different successful reply bodies:
                # 0 carries mode, 5 carries four wheel bytes, 4/6/7 are empty.
                stage = request.data[1]
                valid = len(frame.data) == {0: 1, 5: 4}.get(stage, 0)
                if valid and stage == 0:
                    valid = frame.data == b"\x00"
            else:
                valid = frame.data == b""
        if not valid:
            self.cancel()
            raise RuntimeError("material trial stopped at a missing, abnormal or malformed reply")
        if self._unknown_source and self._phase == "unload":
            if self._unknown_retract_pending != request.body:
                self.cancel()
                raise RuntimeError("unknown-source reply does not own the pending unload step")
            self._unknown_retract_pending = None
            self._unknown_retract_step += 1

    def head_event(self):
        """Permit only the current load's first real, armed head arrival."""
        box = self.box
        if (not self.active or self._cancelled or self.action != "LOAD"
                or self._phase != "load" or self._head_owner is not None
                or not box.operations.active or not box.filament_sensor_enable
                or box.extrude_process_stage7_flag or box.operations._head_running
                or self._lifecycle.head_sensor.runout_helper.filament_present != 1):
            return
        self._head_owner = self._lifecycle.current_dispatch()
        self._head_started = True
        try:
            self._guard()
            box.operations.head(self._lifecycle.move_head, self._lifecycle.stage6)
        except Exception:
            self.cancel()
            # A reactor head callback has no command caller to receive this
            # failure. Its revoked scope stops the owning command at its next
            # guard instead of running any recovery from the callback.
            return
        finally:
            self._head_owner = None
            if self._root_done:
                self._token = None

    def _read(self, command, data=b""):
        self._guard()
        self._reading = request = build_request(self.address, command, data, timeout=2)
        previous = self.box.last_replies.get(self.address)
        try:
            result = self.box.send_native(request)
            self._guard()
            frame = self.box.last_replies.get(self.address)
            if result is not True or frame is None or frame is previous:
                raise RuntimeError("a fresh material-trial observation is required")
            return frame.data
        finally:
            self._reading = None

    def _geometry(self):
        """Check the complete normal route before heat, motion or native writes."""
        box, cfg = self.box, self.box.boxcfg
        status = box.toolhead.get_status(box._reactor.monotonic())
        if not set("xyz").issubset(status["homed_axes"]):
            raise RuntimeError("material trial requires already homed XYZ; it does not home")
        position = box.toolhead.get_position()
        for index, axis in enumerate("xyz"):
            section = cfg.config.getsection("stepper_" + axis)
            low, high = section.getfloat("position_min"), section.getfloat("position_max")
            names = {"x": ("extrude_pos_x", "pre_cut_pos_x", "retrude_position_x", "clean_right_pos_x"),
                     "y": ("safe_pos_y", "extrude_pos_y", "pre_cut_pos_y", "retrude_position_y"),
                     "z": ()}[axis]
            values = [position[index]] + [getattr(cfg, name) for name in names]
            if axis == "y":
                values.extend((cfg.get_real_cut_pos_y(), cfg.extrude_pos_y - 10))
            if axis == "z":
                values.extend((25, 50))
            if (not all(type(value) in (int, float) and isfinite(value) for value in [low, high] + values)
                    or low >= high or any(not low <= value <= high for value in values)):
                raise ValueError("material-trial coordinates exceed configured %s travel" % axis)
        positive = (cfg.clean_velocity, cfg.cut_velocity, cfg.cut_accel, cfg.cut_accel_to_decel,
                    cfg.extrude_material_len_for_extruder, cfg.extrude_material_velocity)
        if not all(type(value) in (int, float) and isfinite(value) and value > 0 for value in positive):
            raise ValueError("material-trial speeds and feed lengths must be finite and positive")
        amount = cfg.extrude_material_len_for_box
        if (type(amount) not in (int, float) or not isfinite(amount)
                or not 0 <= amount <= 255 or amount != int(amount)):
            raise ValueError("material-trial box feed must be an integral byte")
        if (type(cfg.extrude_material_times) is not int or cfg.extrude_material_times <= 0
                or type(cfg.cut_run_count) is not int or cfg.cut_run_count <= 0):
            raise ValueError("material-trial iteration counts must be positive integers")
        if self.action == "LOAD":
            length = cfg.Tn_extrude
            if (type(length) not in (int, float) or not isfinite(length)
                    or not 1 <= length <= 200 or length != int(length)):
                raise ValueError("owned LOAD flush length must be an integer from 1 to 200")
            velocity, percent = cfg.Tn_extrude_velocity, cfg.Tn_extrude_percent
            if (not isinstance(velocity, (tuple, list)) or not isinstance(percent, (tuple, list))
                    or not velocity or len(velocity) != len(percent)
                    or not all(type(value) in (int, float) and isfinite(value) and value > 0
                               for value in list(velocity) + list(percent))):
                raise ValueError("owned LOAD flush needs finite positive configured velocity/percent pairs")
            if not isfinite(length * sum(percent)) or length * sum(percent) > 200:
                raise ValueError("owned LOAD configured positive flush must not exceed 200 mm")
            self._bounded_profile()
        elif not self._unknown_source:
            self._bounded_profile()
        velocities, _ = box.get_flush_velocity(self.source, self.target)
        if not all(type(value) in (int, float) and isfinite(value) and value > 0 for value in velocities):
            raise ValueError("material-trial extrusion velocities must be finite and positive")
        if self._unknown_source:
            velocity = cfg.Tn_retrude_velocity
            if (not isinstance(velocity, (tuple, list)) or not velocity
                    or type(velocity[0]) not in (int, float)
                    or not isfinite(velocity[0]) or velocity[0] <= 0):
                raise ValueError("unknown-source unload requires a finite positive retraction velocity")
        heater = box.toolhead.extruder.get_heater()
        temperatures = [cfg.Tn_extrude_temp, box.get_material_target_temp(self.source),
                        box.get_material_target_temp(self.target)]
        if not all(type(value) in (int, float) and isfinite(value)
                   and heater.min_temp < value < heater.max_temp for value in temperatures):
            raise ValueError("material-trial temperature exceeds configured heater limits")
        _, _, temperature = box.get_flush_temp(self.source, self.target)
        if (type(temperature) not in (int, float) or not isfinite(temperature)
                or not heater.min_temp < temperature < heater.max_temp
                or temperature < heater.min_extrude_temp
                or (self._unknown_source and cfg.Tn_extrude_temp < heater.min_extrude_temp)):
            raise ValueError("material-trial temperature exceeds configured heater limits")
        box.cutter._preflight_cut()
        if self.action == "UNLOAD":
            if position[1] > cfg.safe_pos_y:
                raise RuntimeError("material trial requires a safe-Y entry before the original cutter approach")
            box.cutter._require_sensor()
            if box.cut_sensor.cut_present != 0:
                raise RuntimeError("material trial requires an initially released cutter switch")

    def _recipe(self):
        """Read the configured stock service recipe without rewriting its macros.

        The LAN service invokes these bodies line by line and supplies TNN to
        the two loading commands. This bounded trial accepts that known phase
        order only; custom recipes need separate admission before any effects.
        Ordinary BOX commands remain phases, so external clients cannot cause
        a second flush by calling the existing load primitive.
        """
        loading = self.action == "LOAD"
        name = "BOX_LOAD_MATERIAL_WITH_MATERIAL" if loading else "BOX_QUIT_MATERIAL"
        expected = (("M104", "IF_NEED_HOME", "BOX_ERROR_CLEAR", "BOX_CHECK_MATERIAL",
                     "BOX_CUT_MATERIAL", "BOX_RETRUDE_MATERIAL", "BOX_GO_TO_EXTRUDE_POS",
                     "BOX_NOZZLE_CLEAN", "BOX_EXTRUDE_MATERIAL", "BOX_EXTRUDER_EXTRUDE",
                     "BOX_MATERIAL_FLUSH", "RESTORE_POSITION") if loading else
                    ("BOX_ERROR_CLEAR", "BOX_CHECK_MATERIAL", "BOX_CUT_MATERIAL",
                     "BOX_RETRUDE_MATERIAL", "BOX_GO_TO_BOX_EXTRUDE_POS"))
        raw = self.box.boxcfg.config.getsection("gcode_macro " + name).get("gcode", None)
        if not isinstance(raw, str):
            raise ValueError("material trial needs the configured " + name + " recipe")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        phases = tuple(line for line in lines if not line.startswith("RESPOND "))
        if phases != expected:
            raise ValueError("material trial requires the configured stock " + name + " command order")
        gcode = self.box.gcode
        handlers = getattr(gcode, "ready_gcode_handlers", None)
        factory = getattr(gcode, "create_gcode_command", None)
        if not isinstance(handlers, dict) or not callable(factory):
            raise RuntimeError("material trial needs retained G-code command registration")
        recipe = []
        for line in lines:
            command = line.split()[0]
            parameters = {}
            if command in ("BOX_EXTRUDE_MATERIAL", "BOX_EXTRUDER_EXTRUDE"):
                parameters["TNN"] = self.target
                line = command + " TNN=" + self.target
            elif loading and command == "BOX_MATERIAL_FLUSH":
                parameters["LEN"] = str(int(self.box.boxcfg.Tn_extrude))
                line = command + " LEN=" + parameters["LEN"]
            handler = handlers.get(command)
            if not callable(handler):
                raise RuntimeError("material trial is missing retained command " + command)
            recipe.append((command, handler, factory(command, line, parameters)))
        return recipe

    def run(self, action, tnn, confirmed=False):
        box, lifecycle = self.box, self.box.lifecycle
        if (action not in ("LOAD", "UNLOAD") or confirmed is not True
                or not isinstance(tnn, str) or tnn not in box.box_state.Tnn_content):
            raise ValueError("material trial needs LOAD/UNLOAD, physical TNN and CONFIRM=1")
        if self.active or box.preload_trial.active or box.operations.active:
            raise RuntimeError("another CFS trial or operation is still owned")
        if lifecycle is None or not lifecycle.passive:
            raise RuntimeError("material trial requires the passive lifecycle")
        lifecycle.check()
        address = box.box_state.Tnn_content[tnn]["addr"]
        if (box.native is None or box.boxcfg.version != 1
                or box.device_versions.require_supported(address).version != "1.1.3"):
            raise RuntimeError("material trial requires confirmed cfs0 MCU 1.1.3 and encoding 1")
        pause = box._printer.lookup_object("pause_resume", None)
        owner = lifecycle.current_dispatch()
        if pause is None or owner is None:
            raise RuntimeError("material trial requires retained pause state and a dispatch owner")
        context = lifecycle._report_guard(address, operation=False)
        self._lifecycle, self._owner = lifecycle, owner
        self.address, self.target, self.action = address, tnn, action
        self.mask = slot_mask(tnn[2])
        self._deadline = lifecycle.reactor.monotonic() + 900
        self._phase, self._reading = "before", None
        self._head_started = False
        self._unknown_source = (action == "UNLOAD" and box.last_cmd is None
            and box.box_state.get_Tn_data(address, "filament") in (None, "None"))
        self._unknown_retract_step = 0
        self._unknown_retract_pending = None
        configuration = {key: deepcopy(value) for key, value in vars(box.boxcfg).items() if key != "config"}
        token = self._token = object()
        self._cancelled = self._root_done = False
        serial = None

        def check():
            context()
            lifecycle.guard_device(address)
            context()
            try:
                factors = (box.gcode_move.get_status(box._reactor.monotonic())
                    if action == "LOAD" and self._command_name == "BOX_MATERIAL_FLUSH"
                    and self._command_claimed and not self._command_completed else None)
                unsafe = (self._token is not token or self._cancelled
                    or box.lifecycle is not lifecycle or not lifecycle.passive
                    or not self.owns_dispatch() or box.boxcfg.version != 1
                    or configuration != {key: value for key, value in vars(box.boxcfg).items() if key != "config"}
                    or lifecycle.reactor.monotonic() >= self._deadline
                    or (factors is not None and any(type(factors[key]) not in (int, float) or factors[key] != 1
                                                   for key in ("speed_factor", "extrude_factor")))
                    or box.box_state.get_Tn_data(address, "state") != "connect"
                    or not lifecycle.online(address) or not box.native.is_connected
                    or any(lifecycle.online(other) or box.box_state.get_Tn_data(other, "state") == "connect"
                           for other in range(1, 5) if other != address)
                    or box.error is not None or box.inside_error or box.box_state.extrude_err
                    or box.is_use_ending_material or box.inside_auto_refill or box.inside_extrusion_all_materials
                    or box.print_stats.state != "standby" or box.virtual_sdcard.is_active()
                    or box.virtual_sdcard.do_resume_status or pause.is_paused
                    or lifecycle.head_sensor is None
                    or lifecycle.head_sensor.runout_helper.filament_present not in (0, 1)
                    or (action == "LOAD" and not self._head_started
                        and not box.filament_sensor_enable
                        and lifecycle.head_sensor.runout_helper.filament_present != 0)
                    or (action == "LOAD" and self._head_started
                        and lifecycle.head_sensor.runout_helper.filament_present != 1)
                    or (action == "UNLOAD" and not box.cut_sensor.active)
                    or (self._unknown_source and (box.last_cmd is not None
                        or box.box_state.get_Tn_data(address, "filament") not in (None, "None")
                        or (self._phase == "unload" and self._unknown_retract_step < 6 and (
                            box.box_state.get_Tn_inner_data(address, "filament_sensor", subpart="connections") != self.mask
                            or not box.box_state.get_Tn_inner_data(address, "filament_sensor", subpart="material") & self.mask
                            or ((self._unknown_retract_step < 4
                                 or (self._unknown_retract_step == 4 and self._unknown_retract_pending is None))
                                and lifecycle.head_sensor.runout_helper.filament_present != 1)))))
                    or (action == "LOAD" and box.last_cmd not in (None, tnn))
                    or not set("xyz").issubset(box.toolhead.get_status(lifecycle.reactor.monotonic())["homed_axes"])
                    or (serial is not None and (not box.operations.active or box.operations._serial != serial)))
            except (AttributeError, TypeError, KeyError):
                self.cancel()
                raise OperationCancelled("material-trial observations became unavailable")
            if unsafe:
                self.cancel()
                raise OperationCancelled("material-trial ownership or idle observations changed")

        self._guard = check
        try:
            check()
            loaded = action == "UNLOAD"
            if (lifecycle.head_sensor.runout_helper.filament_present != int(loaded)
                    or box.last_cmd != (tnn if loaded and not self._unknown_source else None)
                    or (not loaded and box.box_state.get_Tn_data(address, "filament") not in (None, "None"))
                    or (loaded and not self._unknown_source and box.box_state.get_Tn_data(address, "filament") != tnn)):
                raise RuntimeError("material-trial head/source observations do not match its action")
            # TNN pins the observed hub lane, not a fabricated loaded source.
            # Do not identify the current source from an older last_cmd_tmp.
            self.source = None if self._unknown_source else (tnn if loaded else box.last_cmd_tmp)
            box.operations.begin(self.source, tnn, address)
            serial = box.operations._serial
            self._geometry()
            recipe = self._recipe()
            check()
            mode = self._read("GET_BOX_STATE")[3]
            material = self._read("GET_FILAMENT_SENSOR_STATE", b"\x00")[0]
            connections = self._read("GET_FILAMENT_SENSOR_STATE", b"\x01")[0]
            buffer = self._read("GET_BUFFER_STATE")[0]
            # Neutral buffer 0 is neither limit, not evidence of an empty path.
            if (mode not in ((0, 2) if loaded else (0,)) or not material & self.mask
                    or connections != (self.mask if loaded else 0) or (not loaded and buffer not in (0, 2))):
                raise RuntimeError("fresh material-trial preconditions do not match the selected path")
            if (lifecycle.head_sensor.runout_helper.filament_present != int(loaded)
                    or box.last_cmd != (tnn if loaded and not self._unknown_source else None)
                    or (loaded and not self._unknown_source and box.box_state.get_Tn_data(address, "filament") != tnn)):
                raise RuntimeError("material-trial source changed during the fresh preflight reads")
            self._phase = "unload" if loaded else "load"
            for name, handler, command in recipe:
                # Call registered handlers directly: unexpected Python errors
                # must revoke this trial rather than be swallowed by a nested
                # service dispatcher. Macro aliases still render normally.
                self._command_name, self._command = name, command
                self._command_claimed = self._command_completed = False
                handler(command)
                check()
                if name.startswith("BOX_") or name == "RESTORE_POSITION":
                    if not self._command_completed:
                        raise RuntimeError("material trial did not complete the registered " + name + " phase")
                if (self._unknown_source and name == "BOX_RETRUDE_MATERIAL"
                        and self._unknown_retract_step != 6):
                    raise RuntimeError("unknown-source unload did not complete its scoped native sequence")
                self._command = self._command_name = None
            self._phase = "after"
            check()
            mode = self._read("GET_BOX_STATE")[3]
            material = self._read("GET_FILAMENT_SENSOR_STATE", b"\x00")[0]
            connections = self._read("GET_FILAMENT_SENSOR_STATE", b"\x01")[0]
            buffer = self._read("GET_BUFFER_STATE")[0]
            expected = None if loaded else tnn
            if (mode != (0 if loaded else 2) or connections != (0 if loaded else self.mask)
                    or not material & self.mask or buffer == 1 or (loaded and buffer != 2)
                    or lifecycle.head_sensor.runout_helper.filament_present != int(not loaded)
                    or (loaded and box.cut_sensor.cut_present != 0)
                    or box.last_cmd != expected
                    or (self._unknown_source and box.box_state.get_Tn_data(address, "filament") not in (None, "None"))
                    or (not self._unknown_source and box.box_state.get_Tn_data(address, "filament") != ("None" if loaded else tnn))):
                raise RuntimeError("material-trial final observations do not establish its requested result")
            return {"action": action, "target": tnn, "mode": mode,
                    "material": material, "connections": connections, "buffer": buffer}
        finally:
            self.cancel()
            self._command = self._command_name = None
            self._unknown_retract_pending = None
            self._root_done = True
            if serial is not None and box.operations.active and box.operations._serial == serial:
                box.operations.cancel()
            if self._head_owner is None:
                self._token = None


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
