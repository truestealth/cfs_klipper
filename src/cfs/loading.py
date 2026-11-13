"""Recovered host-side loading actions."""

import math


class Loading:
    def __init__(self, box):
        self.box = box

    def move_head(self):
        """Feed five millimeters at the head, retaining absolute E mode."""
        box = self.box
        guard = box.host_guard()
        move = box._printer.lookup_object("gcode_move")
        guard()
        absolute = move.absolute_extrude
        box.gcode.run_script_from_command("M83\n G0 E5 F60")
        guard()
        if absolute:
            box.gcode.run_script_from_command("M82")
            guard()

    def stage6(self, address, target):
        """Send the head-arrival stage for the operation's physical target."""
        box = self.box
        guard = box.host_guard(address)
        content = box.box_state.get_Tnn_content(target)
        if content["addr"] != address:
            raise ValueError("head target does not match its address")
        guard()
        result = box.communication_extrude_process(address, content["num"], stage=6)
        guard()
        return result

    def extruder_extrude(self, tnn):
        """Clear the stock filament-useup latch; the source ignores tnn."""
        box = self.box
        guard = box.host_guard()
        if box.box_state.get_Tn_data(0, uppart="filament_useup"):
            box.box_state.modify_Tn_data(0, 0, uppart="filament_useup")
        guard()
        return True

    def box_extrude_material_stage8(self, tnn):
        """Advance the head and box until the cached buffer is no longer full."""
        from .native import build_request

        box = self.box
        content = box.box_state.get_Tnn_content(tnn)
        address, slot = content["addr"], content["num"]
        guard = box.host_guard(address)
        cfg, reactor = box.boxcfg, box._reactor
        amount = cfg.extrude_material_len_for_box
        # The source passes getfloat output to a strict byte encoder. Preserve
        # exact integral values without silently truncating configured travel.
        if (type(amount) not in (int, float) or not 0 <= amount <= 255
                or not math.isfinite(amount) or amount != int(amount)):
            raise ValueError("box extrusion amount must be an integral byte")
        amount = int(amount)

        def command(script):
            box.gcode.run_script_from_command(script)
            guard()

        def pause(seconds):
            reactor.pause(reactor.monotonic() + seconds)
            guard()

        def buffer():
            previous = box.last_replies.get(address)
            result = box.send_native(build_request(address, "GET_BUFFER_STATE"))
            guard()
            frame = box.last_replies.get(address)
            if (result is not True or frame is None or frame is previous
                    or frame.command != 0x05 or frame.status != 0 or len(frame.data) != 1):
                raise RuntimeError("fresh ordinary buffer observation required")
            return box.box_state.get_Tn_inner_data(address, uppart="buffer")

        for _ in range(cfg.extrude_material_times):
            command("G91\n G0 E%.2f F%.2f\n G90" %
                    (cfg.extrude_material_len_for_extruder, cfg.extrude_material_velocity))
            pause(4)
            observed = buffer()
            if observed is None:
                raise RuntimeError("fresh ordinary buffer observation required")
            if observed != 1:
                return True
            result = box.communication_extrude_process(
                address, slot, stage=7, extrude=amount,
                report_err=False)
            guard()
            if result is False:
                if box.extrude_process_state == "EXTRUDE_ERR9":
                    for _ in range(5):
                        for script in ("G91", "G0 E6 F180", "G90", "M400"):
                            command(script)
                        pause(0.5)
                        observed = buffer()
                        if observed is None:
                            raise RuntimeError("fresh ordinary buffer observation required")
                        if observed != 1:
                            return True
                return False
            command("M400")
            pause(0.5)
            observed = buffer()
            if observed is None:
                raise RuntimeError("fresh ordinary buffer observation required")
            if observed != 1:
                return True
        return False

    def extrude_process_auto_retry_process(self, tnn, retry_type, retry):
        """Run the source's reason-specific loading recovery sequence."""
        box = self.box
        content = box.box_state.get_Tnn_content(tnn)
        address, slot = content["addr"], content["num"]
        guard = box.host_guard(address)

        if retry_type == "auto_retry_filament_sensor":
            box.make_material_loose()
            guard()
            retry[1] += 1
            return self.box_extrude_material_part(tnn, retry)
        if retry_type == "auto_retry_filament_sensor_second":
            retry[2] += 1
            retry[1] = 0
        elif retry_type == "auto_retry_extruder_gear":
            retry[3] += 1
            retry[1] = retry[2] = 0
            box.cutter.move_to_cut()
            guard()
            box.box_retrude_material()
            guard()
            box.gcode.run_script_from_command("M400")
            guard()
            box.motion.go_to_extrude_pos()
            guard()
            result = box.communication_extrude_process(address, slot, stage=0)
            guard()
            if result is False:
                return False
            return self.box_extrude_material_part(tnn, retry)
        elif retry_type != "auto_retry_connection":
            return None

        result = box.communication_set_box_mode(address, "IDLE")
        guard()
        if result is False:
            return False
        result = box.communication_retrude_process(address, slot, "MATERIAL")
        guard()
        if result is False:
            return False
        result = box.communication_extrude_process(address, slot, stage=0)
        guard()
        if result is False:
            return False
        if retry_type == "auto_retry_connection":
            return True
        return self.box_extrude_material_part(tnn, retry)

    def box_extrude_material_part(self, tnn, retry):
        """Poll loading and consume the head callback's saved result."""
        box = self.box
        content = box.box_state.get_Tnn_content(tnn)
        address, slot = content["addr"], content["num"]
        guard = box.host_guard(address)
        box.extrude_tnn = tnn

        def fail(key):
            box.lifecycle.report_error(key, {"address": address, "request_address": address,
                                             "slot": slot})
            box.error_tnn["addr"] = address
            return False

        result = box.communication_extrude_process(address, slot, stage=4)
        guard()
        if result is False:
            return False
        remaining = 90
        while remaining > 0:
            guard()
            if box.extrude_process_stage7_flag_when_retry_loose:
                box.extrude_process_stage7_flag_when_retry_loose = False
                box.operations.head(self.move_head, self.stage6)
                guard()
            elif remaining & 1 == 0:
                result = box.communication_extrude_process(address, slot, stage=5, report_err=False)
                guard()
                if result is False:
                    state = box.extrude_process_state
                    if state == "EXTRUDE_ERR7":
                        return fail("key_joint_err")
                    sensor_retry = state == "EXTRUDE_ERR6"
                    if state == "EXTRUDE_ERR8":
                        sensor_retry = box.get_filament_sensor_detect() is False
                        guard()
                    if sensor_retry:
                        if retry[1] == 0:
                            reason = "auto_retry_filament_sensor"
                        elif retry[1] == 1 and retry[2] == 0:
                            reason = "auto_retry_filament_sensor_second"
                        elif retry[2] == 1:
                            return fail("key_extrude_err2")
                        else:
                            reason = None
                        if reason is not None:
                            return self.extrude_process_auto_retry_process(tnn, reason, retry)
            if box.extrude_process_stage7_flag:
                box.extrude_process_stage7_flag = False
                snapshot = box.extrude_process_stage7_ret
                if snapshot["ret"] is not False:
                    if self.box_extrude_material_stage8(tnn):
                        return True
                    if retry[3] == 0:
                        return self.extrude_process_auto_retry_process(tnn, "auto_retry_extruder_gear", retry)
                    return fail("key_extrude_err3")
                if box.extrude_process_state == "EXTRUDE_ERR9":
                    return fail("key_joint_err")
                if snapshot["extrude_process_ret_state"] == "EXTRUDE_ERR10":
                    snapshot["extrude_process_ret_state"] = None
                    return fail("key_extrude_err5")
                return False
            reactor = box._reactor
            reactor.pause(reactor.monotonic() + 1)
            guard()
            remaining -= 1
        return fail("key_extrude_err2")


    def box_extrude_material(self, tnn):
        """Load a physical slot through the retained box and head actions."""
        box = self.box
        guard = box.host_guard()
        printing = box.print_stats.state in ("printing", "paused")
        if printing:
            if not box.store.update("last_cmd", last_cmd=tnn):
                raise OSError("Unable to save the requested CFS target")
            box.box_state.tn_save_data = box.store.base_data
        content = box.box_state.get_Tnn_content(tnn)
        address, slot = content["addr"], content["num"]
        guard = box.host_guard(address)
        source = box.last_cmd if box.last_cmd is not None else box.last_cmd_tmp
        box.operations.begin(source, tnn, address)
        serial = box.operations._serial
        guard = box.host_guard(address)
        try:
            ready = box.wait_box_preloading(address)
            guard()
            if not ready and not box.is_use_ending_material:
                return False
            if not printing:
                for other in range(1, 5):
                    if box.box_state.get_Tn_data(other, "state") == "connect":
                        box.communication_tighten_up_enable(other, "DISABLE")
                        guard()
            position = box.toolhead.get_position()
            guard()
            if position[1] <= box.boxcfg.safe_pos_y:
                box.motion.go_to_extrude_pos()
                guard()
            available = box.is_material_available(tnn)
            guard()
            if available is False:
                box.lifecycle.report_error("key_filament_err", {"address": address,
                    "request_address": address, "slot": slot})
                box.lifecycle.record_error("filament_err", {"last_tnn": source, "tnn": tnn,
                    "addr": address, "filament_sensor_enable": False})
                return False
            box.get_flush_velocity(source, tnn)
            guard()
            _, _, temperature = box.get_flush_temp(source, tnn)
            guard()
            box.quickly_wait_heating(temperature)
            guard()
            result = box.communication_extrude_process(address, slot, stage=0, report_err=False)
            guard()
            if result is False:
                result = self.extrude_process_auto_retry_process(tnn, "auto_retry_connection", [0, 0, 0, 0])
                guard()
                if result is False:
                    return False
            box.operations.arm_head()
            box.extrude_process_stage7_flag = False
            result = self.box_extrude_material_part(tnn, [0, 0, 0, 0])
            guard()
            box.filament_sensor_enable = False
            if box.operations._heartbeat_saved is not None:
                index, value = box.operations._heartbeat_saved
                box.heart_process_enable[index] = value
                box.operations._heartbeat_saved = None
            if result is False:
                return False
            result = box.communication_set_box_mode(address, "PRINT", slot)
            guard()
            if result is False:
                return False
            if box.print_stats.state not in ("printing", "paused"):
                for other in range(1, 5):
                    if box.box_state.get_Tn_data(other, "state") == "connect":
                        box.communication_tighten_up_enable(other, "ENABLE")
                        guard()
            box.last_cmd = tnn
            box.update_filament_pos(tnn, tnn)
            guard()
            box.box_state.modify_Tn_data(0, 1, uppart="filament")
            return True
        finally:
            if box.operations.active and box.operations._serial == serial:
                box.operations.cancel()
