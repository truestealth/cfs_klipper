"""Complete box-side unloading and the separate explicit-source diagnostic."""

from .native import build_request


class Retraction:
    def __init__(self, box):
        self.box = box

    def _calls(self, addresses):
        # Keep every sampled device generation, including earlier addresses in
        # an inferred-source scan, through all subsequent cooperative effects.
        guards = [self.box.host_guard()]
        guards.extend(self.box.host_guard(address) for address in addresses)

        def call(function, *args, **kwargs):
            for guard in guards:
                guard()
            result = function(*args, **kwargs)
            for guard in guards:
                guard()
            return result
        return call

    def _connected(self):
        return [address for address in range(1, 5)
                if self.box.box_state.get_Tn_data(address, "state") == "connect"]

    def _send(self, call, address, command, data=b""):
        return call(self.box.send_native, build_request(address, command, data))

    def _idle(self, call, address):
        return self._send(call, address, "SET_BOX_MODE", b"\x00\x01")

    def _retract(self, call, address, slot, trigger, report_error=True):
        return call(self.box.communication_retrude_process, address, slot,
                    trigger, report_err=report_error)

    def _sample(self, call, address, command, data=b""):
        previous = self.box.last_replies.get(address)
        result = self._send(call, address, command, data)
        frame = self.box.last_replies.get(address)
        # Sensor events, timeouts and retained cache values cannot identify an
        # unknown source or select a buffer-relief operation.
        if (result is not True or frame is None or frame is previous
                or frame.command != build_request(address, command, data).command
                or frame.status != 0 or len(frame.data) != 1):
            return None
        return frame.data[0]

    def _masks(self, call, address):
        connections = self._sample(call, address, "GET_FILAMENT_SENSOR_STATE", b"\x01")
        material = self._sample(call, address, "GET_FILAMENT_SENSOR_STATE", b"\x00")
        if connections is None or material is None:
            return None
        return connections, material

    def _error(self, call, key, address=None, slot=None, report_error=True):
        if report_error:
            facts = {}
            if address is not None:
                facts["address"] = address
                facts["request_address"] = address
            if slot is not None:
                facts["slot"] = slot
            call(self.box.lifecycle.report_error, key, facts)
        return False

    def _remember_address(self, address, report_error):
        if report_error:
            self.box.error_tnn["addr"] = address
        return False

    def _wait(self, call, address):
        return (call(self.box.wait_box_preloading, addr=address)
                or self.box.is_use_ending_material)

    def _e_move(self, call, command):
        for script in ("G91", command, "G90"):
            call(self.box.gcode.run_script_from_command, script)

    def _finish(self, call, report_error):
        box = self.box
        box.last_cmd_tmp = box.last_cmd
        call(box.update_filament_pos, box.last_cmd, None)
        box.last_cmd = None
        # This is the original bookkeeping order. A present head still fails
        # after clearing last_cmd; that field is not physical unloading proof.
        if call(box.get_filament_sensor_detect):
            return self._error(call, "key_retrude_err4", report_error=report_error)
        return True

    def box_retrude_material(self, report_error=True):
        box = self.box
        guard = box.host_guard()
        if box.last_cmd is not None:
            content = box.box_state.get_Tnn_content(box.last_cmd)
            guard()
            return self._known(self._calls([content["addr"]]), content, report_error)
        addresses = self._connected()
        call = self._calls(addresses)
        if call(box.get_filament_sensor_detect):
            return self._unknown_head(call, addresses, report_error)
        for address in addresses:
            masks = self._masks(call, address)
            if masks is None:
                return False
            connections, material = masks
            for slot in "ABCD":
                if not connections & material & (1 << "ABCD".index(slot)):
                    continue
                if not self._wait(call, address):
                    # This branch stores the address even with reporting off.
                    return self._remember_address(address, True)
                if self._idle(call, address) is False:
                    return self._remember_address(address, True)
                if self._retract(call, address, slot, "MATERIAL", report_error) is False:
                    return self._remember_address(address, report_error)
        return True

    def _unknown_head(self, call, addresses, report_error):
        box, cfg = self.box, self.box.boxcfg
        heater = call(box.toolhead.extruder.get_heater)
        if heater.can_extrude is not True:
            call(box.gcode.run_script_from_command, "M109 S%.2f" % cfg.Tn_extrude_temp)
        for address in addresses:
            masks = self._masks(call, address)
            if masks is None:
                return False
            connections, material = masks
            if not connections:
                continue
            if not self._wait(call, address):
                return False
            if self._idle(call, address) is False:
                return False
            result = self._retract(call, address, "0", "BUFFER", report_error)
            if result is False:
                if call(box.get_filament_sensor_detect):
                    self._error(call, "key_retrude_err3", report_error=report_error)
                    return self._remember_address(address, report_error)
                self._idle(call, address)
                for slot in "ABCD":
                    bit = 1 << "ABCD".index(slot)
                    if connections & material & bit:
                        result = self._retract(call, address, slot, "MATERIAL", report_error)
                        if result is False:
                            return self._remember_address(address, report_error)
                    elif connections & bit and not material & bit:
                        self._error(call, "key_material_err", address, slot, report_error)
                        self._remember_address(address, report_error)
                        return result
                # Continue scanning after cleanup. The final head observation,
                # not the earlier shared BUFFER result, selects the tail.
                continue
            heater = call(box.toolhead.extruder.get_heater)
            if heater.can_extrude is not True:
                call(box.quickly_wait_heating, cfg.Tn_extrude_temp)
            for slot in "ABCD":
                if not connections & material & (1 << "ABCD".index(slot)):
                    continue
                call(box.Tn_Extrude, -20, cfg.Tn_retrude_velocity, cfg.Tn_extrude_temp)
                self._e_move(call, "G1 E-30 F2400")
                if self._retract(call, address, slot, "MATERIAL", report_error) is False:
                    return self._remember_address(address, report_error)
                return True
        if call(box.get_filament_sensor_detect):
            return self._error(call, "key_sensor_err", report_error=report_error)
        return self._finish(call, report_error)

    def _known(self, call, content, report_error):
        box = self.box
        address, slot = content["addr"], content["num"]
        if not self._wait(call, address):
            return False
        if self._idle(call, address) is False:
            return False
        velocity, unused_percent = call(box.get_flush_velocity, box.last_cmd, box.last_cmd)
        unused_old, unused_new, temperature = call(box.get_flush_temp, box.last_cmd, box.last_cmd)
        if call(box.get_filament_sensor_detect):
            buffered = self._retract(call, address, slot, "BUFFER", report_error)
            # The assist return is deliberately ignored in this initial path.
            call(box.Tn_Extrude, -20, velocity, temperature)
            if buffered is False:
                if call(box.get_filament_sensor_detect):
                    return self._error(call, "key_retrude_err3", report_error=report_error)
                if self._idle(call, address) is False:
                    return False
            self._e_move(call, "G0 E-30 F2400")
        result = self._retract(call, address, slot, "MATERIAL", False)
        if result is not False:
            return self._finish(call, report_error)
        if not report_error:
            return False

        self._idle(call, address)
        buffer = self._sample(call, address, "GET_BUFFER_STATE")
        if buffer is None:
            return False
        if buffer != 1:
            call(box.communication_extrude_process, address, slot, stage=0)
            call(box.gcode.run_script_from_command, "G91\n G0 E5 F60\n G90")
            call(box.communication_extrude_process, address, slot, stage=6)
            if self._sample(call, address, "GET_BUFFER_STATE") is None:
                return False
        call(box.make_material_loose)
        if self._idle(call, address) is False:
            return False
        if call(box.get_filament_sensor_detect):
            for unused in range(3):
                result = self._retract(call, address, slot, "BUFFER")
                if result is False:
                    if box.error == "state_err":
                        return False
                    if call(box.get_filament_sensor_detect):
                        return self._error(call, "key_retrude_err3")
                    if self._idle(call, address) is False:
                        return False
                    break
                if not call(box.get_filament_sensor_detect):
                    break
                if call(box.Tn_Extrude, box.boxcfg.Tn_retrude, velocity, temperature) is False:
                    call(box.box_state.clear_e_err)
                    return False
        self._e_move(call, "G0 E-10 F60")
        if self._retract(call, address, slot, "MATERIAL") is False:
            return False
        return self._finish(call, report_error)

    def retrude_material_with_tnn(self, tnn=None):
        """Run the distinct diagnostic route, retaining its ignored returns.

        Its normal tail clears source bookkeeping even after a false sender
        result. This is command compatibility, never evidence of a clear path.
        """
        box, cfg = self.box, self.box.boxcfg
        content = box.box_state.Tnn_content.get(tnn) if isinstance(tnn, str) else None
        addresses = [content["addr"]] if content is not None else self._connected()
        call = self._calls(addresses)
        call(box.motion.go_to_extrude_pos)
        if call(box.get_filament_sensor_detect):
            call(box.Tn_Extrude, cfg.Tn_retrude, cfg.Tn_retrude_velocity, cfg.Tn_extrude_temp)
            self._e_move(call, "G0 E-15 F120")
        if tnn is None:
            for address in addresses:
                self._idle(call, address)
                self._retract(call, address, "0", "MATERIAL")
        else:
            content = call(box.box_state.get_Tnn_content, tnn)
            address = content["addr"]
            self._idle(call, address)
            self._send(call, address, "CTRL_CONNECTION_MOTOR_ACTION", b"\x00")
            self._retract(call, address, content["num"], "MATERIAL")
        box.last_cmd_tmp = box.last_cmd
        box.last_cmd = None
