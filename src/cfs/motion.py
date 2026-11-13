"""Configured CFS fan and positioning helpers."""

import logging


class Motion:
    COMMANDS = {
        "BOX_SAVE_FAN": "cmd_save_fan", "BOX_RESTORE_FAN": "cmd_restore_fan",
        "BOX_BLOW": "cmd_blow", "BOX_MOVE_TO_SAFE_POS": "cmd_move_to_safe_pos",
        "BOX_GO_TO_BOX_EXTRUDE_POS": "cmd_go_to_box_extrude_pos",
        "MOVE_BOX_PRE_CUT_POS": "cmd_pre_cut_pos", "MOVE_BOX_CUT_POS": "cmd_cut_pos",
        "TEST_BOX_EXTRUDE": "cmd_test_extrude",
        "BOX_SAVE_EXTRUDE_POS": "cmd_save_extrude_pos", "BOX_CUSTOM_COMMAND": "cmd_custom_command",
        "BOX_GO_TO_EXTRUDE_POS": "cmd_go_to_extrude_pos", "RESTORE_POSITION": "cmd_restore_position",
    }

    def __init__(self, box):
        self.box = box
        self.fan0_last_value = None
        self.fan2_last_value = None

    def _run(self, guard, command):
        guard()
        self.box.gcode.run_script_from_command(command)
        guard()

    def _fan(self, guard, number):
        guard()
        pin = self.box.lifecycle.printer.lookup_object("output_pin fan%d" % number, None)
        guard()
        return pin

    def _close_fan(self, guard, number):
        pin = self._fan(guard, number)
        if pin is None:
            return
        name = "fan%d_last_value" % number
        if getattr(self, name) is None:
            setattr(self, name, pin.last_value * pin.scale)
        self._run(guard, "SET_PIN PIN=fan%d VALUE=0.0" % number)

    def close_model_fan(self):
        guard = self.box.host_guard()
        self._close_fan(guard, 0)

    def save_fan(self):
        guard = self.box.host_guard()
        self._close_fan(guard, 0)
        self._close_fan(guard, 2)

    def restore_fan(self):
        guard = self.box.host_guard()
        for number in (0, 2):
            if self._fan(guard, number) is None:
                continue
            name = "fan%d_last_value" % number
            value = getattr(self, name)
            if value is not None:
                self._run(guard, "SET_PIN PIN=fan%d VALUE=%.2f" % (number, value))
            setattr(self, name, None)

    def blow(self):
        guard = self.box.host_guard()
        for command in ("SET_PIN PIN=fan0 VALUE=255", "G4 P3000", "M400",
                        "SET_PIN PIN=fan0 VALUE=0"):
            self._run(guard, command)

    def save_printer_accel(self, velocity, accel):
        guard = self.box.host_guard()
        guard()
        self.box.max_velocity, self.box.max_accel = velocity, accel

    def restore_printer_accel(self):
        guard = self.box.host_guard()
        guard()
        box = self.box
        if box.max_velocity and box.max_accel:
            self._run(guard, "G0 F%.2f" % box.max_velocity)
            self._run(guard, "M204 S%d" % box.max_accel)
            box.max_velocity = box.max_accel = None

    def _homed(self, guard):
        guard()
        axes = self.box.toolhead.get_status(self.box.lifecycle.reactor.monotonic())["homed_axes"]
        guard()
        return axes

    def _positions(self, *names):
        cfg = self.box.boxcfg
        missing = [name for name in names if getattr(cfg, name, None) is None]
        if missing:
            raise ValueError("CFS positions not configured: " + ", ".join(missing))
        return [getattr(cfg, name) for name in names]

    def z_move(self):
        guard = self.box.host_guard()
        guard()
        z = self.box.toolhead.get_position()[2]
        guard()
        if z is not None and z < 50.0:
            self.box.move_z = 50.0 - z
            for command in ("G91", "G0 Z%.2f F600" % self.box.move_z, "G90"):
                self._run(guard, command)

    def z_down(self, distance=4.0, speed=600, absolute=False, wait=True):
        guard = self.box.host_guard()
        if "z" not in self._homed(guard):
            return
        guard()
        z = self.box.toolhead.get_position()[2]
        guard()
        maximum = self.box.boxcfg.config.getsection("stepper_z").getfloat("position_max")
        guard()
        if z is None:
            return
        if absolute:
            if z >= distance:
                return
            distance -= z
        elif maximum - z < distance:
            return
        if distance:
            self._run(guard, "G91\n G0 Z%s F%s\n G90" % (distance, speed))
            if wait:
                self._run(guard, "M400")
            if self.box.move_z is None:
                self.box.move_z = 0
            self.box.move_z += distance

    def z_restore(self):
        guard = self.box.host_guard()
        guard()
        if self.box.move_z is not None:
            for command in ("G91", "G0 Z-%.2f F600" % self.box.move_z, "G90"):
                self._run(guard, command)
            self.box.move_z = None

    def move_to_safe_pos(self):
        guard = self.box.host_guard()
        if "xy" not in self._homed(guard):
            return
        y, x = self._positions("safe_pos_y", "clean_right_pos_x")
        for command in ("G90", "G0 Y%.2f F20000.00" % y, "M400",
                        "G0 X%.2f F20000.00" % x):
            self._run(guard, command)
        guard()
        self.box.reset_flushing_sign()
        guard()

    def go_to_box_extrude_pos(self):
        guard = self.box.host_guard()
        x, y = self._positions("retrude_position_x", "retrude_position_y")
        if "x" not in self._homed(guard):
            self._run(guard, "G28 X Y")
            self._run(guard, "BED_MESH_CLEAR")
        self._run(guard, "G90\n G0 Y%.2f F20000.00" % y)
        self._run(guard, "G90\n G0 X%.2f F20000.00" % x)
        self._run(guard, "M400")

    def go_to_extrude_pos(self):
        guard = self.box.host_guard()
        safe_y, x, y, speed = self._positions(
            "safe_pos_y", "extrude_pos_x", "extrude_pos_y", "clean_velocity")
        if "x" not in self._homed(guard):
            self._run(guard, "G28 X Y")
            self._run(guard, "BED_MESH_CLEAR")
        guard()
        self.box.set_flushing_sign()
        guard()
        self.z_down(distance=25, absolute=True, wait=True)
        guard()
        if self.box.move_z:
            self.box.record_tn_move_z(self.box.move_z)
            guard()
        for command in ("G90", "G0 Y%.2f F24000.00" % safe_y, "M400",
                        "G0 X%.2f F24000.00" % x, "M400",
                        "G0 Y%.2f F%.2f \n M400" % (y, speed)):
            self._run(guard, command)

    def do_after_extrude(self):
        guard = self.box.host_guard()
        self.move_to_safe_pos()
        guard()
        self.z_restore()
        guard()
        self.box.record_tn_move_z(0)
        guard()

    def _in_resume(self, guard):
        guard()
        result = self.box.virtual_sdcard.do_resume_status
        guard()
        return result

    def cmd_go_to_extrude_pos(self, gcmd):
        guard = self.box.host_guard()
        if self._in_resume(guard):
            logging.warning("In resume, can not go to extrude pos now")
            return
        try:
            self.go_to_extrude_pos()
        except ValueError as error:
            raise gcmd.error(str(error)) from error

    def cmd_restore_position(self, gcmd):
        guard = self.box.host_guard()
        if self._in_resume(guard):
            logging.warning("In resume, can not restore position now")
            return
        try:
            self.do_after_extrude()
        except ValueError as error:
            raise gcmd.error(str(error)) from error

    def _calibration_move(self, guard, command, clear_mesh):
        if "x" not in self._homed(guard):
            self._run(guard, "G28")
            if clear_mesh:
                self._run(guard, "BED_MESH_CLEAR")
                self._run(guard, "M400")
        self._run(guard, command)
        self._run(guard, "M400")

    def cmd_pre_cut_pos(self, gcmd):
        guard = self.box.host_guard()
        x, y, speed = self._positions("pre_cut_pos_x", "pre_cut_pos_y", "cut_velocity")
        command = "G1 X%.2f Y%.2f F%.2f" % (x, y, speed)
        self._calibration_move(guard, command, True)
        guard()
        self.box.gcode.respond_info("MOVE_BOX_PRE_CUT_POS:" + command)
        guard()

    def cmd_cut_pos(self, gcmd):
        guard = self.box.host_guard()
        x, speed = self._positions("cut_pos_x", "cut_velocity")
        y = self.box.boxcfg.get_real_cut_pos_y()
        command = "G1 X%.2f Y%.2f F%.2f" % (x, y, speed)
        self._calibration_move(guard, command, True)
        guard()
        self.box.gcode.respond_info("MOVE_BOX_PRE_CUT_POS:" + command)
        guard()

    def cmd_test_extrude(self, gcmd):
        guard = self.box.host_guard()
        try:
            x, y, z = self._positions("extrude_pos_x", "extrude_pos_y", "extrude_pos_z")
        except ValueError as error:
            raise gcmd.error(str(error)) from error
        self._calibration_move(guard, "G1 X%.2f Y%.2f Z%.2f" % (x, y, z), False)
        guard()
        self.box.gcode.respond_info("// TEST_BOX_EXTRUDE complete")
        guard()

    def _save_extrude_xy(self, guard, x=None, y=None):
        if x is None or y is None:
            guard()
            x, y = self.box.toolhead.get_position()[:2]
            guard()
        else:
            x, y = float(x), float(y)
        guard()
        self.box.gcode.respond_info("cmd_save_extrude_pos x=%.2f y=%.2f" % (x, y))
        guard()
        for command in ("MODIFY_BOX_CFG EXTRUDE_POS_X=%.2f" % x,
                        "MODIFY_BOX_CFG EXTRUDE_POS_Y=%.2f" % y, "SAVE_BOX_CFG"):
            self._run(guard, command)

    def cmd_save_extrude_pos(self, gcmd):
        guard = self.box.host_guard()
        parameters = gcmd.get_command_parameters()
        self._save_extrude_xy(guard, parameters.get("X"), parameters.get("Y"))

    def cmd_custom_command(self, gcmd):
        guard = self.box.host_guard()
        command = gcmd.get_command_parameters().get("CMD")
        if command not in ("XYZ_ZERO", "COORDINATES_ADJUST_PREPARE",
                           "COORDINATES_ADJUST_SAVE_POS", "Y_SAFE"):
            return
        if command == "COORDINATES_ADJUST_PREPARE":
            try:
                y, x = self._positions("safe_pos_y", "extrude_pos_x")
            except ValueError as error:
                raise gcmd.error(str(error)) from error
        elif command == "Y_SAFE":
            try:
                y, = self._positions("safe_pos_y")
            except ValueError as error:
                raise gcmd.error(str(error)) from error
        guard()
        self.box.custom_command_result = ""
        if command == "XYZ_ZERO":
            self._run(guard, "G28")
        elif command == "COORDINATES_ADJUST_PREPARE":
            for line in ("G0 Z25", "G0 Y%.2f F6000" % y,
                         "G0 X%.2f F6000" % x, "M400"):
                self._run(guard, line)
        elif command == "COORDINATES_ADJUST_SAVE_POS":
            self._save_extrude_xy(guard)
        else:
            self._run(guard, "G0 Y%.2f F6000" % y)
        self.box.custom_command_result = command + "=0"

    def cmd_save_fan(self, gcmd):
        self.save_fan()

    def cmd_restore_fan(self, gcmd):
        self.restore_fan()

    def cmd_blow(self, gcmd):
        self.blow()

    def cmd_move_to_safe_pos(self, gcmd):
        try:
            self.move_to_safe_pos()
        except ValueError as error:
            raise gcmd.error(str(error)) from error

    def cmd_go_to_box_extrude_pos(self, gcmd):
        try:
            self.go_to_box_extrude_pos()
        except ValueError as error:
            raise gcmd.error(str(error)) from error
