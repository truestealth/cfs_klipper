"""Configured cutter motion and observations, without transport success claims."""

import logging
from math import isfinite

from .errors import format_error


class CutSensor:
    """Keep the current switch value separately from its sticky observation."""

    def __init__(self, config, switch_pin=None, printer=None):
        if printer is None:
            printer = config.get_printer()
        self.gcode = printer.lookup_object("gcode")
        self.switch_pin = switch_pin
        self.cut_present = 0
        self.cut_happened = False
        self.cut_return_failed = self.cut_succeed_num = 0
        self.cut_release_failed = self.cut_release_succeed = 0
        self.active = True
        if switch_pin:
            printer.load_object(config, "buttons").register_buttons(
                [switch_pin], self._button_handler)
        else:
            self.gcode.respond_info("[box] do not define 'switch_pin' for cutting")

    def _button_handler(self, eventtime, state):
        if not self.active:
            logging.info("CFS ignored cutter switch event: inactive time=%s state=%s", eventtime, state)
            return
        self.cut_present = state
        if state:
            self.cut_happened = True
        self.gcode.respond_info("[box] cut sensor state:%s" % state)

    def clear_cut_happened(self):
        self.cut_happened = False

    def invalidate(self):
        self.active = False
        self.cut_present = 0
        self.clear_cut_happened()

    def activate(self):
        # Initial MCU reports may arrive before ready; shutdown already clears
        # stale observations when disabling this registration.
        self.active = True

    def state(self):
        present = bool(self.cut_present)
        self.gcode.respond_info("[box] cut sensor %sdetected" % ("" if present else "not "))
        return present


class Cutter:
    def __init__(self, box, motion, sensor):
        self.box, self.motion, self.sensor = box, motion, sensor

    def _call(self, check, function, *args):
        check()
        result = function(*args)
        check()
        return result

    def _run(self, check, script):
        self._call(check, self.box.gcode.run_script_from_command, script)

    def _pause(self, check, seconds):
        reactor = self.box._reactor
        self._call(check, reactor.pause, reactor.monotonic() + seconds)

    def _wait_for_cut(self, check):
        """Wait for the live switch after the stroke without moving the head."""
        trial = next((owner for owner in (getattr(self.box, "material_trial", None),
                                         getattr(self.box, "print_trial", None))
                      if owner is not None and owner.active and owner.owns_dispatch()), None)
        active = getattr(getattr(self.box, "lifecycle", None), "passive", None) is False
        if trial is None and not active:
            self._pause(check, 1)
            return bool(self.sensor.cut_present)

        def owned_check():
            check()
            if trial is not None:
                trial.guard()
            self._require_sensor()

        reactor = self.box._reactor
        deadline = reactor.monotonic() + 5.0
        self._pause(owned_check, 1)
        while True:
            owned_check()
            now = reactor.monotonic()
            if now > deadline:
                return False
            if self.sensor.cut_present:
                return True
            if now >= deadline:
                return False
            self._pause(owned_check, min(.1, deadline - now))

    def _homed(self, check):
        return self._call(check, self.box.toolhead.get_status,
                          self.box._reactor.monotonic())["homed_axes"]

    def _limits(self, check):
        check()
        limits = self.box.toolhead.max_accel, self.box.toolhead.max_accel_to_decel
        check()
        return limits

    def _restore_limits(self, check, limits):
        self._run(check, "SET_VELOCITY_LIMIT ACCEL=%.3f ACCEL_TO_DECEL=%.3f" % limits)

    def _require_sensor(self):
        if not self.sensor.switch_pin or not self.sensor.active:
            raise RuntimeError("a configured active cutter switch is required")

    def _preflight_cut(self):
        cfg = self.box.boxcfg
        if type(cfg.cut_run_count) is not int:
            raise ValueError("cut_run_count must be an integer")
        required = []
        if self.sensor.switch_pin:
            self._require_sensor()
            # A missed switch moves to this fallback even without a tray.
            required.extend(("retrude_position_x", "retrude_position_y"))
        for name in required:
            if getattr(cfg, name, None) is None:
                raise ValueError("cutting requires configured %s" % name)
        check = self.box.host_guard()
        if (self.sensor.switch_pin and self.sensor.cut_present
                and getattr(getattr(self.box, "lifecycle", None), "passive", None) is False):
            raise RuntimeError("cut entry needs a released cutter switch")
        if (self._call(check, self.box.get_filament_sensor_detect) is False
                or "x" not in self._homed(check)):
            return
        position = getattr(self.box.toolhead, "get_position", None)
        if not callable(position):
            raise RuntimeError("cut entry requires the current toolhead position")
        current = self._call(check, position)
        safe_y = getattr(cfg, "safe_pos_y", None)
        if safe_y is None and cfg.has_extrude_pos != 1:
            safe_y = getattr(cfg, "pre_cut_pos_y", None)
        try:
            current_y = current[1]
        except (TypeError, IndexError, KeyError) as error:
            raise RuntimeError("cut entry requires the current toolhead Y") from error
        if (type(current_y) not in (int, float) or not isfinite(current_y)
                or type(safe_y) not in (int, float) or not isfinite(safe_y)):
            raise RuntimeError("cut entry requires finite current and configured safe Y")
        if current_y > safe_y:
            # The stock X-first approach requires its caller's completed clean
            # route. Reject a rear entry rather than inventing a recovery move.
            raise RuntimeError("cut entry requires Y at or before configured safe Y")

    def move_to_cut(self, report_error=True):
        """Run bounded strokes; True without a switch is only a motion result.

        The sensed branch retains the stock sticky observation and five return
        attempts. Failed sensing deliberately leaves the temporary limits set.
        The stock approach is X-first from a validated entry. Normal sensed
        completion stays at the cut position; the enclosing operation returns
        the head afterward. This helper does not invent a release movement.
        """
        box, cfg = self.box, self.box.boxcfg
        check = box.host_guard()
        self._preflight_cut()
        count = cfg.cut_run_count
        if "x" not in self._homed(check):
            self._run(check, "G28 X Y")
            self._run(check, "BED_MESH_CLEAR")
            self._run(check, "M400")
            self._preflight_cut()
        if self._call(check, box.get_filament_sensor_detect) is False:
            logging.warning("No filament found.")
            return True
        if cfg.has_extrude_pos == 1:
            self._call(check, self.motion.z_move)
            self._run(check, "G90")
            self._run(check, "G0 X%.2f F%.2f" % (cfg.pre_cut_pos_x, cfg.cut_velocity))
            self._run(check, "G0 X%.2f Y%.2f F%.2f" % (
                cfg.pre_cut_pos_x, cfg.pre_cut_pos_y, cfg.cut_velocity))
        check()
        can_extrude = box.toolhead.extruder.get_heater().can_extrude
        check()
        if can_extrude:
            self._run(check, "G91")
            self._run(check, "G1 E-8 F4800")
            self._run(check, "G90")
        limits = self._limits(check)
        self._run(check, "SET_VELOCITY_LIMIT ACCEL=%s ACCEL_TO_DECEL=%s" % (
            cfg.cut_accel, cfg.cut_accel_to_decel))
        if not self.sensor.switch_pin:
            for _ in range(3 * count):
                if "x" not in self._homed(check):
                    self._run(check, "G28 X Y")
                self._run(check, "G0 X%.2f F%.2f" % (cfg.pre_cut_pos_x, cfg.cut_velocity))
                self._run(check, "G0 Y%.2f F%.2f" % (cfg.pre_cut_pos_y, cfg.cut_velocity))
                self._run(check, "G0 Y%.2f F%.2f" % (cfg.get_real_cut_pos_y(), cfg.cut_velocity))
            if "x" not in self._homed(check):
                self._run(check, "G28 X Y")
            self._run(check, "G0 Y%.2f F%.2f" % (cfg.pre_cut_pos_y, cfg.cut_velocity))
            self._run(check, "M400")
            check()
            can_extrude = box.toolhead.extruder.get_heater().can_extrude
            check()
            if can_extrude:
                self._run(check, "G91")
                self._run(check, "G1 E-2 F600")
                self._run(check, "G90")
            self._run(check, "M400")
            self._call(check, box.gcode.respond_info, "[box] cut to return OK")
        else:
            if "x" not in self._homed(check):
                self._run(check, "G28 X Y")
            self.sensor.clear_cut_happened()
            for _ in range(5):
                for _ in range(count):
                    self._run(check, "G90")
                    self._run(check, "G0 Y%.2f F%.2f\n M400" % (cfg.pre_cut_pos_y, cfg.cut_velocity))
                    self._run(check, "G0 X%.2f F%.2f\n M400" % (cfg.pre_cut_pos_x, cfg.cut_velocity))
                    self._run(check, "G0 Y%.2f F%.2f\n M400" % (cfg.get_real_cut_pos_y(), cfg.cut_velocity))
                if self._wait_for_cut(check):
                    self._call(check, box.gcode.respond_info, "[box] cut to return OK")
                    break
                if getattr(getattr(box, "lifecycle", None), "passive", None) is False:
                    self._restore_limits(check, limits)
                    logging.warning("The CFS cutter switch did not confirm the current stroke")
                    if report_error:
                        # Keep the stock UI error while the head may still be
                        # against the cutter. The normal reporter can move it.
                        message = format_error("key_cut_err", {})
                        self._call(check, box.gcode.respond_raw, message)
                        box.last_err = message
                    return False
                trial = getattr(box, "material_trial", None)
                if trial is not None and trial.active:
                    trial.guard()
                    raise RuntimeError("material trial stops after the first missed cutter observation")
                trial = getattr(box, "print_trial", None)
                if trial is not None and trial.active and trial.owns_dispatch():
                    trial.cancel()
                    raise RuntimeError("print trial stops after the first missed cutter observation")
                self._run(check, "G0 Y%.2f F%.2f" % (cfg.pre_cut_pos_y, cfg.cut_velocity))
                self._call(check, box.gcode.respond_info, "[box] cut to return failed")
                self._call(check, self.motion.go_to_box_extrude_pos)
            if not self.sensor.cut_happened:
                self._call(check, box.gcode.respond_info, "Cut sensor not triggered.")
                if report_error:
                    self._call(check, box.lifecycle.report_error, "key_cut_err", {})
                return False
            self.sensor.clear_cut_happened()
            self._call(check, box.gcode.respond_info, "Cut sensor triggered.")
        self._restore_limits(check, limits)
        return True

    def cut_material(self):
        check = self.box.host_guard()
        self._preflight_cut()
        self._call(check, self.motion.close_model_fan)
        result = self.move_to_cut()
        check()
        return result

    def _clean_strokes(self, check):
        y = self.box.boxcfg.extrude_pos_y
        for target, speed in ((y - 10, 8000), (y, 12000),
                              (y - 10, 12000), (y, 12000)):
            self._run(check, "G4 P200")
            self._run(check, "G90\n G0 Y%s F%d" % (target, speed))
        self._run(check, "G4 P200")

    def strike_clean_box(self, wait=True):
        check = self.box.host_guard()
        if getattr(self.box.boxcfg, "extrude_pos_y", None) is None:
            raise ValueError("cleaning requires configured extrude_pos_y")
        self._call(check, self.box.set_flushing_sign)
        self._run(check, "G90")
        self._clean_strokes(check)
        if wait:
            self._run(check, "M400")

    def nozzle_clean(self):
        cfg = self.box.boxcfg
        check = self.box.host_guard()
        for name in ("extrude_pos_x", "extrude_pos_y", "safe_pos_y"):
            if getattr(cfg, name, None) is None:
                raise ValueError("cleaning requires configured %s" % name)
        self._call(check, self.box.set_flushing_sign)
        self._run(check, "G90")
        limits = self._limits(check)
        self._run(check, "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=30000")
        self._run(check, "G90\n G0 X%s F500" % cfg.extrude_pos_x)
        self._run(check, "M400")
        self._run(check, "SET_PIN PIN=fan0 VALUE=255")
        self._run(check, "G4 P3000")
        self._clean_strokes(check)
        self._run(check, "SET_PIN PIN=fan0 VALUE=0")
        self._run(check, "G90\n G0 Y%.2f F%.2f" % (cfg.safe_pos_y, 12000))
        self._run(check, "M400")
        self._restore_limits(check, limits)
        self._call(check, self.box.reset_flushing_sign)

    def find_cut_pos(self):
        """Save Y only after an observed switch event during this search."""
        box, cfg = self.box, self.box.boxcfg
        check = box.host_guard()
        box.find_cut_pos = 0.0
        self._require_sensor()
        if not box.has_actual_head_sensor():
            raise RuntimeError("cut calibration requires the configured head sensor")
        if self._call(check, box.get_filament_sensor_detect):
            box.find_cut_pos = -1.0
            self._call(check, box.gcode.respond_raw,
                       format_error("key_cut_filament_err", {}))
            return
        retreat = cfg.retrude_position_y
        section = cfg.config.getsection("stepper_y")
        ymin = section.getfloat("position_min")
        ymax = section.getfloat("position_max", 228)
        start = ymax - cfg.cut_find_start_offset_y
        if not all(ymin <= value <= ymax for value in (start, retreat, cfg.pre_cut_pos_y)):
            raise ValueError("cut calibration positions exceed the configured Y limits")
        xsection = cfg.config.getsection("stepper_x")
        if not xsection.getfloat("position_min") <= cfg.pre_cut_pos_x <= xsection.getfloat("position_max"):
            raise ValueError("cut calibration pre-position exceeds the configured X limits")
        self._run(check, "G28 Y X" if "z" in self._homed(check) else "G28")
        limits = self._limits(check)
        self._run(check, "SET_VELOCITY_LIMIT ACCEL=30000 ACCEL_TO_DECEL=30000")
        self._run(check, "G1 Y%s F2000" % cfg.pre_cut_pos_y)
        self._run(check, "G1 X%s F2000" % cfg.pre_cut_pos_x)
        self._run(check, "M400")
        self.sensor.clear_cut_happened()
        found = None
        for index in range(60):
            target = start + 0.1 * index
            if not ymin <= target <= ymax:
                raise ValueError("cut search reached the configured Y limits")
            self._run(check, "G90\nG1 Y%s F%s \n M400" % (target, cfg.cut_velocity))
            if self.sensor.cut_happened:
                self.sensor.clear_cut_happened()
                check()
                found = box.gcode_move.last_position[1]
                check()
                self._run(check, "MODIFY_BOX_CFG CUT_POS_Y=%.2f" % found)
                self._run(check, "SAVE_BOX_CFG")
                break
            self._pause(check, 0.1)
        self._run(check, "G1 Y%s F2000 \n M400" % retreat)
        self._restore_limits(check, limits)
        box.find_cut_pos = found if found else -2.0

    def hall_zero(self):
        self._require_sensor()
        check = self.box.host_guard()
        config = self.box.boxcfg.config
        xsection = config.getsection("stepper_x")
        xmin = xsection.getfloat("position_min")
        if not xmin <= -3 <= xsection.getfloat("position_max"):
            raise ValueError("Hall zero start exceeds the configured X limits")
        ysection = config.getsection("stepper_y")
        if not ysection.getfloat("position_min") <= 20 <= ysection.getfloat("position_max"):
            raise ValueError("Hall zero start exceeds the configured Y limits")
        if "x" not in self._homed(check):
            self._run(check, "G28 X Y")
        self._run(check, "G1 Y20 F6000")
        self._run(check, "G1 X-3 F6000")
        self.sensor.cut_hall_move = 0
        while self._call(check, self.sensor.state):
            if -3 + self.sensor.cut_hall_move - 0.1 < xmin:
                raise RuntimeError("Hall zero search reached the configured X minimum")
            self._run(check, "G91")
            self._run(check, "G1 x-0.1 F600")
            self._run(check, "G90")
            self._run(check, "M400")
            self.sensor.cut_hall_move -= 0.1
        self.sensor.cut_hall_move -= 4

    def hall_test(self):
        self._require_sensor()
        cfg, sensor = self.box.boxcfg, self.sensor
        check = self.box.host_guard()
        limits = self._limits(check)
        self._run(check, "SET_VELOCITY_LIMIT ACCEL=500 ACCEL_TO_DECEL=300")
        self._run(check, "G0 X%.2f Y%.2f F%.2f" % (cfg.cut_pos_x, cfg.get_real_cut_pos_y(), cfg.cut_velocity))
        self._run(check, "M400")
        if sensor.cut_present:
            sensor.cut_return_failed += 1
            self._call(check, self.box.gcode.respond_info,
                       "cut to return failed index: %d" % sensor.cut_return_failed)
        else:
            sensor.cut_succeed_num += 1
            self._call(check, self.box.gcode.respond_info,
                       "self.cut_succeed_num: %d" % sensor.cut_succeed_num)
        self._run(check, "G0 X%.2f Y%.2f F%.2f" % (cfg.pre_cut_pos_x, cfg.pre_cut_pos_y, cfg.cut_velocity))
        self._run(check, "M400")
        # The diagnostic increments these same counters on both strokes.
        if sensor.cut_present:
            sensor.cut_succeed_num += 1
            self._call(check, self.box.gcode.respond_info,
                       "self.release_succeed_num: %d" % sensor.cut_release_succeed)
        else:
            sensor.cut_return_failed += 1
            self._call(check, self.box.gcode.respond_info,
                       "cself.release_failed_num: %d" % sensor.cut_release_failed)
        self._run(check, "G0 X100 Y%.2f F%.2f" % (cfg.pre_cut_pos_y, cfg.cut_velocity))
        self._run(check, "M400")
        self._restore_limits(check, limits)
