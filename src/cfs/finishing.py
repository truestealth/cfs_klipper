"""Finishing primitives shared by material-change routes."""


class Finishing:
    def __init__(self, box):
        self.box = box

    def material_flush(self, length, velocity, temp, percent=None, tnn=None,
                       error_report=True):
        """Extrude, move to safe Y, and restore the fan when XY is homed."""
        box = self.box
        address = box.box_state.get_Tnn_content(tnn)["addr"] if tnn is not None else None
        guard = box.host_guard(address)
        if box.boxcfg.safe_pos_y is None:
            raise ValueError("CFS positions not configured: safe_pos_y")
        result = box.Tn_Extrude(length, velocity, temp, percent, tnn,
                                error_report=error_report)
        guard()
        box.gcode.run_script_from_command(
            "G90\n G0 Y%.2f F%.2f\n M400" % (box.boxcfg.safe_pos_y, 15000))
        guard()
        if result is False:
            return False
        homed = box.toolhead.get_status(box._reactor.monotonic())["homed_axes"]
        guard()
        if "xy" not in homed:
            return None
        box.motion.restore_fan()
        guard()
        return True

    def get_flush_length_from_gcode(self, last_tnn, current_tnn):
        """Resolve physical spools back to slicer tools and read its matrix."""
        import math

        box = self.box
        guard = box.host_guard()
        sd = box._printer.lookup_object("virtual_sdcard")
        guard()
        parameters = sd.get_gcode_flush_para()
        guard()
        if parameters is None:
            return None
        multiplier = parameters["flush_multiplier"]
        if multiplier is None:
            multiplier = 1
        matrix = parameters["flush_volumes_matrix"]
        if matrix is None or len(matrix) < 2:
            return None
        size = math.sqrt(len(matrix))
        if not size.is_integer():
            return None
        size = int(size)
        indices = []
        for physical in (last_tnn, current_tnn):
            index = next((int(logical[1:]) for logical, spool in box.box_state.Tn_map.items()
                          if box.box_state.Tnn_map[spool] == physical), None)
            if index is None or index >= size:
                return None
            indices.append(index)
        volume = matrix[indices[0] * size + indices[1]]
        length = box.extrusion.material_volume_to_length(volume) * multiplier
        box.gcode.respond_info("flush_volume: %d, flush_len: %d" % (volume, length))
        guard()
        return length

    def get_flush_len(self, last_tnn, current_tnn):
        box = self.box
        guard = box.host_guard()
        if last_tnn is None:
            return box.boxcfg.Tn_extrude
        if box.print_stats.state == "printing":
            length = self.get_flush_length_from_gcode(last_tnn, current_tnn)
            guard()
            if length is not None:
                return length
        colors = []
        for tnn in (last_tnn, current_tnn):
            content = box.box_state.get_Tnn_content(tnn)
            color = box.box_state.get_Tn_data(content["addr"], "color_value", content["num"])
            if color is None or color in ("-1", "none", "unknown"):
                return box.boxcfg.Tn_extrude
            colors.append([int(color[i:i + 2], 16) for i in range(1, len(color), 2)])
        length = box.get_flush_len(*colors)
        guard()
        if length is None:
            return box.boxcfg.Tn_extrude
        box.gcode.respond_info("flush_len: %f" % length)
        guard()
        return length

    def material_change_flush(self, last_tnn, tnn, error_report=True):
        box = self.box
        guards = [box.host_guard()]
        for tool in (last_tnn, tnn):
            if tool is not None:
                address = box.box_state.get_Tnn_content(tool)["addr"]
                guards.append(box.host_guard(address))

        def guard():
            for check in guards:
                check()

        if box.boxcfg.safe_pos_y is None:
            raise ValueError("CFS positions not configured: safe_pos_y")
        length = self.get_flush_len(last_tnn, tnn)
        guard()
        velocity, percent = box.get_flush_velocity(last_tnn, tnn)
        guard()
        _, target_temp, temp = box.get_flush_temp(last_tnn, tnn)
        guard()
        box.quickly_wait_heating(temp)
        guard()
        result = self.material_flush(length, velocity, temp, percent, tnn,
                                     error_report=error_report)
        guard()
        if result is False:
            return False
        box.gcode.run_script_from_command("M104 S%d" % target_temp)
        guard()
        return True


    def cmd_material_change_flush(self, gcmd):
        box = self.box
        guard = box.host_guard()
        if box.error is None:
            params = gcmd.get_command_parameters()
            source = params.get("LAST_TNN")
            target = params.get("TNN")
            if source is None:
                source = box.last_cmd_tmp
            if target is None:
                target = box.last_cmd
                if target is None:
                    return
            if target not in box.box_state.Tnn_map or (source is not None and source not in box.box_state.Tnn_map):
                return
            result = self.material_change_flush(source, target)
            guard()
            if result:
                return
            box.lifecycle.record_error("macro_flush_err", {})
            guard()
        box.error_next_macro.append(gcmd.get_commandline())

    def is_use_ending_material_flush(self, **kwargs):
        # The drain operation is implemented by a later stage; fail before
        # changing the sensor or claiming that residual material was consumed.
        from .box import MissingProtocolEffect
        raise MissingProtocolEffect("ending-material drain is not implemented")

    def cmd_set_box_mode_print_when_nozzle_clear(self, gcmd):
        """Restore PRINT for the first address with a connection sensor bit."""
        box = self.box
        if (not box.box_state.Tn_data["enable"]
                or box.box_state.Tn_data["state"] != "connect"):
            return
        call = box.retraction._calls(range(1, 5))
        if not call(box.get_filament_sensor_detect):
            return
        for address in range(1, 5):
            masks = box.retraction._masks(call, address)
            if masks is None:
                raise RuntimeError("fresh ordinary filament-sensor observations required")
            connections, _ = masks
            if connections:
                slot = chr(ord("A") + connections.bit_length() - 1)
                call(box.communication_set_box_mode, address, "PRINT", num=slot)
                return

    def box_end(self):
        box = self.box
        guard = box.host_guard()
        if box.last_err:
            return
        # The stock route drains before cutting; this operation is not yet
        # available, so reject it before changing the physical position.
        if box.box_state.Tn_data["enable"] != 0 and box.last_cmd is not None:
            address = box.box_state.get_Tnn_content(box.last_cmd)["addr"]
            guard = box.host_guard(address)
            if box.is_use_ending_material:
                from .box import MissingProtocolEffect
                raise MissingProtocolEffect("ending-material drain is not implemented")
        box.motion.z_down(distance=0.8)
        guard()
        if box.box_state.Tn_data["enable"] == 0:
            box.motion.move_to_safe_pos()
            guard()
            return
        if box.last_cmd is None:
            return
        if box.is_use_ending_material:
            from .box import MissingProtocolEffect
            raise MissingProtocolEffect("ending-material drain is not implemented")
        if box.get_filament_sensor_detect():
            guard()
            result = box.move_to_cut(report_error=False)
            guard()
            if result is False:
                box.last_err = None
                return False
        guard()
        result = box.box_retrude_material(report_error=False)
        guard()
        if result is False:
            content = box.box_state.get_Tnn_content(box.last_cmd)
            box.communication_set_box_mode(content["addr"], "IDLE")
            guard()
            box.last_err = None
            return False
        box.last_cmd_tmp = box.last_cmd
        box.last_cmd = None

    def cmd_box_end(self, gcmd):
        box = self.box
        address = None
        if not box.last_err and box.box_state.Tn_data["enable"] != 0 and box.last_cmd is not None:
            address = box.box_state.get_Tnn_content(box.last_cmd)["addr"]
        guard = box.host_guard(address)
        self.box_end()
        guard()
        box.motion.move_to_safe_pos()
        guard()
