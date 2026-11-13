"""Host-side extrusion helpers using the retained Klipper objects."""

import logging
import math


class Extrusion:
    def __init__(self, box):
        self.box = box
        self.scv = self.tcv = None

    def _require_clean_positions(self):
        cfg = self.box.boxcfg
        missing = [name for name in ("extrude_pos_x", "extrude_pos_y", "safe_pos_y")
                   if getattr(cfg, name, None) is None]
        if missing:
            raise ValueError("CFS positions not configured: " + ", ".join(missing))

    def quickly_wait_heating(self, temperature):
        """Set the target until the measured temperature is within two degrees."""
        box = self.box
        guard = box.host_guard()
        while True:
            guard()
            box.gcode.run_script_from_command("M104 S%d" % temperature)
            guard()
            measured = box.toolhead.extruder.get_heater().smoothed_temp
            guard()
            if measured - 2 < temperature < measured + 2 or box.lifecycle.is_shutdown:
                return
            reactor = box.lifecycle.reactor
            guard()
            reactor.pause(reactor.monotonic() + 1)
            guard()

    def cmd_Tn_Extrude(self, gcmd):
        """Forward configured values to the extrusion action, discarding its result."""
        guard = self.box.host_guard()
        cfg = self.box.boxcfg
        guard()
        self.box.Tn_Extrude(cfg.Tn_extrude, cfg.Tn_extrude_velocity,
                            cfg.Tn_extrude_temp, cfg.Tn_extrude_percent)
        guard()

    def get_flush_velocity(self, last_tnn, current_tnn):
        guard = self.box.host_guard()
        guard()
        old = self.box.get_material_max_extrusion_speed(last_tnn)
        guard()
        current = self.box.get_material_max_extrusion_speed(current_tnn)
        guard()
        if old is None or current is None:
            logging.warning("failed to get flush speed from file")
            cfg = self.box.boxcfg
            return cfg.Tn_extrude_velocity, cfg.Tn_extrude_percent
        velocities, percentages = [current * 60], [1]
        for index, (velocity, percent) in enumerate(zip(velocities, percentages)):
            guard()
            self.box.gcode.respond_info("num: %d, velocity: %f, percent %f" %
                                        (index, velocity, percent))
            guard()
        return velocities, percentages

    def get_flush_temp(self, last_tnn, current_tnn):
        guard = self.box.host_guard()
        guard()
        old = self.box.get_material_target_temp(last_tnn)
        guard()
        current = self.box.get_material_target_temp(current_tnn)
        guard()
        temperature = current if current > old else old
        guard()
        self.box.gcode.respond_info("flush_temp: %d" % temperature)
        guard()
        return old, current, temperature

    def cmd_get_flush_velocity_test(self, gcmd):
        self.get_flush_velocity(gcmd.get("LAST"), gcmd.get("NEXT"))

    @staticmethod
    def material_volume_to_length(volume):
        return volume / (0.765625 * math.pi)

    def get_flush_len(self, source, target):
        """Ask Klipper's retained C helper for color purge volume."""
        box = self.box
        guard = box.host_guard()
        if self.scv is None:
            guard()
            scv = box.ffi_main.new("rgb_t *")
            guard()
            tcv = box.ffi_main.new("rgb_t *")
            guard()
            self.scv, self.tcv = scv, tcv
        for name, color, rgb in (("scv", source, self.scv), ("tcv", target, self.tcv)):
            try:
                rgb.r = color[0]
                rgb.g = color[1]
                rgb.b = color[2]
            except Exception:
                logging.warning("convert_%s err, cv: %s", name, color)
                return None
        guard()
        volume = box.ffi_lib.get_flushing_volume(self.scv[0], self.tcv[0])
        guard()
        cfg = box.boxcfg
        guard()
        box.gcode.respond_info("flush_volume: %d, nozzle_volume: %d" %
                               (volume, cfg.nozzle_volume))
        guard()
        length = self.material_volume_to_length(volume + cfg.nozzle_volume) * cfg.flush_multiplier
        guard()
        box.gcode.respond_info("flush_len: %f" % length)
        guard()
        return length

    def cmd_get_flush_len(self, gcmd):
        guard = self.box.host_guard()
        colors = []
        for key in ("SCV", "TCV"):
            value = gcmd.get_command_parameters().get(key)
            if len(value) != 6:
                guard()
                self.box.gcode.respond_info('warning, the length of "%s" is not 6' % key)
                guard()
                return
            color = [int(value[index:index + 2], 16) for index in range(0, len(value), 2)]
            guard()
            self.box.gcode.respond_info("%s: %s" % (key.lower(), color))
            guard()
            colors.append(color)
        self.get_flush_len(*colors)

    def cmd_generate_flush_array(self, gcmd):
        guard = self.box.host_guard()
        value = gcmd.get_command_parameters().get("FLUSH_ARRAY")
        guard()
        self.box.gcode.respond_info("str: %s" % value)
        guard()
        rows = []
        for part in value.strip().split("_"):
            row = [int(item) for item in part.split(",") if item]
            if row:
                rows.append(row)
        guard()
        self.box.gcode.respond_info("flush_array: %s" % rows)
        guard()
        for i in range(4):
            for j in range(4):
                logging.info("flush_array[%d][%d] = %d" % (i, j, rows[i][j]))

    def cmd_material_flush(self, gcmd):
        box = self.box
        guard = box.host_guard()
        guard()
        in_resume = box.if_in_resume()
        guard()
        if in_resume:
            logging.warning("In resume, can not flush material now")
            return
        cfg = box.boxcfg
        length = gcmd.get_int("LEN", cfg.Tn_retrude, minval=0, maxval=200)
        try:
            value = gcmd.get_command_parameters().get("VELOCITY")
            velocity = [int(item) for item in value.strip().split(",")]
        except Exception:
            guard()
            box.gcode.respond_info("warning: get velocity failed, velocity: %s" % None)
            guard()
            velocity = cfg.Tn_extrude_velocity
        value = gcmd.get_command_parameters().get("TEMP", cfg.Tn_extrude_temp)
        try:
            temp = float(value)
        except Exception as error:
            guard()
            box.gcode.respond_info("warning: get temp failed, temp: %s,%s" % (error, value))
            guard()
            temp = cfg.Tn_extrude_temp
        else:
            if not 180 < temp < 300:
                logging.warning("Invalid temperature: %s", temp)
                temp = cfg.Tn_extrude_temp
        try:
            value = gcmd.get_command_parameters().get("PERCENT")
            percent = [int(item) for item in value.strip().split(",")]
        except Exception:
            guard()
            box.gcode.respond_info("warning: get percent failed, percent: %s" % None)
            guard()
            percent = cfg.Tn_extrude_percent
        self._require_clean_positions()
        guard()
        box.Tn_Extrude(length, velocity, temp, percent, tnn=None, error_report=True)
        guard()
        box.nozzle_clean()
        guard()
        old_mode = box.gcode_move.absolute_coord
        guard()
        box.gcode.run_script_from_command("G91\n G0 E-5 F600\n M400")
        guard()
        box.gcode_move.absolute_coord = old_mode

    def Tn_Extrude(self, extrude, velocity, temp, percent=None, tnn=None,
                   error_report=True):
        box, cfg = self.box, self.box.boxcfg
        guard = box.host_guard()
        if (tnn is not None and (percent is None or len(percent) == 1)
                and extrude >= 2 * cfg.buffer_empty_len):
            self._require_clean_positions()

        def call(function, *args):
            guard()
            result = function(*args)
            guard()
            return result

        def run(script):
            call(box.gcode.run_script_from_command, script)

        def wheel(address):
            return call(box.read_measuring_wheel, address)

        def blocked():
            if error_report:
                call(box.lifecycle.report_error, "key_nozzle_blocked_err", {})
            return False

        guard()
        heater = box.toolhead.extruder.get_heater()
        guard()
        if heater.can_extrude is not True:
            run("M109 S%.2f" % temp)
        elif box.print_stats.state == "pause":
            run("M104 S%.2f" % temp)
        run("G91")
        content = call(box.box_state.get_Tnn_content, tnn) if tnn is not None else None
        if percent is None or len(percent) == 1:
            multiplier = 1 if percent is None else percent[0]
            if tnn is None or extrude < 2 * cfg.buffer_empty_len:
                if call(box.check_and_extrude, extrude * multiplier, velocity[0]) is False:
                    return False
            else:
                ok, previous = wheel(content["addr"])
                if not ok:
                    return False
                segments = []
                call(box.cal_flush_list, extrude * multiplier, segments)
                delta = 0
                for index, length in enumerate(segments):
                    run("G90\n G0 X%s F500" % cfg.extrude_pos_x)
                    run("M400")
                    run("G90\n G0 Y%s F500" % cfg.extrude_pos_y)
                    if call(box.check_and_extrude, length, velocity[0]) is False:
                        return False
                    ok, current = wheel(content["addr"])
                    if not ok:
                        return False
                    delta += current - previous
                    previous = current
                    run("G91\n G1 E-3 F4800\n G90")
                    if box.error is not None and not box.is_use_ending_material:
                        return False
                    call(box.nozzle_clean)
                    if index == len(segments) - 1 and box.start_print_retraction_distance:
                        # The source clears this before building the command.
                        box.start_print_retraction_distance = 0
                        run("G91\n G1 E-%s F4800\n G90" % box.start_print_retraction_distance)
                    else:
                        run("G91\n G1 E1.5 F4800\n G90")
                if delta > cfg.diff_length:
                    return blocked()
        elif content is None:
            for index, portion in enumerate(percent):
                run("G0 E%.2f F%.2f" % (extrude * portion, velocity[index]))
                run("M400")
        else:
            ok, previous = wheel(content["addr"])
            if not ok:
                return False
            accumulated = 0
            for index, portion in enumerate(percent):
                length = extrude * portion
                run("G0 E%.2f F%.2f" % (length, velocity[index]))
                run("M400")
                accumulated += length
                if accumulated >= 2 * cfg.buffer_empty_len:
                    ok, current = wheel(content["addr"])
                    if not ok:
                        return False
                    delta = current - previous
                    previous = current
                    accumulated = 0
                    if delta > cfg.diff_length:
                        return blocked()
        run("G90")
        run("M400")
        return True

    def cal_flush_list(self, src_length, cal_length):
        """Append configured purge segments, retaining the original tail rules."""
        cfg = self.box.boxcfg
        first = cfg.box_first_clean_length
        need = cfg.box_need_clean_length
        maximum = cfg.box_need_clean_length_max
        if src_length <= maximum:
            cal_length.append(src_length if src_length >= first else first)
            return
        if need <= 0:
            raise ValueError("box_need_clean_length must be positive for segmented flushing")
        cal_length.append(first)
        remaining = src_length - first
        count = 1
        while remaining >= need:
            cal_length.append(need)
            remaining -= need
            count += 1
        if count == 1 and remaining > 0:
            cal_length.append(need)
        elif count >= 2:
            addition = (remaining - (maximum - first)) / (count - 1)
            if remaining <= maximum - need:
                cal_length[count - 1] += remaining
            elif addition > remaining - (maximum - first):
                cal_length.append(need)
            else:
                cal_length[0] = maximum
                for i in range(count - 1):
                    cal_length[i + 1] += addition
