"""Resume helpers and persisted host hints."""

import json
import os


class Resume:
    def __init__(self, box):
        self.box = box
        self._queued = None

    def shutdown(self):
        # Do not erase a target subsequently replaced by another producer.
        if self._queued is not None:
            sd, target = self._queued
            if sd.resume_tnn == target:
                sd.resume_tnn = None
            self._queued = None

    def cmd_add_tnn(self, gcmd):
        box = self.box
        guard = box.lifecycle._report_guard(operation=False)
        target = gcmd.get_command_parameters().get("TNN")
        if target is None:
            return
        if target not in box.box_state.Tnn_map:
            raise ValueError("Invalid deferred CFS tool")
        guard()
        box.gcode.respond_info("cmd_add_tnn = %s" % target)
        sd = box._printer.lookup_object("virtual_sdcard")
        guard()
        sd.resume_tnn = target
        self._queued = sd, target

    def cmd_power_loss_restore(self, gcmd):
        return self.power_loss_restore()

    def power_loss_restore(self):
        box = self.box
        guard = box.lifecycle._report_guard(operation=False)
        guard()
        hints = box.store.recovery_hints()
        guard()
        if "enable" in hints:
            box.gcode.run_script_from_command("BOX_ENABLE_CFS_PRINT ENABLE=%d" % hints["enable"])
            guard()
        if "tnn_map" in hints:
            box.box_state.Tnn_map.update(hints["tnn_map"])
        if "last_cmd" in hints:
            box.last_cmd = hints["last_cmd"]

    def cmd_box_resume_extrude(self, gcmd):
        box, state = self.box, self.box.box_state
        guard = box.lifecycle._report_guard(operation=False)

        def call(fn, *args, **kwargs):
            guard()
            result = fn(*args, **kwargs)
            guard()
            return result

        source = box.last_cmd
        source_content = state.get_Tnn_content(source) if source is not None else None
        checks = [guard]
        if source_content is not None:
            checks.append(box.lifecycle._report_guard(source_content["addr"], operation=False))
        def guard():
            for check in checks:
                check()
        guard()
        sd = call(box._printer.lookup_object, "virtual_sdcard")
        virtual = sd.resume_tnn
        if not virtual and os.path.exists(sd.print_file_name_path):
            with open(sd.print_file_name_path, encoding="utf-8") as stream:
                virtual = json.load(stream).get("tn_cur_tnn")
            guard()
        if not virtual:
            return False
        target = state.Tnn_map[virtual]
        content = state.get_Tnn_content(target)
        checks.append(box.lifecycle._report_guard(content["addr"], operation=False))
        guard()
        if state.get_Tn_data(content["addr"], "state") != "connect":
            return False
        if source_content is None:
            source_content = state.get_Tnn_content(source)
        if source_content != content or box.error not in (None, "filament_err"):
            return
        # A saved source is not evidence of filament physically in the head.
        if not call(box.get_filament_sensor_detect):
            return
        if getattr(box.boxcfg, "has_extrude_pos", 1) == 0:
            box.motion._positions("safe_pos_y")
        move = call(box._printer.lookup_object, "gcode_move")
        speed = move.speed
        run = lambda command: call(box.gcode.run_script_from_command, command)
        run("M400")
        call(box.motion.z_down, distance=25, absolute=True, wait=True)
        if box.move_z:
            call(box.record_tn_move_z, box.move_z)
        call(box.motion.go_to_extrude_pos)
        call(box.communication_set_box_mode, content["addr"], "PRINT", content["num"])
        velocity, _ = call(box.get_flush_velocity, source, target)
        _, _, temperature = call(box.get_flush_temp, source, target)
        call(box.quickly_wait_heating, temperature)
        fan = call(box._printer.lookup_object, "output_pin fan0", None)
        fan_value = fan.last_value * fan.scale if fan is not None else 0
        run("SET_PIN PIN=fan0 VALUE=0")
        run("G4 P3000")
        run("M400")
        if call(box.Tn_Extrude, box.boxcfg.Tn_extrude, velocity, temperature) is False:
            call(state.clear_e_err)
            call(box.lifecycle.record_error, "e_err_tn_extrude_err", {"vtnn": virtual})
            return False
        call(box.nozzle_clean)
        if fan_value > 0:
            run("SET_PIN PIN=fan0 VALUE=%.2f" % fan_value)
        call(box.motion.move_to_safe_pos)
        run("G91\n G1 E-1.5 F4800\n G90")
        call(box.motion.z_restore)
        guard()
        move.speed = speed
